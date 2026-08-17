import io
import json
import subprocess
from pathlib import Path

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
    parsed = cli.parser().parse_args(["recovery", "revisions", "folder-a", "path/file.txt"])
    assert parsed.func is cli.cmd_recovery
    assert parsed.folder == "folder-a"
    assert parsed.path == "path/file.txt"
    recent = cli.parser().parse_args(["recovery", "recent", "--folder", "folder-a"])
    assert recent.func is cli.cmd_recovery
    assert recent.folder == "folder-a"
    snapshot = cli.parser().parse_args(["recovery", "snapshot", "folder-a", "gen_123"])
    assert snapshot.func is cli.cmd_recovery
    assert snapshot.generation == "gen_123"


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


def test_first_no_change_backup_creates_snapshot_baseline(monkeypatch, tmp_path):
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
    monkeypatch.setattr(
        cli,
        "capture_recovery_snapshot",
        lambda _config: {"kind": "full", "entry_count": 1, "entries": {"file.txt": {"type": "file", "revision": "rev"}}},
    )
    assert cli.publish_generation(folder_config, changes=[]) == 0
    latest = json.loads((cli.generation_local_dir(folder_config) / "latest.json").read_text())
    assert latest["changes"] == []
    assert latest["snapshot"]["kind"] == "full"


def test_recovery_pause_rejects_new_manual_backup_requests():
    state = DaemonApiState()
    state.set_recovery_paused(True)
    state.request_backup()
    assert state.consume_backup_request() is False
    assert state.snapshot()["recovery_paused"] is True
    state.set_recovery_paused(False)
    state.request_backup()
    assert state.consume_backup_request() is True
