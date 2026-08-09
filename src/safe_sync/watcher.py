"""Native filesystem event collection with a visible polling fallback."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from safe_sync.path_filter import should_ignore_watch_event

try:  # The source tree can run without the app-managed dependency.
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # pragma: no cover - exercised through dependency injection
    FileSystemEvent = object  # type: ignore[assignment,misc]
    FileSystemEventHandler = object  # type: ignore[assignment,misc]
    Observer = None  # type: ignore[assignment,misc]


class ChangeQueue:
    """Coalesce native events by configured folder."""

    def __init__(self, wake: Callable[[], None] | None = None, max_paths_per_folder: int = 256) -> None:
        self._lock = threading.Lock()
        self._changed: dict[str, set[str]] = {}
        self._overflow: set[str] = set()
        self._wake = wake
        self._max_paths_per_folder = max_paths_per_folder

    def mark(self, folder_id: str, relative_path: str = "") -> None:
        with self._lock:
            paths = self._changed.setdefault(folder_id, set())
            if len(paths) < self._max_paths_per_folder:
                if relative_path:
                    paths.add(relative_path)
            else:
                self._overflow.add(folder_id)
        if self._wake is not None:
            self._wake()

    def consume(self) -> list[str]:
        return sorted(self.consume_details())

    def consume_details(self) -> dict[str, dict[str, object]]:
        with self._lock:
            changed = {
                folder_id: {
                    "paths": sorted(paths),
                    "path_count": len(paths),
                    "paths_truncated": folder_id in self._overflow,
                }
                for folder_id, paths in self._changed.items()
            }
            self._changed = {}
            self._overflow.clear()
        return changed


class _FolderEventHandler(FileSystemEventHandler):  # type: ignore[misc]
    def __init__(self, folder_id: str, root: Path, queue: ChangeQueue) -> None:
        super().__init__()
        self.folder_id = folder_id
        self.root = root.resolve()
        self.queue = queue

    def _relevant_path(self, raw_path: str | bytes | None, *, event_type: str, is_directory: bool) -> str | None:
        if not raw_path:
            return None
        try:
            relative = Path(raw_path).resolve().relative_to(self.root)
        except (OSError, TypeError, ValueError):
            return None
        if relative == Path("."):
            # Native backends emit a redundant watched-root metadata update
            # when any direct child changes. The precise child event carries
            # the useful path and lets ignored directories remain ignored.
            return "" if not (event_type == "modified" and is_directory) else None
        return None if should_ignore_watch_event(relative) else relative.as_posix()

    def on_any_event(self, event: FileSystemEvent) -> None:
        event_type = str(getattr(event, "event_type", ""))
        if event_type in {"opened", "closed_no_write"}:
            return
        paths = [getattr(event, "src_path", None), getattr(event, "dest_path", None)]
        for path in paths:
            relative = self._relevant_path(
                path,
                event_type=event_type,
                is_directory=bool(getattr(event, "is_directory", False)),
            )
            if relative is not None:
                self.queue.mark(self.folder_id, relative)


class NativeWatcher:
    """Own one watchdog observer for all configured roots."""

    def __init__(
        self,
        folders: list[dict[str, object]],
        wake: Callable[[], None] | None = None,
        observer_factory: Callable[[], object] | None = None,
    ) -> None:
        self.folders = folders
        self.queue = ChangeQueue(wake)
        self._observer_factory = observer_factory
        self._observer: object | None = None

    @property
    def available(self) -> bool:
        return self._observer_factory is not None or Observer is not None

    def start(self) -> None:
        if not self.available:
            raise RuntimeError("app-managed watchdog runtime is unavailable")
        factory = self._observer_factory or Observer
        assert factory is not None
        observer = factory()
        try:
            for folder in self.folders:
                folder_id = str(folder["id"])
                root = Path(str(folder["local_path"])).expanduser().resolve()
                handler = _FolderEventHandler(folder_id, root, self.queue)
                observer.schedule(handler, str(root), recursive=True)
            observer.start()
            time.sleep(0.05)
            emitters = list(getattr(observer, "emitters", ()))
            if emitters and any(not emitter.is_alive() for emitter in emitters):
                raise RuntimeError("native watcher stopped during startup")
        except Exception:
            try:
                observer.stop()
                observer.join(timeout=10)
            except (AttributeError, RuntimeError):
                pass
            raise
        self._observer = observer

    def consume(self) -> list[str]:
        return self.queue.consume()

    def consume_details(self) -> dict[str, dict[str, object]]:
        return self.queue.consume_details()

    def healthy(self) -> bool:
        if self._observer is None:
            return False
        emitters = list(getattr(self._observer, "emitters", ()))
        return not emitters or all(emitter.is_alive() for emitter in emitters)

    def stop(self) -> None:
        if self._observer is None:
            return
        self._observer.stop()
        self._observer.join(timeout=10)
        self._observer = None
