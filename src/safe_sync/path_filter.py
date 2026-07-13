"""Path filtering helpers for the future watcher.

The rclone filter remains the source of truth for backup contents. These
helpers are only for reducing noisy watcher wakeups.
"""

from __future__ import annotations

from pathlib import Path


IGNORED_WATCH_PARTS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "out",
    ".cache",
    "__pycache__",
}


def should_ignore_watch_event(path: str | Path) -> bool:
    parts = Path(path).parts
    return any(part in IGNORED_WATCH_PARTS for part in parts)

