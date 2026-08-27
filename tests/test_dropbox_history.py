import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from safe_sync import cli
from safe_sync.api import DaemonApiState
from safe_sync.dropbox_history import (
    DropboxHistoryError,
    credentials_from_rclone,
    download_revision,
    dropbox_path,
    list_folder_snapshot,
    list_revisions,
)
from safe_sync.transfer import JobStore, dropbox_content_hash


class FakeResponse:
    def __init__(self, body: bytes, headers=None):
        self._body = io.BytesIO(body)
        self.headers = headers or {}

    def read(self, size=-1):
        return self._body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_dropbox_path_is_scoped_and_rejects_escape():
    assert dropbox_path("dropbox:computer-backups/mac/temp", "nested/file.txt") == "/computer-backups/mac/temp/nested/file.txt"
    with pytest.raises(DropboxHistoryError):
        dropbox_path("dropbox:computer-backups/mac/temp", "../secret")


def test_credentials_refresh_and_extract_without_returning_refresh_secret():
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        if command[1] == "about":
            return subprocess.CompletedProcess(command, 0, "{}")
        value = {
            "dropbox": {
                "type": "dropbox",
                "token": json.dumps({"access_token": "short-lived", "refresh_token": "never-return-this"}),
            }
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(value))

    credentials = credentials_from_rclone("rclone", None, "dropbox:path", runner=runner)
    assert credentials == {"access_token": "short-lived"}
    assert calls == [["rclone", "about", "dropbox:", "--json"], ["rclone", "config", "dump"]]


def test_revision_listing_and_download_use_revision_identity(tmp_path):
    listed_request = None

    def list_opener(request, **_kwargs):
        nonlocal listed_request
        listed_request = request
        return FakeResponse(
            json.dumps(
                {
                    "is_deleted": True,
                    "entries": [
                        {
                            "rev": "abc123",
                            "name": "file.txt",
                            "path_display": "/backup/file.txt",
                            "server_modified": "2026-08-17T12:00:00Z",
                            "size": 3,
                            "content_hash": "hash",
                            "is_downloadable": True,
                            "is_restorable": True,
                        }
                    ],
                }
            ).encode()
        )

    value = list_revisions({"access_token": "secret"}, "/backup/file.txt", opener=list_opener)
    assert value["is_deleted"] is True
    assert value["entries"][0]["rev"] == "abc123"
    assert json.loads(listed_request.data)["include_restorable_info"] is True
    assert listed_request.get_header("Authorization") == "Bearer secret"

    downloaded_request = None

    def download_opener(request, **_kwargs):
        nonlocal downloaded_request
        downloaded_request = request
        metadata = json.dumps({"rev": "abc123", "content_hash": "hash"})
        return FakeResponse(b"old", {"Dropbox-API-Result": metadata})

    destination = tmp_path / "file.txt"
    metadata = download_revision({"access_token": "secret"}, "abc123", destination, opener=download_opener)
    assert destination.read_bytes() == b"old"
    assert metadata["rev"] == "abc123"
    assert json.loads(downloaded_request.get_header("Dropbox-api-arg"))["path"] == "rev:abc123"


def test_folder_snapshot_paginates_and_keeps_revision_identity():
    requests = []

    def opener(request, **_kwargs):
        requests.append(request)
        if request.full_url.endswith("/files/list_folder"):
            return FakeResponse(
                json.dumps(
                    {
                        "entries": [{".tag": "folder", "path_display": "/backup/sub"}],
                        "cursor": "cursor-1",
                        "has_more": True,
                    }
                ).encode()
            )
        return FakeResponse(
            json.dumps(
                {
                    "entries": [
                        {
                            ".tag": "file",
                            "path_display": "/backup/sub/file.txt",
                            "id": "id:file",
                            "rev": "rev-file",
                            "size": 3,
                            "server_modified": "2026-08-17T12:00:00Z",
                            "content_hash": "hash",
                        }
                    ],
                    "cursor": "cursor-2",
                    "has_more": False,
                }
            ).encode()
        )

    value = list_folder_snapshot({"access_token": "secret"}, "/backup", opener=opener)
    assert value["cursor"] == "cursor-2"
    assert value["entries"]["sub"]["type"] == "directory"
    assert value["entries"]["sub/file.txt"]["revision"] == "rev-file"
    assert json.loads(requests[1].data) == {"cursor": "cursor-1"}


def test_no_app_trash_and_recovery_job_replace_then_rollback(monkeypatch, tmp_path):
    local = tmp_path / "watched"
    local.mkdir()
    target = local / "nested" / "file.txt"
    target.parent.mkdir()
    target.write_text("current\n")
    state = tmp_path / "state"
    config = cli.normalized_config(
        {
            "active_profile_id": "mac",
            "state_root": str(state),
            "status_path": str(state / "status.json"),
            "socket_path": str(state / "daemon.sock"),
            "lock_file": str(state / "lock"),
            "remote_base": "dropbox:computer-backups",
            "profiles": [
                {
                    "id": "mac",
                    "machine_id": "mac",
                    "install_id": "install",
                    "remote_base": "dropbox:computer-backups",
                    "folders": [
                        {
                            "id": "watched",
                            "local_path": str(local),
                            "remote_path": "mac/watched",
                            "filter_file": str(tmp_path / "filter.txt"),
                        }
                    ],
                }
            ],
        }
    )
    (tmp_path / "filter.txt").write_text("")
    folder_config = cli.folder_config(config, config["folders"][0])
    command = cli.backup_cmd(folder_config, False)
    assert "--backup-dir" not in command
    assert not any(".trash" in part for part in command)
    assert "--track-renames" in command
    assert command[command.index("--track-renames-strategy") + 1] == "hash"

    prior = tmp_path / "prior.txt"
    prior.write_text("prior\n")
    prior_hash = dropbox_content_hash(prior)
    revision_metadata = {
        "rev": "rev123",
        "name": "file.txt",
        "path_display": "/computer-backups/mac/watched/nested/file.txt",
        "server_modified": "2026-08-17T12:00:00Z",
        "size": prior.stat().st_size,
        "content_hash": prior_hash,
        "is_downloadable": True,
        "is_restorable": True,
    }
    monkeypatch.setattr(cli, "dropbox_history_credentials", lambda *_args: {"access_token": "test"})
    monkeypatch.setattr(
        cli,
        "list_revisions",
        lambda *_args, **_kwargs: {"path": revision_metadata["path_display"], "is_deleted": False, "entries": [revision_metadata]},
    )

    def fake_download(_credentials, revision, destination):
        assert revision == "rev123"
        destination.write_bytes(prior.read_bytes())
        return {"rev": revision, "content_hash": prior_hash}

    monkeypatch.setattr(cli, "download_revision", fake_download)
    cli.set_recovery_paused(config, True)
    job = cli.create_recovery_job(config, "watched", "nested/file.txt", "rev123")
    assert job["status"] == "ready"
    assert job["source_kind"] == "dropbox_revision"
    assert job["recovery_compare"]["kind"] == "text"
    assert "-current" in job["recovery_compare"]["unified_diff"]
    assert "+prior" in job["recovery_compare"]["unified_diff"]

    store = JobStore(state)
    # An unrelated edit in a large watched tree must not force this one-file
    # recovery to rescan or reject the whole destination.
    (local / "unrelated.txt").write_text("new unrelated work\n")
    applied = store.apply(job["id"], {"nested/file.txt": "replace"})
    assert applied["status"] == "complete"
    assert target.read_text() == "prior\n"
    rolled_back = store.rollback(job["id"])
    assert rolled_back["status"] == "rolled_back"
    assert target.read_text() == "current\n"
    assert cli.recovery_is_paused(config) is True


def test_recovery_cli_parser():
    commands = [
        ["status"],
        ["enter", "watched"],
        ["clear-legacy", "--confirm", "CLEAR-OLD-PAUSE"],
        ["save-remote-copy"],
        ["cancel", "--confirm", "REPLACE-DROPBOX-WITH-LOCAL"],
        ["mark-rewound"],
        ["export"],
        ["mark-undo-complete"],
        ["verify"],
        ["exit"],
        ["force-exit", "--confirm", "FORCE-UNLOCK-RECOVERY"],
    ]
    for command in commands:
        parsed = cli.parser().parse_args(["recovery", *command])
        assert parsed.func is cli.cmd_recovery
        assert parsed.recovery_cmd == command[0]


def test_clear_legacy_recovery_pause_is_narrow_and_restores_on_notify_failure(monkeypatch, tmp_path):
    config = cli.normalized_config({"state_root": str(tmp_path / "state")})
    cli.set_recovery_paused(config, True)
    legacy_path = cli.recovery_pause_path(config)
    legacy_contents = legacy_path.read_text()

    monkeypatch.setattr(cli, "_notify_recovery_mode", lambda _config, _active: {"ok": False, "error": "daemon refused"})
    with pytest.raises(cli.TransferError, match="daemon refused"):
        cli.clear_legacy_recovery_pause(config)
    assert legacy_path.read_text() == legacy_contents
    assert cli.recovery_is_paused(config) is True

    monkeypatch.setattr(cli, "_notify_recovery_mode", lambda _config, _active: {"ok": True, "daemon_running": True})
    status = cli.clear_legacy_recovery_pause(config)
    assert status["active"] is False
    assert not legacy_path.exists()


def test_clear_legacy_recovery_pause_refuses_new_recovery_state(tmp_path):
    config = cli.normalized_config({"state_root": str(tmp_path / "state")})
    cli.set_recovery_paused(config, True)
    cli.atomic_write_text(cli.recovery_mode_path(config), '{"active": true, "phase": "locked"}\n')
    with pytest.raises(cli.TransferError, match="guided or damaged"):
        cli.clear_legacy_recovery_pause(config)
    assert cli.recovery_pause_path(config).exists()


def _guided_recovery_config(tmp_path):
    local = tmp_path / "watched"
    local.mkdir()
    filter_path = tmp_path / "filter.txt"
    filter_path.write_text("")
    config = cli.normalized_config(
        {
            "active_profile_id": "mac",
            "state_root": str(tmp_path / "state"),
            "status_path": str(tmp_path / "state" / "status.json"),
            "socket_path": str(tmp_path / "state" / "daemon.sock"),
            "filter_file": str(filter_path),
            "rclone_bin": "rclone",
            "profiles": [
                {
                    "id": "mac",
                    "machine_id": "mac",
                    "install_id": "install",
                    "remote_base": "dropbox:backups",
                    "filter_file": str(filter_path),
                    "folders": [
                        {
                            "id": "watched",
                            "local_path": str(local),
                            "remote_root": "dropbox:backups/mac/watched",
                            "filter_file": str(filter_path),
                        }
                    ],
                }
            ],
        }
    )
    cli.enter_recovery_mode(config, "watched", str(tmp_path / "restore"))
    return config


def test_cancel_recovery_unlocks_without_write_when_remote_matches(monkeypatch, tmp_path):
    config = _guided_recovery_config(tmp_path)
    verification = {"equal": True, "remote_stable": True, "counts": {}}
    monkeypatch.setattr(cli, "_live_recovery_equality", lambda *_args: (True, verification))
    monkeypatch.setattr(cli, "_notify_recovery_mode", lambda *_args: {"ok": True})
    monkeypatch.setattr(cli, "_run_command_unlocked", lambda *_args, **_kwargs: pytest.fail("matching cancel must not write Dropbox"))

    status = cli.cancel_recovery_mode(config)

    assert status["active"] is False
    assert status["cancelled"] is True
    assert status["remote_reconciled"] is False
    assert not cli.recovery_mode_path(config).exists()


def test_save_remote_copy_before_cancel_isolated_verified_and_keeps_lock(monkeypatch, tmp_path):
    config = _guided_recovery_config(tmp_path)
    inventory = {
        "nested": {"type": "directory", "size": 0},
        "nested/remote.txt": {"type": "file", "size": 6, "hashes": {"dropbox": "abc"}},
    }
    commands = []

    monkeypatch.setattr(cli, "_filtered_remote_inventory", lambda *_args: inventory)
    monkeypatch.setattr(cli, "local_inventory", lambda *_args, **_kwargs: inventory)
    monkeypatch.setattr(cli, "_run_command_unlocked", lambda _config, command, *_args, **_kwargs: commands.append(command) or 0)

    status = cli.save_remote_copy_before_cancel(config)

    remote_copy = status["cancel_remote_copy"]
    destination = Path(remote_copy["destination"])
    assert status["active"] is True
    assert status["phase"] == "locked"
    assert remote_copy["status"] == "verified"
    assert remote_copy["entry_count"] == 2
    assert destination.is_dir()
    assert destination != tmp_path / "watched"
    assert (tmp_path / "watched") not in destination.parents
    assert commands[0][1:4] == ["sync", "dropbox:backups/mac/watched", str(destination)]
    assert "--ignore-times" in commands[0]
    downloads = cli.recovery_downloads(config)
    assert len(downloads) == 1
    assert downloads[0]["kind"] == "dropbox_safety_copy"
    assert downloads[0]["folder_id"] == "watched"
    assert downloads[0]["destination"] == str(destination)
    assert downloads[0]["entry_count"] == 2
    assert downloads[0]["byte_count"] == 6
    assert downloads[0]["available"] is True

    monkeypatch.setattr(cli, "_live_recovery_equality", lambda *_args: (True, {"equal": True, "remote_stable": True, "counts": {}}))
    monkeypatch.setattr(cli, "_notify_recovery_mode", lambda *_args: {"ok": True})
    cancelled = cli.cancel_recovery_mode(config)
    assert cancelled["active"] is False
    assert cancelled["cancel_remote_copy"]["status"] == "verified"
    assert destination.is_dir()
    assert cli.recovery_downloads(config)[0]["destination"] == str(destination)
    destination.rmdir()
    assert cli.recovery_downloads(config)[0]["available"] is False


def test_save_remote_copy_failure_keeps_recovery_locked_and_retryable(monkeypatch, tmp_path):
    config = _guided_recovery_config(tmp_path)
    monkeypatch.setattr(cli, "_filtered_remote_inventory", lambda *_args: {})
    monkeypatch.setattr(cli, "_run_command_unlocked", lambda *_args, **_kwargs: 5)

    with pytest.raises(cli.TransferError, match="rclone exit 5"):
        cli.save_remote_copy_before_cancel(config)

    status = cli.recovery_mode_status(config)
    assert status["active"] is True
    assert status["phase"] == "locked"
    assert status["cancel_remote_copy"]["status"] == "failed"
    assert "rclone exit 5" in status["cancel_remote_copy"]["last_error"]


def test_recovery_download_catalog_migrates_retained_verified_audit_events(monkeypatch, tmp_path):
    config = _guided_recovery_config(tmp_path)
    destination = tmp_path / "watched_dropbox_before_cancel_20260827T120000Z"
    destination.mkdir()
    event = {
        "event_id": "verified-copy-event",
        "event_type": "recovery.cancel_remote_copy_verified",
        "occurred_at": "2026-08-27T12:00:00Z",
        "data": {"destination": f"<home>/Downloads/{destination.name}", "entry_count": 4},
        "correlation": {"operation_id": "old-copy", "folder_id": "watched"},
    }
    monkeypatch.setattr(
        cli,
        "event_journal",
        lambda _config: SimpleNamespace(
            events=lambda **kwargs: [event] if kwargs.get("event_type") == event["event_type"] else []
        ),
    )

    downloads = cli.recovery_downloads(config)

    assert len(downloads) == 1
    assert downloads[0]["kind"] == "dropbox_safety_copy"
    assert downloads[0]["folder_label"] == "watched"
    assert downloads[0]["destination"] == str(destination)
    assert downloads[0]["entry_count"] == 4
    assert downloads[0]["byte_count"] is None
    assert downloads[0]["migrated_from_audit"] is True
    assert downloads[0]["available"] is True


def test_remove_all_recovery_downloads_deletes_only_managed_folders(monkeypatch, tmp_path):
    config = _guided_recovery_config(tmp_path)
    managed_safety = tmp_path / "watched_dropbox_before_cancel_20260827T120000Z"
    managed_history = tmp_path / "watched_restore_20260827T121000Z"
    custom = tmp_path / "custom-recovery-location"
    for destination in (managed_safety, managed_history, custom):
        destination.mkdir()
        (destination / "copy.txt").write_text("recovery")
    for download_id, destination in (
        ("safety", managed_safety),
        ("history", managed_history),
        ("custom", custom),
    ):
        cli._remember_recovery_download(
            config,
            {
                "id": download_id,
                "kind": "dropbox_safety_copy",
                "folder_id": "watched",
                "destination": str(destination),
                "completed_at": "2026-08-27T12:00:00Z",
            },
        )

    with pytest.raises(cli.TransferError, match="while Recovery Mode is active"):
        cli.remove_recovery_downloads(config, remove_all=True)

    cli.recovery_mode_path(config).unlink()
    monkeypatch.setattr(cli, "record_event", lambda *_args, **_kwargs: None)
    result = cli.remove_recovery_downloads(config, remove_all=True)

    assert {item["id"] for item in result["removed"]} == {"safety", "history"}
    assert [item["id"] for item in result["skipped"]] == ["custom"]
    assert not managed_safety.exists()
    assert not managed_history.exists()
    assert custom.is_dir()
    assert [item["id"] for item in result["downloads"]] == ["custom"]


def test_save_remote_copy_refuses_insufficient_disk_space(monkeypatch, tmp_path):
    config = _guided_recovery_config(tmp_path)
    inventory = {"large.bin": {"type": "file", "size": 1024, "hashes": {"dropbox": "abc"}}}
    monkeypatch.setattr(cli, "_filtered_remote_inventory", lambda *_args: inventory)
    monkeypatch.setattr(cli.shutil, "disk_usage", lambda *_args: SimpleNamespace(total=1024, used=0, free=512))
    monkeypatch.setattr(cli, "_run_command_unlocked", lambda *_args, **_kwargs: pytest.fail("low-space export must not start"))

    with pytest.raises(cli.TransferError, match="Not enough free disk space"):
        cli.save_remote_copy_before_cancel(config)

    status = cli.recovery_mode_status(config)
    assert status["active"] is True
    assert status["cancel_remote_copy"]["status"] == "failed"


def test_save_remote_copy_detects_remote_change_and_keeps_lock(monkeypatch, tmp_path):
    config = _guided_recovery_config(tmp_path)
    before = {"remote.txt": {"type": "file", "size": 1, "hashes": {"dropbox": "before"}}}
    after = {"remote.txt": {"type": "file", "size": 1, "hashes": {"dropbox": "after"}}}
    inventories = iter([before, after])
    monkeypatch.setattr(cli, "_filtered_remote_inventory", lambda *_args: next(inventories))
    monkeypatch.setattr(cli, "_run_command_unlocked", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(cli, "local_inventory", lambda *_args, **_kwargs: before)

    with pytest.raises(cli.TransferError, match="Dropbox changed"):
        cli.save_remote_copy_before_cancel(config)

    status = cli.recovery_mode_status(config)
    assert status["active"] is True
    assert status["cancel_remote_copy"]["status"] == "failed"


def test_cancel_recovery_reconciles_local_to_remote_then_verifies(monkeypatch, tmp_path):
    config = _guided_recovery_config(tmp_path)
    before = {"equal": False, "remote_stable": True, "counts": {"different": 1}}
    after = {"equal": True, "remote_stable": True, "counts": {}}
    comparisons = iter([(False, before), (True, after)])
    commands = []

    def fake_run(_config, command, *_args, **_kwargs):
        commands.append(command)
        Path(command[command.index("--combined") + 1]).write_text("+ restored.txt\n")
        return 0

    monkeypatch.setattr(cli, "_live_recovery_equality", lambda *_args: next(comparisons))
    monkeypatch.setattr(cli, "preflight", lambda *_args: None)
    monkeypatch.setattr(cli, "_run_command_unlocked", fake_run)
    monkeypatch.setattr(cli, "_notify_recovery_mode", lambda *_args: {"ok": True})

    status = cli.cancel_recovery_mode(config)

    assert status["active"] is False
    assert status["remote_reconciled"] is True
    assert commands[0][1:4] == ["sync", str(tmp_path / "watched"), "dropbox:backups/mac/watched"]
    assert "--checksum" in commands[0]


def test_cancel_recovery_failure_remains_locked(monkeypatch, tmp_path):
    config = _guided_recovery_config(tmp_path)
    verification = {"equal": False, "remote_stable": True, "counts": {"different": 1}}
    monkeypatch.setattr(cli, "_live_recovery_equality", lambda *_args: (False, verification))
    monkeypatch.setattr(cli, "preflight", lambda *_args: None)
    monkeypatch.setattr(cli, "_run_command_unlocked", lambda *_args, **_kwargs: 5)

    with pytest.raises(cli.TransferError, match="rclone exit 5"):
        cli.cancel_recovery_mode(config)

    status = cli.recovery_mode_status(config)
    assert status["active"] is True
    assert status["phase"] == "cancel_failed"
    assert cli.recovery_mode_path(config).exists()


def test_cancel_recovery_restores_lock_when_daemon_resume_fails(monkeypatch, tmp_path):
    config = _guided_recovery_config(tmp_path)
    verification = {"equal": True, "remote_stable": True, "counts": {}}
    monkeypatch.setattr(cli, "_live_recovery_equality", lambda *_args: (True, verification))
    monkeypatch.setattr(cli, "_notify_recovery_mode", lambda *_args: {"ok": False, "error": "resume refused"})

    with pytest.raises(cli.TransferError, match="resume refused"):
        cli.cancel_recovery_mode(config)

    status = cli.recovery_mode_status(config)
    assert status["active"] is True
    assert status["phase"] == "cancel_failed"
    assert cli.recovery_mode_path(config).exists()


def test_machine_wide_recovery_mode_exports_verifies_and_guardedly_exits(monkeypatch, tmp_path):
    state = tmp_path / "state"
    local = tmp_path / "watched"
    local.mkdir()
    filter_path = tmp_path / "filter.txt"
    filter_path.write_text("")
    config = cli.normalized_config(
        {
            "active_profile_id": "mac",
            "state_root": str(state),
            "status_path": str(state / "status.json"),
            "socket_path": str(state / "daemon.sock"),
            "remote_base": "dropbox:computer-backups",
            "filter_file": str(filter_path),
            "profiles": [
                {
                    "id": "mac",
                    "machine_id": "mac",
                    "install_id": "install",
                    "remote_base": "dropbox:computer-backups",
                    "filter_file": str(filter_path),
                    "folders": [
                        {
                            "id": "watched",
                            "label": "Watched",
                            "local_path": str(local),
                            "remote_path": "mac/watched",
                            "filter_file": str(filter_path),
                        }
                    ],
                }
            ],
        }
    )
    destination = tmp_path / "watched_restore"
    entered = cli.enter_recovery_mode(config, "watched", str(destination))
    assert entered["active"] is True
    assert entered["phase"] == "locked"
    assert entered["target"]["folder_id"] == "watched"
    assert entered["destination"] == str(destination)

    rewound = cli.mark_recovery_rewind_complete(config)
    assert rewound["phase"] == "rewound"
    inventory = {
        "nested": {"type": "directory", "size": 0},
        "nested/file.txt": {"type": "file", "size": 3, "hashes": {"dropboxhash": "abc"}},
    }
    monkeypatch.setattr(cli, "_filtered_remote_inventory", lambda *_args: inventory)
    monkeypatch.setattr(cli, "run_command", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(cli, "local_inventory", lambda *_args, **_kwargs: inventory)
    exported = cli.export_rewound_folder(config)
    assert exported["phase"] == "exported"
    assert exported["export_entry_count"] == 2
    assert destination.is_dir()
    downloads = cli.recovery_downloads(config)
    assert len(downloads) == 1
    assert downloads[0]["kind"] == "historical_recovery_copy"
    assert downloads[0]["destination"] == str(destination)
    assert downloads[0]["entry_count"] == 2
    assert downloads[0]["byte_count"] == 3

    undo = cli.mark_recovery_undo_complete(config)
    assert undo["phase"] == "undo_complete"
    monkeypatch.setattr(cli, "_filtered_local_inventory", lambda *_args: inventory)
    equal, verified = cli.verify_recovery_current_state(config)
    assert equal is True
    assert verified["phase"] == "verified"
    monkeypatch.setattr(cli, "_notify_recovery_mode", lambda *_args: {"ok": True})
    inactive = cli.exit_recovery_mode(config)
    assert inactive["active"] is False
    assert not cli.recovery_mode_path(config).exists()
    assert cli.recovery_downloads(config)[0]["available"] is True


def test_recovery_mode_blocks_outbound_sync_at_execution_boundary(monkeypatch, tmp_path):
    local = tmp_path / "watched"
    local.mkdir()
    filter_path = tmp_path / "filter.txt"
    filter_path.write_text("")
    config = cli.normalized_config(
        {
            "state_root": str(tmp_path / "state"),
            "remote_base": "dropbox:backups",
            "filter_file": str(filter_path),
            "folders": [
                {
                    "id": "watched",
                    "local_path": str(local),
                    "remote_root": "dropbox:backups/mac/watched",
                    "filter_file": str(filter_path),
                }
            ],
        }
    )
    cli.enter_recovery_mode(config, "watched", str(tmp_path / "restore"))
    folder_cfg = cli.folder_config(config, config["folders"][0])
    called = False

    def fake_run(*_args, **_kwargs):
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(cli, "_run_command_unlocked", fake_run)
    assert cli.run_command(folder_cfg, cli.backup_cmd(folder_cfg, False)) == cli.RECOVERY_PAUSED_EXIT
    assert called is False


def test_recovery_destination_cannot_enter_any_profile_watched_tree(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    config = cli.normalized_config(
        {
            "state_root": str(tmp_path / "state"),
            "active_profile_id": "one",
            "profiles": [
                {"id": "one", "machine_id": "one", "folders": [{"id": "first", "local_path": str(first)}]},
                {"id": "two", "machine_id": "two", "folders": [{"id": "second", "local_path": str(second)}]},
            ],
        }
    )
    with pytest.raises(cli.TransferError, match="outside every watched folder"):
        cli.enter_recovery_mode(config, "first", str(second / "restore"))


def test_damaged_recovery_state_fails_closed_but_emergency_exit_is_possible(monkeypatch, tmp_path):
    config = cli.normalized_config({"state_root": str(tmp_path / "state")})
    mode_path = cli.recovery_mode_path(config)
    mode_path.parent.mkdir(parents=True)
    mode_path.write_text("{damaged")
    status = cli.recovery_mode_status(config)
    assert status["active"] is True
    assert status["phase"] == "invalid_locked"
    assert cli.recovery_is_paused(config) is True
    monkeypatch.setattr(cli, "_notify_recovery_mode", lambda *_args: {"ok": True})
    assert cli.exit_recovery_mode(config, force=True)["active"] is False
    assert not mode_path.exists()


def test_recent_recovery_changes_exposes_bounded_generation_paths(tmp_path):
    state = tmp_path / "state"
    local = tmp_path / "watched"
    local.mkdir()
    config = cli.normalized_config(
        {
            "active_profile_id": "mac",
            "state_root": str(state),
            "remote_base": "dropbox:computer-backups",
            "profiles": [
                {
                    "id": "mac",
                    "machine_id": "mac",
                    "install_id": "install",
                    "remote_base": "dropbox:computer-backups",
                    "folders": [{"id": "watched", "label": "Watched", "local_path": str(local), "remote_path": "mac/watched"}],
                }
            ],
        }
    )
    generation_dir = state / "generations" / "mac" / "watched" / "generations"
    generation_dir.mkdir(parents=True)
    (generation_dir / "gen_new.json").write_text(
        json.dumps(
            {
                "complete": True,
                "generation_id": "gen_new",
                "completed_at": "2026-08-17T12:00:00Z",
                "folder_id": "watched",
                "changes": [
                    {"path": "deleted.txt", "operation": "removed"},
                    {"path": "changed.txt", "operation": "modified"},
                ],
            }
        )
    )
    value = cli.recent_recovery_changes(config, "watched", paths_per_cycle=1)
    assert value["cycle_count"] == 1
    assert value["cycles"][0]["folder_label"] == "Watched"
    assert value["cycles"][0]["change_count"] == 2
    assert value["cycles"][0]["changes"] == [{"path": "deleted.txt", "operation": "removed"}]
    assert value["cycles"][0]["paths_truncated"] is True


def test_complete_snapshot_stages_historical_folder_without_touching_watched(monkeypatch, tmp_path):
    state = tmp_path / "state"
    watched = tmp_path / "watched"
    watched.mkdir()
    (watched / "live.txt").write_text("live watched data\n")
    filter_path = tmp_path / "filter.txt"
    filter_path.write_text("")
    config = cli.normalized_config(
        {
            "active_profile_id": "mac",
            "state_root": str(state),
            "remote_base": "dropbox:computer-backups",
            "profiles": [
                {
                    "id": "mac",
                    "machine_id": "mac",
                    "install_id": "install",
                    "remote_base": "dropbox:computer-backups",
                    "folders": [
                        {
                            "id": "watched",
                            "label": "Watched",
                            "local_path": str(watched),
                            "remote_path": "mac/watched",
                            "filter_file": str(filter_path),
                        }
                    ],
                }
            ],
        }
    )

    content = {}
    hashes = {}
    for name, value in {"same.txt": "same\n", "changed.txt": "historical\n", "deleted.txt": "deleted then\n"}.items():
        source = tmp_path / f"source-{name}"
        source.write_text(value)
        content[name] = value
        hashes[name] = dropbox_content_hash(source)
    expected = {
        name: {
            "type": "file",
            "size": len(value.encode()),
            "mtime": "2026-08-17T12:00:00Z",
            "hashes": {"dropboxhash": hashes[name]},
            "id": f"id:{name}",
            "revision": f"rev-{name}",
        }
        for name, value in content.items()
    }
    generation_dir = state / "generations" / "mac" / "watched" / "generations"
    generation_dir.mkdir(parents=True)
    (generation_dir / "gen_snapshot.json").write_text(
        json.dumps(
            {
                "complete": True,
                "generation_id": "gen_snapshot",
                "completed_at": "2026-08-17T12:00:00Z",
                "folder_id": "watched",
                "changes": [{"path": "changed.txt", "operation": "modified"}],
                "snapshot": {"entry_count": 3, "entries": expected},
            }
        )
    )
    current = {
        "same.txt": expected["same.txt"],
        "changed.txt": {**expected["changed.txt"], "revision": "rev-current", "hashes": {"dropboxhash": "different"}},
        "added-later.txt": {"type": "file", "size": 6, "hashes": {"dropboxhash": "later"}, "revision": "rev-later"},
    }

    def fake_run(_config, command, **_kwargs):
        destination = Path(command[3])
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "same.txt").write_text(content["same.txt"])
        (destination / "changed.txt").write_text("current\n")
        (destination / "added-later.txt").write_text("later\n")
        return 0

    def fake_download(_credentials, revision, destination):
        name = revision.removeprefix("rev-")
        destination.write_text(content[name])
        return {"rev": revision, "content_hash": hashes[name]}

    monkeypatch.setattr(cli, "run_command", fake_run)
    monkeypatch.setattr(cli, "capture_recovery_snapshot", lambda *_args: {"entries": current})
    monkeypatch.setattr(cli, "dropbox_history_credentials", lambda *_args: {"access_token": "test"})
    monkeypatch.setattr(cli, "download_revision", fake_download)
    cli.set_recovery_paused(config, True)
    result = cli.create_recovery_snapshot(config, "watched", "gen_snapshot")
    payload = Path(result["payload"])
    assert result["status"] == "ready"
    assert (payload / "same.txt").read_text() == "same\n"
    assert (payload / "changed.txt").read_text() == "historical\n"
    assert (payload / "deleted.txt").read_text() == "deleted then\n"
    assert not (payload / "added-later.txt").exists()
    assert (watched / "live.txt").read_text() == "live watched data\n"


def test_snapshot_manifest_compacts_against_parent_and_resolves(tmp_path):
    state = tmp_path / "state"
    local = tmp_path / "watched"
    local.mkdir()
    config = cli.normalized_config(
        {
            "active_profile_id": "mac",
            "state_root": str(state),
            "remote_base": "dropbox:computer-backups",
            "profiles": [
                {
                    "id": "mac",
                    "machine_id": "mac",
                    "install_id": "install",
                    "remote_base": "dropbox:computer-backups",
                    "folders": [{"id": "watched", "local_path": str(local), "remote_path": "mac/watched"}],
                }
            ],
        }
    )
    generation_dir = state / "generations" / "mac" / "watched" / "generations"
    generation_dir.mkdir(parents=True)
    base_entries = {
        "same.txt": {"type": "file", "size": 1, "revision": "one"},
        "removed.txt": {"type": "file", "size": 1, "revision": "old"},
    }
    (generation_dir / "gen_base.json").write_text(
        json.dumps(
            {
                "complete": True,
                "folder_id": "watched",
                "generation_id": "gen_base",
                "snapshot": {"kind": "full", "entry_count": 2, "entries": base_entries},
            }
        )
    )
    current = {
        "same.txt": base_entries["same.txt"],
        "added.txt": {"type": "file", "size": 2, "revision": "new"},
    }
    compact = cli.compact_recovery_snapshot(
        config,
        "watched",
        "gen_base",
        {"kind": "full", "entry_count": 2, "entries": current},
    )
    assert compact["kind"] == "delta"
    assert compact["entries"] == {"added.txt": current["added.txt"]}
    assert compact["removed"] == ["removed.txt"]
    resolved = cli.resolve_recovery_snapshot(
        config,
        "watched",
        {
            "generation_id": "gen_next",
            "parent_generation": "gen_base",
            "snapshot": compact,
        },
    )
    assert resolved == current


def test_no_change_backup_does_not_create_history_record(monkeypatch, tmp_path):
    local = tmp_path / "watched"
    local.mkdir()
    filter_path = tmp_path / "filter.txt"
    filter_path.write_text("")
    config = cli.normalized_config(
        {
            "active_profile_id": "mac",
            "state_root": str(tmp_path / "state"),
            "remote_base": "dropbox:computer-backups",
            "profiles": [
                {
                    "id": "mac",
                    "machine_id": "mac",
                    "install_id": "install",
                    "remote_base": "dropbox:computer-backups",
                    "folders": [
                        {"id": "watched", "local_path": str(local), "remote_path": "mac/watched", "filter_file": str(filter_path)}
                    ],
                }
            ],
        }
    )
    folder_config = cli.folder_config(config, config["folders"][0])
    remote_objects = {}

    def fake_capture(_config, command, input_text=None):
        if command[0] == "cat":
            return cli.subprocess.CompletedProcess(command, 0, remote_objects[command[1]]) if command[1] in remote_objects else cli.subprocess.CompletedProcess(command, 1, "not found")
        if command[0] == "rcat":
            remote_objects[command[1]] = input_text
        return cli.subprocess.CompletedProcess(command, 0, "")

    monkeypatch.setattr(cli, "rclone_capture", fake_capture)
    assert cli.publish_generation(folder_config, changes=[]) == 0
    assert not (cli.generation_local_dir(folder_config) / "latest.json").exists()
    assert remote_objects == {}


def test_recovery_pause_rejects_new_manual_backup_requests():
    state = DaemonApiState()
    state.update(state="recovery_paused", note="Machine-wide Recovery Mode is active")
    state.set_recovery_paused(True)
    state.request_backup()
    assert state.consume_backup_request() is False
    assert state.snapshot()["recovery_paused"] is True

    resumed = state.resume_recovery_and_request_backup()
    assert resumed["state"] == "dirty"
    assert resumed["recovery_paused"] is False
    assert resumed["recovery_resume_pending"] is True
    assert resumed["queued_backup"] is True
    assert resumed["sync_phase"] == "preparing"
    assert resumed["note"] == "Recovery complete; preparing normal backup"
    assert resumed["last_progress"] == "Recovery complete; normal backup is queued"
    assert state.consume_backup_request() is True
