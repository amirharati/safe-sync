"""Safe Sync: small rclone guardrail CLI."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import platform
import re
import signal
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from safe_sync.api import DaemonApiServer, DaemonApiState, api_request
from safe_sync.daemon import DaemonState, WatchDaemon, WatchSettings, scan_tree
from safe_sync.event_journal import (
    EventJournal,
    JournalError,
    default_logging_config,
    journal_from_config,
    settings_from_config,
)
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

RATE_LIMIT_PATTERNS = (
    "too_many_requests",
    "too many requests",
    "too_many_write_operations",
    "too many write operations",
    "rate limit",
    "rate_limit",
    "retry-after",
)
TEMPORARY_REMOTE_PATTERNS = (
    "i/o timeout",
    "io timeout",
    "timeout awaiting response headers",
    "context deadline exceeded",
    "connection reset by peer",
    "temporary error",
    "temporarily unavailable",
    "tls handshake timeout",
    "server closed idle connection",
)
REMOTE_NOT_FOUND_PATTERNS = ("not found", "path/not_found", "object not found", "directory not found")
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
PROCESS_RUN_ID = f"run_{uuid.uuid4().hex}"
_EVENT_JOURNALS: dict[tuple[str, str, str, str], EventJournal] = {}
INTERNAL_FILTER_RULES = TEMPLATE_INTERNAL_FILTER.read_text()
MAX_RCLONE_DIAGNOSTIC_LINES = 2_000
DROPBOX_BATCH_SIZE = 32
DROPBOX_TRANSFERS = 8
DROPBOX_BATCH_TIMEOUT = "5s"
BACKUP_PATH_BATCH_SIZE = 250
BACKUP_PATH_BATCH_BYTES = 256 * 1024
ACTIVE_CHILD_GRACE_SECONDS = 15
_ACTIVE_CHILD: subprocess.Popen[str] | None = None
_ACTIVE_CHILD_METADATA: Path | None = None


class DaemonShutdown(BaseException):
    """Stop the daemon without converting shutdown into a backup failure."""


class RateLimitedError(RuntimeError):
    def __init__(self, message: str, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class TemporaryRemoteError(RuntimeError):
    """A remote failure that is safe to retry without user intervention."""


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


def active_child_path(config: dict[str, Any]) -> Path:
    cfg = normalized_config(config)
    identity = f"{safe_id(str(cfg['profile_id']))}-{safe_id(str(cfg['machine_id']))}.json"
    return state_root_path(cfg) / "active-children" / identity


def _write_active_child(config: dict[str, Any], process: subprocess.Popen[str], cmd: list[str]) -> Path:
    path = active_child_path(config)
    report_path = None
    if "--combined" in cmd:
        report_index = cmd.index("--combined") + 1
        if report_index < len(cmd):
            report_path = cmd[report_index]
    document = {
        "schema_version": 1,
        "pid": process.pid,
        "process_group_id": process.pid,
        "started_at": now_iso(),
        "operation_id": config.get("_operation_id"),
        "folder_id": config.get("folder_id"),
        "executable": str(cmd[0]),
        "argv": cmd,
        "report_path": report_path,
        "argv_sha256": hashlib.sha256("\0".join(cmd).encode()).hexdigest(),
    }
    atomic_write_text(path, json.dumps(document, indent=2, sort_keys=True) + "\n")
    return path


def _terminate_process_group(process: subprocess.Popen[str], grace_seconds: float = ACTIVE_CHILD_GRACE_SECONDS) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        process.terminate()
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        process.kill()
    process.wait(timeout=5)


def request_active_child_stop() -> None:
    process = _ACTIVE_CHILD
    if process is not None and process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            process.terminate()


def reconcile_orphan_child(config: dict[str, Any]) -> dict[str, Any] | None:
    """Stop only a previously recorded Safe Sync rclone process group."""
    path = active_child_path(config)
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text())
        pid = int(document["pid"])
        pgid = int(document.get("process_group_id", pid))
        executable = Path(str(document["executable"])).name
        expected_argv = [str(value) for value in document.get("argv") or []]
        report_path = str(document.get("report_path") or "")
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        path.unlink(missing_ok=True)
        return {"status": "invalid_metadata_removed"}

    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    command = result.stdout.strip()
    exact_child = (
        result.returncode == 0
        and executable == Path(rclone_bin(config)).name
        and bool(expected_argv)
        and Path(command.split(maxsplit=1)[0]).name == executable
        and all(argument in command for argument in expected_argv[1:])
        and (not report_path or report_path in command)
    )
    if exact_child:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + ACTIVE_CHILD_GRACE_SECONDS
        while time.monotonic() < deadline:
            probe = subprocess.run(
                ["ps", "-p", str(pid), "-o", "stat="],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            if probe.returncode != 0 or probe.stdout.strip().startswith("Z"):
                break
            time.sleep(0.1)
        else:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    path.unlink(missing_ok=True)
    return {
        "status": "orphan_stopped" if exact_child else "stale_metadata_removed",
        "operation_id": document.get("operation_id"),
        "folder_id": document.get("folder_id"),
        "report_path": report_path or None,
    }


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


def emergency_log_path(config: dict[str, Any]) -> Path:
    """Return the last-resort text log used only when the journal is unavailable."""
    log_dir = Path(config.get("log_dir", DEFAULT_LOG_DIR)).expanduser()
    return log_dir / f"safe-sync-emergency-{dt.date.today().isoformat()}.log"


def socket_path(config: dict[str, Any]) -> Path:
    return Path(config.get("socket_path", DEFAULT_SOCKET)).expanduser()


def append_emergency_log(config: dict[str, Any], line: str) -> None:
    """Fail open when the structured journal itself cannot accept an event."""
    path = emergency_log_path(config)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(line)
    except OSError as exc:
        print(f"warning: could not write log {path}: {exc}", file=sys.stderr)


def event_journal(config: dict[str, Any]) -> EventJournal:
    settings = settings_from_config(config)
    key = (
        str(Path(config.get("state_root", DEFAULT_STATE_DIR)).expanduser()),
        str(config.get("profile_id") or config.get("active_profile_id") or "system"),
        str(config.get("install_id") or "unconfigured"),
        repr(settings),
    )
    journal = _EVENT_JOURNALS.get(key)
    if journal is None:
        journal = journal_from_config(config)
        _EVENT_JOURNALS[key] = journal
    return journal


def record_event(
    config: dict[str, Any],
    event_type: str,
    *,
    component: str,
    channel: str = "audit",
    severity: str = "info",
    data: dict[str, Any] | None = None,
    correlation: dict[str, Any] | None = None,
    effect: str | None = None,
) -> dict[str, Any] | None:
    """Record observability without allowing journal failure to alter user data."""
    try:
        return event_journal(config).emit(
            event_type,
            component=component,
            channel=channel,
            severity=severity,
            data=data,
            correlation=correlation,
            run_id=PROCESS_RUN_ID,
            effect=effect,
        )
    except (JournalError, OSError, ValueError) as exc:
        append_emergency_log(config, f"[{now_iso()}] structured event journal degraded: {exc}\n")
        return None


def daemon_api(config: dict[str, Any], command: str, *, _timeout_seconds: float = 5.0, **payload: Any) -> dict[str, Any]:
    request = {"command": command, **payload}
    return api_request(socket_path(config), request, timeout_seconds=_timeout_seconds)


def text_looks_rate_limited(text: str) -> bool:
    lower = text.lower()
    return any(pattern in lower for pattern in RATE_LIMIT_PATTERNS)


def rate_limit_retry_after_seconds(text: str, default: int = 300) -> int:
    lower = text.lower()
    explicit = re.search(r"retry-after[:= ]+(\d+)", lower)
    if explicit:
        return max(1, int(explicit.group(1)))
    # Provider messages sometimes express Retry-After in prose. Ignore short
    # values here: those are normally rclone's own low-level retry interval,
    # not a provider-wide cooldown.
    prose = re.search(r"(?:trying|try) again in (\d+) seconds", lower)
    if prose and int(prose.group(1)) >= 30:
        return int(prose.group(1))
    return default


def text_looks_temporary_remote_failure(text: str, exit_code: int | None = None) -> bool:
    if exit_code == 5:
        return True
    lower = text.lower()
    return any(pattern in lower for pattern in TEMPORARY_REMOTE_PATTERNS)


def text_looks_remote_not_found(text: str) -> bool:
    lower = text.lower()
    return any(pattern in lower for pattern in REMOTE_NOT_FOUND_PATTERNS)


def temporary_retry_seconds(attempt: int) -> int:
    """Return deterministic bounded backoff for an app-level retry."""
    return min(900, 30 * (2 ** max(0, min(attempt - 1, 5))))


def future_iso(seconds: int) -> str:
    return (dt.datetime.now(dt.timezone.utc).astimezone() + dt.timedelta(seconds=seconds)).isoformat(timespec="seconds")


def run_command(
    config: dict[str, Any],
    cmd: list[str],
    dry_run: bool = False,
    progress_callback: Callable[[str], None] | None = None,
) -> int:
    global LAST_COMMAND_OUTPUT, _ACTIVE_CHILD, _ACTIVE_CHILD_METADATA
    LAST_COMMAND_OUTPUT = ""
    env = rclone_env(config)
    operation_id = str(config.get("_operation_id") or "") or None
    correlation = {
        "operation_id": operation_id,
        "folder_id": config.get("folder_id"),
        "job_id": config.get("_job_id"),
    }
    correlation = {key: value for key, value in correlation.items() if value}
    record_event(
        config,
        "command.started",
        component="rclone",
        channel="diagnostic",
        severity="debug",
        data={"argv": cmd, "dry_run": dry_run},
        correlation=correlation,
        effect="none" if dry_run else None,
    )
    if progress_callback is None:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        output = result.stdout or ""
        retained: list[str] = []
        suppressed_line_count = 0
        for output_line_number, output_line in enumerate(output.splitlines(keepends=True), start=1):
            cleaned = output_line.rstrip("\n")
            if output_line_number <= MAX_RCLONE_DIAGNOSTIC_LINES or rclone_line_must_retain(cleaned):
                retained.append(output_line)
                print(output_line, end="")
                record_event(
                    config,
                    "rclone.output",
                    component="rclone",
                    channel="diagnostic",
                    severity="debug",
                    data={"line": cleaned},
                    correlation=correlation,
                    effect="none" if dry_run else None,
                )
            else:
                suppressed_line_count += 1
        LAST_COMMAND_OUTPUT = "".join(retained)
        if suppressed_line_count:
            record_event(
                config,
                "rclone.output_suppressed",
                component="rclone",
                channel="diagnostic",
                severity="info",
                data={
                    "captured_lines": len(retained),
                    "suppressed_lines": suppressed_line_count,
                    "limit": MAX_RCLONE_DIAGNOSTIC_LINES,
                },
                correlation=correlation,
                effect="none" if dry_run else None,
            )
            print(
                f"Safe Sync suppressed {suppressed_line_count:,} repetitive rclone diagnostic lines "
                f"after retaining {len(retained):,}.\n"
            )
        record_event(
            config,
            "command.completed",
            component="rclone",
            channel="diagnostic",
            severity="info" if result.returncode == 0 else "error",
            data={"exit_code": result.returncode, "dry_run": dry_run},
            correlation=correlation,
            effect="none" if dry_run else None,
        )
        return int(result.returncode)
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
        start_new_session=True,
    )
    _ACTIVE_CHILD = process
    try:
        _ACTIVE_CHILD_METADATA = _write_active_child(config, process, cmd)
    except BaseException:
        _terminate_process_group(process)
        _ACTIVE_CHILD = None
        raise
    lines: list[str] = []
    output_line_count = 0
    suppressed_line_count = 0
    assert process.stdout is not None
    try:
        for line in process.stdout:
            cleaned = line.rstrip("\n")
            progress_callback(cleaned)
            output_line_count += 1
            must_retain = rclone_line_must_retain(cleaned)
            if output_line_count <= MAX_RCLONE_DIAGNOSTIC_LINES or must_retain:
                lines.append(line)
                print(line, end="")
                record_event(
                    config,
                    "rclone.output",
                    component="rclone",
                    channel="diagnostic",
                    severity="debug",
                    data={"line": cleaned},
                    correlation=correlation,
                    effect="none" if dry_run else None,
                )
            else:
                suppressed_line_count += 1
        returncode = process.wait()
        LAST_COMMAND_OUTPUT = "".join(lines)
        if suppressed_line_count:
            record_event(
                config,
                "rclone.output_suppressed",
                component="rclone",
                channel="diagnostic",
                severity="info",
                data={
                    "captured_lines": output_line_count - suppressed_line_count,
                    "suppressed_lines": suppressed_line_count,
                    "limit": MAX_RCLONE_DIAGNOSTIC_LINES,
                },
                correlation=correlation,
                effect="none" if dry_run else None,
            )
            print(
                f"Safe Sync suppressed {suppressed_line_count:,} repetitive rclone diagnostic lines "
                f"after retaining {output_line_count - suppressed_line_count:,}.\n"
            )
        record_event(
            config,
            "command.completed",
            component="rclone",
            channel="diagnostic",
            severity="info" if returncode == 0 else "error",
            data={"exit_code": returncode, "dry_run": dry_run},
            correlation=correlation,
            effect="none" if dry_run else None,
        )
        return int(returncode)
    finally:
        if process.poll() is None:
            _terminate_process_group(process)
        if _ACTIVE_CHILD is process:
            _ACTIVE_CHILD = None
        metadata_path = _ACTIVE_CHILD_METADATA
        if metadata_path is not None:
            try:
                current = json.loads(metadata_path.read_text())
            except (OSError, json.JSONDecodeError):
                current = {}
            if int(current.get("pid", -1)) == process.pid:
                metadata_path.unlink(missing_ok=True)
            _ACTIVE_CHILD_METADATA = None


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


def rclone_log_level(config: dict[str, Any]) -> str:
    level = settings_from_config(config).level
    # Safe Sync Debug records its own detailed lifecycle and audit events. Raw
    # rclone DEBUG output is intentionally reserved for the short-lived Trace
    # mode because a small-file tree can otherwise fill the bounded journal.
    return {"quiet": "ERROR", "normal": "INFO", "debug": "INFO", "trace": "DEBUG"}[level]


def rclone_line_must_retain(line: str) -> bool:
    """Retain errors and aggregate progress after the raw diagnostic cap."""
    lowered = line.lower()
    return (
        "error :" in lowered
        or "warning :" in lowered
        or "notice :" in lowered
        or text_looks_rate_limited(lowered)
        or text_looks_temporary_remote_failure(lowered)
        or text_looks_auth_failure(lowered)
        or "transferred:" in lowered
        or "checks:" in lowered
        or "elapsed time:" in lowered
        or "running all checks before starting transfers" in lowered
        or "checks finished, now starting transfers" in lowered
    )


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
    raw_logging = normalized.get("logging") if isinstance(normalized.get("logging"), dict) else {}
    normalized["logging"] = {**default_logging_config(), **raw_logging}
    # Validate eagerly so invalid bounds/levels cannot silently change runtime
    # observability behavior.
    settings_from_config(normalized)

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
        "logging": normalized["logging"],
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
    record_event(
        normalized,
        "configuration.changed",
        component="configuration",
        data={
            "active_profile_id": normalized["active_profile_id"],
            "profile_count": len(normalized["profiles"]),
            "folder_count": len(normalized["folders"]),
        },
    )
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
    cmd = [rclone_bin(config), "about", remote, "--timeout", "20s", "--contimeout", "10s", "--retries", "1", "--low-level-retries", "10"]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=45, env=rclone_env(config))
    if result.returncode != 0:
        output = result.stdout or ""
        record_event(
            config,
            "remote.preflight_failed",
            component="remote",
            severity="error",
            data={"exit_code": result.returncode, "error": output},
        )
        if text_looks_rate_limited(output):
            retry_after = rate_limit_retry_after_seconds(output, int(config.get("rate_limit_backoff_seconds", 300)))
            raise RateLimitedError(f"Dropbox rate limited Safe Sync; cooling down for {retry_after}s", retry_after)
        if text_looks_auth_failure(output):
            raise SystemExit(reconnect_dropbox_message())
        if text_looks_temporary_remote_failure(output, result.returncode):
            raise TemporaryRemoteError("Temporary Dropbox connection failure during preflight")
        raise SystemExit("Remote preflight failed; inspect safe-sync logs")


def text_looks_auth_failure(output: str) -> bool:
    lowered = output.lower()
    return any(pattern in lowered for pattern in AUTH_FAILURE_PATTERNS)


def reconnect_dropbox_message() -> str:
    return "Dropbox authorization is invalid or revoked. Reconnect with: safe-sync connect-dropbox"


def dropbox_transfer_count(config: dict[str, Any]) -> int:
    attempt = max(1, int(config.get("_backup_attempt", 1)))
    return max(4, DROPBOX_TRANSFERS // (2 ** min(attempt - 1, 1)))


def backup_cmd(config: dict[str, Any], dry_run: bool, report_path: Path | None = None) -> list[str]:
    remote = config["remote_root"].rstrip("/")
    local = str(Path(config["local_path"]).expanduser())
    trash = f"{config['trash_root'].rstrip('/')}/{stamp()}"
    cmd = [
        rclone_bin(config), "sync", local, remote,
        *filter_args(config),
        "--backup-dir", trash,
        "--create-empty-src-dirs",
        # Finish the comparison before uploading so rclone's transfer totals
        # are stable. This lets the UI show an honest completion percentage
        # instead of a denominator that grows while the tree is discovered.
        "--check-first",
        # Dropbox commits uploads in synchronous batches with full integrity
        # checking. A larger batch and transfer pool amortize API round trips
        # for the worst-case many-small-file workload without changing the
        # direct-mirror format or using the unsafe async batch mode. Start
        # conservatively for Dropbox's write-operation limit and reduce the
        # pool again after a failed attempt.
        "--dropbox-batch-mode", "sync",
        "--dropbox-batch-size", str(DROPBOX_BATCH_SIZE),
        "--dropbox-batch-timeout", DROPBOX_BATCH_TIMEOUT,
        "--transfers", str(dropbox_transfer_count(config)),
        "--stats", "10s",
        "--timeout", "30s", "--contimeout", "10s",
        # Keep high-level retries at one because rclone's combined report is
        # per attempt. Low-level request retries are safe and prevent a single
        # transient API timeout from aborting a large tree traversal.
        "--retries", "1", "--low-level-retries", "10", "--retries-sleep", "5s",
        "--log-level", rclone_log_level(config),
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
        "--retries", "1", "--low-level-retries", "10", "--retries-sleep", "5s",
        "--log-level", rclone_log_level(config),
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
        "logging": default_logging_config(),
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
        "logging": normalized["logging"],
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


def new_operation_id(prefix: str) -> str:
    return f"{safe_id(prefix)}_{uuid.uuid4().hex}"


def backup_report_details(report_path: Path) -> tuple[list[dict[str, str]], dict[str, int], str, int]:
    raw = report_path.read_bytes() if report_path.exists() else b""
    changes = parse_combined_report(raw.decode(errors="replace"))
    counts = {"added": 0, "modified": 0, "removed": 0, "error": 0}
    for change in changes:
        operation = str(change["operation"])
        counts[operation] = counts.get(operation, 0) + 1
    return changes, counts, hashlib.sha256(raw).hexdigest(), len(raw)


def record_backup_report(
    config: dict[str, Any],
    report_path: Path,
    operation_id: str,
    *,
    dry_run: bool,
    details: tuple[list[dict[str, str]], dict[str, int], str, int] | None = None,
) -> dict[str, int]:
    changes, counts, report_hash, report_bytes = details or backup_report_details(report_path)
    correlation = {"operation_id": operation_id, "folder_id": config.get("folder_id")}
    record_event(
        config,
        "backup.report_committed",
        component="backup",
        data={
            "report_sha256": report_hash,
            "report_bytes": report_bytes,
            "counts": counts,
            "change_count": len(changes),
        },
        correlation=correlation,
        effect="none" if dry_run else None,
    )
    batches: list[list[dict[str, str]]] = []
    batch: list[dict[str, str]] = []
    batch_bytes = 0
    for change in changes:
        change_bytes = len(json.dumps([change["operation"], change["path"]], separators=(",", ":")).encode())
        if batch and (len(batch) >= BACKUP_PATH_BATCH_SIZE or batch_bytes + change_bytes > BACKUP_PATH_BATCH_BYTES):
            batches.append(batch)
            batch = []
            batch_bytes = 0
        batch.append(change)
        batch_bytes += change_bytes
    if batch:
        batches.append(batch)
    for batch_index, batch in enumerate(batches, start=1):
        record_event(
            config,
            "backup.path_batch",
            component="backup",
            severity="error" if any(change["operation"] == "error" for change in batch) else "info",
            data={
                "batch_index": batch_index,
                "batch_size": len(batch),
                "changes": [[change["operation"], change["path"]] for change in batch],
            },
            correlation=correlation,
            effect="none" if dry_run else None,
        )
    return counts


def run_backup_with_config(config: dict[str, Any], dry_run: bool) -> int:
    with Lock(lock_file(config)):
        operation_id = new_operation_id("backup")
        correlation = {"operation_id": operation_id, "folder_id": config.get("folder_id")}
        existing_backoff_until = active_backoff_until(config)
        if existing_backoff_until:
            record_event(
                config,
                "backup.delayed",
                component="backup",
                data={"reason": "rate_limit_backoff", "until": existing_backoff_until},
                correlation=correlation,
                effect="none" if dry_run else None,
            )
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
        operation_config = {**config, "_operation_id": operation_id}
        record_event(
            config,
            "backup.started",
            component="backup",
            data={"trigger": "direct", "dry_run": dry_run},
            correlation=correlation,
            effect="none" if dry_run else None,
        )
        try:
            preflight(config)
            code = run_command(operation_config, backup_cmd(config, dry_run, report_path), dry_run=dry_run)
        except RateLimitedError as exc:
            record_event(
                config,
                "backup.failed",
                component="backup",
                severity="warning",
                data={"reason": "rate_limited", "retry_after_seconds": exc.retry_after_seconds},
                correlation=correlation,
                effect="none" if dry_run else None,
            )
            save_rate_limit_status(config, str(exc), exc.retry_after_seconds, queued=True)
            print(str(exc))
            return RATE_LIMIT_EXIT
        except BaseException as exc:
            record_event(
                config,
                "backup.failed",
                component="backup",
                severity="error",
                data={"reason": type(exc).__name__, "error": str(exc)},
                correlation=correlation,
                effect="none" if dry_run else None,
            )
            save_status(config, state="error", folder_id=config.get("folder_id"), last_error=str(exc), last_finish=now_iso())
            raise
        counts = record_backup_report(config, report_path, operation_id, dry_run=dry_run)
        if code == 0:
            if text_looks_rate_limited(LAST_COMMAND_OUTPUT):
                retry_after = rate_limit_retry_after_seconds(LAST_COMMAND_OUTPUT, int(config.get("rate_limit_backoff_seconds", 300)))
                save_rate_limit_status(
                    config,
                    f"Dropbox reported throttling; cooling down for {retry_after}s",
                    retry_after,
                    queued=False,
                )
                record_event(
                    config,
                    "backup.failed",
                    component="backup",
                    severity="warning",
                    data={"reason": "rate_limited", "retry_after_seconds": retry_after, "counts": counts},
                    correlation=correlation,
                    effect="none" if dry_run else None,
                )
                return RATE_LIMIT_EXIT
            else:
                if not dry_run:
                    publication_code = publish_generation(config, report_path, operation_id=operation_id)
                    if publication_code != 0:
                        save_status(config, state="error", folder_id=config.get("folder_id"), last_error="generation publication failed", last_finish=now_iso())
                        return publication_code
                record_event(
                    config,
                    "backup.completed",
                    component="backup",
                    data={"exit_code": code, "counts": counts, "dry_run": dry_run},
                    correlation=correlation,
                    effect="none" if dry_run else None,
                )
                save_status(config, state="idle", folder_id=config.get("folder_id"), last_success=now_iso(), last_finish=now_iso(), last_error=None, last_warning=None)
        else:
            if text_looks_rate_limited(LAST_COMMAND_OUTPUT):
                retry_after = rate_limit_retry_after_seconds(LAST_COMMAND_OUTPUT, int(config.get("rate_limit_backoff_seconds", 300)))
                record_event(
                    config,
                    "backup.failed",
                    component="backup",
                    severity="warning",
                    data={"reason": "rate_limited", "exit_code": code, "retry_after_seconds": retry_after, "counts": counts},
                    correlation=correlation,
                    effect="none" if dry_run else None,
                )
                save_rate_limit_status(config, f"Dropbox rate limited Safe Sync; cooling down for {retry_after}s", retry_after, queued=True)
            else:
                record_event(
                    config,
                    "backup.failed",
                    component="backup",
                    severity="error",
                    data={"reason": "rclone_exit", "exit_code": code, "counts": counts},
                    correlation=correlation,
                    effect="none" if dry_run else None,
                )
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
            replicate_event_journal(config)
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
    elif last_warning and sync_status not in {"syncing", "transferring", "publishing"}:
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

    # Configuration truth must not depend on whichever folder the daemon most
    # recently processed. Keep the complete enabled-folder list available to
    # CLI and UI status consumers even while the service is stopped or stale.
    configured_folders = [
        {
            "id": folder["id"],
            "label": folder.get("label", folder["id"]),
            "local_path": str(Path(folder["local_path"]).expanduser()),
        }
        for folder in enabled_folders(config)
    ]
    sync_state["folders"] = configured_folders
    sync_state["configured_folder_count"] = len(configured_folders)

    service_text = service_status_text()
    service_state = service_text.split(":", 1)[1].strip() if ":" in service_text else service_text
    health = status_health(config, service_state, sync_state)
    try:
        audit_journal = event_journal(config)
        audit_status = audit_journal.status()
        audit_path = str(audit_journal.root)
    except (JournalError, OSError, ValueError) as exc:
        audit_status = {"health": "degraded", "error": str(exc), "gaps": [], "pending_cloud_segments": None}
        audit_path = str(state_root_path(config) / "event-journal" / safe_id(str(config["profile_id"])))
    if health["health"] == "ok" and audit_status.get("health") == "degraded":
        health = {**health, "health": "warning", "reason": "structured audit logging is degraded"}
    return {
        "daemon_seen_at": health["daemon_seen_at"],
        "health": health["health"],
        "health_reason": health["reason"],
        "log": audit_path,
        "emergency_log": str(emergency_log_path(config)),
        "audit": audit_status,
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
    if "Transferred:" in cleaned or "Checks:" in cleaned or "Elapsed time:" in cleaned:
        return cleaned
    if ": Copied" in cleaned or ": Deleted" in cleaned or ": Updated" in cleaned:
        _, detail = cleaned.split("INFO  :", 1) if "INFO  :" in cleaned else ("", cleaned)
        return detail.strip()
    return None


def parse_backup_progress_line(line: str) -> dict[str, Any]:
    """Translate rclone stats into stable, UI-safe backup progress fields."""
    cleaned = line.strip()
    updates: dict[str, Any] = {}

    # With --check-first rclone still prints provisional transfer counters
    # during traversal. Only this explicit message marks the point at which
    # their denominator is complete and safe to present as a percentage.
    if "Checks finished, now starting transfers" in cleaned:
        return {
            "sync_phase": "transferring",
            "progress_percent": None,
            "transferred_files": None,
            "total_transfer_files": None,
            "transferred_bytes_display": None,
            "total_bytes_display": None,
            "eta": None,
        }
    if "Running all checks before starting transfers" in cleaned:
        return {"sync_phase": "scanning"}

    checks = re.search(r"Checks:\s+([\d,]+)\s*/\s*([\d,]+),\s*(\d+)%", cleaned)
    if checks:
        updates.update({
            "checks_completed": int(checks.group(1).replace(",", "")),
            "checks_total": int(checks.group(2).replace(",", "")),
        })
        listed = re.search(r"Listed\s+([\d,]+)", cleaned)
        if listed:
            updates["listed_entries"] = int(listed.group(1).replace(",", ""))
        return updates

    transferred = re.search(r"Transferred:\s+(.+?)\s*/\s*(.+?),\s*(\d+)%", cleaned)
    if transferred:
        completed = transferred.group(1).strip()
        total = transferred.group(2).strip()
        percent = int(transferred.group(3))
        if completed.replace(",", "").isdigit() and total.replace(",", "").isdigit():
            total_files = int(total.replace(",", ""))
            updates.update({
                "transferred_files": int(completed.replace(",", "")),
                "total_transfer_files": total_files,
            })
            if total_files > 0:
                updates["progress_percent"] = percent
        else:
            updates.update({
                "transferred_bytes_display": completed,
                "total_bytes_display": total,
                "transfer_bytes_percent": percent,
            })
            speed = re.search(r",\s*([^,]+/s)(?:,|$)", cleaned)
            eta = re.search(r"ETA\s+(.+)$", cleaned)
            if speed:
                updates["transfer_speed"] = speed.group(1).strip()
            if eta:
                updates["eta"] = eta.group(1).strip()
        return updates

    elapsed = re.search(r"Elapsed time:\s+(.+)$", cleaned)
    if elapsed:
        updates["elapsed"] = elapsed.group(1).strip()
    return updates


def rclone_size_bytes(display: str) -> float | None:
    match = re.fullmatch(r"([\d.]+)\s*([KMGTPE]?i?B)", display.strip(), re.IGNORECASE)
    if not match:
        return None
    units = {
        "b": 1,
        "kb": 1_000,
        "mb": 1_000**2,
        "gb": 1_000**3,
        "tb": 1_000**4,
        "pb": 1_000**5,
        "eb": 1_000**6,
        "kib": 1_024,
        "mib": 1_024**2,
        "gib": 1_024**3,
        "tib": 1_024**4,
        "pib": 1_024**5,
        "eib": 1_024**6,
    }
    return float(match.group(1)) * units[match.group(2).lower()]


class BackupProgressTracker:
    """Freeze --check-first totals and retain failed-file visibility."""

    _TRANSFER_KEYS = {
        "progress_percent",
        "transferred_files",
        "total_transfer_files",
        "transferred_bytes_display",
        "total_bytes_display",
        "transfer_bytes_percent",
        "transfer_speed",
        "eta",
    }

    def __init__(self) -> None:
        self.phase = "scanning"
        self.planned_files: int | None = None
        self.planned_bytes_display: str | None = None
        self.planned_bytes: float | None = None
        self.failed_paths: set[str] = set()

    def update(self, line: str) -> dict[str, Any]:
        updates = parse_backup_progress_line(line)
        reported_phase = updates.get("sync_phase")

        reported_files = updates.get("total_transfer_files")
        if self.phase == "scanning" and isinstance(reported_files, int):
            self.planned_files = max(self.planned_files or 0, reported_files)

        reported_bytes_display = updates.get("total_bytes_display")
        if self.phase == "scanning" and isinstance(reported_bytes_display, str):
            reported_bytes = rclone_size_bytes(reported_bytes_display)
            if reported_bytes is not None and (self.planned_bytes is None or reported_bytes >= self.planned_bytes):
                self.planned_bytes = reported_bytes
                self.planned_bytes_display = reported_bytes_display

        failed = re.search(r"ERROR\s+:\s+(.+?):\s+Failed to copy", line, re.IGNORECASE)
        if failed:
            self.failed_paths.add(failed.group(1).strip())

        if reported_phase == "transferring":
            self.phase = "transferring"
        elif reported_phase == "scanning":
            self.phase = "scanning"

        if self.phase == "scanning":
            for key in self._TRANSFER_KEYS:
                updates.pop(key, None)
        else:
            completed_files = updates.get("transferred_files")
            if self.planned_files is None and isinstance(reported_files, int):
                self.planned_files = reported_files
            if self.planned_files is not None:
                updates["total_transfer_files"] = self.planned_files
                updates["planned_transfer_files"] = self.planned_files
                if isinstance(completed_files, int) and self.planned_files > 0:
                    updates["progress_percent"] = min(100, round(completed_files * 100 / self.planned_files))

            completed_bytes_display = updates.get("transferred_bytes_display")
            if self.planned_bytes is None and isinstance(reported_bytes_display, str):
                self.planned_bytes = rclone_size_bytes(reported_bytes_display)
                self.planned_bytes_display = reported_bytes_display
            if self.planned_bytes_display is not None:
                updates["total_bytes_display"] = self.planned_bytes_display
                completed_bytes = rclone_size_bytes(completed_bytes_display) if isinstance(completed_bytes_display, str) else None
                if completed_bytes is not None and self.planned_bytes:
                    updates["transfer_bytes_percent"] = min(100, round(completed_bytes * 100 / self.planned_bytes))

        if failed or reported_phase == "transferring":
            updates["failed_transfer_files"] = len(self.failed_paths)
        return updates


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


def backup_queue_path(config: dict[str, Any]) -> Path:
    cfg = normalized_config(config)
    name = f"{safe_id(str(cfg['profile_id']))}-{safe_id(str(cfg['machine_id']))}.json"
    return state_root_path(cfg) / "backup-queue" / name


def _new_backup_queue(config: dict[str, Any], folders: list[dict[str, Any]]) -> dict[str, Any]:
    cfg = normalized_config(config)
    created = now_iso()
    return {
        "schema_version": 2,
        "cycle_id": new_operation_id("cycle"),
        "profile_id": cfg["profile_id"],
        "machine_id": cfg["machine_id"],
        "created_at": created,
        "updated_at": created,
        "items": [
            {"folder_id": folder["id"], "stage": "payload", "attempts": 0, "changes": [], "attempt": None}
            for folder in folders
        ],
    }


def load_backup_queue(config: dict[str, Any], folders: list[dict[str, Any]]) -> tuple[dict[str, Any], bool]:
    path = backup_queue_path(config)
    if not path.exists():
        return _new_backup_queue(config, folders), False
    try:
        queue = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise TransferError(f"backup retry state is invalid: {path}: {exc}") from exc
    cfg = normalized_config(config)
    if queue.get("profile_id") != cfg["profile_id"] or queue.get("machine_id") != cfg["machine_id"]:
        raise TransferError(f"backup retry state belongs to another profile: {path}")
    enabled = {str(folder["id"]) for folder in folders}
    items = [item for item in queue.get("items") or [] if str(item.get("folder_id")) in enabled]
    for item in items:
        item.setdefault("stage", "payload")
        item.setdefault("attempts", 0)
        item.setdefault("changes", [])
        item.setdefault("attempt", None)
        folder_cfg = folder_config(config, next(folder for folder in folders if folder["id"] == item["folder_id"]))
        if pending_generation_path(folder_cfg).exists():
            item["stage"] = "generation"
    queue["items"] = items
    queue["schema_version"] = 2
    return queue, True


def save_backup_queue(config: dict[str, Any], queue: dict[str, Any]) -> None:
    path = backup_queue_path(config)
    queue["updated_at"] = now_iso()
    if queue.get("items"):
        atomic_write_text(path, json.dumps(queue, indent=2, sort_keys=True) + "\n")
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def merge_backup_changes(
    existing: list[dict[str, str]],
    incoming: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Accumulate successful path results across failed rclone attempts."""
    merged: dict[str, dict[str, str]] = {
        str(change["path"]): {"path": str(change["path"]), "operation": str(change["operation"])}
        for change in existing
        if change.get("path") and change.get("operation") != "error"
    }
    for change in incoming:
        path = str(change.get("path") or "")
        operation = str(change.get("operation") or "")
        if not path or operation == "error":
            continue
        previous = (merged.get(path) or {}).get("operation")
        if previous == "added" and operation == "modified":
            operation = "added"
        elif previous == "added" and operation == "removed":
            merged.pop(path, None)
            continue
        elif previous == "removed" and operation in {"added", "modified"}:
            operation = "modified"
        merged[path] = {"path": path, "operation": operation}
    return [merged[path] for path in sorted(merged)]


def reconcile_backup_queue_reports(config: dict[str, Any], queue: dict[str, Any]) -> list[dict[str, Any]]:
    """Recover report facts saved before an interrupted attempt could commit them."""
    recovered: list[dict[str, Any]] = []
    for item in queue.get("items") or []:
        attempt = item.get("attempt")
        if not isinstance(attempt, dict) or attempt.get("status") != "running":
            continue
        report_text = str(attempt.get("report_path") or "")
        if not report_text:
            continue
        report_path = Path(report_text)
        if not report_path.exists():
            attempt["status"] = "interrupted_without_report"
            recovered.append({"folder_id": item.get("folder_id"), "status": attempt["status"]})
            continue
        changes, counts, report_hash, report_bytes = backup_report_details(report_path)
        item["changes"] = merge_backup_changes(list(item.get("changes") or []), changes)
        attempt.update(
            {
                "status": "report_recovered",
                "recovered_at": now_iso(),
                "report_sha256": report_hash,
                "report_bytes": report_bytes,
                "counts": counts,
            }
        )
        recovered.append(
            {
                "folder_id": item.get("folder_id"),
                "status": attempt["status"],
                "operation_id": attempt.get("operation_id"),
                "counts": counts,
            }
        )
    if recovered:
        save_backup_queue(config, queue)
    return recovered


def run_all_backups_runtime(config: dict[str, Any], dry_run: bool, api_state: DaemonApiState) -> tuple[int, str | None]:
    folders = enabled_folders(config)
    total_folders = len(folders)
    folder_by_id = {str(folder["id"]): folder for folder in folders}
    queue, resumed = load_backup_queue(config, folders) if not dry_run else (_new_backup_queue(config, folders), False)
    if not dry_run:
        save_backup_queue(config, queue)
        recovered_reports = reconcile_backup_queue_reports(config, queue)
        for recovered in recovered_reports:
            record_event(
                config,
                "backup.report_recovered",
                component="backup",
                severity="warning",
                data=recovered,
                correlation={
                    "operation_id": recovered.get("operation_id"),
                    "folder_id": recovered.get("folder_id"),
                    "cycle_id": queue["cycle_id"],
                },
            )
    retry_delay = 0
    retry_reason: str | None = None
    provider_limited = False
    completed_this_run: list[str] = []

    for item in list(queue["items"]):
        folder_id = str(item["folder_id"])
        folder = folder_by_id.get(folder_id)
        if folder is None:
            queue["items"].remove(item)
            if not dry_run:
                save_backup_queue(config, queue)
            continue
        index = next(position for position, candidate in enumerate(folders, start=1) if candidate["id"] == folder_id)
        folder_cfg = folder_config(config, folder)
        previous_attempt = item.get("attempt") if isinstance(item.get("attempt"), dict) else {}
        operation_id = (
            str(previous_attempt.get("operation_id"))
            if item["stage"] == "generation" and previous_attempt.get("operation_id")
            else new_operation_id("backup")
        )
        operation_cfg = {
            **folder_cfg,
            "_operation_id": operation_id,
            "_backup_attempt": int(item.get("attempts", 0)) + 1,
        }
        correlation = {"operation_id": operation_id, "folder_id": folder_id, "cycle_id": queue["cycle_id"]}
        report_path = backup_report_path(folder_cfg)
        pending_ids = [str(pending["folder_id"]) for pending in queue["items"]]
        publish_runtime_status(
            api_state,
            config,
            state="publishing" if item["stage"] == "generation" else "syncing",
            folder_id=folder_id,
            current_folder_index=index,
            current_folder_total=total_folders,
            current_folder_label=folder.get("label", folder["id"]),
            local_path=str(Path(folder["local_path"]).expanduser()),
            last_start=now_iso(),
            last_command="daemon",
            last_error=None,
            last_warning=None,
            queued_backup=False,
            configured_folder_count=total_folders,
            pending_folders=pending_ids,
            completed_folders=completed_this_run,
            retry_cycle=bool(resumed),
            sync_phase="finalizing" if item["stage"] == "generation" else "scanning",
            progress_percent=None,
            transferred_files=None,
            total_transfer_files=None,
            planned_transfer_files=None,
            failed_transfer_files=0,
            transferred_bytes_display=None,
            total_bytes_display=None,
            transfer_bytes_percent=None,
            transfer_speed=None,
            eta=None,
            checks_completed=None,
            checks_total=None,
            listed_entries=None,
            elapsed=None,
            current_file=None,
            last_progress="Resuming change publication" if item["stage"] == "generation" else "Scanning and comparing",
        )

        if item["stage"] == "generation":
            publication_code = publish_generation(
                folder_cfg,
                operation_id=operation_id,
                changes=None if pending_generation_path(folder_cfg).exists() else list(item.get("changes") or []),
            )
            if publication_code == 0:
                record_event(
                    folder_cfg,
                    "backup.completed",
                    component="backup",
                    data={"exit_code": 0, "resumed_stage": "generation", "dry_run": False},
                    correlation=correlation,
                )
                queue["items"].remove(item)
                completed_this_run.append(folder_id)
                save_backup_queue(config, queue)
                replicate_event_journal(config)
                continue
            item["attempts"] = int(item.get("attempts", 0)) + 1
            retry_delay = max(retry_delay, temporary_retry_seconds(int(item["attempts"])))
            retry_reason = f"Change publication for {folder_id} is pending after a temporary remote failure"
            save_backup_queue(config, queue)
            record_event(
                folder_cfg,
                "backup.retry_scheduled",
                component="backup",
                severity="warning",
                data={"stage": "generation", "attempt": item["attempts"], "retry_after_seconds": retry_delay},
                correlation=correlation,
            )
            continue

        record_event(
            folder_cfg,
            "backup.started",
            component="backup",
            data={
                "trigger": "daemon",
                "dry_run": dry_run,
                "folder_index": index,
                "folder_total": total_folders,
                "retry_cycle": bool(resumed),
                "attempt": int(item.get("attempts", 0)) + 1,
            },
            correlation=correlation,
            effect="none" if dry_run else None,
        )

        progress_tracker = BackupProgressTracker()

        def on_progress(line: str) -> None:
            summary = summarize_progress_line(line)
            structured = progress_tracker.update(line)
            if summary or structured:
                current_file = current_file_from_progress(summary)
                publish_runtime_status(
                    api_state,
                    config,
                    state="syncing",
                    folder_id=folder_id,
                    current_folder_index=index,
                    current_folder_total=total_folders,
                    current_folder_label=folder.get("label", folder["id"]),
                    local_path=str(Path(folder["local_path"]).expanduser()),
                    **structured,
                    last_progress=summary if summary else api_state.snapshot().get("last_progress"),
                    current_file=current_file if current_file else api_state.snapshot().get("current_file"),
                    _activity_event=summary if current_file else None,
                )
        try:
            preflight(folder_cfg)
            if not dry_run:
                item["attempt"] = {
                    "operation_id": operation_id,
                    "report_path": str(report_path),
                    "started_at": now_iso(),
                    "status": "running",
                }
                save_backup_queue(config, queue)
            code = run_command(operation_cfg, backup_cmd(operation_cfg, dry_run, report_path), dry_run=dry_run, progress_callback=on_progress)
        except RateLimitedError as exc:
            item["attempts"] = int(item.get("attempts", 0)) + 1
            retry_delay = max(retry_delay, exc.retry_after_seconds)
            retry_reason = str(exc)
            provider_limited = True
            if not dry_run:
                save_backup_queue(config, queue)
            record_event(
                folder_cfg,
                "backup.failed",
                component="backup",
                severity="warning",
                data={"reason": "rate_limited", "retry_after_seconds": exc.retry_after_seconds},
                correlation=correlation,
                effect="none" if dry_run else None,
            )
            publish_runtime_status(
                api_state,
                config,
                state="backoff",
                failed_folder=folder_id,
                last_warning=str(exc),
                last_error=None,
                queued_backup=True,
                backoff_seconds=exc.retry_after_seconds,
                backoff_until=future_iso(exc.retry_after_seconds),
                last_finish=now_iso(),
                last_progress=f"Paused before folder {index} of {total_folders}",
            )
            break
        except TemporaryRemoteError as exc:
            item["attempts"] = int(item.get("attempts", 0)) + 1
            retry_delay = max(retry_delay, temporary_retry_seconds(int(item["attempts"])))
            retry_reason = str(exc)
            if not dry_run:
                save_backup_queue(config, queue)
            record_event(
                folder_cfg,
                "backup.retry_scheduled",
                component="backup",
                severity="warning",
                data={"stage": "preflight", "attempt": item["attempts"], "retry_after_seconds": retry_delay},
                correlation=correlation,
            )
            continue
        except BaseException as exc:
            record_event(
                folder_cfg,
                "backup.failed",
                component="backup",
                severity="error",
                data={"reason": type(exc).__name__, "error": str(exc)},
                correlation=correlation,
                effect="none" if dry_run else None,
            )
            publish_runtime_status(
                api_state,
                config,
                state="error",
                failed_folder=folder_id,
                last_error=str(exc),
                last_finish=now_iso(),
                last_progress=f"Failed on folder {index} of {total_folders}",
            )
            raise

        report_details = backup_report_details(report_path)
        attempt_changes, counts, report_hash, report_bytes = report_details
        item["changes"] = merge_backup_changes(list(item.get("changes") or []), attempt_changes)
        if not dry_run:
            item["attempt"] = {
                "operation_id": operation_id,
                "report_path": str(report_path),
                "started_at": (item.get("attempt") or {}).get("started_at"),
                "completed_at": now_iso(),
                "status": "report_committed",
                "exit_code": code,
                "report_sha256": report_hash,
                "report_bytes": report_bytes,
                "counts": counts,
            }
            if code == 0:
                item["stage"] = "generation"
            save_backup_queue(config, queue)
        record_backup_report(folder_cfg, report_path, operation_id, dry_run=dry_run, details=report_details)

        if code != 0:
            if text_looks_rate_limited(LAST_COMMAND_OUTPUT):
                retry_after = rate_limit_retry_after_seconds(LAST_COMMAND_OUTPUT, int(config.get("rate_limit_backoff_seconds", 300)))
                item["attempts"] = int(item.get("attempts", 0)) + 1
                retry_delay = max(retry_delay, retry_after)
                retry_reason = f"Dropbox rate limited Safe Sync; cooling down for {retry_after}s"
                provider_limited = True
                if not dry_run:
                    save_backup_queue(config, queue)
                record_event(
                    folder_cfg,
                    "backup.failed",
                    component="backup",
                    severity="warning",
                    data={"reason": "rate_limited", "retry_after_seconds": retry_after, "counts": counts},
                    correlation=correlation,
                    effect="none" if dry_run else None,
                )
                publish_runtime_status(
                    api_state,
                    config,
                    state="backoff",
                    failed_folder=folder_id,
                    last_warning=f"Dropbox rate limited Safe Sync; cooling down for {retry_after}s",
                    last_error=None,
                    queued_backup=True,
                    backoff_seconds=retry_after,
                    backoff_until=future_iso(retry_after),
                    last_finish=now_iso(),
                    last_progress=f"Dropbox throttled folder {index} of {total_folders}",
                )
                break
            if text_looks_temporary_remote_failure(LAST_COMMAND_OUTPUT, code):
                item["attempts"] = int(item.get("attempts", 0)) + 1
                delay = temporary_retry_seconds(int(item["attempts"]))
                retry_delay = max(retry_delay, delay)
                retry_reason = f"Temporary Dropbox failure on {folder_id}; retrying in {delay}s"
                if not dry_run:
                    save_backup_queue(config, queue)
                record_event(
                    folder_cfg,
                    "backup.retry_scheduled",
                    component="backup",
                    severity="warning",
                    data={
                        "stage": "payload",
                        "reason": "temporary_remote",
                        "exit_code": code,
                        "attempt": item["attempts"],
                        "retry_after_seconds": delay,
                        "counts": counts,
                    },
                    correlation=correlation,
                    effect="none" if dry_run else None,
                )
                publish_runtime_status(
                    api_state,
                    config,
                    state="retry_pending",
                    failed_folder=folder_id,
                    last_warning=retry_reason,
                    last_error=None,
                    queued_backup=True,
                    backoff_seconds=delay,
                    last_finish=now_iso(),
                    last_progress=f"Temporary failure on folder {index} of {total_folders}; later folders will continue",
                )
                continue
            error = reconnect_dropbox_message() if text_looks_auth_failure(LAST_COMMAND_OUTPUT) else f"rclone exit {code}"
            record_event(
                folder_cfg,
                "backup.failed",
                component="backup",
                severity="error",
                data={"reason": "authentication" if text_looks_auth_failure(LAST_COMMAND_OUTPUT) else "rclone_exit", "exit_code": code, "counts": counts},
                correlation=correlation,
                effect="none" if dry_run else None,
            )
            publish_runtime_status(
                api_state,
                config,
                state="error",
                failed_folder=folder_id,
                last_error=error,
                last_finish=now_iso(),
                last_progress=f"Failed on folder {index} of {total_folders}",
            )
            return code, folder_id

        saw_rate_limit = text_looks_rate_limited(LAST_COMMAND_OUTPUT)

        if not dry_run:
            publish_runtime_status(
                api_state,
                config,
                state="publishing",
                sync_phase="finalizing",
                progress_percent=100,
                current_file=None,
                last_progress="Finalizing folder backup",
            )
            publication_code = publish_generation(
                folder_cfg,
                operation_id=operation_id,
                changes=list(item.get("changes") or []),
            )
            if publication_code != 0:
                item["attempts"] = int(item.get("attempts", 0)) + 1
                delay = temporary_retry_seconds(int(item["attempts"]))
                retry_delay = max(retry_delay, delay)
                retry_reason = f"Change publication for {folder_id} is pending; retrying in {delay}s"
                save_backup_queue(config, queue)
                publish_runtime_status(
                    api_state,
                    config,
                    state="retry_pending",
                    failed_folder=folder_id,
                    last_error=None,
                    last_warning=retry_reason,
                    queued_backup=True,
                    backoff_seconds=delay,
                    last_finish=now_iso(),
                    last_progress=f"Folder data converged; change publication is pending for folder {index} of {total_folders}",
                )
                continue

        record_event(
            folder_cfg,
            "backup.completed",
            component="backup",
            data={"exit_code": code, "counts": counts, "dry_run": dry_run},
            correlation=correlation,
            effect="none" if dry_run else None,
        )
        queue["items"].remove(item)
        completed_this_run.append(folder_id)
        if not dry_run:
            save_backup_queue(config, queue)
            replicate_event_journal(config)
        if saw_rate_limit:
            retry_delay = max(retry_delay, int(config.get("rate_limit_backoff_seconds", 300)))
            retry_reason = f"Dropbox throttled requests but {folder_id} completed; pausing before remaining folders"
            provider_limited = True
            break

    if queue["items"]:
        retry_delay = retry_delay or temporary_retry_seconds(1)
        failed_folder = str(queue["items"][0]["folder_id"])
        pending_ids = [str(item["folder_id"]) for item in queue["items"]]
        warning = retry_reason or f"Backup work remains for {len(pending_ids)} folder(s)"
        publish_runtime_status(
            api_state,
            config,
            state="backoff",
            failed_folder=failed_folder,
            last_warning=warning,
            last_error=None,
            queued_backup=True,
            configured_folder_count=total_folders,
            pending_folders=pending_ids,
            completed_folders=completed_this_run,
            backoff_seconds=retry_delay,
            backoff_until=future_iso(retry_delay),
            last_finish=now_iso(),
            last_progress=f"{len(pending_ids)} of {total_folders} folders pending; completed folders will not be repeated",
            retry_kind="provider_rate_limit" if provider_limited else "temporary_remote",
        )
        return RATE_LIMIT_EXIT, failed_folder

    publish_runtime_status(
        api_state,
        config,
        configured_folder_count=total_folders,
        pending_folders=[],
        completed_folders=[str(folder["id"]) for folder in folders],
        queued_backup=False,
    )
    if not dry_run:
        registry_code = update_registry(config)
        if registry_code != 0:
            publish_runtime_status(api_state, config, state="error", last_error="registry update failed", last_finish=now_iso())
            return registry_code, "registry"
    return 0, None


def run_receive_runtime(config: dict[str, Any], request: dict[str, Any], api_state: DaemonApiState) -> int:
    source = str(request["source"])
    destination = str(request["destination"])
    selected_paths = [str(path) for path in request.get("selected_paths") or []]
    operation_id = new_operation_id("receive")
    correlation = {
        "operation_id": operation_id,
        "generation_id": request.get("source_generation"),
        "link_id": request.get("link_id"),
    }
    record_event(
        config,
        "job.stage_started",
        component="receive",
        data={
            "source": source,
            "destination": destination,
            "selected_paths": selected_paths,
            "mode": str(request.get("mode") or "receive"),
        },
        correlation=correlation,
    )
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
        record_event(
            config,
            "job.stage_failed",
            component="receive",
            severity="error",
            data={"error": str(exc), "reason": type(exc).__name__},
            correlation=correlation,
        )
        publish_runtime_status(api_state, config, state="error", last_error=str(exc), last_finish=now_iso())
        return 1
    if code != 0:
        record_event(
            config,
            "job.stage_failed",
            component="receive",
            severity="error",
            data={"exit_code": code, "error": str(job.get("error") or "")},
            correlation={**correlation, "job_id": job["id"]},
        )
        publish_runtime_status(
            api_state,
            config,
            state="error",
            last_error=str(job.get("error") or f"rclone exit {code}"),
            last_finish=now_iso(),
            receive_job_id=job["id"],
        )
        return code
    record_event(
        config,
        "job.staged",
        component="receive",
        data={
            "status": job["status"],
            "mode": job.get("mode"),
            "selected_path_count": len(selected_paths),
        },
        correlation={**correlation, "job_id": job["id"]},
    )
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
        if query == "audit_sync":
            result = replicate_event_journal(config)
            api_state.complete_query(ticket, {"ok": True, "status": result})
            publish_runtime_status(
                api_state,
                config,
                state="watching",
                audit_health=result.get("health"),
                audit_pending_cloud_segments=result.get("pending_cloud_segments"),
                audit_last_cloud_sync=(result.get("replication") or {}).get("last_success_at"),
            )
            return
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
    started_event = {
        "apply": "job.apply_started",
        "reconcile": "job.reconciliation_required",
        "rollback": "job.rollback_started",
    }[operation]
    record_event(
        config,
        started_event,
        component="receive",
        data={"policy_count": len(request.get("policies") or {})},
        correlation={"job_id": job_id},
    )
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
        record_event(
            config,
            "job.blocked",
            component="receive",
            severity="warning",
            data={"operation": operation, "error": str(exc), "reason": type(exc).__name__},
            correlation={"job_id": job_id},
        )
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
    completed_event = {
        "apply": "job.applied",
        "reconcile": "job.reconciled",
        "rollback": "job.rolled_back",
    }[operation]
    record_event(
        config,
        completed_event,
        component="receive",
        data={
            "status": result.get("status"),
            "action_count": len(result.get("actions") or []),
            "rollback_conflicts": result.get("rollback_conflicts") or [],
        },
        correlation={"job_id": job_id, "generation_id": result.get("source_generation"), "link_id": result.get("link_id")},
    )
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


def parse_duration(value: str) -> dt.timedelta:
    match = re.fullmatch(r"\s*(\d+)\s*([smhdw])\s*", value.lower())
    if not match:
        raise SystemExit("duration must use s, m, h, d, or w, for example 30m or 2h")
    amount = int(match.group(1))
    unit = match.group(2)
    seconds = amount * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    return dt.timedelta(seconds=seconds)


def event_summary(event: dict[str, Any]) -> str:
    correlation = event.get("correlation") or {}
    context = [
        f"folder={correlation['folder_id']}" if correlation.get("folder_id") else "",
        f"job={correlation['job_id']}" if correlation.get("job_id") else "",
        f"generation={correlation['generation_id']}" if correlation.get("generation_id") else "",
    ]
    data = event.get("data") or {}
    detail = ""
    if data.get("path"):
        detail = f" path={data['path']}"
    elif data.get("reason"):
        detail = f" reason={data['reason']}"
    elif data.get("status"):
        detail = f" status={data['status']}"
    suffix = f" {' '.join(item for item in context if item)}" if any(context) else ""
    return f"{event['occurred_at']} {str(event['severity']).upper():7} {event['event_type']}{suffix}{detail}"


def cmd_logs(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser()
    config = normalized_config(load_config(config_path))
    journal = event_journal(config)
    command = getattr(args, "logs_cmd", None) or "show"
    if command in {"status", "cloud-status"}:
        status = journal.status()
        status["local_path"] = str(journal.root)
        status["remote_path"] = audit_remote_root(config)
        print(json.dumps(status if command == "status" else status["replication"] | {
            "health": status["health"],
            "pending_cloud_segments": status["pending_cloud_segments"],
            "gaps": status["gaps"],
            "remote_path": status["remote_path"],
        }, indent=2, sort_keys=True))
        return 0
    if command == "sync":
        try:
            response = daemon_api(config, "audit_sync", _timeout_seconds=305)
            if not response.get("ok"):
                raise SystemExit(str(response.get("error") or "audit sync failed"))
            status = dict(response.get("status") or {})
        except OSError:
            with Lock(lock_file(config)):
                status = replicate_event_journal(config)
        print(json.dumps(status, indent=2, sort_keys=True))
        return 1 if (status.get("replication") or {}).get("last_error") else 0
    if command == "level":
        old_settings = settings_from_config(config)
        logging_config = dict(config["logging"])
        level = str(args.level).lower()
        if level not in {"quiet", "normal", "debug", "trace"}:
            raise SystemExit("level must be quiet, normal, debug, or trace")
        if args.for_duration:
            duration = parse_duration(args.for_duration)
            logging_config["temporary_level"] = level
            logging_config["temporary_until"] = (
                dt.datetime.now(dt.timezone.utc) + duration
            ).isoformat(timespec="seconds").replace("+00:00", "Z")
        else:
            logging_config["level"] = level
            logging_config.pop("temporary_level", None)
            logging_config.pop("temporary_until", None)
        config["logging"] = logging_config
        updated = write_config(config_path, config)
        record_event(
            updated,
            "logging.level_changed",
            component="logging",
            data={
                "old_level": old_settings.level,
                "new_level": settings_from_config(updated).level,
                "temporary_until": logging_config.get("temporary_until"),
                "actor": "cli",
            },
        )
        print(json.dumps(updated["logging"], indent=2, sort_keys=True))
        return 0
    since = None
    since_value = getattr(args, "since", None)
    if since_value:
        since = dt.datetime.now(dt.timezone.utc) - parse_duration(since_value)
    events = journal.events(
        limit=getattr(args, "limit", None) or getattr(args, "lines", 80),
        event_type=getattr(args, "event_type", None),
        folder_id=getattr(args, "folder", None),
        severity=getattr(args, "severity", None),
        since=since,
    )
    if command == "export":
        output = Path(args.output).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(output, "".join(json.dumps(event, sort_keys=True) + "\n" for event in events))
        print(output)
        return 0
    if getattr(args, "json", False):
        print(json.dumps(events, indent=2, sort_keys=True))
    elif getattr(args, "jsonl", False):
        for event in events:
            print(json.dumps(event, sort_keys=True))
    else:
        for event in events:
            print(event_summary(event))
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
    correlation = {"profile_id": config.get("profile_id")}
    if result.returncode != 0:
        record_event(
            config,
            "registry.publication_failed",
            component="registry",
            severity="error",
            data={"exit_code": result.returncode, "error": result.stdout or ""},
            correlation=correlation,
        )
    else:
        record_event(
            config,
            "registry.published",
            component="registry",
            data={"folder_count": len(config.get("folders") or [])},
            correlation=correlation,
        )
    return int(result.returncode)


def list_registry_files(config: dict[str, Any]) -> set[str] | None:
    result = rclone_capture(config, ["lsf", registry_dir(config), "--files-only"])
    if result.returncode != 0:
        record_event(
            config,
            "registry.read_failed",
            component="registry",
            severity="error",
            data={"exit_code": result.returncode, "error": result.stdout or ""},
            correlation={"profile_id": config.get("profile_id")},
        )
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
    orphan_result = reconcile_orphan_child(config)
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
    next_audit_flush = startup_now + settings_from_config(config).cloud_flush_interval_seconds
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
    record_event(
        config,
        "runtime.started",
        component="runtime",
        data={"watcher": watcher_mode, "folder_count": len(folders), "dry_run": args.dry_run},
        effect="none" if args.dry_run else None,
    )
    record_event(
        config,
        "watcher.started" if watcher_mode == "native" else "watcher.degraded",
        component="watcher",
        severity="info" if watcher_mode == "native" else "warning",
        data={"mode": watcher_mode, "folder_ids": [folder["id"] for folder in folders], "reason": watcher_warning},
    )
    record_event(
        config,
        "reconciliation.completed",
        component="watcher",
        data={"reason": "daemon_startup", "reconciled_jobs": reconciled_jobs, "orphan_child": orphan_result},
    )
    api_server.start()

    previous_signal_handlers: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        def handle_shutdown(signum: int, _frame: Any) -> None:
            request_active_child_stop()
            raise DaemonShutdown(f"signal {signum}")

        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_signal_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, handle_shutdown)

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
                record_event(
                    config,
                    "runtime.stopping",
                    component="runtime",
                    data={"reason": "configuration_changed"},
                )
                return 0
            if watcher_mode == "native" and not watcher.healthy():
                watcher.stop()
                watcher_mode = "polling"
                watcher_warning = "native watcher stopped; using full-tree polling"
                previous_snapshots = folder_snapshots(config)
                publish_runtime_status(api_state, config, watcher=watcher_mode, last_warning=watcher_warning)
                record_event(
                    config,
                    "watcher.degraded",
                    component="watcher",
                    severity="warning",
                    data={"mode": watcher_mode, "reason": watcher_warning},
                )
            if api_state.consume_backup_request():
                manual_backup_pending = True
                publish_runtime_status(api_state, config, state="dirty", last_error=None, queued_backup=True, note="manual backup queued")
                record_event(
                    config,
                    "backup.queued",
                    component="scheduler",
                    data={"trigger": "manual", "folder_ids": [folder["id"] for folder in enabled_folders(config)]},
                )

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
                        for notification in link_notifications:
                            record_event(
                                config,
                                "link.change_detected",
                                component="links",
                                data={"label": notification.get("label")},
                                correlation={
                                    "job_id": None,
                                    "generation_id": notification.get("generation_id"),
                                    "link_id": notification.get("link_id"),
                                },
                            )
                        publish_runtime_status(
                            api_state,
                            config,
                            linked_folder_changes=link_notifications,
                            last_warning=f"{len(link_notifications)} linked folder(s) have peer changes ready for review",
                            _activity_event=f"Linked-folder changes detected: {', '.join(item['label'] for item in link_notifications)}",
                        )
                except BaseException as exc:
                    record_event(
                        config,
                        "link.detection_failed",
                        component="links",
                        severity="warning",
                        data={"error": str(exc)},
                    )
                next_link_check = now + 60.0

            if now >= next_audit_flush and api_state.snapshot().get("state") not in {"syncing", "transferring", "staging", "applying", "backoff"}:
                audit_status = replicate_event_journal(config)
                publish_runtime_status(
                    api_state,
                    config,
                    audit_health=audit_status.get("health"),
                    audit_pending_cloud_segments=audit_status.get("pending_cloud_segments"),
                    audit_last_cloud_sync=(audit_status.get("replication") or {}).get("last_success_at"),
                    audit_gaps=len(audit_status.get("gaps") or []),
                )
                next_audit_flush = now + settings_from_config(config).cloud_flush_interval_seconds

            if watcher_mode == "native":
                if hasattr(watcher, "consume_details"):
                    change_details = watcher.consume_details()
                    changed = sorted(change_details)
                else:  # Compatibility for injected/older watcher adapters.
                    changed = watcher.consume()
                    change_details = {
                        folder_id: {"paths": [], "path_count": None, "paths_truncated": False}
                        for folder_id in changed
                    }
            else:
                current_snapshots = folder_snapshots(config)
                changed = [folder_id for folder_id, snapshot in current_snapshots.items() if snapshot != previous_snapshots.get(folder_id)]
                previous_snapshots = current_snapshots
                change_details = {
                    folder_id: {"paths": [], "path_count": None, "paths_truncated": False}
                    for folder_id in changed
                }
            if changed:
                daemon.mark_dirty(now)
                local_link_changes = detect_local_link_changes(config, changed)
                for folder_id in changed:
                    details = change_details.get(folder_id, {})
                    record_event(
                        config,
                        "watcher.change_detected",
                        component="watcher",
                        data={
                            "mode": watcher_mode,
                            "paths": details.get("paths", []),
                            "path_count": details.get("path_count"),
                            "paths_truncated": bool(details.get("paths_truncated", False)),
                        },
                        correlation={"folder_id": folder_id},
                    )
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
                record_event(
                    config,
                    "backup.queued",
                    component="scheduler",
                    data={"trigger": "manual" if manual_run else "automatic", "ready": True},
                )
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
                requested_backoff = api_state.snapshot().get("backoff_seconds") if rate_limited else None
                try:
                    backoff_seconds = float(requested_backoff) if requested_backoff is not None else None
                except (TypeError, ValueError):
                    backoff_seconds = None
                daemon.note_sync_finished(
                    after,
                    rate_limited=rate_limited,
                    backoff_seconds=backoff_seconds,
                )
                if code == 0:
                    publish_runtime_status(
                        api_state,
                        config,
                        state="watching",
                        sync_phase="complete",
                        progress_percent=100,
                        current_file=None,
                        last_progress="Backup cycle complete",
                        last_success=now_iso(),
                        last_finish=now_iso(),
                        last_error=None,
                        last_warning=None,
                        queued_backup=False,
                    )
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
                record_event(config, "runtime.stopping", component="runtime", data={"reason": "max_loops"})
                return 0
            api_state.wait(settings.poll_interval_seconds)
    except DaemonShutdown as exc:
        record_event(config, "runtime.stopping", component="runtime", data={"reason": str(exc)})
        return 0
    finally:
        for signum, previous in previous_signal_handlers.items():
            signal.signal(signum, previous)
        watcher.stop()
        api_server.stop()
        record_event(config, "runtime.stopped", component="runtime", data={"watcher": watcher_mode})



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
        # Flush the old profile through its own remote before changing routing.
        replicate_event_journal(config)
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
        "10",
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


def audit_remote_root(config: dict[str, Any]) -> str:
    cfg = normalized_config(config)
    return remote_join(
        str(cfg["remote_base"]),
        f".audit/{safe_id(str(cfg['profile_id']))}/{safe_id(str(cfg['machine_id']))}/{safe_id(str(cfg['install_id']))}",
    )


@contextmanager
def audit_replication_lock(config: dict[str, Any]):
    path = event_journal(normalized_config(config)).root / "replication.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def replicate_event_journal(config: dict[str, Any], *, seal: bool = True) -> dict[str, Any]:
    with audit_replication_lock(config):
        return _replicate_event_journal_unlocked(config, seal=seal)


def _replicate_event_journal_unlocked(config: dict[str, Any], *, seal: bool = True) -> dict[str, Any]:
    """Publish verified sealed journal segments to this profile's owned remote."""
    cfg = normalized_config(config)
    journal = event_journal(cfg)
    if not journal.settings.cloud_enabled:
        return journal.status()
    previous = journal.status()
    if seal:
        journal.seal_active()
    segments = journal.segment_records()
    remote_root = audit_remote_root(cfg)
    remote_segments = remote_join(remote_root, "segments")
    uploaded_hashes: set[str] = set()
    remote_names: dict[str, str] = {}
    try:
        for segment in segments:
            name = (
                f"{segment['epoch']}-{int(segment['start_sequence']):012d}-"
                f"{int(segment['end_sequence']):012d}-{segment['sha256']}.jsonl"
            )
            remote_names[str(segment["sha256"])] = name
            if segment.get("replicated", False):
                uploaded_hashes.add(str(segment["sha256"]))
                continue
            remote_object = remote_join(remote_segments, name)
            segment_text = journal.segment_text(segment)
            result = rclone_capture(
                cfg,
                ["rcat", remote_object],
                input_text=segment_text,
            )
            if result.returncode != 0:
                raise JournalError(f"segment upload failed ({result.returncode}): {result.stdout or ''}")
            verified = rclone_capture(cfg, ["cat", remote_object])
            remote_hash = hashlib.sha256((verified.stdout or "").encode("utf-8")).hexdigest()
            if verified.returncode != 0 or remote_hash != str(segment["sha256"]):
                raise JournalError(f"segment verification failed ({verified.returncode}): {remote_object}")
            uploaded_hashes.add(str(segment["sha256"]))

        manifest = journal.cloud_manifest()
        for segment in manifest["segments"]:
            segment["object"] = remote_names[str(segment["sha256"])]
        manifest_body = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        manifest_hash = hashlib.sha256(manifest_body.encode()).hexdigest()
        immutable_manifest = remote_join(remote_root, f"manifests/{manifest_hash}.json")
        staged = rclone_capture(cfg, ["rcat", immutable_manifest], input_text=manifest_body)
        if staged.returncode != 0:
            raise JournalError(f"manifest upload failed ({staged.returncode}): {staged.stdout or ''}")
        manifest_check = rclone_capture(cfg, ["cat", immutable_manifest])
        if manifest_check.returncode != 0 or hashlib.sha256((manifest_check.stdout or "").encode()).hexdigest() != manifest_hash:
            raise JournalError(f"manifest verification failed ({manifest_check.returncode}): {immutable_manifest}")
        pointer = {
            "schema_version": 1,
            "manifest_sha256": manifest_hash,
            "object": f"manifests/{manifest_hash}.json",
            "published_at": now_iso(),
        }
        pointer_body = json.dumps(pointer, indent=2, sort_keys=True) + "\n"
        latest_manifest = remote_join(remote_root, "manifest.json")
        published = rclone_capture(cfg, ["rcat", latest_manifest], input_text=pointer_body)
        if published.returncode != 0:
            raise JournalError(f"manifest pointer upload failed ({published.returncode}): {published.stdout or ''}")
        pointer_check = rclone_capture(cfg, ["cat", latest_manifest])
        if pointer_check.returncode != 0 or (pointer_check.stdout or "") != pointer_body:
            raise JournalError(f"manifest pointer verification failed ({pointer_check.returncode}): {latest_manifest}")
        journal.mark_replicated(uploaded_hashes, manifest_hash=manifest_hash)

        # Cleanup is deliberately after manifest publication. A cleanup error
        # leaves harmless stale immutable objects rather than invalidating the
        # newly verified manifest.
        listing = rclone_capture(cfg, ["lsjson", remote_segments])
        if listing.returncode == 0:
            try:
                entries = json.loads(listing.stdout or "[]")
            except json.JSONDecodeError:
                entries = []
            referenced = set(remote_names.values())
            for entry in entries if isinstance(entries, list) else []:
                name = str(entry.get("Name") or entry.get("Path") or "") if isinstance(entry, dict) else ""
                if name and name not in referenced and "/" not in name and "\\" not in name:
                    rclone_capture(cfg, ["deletefile", remote_join(remote_segments, name)])
        if previous.get("replication", {}).get("last_error"):
            record_event(
                cfg,
                "logging.cloud_recovered",
                component="logging",
                data={"replicated_segments": len(uploaded_hashes)},
            )
        return journal.status()
    except (JournalError, OSError, ValueError) as exc:
        journal.mark_replication_error(str(exc))
        if not previous.get("replication", {}).get("last_error"):
            record_event(
                cfg,
                "logging.cloud_degraded",
                component="logging",
                severity="warning",
                data={"error": str(exc)},
            )
        return journal.status()


def state_root_path(config: dict[str, Any]) -> Path:
    return Path(normalized_config(config)["state_root"]).expanduser()


def backup_report_path(config: dict[str, Any]) -> Path:
    root = state_root_path(config) / "backup-reports"
    root.mkdir(parents=True, exist_ok=True)
    folder_id = safe_id(str(config.get("folder_id") or "folder"))
    return root / f"{folder_id}-{uuid.uuid4().hex}.combined"


def generation_local_dir(config: dict[str, Any]) -> Path:
    cfg = normalized_config(config)
    folder_id = safe_id(str(config.get("folder_id") or "folder"))
    return state_root_path(cfg) / "generations" / str(cfg["machine_id"]) / folder_id


def pending_generation_path(config: dict[str, Any]) -> Path:
    return generation_local_dir(config) / "pending.json"


def _verified_remote_write(
    config: dict[str, Any],
    remote_path: str,
    body: str,
    *,
    immutable: bool,
) -> tuple[int, str]:
    """Write one object and resolve an ambiguous upload by reading it back."""
    existing = rclone_capture(config, ["cat", remote_path])
    if existing.returncode == 0 and (existing.stdout or "") == body:
        return 0, "already verified"
    if immutable and existing.returncode == 0:
        return 1, "immutable generation path already contains different data"
    uploaded = rclone_capture(config, ["rcat", remote_path], input_text=body)
    verified = rclone_capture(config, ["cat", remote_path])
    if verified.returncode == 0 and (verified.stdout or "") == body:
        return 0, "verified"
    detail = (uploaded.stdout or verified.stdout or "remote verification failed").strip()
    return int(uploaded.returncode or verified.returncode or 1), detail


def _publish_pending_generation(config: dict[str, Any], body: str, *, operation_id: str | None) -> int:
    cfg = normalized_config(config)
    try:
        record = json.loads(body)
    except json.JSONDecodeError:
        return 1
    folder_id = str(record.get("folder_id") or config.get("folder_id") or "")
    generation_id = str(record.get("generation_id") or "")
    remote_dir = generation_remote_dir(str(cfg["remote_base"]), str(cfg["machine_id"]), folder_id)
    immutable_path = remote_join(remote_dir, f"generations/{generation_id}.json")
    latest_path = remote_join(remote_dir, "latest.json")
    correlation = {
        "operation_id": operation_id,
        "folder_id": folder_id,
        "generation_id": generation_id,
    }
    immutable_code, immutable_detail = _verified_remote_write(cfg, immutable_path, body, immutable=True)
    if immutable_code != 0:
        record_event(
            cfg,
            "generation.publication_failed",
            component="generation",
            severity="error",
            data={"stage": "immutable", "exit_code": immutable_code, "error": immutable_detail},
            correlation=correlation,
        )
        return immutable_code
    latest_code, latest_detail = _verified_remote_write(cfg, latest_path, body, immutable=False)
    if latest_code != 0:
        record_event(
            cfg,
            "generation.publication_failed",
            component="generation",
            severity="error",
            data={"stage": "latest", "exit_code": latest_code, "error": latest_detail},
            correlation=correlation,
        )
        return latest_code

    local_dir = generation_local_dir({**cfg, "folder_id": folder_id})
    atomic_write_text(local_dir / "generations" / f"{generation_id}.json", body)
    atomic_write_text(local_dir / "latest.json", body)
    try:
        pending_generation_path({**cfg, "folder_id": folder_id}).unlink()
    except FileNotFoundError:
        pass
    record_event(
        cfg,
        "generation.published",
        component="generation",
        data={
            "change_count": len(record.get("changes") or []),
            "parent_generation": record.get("parent_generation"),
        },
        correlation=correlation,
    )
    return 0


def publish_generation(
    config: dict[str, Any],
    report_path: Path | None = None,
    *,
    operation_id: str | None = None,
    changes: list[dict[str, str]] | None = None,
) -> int:
    """Durably publish an immutable owner generation, then its latest pointer."""
    cfg = normalized_config(config)
    folder_id = str(config.get("folder_id") or "")
    if not folder_id:
        raise TransferError("folder_id is required to publish a generation")
    supplied_changes = changes
    if supplied_changes is None:
        report_text = report_path.read_text(errors="replace") if report_path is not None and report_path.exists() else ""
        supplied_changes = parse_combined_report(report_text)
    supplied_changes = [change for change in supplied_changes if change.get("operation") != "error"]

    pending_path = pending_generation_path(config)
    if pending_path.exists():
        pending_body = pending_path.read_text()
        try:
            pending_record = json.loads(pending_body)
        except json.JSONDecodeError:
            record_event(
                cfg,
                "generation.publication_failed",
                component="generation",
                severity="error",
                data={"stage": "local_pending", "error": "pending generation is invalid JSON"},
                correlation={"operation_id": operation_id, "folder_id": folder_id},
            )
            return 1
        pending_code = _publish_pending_generation(config, pending_body, operation_id=operation_id)
        if pending_code != 0:
            return pending_code
        canonical_supplied = sorted(supplied_changes, key=lambda change: (str(change.get("path")), str(change.get("operation"))))
        canonical_pending = sorted(
            list(pending_record.get("changes") or []),
            key=lambda change: (str(change.get("path")), str(change.get("operation"))),
        )
        if canonical_supplied == canonical_pending:
            return 0

    if not supplied_changes:
        record_event(
            cfg,
            "generation.skipped",
            component="generation",
            data={"reason": "no_changes"},
            correlation={"operation_id": operation_id, "folder_id": folder_id},
        )
        return 0

    remote_dir = generation_remote_dir(str(cfg["remote_base"]), str(cfg["machine_id"]), folder_id)
    latest_path = remote_join(remote_dir, "latest.json")
    parent_generation: str | None = None
    previous = rclone_capture(cfg, ["cat", latest_path])
    if previous.returncode == 0:
        try:
            parent_generation = str(json.loads(previous.stdout).get("generation_id") or "") or None
        except (json.JSONDecodeError, AttributeError):
            record_event(
                cfg,
                "generation.publication_failed",
                component="generation",
                severity="error",
                data={"stage": "parent_lookup", "error": "latest generation pointer is invalid"},
                correlation={"operation_id": operation_id, "folder_id": folder_id},
            )
            return 1
    elif not text_looks_remote_not_found(previous.stdout or ""):
        record_event(
            cfg,
            "generation.publication_failed",
            component="generation",
            severity="warning",
            data={
                "stage": "parent_lookup",
                "exit_code": previous.returncode,
                "error": previous.stdout or "",
            },
            correlation={"operation_id": operation_id, "folder_id": folder_id},
        )
        return int(previous.returncode or 1)
    record = generation_record(
        machine_id=str(cfg["machine_id"]),
        install_id=str(cfg["install_id"]),
        profile_id=str(cfg["profile_id"]),
        folder_id=folder_id,
        filter_policy=effective_filter_fingerprint(config),
        changes=supplied_changes,
        parent_generation=parent_generation,
    )
    correlation = {
        "operation_id": operation_id,
        "folder_id": folder_id,
        "generation_id": record["generation_id"],
    }
    record_event(
        cfg,
        "generation.publication_started",
        component="generation",
        data={"change_count": len(supplied_changes), "parent_generation": parent_generation},
        correlation=correlation,
    )
    body = json.dumps(record, indent=2, sort_keys=True) + "\n"
    # Persist the exact ID/body before the first remote mutation. A restart or
    # timeout retries this same generation rather than inventing another one.
    atomic_write_text(pending_path, body)
    return _publish_pending_generation(config, body, operation_id=operation_id)


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
        {**config, "_job_id": job["id"]},
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
    operation_id = new_operation_id("receive")
    correlation = {"operation_id": operation_id, "generation_id": source_generation, "link_id": link_id}
    record_event(
        config,
        "job.stage_started",
        component="receive",
        data={"source": source, "destination": destination, "selected_paths": selected_paths, "mode": mode},
        correlation=correlation,
    )
    with Lock(lock_file(config)):
        try:
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
        except BaseException as exc:
            record_event(
                config,
                "job.stage_failed",
                component="receive",
                severity="error",
                data={"error": str(exc), "reason": type(exc).__name__},
                correlation=correlation,
            )
            raise
    record_event(
        config,
        "job.staged" if code == 0 else "job.stage_failed",
        component="receive",
        severity="info" if code == 0 else "error",
        data={"status": job.get("status"), "exit_code": code, "mode": mode},
        correlation={**correlation, "job_id": job["id"]},
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
            started_event = {
                "apply": "job.apply_started",
                "reconcile": "job.reconciliation_required",
                "rollback": "job.rollback_started",
            }[args.jobs_cmd]
            record_event(
                config,
                started_event,
                component="receive",
                data={"policy_count": len(policies)},
                correlation={"job_id": args.job_id},
            )
            with Lock(lock_file(config)):
                try:
                    if args.jobs_cmd == "apply":
                        job = store.load(args.job_id)
                        revalidate_remote_job_source(config, job)
                        value = store.commit_clone(args.job_id) if job.get("mode") == "clone" else store.apply(args.job_id, policies)
                    elif args.jobs_cmd == "reconcile":
                        value = store.reconcile(args.job_id)
                    else:
                        value = store.rollback(args.job_id)
                except BaseException as exc:
                    record_event(
                        config,
                        "job.blocked",
                        component="receive",
                        severity="warning",
                        data={"operation": args.jobs_cmd, "error": str(exc), "reason": type(exc).__name__},
                        correlation={"job_id": args.job_id},
                    )
                    raise
            completed_event = {
                "apply": "job.applied",
                "reconcile": "job.reconciled",
                "rollback": "job.rolled_back",
            }[args.jobs_cmd]
            record_event(
                config,
                completed_event,
                component="receive",
                data={"status": value.get("status"), "action_count": len(value.get("actions") or [])},
                correlation={"job_id": args.job_id, "generation_id": value.get("source_generation"), "link_id": value.get("link_id")},
            )
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

    logs = sub.add_parser("logs", help="Query structured audit and diagnostic events")
    logs.add_argument("--lines", type=int, default=80, help="Compatibility shortcut for recent events")
    logs.set_defaults(func=cmd_logs, logs_cmd=None)
    logs_sub = logs.add_subparsers(dest="logs_cmd")
    logs_sub.add_parser("status", help="Show local journal capacity and health").set_defaults(func=cmd_logs)
    logs_sub.add_parser("cloud-status", help="Show cloud replication health").set_defaults(func=cmd_logs)
    logs_sub.add_parser("sync", help="Seal and replicate pending events now").set_defaults(func=cmd_logs)
    logs_level = logs_sub.add_parser("level", help="Set persistent or temporary diagnostic verbosity")
    logs_level.add_argument("level", choices=("quiet", "normal", "debug", "trace"))
    logs_level.add_argument("--for", dest="for_duration", help="Temporary duration such as 30m or 2h")
    logs_level.set_defaults(func=cmd_logs)
    logs_show = logs_sub.add_parser("show", help="Query recent structured events")
    logs_show.add_argument("--since", help="Only events within a duration such as 2h or 7d")
    logs_show.add_argument("--event", dest="event_type", help="Exact event type")
    logs_show.add_argument("--folder", help="Configured folder id")
    logs_show.add_argument("--severity", choices=("error", "warning", "info", "debug", "trace"))
    logs_show.add_argument("--limit", type=int, default=200)
    logs_show.add_argument("--json", action="store_true", help="Print one JSON array")
    logs_show.add_argument("--jsonl", action="store_true", help="Print newline-delimited JSON")
    logs_show.set_defaults(func=cmd_logs)
    logs_export = logs_sub.add_parser("export", help="Export filtered events as JSONL")
    logs_export.add_argument("--since", help="Only events within a duration such as 24h")
    logs_export.add_argument("--event", dest="event_type", help="Exact event type")
    logs_export.add_argument("--folder", help="Configured folder id")
    logs_export.add_argument("--severity", choices=("error", "warning", "info", "debug", "trace"))
    logs_export.add_argument("--limit", type=int)
    logs_export.add_argument("--output", required=True)
    logs_export.set_defaults(func=cmd_logs)

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
