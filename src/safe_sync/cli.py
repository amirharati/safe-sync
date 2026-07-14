"""Safe Sync: small rclone guardrail CLI."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from safe_sync.daemon import DaemonState, WatchDaemon, WatchSettings, scan_tree
from safe_sync.service import (
    backend_autostart_cmd,
    backend_autostart_status_text,
    install_script,
    launchd_plist,
    service_cmd,
    service_status_text,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_HOME = Path.home() / ".safe-sync"
DEFAULT_CONFIG = CONFIG_HOME / "config.json"
LEGACY_CONFIG = Path.home() / ".config" / "safe-sync" / "config.json"
DEFAULT_STATUS = Path.home() / ".local" / "state" / "safe-sync" / "status.json"
DEFAULT_LOG_DIR = Path.home() / ".local" / "log" / "safe-sync"
DEFAULT_FILTER = CONFIG_HOME / "filter.txt"
TEMPLATE_FILTER = PROJECT_ROOT / "config" / "filter.txt"

RATE_LIMIT_PATTERNS = ("too_many_requests", "rate limit", "rate_limit", "retry-after")


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
    status_path.write_text(json.dumps(previous, indent=2, sort_keys=True) + "\n")


def log_path(config: dict[str, Any]) -> Path:
    log_dir = Path(config.get("log_dir", DEFAULT_LOG_DIR)).expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"safe-sync-{dt.date.today().isoformat()}.log"


def append_log(config: dict[str, Any], line: str) -> None:
    path = log_path(config)
    with path.open("a") as fh:
        fh.write(line)


def recent_log_text(config: dict[str, Any], max_chars: int = 12000) -> str:
    path = log_path(config)
    if not path.exists():
        return ""
    text = path.read_text(errors="replace")
    return text[-max_chars:]


def looks_rate_limited(config: dict[str, Any]) -> bool:
    text = recent_log_text(config).lower()
    return any(pattern in text for pattern in RATE_LIMIT_PATTERNS)


def run_command(config: dict[str, Any], cmd: list[str], dry_run: bool = False) -> int:
    log = log_path(config)
    log.parent.mkdir(parents=True, exist_ok=True)
    header = f"\n[{now_iso()}] $ {' '.join(cmd)}\n"
    timeout = int(config.get("command_timeout_seconds", 180))
    with log.open("a") as fh:
        fh.write(header)
        fh.flush()
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
            output = result.stdout or ""
            print(output, end="")
            fh.write(output)
            fh.write(f"[{now_iso()}] exit={result.returncode} dry_run={dry_run}\n")
            return int(result.returncode)
        except subprocess.TimeoutExpired as exc:
            output = exc.stdout or ""
            if isinstance(output, bytes):
                output = output.decode(errors="replace")
            print(output, end="")
            fh.write(output)
            message = f"[{now_iso()}] timeout after {timeout}s; treating run as failed\n"
            print(message, end="")
            fh.write(message)
            return 124


def rclone_bin(config: dict[str, Any]) -> str:
    configured = config.get("rclone_bin")
    if configured:
        return str(Path(configured).expanduser())
    found = shutil.which("rclone")
    if not found:
        raise SystemExit("rclone not found in PATH")
    return found


def filter_file(config: dict[str, Any]) -> Path:
    return Path(config["filter_file"]).expanduser()


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
    for folder in enabled_folders(config):
        path = resolved_path(str(folder["local_path"]))
        reason = unsafe_local_path_reason(path)
        if reason and not config.get("allow_unsafe_local_path") and not folder.get("allow_unsafe_local_path"):
            raise SystemExit(f"{reason}\nSet allow_unsafe_local_path=true only if you are certain.")


def safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower()).strip("-")
    return cleaned or "default"


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


def normalized_config(config: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(config)
    machine_id = str(normalized.get("machine_id") or normalized.get("machine") or machine_name())
    normalized.setdefault("machine", machine_id)
    normalized["machine_id"] = machine_id
    normalized.setdefault("machine_label", machine_id)
    normalized.setdefault("install_id", default_install_id())
    normalized.setdefault("remote_base", "dropbox:computer-backups/test")
    if not normalized.get("folders"):
        normalized["folders"] = [legacy_folder(normalized)]
    for folder in normalized["folders"]:
        folder["id"] = safe_id(str(folder.get("id") or Path(str(folder.get("local_path", "default"))).name))
        folder.setdefault("label", folder["id"])
        folder.setdefault("enabled", True)
        folder.setdefault("filter_file", str(normalized.get("filter_file", DEFAULT_FILTER)))
        folder.setdefault("remote_path", f"{machine_id}/{folder['id']}")
        folder.setdefault("trash_path", f".trash/{machine_id}/{folder['id']}")
        folder.setdefault("remote_root", remote_join(str(normalized["remote_base"]), str(folder["remote_path"])))
        folder.setdefault("trash_root", remote_join(str(normalized["remote_base"]), str(folder["trash_path"])))
    return normalized


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
            }
            for folder in cfg["folders"]
        ],
    }


class Lock:
    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "Lock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self.fd, str(os.getpid()).encode())
            return self
        except FileExistsError:
            pid = self.path.read_text(errors="ignore").strip()
            raise SystemExit(f"Safe Sync already running (lock {self.path}, pid {pid or 'unknown'})")

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
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=45)
    if result.returncode != 0:
        append_log(config, f"[{now_iso()}] preflight failed:\n{result.stdout}\n")
        raise SystemExit("Remote preflight failed; see log")


def backup_cmd(config: dict[str, Any], dry_run: bool) -> list[str]:
    remote = config["remote_root"].rstrip("/")
    local = str(Path(config["local_path"]).expanduser())
    trash = f"{config['trash_root'].rstrip('/')}/{stamp()}"
    cmd = [
        rclone_bin(config), "sync", local, remote,
        "--filter-from", str(filter_file(config)),
        "--backup-dir", trash,
        "--stats", "10s",
        "--max-duration", f"{int(config.get('rclone_max_duration_seconds', 120))}s",
        "--timeout", "30s", "--contimeout", "10s",
        "--retries", "1", "--low-level-retries", "1", "--retries-sleep", "5s",
        "--log-level", "INFO",
    ]
    if config.get("preserve_metadata"):
        cmd.append("--metadata")
    if dry_run:
        cmd.append("--dry-run")
    return cmd


def copy_cmd(config: dict[str, Any], src: str, dst: str, dry_run: bool) -> list[str]:
    cmd = [
        rclone_bin(config), "copy", src, dst,
        "--filter-from", str(filter_file(config)),
        "--stats", "10s",
        "--max-duration", f"{int(config.get('rclone_max_duration_seconds', 120))}s",
        "--timeout", "30s", "--contimeout", "10s",
        "--retries", "1", "--low-level-retries", "1", "--retries-sleep", "5s",
        "--log-level", "INFO",
    ]
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
    folder_id = "test_sync"
    remote_base = "dropbox:computer-backups/test"
    return {
        "machine": machine,
        "machine_id": machine,
        "machine_label": machine,
        "install_id": default_install_id(),
        "remote_base": remote_base,
        "folders": [
            {
                "id": folder_id,
                "label": "Test Sync",
                "local_path": "~/test_sync",
                "remote_path": f"{machine}/{folder_id}",
                "trash_path": f".trash/{machine}/{folder_id}",
                "filter_file": str(DEFAULT_FILTER),
                "enabled": True,
            }
        ],
        "filter_file": str(DEFAULT_FILTER),
        "status_path": str(DEFAULT_STATUS),
        "log_dir": str(DEFAULT_LOG_DIR),
        "lock_file": str(Path.home() / ".local" / "state" / "safe-sync" / "safe-sync.lock"),
        "command_timeout_seconds": 180,
        "rclone_max_duration_seconds": 120,
        "poll_interval_seconds": 5,
        "debounce_seconds": 20,
        "min_interval_seconds": 120,
        "fallback_interval_seconds": 1800,
        "rate_limit_backoff_seconds": 300,
        "preserve_metadata": False,
    }


def cmd_init_config(args: argparse.Namespace) -> int:
    path = Path(args.config).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not args.force:
        raise SystemExit(f"Config already exists: {path}")
    config = default_config(args.machine or machine_name())
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
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
    dst.write_text(json.dumps(normalized_config(config), indent=2, sort_keys=True) + "\n")
    print(f"migrated {src} -> {dst}")
    return 0


def run_backup_with_config(config: dict[str, Any], dry_run: bool) -> int:
    with Lock(lock_file(config)):
        save_status(
            config,
            state="syncing",
            folder_id=config.get("folder_id"),
            last_start=now_iso(),
            last_command="backup",
            last_error=None,
        )
        try:
            preflight(config)
            code = run_command(config, backup_cmd(config, dry_run), dry_run=dry_run)
        except BaseException as exc:
            save_status(config, state="error", folder_id=config.get("folder_id"), last_error=str(exc), last_finish=now_iso())
            raise
        if code == 0:
            save_status(config, state="idle", folder_id=config.get("folder_id"), last_success=now_iso(), last_finish=now_iso(), last_error=None)
        else:
            save_status(config, state="error", folder_id=config.get("folder_id"), last_error=f"rclone exit {code}", last_finish=now_iso())
        return code


def cmd_backup(args: argparse.Namespace) -> int:
    config = normalized_config(load_config(Path(args.config).expanduser()))
    validate_local_path(config)
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


def cmd_pull(args: argparse.Namespace) -> int:
    config = normalized_config(load_config(Path(args.config).expanduser()))
    src = args.source
    dst = args.destination
    with Lock(lock_file(config)):
        save_status(config, state="syncing", last_start=now_iso(), last_command="pull", last_error=None)
        try:
            code = run_command(config, copy_cmd(config, src, dst, args.dry_run), dry_run=args.dry_run)
        except BaseException as exc:
            save_status(config, state="error", last_error=str(exc), last_finish=now_iso())
            raise
        save_status(config, state="idle" if code == 0 else "error", last_success=now_iso() if code == 0 else None, last_error=None if code == 0 else f"rclone exit {code}", last_finish=now_iso())
        return code


def cmd_list(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config).expanduser())
    return run_command(config, [rclone_bin(config), "lsf", args.target, "--max-depth", str(args.depth)])


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
    if last_error:
        health = "error"
        reason = str(last_error)
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


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config).expanduser())
    status_path = Path(config.get("status_path", DEFAULT_STATUS)).expanduser()
    if status_path.exists():
        try:
            sync_state = json.loads(status_path.read_text())
        except json.JSONDecodeError:
            sync_state = {"state": "unknown", "status_path": str(status_path), "error": "status JSON is invalid"}
    else:
        sync_state = {"state": "unknown", "status_path": str(status_path)}

    service_text = service_status_text()
    service_state = service_text.split(":", 1)[1].strip() if ":" in service_text else service_text
    health = status_health(config, service_state, sync_state)
    print(json.dumps({
        "daemon_seen_at": health["daemon_seen_at"],
        "health": health["health"],
        "health_reason": health["reason"],
        "log": str(log_path(config)),
        "service_state": service_state,
        "sync_state": sync_state,
    }, indent=2, sort_keys=True))
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


def folder_snapshots(config: dict[str, Any]) -> dict[str, dict[str, tuple[int, int]]]:
    snapshots: dict[str, dict[str, tuple[int, int]]] = {}
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
    config = normalized_config(load_config(Path(args.config).expanduser()))
    validate_local_path(config)
    settings = watch_settings_from_config(config, args)
    daemon = WatchDaemon(settings)
    folders = enabled_folders(config)
    if not folders:
        raise SystemExit("No enabled folders configured")

    previous_snapshots = folder_snapshots(config)
    save_status(
        config,
        state="watching",
        watcher="polling",
        folders=[{"id": folder["id"], "local_path": str(Path(folder["local_path"]).expanduser())} for folder in folders],
        dry_run=args.dry_run,
        poll_interval_seconds=settings.poll_interval_seconds,
        debounce_seconds=settings.debounce_seconds,
        fallback_interval_seconds=settings.fallback_interval_seconds,
        last_error=None,
    )
    append_log(config, f"[{now_iso()}] daemon started watcher=polling folders={','.join(folder['id'] for folder in folders)} dry_run={args.dry_run}\n")

    loops = 0
    while True:
        loops += 1
        now = time.monotonic()
        current_snapshots = folder_snapshots(config)
        changed = [folder_id for folder_id, snapshot in current_snapshots.items() if snapshot != previous_snapshots.get(folder_id)]
        if changed:
            daemon.mark_dirty(now)
            previous_snapshots = current_snapshots
            save_status(config, state="dirty", changed_folders=changed, last_change=now_iso(), watcher="polling")

        if daemon.state.state == DaemonState.BACKOFF:
            if daemon.backoff_expired(now):
                daemon.state.state = DaemonState.DIRTY
                daemon.mark_dirty(now)
                save_status(config, state="dirty", last_error=None, note="backoff expired")
            else:
                save_status(config, state="backoff", backoff_remaining_seconds=round(daemon.backoff_remaining(now), 1))
                if args.once or (args.max_loops and loops >= args.max_loops):
                    return 75
                time.sleep(settings.poll_interval_seconds)
                continue

        should_run = daemon.should_sync_after_debounce(now) or daemon.should_run_fallback(now)
        if should_run and daemon.in_min_interval(now):
            save_status(config, state="cooldown", cooldown_remaining_seconds=round(daemon.min_interval_remaining(now), 1))
            should_run = False

        if should_run:
            daemon.note_sync_started(now)
            save_status(config, state="syncing", last_start=now_iso(), last_command="daemon")
            failed_folder = None
            try:
                code, failed_folder = run_all_backups(config, args.dry_run)
                error_text = f"rclone exit {code}" if code != 0 else None
            except SystemExit as exc:
                code = int(exc.code) if isinstance(exc.code, int) else 75
                error_text = str(exc) or "backup failed"
            after = time.monotonic()
            should_backoff = code != 0
            rate_limited = should_backoff and looks_rate_limited(config)
            daemon.note_sync_finished(after, rate_limited=should_backoff)
            if code == 0:
                previous_snapshots = folder_snapshots(config)
                save_status(config, state="watching", last_success=now_iso(), last_error=None)
            else:
                reason = "rate limited" if rate_limited else "remote/preflight failed"
                save_status(config, state="backoff", failed_folder=failed_folder, last_error=f"{error_text}; {reason}")
            if args.once:
                return code

        if args.max_loops and loops >= args.max_loops:
            save_status(config, state="watching", note="max loops reached")
            return 0
        time.sleep(settings.poll_interval_seconds)



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
        config_path.write_text(json.dumps(normalized_config(config), indent=2, sort_keys=True) + "\n")
        print(folder_id)
        return 0
    raise SystemExit(f"Unknown folders command: {args.folder_cmd}")


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
        timeout=int(config.get("command_timeout_seconds", 180)),
    )


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
        "install-service.sh": install_script(install_dir),
    }
    for name, content in files.items():
        target = install_dir / name
        target.write_text(content)
        if name.endswith(".sh"):
            target.chmod(0o755)
        print(target)
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="safe-sync")
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = p.add_subparsers(
        dest="cmd",
        required=True,
        metavar="{backup,start,stop,restart,status,logs,autostart,folders,computers,pull,list,doctor}",
    )

    init = sub.add_parser("init-config")
    init.add_argument("--force", action="store_true")
    init.add_argument("--machine")
    init.set_defaults(func=cmd_init_config)

    migrate = sub.add_parser("migrate-config")
    migrate.add_argument("--from-path", default=str(LEGACY_CONFIG))
    migrate.add_argument("--force", action="store_true")
    migrate.set_defaults(func=cmd_migrate_config)

    backup = sub.add_parser("backup")
    backup.add_argument("folder", nargs="?")
    backup.add_argument("--all", action="store_true")
    backup.add_argument("--dry-run", action="store_true")
    backup.set_defaults(func=cmd_backup)

    daemon = sub.add_parser("daemon", help=argparse.SUPPRESS)
    daemon.add_argument("--dry-run", action="store_true")
    daemon.add_argument("--once", action="store_true", help="Exit after the first backup attempt")
    daemon.add_argument("--poll-interval", type=int)
    daemon.add_argument("--debounce", type=int)
    daemon.add_argument("--max-loops", type=int, help=argparse.SUPPRESS)
    daemon.set_defaults(func=cmd_daemon)

    start = sub.add_parser("start")
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser("stop")
    stop.set_defaults(func=cmd_stop)

    restart = sub.add_parser("restart")
    restart.set_defaults(func=cmd_restart)

    autostart = sub.add_parser("autostart")
    autostart_sub = autostart.add_subparsers(dest="autostart_target", required=True)
    backend = autostart_sub.add_parser("backend")
    backend_sub = backend.add_subparsers(dest="autostart_action", required=True)
    for action in ("status", "enable", "disable"):
        backend_sub.add_parser(action).set_defaults(func=cmd_autostart)

    pull = sub.add_parser("pull")
    pull.add_argument("source", help="Full rclone source path, e.g. dropbox:computer-backups/test/linux/test_sync/data")
    pull.add_argument("destination")
    pull.add_argument("--dry-run", action="store_true")
    pull.set_defaults(func=cmd_pull)

    list_cmd = sub.add_parser("list")
    list_cmd.add_argument("target")
    list_cmd.add_argument("--depth", type=int, default=1)
    list_cmd.set_defaults(func=cmd_list)

    status = sub.add_parser("status")
    status.set_defaults(func=cmd_status)

    folders_cmd = sub.add_parser("folders")
    folders_sub = folders_cmd.add_subparsers(dest="folder_cmd", required=True)
    folders_list = folders_sub.add_parser("list")
    folders_list.set_defaults(func=cmd_folders)
    folders_add = folders_sub.add_parser("add")
    folders_add.add_argument("id")
    folders_add.add_argument("local_path")
    folders_add.add_argument("--label")
    folders_add.add_argument("--remote-path")
    folders_add.add_argument("--trash-path")
    folders_add.add_argument("--filter-file")
    folders_add.add_argument("--disabled", action="store_true")
    folders_add.set_defaults(func=cmd_folders)

    computers = sub.add_parser("computers")
    computers.set_defaults(func=cmd_computers)

    registry = sub.add_parser("registry", help=argparse.SUPPRESS)
    registry_sub = registry.add_subparsers(dest="registry_cmd", required=True)
    registry_update = registry_sub.add_parser("update")
    registry_update.set_defaults(func=cmd_registry)
    registry_path_cmd = registry_sub.add_parser("path")
    registry_path_cmd.set_defaults(func=cmd_registry)

    logs = sub.add_parser("logs")
    logs.add_argument("--lines", type=int, default=80)
    logs.set_defaults(func=cmd_logs)

    doctor = sub.add_parser("doctor")
    doctor.set_defaults(func=cmd_doctor)

    render = sub.add_parser("render-install", help=argparse.SUPPRESS)
    render.add_argument("--output-dir", required=True)
    render.add_argument("--program", default=str(PROJECT_ROOT / "bin" / "safe-sync"))
    render.set_defaults(func=cmd_render_install)

    sub._choices_actions = [
        action for action in sub._choices_actions
        if action.dest not in {"daemon", "render-install", "registry"}
    ]
    return p


def main() -> int:
    args = parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
