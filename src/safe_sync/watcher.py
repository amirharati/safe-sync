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

    def __init__(self, wake: Callable[[], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._changed: set[str] = set()
        self._wake = wake

    def mark(self, folder_id: str) -> None:
        with self._lock:
            self._changed.add(folder_id)
        if self._wake is not None:
            self._wake()

    def consume(self) -> list[str]:
        with self._lock:
            changed = sorted(self._changed)
            self._changed.clear()
        return changed


class _FolderEventHandler(FileSystemEventHandler):  # type: ignore[misc]
    def __init__(self, folder_id: str, root: Path, queue: ChangeQueue) -> None:
        super().__init__()
        self.folder_id = folder_id
        self.root = root.resolve()
        self.queue = queue

    def _is_relevant(self, raw_path: str | bytes | None, *, event_type: str, is_directory: bool) -> bool:
        if not raw_path:
            return False
        try:
            relative = Path(raw_path).resolve().relative_to(self.root)
        except (OSError, TypeError, ValueError):
            return False
        if relative == Path("."):
            # Native backends emit a redundant watched-root metadata update
            # when any direct child changes. The precise child event carries
            # the useful path and lets ignored directories remain ignored.
            return not (event_type == "modified" and is_directory)
        return not should_ignore_watch_event(relative)

    def on_any_event(self, event: FileSystemEvent) -> None:
        event_type = str(getattr(event, "event_type", ""))
        if event_type in {"opened", "closed_no_write"}:
            return
        paths = [getattr(event, "src_path", None), getattr(event, "dest_path", None)]
        if any(
            self._is_relevant(path, event_type=event_type, is_directory=bool(getattr(event, "is_directory", False)))
            for path in paths
        ):
            self.queue.mark(self.folder_id)


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
