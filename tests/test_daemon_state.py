import json
import os
import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest

from safe_sync.daemon import DaemonState, WatchDaemon, WatchSettings, scan_tree
from safe_sync.cli import (
    Lock,
    add_setup_folder,
    cmd_daemon,
    cmd_connect_dropbox,
    default_config,
    enabled_folders,
    ensure_local_profiles_registered,
    folder_snapshots,
    normalized_config,
    config_view,
    registry_doc,
    registry_path,
    RATE_LIMIT_EXIT,
    backup_cmd,
    bounded_seconds,
    copy_cmd,
    restart_backend_if_running,
    restore_last_sync_finish,
    rclone_env,
    run_command,
    run_backup_with_config,
    cmd_login_check,
    preflight,
    selected_folders,
    status_health,
    status_payload,
    unsafe_local_path_reason,
    write_config,
)
from safe_sync.api import DaemonApiState
from safe_sync.path_filter import should_ignore_watch_event
from safe_sync.service import backend_autostart_cmd, backend_autostart_status_text, service_status_text, systemd_unit


def test_debounce_waits_for_quiet_window():
    daemon = WatchDaemon(WatchSettings(debounce_seconds=20))
    daemon.mark_dirty(100.0)

    assert daemon.state.state == DaemonState.DIRTY
    assert not daemon.should_sync_after_debounce(119.9)
    assert daemon.should_sync_after_debounce(120.0)


def test_pending_change_during_sync_goes_to_cooldown():
    daemon = WatchDaemon()
    daemon.note_sync_started(100.0)
    daemon.mark_dirty(101.0)

    assert daemon.state.pending

    daemon.note_sync_finished(102.0)

    assert daemon.state.state == DaemonState.COOLDOWN
    assert daemon.state.dirty
    assert not daemon.state.pending


def test_rate_limit_enters_backoff():
    daemon = WatchDaemon(WatchSettings(rate_limit_backoff_seconds=300))
    daemon.note_sync_started(100.0)
    daemon.note_sync_finished(120.0, rate_limited=True)

    assert daemon.state.state == DaemonState.BACKOFF
    assert daemon.state.backoff_until_monotonic == 420.0


def test_status_health_reports_rate_limit_as_warning():
    health = status_health(
        {"poll_interval_seconds": 5},
        "running",
        {
            "state": "backoff",
            "last_warning": "Dropbox rate limited Safe Sync; cooling down for 300s",
            "backoff_until": "2099-01-01T00:00:00+00:00",
        },
    )

    assert health["health"] == "warning"
    assert "rate limited" in health["reason"]


def test_status_health_reports_setup_required_before_the_first_folder():
    health = status_health(default_config("test-machine"), "running", {"state": "unknown"})

    assert health["health"] == "setup_required"
    assert "Choose a folder" in health["reason"]


def test_login_check_is_silent_when_healthy(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config = normalized_config(default_config("test-machine"))
    local_folder = tmp_path / "work"
    local_folder.mkdir()
    add_setup_folder(config, str(local_folder))
    write_config(config_path, config)
    monkeypatch.setattr("safe_sync.cli.service_status_text", lambda: "service: running")
    monkeypatch.setattr(
        "safe_sync.cli.api_request",
        lambda *_args, **_kwargs: {"ok": True, "status": {"state": "watching", "updated_at": "2099-01-01T00:00:00+00:00"}},
    )

    assert cmd_login_check(SimpleNamespace(config=str(config_path))) == 0
    assert capsys.readouterr().out == ""


def test_login_check_gives_headless_reconnect_command(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config = normalized_config(default_config("test-machine"))
    write_config(config_path, config)
    monkeypatch.setattr("safe_sync.cli.service_status_text", lambda: "service: running")
    monkeypatch.setattr(
        "safe_sync.cli.api_request",
        lambda *_args, **_kwargs: {"ok": True, "status": {"state": "error", "last_error": "Dropbox authorization is invalid or revoked. Reconnect with: safe-sync connect-dropbox"}},
    )

    assert cmd_login_check(SimpleNamespace(config=str(config_path))) == 0
    assert "safe-sync connect-dropbox --headless --reconnect" in capsys.readouterr().out


def test_connect_dropbox_headless_skips_rclone_menu(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    config = normalized_config(default_config("test-machine"))
    config["rclone_bin"] = "rclone"
    write_config(config_path, config)
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[1] == "listremotes":
            return subprocess.CompletedProcess(command, 0, "")
        return subprocess.CompletedProcess(command, 0, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("builtins.input", lambda _prompt: '{"access_token":"test"}')

    assert cmd_connect_dropbox(SimpleNamespace(config=str(config_path), headless=True)) == 0
    assert commands[-1] == ["rclone", "config", "create", "dropbox", "dropbox", "config_is_local", "false", "token", '{"access_token":"test"}']


def test_connect_dropbox_headless_replaces_an_existing_token(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    config = normalized_config(default_config("test-machine"))
    config["rclone_bin"] = "rclone"
    write_config(config_path, config)
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        output = "dropbox:\n" if command[1] == "listremotes" else ""
        return subprocess.CompletedProcess(command, 0, output)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("builtins.input", lambda _prompt: '{"access_token":"replacement"}')

    assert cmd_connect_dropbox(SimpleNamespace(config=str(config_path), headless=True, reconnect=True)) == 0
    assert commands[-1] == ["rclone", "config", "update", "dropbox", "config_is_local", "false", "token", '{"access_token":"replacement"}']


def test_preflight_requests_reconnect_for_revoked_dropbox_token(monkeypatch, tmp_path):
    config = {
        "rclone_bin": "rclone",
        "remote_root": "dropbox:computer-backups/test",
        "rate_limit_backoff_seconds": 300,
        "log_dir": str(tmp_path),
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "invalid_access_token: token has expired"),
    )

    with pytest.raises(SystemExit, match="safe-sync connect-dropbox"):
        preflight(config)


def test_setup_requires_explicit_opt_in_for_projects_root(monkeypatch, tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    config = normalized_config(default_config("test-machine"))
    monkeypatch.setattr("safe_sync.cli.unsafe_local_path_reason", lambda _path: "refusing unsafe local_path")

    with pytest.raises(SystemExit, match="safe-sync setup --folder"):
        add_setup_folder(config, str(projects))

    assert add_setup_folder(config, str(projects), allow_unsafe_local_path=True) == "projects"
    assert config["folders"][0]["allow_unsafe_local_path"] is True


def test_backup_preflight_rate_limit_sets_backoff_warning(monkeypatch, tmp_path):
    local = tmp_path / "local"
    local.mkdir()
    (tmp_path / "filter.txt").write_text("")
    config = {
        "rclone_bin": "rclone",
        "local_path": str(local),
        "remote_root": "dropbox:computer-backups/test/mac/test_sync",
        "trash_root": "dropbox:computer-backups/test/.trash/mac/test_sync",
        "filter_file": str(tmp_path / "filter.txt"),
        "status_path": str(tmp_path / "status.json"),
        "log_dir": str(tmp_path),
        "lock_file": str(tmp_path / "safe-sync.lock"),
        "rate_limit_backoff_seconds": 120,
    }

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(_args[0], 1, "Too many requests or write operations. Trying again in 300 seconds.")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert run_backup_with_config(config, dry_run=False) == RATE_LIMIT_EXIT
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["state"] == "backoff"
    assert status["last_error"] is None
    assert "rate limited" in status["last_warning"]
    assert status["backoff_seconds"] == 300
    assert status["queued_backup"] is True


def test_backup_success_with_rate_limit_notice_still_cools_down(monkeypatch, tmp_path):
    local = tmp_path / "local"
    local.mkdir()
    (tmp_path / "filter.txt").write_text("")
    config = {
        "rclone_bin": "rclone",
        "local_path": str(local),
        "remote_root": "dropbox:computer-backups/test/mac/test_sync",
        "trash_root": "dropbox:computer-backups/test/.trash/mac/test_sync",
        "filter_file": str(tmp_path / "filter.txt"),
        "status_path": str(tmp_path / "status.json"),
        "log_dir": str(tmp_path),
        "lock_file": str(tmp_path / "safe-sync.lock"),
        "rate_limit_backoff_seconds": 120,
    }
    calls = iter([
        subprocess.CompletedProcess(["rclone", "about"], 0, "ok"),
        subprocess.CompletedProcess(["rclone", "sync"], 0, "Too many requests or write operations. Trying again in 300 seconds."),
    ])

    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: next(calls))

    assert run_backup_with_config(config, dry_run=False) == RATE_LIMIT_EXIT
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["state"] == "backoff"
    assert status["last_error"] is None
    assert status["queued_backup"] is False
    assert status["backoff_seconds"] == 300


def test_backup_during_active_backoff_queues_without_rclone(monkeypatch, tmp_path):
    local = tmp_path / "local"
    local.mkdir()
    (tmp_path / "filter.txt").write_text("")
    status_path = tmp_path / "status.json"
    status_path.write_text('{"state":"backoff","backoff_until":"2099-01-01T00:00:00+00:00"}')
    config = {
        "rclone_bin": "rclone",
        "local_path": str(local),
        "remote_root": "dropbox:computer-backups/test/mac/test_sync",
        "trash_root": "dropbox:computer-backups/test/.trash/mac/test_sync",
        "filter_file": str(tmp_path / "filter.txt"),
        "status_path": str(status_path),
        "log_dir": str(tmp_path),
        "lock_file": str(tmp_path / "safe-sync.lock"),
    }

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("rclone should not be called during active backoff")

    monkeypatch.setattr(subprocess, "run", fail_if_called)

    assert run_backup_with_config(config, dry_run=False) == RATE_LIMIT_EXIT
    status = json.loads(status_path.read_text())
    assert status["state"] == "backoff"
    assert status["queued_backup"] is True
    assert "queued" in status["last_warning"]


def test_watch_filter_ignores_generated_paths():
    assert should_ignore_watch_event("/tmp/project/node_modules/pkg/index.js")
    assert should_ignore_watch_event("/tmp/project/.venv/lib/site.py")
    assert should_ignore_watch_event("/tmp/project/dist/app.js")
    assert not should_ignore_watch_event("/tmp/project/data/results.csv")
    assert not should_ignore_watch_event("/tmp/project/models/model.pt")


def test_transfer_commands_do_not_set_a_whole_upload_deadline(tmp_path):
    config = {
        "local_path": str(tmp_path),
        "remote_root": "dropbox:computer-backups/test/machine/folder",
        "trash_root": "dropbox:computer-backups/test/.trash/machine/folder",
        "filter_file": str(tmp_path / "filter.txt"),
    }

    assert "--max-duration" not in backup_cmd(config, dry_run=False)
    assert "--max-duration" not in copy_cmd(config, "dropbox:source", str(tmp_path), dry_run=False)


def test_copy_command_limits_a_transfer_to_selected_files_and_folders(tmp_path):
    command = copy_cmd(
        {"filter_file": str(tmp_path / "filter.txt")},
        "dropbox:source",
        str(tmp_path),
        dry_run=True,
        selected_paths=["report.csv", "assets/"],
    )

    assert command.count("--include") == 2
    assert "/report.csv" in command
    assert "/assets/**" in command
    assert "--create-empty-src-dirs" in command
    assert "--dry-run" in command


def test_bounded_seconds_validates_settings_limits():
    assert bounded_seconds("poll interval", 5, 1, 3600) == 5
    with pytest.raises(SystemExit, match="poll interval must be between 1 and 3600 seconds"):
        bounded_seconds("poll interval", 0, 1, 3600)


def test_lock_recovers_when_a_reused_pid_belongs_to_another_app(monkeypatch, tmp_path):
    path = tmp_path / "safe-sync.lock"
    path.write_text("50666")

    def fake_ps(*args, **_kwargs):
        assert args[0][:3] == ["ps", "-p", "50666"]
        return subprocess.CompletedProcess(args[0], 0, "/Applications/Dropbox.app/crashpad-handler\n")

    monkeypatch.setattr(subprocess, "run", fake_ps)

    with Lock(path):
        assert path.read_text() == str(os.getpid())

    assert not path.exists()


def test_lock_refuses_a_live_safe_sync_owner(monkeypatch, tmp_path):
    path = tmp_path / "safe-sync.lock"
    path.write_text("12345")

    def fake_ps(*args, **_kwargs):
        return subprocess.CompletedProcess(args[0], 0, "python3 /home/user/bin/safe-sync daemon\n")

    monkeypatch.setattr(subprocess, "run", fake_ps)

    with pytest.raises(SystemExit, match="Safe Sync already running"):
        with Lock(path):
            pass


def test_daemon_holds_the_global_lock_for_its_full_lifetime(monkeypatch, tmp_path):
    import safe_sync.cli as cli

    captured = {}

    class CapturingLock:
        def __init__(self, path):
            captured["path"] = path

        def __enter__(self):
            captured["entered"] = True
            return self

        def __exit__(self, *_args):
            captured["exited"] = True

    monkeypatch.setattr(cli, "Lock", CapturingLock)
    monkeypatch.setattr(cli, "load_config", lambda _path: {"lock_file": str(tmp_path / "daemon.lock")})
    monkeypatch.setattr(cli, "normalized_config", lambda config: config)
    monkeypatch.setattr(cli, "run_daemon", lambda _args, _path, _config: 0)

    assert cmd_daemon(SimpleNamespace(config=str(tmp_path / "config.json"))) == 0
    assert captured["entered"] is True
    assert captured["exited"] is True


def test_daemon_state_queues_one_transfer_at_a_time():
    state = DaemonApiState()

    assert state.request_pull("dropbox:source", "/tmp/destination", False)
    assert not state.request_pull("dropbox:other", "/tmp/other", False)
    assert state.consume_pull_request() == {
        "source": "dropbox:source",
        "destination": "/tmp/destination",
        "dry_run": False,
        "selected_paths": [],
    }
    assert state.consume_pull_request() is None


def test_daemon_state_marks_a_queued_transfer_in_live_status():
    state = DaemonApiState()

    assert state.request_pull("dropbox:source", "/tmp/destination", False)
    assert state.snapshot()["queued_transfer"] is True
    state.consume_pull_request()
    assert state.snapshot()["queued_transfer"] is False


def test_transfer_runner_does_not_set_a_whole_process_timeout(monkeypatch, tmp_path):
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args[0], 0, "ok")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert run_command({"log_dir": str(tmp_path)}, ["rclone", "sync"], dry_run=False) == 0
    assert "timeout" not in captured


def test_temporary_config_change_does_not_restart_installed_backend(monkeypatch, tmp_path):
    monkeypatch.setattr("safe_sync.cli.os_name", lambda: "Darwin")

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("temporary config must not touch launchctl")

    monkeypatch.setattr(subprocess, "run", fail_if_called)

    restart_backend_if_running(tmp_path / "dogfood-config.json")



def test_scan_tree_detects_useful_changes(tmp_path):
    useful = tmp_path / "data"
    ignored = tmp_path / "node_modules" / "pkg"
    useful.mkdir()
    ignored.mkdir(parents=True)

    (useful / "results.csv").write_text("a,b\n1,2\n")
    (ignored / "index.js").write_text("ignored")

    snapshot = scan_tree(Path(tmp_path))

    assert "data/results.csv" in snapshot
    assert "data/" in snapshot
    assert "node_modules/pkg/index.js" not in snapshot


def test_scan_tree_detects_empty_directory_changes(tmp_path):
    snapshot_before = scan_tree(Path(tmp_path))
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    snapshot_after_create = scan_tree(Path(tmp_path))
    assert "empty/" in snapshot_after_create
    assert snapshot_before != snapshot_after_create

    empty_dir.rmdir()
    snapshot_after_remove = scan_tree(Path(tmp_path))
    assert "empty/" not in snapshot_after_remove
    assert snapshot_after_remove == snapshot_before


def test_scan_tree_does_not_ignore_root_named_like_build_artifact(tmp_path):
    watched_root = tmp_path / "dist"
    watched_root.mkdir()
    new_dir = watched_root / "nested"
    new_dir.mkdir()
    (new_dir / "hello.txt").write_text("hi")

    snapshot = scan_tree(watched_root)

    assert "nested/" in snapshot
    assert "nested/hello.txt" in snapshot


def test_scan_tree_detects_file_modifications_and_deletions(tmp_path):
    file_path = tmp_path / "data.txt"
    file_path.write_text("v1")
    snapshot_before = scan_tree(Path(tmp_path))

    file_path.write_text("v2 changed")
    snapshot_after_modify = scan_tree(Path(tmp_path))
    assert snapshot_after_modify["data.txt"] != snapshot_before["data.txt"]

    file_path.unlink()
    snapshot_after_delete = scan_tree(Path(tmp_path))
    assert "data.txt" not in snapshot_after_delete


def test_unsafe_local_path_guard_blocks_broad_paths():
    home = Path.home().resolve()

    assert unsafe_local_path_reason(Path("/").resolve())
    assert unsafe_local_path_reason(home)
    assert unsafe_local_path_reason(home / "projects")
    assert not unsafe_local_path_reason(home / "test_sync")


def test_normalized_config_migrates_legacy_single_folder():
    config = normalized_config({
        "machine": "macbook",
        "local_path": "~/test_sync",
        "remote_root": "dropbox:computer-backups/test/macbook/test_sync",
        "trash_root": "dropbox:computer-backups/test/.trash/macbook/test_sync",
    })

    assert config["machine_id"] == "macbook"
    assert config["remote_base"] == "dropbox:computer-backups/test"
    assert config["folders"][0]["id"] == "test_sync"
    assert config["folders"][0]["remote_root"] == "dropbox:computer-backups/test/macbook/test_sync"
    assert config["folders"][0]["trash_root"] == "dropbox:computer-backups/test/.trash/macbook/test_sync"


def test_selected_folders_respects_enabled_state():
    config = normalized_config({
        "machine_id": "workstation",
        "remote_base": "dropbox:computer-backups/test",
        "folders": [
            {"id": "projects", "local_path": "~/test_sync"},
            {"id": "data", "local_path": "~/data", "enabled": False},
        ],
    })

    assert [folder["id"] for folder in enabled_folders(config)] == ["projects"]
    assert [folder["id"] for folder in selected_folders(config, None)] == ["projects"]
    assert selected_folders(config, "projects")[0]["remote_root"] == "dropbox:computer-backups/test/workstation/projects"


def test_folder_config_uses_config_filter_default():
    config = normalized_config({
        "machine_id": "workstation",
        "remote_base": "dropbox:computer-backups/test",
        "filter_file": "/tmp/custom-filter.txt",
        "folders": [
            {"id": "projects", "local_path": "~/test_sync"},
        ],
    })

    assert config["folders"][0]["filter_file"] == "/tmp/custom-filter.txt"

def test_registry_doc_lists_machine_owned_folders():
    config = normalized_config({
        "machine_id": "linuxbox",
        "machine_label": "Linux Box",
        "install_id": "install-123",
        "remote_base": "dropbox:computer-backups/test",
        "folders": [
            {"id": "projects", "local_path": "~/projects-safe"},
            {"id": "data", "local_path": "~/data-safe", "enabled": False},
        ],
    })

    doc = registry_doc(config)

    assert registry_path(config) == "dropbox:computer-backups/test/.registry/computers/linuxbox.json"
    assert doc["machine_id"] == "linuxbox"
    assert doc["machine_label"] == "Linux Box"
    assert doc["install_id"] == "install-123"
    assert [folder["id"] for folder in doc["folders"]] == ["projects", "data"]
    assert doc["folders"][0]["remote_path"] == "linuxbox/projects"
    assert doc["folders"][1]["enabled"] is False


def test_normalized_config_flattens_active_profile():
    config = normalized_config({
        "active_profile_id": "linux-box",
        "profiles": [
            {
                "id": "macbook",
                "machine_id": "macbook",
                "machine_label": "MacBook",
                "remote_base": "dropbox:computer-backups/test",
                "folders": [{"id": "projects", "local_path": "~/mac-projects"}],
            },
            {
                "id": "linux-box",
                "machine_id": "linux-box",
                "machine_label": "Linux Box",
                "remote_base": "dropbox:computer-backups/test",
                "folders": [{"id": "results", "local_path": "~/linux-results"}],
            },
        ],
    })

    assert config["profile_id"] == "linux-box"
    assert config["machine_id"] == "linux-box"
    assert config["machine_label"] == "Linux Box"
    assert [folder["id"] for folder in config["folders"]] == ["results"]
    assert config["folders"][0]["remote_root"] == "dropbox:computer-backups/test/linux-box/results"


def test_config_view_reports_profiles_and_active_profile():
    view = config_view(normalized_config({
        "active_profile_id": "macbook",
        "profiles": [
            {
                "id": "macbook",
                "label": "Personal Mac",
                "machine_id": "macbook",
                "machine_label": "MacBook",
                "remote_base": "dropbox:computer-backups/test",
                "folders": [{"id": "projects", "local_path": "~/projects"}],
            },
            {
                "id": "linux-box",
                "label": "Linux Runner",
                "machine_id": "linux-box",
                "machine_label": "Linux Box",
                "remote_base": "dropbox:computer-backups/test",
                "folders": [],
            },
        ],
    }))

    assert view["active_profile_id"] == "macbook"
    assert view["profile_label"] == "Personal Mac"
    assert len(view["profiles"]) == 2
    assert view["profiles"][0]["active"] is True
    assert view["profiles"][1]["active"] is False


def test_ensure_local_profiles_registered_creates_missing_entries(monkeypatch):
    config = normalized_config({
        "active_profile_id": "macbook2",
        "remote_base": "dropbox:computer-backups/test",
        "profiles": [
            {
                "id": "macbook",
                "machine_id": "macbook",
                "machine_label": "macbook",
                "remote_base": "dropbox:computer-backups/test",
                "folders": [],
            },
            {
                "id": "macbook2",
                "machine_id": "macbook2",
                "machine_label": "macbook2",
                "remote_base": "dropbox:computer-backups/test",
                "folders": [],
            },
        ],
    })

    calls: list[list[str]] = []

    def fake_rclone_capture(_config, cmd, input_text=None):
        calls.append(cmd)
        if cmd[:2] == ["lsf", "dropbox:computer-backups/test/.registry/computers"]:
            return subprocess.CompletedProcess(cmd, 0, "macbook.json\n")
        if cmd[:2] == ["rcat", "dropbox:computer-backups/test/.registry/computers/macbook2.json"]:
            return subprocess.CompletedProcess(cmd, 0, input_text or "")
        raise AssertionError(f"unexpected rclone call: {cmd}")

    monkeypatch.setattr("safe_sync.cli.rclone_capture", fake_rclone_capture)

    created = ensure_local_profiles_registered(config)

    assert created == ["macbook2"]
    assert ["lsf", "dropbox:computer-backups/test/.registry/computers", "--files-only"] in calls
    assert ["rcat", "dropbox:computer-backups/test/.registry/computers/macbook2.json"] in calls


def test_restore_last_sync_finish_rehydrates_fallback_clock(tmp_path):
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps({"last_finish": "2026-07-14T10:00:00-04:00"}))
    daemon = WatchDaemon(WatchSettings(fallback_interval_seconds=1800))
    config = {"status_path": str(status_path)}

    from safe_sync import cli as cli_module

    class FakeDateTime(cli_module.dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromisoformat("2026-07-14T11:00:00-04:00")

    original_datetime = cli_module.dt.datetime
    cli_module.dt.datetime = FakeDateTime
    try:
        restore_last_sync_finish(daemon, config, 1000.0)
    finally:
        cli_module.dt.datetime = original_datetime

    assert daemon.state.last_sync_finish_monotonic == 1000.0 - 3600.0
    assert daemon.should_run_fallback(1000.0)


def test_folder_snapshots_tracks_multiple_roots(tmp_path):
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    (one / "a.txt").write_text("a")
    (two / "b.txt").write_text("b")
    (two / "node_modules").mkdir()
    (two / "node_modules" / "ignored.js").write_text("ignored")

    snapshots = folder_snapshots({
        "machine_id": "test",
        "remote_base": "dropbox:computer-backups/test",
        "folders": [
            {"id": "one", "local_path": str(one)},
            {"id": "two", "local_path": str(two)},
        ],
    })

    assert "a.txt" in snapshots["one"]
    assert "b.txt" in snapshots["two"]
    assert "node_modules/ignored.js" not in snapshots["two"]


def test_backup_cmd_metadata_is_opt_in():
    from safe_sync.cli import backup_cmd

    base = {
        "rclone_bin": "rclone",
        "local_path": "~/test_sync",
        "remote_root": "dropbox:computer-backups/test/mac/test_sync",
        "trash_root": "dropbox:computer-backups/test/.trash/mac/test_sync",
        "filter_file": "/tmp/filter.txt",
    }

    assert "--metadata" not in backup_cmd(base, dry_run=True)
    assert "--create-empty-src-dirs" in backup_cmd(base, dry_run=True)
    assert "--metadata" in backup_cmd({**base, "preserve_metadata": True}, dry_run=True)


def test_rclone_bin_uses_common_homebrew_fallback(monkeypatch):
    from safe_sync.cli import rclone_bin

    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr(Path, "exists", lambda self: str(self) == "/opt/homebrew/bin/rclone")

    assert rclone_bin({}) == "/opt/homebrew/bin/rclone"


def test_rclone_env_uses_dedicated_config_without_touching_global_config(tmp_path):
    dedicated = tmp_path / "safe-sync-rclone.conf"

    assert rclone_env({"rclone_config": str(dedicated)}) == {
        **os.environ,
        "RCLONE_CONFIG": str(dedicated),
    }
    assert rclone_env({}) is None


def test_default_config_owns_a_dedicated_rclone_config():
    config = default_config("test-machine")

    assert config["rclone_config"].endswith(".safe-sync/rclone.conf")


def test_write_config_preserves_managed_rclone_paths(tmp_path):
    path = tmp_path / "config.json"
    config = {
        "machine_id": "test-machine",
        "remote_base": "dropbox:computer-backups",
        "folders": [],
        "rclone_bin": "/tmp/managed-rclone",
        "rclone_config": str(tmp_path / "rclone.conf"),
    }

    write_config(path, config)

    persisted = json.loads(path.read_text())
    assert persisted["rclone_bin"] == "/tmp/managed-rclone"
    assert persisted["rclone_config"] == str(tmp_path / "rclone.conf")


def test_backend_autostart_status_mac_not_installed(monkeypatch, tmp_path):
    monkeypatch.setattr("safe_sync.service.launchd_plist_path", lambda: tmp_path / "missing.plist")

    assert backend_autostart_status_text("Darwin") == "backend autostart: not installed"


def test_backend_autostart_status_mac_enabled(monkeypatch, tmp_path):
    plist = tmp_path / "com.safe-sync.daemon.plist"
    plist.write_text("plist")
    monkeypatch.setattr("safe_sync.service.launchd_plist_path", lambda: plist)
    monkeypatch.setattr("safe_sync.service.launchd_disabled", lambda: False)
    monkeypatch.setattr("safe_sync.service.service_status_text", lambda: "service: running")

    assert backend_autostart_status_text("Darwin") == "backend autostart: enabled (running)"


def test_backend_autostart_status_mac_disabled(monkeypatch, tmp_path):
    plist = tmp_path / "com.safe-sync.daemon.plist"
    plist.write_text("plist")
    monkeypatch.setattr("safe_sync.service.launchd_plist_path", lambda: plist)
    monkeypatch.setattr("safe_sync.service.launchd_disabled", lambda: True)
    monkeypatch.setattr("safe_sync.service.service_status_text", lambda: "service: stopped")

    assert backend_autostart_status_text("Darwin") == "backend autostart: disabled (stopped)"


def test_backend_autostart_mac_commands(monkeypatch, tmp_path):
    plist = tmp_path / "com.safe-sync.daemon.plist"
    plist.write_text("plist")
    monkeypatch.setattr("safe_sync.service.launchd_plist_path", lambda: plist)
    monkeypatch.setattr("safe_sync.service.launchd_service_target", lambda: "gui/501/com.safe-sync.daemon")

    assert backend_autostart_cmd("enable", "Darwin") == ["launchctl", "enable", "gui/501/com.safe-sync.daemon"]
    assert backend_autostart_cmd("disable", "Darwin") == ["launchctl", "disable", "gui/501/com.safe-sync.daemon"]


def test_backend_autostart_linux_commands(monkeypatch, tmp_path):
    unit = tmp_path / "safe-sync-daemon.service"
    unit.write_text("unit")
    monkeypatch.setattr("safe_sync.service.systemd_unit_path", lambda: unit)

    assert backend_autostart_cmd("enable", "Linux") == ["systemctl", "--user", "enable", "safe-sync-daemon.service"]
    assert backend_autostart_cmd("disable", "Linux") == ["systemctl", "--user", "disable", "safe-sync-daemon.service"]


def test_linux_service_status_and_unit(monkeypatch, tmp_path):
    monkeypatch.setattr("safe_sync.service.os_name", lambda: "Linux")
    monkeypatch.setattr("safe_sync.service.systemd_service_active", lambda: True)

    assert service_status_text() == "service: running"
    rendered = systemd_unit(tmp_path / "config.json", tmp_path / "safe-sync")
    assert "ExecStart=" in rendered
    assert "Restart=always" in rendered
    assert "WantedBy=default.target" in rendered


def test_backend_autostart_windows_is_todo():
    import pytest

    with pytest.raises(SystemExit) as exc:
        backend_autostart_cmd("enable", "Windows")

    assert "TODO on Windows" in str(exc.value)
    assert "unsupported OS Windows" in backend_autostart_status_text("Windows")


def test_parser_accepts_autostart_backend_status():
    from safe_sync.cli import cmd_autostart, parser

    args = parser().parse_args(["autostart", "backend", "status"])

    assert args.func is cmd_autostart
    assert args.autostart_target == "backend"
    assert args.autostart_action == "status"
