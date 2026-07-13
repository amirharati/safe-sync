from pathlib import Path

from safe_sync.daemon import DaemonState, WatchDaemon, WatchSettings, scan_tree
from safe_sync.cli import unsafe_local_path_reason
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
