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
from pathlib import Path
from typing import Any

from safe_sync.daemon import DaemonState, WatchDaemon, WatchSettings, scan_tree


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
        "--metadata", "--stats", "10s",
        "--max-duration", f"{int(config.get('rclone_max_duration_seconds', 120))}s",
        "--timeout", "30s", "--contimeout", "10s",
        "--retries", "1", "--low-level-retries", "1", "--retries-sleep", "5s",
        "--log-level", "INFO",
    ]
    if dry_run:
        cmd.append("--dry-run")
    return cmd


def copy_cmd(config: dict[str, Any], src: str, dst: str, dry_run: bool) -> list[str]:
    cmd = [
        rclone_bin(config), "copy", src, dst,
        "--filter-from", str(filter_file(config)),
        "--metadata", "--stats", "10s",
        "--max-duration", f"{int(config.get('rclone_max_duration_seconds', 120))}s",
        "--timeout", "30s", "--contimeout", "10s",
        "--retries", "1", "--low-level-retries", "1", "--retries-sleep", "5s",
        "--log-level", "INFO",
    ]
    if dry_run:
        cmd.append("--dry-run")
    return cmd


def ensure_filter_template(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        shutil.copyfile(TEMPLATE_FILTER, path)


def default_config(machine: str) -> dict[str, Any]:
    ensure_filter_template(DEFAULT_FILTER)
    return {
        "machine": machine,
        "local_path": "~/test_sync",
        "remote_root": f"dropbox:computer-backups/test/{machine}/test_sync",
        "trash_root": f"dropbox:computer-backups/.trash/test/{machine}",
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
    dst.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    print(f"migrated {src} -> {dst}")
    return 0


def run_backup_with_config(config: dict[str, Any], dry_run: bool) -> int:
    with Lock(lock_file(config)):
        save_status(config, state="syncing", last_start=now_iso(), last_command="backup", last_error=None)
        try:
            preflight(config)
            code = run_command(config, backup_cmd(config, dry_run), dry_run=dry_run)
        except BaseException as exc:
            save_status(config, state="error", last_error=str(exc), last_finish=now_iso())
            raise
        if code == 0:
            save_status(config, state="idle", last_success=now_iso(), last_finish=now_iso(), last_error=None)
        else:
            save_status(config, state="error", last_error=f"rclone exit {code}", last_finish=now_iso())
        return code


def cmd_backup(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config).expanduser())
    return run_backup_with_config(config, args.dry_run)


def cmd_pull(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config).expanduser())
    src = f"{config['remote_base'].rstrip('/')}/{args.machine}/{args.remote_path.strip('/')}" if "remote_base" in config else args.source
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


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config).expanduser())
    status_path = Path(config.get("status_path", DEFAULT_STATUS)).expanduser()
    if status_path.exists():
        print(status_path.read_text(), end="")
    else:
        print(json.dumps({"state": "unknown", "status_path": str(status_path)}, indent=2))
    print(service_status_text())
    print(f"log: {log_path(config)}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config).expanduser())
    checks = {
        "config": str(Path(args.config).expanduser()),
        "rclone": rclone_bin(config),
        "filter_file": str(filter_file(config)),
        "local_path": str(Path(config["local_path"]).expanduser()),
        "remote_root": config["remote_root"],
        "trash_root": config["trash_root"],
        "poll_interval_seconds": str(config.get("poll_interval_seconds", 5)),
        "debounce_seconds": str(config.get("debounce_seconds", 20)),
        "fallback_interval_seconds": str(config.get("fallback_interval_seconds", 1800)),
    }
    for name, value in checks.items():
        print(f"{name}: {value}")
    missing = [p for p in [filter_file(config), Path(config["local_path"]).expanduser()] if not p.exists()]
    if missing:
        for p in missing:
            print(f"missing: {p}", file=sys.stderr)
        return 1
    preflight(config)
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



def os_name() -> str:
    return platform.system()


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "com.safe-sync.daemon.plist"


def service_status_text() -> str:
    system = os_name()
    if system == "Darwin":
        label = "com.safe-sync.daemon"
        result = subprocess.run(["launchctl", "list", label], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return "service: running" if result.returncode == 0 else "service: stopped"
    if system == "Linux":
        result = subprocess.run(["systemctl", "--user", "is-active", "safe-sync.service"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        state = (result.stdout or "unknown").strip()
        return f"service: {state}"
    return f"service: unsupported OS {system}"


def require_service_installed() -> None:
    system = os_name()
    if system == "Darwin" and not launchd_plist_path().exists():
        raise SystemExit("Service is not installed. Run ./install.sh from the repo first.")
    if system == "Linux":
        unit = Path.home() / ".config" / "systemd" / "user" / "safe-sync.service"
        if not unit.exists():
            raise SystemExit("Service is not installed. Run ./install.sh from the repo first.")


def service_cmd(action: str) -> int:
    system = os_name()
    require_service_installed()
    if system == "Darwin":
        plist = str(launchd_plist_path())
        if action == "start":
            result = subprocess.run(["launchctl", "load", plist], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        elif action == "stop":
            result = subprocess.run(["launchctl", "unload", plist], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        elif action == "restart":
            subprocess.run(["launchctl", "unload", plist], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            result = subprocess.run(["launchctl", "load", plist], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        else:
            raise SystemExit(f"Unknown service action: {action}")
    elif system == "Linux":
        result = subprocess.run(["systemctl", "--user", action, "safe-sync.service"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    else:
        raise SystemExit(f"Unsupported OS: {system}")
    if result.stdout:
        print(result.stdout, end="")
    if result.returncode == 0:
        print(service_status_text())
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

def cmd_daemon(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config).expanduser())
    settings = watch_settings_from_config(config, args)
    daemon = WatchDaemon(settings)
    local_path = Path(config["local_path"]).expanduser()
    if not local_path.exists():
        raise SystemExit(f"Local path does not exist: {local_path}")

    previous_snapshot = scan_tree(local_path)
    save_status(config, state="watching", watcher="polling", local_path=str(local_path), dry_run=args.dry_run, poll_interval_seconds=settings.poll_interval_seconds, debounce_seconds=settings.debounce_seconds, fallback_interval_seconds=settings.fallback_interval_seconds, last_error=None)
    append_log(config, f"[{now_iso()}] daemon started watcher=polling dry_run={args.dry_run}\n")

    loops = 0
    while True:
        loops += 1
        now = time.monotonic()
        current_snapshot = scan_tree(local_path)
        if current_snapshot != previous_snapshot:
            daemon.mark_dirty(now)
            previous_snapshot = current_snapshot
            save_status(config, state="dirty", last_change=now_iso(), watcher="polling")

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
            try:
                code = run_backup_with_config(config, args.dry_run)
                error_text = f"rclone exit {code}" if code != 0 else None
            except SystemExit as exc:
                code = int(exc.code) if isinstance(exc.code, int) else 75
                error_text = str(exc) or "backup failed"
            after = time.monotonic()
            should_backoff = code != 0
            rate_limited = should_backoff and looks_rate_limited(config)
            daemon.note_sync_finished(after, rate_limited=should_backoff)
            if code == 0:
                save_status(config, state="watching", last_success=now_iso(), last_error=None)
            else:
                reason = "rate limited" if rate_limited else "remote/preflight failed"
                save_status(config, state="backoff", last_error=f"{error_text}; {reason}")
            if args.once:
                return code

        if args.max_loops and loops >= args.max_loops:
            save_status(config, state="watching", note="max loops reached")
            return 0
        time.sleep(settings.poll_interval_seconds)


def launchd_plist(config_path: Path, program: Path, label: str = "com.safe-sync.daemon") -> str:
    log = DEFAULT_LOG_DIR / "launchd-daemon.log"
    return f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\">
<dict>
  <key>Label</key>
  <string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{program}</string>
    <string>--config</string>
    <string>{config_path}</string>
    <string>daemon</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{log}</string>
  <key>StandardErrorPath</key>
  <string>{log}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
"""


def systemd_service(config_path: Path, program: Path) -> str:
    return f"""[Unit]
Description=Safe Sync daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={program} --config {config_path} daemon
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
"""


def install_script(generated_dir: Path) -> str:
    return f"""#!/bin/sh
set -eu
GENERATED_DIR=\"{generated_dir}\"
case \"$(uname -s)\" in
  Darwin)
    mkdir -p \"$HOME/Library/LaunchAgents\"
    cp \"$GENERATED_DIR/com.safe-sync.daemon.plist\" \"$HOME/Library/LaunchAgents/com.safe-sync.daemon.plist\"
    ;;
  Linux)
    mkdir -p \"$HOME/.config/systemd/user\"
    cp \"$GENERATED_DIR/safe-sync.service\" \"$HOME/.config/systemd/user/safe-sync.service\"
    systemctl --user daemon-reload
    ;;
  *)
    echo \"Unsupported OS: $(uname -s)\" >&2
    exit 1
    ;;
esac
"""

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
        "safe-sync.service": systemd_service(config_path, program),
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
        metavar="{backup,start,stop,restart,status,logs,pull,list,doctor}",
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
        if action.dest not in {"daemon", "render-install"}
    ]
    return p


def main() -> int:
    args = parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
