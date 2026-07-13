"""Watch daemon state and filesystem scanning helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from safe_sync.path_filter import should_ignore_watch_event


class DaemonState(str, Enum):
    IDLE = "idle"
    DIRTY = "dirty"
    SYNCING = "syncing"
    COOLDOWN = "cooldown"
    BACKOFF = "backoff"
    ERROR = "error"


@dataclass(frozen=True)
class WatchSettings:
    poll_interval_seconds: int = 5
    debounce_seconds: int = 20
    min_interval_seconds: int = 120
    fallback_interval_seconds: int = 1800
    rate_limit_backoff_seconds: int = 300


@dataclass
class WatchState:
    state: DaemonState = DaemonState.IDLE
    dirty: bool = False
    pending: bool = False
    last_change_monotonic: float | None = None
    last_sync_start_monotonic: float | None = None
    last_sync_finish_monotonic: float | None = None
    backoff_until_monotonic: float | None = None


def scan_tree(root: Path) -> dict[str, tuple[int, int]]:
    """Return a lightweight file snapshot for polling-based change detection."""
    root = root.expanduser().resolve()
    snapshot: dict[str, tuple[int, int]] = {}
    for current_root, dirnames, filenames in os.walk(root):
        current = Path(current_root)
        dirnames[:] = [name for name in dirnames if not should_ignore_watch_event(str(current / name))]
        for filename in filenames:
            path = current / filename
            if should_ignore_watch_event(str(path)):
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            relative = path.relative_to(root).as_posix()
            snapshot[relative] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


class WatchDaemon:
    """State machine for coalescing filesystem changes into backup runs."""

    def __init__(self, settings: WatchSettings | None = None) -> None:
        self.settings = settings or WatchSettings()
        self.state = WatchState()

    def mark_dirty(self, monotonic_time: float) -> None:
        """Record that at least one meaningful filesystem change happened."""
        self.state.dirty = True
        self.state.last_change_monotonic = monotonic_time
        if self.state.state == DaemonState.SYNCING:
            self.state.pending = True
        elif self.state.state != DaemonState.BACKOFF:
            self.state.state = DaemonState.DIRTY

    def should_sync_after_debounce(self, monotonic_time: float) -> bool:
        """Return true when dirty changes have been quiet long enough."""
        if self.state.state == DaemonState.BACKOFF:
            return False
        if not self.state.dirty or self.state.last_change_monotonic is None:
            return False
        quiet_for = monotonic_time - self.state.last_change_monotonic
        return quiet_for >= self.settings.debounce_seconds

    def should_run_fallback(self, monotonic_time: float) -> bool:
        if self.state.state == DaemonState.BACKOFF:
            return False
        if self.state.last_sync_finish_monotonic is None:
            return False
        return monotonic_time - self.state.last_sync_finish_monotonic >= self.settings.fallback_interval_seconds

    def in_min_interval(self, monotonic_time: float) -> bool:
        if self.state.last_sync_finish_monotonic is None:
            return False
        return monotonic_time - self.state.last_sync_finish_monotonic < self.settings.min_interval_seconds

    def min_interval_remaining(self, monotonic_time: float) -> float:
        if self.state.last_sync_finish_monotonic is None:
            return 0.0
        elapsed = monotonic_time - self.state.last_sync_finish_monotonic
        return max(0.0, self.settings.min_interval_seconds - elapsed)

    def note_sync_started(self, monotonic_time: float) -> None:
        self.state.state = DaemonState.SYNCING
        self.state.last_sync_start_monotonic = monotonic_time

    def note_sync_finished(self, monotonic_time: float, rate_limited: bool = False) -> None:
        self.state.last_sync_finish_monotonic = monotonic_time
        if rate_limited:
            self.state.state = DaemonState.BACKOFF
            self.state.backoff_until_monotonic = monotonic_time + self.settings.rate_limit_backoff_seconds
            return
        if self.state.pending:
            self.state.state = DaemonState.COOLDOWN
            self.state.dirty = True
            self.state.pending = False
            self.state.last_change_monotonic = monotonic_time
            return
        self.state.state = DaemonState.IDLE
        self.state.dirty = False

    def backoff_expired(self, monotonic_time: float) -> bool:
        return self.state.backoff_until_monotonic is not None and monotonic_time >= self.state.backoff_until_monotonic

    def backoff_remaining(self, monotonic_time: float) -> float:
        if self.state.backoff_until_monotonic is None:
            return 0.0
        return max(0.0, self.state.backoff_until_monotonic - monotonic_time)
