from pathlib import Path

from safe_sync.daemon import DaemonState, WatchDaemon, WatchSettings, scan_tree
from safe_sync.cli import (
    enabled_folders,
    folder_snapshots,
    normalized_config,
    registry_doc,
    registry_path,
    selected_folders,
    unsafe_local_path_reason,
)
from safe_sync.path_filter import should_ignore_watch_event


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


def test_watch_filter_ignores_generated_paths():
    assert should_ignore_watch_event("/tmp/project/node_modules/pkg/index.js")
    assert should_ignore_watch_event("/tmp/project/.venv/lib/site.py")
    assert should_ignore_watch_event("/tmp/project/dist/app.js")
    assert not should_ignore_watch_event("/tmp/project/data/results.csv")
    assert not should_ignore_watch_event("/tmp/project/models/model.pt")



def test_scan_tree_detects_useful_changes(tmp_path):
    useful = tmp_path / "data"
    ignored = tmp_path / "node_modules" / "pkg"
    useful.mkdir()
    ignored.mkdir(parents=True)

    (useful / "results.csv").write_text("a,b\n1,2\n")
    (ignored / "index.js").write_text("ignored")

    snapshot = scan_tree(Path(tmp_path))

    assert "data/results.csv" in snapshot
    assert "node_modules/pkg/index.js" not in snapshot


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
