"""Safe Sync: small rclone guardrail CLI."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from safe_sync.api import DaemonApiServer, DaemonApiState, api_request
from safe_sync.daemon import DaemonState, WatchDaemon, WatchSettings, scan_tree
from safe_sync.watcher import NativeWatcher
from safe_sync.transfer import (
    JobConflictError,
    JobStore,
    LinkStore,
    TransferError,
    compare_inventories,
    generation_record,
    generation_remote_dir,
    join_remote_scope,
    local_inventory,
    inventories_equal,
    normalize_subpath,
    parse_combined_report,
    parse_rclone_inventory,
    resolve_local_scope,
    select_inventory,
)
from safe_sync.service import (
    backend_autostart_cmd,
    backend_autostart_status_text,
    install_script,
    launchd_plist,
    systemd_unit,
    os_name,
    service_cmd,
    service_status_text,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_HOME = Path.home() / ".safe-sync"
DEFAULT_CONFIG = CONFIG_HOME / "config.json"
LEGACY_CONFIG = Path.home() / ".config" / "safe-sync" / "config.json"
DEFAULT_STATUS = Path.home() / ".local" / "state" / "safe-sync" / "status.json"
DEFAULT_SOCKET = Path.home() / ".local" / "state" / "safe-sync" / "daemon.sock"
DEFAULT_LOG_DIR = Path.home() / ".local" / "log" / "safe-sync"
DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "safe-sync"
DEFAULT_FILTER = CONFIG_HOME / "filter.txt"
DEFAULT_RCLONE_CONFIG = CONFIG_HOME / "rclone.conf"
TEMPLATE_FILTER = PROJECT_ROOT / "config" / "filter.txt"
TEMPLATE_INTERNAL_FILTER = PROJECT_ROOT / "config" / "internal-filter.txt"
USER_GUIDE = PROJECT_ROOT / "docs" / "user-guide.md"

RATE_LIMIT_PATTERNS = ("too_many_requests", "too many requests", "rate limit", "rate_limit", "retry-after")
AUTH_FAILURE_PATTERNS = (
    "invalid_access_token",
    "expired_access_token",
    "invalid token",
    "token has expired",
    "authorization has been revoked",
    "authentication failed",
    "unauthorized",
)
RATE_LIMIT_EXIT = 75
LAST_COMMAND_OUTPUT = ""
INTERNAL_FILTER_MARKER = "# Safe Sync internal work data"
INTERNAL_FILTER_RULES = (
    f"{INTERNAL_FILTER_MARKER}\n"
    "- .safe-sync-work/**\n"
    "- **/.safe-sync-work/**\n"
)


class RateLimitedError(RuntimeError):
    def __init__(self, message: str, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def stamp() -> str:
    return dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")


def machine_name() -> str:
    name = socket.gethostname().split(".")[0] or platform.node() or "unknown"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-").lower() or "unknown"


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        hint = "Run: safe-sync init-config"
        if path == DEFAULT_CONFIG and LEGACY_CONFIG.exists():
            hint = f"Legacy config exists at {LEGACY_CONFIG}; run: safe-sync migrate-config"
        raise SystemExit(f"Config not found: {path}\n{hint}")
    return json.loads(path.read_text())


def atomic_write_text(path: Path, text: str) -> None:
    """Replace a text file atomically, preserving the last valid version."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def save_status(config: dict[str, Any], **updates: Any) -> None:
    status_path = Path(config.get("status_path", DEFAULT_STATUS)).expanduser()
    status_path.parent.mkdir(parents=True, exist_ok=True)
    previous: dict[str, Any] = {}
    if status_path.exists():
        try:
            previous = json.loads(status_path.read_text())
        except json.JSONDecodeError:
            previous = {}
    previous.update(updates)
    previous["updated_at"] = now_iso()
    atomic_write_text(status_path, json.dumps(previous, indent=2, sort_keys=True) + "\n")


def log_path(config: dict[str, Any]) -> Path:
    log_dir = Path(config.get("log_dir", DEFAULT_LOG_DIR)).expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"safe-sync-{dt.date.today().isoformat()}.log"


def socket_path(config: dict[str, Any]) -> Path:
    return Path(config.get("socket_path", DEFAULT_SOCKET)).expanduser()


def append_log(config: dict[str, Any], line: str) -> None:
    path = log_path(config)
    try:
        with path.open("a") as fh:
            fh.write(line)
    except OSError as exc:
        print(f"warning: could not write log {path}: {exc}", file=sys.stderr)


def recent_log_text(config: dict[str, Any], max_chars: int = 12000) -> str:
    path = log_path(config)
    if not path.exists():
        return ""
    text = path.read_text(errors="replace")
    return text[-max_chars:]


def daemon_api(config: dict[str, Any], command: str, *, _timeout_seconds: float = 5.0, **payload: Any) -> dict[str, Any]:
    request = {"command": command, **payload}
    return api_request(socket_path(config), request, timeout_seconds=_timeout_seconds)


def text_looks_rate_limited(text: str) -> bool:
    lower = text.lower()
    return any(pattern in lower for pattern in RATE_LIMIT_PATTERNS)


def rate_limit_retry_after_seconds(text: str, default: int = 300) -> int:
    patterns = (
        r"retry-after[:= ]+(\d+)",
        r"trying again in (\d+) seconds",
        r"try again in (\d+) seconds",
    )
    lower = text.lower()
    for pattern in patterns:
        match = re.search(pattern, lower)
        if match:
            return max(1, int(match.group(1)))
    return default


def future_iso(seconds: int) -> str:
    return (dt.datetime.now(dt.timezone.utc).astimezone() + dt.timedelta(seconds=seconds)).isoformat(timespec="seconds")


def looks_rate_limited(config: dict[str, Any]) -> bool:
    return text_looks_rate_limited(recent_log_text(config))


def run_command(
    config: dict[str, Any],
    cmd: list[str],
    dry_run: bool = False,
    progress_callback: Callable[[str], None] | None = None,
) -> int:
    global LAST_COMMAND_OUTPUT
    LAST_COMMAND_OUTPUT = ""
    log = log_path(config)
    env = rclone_env(config)
    log.parent.mkdir(parents=True, exist_ok=True)
    header = f"\n[{now_iso()}] $ {' '.join(cmd)}\n"
    with log.open("a") as fh:
        fh.write(header)
        fh.flush()
        if progress_callback is None:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
            output = result.stdout or ""
            LAST_COMMAND_OUTPUT = output
            print(output, end="")
            fh.write(output)
            fh.write(f"[{now_iso()}] exit={result.returncode} dry_run={dry_run}\n")
            return int(result.returncode)
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        lines: list[str] = []
        assert process.stdout is not None
        try:
            for line in process.stdout:
                lines.append(line)
                print(line, end="")
                fh.write(line)
                if progress_callback:
                    progress_callback(line.rstrip("\n"))
            returncode = process.wait()
            output = "".join(lines)
            LAST_COMMAND_OUTPUT = output
            fh.write(f"[{now_iso()}] exit={returncode} dry_run={dry_run}\n")
            return int(returncode)
        finally:
            if process.poll() is None:
                process.kill()


def rclone_bin(config: dict[str, Any]) -> str:
    configured = config.get("rclone_bin")
    if configured:
        return str(Path(configured).expanduser())
    found = shutil.which("rclone")
    if found:
        return found
    for candidate in (Path("/opt/homebrew/bin/rclone"), Path("/usr/local/bin/rclone")):
        if candidate.exists():
            return str(candidate)
    raise SystemExit("rclone not found in PATH")


def rclone_env(config: dict[str, Any]) -> dict[str, str] | None:
    """Return an explicit environment for a Safe Sync-owned rclone config."""
    configured = config.get("rclone_config")
    if not configured:
        # Existing configs predate dedicated rclone ownership. Preserve their
        # working global configuration until they are explicitly migrated.
        return None
    env = os.environ.copy()
    env["RCLONE_CONFIG"] = str(Path(configured).expanduser())
    return env


def filter_file(config: dict[str, Any]) -> Path:
    return Path(config["filter_file"]).expanduser()


def filter_args(config: dict[str, Any]) -> list[str]:
    return [
        "--filter-from", str(TEMPLATE_INTERNAL_FILTER),
        "--filter-from", str(filter_file(config)),
    ]


def effective_filter_fingerprint(config: dict[str, Any]) -> str:
    path = filter_file(config)
    digest = hashlib.sha256()
    digest.update(INTERNAL_FILTER_RULES.encode())
    if path.exists():
        digest.update(path.read_bytes())
    else:
        digest.update(f"missing:{path}".encode())
    return digest.hexdigest()


def lock_file(config: dict[str, Any]) -> Path:
    return Path(config.get("lock_file", Path.home() / ".local" / "state" / "safe-sync" / "safe-sync.lock")).expanduser()


def resolved_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def unsafe_local_path_reason(path: Path) -> str | None:
    home = Path.home().resolve()
    root = Path(path.anchor or "/").resolve()
    dangerous = {root, home, home / "projects"}
    if path in dangerous:
        return f"refusing unsafe local_path: {path}"
    try:
        if path == Path.cwd().resolve():
            return f"refusing current working directory as local_path: {path}"
    except OSError:
        pass
    return None


def validate_local_path(config: dict[str, Any]) -> None:
    normalized = normalized_config(config)
    # Callers validating a prospective folder pass it at the top level before
    # it is attached to the active profile. Respect that candidate list.
    folders = config.get("folders") if isinstance(config.get("folders"), list) else normalized["folders"]
    for folder in folders:
        if not folder.get("enabled", True):
            continue
        path = resolved_path(str(folder["local_path"]))
        reason = unsafe_local_path_reason(path)
        if reason and not normalized.get("allow_unsafe_local_path") and not folder.get("allow_unsafe_local_path"):
            command = f"safe-sync setup --folder {shlex.quote(str(path))} --allow-unsafe-local-path"
            raise SystemExit(f"{reason}\nIf this broad folder is intentional, rerun:\n  {command}")


def safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower()).strip("-")
    return cleaned or "default"


def bounded_seconds(name: str, value: int, minimum: int, maximum: int) -> int:
    if value < minimum or value > maximum:
        raise SystemExit(f"{name} must be between {minimum} and {maximum} seconds")
    return value


def default_install_id() -> str:
    return str(uuid.uuid4())


def remote_join(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.strip('/')}"


def legacy_folder(config: dict[str, Any]) -> dict[str, Any]:
    machine_id = str(config.get("machine_id") or config.get("machine") or machine_name())
    local_path = str(config.get("local_path", "~/test_sync"))
    folder_id = safe_id(config.get("folder_id") or Path(local_path).expanduser().name or "default")
    remote_base = str(config.get("remote_base", "dropbox:computer-backups/test"))
    remote_root = str(config.get("remote_root", remote_join(remote_base, f"{machine_id}/{folder_id}")))
    trash_root = str(config.get("trash_root", remote_join(remote_base, f".trash/{machine_id}/{folder_id}")))
    return {
        "id": folder_id,
        "label": config.get("folder_label", folder_id),
        "local_path": local_path,
        "remote_root": remote_root,
        "trash_root": trash_root,
        "remote_path": remote_root.split(":", 1)[1].lstrip("/") if ":" in remote_root else remote_root,
        "trash_path": trash_root.split(":", 1)[1].lstrip("/") if ":" in trash_root else trash_root,
        "filter_file": str(config.get("filter_file", DEFAULT_FILTER)),
        "enabled": True,
    }


def legacy_profile(config: dict[str, Any]) -> dict[str, Any]:
    machine_id = str(config.get("machine_id") or config.get("machine") or machine_name())
    return {
        "id": safe_id(str(config.get("profile_id") or machine_id)),
        "label": str(config.get("profile_label") or config.get("machine_label") or machine_id),
        "machine": machine_id,
        "machine_id": machine_id,
        "machine_label": str(config.get("machine_label") or machine_id),
        "install_id": str(config.get("install_id") or default_install_id()),
        "remote_base": str(config.get("remote_base", "dropbox:computer-backups/test")),
        "filter_file": str(config.get("filter_file", DEFAULT_FILTER)),
        "folders": list(config.get("folders") or [legacy_folder(config)]),
    }


def normalized_profile(profile: dict[str, Any], config_defaults: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(profile)
    machine_id = str(normalized.get("machine_id") or normalized.get("machine") or normalized.get("id") or machine_name())
    profile_id = safe_id(str(normalized.get("id") or machine_id))
    normalized["id"] = profile_id
    normalized.setdefault("label", str(normalized.get("machine_label") or machine_id))
    normalized.setdefault("machine", machine_id)
    normalized["machine_id"] = machine_id
    normalized.setdefault("machine_label", machine_id)
    normalized.setdefault("install_id", default_install_id())
    normalized.setdefault("remote_base", str(config_defaults.get("remote_base", "dropbox:computer-backups/test")))
    normalized.setdefault("filter_file", str(config_defaults.get("filter_file", DEFAULT_FILTER)))
    if not normalized.get("folders"):
        normalized["folders"] = []
    for folder in normalized["folders"]:
        folder["id"] = safe_id(str(folder.get("id") or Path(str(folder.get("local_path", "default"))).name))
        folder.setdefault("label", folder["id"])
        folder.setdefault("enabled", True)
        folder.setdefault("filter_file", str(folder.get("filter_file") or normalized.get("filter_file", DEFAULT_FILTER)))
        folder.setdefault("remote_path", f"{machine_id}/{folder['id']}")
        folder.setdefault("trash_path", f".trash/{machine_id}/{folder['id']}")
        folder.setdefault("remote_root", remote_join(str(normalized["remote_base"]), str(folder["remote_path"])))
        folder.setdefault("trash_root", remote_join(str(normalized["remote_base"]), str(folder["trash_path"])))
    return normalized


def normalized_config(config: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(config)
    normalized.setdefault("filter_file", str(DEFAULT_FILTER))
    normalized.setdefault("socket_path", str(DEFAULT_SOCKET))
    normalized.setdefault("status_path", str(DEFAULT_STATUS))
    normalized.setdefault("log_dir", str(DEFAULT_LOG_DIR))
    normalized.setdefault("state_root", str(Path(normalized.get("status_path", DEFAULT_STATUS)).expanduser().parent))
    normalized.setdefault("lock_file", str(Path.home() / ".local" / "state" / "safe-sync" / "safe-sync.lock"))
    normalized.setdefault("poll_interval_seconds", 5)
    normalized.setdefault("debounce_seconds", 20)
    normalized.setdefault("min_interval_seconds", 120)
    normalized.setdefault("fallback_interval_seconds", 1800)
    normalized.setdefault("rate_limit_backoff_seconds", 300)
    normalized.setdefault("preserve_metadata", False)

    raw_profiles = normalized.get("profiles")
    if isinstance(raw_profiles, list) and raw_profiles:
        profiles = [normalized_profile(profile, normalized) for profile in raw_profiles]
    else:
        profiles = [normalized_profile(legacy_profile(normalized), normalized)]

    active_profile_id = safe_id(str(normalized.get("active_profile_id") or profiles[0]["id"]))
    if not any(profile["id"] == active_profile_id for profile in profiles):
        active_profile_id = profiles[0]["id"]
    active_profile = next(profile for profile in profiles if profile["id"] == active_profile_id)

    normalized["profiles"] = profiles
    normalized["active_profile_id"] = active_profile_id
    normalized["profile_id"] = active_profile["id"]
    normalized["profile_label"] = str(active_profile.get("label", active_profile["id"]))
    normalized["machine"] = active_profile["machine_id"]
    normalized["machine_id"] = active_profile["machine_id"]
    normalized["machine_label"] = str(active_profile.get("machine_label", active_profile["machine_id"]))
    normalized["install_id"] = str(active_profile.get("install_id", default_install_id()))
    normalized["remote_base"] = str(active_profile["remote_base"])
    normalized["folders"] = active_profile["folders"]
    normalized["filter_file"] = str(active_profile.get("filter_file", normalized["filter_file"]))
    return normalized


def write_config(path: Path, config: dict[str, Any]) -> dict[str, Any]:
    normalized = normalized_config(config)
    active_profile = next(profile for profile in normalized["profiles"] if profile["id"] == normalized["active_profile_id"])
    persisted = {
        "active_profile_id": normalized["active_profile_id"],
        "debounce_seconds": normalized["debounce_seconds"],
        "fallback_interval_seconds": normalized["fallback_interval_seconds"],
        "filter_file": normalized["filter_file"],
        "lock_file": normalized["lock_file"],
        "log_dir": normalized["log_dir"],
        "machine": active_profile["machine_id"],
        "machine_id": active_profile["machine_id"],
        "machine_label": active_profile["machine_label"],
        "min_interval_seconds": normalized["min_interval_seconds"],
        "poll_interval_seconds": normalized["poll_interval_seconds"],
        "preserve_metadata": normalized["preserve_metadata"],
        "profiles": normalized["profiles"],
        "rate_limit_backoff_seconds": normalized["rate_limit_backoff_seconds"],
        "remote_base": active_profile["remote_base"],
        "status_path": normalized["status_path"],
        "socket_path": normalized["socket_path"],
        "state_root": normalized["state_root"],
    }
    persisted["install_id"] = active_profile["install_id"]
    persisted["folders"] = active_profile["folders"]
    for key in ("rclone_bin", "rclone_config"):
        if normalized.get(key):
            persisted[key] = normalized[key]
    atomic_write_text(path, json.dumps(persisted, indent=2, sort_keys=True) + "\n")
    return normalized


def active_profile(config: dict[str, Any]) -> dict[str, Any]:
    normalized = normalized_config(config)
    return next(profile for profile in normalized["profiles"] if profile["id"] == normalized["active_profile_id"])


def enabled_folders(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [folder for folder in normalized_config(config)["folders"] if folder.get("enabled", True)]


def folder_config(config: dict[str, Any], folder: dict[str, Any]) -> dict[str, Any]:
    merged = normalized_config(config)
    merged.update({
        "folder_id": folder["id"],
        "local_path": folder["local_path"],
        "remote_root": folder["remote_root"],
        "trash_root": folder["trash_root"],
        "filter_file": folder.get("filter_file", merged.get("filter_file", str(DEFAULT_FILTER))),
    })
    return merged


def selected_folders(config: dict[str, Any], folder_id: str | None, all_folders: bool = False) -> list[dict[str, Any]]:
    folders = enabled_folders(config)
    if all_folders or folder_id is None:
        return folders
    wanted = safe_id(folder_id)
    matches = [folder for folder in folders if folder["id"] == wanted]
    if not matches:
        known = ", ".join(folder["id"] for folder in folders) or "none"
        raise SystemExit(f"Unknown or disabled folder '{folder_id}'. Known enabled folders: {known}")
    return matches


def registry_path(config: dict[str, Any]) -> str:
    cfg = normalized_config(config)
    base = str(cfg["remote_base"])
    return remote_join(base, f".registry/computers/{cfg['machine_id']}.json")


def registry_dir(config: dict[str, Any]) -> str:
    return remote_join(str(normalized_config(config)["remote_base"]), ".registry/computers")


def registry_filename(machine_id: str) -> str:
    return f"{machine_id}.json"


def registry_doc(config: dict[str, Any]) -> dict[str, Any]:
    cfg = normalized_config(config)
    return {
        "machine_id": cfg["machine_id"],
        "machine_label": cfg.get("machine_label", cfg["machine_id"]),
        "install_id": cfg.get("install_id"),
        "safe_sync_version": "0.1",
        "last_seen": now_iso(),
        "folders": [
            {
                "id": folder["id"],
                "label": folder.get("label", folder["id"]),
                "remote_path": folder["remote_path"],
                "trash_path": folder["trash_path"],
                "enabled": bool(folder.get("enabled", True)),
                "filter_fingerprint": effective_filter_fingerprint(folder_config(cfg, folder)),
            }
            for folder in cfg["folders"]
        ],
    }


def config_for_profile(config: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    merged = normalized_config(config)
    profile_cfg = normalized_profile(profile, merged)
    merged["active_profile_id"] = profile_cfg["id"]
    merged["profile_id"] = profile_cfg["id"]
    merged["profile_label"] = str(profile_cfg.get("label", profile_cfg["id"]))
    merged["machine"] = profile_cfg["machine_id"]
    merged["machine_id"] = profile_cfg["machine_id"]
    merged["machine_label"] = str(profile_cfg.get("machine_label", profile_cfg["machine_id"]))
    merged["install_id"] = str(profile_cfg.get("install_id", default_install_id()))
    merged["remote_base"] = str(profile_cfg["remote_base"])
    merged["folders"] = profile_cfg["folders"]
    merged["filter_file"] = str(profile_cfg.get("filter_file", merged.get("filter_file", DEFAULT_FILTER)))
    return merged


class Lock:
    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "Lock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _attempt in range(2):
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                pid = self.path.read_text(errors="ignore").strip()
                if self._is_stale(pid):
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                raise SystemExit(f"Safe Sync already running (lock {self.path}, pid {pid or 'unknown'})")
        raise SystemExit(f"Safe Sync could not acquire lock {self.path}")

    @staticmethod
    def _is_stale(pid: str) -> bool:
        if not pid.isdigit():
            return True
        result = subprocess.run(
            ["ps", "-p", pid, "-o", "command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        command = result.stdout.strip()
        # A numeric PID alone is not sufficient: macOS can reuse it for an
        # unrelated program after Safe Sync exits.
        return result.returncode != 0 or "safe-sync" not in command

    def __exit__(self, *_exc: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def preflight(config: dict[str, Any]) -> None:
    remote = config["remote_root"].split(":", 1)[0] + ":"
    cmd = [rclone_bin(config), "about", remote, "--timeout", "20s", "--contimeout", "10s", "--retries", "1", "--low-level-retries", "1"]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=45, env=rclone_env(config))
    if result.returncode != 0:
        output = result.stdout or ""
        append_log(config, f"[{now_iso()}] preflight failed:\n{output}\n")
        if text_looks_rate_limited(output):
            retry_after = rate_limit_retry_after_seconds(output, int(config.get("rate_limit_backoff_seconds", 300)))
            raise RateLimitedError(f"Dropbox rate limited Safe Sync; cooling down for {retry_after}s", retry_after)
        if text_looks_auth_failure(output):
            raise SystemExit(reconnect_dropbox_message())
        raise SystemExit("Remote preflight failed; see log")


def text_looks_auth_failure(output: str) -> bool:
    lowered = output.lower()
    return any(pattern in lowered for pattern in AUTH_FAILURE_PATTERNS)


def reconnect_dropbox_message() -> str:
    return "Dropbox authorization is invalid or revoked. Reconnect with: safe-sync connect-dropbox"


def backup_cmd(config: dict[str, Any], dry_run: bool, report_path: Path | None = None) -> list[str]:
    remote = config["remote_root"].rstrip("/")
    local = str(Path(config["local_path"]).expanduser())
    trash = f"{config['trash_root'].rstrip('/')}/{stamp()}"
    cmd = [
        rclone_bin(config), "sync", local, remote,
        *filter_args(config),
        "--backup-dir", trash,
        "--create-empty-src-dirs",
        "--stats", "10s",
        "--timeout", "30s", "--contimeout", "10s",
        "--retries", "1", "--low-level-retries", "1", "--retries-sleep", "5s",
        "--log-level", "INFO",
    ]
    if config.get("preserve_metadata"):
        cmd.append("--metadata")
    if report_path is not None:
        cmd.extend(["--combined", str(report_path)])
    if dry_run:
        cmd.append("--dry-run")
    return cmd


def copy_cmd(config: dict[str, Any], src: str, dst: str, dry_run: bool, selected_paths: list[str] | None = None) -> list[str]:
    cmd = [
        rclone_bin(config), "copy", src, dst,
        *filter_args(config),
        "--create-empty-src-dirs",
        "--stats", "10s",
        "--timeout", "30s", "--contimeout", "10s",
        "--retries", "1", "--low-level-retries", "1", "--retries-sleep", "5s",
        "--log-level", "INFO",
    ]
    for selected_path in selected_paths or []:
        normalized = selected_path.strip().strip("/")
        if not normalized or ".." in normalized.split("/"):
            raise SystemExit(f"unsafe selected path: {selected_path}")
        if selected_path.endswith("/"):
            cmd.extend(["--include", f"/{normalized}/**"])
        else:
            cmd.extend(["--include", f"/{normalized}"])
    if config.get("preserve_metadata"):
        cmd.append("--metadata")
    if dry_run:
        cmd.append("--dry-run")
    return cmd


def ensure_filter_template(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        shutil.copyfile(TEMPLATE_FILTER, path)


def default_config(machine: str) -> dict[str, Any]:
    ensure_filter_template(DEFAULT_FILTER)
    remote_base = "dropbox:computer-backups"
    return {
        "active_profile_id": safe_id(machine),
        "remote_base": remote_base,
        "profiles": [
            {
                "id": safe_id(machine),
                "label": machine,
                "machine": machine,
                "machine_id": machine,
                "machine_label": machine,
                "install_id": default_install_id(),
                "remote_base": remote_base,
                "filter_file": str(DEFAULT_FILTER),
                "folders": [],
            }
        ],
        "filter_file": str(DEFAULT_FILTER),
        "rclone_config": str(DEFAULT_RCLONE_CONFIG),
        "socket_path": str(DEFAULT_SOCKET),
        "status_path": str(DEFAULT_STATUS),
        "log_dir": str(DEFAULT_LOG_DIR),
        "state_root": str(DEFAULT_STATE_DIR),
        "lock_file": str(Path.home() / ".local" / "state" / "safe-sync" / "safe-sync.lock"),
        "poll_interval_seconds": 5,
        "debounce_seconds": 20,
        "min_interval_seconds": 120,
        "fallback_interval_seconds": 1800,
        "rate_limit_backoff_seconds": 300,
        "preserve_metadata": False,
    }


def config_view(config: dict[str, Any], config_path: Path | None = None) -> dict[str, Any]:
    normalized = normalized_config(config)
    return {
        "config_path": str((config_path or DEFAULT_CONFIG).expanduser()),
        "profile_id": normalized["profile_id"],
        "profile_label": normalized["profile_label"],
        "active_profile_id": normalized["active_profile_id"],
        "machine_id": normalized["machine_id"],
        "machine_label": normalized["machine_label"],
        "remote_base": normalized["remote_base"],
        "rclone_config": normalized.get("rclone_config"),
        "socket_path": normalized["socket_path"],
        "state_root": normalized["state_root"],
        "poll_interval_seconds": int(normalized["poll_interval_seconds"]),
        "debounce_seconds": int(normalized["debounce_seconds"]),
        "min_interval_seconds": int(normalized["min_interval_seconds"]),
        "fallback_interval_seconds": int(normalized["fallback_interval_seconds"]),
        "rate_limit_backoff_seconds": int(normalized["rate_limit_backoff_seconds"]),
        "folders": normalized["folders"],
        "profiles": [
            {
                "id": profile["id"],
                "label": profile.get("label", profile["id"]),
                "machine_id": profile["machine_id"],
                "machine_label": profile.get("machine_label", profile["machine_id"]),
                "remote_base": profile["remote_base"],
                "folder_count": len(profile.get("folders", [])),
                "active": profile["id"] == normalized["active_profile_id"],
            }
            for profile in normalized["profiles"]
        ],
    }


def restart_backend_if_running(config_path: Path | None = None) -> None:
    """Reload launchd only when the installed configuration changed."""
    if config_path is not None and config_path.expanduser().resolve() != DEFAULT_CONFIG.expanduser().resolve():
        return
    if os_name() != "Darwin":
        return
    plist = Path.home() / "Library" / "LaunchAgents" / "com.safe-sync.daemon.plist"
    if not plist.exists():
        return
    if service_status_text() != "service: running":
        return
    subprocess.run(["launchctl", "unload", str(plist)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    subprocess.run(["launchctl", "load", str(plist)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def cmd_init_config(args: argparse.Namespace) -> int:
    path = Path(args.config).expanduser()
    if path.exists() and not args.force:
        raise SystemExit(f"Config already exists: {path}")
    config = default_config(args.machine or machine_name())
    write_config(path, config)
    print(path)
    return 0


def cmd_migrate_config(args: argparse.Namespace) -> int:
    src = Path(args.from_path).expanduser()
    dst = Path(args.config).expanduser()
    if not src.exists():
        raise SystemExit(f"Legacy config not found: {src}")
    if dst.exists() and not args.force:
        raise SystemExit(f"Config already exists: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    ensure_filter_template(DEFAULT_FILTER)
    config = json.loads(src.read_text())
    config["filter_file"] = str(DEFAULT_FILTER)
    config.setdefault("poll_interval_seconds", 5)
    config.setdefault("debounce_seconds", 20)
    config.setdefault("min_interval_seconds", 120)
    config.setdefault("fallback_interval_seconds", 1800)
    config.setdefault("rate_limit_backoff_seconds", 300)
    write_config(dst, config)
    print(f"migrated {src} -> {dst}")
    return 0


def set_active_remote_base(config: dict[str, Any], remote_base: str) -> None:
    if ":" not in remote_base or not remote_base.split(":", 1)[0].strip():
        raise SystemExit("remote must look like remote-name:path, for example dropbox:computer-backups")
    config["remote_base"] = remote_base.rstrip("/")
    for folder in config["folders"]:
        folder["remote_root"] = remote_join(config["remote_base"], str(folder["remote_path"]))
        folder["trash_root"] = remote_join(config["remote_base"], str(folder["trash_path"]))
    for profile in config["profiles"]:
        if profile["id"] == config["active_profile_id"]:
            profile["remote_base"] = config["remote_base"]
            profile["folders"] = config["folders"]
            break


def add_setup_folder(config: dict[str, Any], local_path: str, allow_unsafe_local_path: bool = False) -> str:
    path = resolved_path(local_path)
    if not path.is_dir():
        raise SystemExit(f"Setup folder does not exist or is not a directory: {path}")
    folder_id = safe_id(path.name)
    existing = next((folder for folder in config["folders"] if folder["id"] == folder_id), None)
    if existing:
        if resolved_path(str(existing["local_path"])) != path:
            raise SystemExit(f"Folder id '{folder_id}' already belongs to {existing['local_path']}")
        if allow_unsafe_local_path:
            existing["allow_unsafe_local_path"] = True
        validate_local_path({**config, "folders": [existing]})
        return folder_id
    folder = {
        "id": folder_id,
        "label": path.name,
        "local_path": str(path),
        "remote_path": f"{config['machine_id']}/{folder_id}",
        "trash_path": f".trash/{config['machine_id']}/{folder_id}",
        "filter_file": str(config.get("filter_file", DEFAULT_FILTER)),
        "enabled": True,
    }
    if allow_unsafe_local_path:
        folder["allow_unsafe_local_path"] = True
    folder["remote_root"] = remote_join(str(config["remote_base"]), str(folder["remote_path"]))
    folder["trash_root"] = remote_join(str(config["remote_base"]), str(folder["trash_path"]))
    validate_local_path({**config, "folders": [folder]})
    config["folders"].append(folder)
    for profile in config["profiles"]:
        if profile["id"] == config["active_profile_id"]:
            profile["folders"] = config["folders"]
            break
    return folder_id


def cmd_setup(args: argparse.Namespace) -> int:
    """Finish the local, repeatable portion of first-time configuration."""
    config_path = Path(args.config).expanduser()
    if not config_path.exists():
        write_config(config_path, default_config(args.machine or machine_name()))
        print(f"created config: {config_path}")
    config = normalized_config(load_config(config_path))
    if args.remote:
        set_active_remote_base(config, args.remote)
    added = [add_setup_folder(config, value, args.allow_unsafe_local_path) for value in args.folder]
    updated = write_config(config_path, config)
    print(f"profile: {updated['profile_id']}")
    print(f"remote: {updated['remote_base']}")
    if added:
        print(f"folders added: {', '.join(added)}")

    if args.skip_remote_check:
        print("remote check: skipped")
        return 0

    if not enabled_folders(updated):
        raise SystemExit("No folders are configured. Rerun setup with --folder /path/to/folder.")

    remote_name = updated["remote_base"].split(":", 1)[0] + ":"
    remotes = subprocess.run(
        [rclone_bin(updated), "listremotes"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        env=rclone_env(updated),
    )
    if remotes.returncode != 0 or remote_name not in remotes.stdout.splitlines():
        raise SystemExit(
            f"Dropbox remote '{remote_name}' is not configured. Run 'safe-sync connect-dropbox' "
            "to create it, then rerun 'safe-sync setup'. For a headless server, use "
            "'safe-sync connect-dropbox --headless'."
        )
    preflight(folder_config(updated, enabled_folders(updated)[0]))
    registry_code = update_registry(updated)
    if registry_code != 0:
        raise SystemExit("Remote registry update failed; see logs")
    print("remote preflight: ok")
    if not args.skip_start:
        service_cmd("start")
    return 0


def current_status(config: dict[str, Any]) -> dict[str, Any]:
    status_path = Path(config.get("status_path", DEFAULT_STATUS)).expanduser()
    if not status_path.exists():
        return {}
    try:
        return json.loads(status_path.read_text())
    except json.JSONDecodeError:
        return {}


def active_backoff_remaining_seconds(config: dict[str, Any]) -> tuple[str, float] | None:
    status = current_status(config)
    if status.get("state") != "backoff":
        return None
    until = parse_status_time(status.get("backoff_until"))
    if until is None:
        return None
    remaining = (until - dt.datetime.now(dt.timezone.utc).astimezone()).total_seconds()
    if remaining > 0:
        return until.isoformat(timespec="seconds"), remaining
    return None


def active_backoff_until(config: dict[str, Any]) -> str | None:
    active = active_backoff_remaining_seconds(config)
    return active[0] if active else None


def save_rate_limit_status(config: dict[str, Any], message: str, retry_after_seconds: int, *, queued: bool) -> None:
    save_status(
        config,
        state="backoff",
        folder_id=config.get("folder_id"),
        last_warning=message,
        last_error=None,
        backoff_seconds=retry_after_seconds,
        backoff_until=future_iso(retry_after_seconds),
        queued_backup=queued,
        last_finish=now_iso(),
    )


def run_backup_with_config(config: dict[str, Any], dry_run: bool) -> int:
    with Lock(lock_file(config)):
        existing_backoff_until = active_backoff_until(config)
        if existing_backoff_until:
            save_status(
                config,
                state="backoff",
                folder_id=config.get("folder_id"),
                last_warning=f"Backup queued; Dropbox cooldown is active until {existing_backoff_until}",
                last_error=None,
                backoff_until=existing_backoff_until,
                queued_backup=True,
                last_command="backup",
                last_finish=now_iso(),
            )
            print(f"Dropbox cooldown active until {existing_backoff_until}; backup queued")
            return RATE_LIMIT_EXIT
        save_status(
            config,
            state="syncing",
            folder_id=config.get("folder_id"),
            last_start=now_iso(),
            last_command="backup",
            last_error=None,
            last_warning=None,
        )
        report_path = backup_report_path(config)
        try:
            preflight(config)
            code = run_command(config, backup_cmd(config, dry_run, report_path), dry_run=dry_run)
        except RateLimitedError as exc:
            save_rate_limit_status(config, str(exc), exc.retry_after_seconds, queued=True)
            print(str(exc))
            return RATE_LIMIT_EXIT
        except BaseException as exc:
            save_status(config, state="error", folder_id=config.get("folder_id"), last_error=str(exc), last_finish=now_iso())
            raise
        if code == 0:
            if text_looks_rate_limited(LAST_COMMAND_OUTPUT):
                retry_after = rate_limit_retry_after_seconds(LAST_COMMAND_OUTPUT, int(config.get("rate_limit_backoff_seconds", 300)))
                save_rate_limit_status(
                    config,
                    f"Dropbox reported throttling; cooling down for {retry_after}s",
                    retry_after,
                    queued=False,
                )
                return RATE_LIMIT_EXIT
            else:
                if not dry_run:
                    publication_code = publish_generation(config, report_path)
                    if publication_code != 0:
                        save_status(config, state="error", folder_id=config.get("folder_id"), last_error="generation publication failed", last_finish=now_iso())
                        return publication_code
                save_status(config, state="idle", folder_id=config.get("folder_id"), last_success=now_iso(), last_finish=now_iso(), last_error=None, last_warning=None)
        else:
            if text_looks_rate_limited(LAST_COMMAND_OUTPUT):
                retry_after = rate_limit_retry_after_seconds(LAST_COMMAND_OUTPUT, int(config.get("rate_limit_backoff_seconds", 300)))
                save_rate_limit_status(config, f"Dropbox rate limited Safe Sync; cooling down for {retry_after}s", retry_after, queued=True)
            else:
                save_status(config, state="error", folder_id=config.get("folder_id"), last_error=f"rclone exit {code}", last_finish=now_iso())
        return code


def cmd_backup(args: argparse.Namespace) -> int:
    config = normalized_config(load_config(Path(args.config).expanduser()))
    validate_local_path(config)
    if args.dry_run or args.folder or args.all:
        folders = selected_folders(config, args.folder, args.all)
        last_code = 0
        for folder in folders:
            print(f"folder: {folder['id']}")
            last_code = run_backup_with_config(folder_config(config, folder), args.dry_run)
            if last_code != 0:
                return last_code
        if not args.dry_run:
            registry_code = update_registry(config)
            if registry_code != 0:
                save_status(config, state="error", last_error="registry update failed", last_finish=now_iso())
                return registry_code
        return last_code
    response = daemon_api(config, "backup")
    if not response.get("ok"):
        raise SystemExit(str(response.get("error") or "daemon backup request failed"))
    print("backup queued")
    return 0


def run_pull_direct(config: dict[str, Any], src: str, dst: str, dry_run: bool, selected_paths: list[str] | None = None) -> int:
    with Lock(lock_file(config)):
        save_status(config, state="syncing", last_start=now_iso(), last_command="pull", last_error=None)
        try:
            code = run_command(config, copy_cmd(config, src, dst, dry_run, selected_paths), dry_run=dry_run)
        except BaseException as exc:
            save_status(config, state="error", last_error=str(exc), last_finish=now_iso())
            raise
        save_status(config, state="idle" if code == 0 else "error", last_success=now_iso() if code == 0 else None, last_error=None if code == 0 else f"rclone exit {code}", last_finish=now_iso())
        return code


def cmd_pull(args: argparse.Namespace) -> int:
    config = normalized_config(load_config(Path(args.config).expanduser()))
    if args.dry_run:
        print(json.dumps(comparison_through_work_lane(config, args.source, args.destination, args.select), indent=2, sort_keys=True))
        return 0
    try:
        response = daemon_api(
            config,
            "receive",
            source=args.source,
            destination=args.destination,
            selected_paths=args.select,
            source_label="peer",
            mode="receive",
        )
    except OSError:
        return run_receive_direct(config, args.source, args.destination, args.select)
    if not response.get("ok"):
        raise SystemExit(str(response.get("error") or "daemon transfer request failed"))
    print("safe receive queued; destination files will not change until the job is reviewed and applied")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config).expanduser())
    return run_command(config, [rclone_bin(config), "lsf", args.target, "--max-depth", str(args.depth)])


def cmd_rclone(args: argparse.Namespace) -> int:
    """Run the Safe Sync-managed rclone without exposing its runtime path."""
    config_path = Path(args.config).expanduser()
    config = load_config(config_path)
    if not args.rclone_args:
        raise SystemExit("Usage: safe-sync rclone <rclone command>")
    if args.rclone_args[0] == "config" and not config.get("rclone_config"):
        config["rclone_config"] = str(DEFAULT_RCLONE_CONFIG)
        config = write_config(config_path, config)
        print(f"Safe Sync rclone config: {config['rclone_config']}")
    return subprocess.run([rclone_bin(config), *args.rclone_args], check=False, env=rclone_env(config)).returncode


def cmd_connect_dropbox(args: argparse.Namespace) -> int:
    """Create Safe Sync's default Dropbox remote without rclone's broad menu."""
    config_path = Path(args.config).expanduser()
    if not config_path.exists():
        write_config(config_path, default_config(machine_name()))
    config = normalized_config(load_config(config_path))
    remote_name = "dropbox"
    remotes = subprocess.run(
        [rclone_bin(config), "listremotes"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        env=rclone_env(config),
    )
    if remotes.returncode != 0:
        print(remotes.stdout or "", end="")
        return int(remotes.returncode)
    remote_exists = f"{remote_name}:" in remotes.stdout.splitlines()
    if remote_exists and not getattr(args, "reconnect", False):
        print("Dropbox is already connected to Safe Sync.")
        return 0

    command = [rclone_bin(config), "config", "create", remote_name, "dropbox"]
    if args.headless:
        print("Headless Dropbox authorization")
        print("On a browser-equipped machine, run: safe-sync rclone authorize dropbox")
        token = input("Paste the resulting Dropbox token here: ").strip()
        if not token:
            raise SystemExit("Dropbox token is required; no remote was created.")
        if remote_exists:
            command = [rclone_bin(config), "config", "update", remote_name, "config_is_local", "false", "token", token]
        else:
            command.extend(["config_is_local", "false", "token", token])
    else:
        print("Opening Dropbox authorization in your browser...")
        if remote_exists:
            command = [rclone_bin(config), "config", "reconnect", remote_name]
    result = subprocess.run(command, check=False, env=rclone_env(config))
    if result.returncode == 0 and enabled_folders(config):
        service_cmd("restart")
        print("Safe Sync backend restarted with the new Dropbox authorization.")
    return result.returncode


def parse_status_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def status_health(config: dict[str, Any], service_state: str, sync_state: dict[str, Any]) -> dict[str, Any]:
    daemon_seen_at = sync_state.get("updated_at")
    last_error = sync_state.get("last_error")
    last_warning = sync_state.get("last_warning")
    sync_status = sync_state.get("state")
    if sync_status in {"backoff", "cooldown"} and (last_warning or sync_state.get("backoff_until")):
        health = "warning"
        reason = str(last_warning or "Dropbox cooldown is active")
    elif last_error and text_looks_rate_limited(str(last_error)):
        health = "warning"
        reason = str(last_error)
    elif last_error:
        health = "error"
        reason = str(last_error)
    elif last_warning:
        health = "warning"
        reason = str(last_warning)
    elif not enabled_folders(config):
        health = "setup_required"
        reason = "Choose a folder and connect Dropbox to finish setup"
    elif service_state == "stopped":
        health = "stopped"
        reason = "daemon service is stopped"
    elif service_state != "running":
        health = "unknown"
        reason = f"service state is {service_state}"
    else:
        seen = parse_status_time(daemon_seen_at)
        if seen is None:
            health = "stale"
            reason = "daemon has not written status yet"
        else:
            age = (dt.datetime.now(dt.timezone.utc).astimezone() - seen).total_seconds()
            stale_after = max(60, int(config.get("poll_interval_seconds", 5)) * 4 + 30)
            if age > stale_after:
                health = "stale"
                reason = f"daemon status is {int(age)}s old"
            else:
                health = "ok"
                reason = "daemon status is fresh"
    return {"health": health, "reason": reason, "daemon_seen_at": daemon_seen_at}


def status_payload(config_path: Path, api_timeout_seconds: float = 5.0) -> dict[str, Any]:
    if not config_path.exists():
        return {
            "daemon_seen_at": None,
            "health": "setup_required",
            "health_reason": "Safe Sync has not been configured yet",
            "log": None,
            "service_state": "not configured",
            "sync_state": {"state": "setup_required"},
        }
    config = normalized_config(load_config(config_path))
    try:
        response = api_request(socket_path(config), {"command": "status"}, timeout_seconds=api_timeout_seconds)
        if not response.get("ok"):
            raise RuntimeError(str(response.get("error") or "daemon API error"))
        sync_state = dict(response.get("status") or {})
    except Exception as exc:
        sync_state = {"state": "unknown", "socket_path": str(socket_path(config)), "error": str(exc)}

    service_text = service_status_text()
    service_state = service_text.split(":", 1)[1].strip() if ":" in service_text else service_text
    health = status_health(config, service_state, sync_state)
    return {
        "daemon_seen_at": health["daemon_seen_at"],
        "health": health["health"],
        "health_reason": health["reason"],
        "log": str(log_path(config)),
        "service_state": service_state,
        "sync_state": sync_state,
    }


def cmd_status(args: argparse.Namespace) -> int:
    payload = status_payload(Path(args.config).expanduser())
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def cmd_login_check(args: argparse.Namespace) -> int:
    """Print a short interactive-shell notice only when Safe Sync needs attention."""
    try:
        payload = status_payload(Path(args.config).expanduser(), api_timeout_seconds=0.75)
    except Exception as exc:
        print(f"Safe Sync needs attention: health check failed ({exc}). Run: safe-sync status")
        return 0

    health = str(payload["health"])
    if health == "ok":
        return 0

    reason = str(payload["health_reason"])
    prefix = "Safe Sync warning" if health == "warning" else "Safe Sync needs attention"
    if health == "setup_required":
        print(f"{prefix}: {reason}. Run: safe-sync setup")
    elif health == "stopped":
        print(f"{prefix}: {reason}. Run: safe-sync start")
    elif health == "error" and "Dropbox authorization is invalid or revoked" in reason:
        print(f"{prefix}: {reason}. Run: safe-sync connect-dropbox --headless --reconnect")
    else:
        print(f"{prefix}: {reason}. Run: safe-sync status")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = normalized_config(load_config(Path(args.config).expanduser()))
    validate_local_path(config)
    folders = enabled_folders(config)
    if not folders:
        raise SystemExit("No enabled folders configured")
    first_folder = folders[0]
    checks = {
        "config": str(Path(args.config).expanduser()),
        "rclone": rclone_bin(config),
        "filter_file": str(filter_file(folder_config(config, first_folder))),
        "folders": ", ".join(folder["id"] for folder in folders),
        "local_path": str(Path(first_folder["local_path"]).expanduser()),
        "remote_root": first_folder["remote_root"],
        "trash_root": first_folder["trash_root"],
        "poll_interval_seconds": str(config.get("poll_interval_seconds", 5)),
        "debounce_seconds": str(config.get("debounce_seconds", 20)),
        "fallback_interval_seconds": str(config.get("fallback_interval_seconds", 1800)),
    }
    for name, value in checks.items():
        print(f"{name}: {value}")
    missing = []
    for folder in enabled_folders(config):
        fcfg = folder_config(config, folder)
        missing.extend(p for p in [filter_file(fcfg), Path(fcfg["local_path"]).expanduser()] if not p.exists())
    if missing:
        for p in missing:
            print(f"missing: {p}", file=sys.stderr)
        return 1
    preflight(folder_config(config, first_folder))
    print("remote preflight: ok")
    return 0


def watch_settings_from_config(config: dict[str, Any], args: argparse.Namespace) -> WatchSettings:
    return WatchSettings(
        poll_interval_seconds=int(args.poll_interval or config.get("poll_interval_seconds", 5)),
        debounce_seconds=int(args.debounce or config.get("debounce_seconds", 20)),
        min_interval_seconds=int(config.get("min_interval_seconds", 120)),
        fallback_interval_seconds=int(config.get("fallback_interval_seconds", 1800)),
        rate_limit_backoff_seconds=int(config.get("rate_limit_backoff_seconds", 300)),
    )


def restore_last_sync_finish(daemon: WatchDaemon, config: dict[str, Any], monotonic_now: float) -> None:
    snapshot = current_status(config)
    last_finish = parse_status_time(snapshot.get("last_finish")) or parse_status_time(snapshot.get("last_success"))
    if last_finish is None:
        return
    elapsed = max(0.0, (dt.datetime.now(dt.timezone.utc).astimezone() - last_finish).total_seconds())
    daemon.state.last_sync_finish_monotonic = monotonic_now - elapsed


def publish_runtime_status(api_state: DaemonApiState, config: dict[str, Any], **updates: Any) -> dict[str, Any]:
    current = api_state.snapshot()
    merged = dict(current)
    merged.update(updates)
    activity_event = merged.pop("_activity_event", None)
    merged["updated_at"] = now_iso()
    if "folders" not in merged:
        merged["folders"] = [
            {"id": folder["id"], "local_path": str(Path(folder["local_path"]).expanduser())}
            for folder in enabled_folders(config)
        ]
    if "poll_interval_seconds" not in merged:
        merged["poll_interval_seconds"] = int(config.get("poll_interval_seconds", 5))
    if "debounce_seconds" not in merged:
        merged["debounce_seconds"] = int(config.get("debounce_seconds", 20))
    if "fallback_interval_seconds" not in merged:
        merged["fallback_interval_seconds"] = int(config.get("fallback_interval_seconds", 1800))
    existing_activity = current.get("recent_activity")
    activity: list[str] = list(existing_activity) if isinstance(existing_activity, list) else []
    if isinstance(activity_event, str) and activity_event:
        if not activity or activity[0] != activity_event:
            activity.insert(0, activity_event)
        activity = activity[:8]
    merged["recent_activity"] = activity
    api_state.update(**merged)
    return merged


def summarize_progress_line(line: str) -> str | None:
    cleaned = line.strip()
    if not cleaned:
        return None
    if "Transferred:" in cleaned or "Checks:" in cleaned or "Elapsed time:" in cleaned or "Transferring:" in cleaned:
        return cleaned
    if ": Copied" in cleaned or ": Deleted" in cleaned or ": Updated" in cleaned:
        _, detail = cleaned.split("INFO  :", 1) if "INFO  :" in cleaned else ("", cleaned)
        return detail.strip()
    return None


def current_file_from_progress(progress: str | None) -> str | None:
    if not progress:
        return None
    cleaned = progress.strip()
    if ": Copied" in cleaned or ": Deleted" in cleaned or ": Updated" in cleaned:
        return cleaned.split(":", 1)[0].strip()
    if cleaned.startswith("*"):
        body = cleaned.lstrip("*").strip()
        return body.split(":", 1)[0].strip() or None
    return None


def run_all_backups_runtime(config: dict[str, Any], dry_run: bool, api_state: DaemonApiState) -> tuple[int, str | None]:
    last_code = 0
    folders = enabled_folders(config)
    total_folders = len(folders)
    for index, folder in enumerate(folders, start=1):
        folder_cfg = folder_config(config, folder)
        report_path = backup_report_path(folder_cfg)
        publish_runtime_status(
            api_state,
            config,
            state="syncing",
            folder_id=folder["id"],
            current_folder_index=index,
            current_folder_total=total_folders,
            current_folder_label=folder.get("label", folder["id"]),
            local_path=str(Path(folder["local_path"]).expanduser()),
            last_start=now_iso(),
            last_command="daemon",
            last_error=None,
            last_progress="Starting folder sync",
        )
        def on_progress(line: str) -> None:
            summary = summarize_progress_line(line)
            if summary:
                current_file = current_file_from_progress(summary)
                publish_runtime_status(
                    api_state,
                    config,
                    state="syncing",
                    folder_id=folder["id"],
                    current_folder_index=index,
                    current_folder_total=total_folders,
                    current_folder_label=folder.get("label", folder["id"]),
                    local_path=str(Path(folder["local_path"]).expanduser()),
                    last_progress=summary,
                    current_file=current_file,
                    _activity_event=summary if current_file else None,
                )
        try:
            preflight(folder_cfg)
            code = run_command(folder_cfg, backup_cmd(folder_cfg, dry_run, report_path), dry_run=dry_run, progress_callback=on_progress)
        except RateLimitedError as exc:
            publish_runtime_status(
                api_state,
                config,
                state="backoff",
                failed_folder=folder["id"],
                last_warning=str(exc),
                last_error=None,
                queued_backup=True,
                backoff_seconds=exc.retry_after_seconds,
                backoff_until=future_iso(exc.retry_after_seconds),
                last_finish=now_iso(),
                last_progress=f"Paused before folder {index} of {total_folders}",
            )
            return RATE_LIMIT_EXIT, folder["id"]
        except BaseException as exc:
            publish_runtime_status(
                api_state,
                config,
                state="error",
                failed_folder=folder["id"],
                last_error=str(exc),
                last_finish=now_iso(),
                last_progress=f"Failed on folder {index} of {total_folders}",
            )
            raise

        if code != 0:
            if text_looks_rate_limited(LAST_COMMAND_OUTPUT):
                retry_after = rate_limit_retry_after_seconds(LAST_COMMAND_OUTPUT, int(config.get("rate_limit_backoff_seconds", 300)))
                publish_runtime_status(
                    api_state,
                    config,
                    state="backoff",
                    failed_folder=folder["id"],
                    last_warning=f"Dropbox rate limited Safe Sync; cooling down for {retry_after}s",
                    last_error=None,
                    queued_backup=True,
                    backoff_seconds=retry_after,
                    backoff_until=future_iso(retry_after),
                    last_finish=now_iso(),
                    last_progress=f"Dropbox throttled folder {index} of {total_folders}",
                )
                return RATE_LIMIT_EXIT, folder["id"]
            error = reconnect_dropbox_message() if text_looks_auth_failure(LAST_COMMAND_OUTPUT) else f"rclone exit {code}"
            publish_runtime_status(
                api_state,
                config,
                state="error",
                failed_folder=folder["id"],
                last_error=error,
                last_finish=now_iso(),
                last_progress=f"Failed on folder {index} of {total_folders}",
            )
            return code, folder["id"]

        if text_looks_rate_limited(LAST_COMMAND_OUTPUT):
            retry_after = rate_limit_retry_after_seconds(LAST_COMMAND_OUTPUT, int(config.get("rate_limit_backoff_seconds", 300)))
            publish_runtime_status(
                api_state,
                config,
                state="backoff",
                failed_folder=folder["id"],
                last_warning=f"Dropbox reported throttling; cooling down for {retry_after}s",
                last_error=None,
                queued_backup=False,
                backoff_seconds=retry_after,
                backoff_until=future_iso(retry_after),
                last_finish=now_iso(),
                last_progress=f"Dropbox throttled folder {index} of {total_folders}",
            )
            return RATE_LIMIT_EXIT, folder["id"]

        if not dry_run:
            publication_code = publish_generation(folder_cfg, report_path)
            if publication_code != 0:
                publish_runtime_status(
                    api_state,
                    config,
                    state="error",
                    failed_folder=folder["id"],
                    last_error="generation publication failed",
                    last_finish=now_iso(),
                    last_progress=f"Backup completed but change publication failed for folder {index} of {total_folders}",
                )
                return publication_code, folder["id"]

        last_code = code

    if not dry_run:
        registry_code = update_registry(config)
        if registry_code != 0:
            publish_runtime_status(api_state, config, state="error", last_error="registry update failed", last_finish=now_iso())
            return registry_code, "registry"
    return last_code, None


def run_receive_runtime(config: dict[str, Any], request: dict[str, Any], api_state: DaemonApiState) -> int:
    source = str(request["source"])
    destination = str(request["destination"])
    selected_paths = [str(path) for path in request.get("selected_paths") or []]
    publish_runtime_status(
        api_state,
        config,
        state="comparing",
        last_start=now_iso(),
        last_command="receive",
        source=source,
        destination=destination,
        last_error=None,
        last_warning=None,
        last_progress="Comparing remote source and local destination",
        _activity_event=f"Safe receive comparison started: {source}",
    )

    def on_progress(line: str) -> None:
        summary = summarize_progress_line(line)
        if summary:
            publish_runtime_status(
                api_state,
                config,
                state="staging",
                last_progress=summary,
                current_file=current_file_from_progress(summary),
                _activity_event=summary,
            )

    try:
        code, job = create_receive_job(
            config,
            source=source,
            destination=destination,
            selected_paths=selected_paths,
            source_label=str(request.get("source_label") or "peer"),
            mode=str(request.get("mode") or "receive"),
            baseline_inventory=request.get("baseline_inventory"),
            source_generation=str(request.get("source_generation") or "") or None,
            link_id=str(request.get("link_id") or "") or None,
            progress_callback=on_progress,
        )
    except BaseException as exc:
        publish_runtime_status(api_state, config, state="error", last_error=str(exc), last_finish=now_iso())
        return 1
    if code != 0:
        publish_runtime_status(
            api_state,
            config,
            state="error",
            last_error=str(job.get("error") or f"rclone exit {code}"),
            last_finish=now_iso(),
            receive_job_id=job["id"],
        )
        return code
    publish_runtime_status(
        api_state,
        config,
        state="watching",
        last_success=now_iso(),
        last_finish=now_iso(),
        last_error=None,
        last_progress="Receive job staged and ready for review",
        receive_job_id=job["id"],
        receive_job_status=job["status"],
        _activity_event=f"Receive job ready: {job['id']}",
    )
    return 0


def run_query_runtime(config: dict[str, Any], ticket: dict[str, Any], api_state: DaemonApiState) -> None:
    query = str(ticket.get("query") or "")
    payload = ticket.get("payload") or {}
    try:
        if api_state.snapshot().get("state") == "backoff":
            raise TransferError("Dropbox cooldown is active; retry comparison after backoff")
        if query != "compare":
            raise TransferError(f"unknown remote query: {query}")
        publish_runtime_status(
            api_state,
            config,
            state="comparing",
            last_command="compare",
            last_progress="Running read-only remote/local comparison",
        )
        result = comparison_payload(
            config,
            str(payload["source"]),
            str(payload["destination"]),
            [str(path) for path in payload.get("selected_paths") or []],
        )
        api_state.complete_query(ticket, {"ok": True, "comparison": result})
        publish_runtime_status(api_state, config, state="watching", last_progress="Comparison complete")
    except BaseException as exc:
        api_state.complete_query(ticket, {"ok": False, "error": str(exc)})
        publish_runtime_status(api_state, config, state="watching", last_warning=str(exc))


def run_job_operation_runtime(config: dict[str, Any], request: dict[str, Any], api_state: DaemonApiState) -> int:
    operation = str(request["operation"])
    job_id = str(request["job_id"])
    store = JobStore(state_root_path(config))
    publish_runtime_status(
        api_state,
        config,
        state="applying" if operation == "apply" else operation,
        last_command=f"jobs {operation}",
        receive_job_id=job_id,
        last_error=None,
        last_progress=f"{operation.capitalize()} started for {job_id}",
        _activity_event=f"Job {operation} started: {job_id}",
    )
    try:
        if operation == "apply":
            job = store.load(job_id)
            revalidate_remote_job_source(config, job)
            result = store.commit_clone(job_id) if job.get("mode") == "clone" else store.apply(job_id, request.get("policies") or {})
        elif operation == "reconcile":
            result = store.reconcile(job_id)
        elif operation == "rollback":
            result = store.rollback(job_id)
        else:
            raise TransferError(f"unknown job operation: {operation}")
        if operation == "apply" and result.get("status") == "complete" and result.get("link_id"):
            destination_inventory = local_inventory(Path(result["destination"]))
            if inventories_equal(destination_inventory, result.get("source_inventory") or {}):
                LinkStore(state_root_path(config)).accept_baseline(
                    str(result["link_id"]),
                    destination_inventory,
                    peer_generation=str(result.get("source_generation") or "") or None,
                )
    except BaseException as exc:
        publish_runtime_status(
            api_state,
            config,
            state="watching",
            last_error=None,
            last_warning=str(exc),
            last_finish=now_iso(),
            receive_job_id=job_id,
            receive_job_status="needs_review",
            last_progress=f"Job {operation} needs review",
        )
        return 1
    publish_runtime_status(
        api_state,
        config,
        state="watching",
        last_error=None,
        last_warning=None,
        last_finish=now_iso(),
        receive_job_id=job_id,
        receive_job_status=result.get("status"),
        last_progress=f"Job {operation} finished: {result.get('status')}",
        _activity_event=f"Job {operation} finished: {job_id}",
    )
    return 0


def reconcile_interrupted_jobs(config: dict[str, Any]) -> list[str]:
    store = JobStore(state_root_path(config))
    reconciled: list[str] = []
    for job in store.list():
        if job.get("status") in {"applying", "interrupted"}:
            store.reconcile(str(job["id"]))
            reconciled.append(str(job["id"]))
    return reconciled


def backup_blocking_jobs(config: dict[str, Any]) -> list[str]:
    blocked: list[str] = []
    for job in JobStore(state_root_path(config)).list():
        if job.get("status") in {"applying", "interrupted"}:
            blocked.append(str(job["id"]))
            continue
        if job.get("status") == "needs_review" and any(
            action.get("state") == "old_checkpointed" for action in job.get("actions") or []
        ):
            blocked.append(str(job["id"]))
    return blocked


def revalidate_remote_job_source(config: dict[str, Any], job: dict[str, Any]) -> None:
    current = select_inventory(fetch_remote_inventory(config, str(job["source"])), job.get("selected_paths") or [])
    if inventories_equal(current, job.get("source_inventory") or {}):
        return
    job["status"] = "source_changed"
    job["source_changed_at"] = now_iso()
    JobStore(state_root_path(config)).save(job)
    raise JobConflictError("remote source changed after staging; create a refreshed receive job")


def run_pull_runtime(config: dict[str, Any], request: dict[str, Any], api_state: DaemonApiState) -> int:
    """Run an explicit copy inside the daemon's single rclone work queue."""
    source = str(request["source"])
    destination = str(request["destination"])
    dry_run = bool(request.get("dry_run"))
    selected_paths = [str(path) for path in request.get("selected_paths") or []]
    publish_runtime_status(
        api_state,
        config,
        state="transferring",
        last_start=now_iso(),
        last_command="pull",
        source=source,
        destination=destination,
        current_folder_label=destination,
        last_error=None,
        last_warning=None,
        last_progress=f"Starting requested transfer ({len(selected_paths)} selected items)" if selected_paths else "Starting requested transfer",
        _activity_event=f"Transfer started: {source} -> {destination}",
    )

    def on_progress(line: str) -> None:
        summary = summarize_progress_line(line)
        if summary:
            publish_runtime_status(
                api_state,
                config,
                state="transferring",
                current_folder_label=destination,
                last_progress=summary,
                current_file=current_file_from_progress(summary),
                _activity_event=summary,
            )

    try:
        code = run_command(config, copy_cmd(config, source, destination, dry_run, selected_paths), dry_run=dry_run, progress_callback=on_progress)
    except BaseException as exc:
        publish_runtime_status(api_state, config, state="error", last_error=str(exc), last_finish=now_iso())
        return 1

    if code == 0 and not text_looks_rate_limited(LAST_COMMAND_OUTPUT):
        publish_runtime_status(
            api_state,
            config,
            state="watching",
            last_success=now_iso(),
            last_finish=now_iso(),
            last_error=None,
            last_progress="Transfer complete",
            _activity_event=f"Transfer complete: {source} -> {destination}",
        )
        return 0

    if text_looks_rate_limited(LAST_COMMAND_OUTPUT):
        retry_after = rate_limit_retry_after_seconds(LAST_COMMAND_OUTPUT, int(config.get("rate_limit_backoff_seconds", 300)))
        publish_runtime_status(
            api_state,
            config,
            state="backoff",
            last_warning=f"Dropbox rate limited Safe Sync; cooling down for {retry_after}s",
            last_error=None,
            queued_backup=True,
            backoff_seconds=retry_after,
            backoff_until=future_iso(retry_after),
            last_finish=now_iso(),
            last_progress="Transfer paused by Dropbox throttling",
        )
        return RATE_LIMIT_EXIT

    publish_runtime_status(
        api_state,
        config,
        state="error",
        last_error=f"rclone exit {code}",
        last_finish=now_iso(),
        last_progress="Transfer failed",
    )
    return code





def cmd_autostart(args: argparse.Namespace) -> int:
    if args.autostart_target != "backend":
        raise SystemExit(f"Unknown autostart target: {args.autostart_target}")
    if args.autostart_action == "status":
        print(backend_autostart_status_text())
        return 0
    cmd = backend_autostart_cmd(args.autostart_action)
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if result.stdout:
        print(result.stdout, end="")
    if result.returncode == 0:
        print(backend_autostart_status_text())
    return int(result.returncode)


def cmd_start(args: argparse.Namespace) -> int:
    return service_cmd("start")


def cmd_stop(args: argparse.Namespace) -> int:
    return service_cmd("stop")


def cmd_restart(args: argparse.Namespace) -> int:
    return service_cmd("restart")


def cmd_logs(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config).expanduser())
    path = log_path(config)
    if not path.exists():
        print(f"No log file yet: {path}")
        return 0
    lines = path.read_text(errors="replace").splitlines()
    for line in lines[-args.lines:]:
        print(line)
    return 0


def folder_snapshots(config: dict[str, Any]) -> dict[str, dict[str, tuple[str, int, int]]]:
    snapshots: dict[str, dict[str, tuple[str, int, int]]] = {}
    for folder in enabled_folders(config):
        local_path = Path(folder["local_path"]).expanduser()
        if not local_path.exists():
            raise SystemExit(f"Local path does not exist for folder {folder['id']}: {local_path}")
        snapshots[folder["id"]] = scan_tree(local_path)
    return snapshots


def update_registry(config: dict[str, Any]) -> int:
    doc = json.dumps(registry_doc(config), indent=2, sort_keys=True) + "\n"
    result = rclone_capture(config, ["rcat", registry_path(config)], input_text=doc)
    if result.returncode != 0:
        append_log(config, f"[{now_iso()}] registry update failed:\n{result.stdout or ''}\n")
    return int(result.returncode)


def list_registry_files(config: dict[str, Any]) -> set[str] | None:
    result = rclone_capture(config, ["lsf", registry_dir(config), "--files-only"])
    if result.returncode != 0:
        append_log(config, f"[{now_iso()}] registry list failed:\n{result.stdout or ''}\n")
        return None
    return {name.strip() for name in (result.stdout or "").splitlines() if name.strip()}


def ensure_local_profiles_registered(config: dict[str, Any]) -> list[str]:
    normalized = normalized_config(config)
    existing = list_registry_files(normalized)
    if existing is None:
        return []

    created: list[str] = []
    for profile in normalized["profiles"]:
        machine_id = str(profile["machine_id"])
        if registry_filename(machine_id) in existing:
            continue
        profile_config = config_for_profile(normalized, profile)
        if update_registry(profile_config) == 0:
            created.append(machine_id)
            existing.add(registry_filename(machine_id))
    return created


def run_all_backups(config: dict[str, Any], dry_run: bool) -> tuple[int, str | None]:
    last_code = 0
    for folder in enabled_folders(config):
        code = run_backup_with_config(folder_config(config, folder), dry_run)
        if code != 0:
            return code, folder["id"]
        last_code = code
    if not dry_run:
        registry_code = update_registry(config)
        if registry_code != 0:
            return registry_code, "registry"
    return last_code, None


def cmd_daemon(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser()
    config = normalized_config(load_config(config_path))
    with Lock(lock_file(config)):
        return run_daemon(args, config_path, config)


def run_daemon(args: argparse.Namespace, config_path: Path, config: dict[str, Any]) -> int:
    validate_local_path(config)
    settings = watch_settings_from_config(config, args)
    daemon = WatchDaemon(settings)
    api_state = DaemonApiState()
    api_server = DaemonApiServer(socket_path(config), api_state)
    folders = enabled_folders(config)
    if not folders:
        raise SystemExit("No enabled folders configured")

    config_mtime_ns = config_path.stat().st_mtime_ns if config_path.exists() else None
    watcher = NativeWatcher(folders, wake=api_state.wake)
    watcher_mode = "native"
    watcher_warning = None
    previous_snapshots: dict[str, dict[str, tuple[str, int, int]]] = {}
    try:
        watcher.start()
    except Exception as exc:
        watcher_mode = "polling"
        watcher_warning = f"native watcher unavailable; using full-tree polling: {exc}"
        previous_snapshots = folder_snapshots(config)
    startup_now = time.monotonic()
    next_link_check = startup_now
    daemon.mark_dirty(startup_now)
    ensure_local_profiles_registered(config)
    reconciled_jobs = reconcile_interrupted_jobs(config)
    publish_runtime_status(
        api_state,
        config,
        state="dirty",
        watcher=watcher_mode,
        folders=[{"id": folder["id"], "local_path": str(Path(folder["local_path"]).expanduser())} for folder in folders],
        dry_run=args.dry_run,
        poll_interval_seconds=settings.poll_interval_seconds,
        debounce_seconds=settings.debounce_seconds,
        fallback_interval_seconds=settings.fallback_interval_seconds,
        last_error=None,
        last_warning=watcher_warning,
        note="startup reconcile queued",
        reconciled_jobs=reconciled_jobs,
    )
    append_log(config, f"[{now_iso()}] daemon started watcher={watcher_mode} folders={','.join(folder['id'] for folder in folders)} dry_run={args.dry_run}\n")
    if watcher_warning:
        append_log(config, f"[{now_iso()}] warning: {watcher_warning}\n")
    api_server.start()

    try:
        loops = 0
        manual_backup_pending = False
        while True:
            loops += 1
            now = time.monotonic()
            latest_mtime_ns = config_path.stat().st_mtime_ns if config_path.exists() else None
            if latest_mtime_ns != config_mtime_ns:
                publish_runtime_status(
                    api_state,
                    config,
                    state="watching",
                    last_error=None,
                    note="config changed; restarting daemon",
                )
                append_log(config, f"[{now_iso()}] config changed on disk; exiting daemon for reload\n")
                return 0
            if watcher_mode == "native" and not watcher.healthy():
                watcher.stop()
                watcher_mode = "polling"
                watcher_warning = "native watcher stopped; using full-tree polling"
                previous_snapshots = folder_snapshots(config)
                publish_runtime_status(api_state, config, watcher=watcher_mode, last_warning=watcher_warning)
                append_log(config, f"[{now_iso()}] warning: {watcher_warning}\n")
            if api_state.consume_backup_request():
                manual_backup_pending = True
                publish_runtime_status(api_state, config, state="dirty", last_error=None, queued_backup=True, note="manual backup queued")

            job_operation = api_state.consume_job_operation()
            if job_operation is not None:
                run_job_operation_runtime(config, job_operation, api_state)
                now = time.monotonic()

            query_ticket = api_state.consume_query()
            if query_ticket is not None:
                run_query_runtime(config, query_ticket, api_state)
                now = time.monotonic()

            if api_state.snapshot().get("state") != "backoff":
                pull_request = api_state.consume_pull_request()
                if pull_request is not None:
                    if pull_request.get("safe_receive"):
                        run_receive_runtime(config, pull_request, api_state)
                    else:
                        run_pull_runtime(config, pull_request, api_state)
                    # A native event or polling snapshot schedules a backup if
                    # the destination belongs to a watched folder.
                    now = time.monotonic()
            elif api_state.has_pull_request():
                publish_runtime_status(
                    api_state,
                    config,
                    queued_transfer=True,
                    note="transfer queued until Dropbox cooldown ends",
                )

            if now >= next_link_check and api_state.snapshot().get("state") not in {"syncing", "transferring", "staging", "backoff"}:
                try:
                    link_notifications = detect_link_generations(config)
                    if link_notifications:
                        publish_runtime_status(
                            api_state,
                            config,
                            linked_folder_changes=link_notifications,
                            last_warning=f"{len(link_notifications)} linked folder(s) have peer changes ready for review",
                            _activity_event=f"Linked-folder changes detected: {', '.join(item['label'] for item in link_notifications)}",
                        )
                except BaseException as exc:
                    append_log(config, f"[{now_iso()}] linked-folder generation check failed: {exc}\n")
                next_link_check = now + 60.0

            if watcher_mode == "native":
                changed = watcher.consume()
            else:
                current_snapshots = folder_snapshots(config)
                changed = [folder_id for folder_id, snapshot in current_snapshots.items() if snapshot != previous_snapshots.get(folder_id)]
                previous_snapshots = current_snapshots
            if changed:
                daemon.mark_dirty(now)
                local_link_changes = detect_local_link_changes(config, changed)
                publish_runtime_status(
                    api_state,
                    config,
                    state="dirty",
                    changed_folders=changed,
                    changed_links=local_link_changes,
                    last_change=now_iso(),
                    watcher=watcher_mode,
                )
            elif daemon.state.state not in {DaemonState.SYNCING, DaemonState.BACKOFF}:
                publish_runtime_status(api_state, config, state="watching", watcher=watcher_mode)

            if daemon.state.state == DaemonState.BACKOFF:
                if daemon.backoff_expired(now):
                    daemon.state.state = DaemonState.DIRTY
                    daemon.mark_dirty(now)
                    publish_runtime_status(api_state, config, state="dirty", last_error=None, note="backoff expired", backoff_remaining_seconds=0)
                else:
                    publish_runtime_status(
                        api_state,
                        config,
                        state="backoff",
                        backoff_remaining_seconds=round(daemon.backoff_remaining(now), 1),
                    )
                    if args.once or (args.max_loops and loops >= args.max_loops):
                        return 75
                    api_state.wait(settings.poll_interval_seconds)
                    continue

            manual_run = manual_backup_pending
            automatic_due = daemon.should_sync_after_debounce(now) or daemon.should_run_fallback(now)
            should_run = daemon.should_run_backup(now, manual=manual_run)
            blocking_jobs = backup_blocking_jobs(config)
            if should_run and blocking_jobs:
                should_run = False
                publish_runtime_status(
                    api_state,
                    config,
                    state="watching",
                    last_warning=f"Backup paused until interrupted receive jobs are reconciled: {', '.join(blocking_jobs)}",
                    blocking_receive_jobs=blocking_jobs,
                )
            if automatic_due and not manual_run and daemon.in_min_interval(now):
                publish_runtime_status(api_state, config, state="cooldown", cooldown_remaining_seconds=round(daemon.min_interval_remaining(now), 1))

            if should_run:
                manual_backup_pending = False
                daemon.note_sync_started(now)
                publish_runtime_status(
                    api_state,
                    config,
                    state="syncing",
                    last_start=now_iso(),
                    last_command="backup" if manual_run else "daemon",
                    queued_backup=False,
                )
                failed_folder = None
                try:
                    code, failed_folder = run_all_backups_runtime(config, args.dry_run, api_state)
                    error_text = f"rclone exit {code}" if code != 0 else None
                except SystemExit as exc:
                    code = int(exc.code) if isinstance(exc.code, int) else 75
                    error_text = str(exc) or "backup failed"
                after = time.monotonic()
                rate_limited = code == RATE_LIMIT_EXIT
                daemon.note_sync_finished(after, rate_limited=rate_limited)
                if code == 0:
                    publish_runtime_status(api_state, config, state="watching", last_success=now_iso(), last_finish=now_iso(), last_error=None, last_warning=None, queued_backup=False)
                elif rate_limited:
                    manual_backup_pending = manual_backup_pending or manual_run
                    publish_runtime_status(
                        api_state,
                        config,
                        state="backoff",
                        failed_folder=failed_folder,
                        last_warning=(api_state.snapshot().get("last_warning") or f"{error_text}; rate limited"),
                        last_error=None,
                        queued_backup=True,
                    )
                else:
                    existing_error = str(api_state.snapshot().get("last_error") or error_text)
                    publish_runtime_status(api_state, config, state="error", failed_folder=failed_folder, last_error=existing_error)
                if args.once:
                    return code

            if args.max_loops and loops >= args.max_loops:
                publish_runtime_status(api_state, config, state="watching", note="max loops reached")
                return 0
            api_state.wait(settings.poll_interval_seconds)
    finally:
        watcher.stop()
        api_server.stop()



def cmd_folders(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser()
    config = normalized_config(load_config(config_path))
    if args.folder_cmd == "list":
        print(json.dumps(config["folders"], indent=2, sort_keys=True))
        return 0
    if args.folder_cmd == "add":
        folder_id = safe_id(args.id)
        if any(folder["id"] == folder_id for folder in config["folders"]):
            raise SystemExit(f"Folder already exists: {folder_id}")
        machine_id = config["machine_id"]
        folder = {
            "id": folder_id,
            "label": args.label or folder_id,
            "local_path": args.local_path,
            "remote_path": args.remote_path or f"{machine_id}/{folder_id}",
            "trash_path": args.trash_path or f".trash/{machine_id}/{folder_id}",
            "filter_file": args.filter_file or str(config.get("filter_file", DEFAULT_FILTER)),
            "enabled": not args.disabled,
        }
        folder.setdefault("remote_root", remote_join(str(config["remote_base"]), str(folder["remote_path"])))
        folder.setdefault("trash_root", remote_join(str(config["remote_base"]), str(folder["trash_path"])))
        validate_local_path({**config, "folders": [folder]})
        config["folders"].append(folder)
        for profile in config["profiles"]:
            if profile["id"] == config["active_profile_id"]:
                profile["folders"] = config["folders"]
                break
        updated = write_config(config_path, config)
        update_registry(updated)
        restart_backend_if_running(config_path)
        print(folder_id)
        return 0
    if args.folder_cmd == "update":
        folder_id = safe_id(args.id)
        folder = next((folder for folder in config["folders"] if folder["id"] == folder_id), None)
        if folder is None:
            raise SystemExit(f"Folder not found: {folder_id}")
        enabled = bool(folder.get("enabled", True))
        if args.disabled:
            enabled = False
        elif args.enabled:
            enabled = True
        folder["local_path"] = args.local_path
        folder["enabled"] = enabled
        folder["label"] = args.label or folder.get("label") or folder_id
        folder["filter_file"] = args.filter_file or folder.get("filter_file") or str(config.get("filter_file", DEFAULT_FILTER))
        validate_local_path({**config, "folders": [folder]})
        for profile in config["profiles"]:
            if profile["id"] == config["active_profile_id"]:
                profile["folders"] = config["folders"]
                break
        updated = write_config(config_path, config)
        update_registry(updated)
        restart_backend_if_running(config_path)
        print(folder_id)
        return 0
    if args.folder_cmd == "remove":
        folder_id = safe_id(args.id)
        remaining = [folder for folder in config["folders"] if folder["id"] != folder_id]
        if len(remaining) == len(config["folders"]):
            raise SystemExit(f"Folder not found: {folder_id}")
        config["folders"] = remaining
        for profile in config["profiles"]:
            if profile["id"] == config["active_profile_id"]:
                profile["folders"] = remaining
                break
        updated = write_config(config_path, config)
        update_registry(updated)
        restart_backend_if_running(config_path)
        print(folder_id)
        return 0
    raise SystemExit(f"Unknown folders command: {args.folder_cmd}")


def cmd_profiles(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser()
    config = normalized_config(load_config(config_path))
    if args.profile_cmd == "list":
        print(json.dumps(config_view(config, config_path)["profiles"], indent=2, sort_keys=True))
        return 0
    if args.profile_cmd == "add":
        profile_id = safe_id(args.id)
        if any(profile["id"] == profile_id for profile in config["profiles"]):
            raise SystemExit(f"Profile already exists: {profile_id}")
        machine_id = str(args.machine_id or profile_id)
        profile = normalized_profile(
            {
                "id": profile_id,
                "label": args.label or machine_id,
                "machine": machine_id,
                "machine_id": machine_id,
                "machine_label": args.machine_label or machine_id,
                "install_id": default_install_id(),
                "remote_base": args.remote_base or config.get("remote_base", "dropbox:computer-backups/test"),
                "filter_file": str(config.get("filter_file", DEFAULT_FILTER)),
                "folders": [],
            },
            config,
        )
        config["profiles"].append(profile)
        updated = write_config(config_path, config)
        update_registry(config_for_profile(updated, profile))
        print(profile_id)
        return 0
    if args.profile_cmd == "activate":
        profile_id = safe_id(args.id)
        if not any(profile["id"] == profile_id for profile in config["profiles"]):
            raise SystemExit(f"Profile not found: {profile_id}")
        config["active_profile_id"] = profile_id
        updated = write_config(config_path, config)
        update_registry(updated)
        restart_backend_if_running(config_path)
        print(profile_id)
        return 0
    raise SystemExit(f"Unknown profiles command: {args.profile_cmd}")


def cmd_config(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser()
    config = normalized_config(load_config(config_path))
    if args.config_cmd == "show":
        print(json.dumps(config_view(config, config_path), indent=2, sort_keys=True))
        return 0
    if args.config_cmd == "update":
        if args.machine_label:
            config["machine_label"] = args.machine_label
        if args.profile_label:
            config["profile_label"] = args.profile_label
        if args.remote_base:
            config["remote_base"] = args.remote_base
            for folder in config["folders"]:
                folder["remote_root"] = remote_join(args.remote_base, str(folder["remote_path"]))
                folder["trash_root"] = remote_join(args.remote_base, str(folder["trash_path"]))
        config["poll_interval_seconds"] = bounded_seconds("poll interval", int(args.poll_interval_seconds), 1, 3600)
        config["debounce_seconds"] = bounded_seconds("debounce", int(args.debounce_seconds), 1, 3600)
        config["min_interval_seconds"] = bounded_seconds("minimum interval", int(args.min_interval_seconds), 0, 86400)
        config["fallback_interval_seconds"] = bounded_seconds("fallback interval", int(args.fallback_interval_seconds), 60, 86400)
        config["rate_limit_backoff_seconds"] = bounded_seconds("rate limit backoff", int(args.rate_limit_backoff_seconds), 60, 86400)
        for profile in config["profiles"]:
            if profile["id"] == config["active_profile_id"]:
                profile["machine_label"] = config["machine_label"]
                profile["label"] = config.get("profile_label", profile.get("label", profile["id"]))
                profile["remote_base"] = config["remote_base"]
                profile["folders"] = config["folders"]
                break
        updated = write_config(config_path, config)
        update_registry(updated)
        restart_backend_if_running(config_path)
        print(json.dumps(config_view(updated, config_path), indent=2, sort_keys=True))
        return 0
    raise SystemExit(f"Unknown config command: {args.config_cmd}")


def rclone_capture(config: dict[str, Any], cmd: list[str], input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    guarded_cmd = [
        rclone_bin(config),
        *cmd,
        "--timeout",
        "30s",
        "--contimeout",
        "10s",
        "--retries",
        "1",
        "--low-level-retries",
        "1",
    ]
    return subprocess.run(
        guarded_cmd,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=180,
        env=rclone_env(config),
    )


def state_root_path(config: dict[str, Any]) -> Path:
    return Path(normalized_config(config)["state_root"]).expanduser()


def backup_report_path(config: dict[str, Any]) -> Path:
    root = state_root_path(config) / "backup-reports"
    root.mkdir(parents=True, exist_ok=True)
    folder_id = safe_id(str(config.get("folder_id") or "folder"))
    return root / f"{folder_id}-{uuid.uuid4().hex}.combined"


def publish_generation(config: dict[str, Any], report_path: Path) -> int:
    """Publish an immutable owner generation, then its latest pointer."""
    cfg = normalized_config(config)
    folder_id = str(config.get("folder_id") or "")
    if not folder_id:
        raise TransferError("folder_id is required to publish a generation")
    remote_dir = generation_remote_dir(str(cfg["remote_base"]), str(cfg["machine_id"]), folder_id)
    latest_path = remote_join(remote_dir, "latest.json")
    parent_generation: str | None = None
    previous = rclone_capture(cfg, ["cat", latest_path])
    if previous.returncode == 0:
        try:
            parent_generation = str(json.loads(previous.stdout).get("generation_id") or "") or None
        except (json.JSONDecodeError, AttributeError):
            parent_generation = None
    changes = parse_combined_report(report_path.read_text(errors="replace") if report_path.exists() else "")
    record = generation_record(
        machine_id=str(cfg["machine_id"]),
        install_id=str(cfg["install_id"]),
        profile_id=str(cfg["profile_id"]),
        folder_id=folder_id,
        filter_policy=effective_filter_fingerprint(config),
        changes=changes,
        parent_generation=parent_generation,
    )
    body = json.dumps(record, indent=2, sort_keys=True) + "\n"
    immutable_path = remote_join(remote_dir, f"generations/{record['generation_id']}.json")
    immutable = rclone_capture(cfg, ["rcat", immutable_path], input_text=body)
    if immutable.returncode != 0:
        append_log(cfg, f"[{now_iso()}] generation upload failed: {immutable.stdout or ''}\n")
        return int(immutable.returncode)
    latest = rclone_capture(cfg, ["rcat", latest_path], input_text=body)
    if latest.returncode != 0:
        append_log(cfg, f"[{now_iso()}] generation latest update failed: {latest.stdout or ''}\n")
        return int(latest.returncode)

    local_dir = state_root_path(cfg) / "generations" / str(cfg["machine_id"]) / folder_id
    atomic_write_text(local_dir / "generations" / f"{record['generation_id']}.json", body)
    atomic_write_text(local_dir / "latest.json", body)
    return 0


def fetch_remote_inventory(config: dict[str, Any], remote_scope: str) -> dict[str, dict[str, Any]]:
    result = rclone_capture(config, ["lsjson", remote_scope, "--recursive", "--hash"])
    if result.returncode != 0:
        raise TransferError((result.stdout or "remote inventory failed").strip())
    return parse_rclone_inventory(result.stdout or "[]")


def fetch_latest_generation(config: dict[str, Any], machine_id: str, folder_id: str) -> dict[str, Any] | None:
    remote_dir = generation_remote_dir(str(normalized_config(config)["remote_base"]), machine_id, folder_id)
    result = rclone_capture(config, ["cat", remote_join(remote_dir, "latest.json")])
    if result.returncode != 0:
        return None
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) and value.get("complete") is True else None


def change_intersects_scope(changes: list[dict[str, Any]], subpath: str | None) -> bool:
    scope = normalize_subpath(subpath)
    if not scope:
        return bool(changes)
    return any(
        (path := normalize_subpath(str(change.get("path") or ""))) == scope or path.startswith(f"{scope}/")
        for change in changes
        if change.get("path")
    )


def detect_link_generations(config: dict[str, Any]) -> list[dict[str, Any]]:
    store = LinkStore(state_root_path(config))
    notifications: list[dict[str, Any]] = []
    for link in store.list():
        peer = link["peer"]
        generation = fetch_latest_generation(config, str(peer["machine_id"]), str(peer["folder_id"]))
        if generation is None:
            store.update(str(link["id"]), status="peer_unavailable", last_checked_at=now_iso())
            continue
        if str(generation.get("install_id") or "") != str(peer.get("install_id") or ""):
            store.update(str(link["id"]), status="peer_replaced", last_checked_at=now_iso())
            continue
        if str(generation.get("filter_fingerprint") or "") != str(link.get("filter_fingerprint") or ""):
            store.update(str(link["id"]), status="filter_changed", last_checked_at=now_iso())
            continue
        generation_id = str(generation.get("generation_id") or "")
        known = str(link.get("last_seen_peer_generation") or link.get("pending_peer_generation") or link["baseline"].get("peer_generation") or "")
        if generation_id and generation_id != known:
            relevant = change_intersects_scope(list(generation.get("changes") or []), peer.get("subpath"))
            updates: dict[str, Any] = {
                "last_seen_peer_generation": generation_id,
                "last_checked_at": now_iso(),
            }
            if relevant:
                current_status = str(link.get("status") or "")
                updates["status"] = "changes_on_both" if current_status == "local_changes" else "peer_changes"
                updates["pending_peer_generation"] = generation_id
                notifications.append({"link_id": link["id"], "label": link["label"], "generation_id": generation_id})
            store.update(str(link["id"]), **updates)
        else:
            store.update(str(link["id"]), last_checked_at=now_iso())
    return notifications


def detect_local_link_changes(config: dict[str, Any], changed_folder_ids: list[str]) -> list[str]:
    store = LinkStore(state_root_path(config))
    changed_links: list[str] = []
    for link in store.list():
        if link["local"]["folder_id"] not in changed_folder_ids:
            continue
        folder = local_folder_by_id(config, str(link["local"]["folder_id"]))
        scope = resolve_local_scope(folder["local_path"], link["local"].get("subpath"), require_exists=False)
        current = local_inventory(scope) if scope.exists() else {}
        baseline = json.loads(Path(link["baseline"]["inventory_path"]).read_text())
        if inventories_equal(current, baseline):
            if link.get("status") == "local_changes":
                store.update(str(link["id"]), status="up_to_date", local_checked_at=now_iso())
            continue
        status = "changes_on_both" if link.get("status") in {"peer_changes", "changes_on_both"} else "local_changes"
        store.update(str(link["id"]), status=status, local_checked_at=now_iso())
        changed_links.append(str(link["id"]))
    return changed_links


def comparison_payload(
    config: dict[str, Any],
    remote_scope: str,
    local_scope: str | Path,
    selected_paths: list[str] | None = None,
) -> dict[str, Any]:
    remote = select_inventory(fetch_remote_inventory(config, remote_scope), selected_paths or [])
    local_path = Path(local_scope).expanduser()
    local = local_inventory(local_path, include_hashes=True) if local_path.exists() else {}
    local = select_inventory(local, selected_paths or [])
    result = compare_inventories(local, remote)
    result["remote_scope"] = remote_scope
    result["local_scope"] = str(local_path.resolve(strict=False))
    return result


def comparison_through_work_lane(
    config: dict[str, Any],
    remote_scope: str,
    local_scope: str | Path,
    selected_paths: list[str] | None = None,
) -> dict[str, Any]:
    try:
        response = daemon_api(
            config,
            "compare",
            _timeout_seconds=310.0,
            source=remote_scope,
            destination=str(local_scope),
            selected_paths=selected_paths or [],
        )
    except (FileNotFoundError, ConnectionRefusedError):
        return comparison_payload(config, remote_scope, local_scope, selected_paths)
    if not response.get("ok"):
        raise TransferError(str(response.get("error") or "comparison failed"))
    comparison = response.get("comparison")
    if not isinstance(comparison, dict):
        raise TransferError("daemon returned an invalid comparison")
    return comparison


def create_receive_job(
    config: dict[str, Any],
    *,
    source: str,
    destination: str,
    selected_paths: list[str],
    source_label: str = "peer",
    mode: str = "receive",
    baseline_inventory: dict[str, dict[str, Any]] | None = None,
    source_generation: str | None = None,
    link_id: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> tuple[int, dict[str, Any]]:
    source_inventory = fetch_remote_inventory(config, source)
    store = JobStore(state_root_path(config))
    job = store.create(
        source=source,
        destination=destination,
        source_label=source_label,
        selected_paths=selected_paths,
        mode=mode,
        source_inventory=source_inventory,
        baseline_inventory=baseline_inventory,
        source_generation=source_generation,
        link_id=link_id,
    )
    stage = str(Path(job["paths"]["staging"]))
    copy_selections: list[str] = []
    for selected in selected_paths:
        normalized = normalize_subpath(selected)
        entry = source_inventory.get(normalized)
        copy_selections.append(f"{normalized}/" if entry and entry.get("type") == "directory" else normalized)
    code = run_command(
        config,
        copy_cmd(config, source, stage, False, copy_selections),
        progress_callback=progress_callback,
    )
    if code != 0:
        job = store.load(job["id"])
        job["status"] = "staging_failed"
        job["error"] = f"rclone exit {code}"
        store.save(job)
        return code, job
    return 0, store.mark_staged(job["id"])


def run_receive_direct(
    config: dict[str, Any],
    source: str,
    destination: str,
    selected_paths: list[str],
    *,
    source_label: str = "peer",
    mode: str = "receive",
    baseline_inventory: dict[str, dict[str, Any]] | None = None,
    source_generation: str | None = None,
    link_id: str | None = None,
) -> int:
    with Lock(lock_file(config)):
        code, job = create_receive_job(
            config,
            source=source,
            destination=destination,
            selected_paths=selected_paths,
            source_label=source_label,
            mode=mode,
            baseline_inventory=baseline_inventory,
            source_generation=source_generation,
            link_id=link_id,
        )
    print(json.dumps(job, indent=2, sort_keys=True))
    return code


def policy_map(values: list[str]) -> dict[str, str]:
    policies: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise SystemExit(f"policy must be PATH=ACTION: {value}")
        raw_path, action = value.rsplit("=", 1)
        policies[normalize_subpath(raw_path)] = action.strip()
    return policies


def cmd_compare(args: argparse.Namespace) -> int:
    config = normalized_config(load_config(Path(args.config).expanduser()))
    print(json.dumps(comparison_through_work_lane(config, args.remote_scope, args.local_scope), indent=2, sort_keys=True))
    return 0


def cmd_receive(args: argparse.Namespace) -> int:
    config = normalized_config(load_config(Path(args.config).expanduser()))
    mode = "clone" if args.clone else "receive"
    if args.dry_run:
        print(json.dumps(comparison_through_work_lane(config, args.source, args.destination, args.select), indent=2, sort_keys=True))
        return 0
    payload = {
        "source": args.source,
        "destination": args.destination,
        "selected_paths": args.select,
        "source_label": args.source_label,
        "mode": mode,
    }
    try:
        response = daemon_api(config, "receive", **payload)
    except OSError:
        return run_receive_direct(config, **payload)
    if not response.get("ok"):
        raise SystemExit(str(response.get("error") or "daemon receive request failed"))
    print("receive job queued; run 'safe-sync jobs list' to follow it")
    return 0


def cmd_jobs(args: argparse.Namespace) -> int:
    config = normalized_config(load_config(Path(args.config).expanduser()))
    store = JobStore(state_root_path(config))
    if args.jobs_cmd == "list":
        value: Any = store.list()
    elif args.jobs_cmd == "show":
        value = store.load(args.job_id)
    elif args.jobs_cmd in {"apply", "reconcile", "rollback"}:
        policies = policy_map(args.policy) if args.jobs_cmd == "apply" else {}
        try:
            response = daemon_api(
                config,
                "job_operation",
                operation=args.jobs_cmd,
                job_id=args.job_id,
                policies=policies,
            )
        except OSError:
            with Lock(lock_file(config)):
                if args.jobs_cmd == "apply":
                    job = store.load(args.job_id)
                    revalidate_remote_job_source(config, job)
                    value = store.commit_clone(args.job_id) if job.get("mode") == "clone" else store.apply(args.job_id, policies)
                elif args.jobs_cmd == "reconcile":
                    value = store.reconcile(args.job_id)
                else:
                    value = store.rollback(args.job_id)
        else:
            if not response.get("ok"):
                raise SystemExit(str(response.get("error") or "daemon job operation failed"))
            print(f"{args.jobs_cmd} queued for {args.job_id}")
            return 0
    else:
        raise SystemExit(f"Unknown jobs command: {args.jobs_cmd}")
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


def remote_computers(config: dict[str, Any]) -> list[dict[str, Any]]:
    result = rclone_capture(config, ["lsf", registry_dir(config), "--files-only"])
    if result.returncode != 0:
        raise TransferError((result.stdout or "remote computer registry unavailable").strip())
    computers: list[dict[str, Any]] = []
    for name in (result.stdout or "").splitlines():
        if not name.endswith(".json"):
            continue
        document = rclone_capture(config, ["cat", remote_join(registry_dir(config), name)])
        if document.returncode == 0:
            try:
                computers.append(json.loads(document.stdout))
            except json.JSONDecodeError:
                continue
    return computers


def local_folder_by_id(config: dict[str, Any], folder_id: str) -> dict[str, Any]:
    for folder in normalized_config(config)["folders"]:
        if folder["id"] == folder_id:
            return folder
    raise TransferError(f"local folder not found: {folder_id}")


def peer_folder_by_id(config: dict[str, Any], machine_id: str, folder_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    for computer in remote_computers(config):
        if computer.get("machine_id") != machine_id:
            continue
        for folder in computer.get("folders") or []:
            if folder.get("id") == folder_id:
                return computer, folder
        raise TransferError(f"peer folder not found: {machine_id}/{folder_id}")
    raise TransferError(f"peer computer not found: {machine_id}")


def peer_remote_root(config: dict[str, Any], peer_folder: dict[str, Any]) -> str:
    remote_path = str(peer_folder.get("remote_path") or "")
    if not remote_path:
        raise TransferError("peer registry does not contain a remote path")
    return remote_join(str(normalized_config(config)["remote_base"]), remote_path)


def cmd_links(args: argparse.Namespace) -> int:
    config = normalized_config(load_config(Path(args.config).expanduser()))
    store = LinkStore(state_root_path(config))
    if args.links_cmd == "list":
        value: Any = store.list()
    elif args.links_cmd == "remove":
        value = {"removed": store.remove(args.link_id), "id": args.link_id}
    elif args.links_cmd == "add":
        local_folder = local_folder_by_id(config, args.local_folder)
        computer, peer_folder = peer_folder_by_id(config, args.peer_machine, args.peer_folder)
        local_root = resolve_local_scope(local_folder["local_path"], args.local_subpath)
        remote_root = join_remote_scope(peer_remote_root(config, peer_folder), args.peer_subpath)
        local_value = local_inventory(local_root)
        peer_value = fetch_remote_inventory(config, remote_root)
        latest_generation = fetch_latest_generation(config, args.peer_machine, args.peer_folder)
        peer_filter = str((latest_generation or {}).get("filter_fingerprint") or peer_folder.get("filter_fingerprint") or "missing")
        peer_install = str((latest_generation or {}).get("install_id") or computer.get("install_id") or "")
        value = store.add(
            label=args.label or args.local_subpath or args.local_folder,
            local_profile_id=str(config["profile_id"]),
            local_folder_id=args.local_folder,
            local_subpath=args.local_subpath,
            peer_machine_id=args.peer_machine,
            peer_install_id=peer_install,
            peer_folder_id=args.peer_folder,
            peer_subpath=args.peer_subpath,
            local_filter_fingerprint=effective_filter_fingerprint(folder_config(config, local_folder)),
            peer_filter_fingerprint=peer_filter,
            local_inventory_value=local_value,
            peer_inventory_value=peer_value,
            peer_generation=str((latest_generation or {}).get("generation_id") or "") or None,
        )
    elif args.links_cmd == "review":
        link = store.get(args.link_id)
        local_folder = local_folder_by_id(config, str(link["local"]["folder_id"]))
        _computer, peer_folder = peer_folder_by_id(config, str(link["peer"]["machine_id"]), str(link["peer"]["folder_id"]))
        destination = resolve_local_scope(local_folder["local_path"], link["local"].get("subpath"), require_exists=False)
        source = join_remote_scope(peer_remote_root(config, peer_folder), link["peer"].get("subpath"))
        baseline = json.loads(Path(link["baseline"]["inventory_path"]).read_text())
        payload = {
            "source": source,
            "destination": str(destination),
            "selected_paths": [],
            "source_label": str(link["peer"]["machine_id"]),
            "mode": "receive",
            "baseline_inventory": baseline,
            "source_generation": link.get("pending_peer_generation") or link.get("last_seen_peer_generation"),
            "link_id": link["id"],
        }
        try:
            response = daemon_api(config, "receive", **payload)
        except OSError:
            return run_receive_direct(config, **payload)
        if not response.get("ok"):
            raise SystemExit(str(response.get("error") or "linked-folder review queue failed"))
        print(f"linked-folder review queued for {link['id']}")
        return 0
    elif args.links_cmd == "status":
        links = [link for link in store.list() if not args.link_id or link["id"] == args.link_id]
        value = []
        for link in links:
            local_folder = local_folder_by_id(config, link["local"]["folder_id"])
            computer, peer_folder = peer_folder_by_id(config, link["peer"]["machine_id"], link["peer"]["folder_id"])
            local_root = resolve_local_scope(local_folder["local_path"], link["local"].get("subpath"), require_exists=False)
            remote_root = join_remote_scope(peer_remote_root(config, peer_folder), link["peer"].get("subpath"))
            local_value = local_inventory(local_root) if local_root.exists() else {}
            peer_value = fetch_remote_inventory(config, remote_root)
            latest_generation = fetch_latest_generation(config, str(link["peer"]["machine_id"]), str(link["peer"]["folder_id"]))
            status = store.status(
                link,
                local_inventory_value=local_value,
                peer_inventory_value=peer_value,
                peer_install_id=str((latest_generation or {}).get("install_id") or computer.get("install_id") or ""),
                peer_filter_fingerprint=str((latest_generation or {}).get("filter_fingerprint") or peer_folder.get("filter_fingerprint") or "missing"),
            )
            if status["comparison"] and status["comparison"].get("equal"):
                updated = store.accept_baseline(
                    str(link["id"]),
                    local_value,
                    peer_generation=str((latest_generation or {}).get("generation_id") or "") or None,
                )
            else:
                updated = store.update(str(link["id"]), status=status["status"], last_checked_at=now_iso())
            value.append({"link": updated, **status})
    else:
        raise SystemExit(f"Unknown links command: {args.links_cmd}")
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    config = normalized_config(load_config(Path(args.config).expanduser()))
    folder = local_folder_by_id(config, args.folder)
    if args.receive:
        retained_path = normalize_subpath(args.receive)
        if "/" not in retained_path:
            raise TransferError("history recovery path must include its timestamp and relative file path")
        timestamp, relative_path = retained_path.split("/", 1)
        payload = {
            "source": join_remote_scope(str(folder["trash_root"]), timestamp),
            "destination": str(Path(folder["local_path"]).expanduser()),
            "selected_paths": [normalize_subpath(relative_path)],
            "source_label": "history",
            "mode": "receive",
        }
        try:
            response = daemon_api(config, "receive", **payload)
        except OSError:
            return run_receive_direct(config, **payload)
        if not response.get("ok"):
            raise SystemExit(str(response.get("error") or "history recovery queue failed"))
        print("history recovery job queued; review it with 'safe-sync jobs list'")
        return 0
    target = join_remote_scope(str(folder["trash_root"]), args.subpath)
    result = rclone_capture(config, ["lsjson", target, "--recursive", "--hash"])
    if result.returncode != 0:
        print(result.stdout or "", end="")
        return int(result.returncode)
    print(json.dumps({"folder_id": args.folder, "remote_trash": target, "entries": json.loads(result.stdout or "[]")}, indent=2, sort_keys=True))
    return 0


def cmd_registry(args: argparse.Namespace) -> int:
    config = normalized_config(load_config(Path(args.config).expanduser()))
    if args.registry_cmd == "update":
        result = rclone_capture(config, ["rcat", registry_path(config)], input_text=json.dumps(registry_doc(config), indent=2, sort_keys=True) + "\n")
        print(result.stdout or "", end="")
        if result.returncode == 0:
            print(registry_path(config))
        return int(result.returncode)
    if args.registry_cmd == "path":
        print(registry_path(config))
        return 0
    raise SystemExit(f"Unknown registry command: {args.registry_cmd}")


def cmd_computers(args: argparse.Namespace) -> int:
    config = normalized_config(load_config(Path(args.config).expanduser()))
    ensure_local_profiles_registered(config)
    result = rclone_capture(config, ["lsf", registry_dir(config), "--files-only"])
    if result.returncode != 0:
        print(result.stdout or "", end="")
        return int(result.returncode)
    computers = []
    for name in (result.stdout or "").splitlines():
        if not name.endswith(".json"):
            continue
        path = remote_join(registry_dir(config), name)
        cat = rclone_capture(config, ["cat", path])
        if cat.returncode != 0:
            computers.append({"registry_file": name, "error": cat.stdout.strip()})
            continue
        try:
            computers.append(json.loads(cat.stdout))
        except json.JSONDecodeError:
            computers.append({"registry_file": name, "error": "invalid json"})
    print(json.dumps(computers, indent=2, sort_keys=True))
    return 0


def cmd_render_install(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser()
    load_config(config_path)
    ensure_filter_template(DEFAULT_FILTER)
    install_dir = Path(args.output_dir).expanduser()
    install_dir.mkdir(parents=True, exist_ok=True)
    program = Path(args.program).expanduser()
    if not program.is_absolute():
        program = Path.cwd() / program
    files = {
        "com.safe-sync.daemon.plist": launchd_plist(config_path, program),
        "safe-sync-daemon.service": systemd_unit(config_path, program),
        "install-service.sh": install_script(install_dir),
    }
    for name, content in files.items():
        target = install_dir / name
        target.write_text(content)
        if name.endswith(".sh"):
            target.chmod(0o755)
        print(target)
    return 0


def cmd_help(_args: argparse.Namespace) -> int:
    """Print the canonical guide shipped with both source and installed runtimes."""
    if not USER_GUIDE.exists():
        raise SystemExit(f"Safe Sync user guide is missing: {USER_GUIDE}")
    print(USER_GUIDE.read_text(), end="")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="safe-sync",
        description="Safe one-way Dropbox backup and explicit cross-computer transfer.",
        epilog="Run 'safe-sync help' for the complete install, setup, usage, recovery, and uninstall guide.",
    )
    p.add_argument("--config", default=str(DEFAULT_CONFIG), help="Use a specific configuration file")
    sub = p.add_subparsers(
        dest="cmd",
        required=True,
        metavar="{help,setup,connect-dropbox,backup,start,stop,restart,status,logs,doctor,folders,profiles,computers,compare,receive,jobs,links,history,pull,list,config,autostart,rclone,init-config,migrate-config}",
    )

    help_cmd = sub.add_parser("help", help="Print the complete user guide")
    help_cmd.set_defaults(func=cmd_help)

    init = sub.add_parser("init-config", help="Create an empty configuration (advanced)")
    init.add_argument("--force", action="store_true", help="Replace an existing configuration")
    init.add_argument("--machine", help="Initial machine id")
    init.set_defaults(func=cmd_init_config)

    setup = sub.add_parser("setup", help="Create or validate local configuration and verify Dropbox")
    setup.add_argument("--remote", help="rclone base path, e.g. dropbox:computer-backups")
    setup.add_argument("--folder", action="append", default=[], help="Local folder to add to the active profile; may be repeated")
    setup.add_argument("--machine", help="Machine id to use only when creating a new config")
    setup.add_argument("--allow-unsafe-local-path", action="store_true", help="Explicitly allow protected broad paths such as ~/projects for folders in this setup command")
    setup.add_argument("--skip-remote-check", action="store_true", help=argparse.SUPPRESS)
    setup.add_argument("--skip-start", action="store_true", help=argparse.SUPPRESS)
    setup.set_defaults(func=cmd_setup)

    connect_dropbox = sub.add_parser("connect-dropbox", help="Connect the default Safe Sync Dropbox remote")
    connect_dropbox.add_argument("--headless", action="store_true", help="Request a browser-machine token instead of opening a local browser")
    connect_dropbox.add_argument("--reconnect", action="store_true", help="Replace an existing Dropbox authorization")
    connect_dropbox.set_defaults(func=cmd_connect_dropbox)

    migrate = sub.add_parser("migrate-config", help="Migrate a legacy configuration")
    migrate.add_argument("--from-path", default=str(LEGACY_CONFIG), help="Legacy JSON path")
    migrate.add_argument("--force", action="store_true", help="Replace an existing destination")
    migrate.set_defaults(func=cmd_migrate_config)

    backup = sub.add_parser("backup", help="Queue a backup or run a selected direct backup")
    backup.add_argument("folder", nargs="?", help="Folder id for a direct backup")
    backup.add_argument("--all", action="store_true", help="Run all enabled folders directly")
    backup.add_argument("--dry-run", action="store_true", help="Show changes without modifying Dropbox")
    backup.set_defaults(func=cmd_backup)

    daemon = sub.add_parser("daemon", help=argparse.SUPPRESS)
    daemon.add_argument("--dry-run", action="store_true")
    daemon.add_argument("--once", action="store_true", help="Exit after the first backup attempt")
    daemon.add_argument("--poll-interval", type=int)
    daemon.add_argument("--debounce", type=int)
    daemon.add_argument("--max-loops", type=int, help=argparse.SUPPRESS)
    daemon.set_defaults(func=cmd_daemon)

    start = sub.add_parser("start", help="Start the per-user backend service")
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser("stop", help="Stop the per-user backend service")
    stop.set_defaults(func=cmd_stop)

    restart = sub.add_parser("restart", help="Restart the per-user backend service")
    restart.set_defaults(func=cmd_restart)

    autostart = sub.add_parser("autostart", help="Inspect or control login startup")
    autostart_sub = autostart.add_subparsers(dest="autostart_target", required=True)
    backend = autostart_sub.add_parser("backend", help="Manage backend login startup")
    backend_sub = backend.add_subparsers(dest="autostart_action", required=True)
    for action in ("status", "enable", "disable"):
        backend_sub.add_parser(action).set_defaults(func=cmd_autostart)

    compare = sub.add_parser("compare", help="Read-only comparison of a remote folder and local folder")
    compare.add_argument("remote_scope", help="Full rclone remote folder path")
    compare.add_argument("local_scope", help="Local folder path; it may not exist yet")
    compare.set_defaults(func=cmd_compare)

    receive = sub.add_parser("receive", help="Create a staged, verified receive job")
    receive.add_argument("source", help="Full rclone remote folder path")
    receive.add_argument("destination", help="Local destination directory")
    receive.add_argument("--select", action="append", default=[], help="Relative file or folder path to receive; may be repeated")
    receive.add_argument("--source-label", default="peer", help="Friendly source label used for keep-both names")
    receive.add_argument("--clone", action="store_true", help="Require a new/empty destination and commit as one cloned folder")
    receive.add_argument("--dry-run", action="store_true", help="Compare only; do not stage any files")
    receive.set_defaults(func=cmd_receive)

    jobs = sub.add_parser("jobs", help="Inspect, apply, reconcile, or roll back receive jobs")
    jobs_sub = jobs.add_subparsers(dest="jobs_cmd", required=True)
    jobs_sub.add_parser("list", help="List receive jobs").set_defaults(func=cmd_jobs)
    jobs_show = jobs_sub.add_parser("show", help="Show one receive job")
    jobs_show.add_argument("job_id")
    jobs_show.set_defaults(func=cmd_jobs)
    jobs_apply = jobs_sub.add_parser("apply", help="Apply reviewed actions; missing files are added by default")
    jobs_apply.add_argument("job_id")
    jobs_apply.add_argument("--policy", action="append", default=[], metavar="PATH=ACTION", help="Decision: keep_local, keep_both, replace, delete, leave_staged, or add")
    jobs_apply.set_defaults(func=cmd_jobs)
    jobs_reconcile = jobs_sub.add_parser("reconcile", help="Safely finish an interrupted journal transition")
    jobs_reconcile.add_argument("job_id")
    jobs_reconcile.set_defaults(func=cmd_jobs)
    jobs_rollback = jobs_sub.add_parser("rollback", help="Conditionally restore checkpointed files")
    jobs_rollback.add_argument("job_id")
    jobs_rollback.set_defaults(func=cmd_jobs)

    links = sub.add_parser("links", help="Manage granular, manually approved linked folders")
    links_sub = links.add_subparsers(dest="links_cmd", required=True)
    links_sub.add_parser("list", help="List local link declarations").set_defaults(func=cmd_links)
    links_add = links_sub.add_parser("add", help="Link two already-converged folder scopes")
    links_add.add_argument("local_folder", help="Local configured folder id")
    links_add.add_argument("peer_machine", help="Published peer machine id")
    links_add.add_argument("peer_folder", help="Published peer folder id")
    links_add.add_argument("--local-subpath", default="", help="Granular path inside the local folder")
    links_add.add_argument("--peer-subpath", default="", help="Granular path inside the peer folder")
    links_add.add_argument("--label", help="Friendly link label")
    links_add.set_defaults(func=cmd_links)
    links_status = links_sub.add_parser("status", help="Run a read-only three-way status comparison")
    links_status.add_argument("link_id", nargs="?")
    links_status.set_defaults(func=cmd_links)
    links_review = links_sub.add_parser("review", help="Stage the peer scope for three-way review")
    links_review.add_argument("link_id")
    links_review.set_defaults(func=cmd_links)
    links_remove = links_sub.add_parser("remove", help="Remove a link without changing files")
    links_remove.add_argument("link_id")
    links_remove.set_defaults(func=cmd_links)

    history = sub.add_parser("history", help="List recoverable Safe Sync remote-trash versions")
    history.add_argument("folder", help="Local configured folder id")
    history.add_argument("--subpath", default="", help="Optional path below that folder's remote trash")
    history.add_argument("--receive", metavar="TIMESTAMP/PATH", help="Stage one retained version as a receive job for review")
    history.set_defaults(func=cmd_history)

    pull = sub.add_parser("pull", help="Compatibility alias that now creates a safe staged receive job")
    pull.add_argument("source", help="Full rclone source path, e.g. dropbox:computer-backups/test/linux/test_sync/data")
    pull.add_argument("destination", help="Local destination directory")
    pull.add_argument("--dry-run", action="store_true", help="Show a read-only comparison without staging files")
    pull.add_argument("--select", action="append", default=[], help="Relative file or folder path to stage from the source")
    pull.set_defaults(func=cmd_pull)

    list_cmd = sub.add_parser("list", help="List entries below a remote path")
    list_cmd.add_argument("target", help="Full rclone remote path")
    list_cmd.add_argument("--depth", type=int, default=1, help="Maximum directory depth (default: 1)")
    list_cmd.set_defaults(func=cmd_list)

    rclone = sub.add_parser("rclone", help="Run the rclone binary managed by Safe Sync")
    rclone.add_argument("rclone_args", nargs=argparse.REMAINDER)
    rclone.set_defaults(func=cmd_rclone)

    config_cmd = sub.add_parser("config", help="Show or update effective local settings")
    config_sub = config_cmd.add_subparsers(dest="config_cmd", required=True)
    config_show = config_sub.add_parser("show", help="Print effective settings")
    config_show.set_defaults(func=cmd_config)
    config_update = config_sub.add_parser("update", help="Update labels, remote base, and timing controls")
    config_update.add_argument("--machine-label", help="Human-readable machine label")
    config_update.add_argument("--profile-label", help="Human-readable active-profile label")
    config_update.add_argument("--remote-base", help="Remote base such as dropbox:computer-backups")
    config_update.add_argument("--poll-interval-seconds", type=int, required=True, help="Polling-fallback loop interval")
    config_update.add_argument("--debounce-seconds", type=int, required=True, help="Quiet time before automatic backup")
    config_update.add_argument("--min-interval-seconds", type=int, required=True, help="Minimum interval between automatic backups")
    config_update.add_argument("--fallback-interval-seconds", type=int, required=True, help="Periodic reconciliation interval")
    config_update.add_argument("--rate-limit-backoff-seconds", type=int, required=True, help="Default Dropbox cooldown")
    config_update.set_defaults(func=cmd_config)

    status = sub.add_parser("status", help="Show service, watcher, queue, and sync health")
    status.set_defaults(func=cmd_status)

    login_check = sub.add_parser("login-check", help=argparse.SUPPRESS)
    login_check.set_defaults(func=cmd_login_check)

    folders_cmd = sub.add_parser("folders", help="List or manage watched folders")
    folders_sub = folders_cmd.add_subparsers(dest="folder_cmd", required=True)
    folders_list = folders_sub.add_parser("list", help="Print configured folders")
    folders_list.set_defaults(func=cmd_folders)
    folders_add = folders_sub.add_parser("add", help="Add a folder to the active profile")
    folders_add.add_argument("id", help="Stable folder id")
    folders_add.add_argument("local_path", help="Existing local directory")
    folders_add.add_argument("--label", help="Human-readable folder label")
    folders_add.add_argument("--remote-path", help="Override the owned path below the remote base")
    folders_add.add_argument("--trash-path", help="Override the recoverable remote-trash path")
    folders_add.add_argument("--filter-file", help="Override the rclone filter file")
    folders_add.add_argument("--disabled", action="store_true", help="Add without enabling backups")
    folders_add.set_defaults(func=cmd_folders)

    folders_update = folders_sub.add_parser("update", help="Change a configured folder")
    folders_update.add_argument("id", help="Existing folder id")
    folders_update.add_argument("local_path", help="Existing local directory")
    folders_update.add_argument("--label", help="Human-readable folder label")
    folders_update.add_argument("--filter-file", help="Override the rclone filter file")
    folders_update.add_argument("--enabled", action="store_true", help="Enable automatic backups")
    folders_update.add_argument("--disabled", action="store_true", help="Disable automatic backups")
    folders_update.set_defaults(func=cmd_folders)

    folders_remove = folders_sub.add_parser("remove", help="Stop managing a folder without deleting data")
    folders_remove.add_argument("id", help="Existing folder id")
    folders_remove.set_defaults(func=cmd_folders)

    profiles = sub.add_parser("profiles", help="List or manage local computer identities")
    profiles_sub = profiles.add_subparsers(dest="profile_cmd", required=True)
    profiles_list = profiles_sub.add_parser("list", help="Print local profiles")
    profiles_list.set_defaults(func=cmd_profiles)
    profiles_add = profiles_sub.add_parser("add", help="Add a local computer identity")
    profiles_add.add_argument("id", help="Stable profile id")
    profiles_add.add_argument("--label", help="Human-readable profile label")
    profiles_add.add_argument("--machine-id", help="Published machine id")
    profiles_add.add_argument("--machine-label", help="Human-readable machine label")
    profiles_add.add_argument("--remote-base", help="Remote base such as dropbox:computer-backups")
    profiles_add.set_defaults(func=cmd_profiles)
    profiles_activate = profiles_sub.add_parser("activate", help="Switch the active local identity and folder set")
    profiles_activate.add_argument("id", help="Existing profile id")
    profiles_activate.set_defaults(func=cmd_profiles)

    computers = sub.add_parser("computers", help="List computers published to Dropbox")
    computers.set_defaults(func=cmd_computers)

    registry = sub.add_parser("registry", help=argparse.SUPPRESS)
    registry_sub = registry.add_subparsers(dest="registry_cmd", required=True)
    registry_update = registry_sub.add_parser("update")
    registry_update.set_defaults(func=cmd_registry)
    registry_path_cmd = registry_sub.add_parser("path")
    registry_path_cmd.set_defaults(func=cmd_registry)

    logs = sub.add_parser("logs", help="Print recent Safe Sync logs")
    logs.add_argument("--lines", type=int, default=80, help="Number of recent lines (default: 80)")
    logs.set_defaults(func=cmd_logs)

    doctor = sub.add_parser("doctor", help="Check configuration and Dropbox connectivity")
    doctor.set_defaults(func=cmd_doctor)

    render = sub.add_parser("render-install", help=argparse.SUPPRESS)
    render.add_argument("--output-dir", required=True)
    render.add_argument("--program", default=str(PROJECT_ROOT / "bin" / "safe-sync"))
    render.set_defaults(func=cmd_render_install)

    sub._choices_actions = [
        action for action in sub._choices_actions
        if action.dest not in {"daemon", "render-install", "registry", "login-check"}
    ]
    return p


def main() -> int:
    args = parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
