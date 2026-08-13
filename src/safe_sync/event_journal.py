"""Structured, bounded Safe Sync observability journal.

The journal is authoritative for historical observability, not transaction
recovery.  Transactional generations and receive-job journals remain separate.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_SEGMENT_BYTES = 1024 * 1024
DEFAULT_CLOUD_FLUSH_SECONDS = 60
MIN_SEGMENTS = 4
MAX_JOURNAL_BYTES = 1024 * 1024 * 1024
LEVELS = ("quiet", "normal", "debug", "trace")
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2, "debug": 3, "trace": 4}
LEVEL_THRESHOLD = {"quiet": 0, "normal": 2, "debug": 3, "trace": 4}
SECRET_KEY = re.compile(r"(?:token|password|secret|authorization|cookie|credential|header)", re.IGNORECASE)
REMOTE_VALUE = re.compile(r"^[A-Za-z0-9._-]+:")
ABSOLUTE_PATH_FRAGMENT = re.compile(r"(^|[\s=(])(/[^\s,;]+)")


class JournalError(RuntimeError):
    """The event journal is unavailable or structurally invalid."""


@dataclass(frozen=True)
class JournalSettings:
    level: str = "normal"
    path_detail: str = "relative"
    max_local_bytes: int = DEFAULT_MAX_BYTES
    segment_bytes: int = DEFAULT_SEGMENT_BYTES
    cloud_enabled: bool = True
    max_cloud_bytes: int = DEFAULT_MAX_BYTES
    cloud_flush_interval_seconds: int = DEFAULT_CLOUD_FLUSH_SECONDS

    @property
    def slot_count(self) -> int:
        # Reserve one segment budget for active.tmp so sealed slots plus the
        # active writer remain within max_local_bytes.
        return max(1, self.max_local_bytes // self.segment_bytes - 1)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower()).strip("-")
    return cleaned or "default"


def _bounded_int(name: str, value: Any, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise JournalError(f"{name} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise JournalError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def settings_from_config(config: dict[str, Any]) -> JournalSettings:
    raw = config.get("logging") if isinstance(config.get("logging"), dict) else {}
    level = str(raw.get("level", "normal")).lower()
    temporary = raw.get("temporary_level")
    temporary_until = raw.get("temporary_until")
    if temporary and temporary_until:
        try:
            expiry = dt.datetime.fromisoformat(str(temporary_until).replace("Z", "+00:00"))
            if expiry > dt.datetime.now(dt.timezone.utc):
                level = str(temporary).lower()
        except ValueError:
            pass
    if level not in LEVELS:
        raise JournalError(f"logging level must be one of: {', '.join(LEVELS)}")
    path_detail = str(raw.get("path_detail", "relative")).lower()
    if path_detail not in {"relative", "hashed", "none"}:
        raise JournalError("logging path_detail must be relative, hashed, or none")
    segment_bytes = _bounded_int(
        "logging segment_bytes",
        raw.get("segment_bytes", DEFAULT_SEGMENT_BYTES),
        64 * 1024,
        16 * 1024 * 1024,
    )
    max_local = _bounded_int(
        "logging max_local_bytes",
        raw.get("max_local_bytes", DEFAULT_MAX_BYTES),
        segment_bytes * MIN_SEGMENTS,
        MAX_JOURNAL_BYTES,
    )
    max_local -= max_local % segment_bytes
    max_cloud = _bounded_int(
        "logging max_cloud_bytes",
        raw.get("max_cloud_bytes", max_local),
        max_local,
        MAX_JOURNAL_BYTES,
    )
    flush_seconds = _bounded_int(
        "logging cloud_flush_interval_seconds",
        raw.get("cloud_flush_interval_seconds", DEFAULT_CLOUD_FLUSH_SECONDS),
        10,
        3600,
    )
    return JournalSettings(
        level=level,
        path_detail=path_detail,
        max_local_bytes=max_local,
        segment_bytes=segment_bytes,
        cloud_enabled=bool(raw.get("cloud_enabled", True)),
        max_cloud_bytes=max_cloud,
        cloud_flush_interval_seconds=flush_seconds,
    )


def default_logging_config() -> dict[str, Any]:
    return {
        "level": "normal",
        "path_detail": "relative",
        "max_local_bytes": DEFAULT_MAX_BYTES,
        "segment_bytes": DEFAULT_SEGMENT_BYTES,
        "cloud_enabled": True,
        "max_cloud_bytes": DEFAULT_MAX_BYTES,
        "cloud_flush_interval_seconds": DEFAULT_CLOUD_FLUSH_SECONDS,
    }


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        if path.exists():
            mode = stat.S_IMODE(path.stat().st_mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _redact_string(value: str, home: Path, path_detail: str, install_id: str) -> str:
    if not value:
        return value
    home_text = str(home)
    if home_text and home_text in value:
        value = value.replace(home_text, "<home>")
    # Preserve rclone remotes and safe relative paths. Absolute paths outside
    # HOME are removed rather than guessed at.
    if value.startswith("/") and not value.startswith("<home>"):
        value = "<absolute-path-redacted>"
    else:
        value = ABSOLUTE_PATH_FRAGMENT.sub(lambda match: f"{match.group(1)}<absolute-path-redacted>", value)
    if path_detail == "none" and ("/" in value or "\\" in value) and not REMOTE_VALUE.match(value):
        return "<path-redacted>"
    if path_detail == "hashed" and ("/" in value or "\\" in value) and not REMOTE_VALUE.match(value):
        digest = hashlib.sha256(f"{install_id}\0{value}".encode()).hexdigest()[:20]
        suffix = Path(value).suffix[:16]
        return f"path:{digest}{suffix}"
    return value


def redact(value: Any, *, home: Path, path_detail: str, install_id: str, key: str = "") -> Any:
    if SECRET_KEY.search(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {
            str(child_key): redact(
                child_value,
                home=home,
                path_detail=path_detail,
                install_id=install_id,
                key=str(child_key),
            )
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [redact(item, home=home, path_detail=path_detail, install_id=install_id, key=key) for item in value]
    if isinstance(value, tuple):
        return [redact(item, home=home, path_detail=path_detail, install_id=install_id, key=key) for item in value]
    if isinstance(value, str):
        return _redact_string(value, home, path_detail, install_id)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_string(str(value), home, path_detail, install_id)


def diagnostic_enabled(settings: JournalSettings, channel: str, severity: str) -> bool:
    rank = SEVERITY_ORDER.get(severity)
    if rank is None:
        raise JournalError(f"unknown event severity: {severity}")
    if channel == "audit":
        return True
    return rank <= LEVEL_THRESHOLD[settings.level]


class EventJournal:
    """One process-safe segmented circular journal for one profile stream."""

    def __init__(
        self,
        *,
        state_root: str | Path,
        profile_id: str,
        machine_id: str,
        install_id: str,
        settings: JournalSettings | None = None,
        home: str | Path | None = None,
    ) -> None:
        self.settings = settings or JournalSettings()
        self.profile_id = safe_id(profile_id)
        self.machine_id = safe_id(machine_id)
        self.install_id = safe_id(install_id)
        self.home = Path(home or Path.home()).expanduser()
        self.root = Path(state_root).expanduser() / "event-journal" / self.profile_id
        self.slots_dir = self.root / "slots"
        self.quarantine_dir = self.root / "quarantine"
        self.cursor_path = self.root / "cursor.json"
        self.active_path = self.root / "active.tmp"
        self.intent_path = self.root / "seal-intent.json"
        self.lock_path = self.root / "journal.lock"
        self._thread_lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)
        self.slots_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            with self.lock_path.open("a+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _new_cursor(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "profile_id": self.profile_id,
            "machine_id": self.machine_id,
            "install_id": self.install_id,
            "epoch": uuid.uuid4().hex,
            "next_sequence": 1,
            "current_slot": 0,
            "slot_count": self.settings.slot_count,
            "segments": {},
            "gaps": [],
            "diagnostics_suppressed": 0,
            "replication": {
                "last_success_at": None,
                "last_error": None,
                "remote_manifest_hash": None,
            },
        }

    def _save_cursor(self, cursor: dict[str, Any]) -> None:
        _atomic_write(self.cursor_path, json.dumps(cursor, indent=2, sort_keys=True).encode("utf-8") + b"\n")

    def _read_cursor(self) -> dict[str, Any]:
        try:
            cursor = json.loads(self.cursor_path.read_text())
            if int(cursor.get("schema_version")) != SCHEMA_VERSION:
                raise ValueError("unsupported cursor schema")
            if cursor.get("install_id") != self.install_id or cursor.get("profile_id") != self.profile_id:
                raise ValueError("cursor identity mismatch")
            if int(cursor.get("slot_count")) != self.settings.slot_count:
                return self._resize_cursor(cursor)
            if not isinstance(cursor.get("segments"), dict) or not isinstance(cursor.get("gaps"), list):
                raise ValueError("invalid cursor collections")
            return cursor
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return self._rebuild_cursor()

    def _resize_cursor(self, cursor: dict[str, Any]) -> dict[str, Any]:
        # Retain newest segments that fit. Old slot files are removed only
        # after the new cursor is safely published.
        segments = sorted(cursor.get("segments", {}).values(), key=lambda item: int(item["end_sequence"]))
        keep = segments[-self.settings.slot_count :]
        kept_paths = {str(item["path"]) for item in keep}
        new_cursor = dict(cursor)
        new_cursor["slot_count"] = self.settings.slot_count
        new_cursor["segments"] = {str(index): {**item, "slot": index} for index, item in enumerate(keep)}
        new_cursor["current_slot"] = len(keep) % self.settings.slot_count
        self._save_cursor(new_cursor)
        for path in self.slots_dir.glob("*.jsonl"):
            if path.name not in kept_paths:
                try:
                    path.unlink()
                except OSError:
                    pass
        return new_cursor

    @staticmethod
    def _complete_lines(data: bytes) -> bytes:
        if not data or data.endswith(b"\n"):
            return data
        boundary = data.rfind(b"\n")
        return data[: boundary + 1] if boundary >= 0 else b""

    def _parse_events(self, data: bytes) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for raw in data.splitlines():
            if not raw.strip():
                continue
            value = json.loads(raw)
            if not isinstance(value, dict) or int(value.get("schema_version", 0)) != SCHEMA_VERSION:
                raise ValueError("invalid event schema")
            events.append(value)
        return events

    def _segment_meta(self, slot: int, path: Path, data: bytes, *, replicated: bool = False) -> dict[str, Any]:
        events = self._parse_events(data)
        if not events:
            raise ValueError("empty segment")
        return {
            "slot": slot,
            "path": path.name,
            "epoch": str(events[0]["stream"]["epoch"]),
            "start_sequence": int(events[0]["sequence"]),
            "end_sequence": int(events[-1]["sequence"]),
            "event_count": len(events),
            "bytes": len(data),
            "sha256": _sha256(data),
            "replicated": replicated,
            "sealed_at": utc_now(),
        }

    def _quarantine(self, path: Path) -> None:
        try:
            self.quarantine_dir.mkdir(parents=True, exist_ok=True)
            target = self.quarantine_dir / f"{path.name}.{uuid.uuid4().hex}.corrupt"
            os.replace(path, target)
        except OSError:
            pass

    def _rebuild_cursor(self) -> dict[str, Any]:
        cursor = self._new_cursor()
        candidates: list[tuple[int, Path, dict[str, Any]]] = []
        for path in sorted(self.slots_dir.glob("*.jsonl")):
            try:
                slot = int(path.stem)
                data = path.read_bytes()
                meta = self._segment_meta(slot, path, data)
                candidates.append((slot, path, meta))
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                self._quarantine(path)
        active = b""
        try:
            active = self._complete_lines(self.active_path.read_bytes())
        except OSError:
            pass
        if self.active_path.exists() and self.active_path.read_bytes() != active:
            _atomic_write(self.active_path, active)
        epochs: dict[str, float] = {}
        for _slot, path, meta in candidates:
            epochs[str(meta["epoch"])] = max(epochs.get(str(meta["epoch"]), 0.0), path.stat().st_mtime)
        try:
            for event in self._parse_events(active):
                epochs[str(event["stream"]["epoch"])] = max(
                    epochs.get(str(event["stream"]["epoch"]), 0.0),
                    self.active_path.stat().st_mtime,
                )
        except (ValueError, KeyError, json.JSONDecodeError):
            self._quarantine(self.active_path)
            active = b""
        if epochs:
            cursor["epoch"] = max(epochs, key=epochs.get)
        current_epoch = cursor["epoch"]
        selected = [item for item in candidates if item[2]["epoch"] == current_epoch]
        selected.sort(key=lambda item: int(item[2]["end_sequence"]))
        selected = selected[-self.settings.slot_count :]
        cursor["segments"] = {str(slot): meta for slot, _path, meta in selected}
        used_slots = {slot for slot, _path, _meta in selected}
        cursor["current_slot"] = next((slot for slot in range(self.settings.slot_count) if slot not in used_slots), 0)
        maximum = max((int(meta["end_sequence"]) for _slot, _path, meta in selected), default=0)
        try:
            active_events = [event for event in self._parse_events(active) if event["stream"]["epoch"] == current_epoch]
        except (ValueError, KeyError, json.JSONDecodeError):
            active_events = []
        if active_events:
            maximum = max(maximum, max(int(event["sequence"]) for event in active_events))
        cursor["next_sequence"] = maximum + 1
        self._save_cursor(cursor)
        if not self.active_path.exists():
            _atomic_write(self.active_path, b"")
        return cursor

    def _recover_intent(self, cursor: dict[str, Any]) -> dict[str, Any]:
        if not self.intent_path.exists():
            return cursor
        try:
            intent = json.loads(self.intent_path.read_text())
            slot = int(intent["slot"])
            target = self.slots_dir / f"{slot:04d}.jsonl"
            expected_hash = str(intent["new_segment"]["sha256"])
            if target.exists() and _sha256(target.read_bytes()) == expected_hash:
                old = intent.get("old_segment")
                if isinstance(old, dict) and not old.get("replicated", False):
                    self._append_gap(cursor, old, "local_wrap_before_cloud_replication")
                cursor["segments"][str(slot)] = intent["new_segment"]
                cursor["current_slot"] = (slot + 1) % self.settings.slot_count
                cursor["next_sequence"] = max(
                    int(cursor.get("next_sequence", 1)),
                    int(intent["new_segment"]["end_sequence"]) + 1,
                )
                self._save_cursor(cursor)
                self.intent_path.unlink(missing_ok=True)
                if not self.active_path.exists():
                    _atomic_write(self.active_path, b"")
                return cursor
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            pass
        self._quarantine(self.intent_path)
        return self._rebuild_cursor()

    def _initialize(self) -> None:
        with self._locked():
            cursor = self._read_cursor()
            self._recover_intent(cursor)
            if not self.active_path.exists():
                _atomic_write(self.active_path, b"")
            else:
                data = self.active_path.read_bytes()
                complete = self._complete_lines(data)
                if complete != data:
                    _atomic_write(self.active_path, complete)

    @staticmethod
    def _append_gap(cursor: dict[str, Any], segment: dict[str, Any], reason: str) -> None:
        gap = {
            "epoch": segment.get("epoch"),
            "start_sequence": segment.get("start_sequence"),
            "end_sequence": segment.get("end_sequence"),
            "reason": reason,
            "recorded_at": utc_now(),
        }
        if gap not in cursor["gaps"]:
            cursor["gaps"].append(gap)
            cursor["gaps"] = cursor["gaps"][-256:]

    def _seal_locked(self, cursor: dict[str, Any]) -> dict[str, Any] | None:
        if not self.active_path.exists():
            _atomic_write(self.active_path, b"")
            return None
        data = self._complete_lines(self.active_path.read_bytes())
        if not data:
            if self.active_path.stat().st_size:
                _atomic_write(self.active_path, b"")
            return None
        slot = int(cursor["current_slot"])
        old = cursor["segments"].get(str(slot))
        if isinstance(old, dict):
            # A capacity resize can remap logical slots while retained segment
            # files keep their old names. Replace the file owned by this
            # logical slot, not an unrelated retained file with the same stem.
            target = self.slots_dir / str(old["path"])
        else:
            occupied = {str(item["path"]) for item in cursor["segments"].values()}
            candidate = f"{slot:04d}.jsonl"
            if candidate in occupied:
                physical_slot = max(
                    [int(Path(name).stem) for name in occupied if Path(name).stem.isdigit()] + [-1]
                ) + 1
                candidate = f"{physical_slot:04d}.jsonl"
            target = self.slots_dir / candidate
        meta = self._segment_meta(slot, target, data)
        intent = {"schema_version": 1, "slot": slot, "old_segment": old, "new_segment": meta}
        _atomic_write(self.intent_path, json.dumps(intent, indent=2, sort_keys=True).encode() + b"\n")
        with self.active_path.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(self.active_path, target)
        if isinstance(old, dict) and not old.get("replicated", False):
            self._append_gap(cursor, old, "local_wrap_before_cloud_replication")
        cursor["segments"][str(slot)] = meta
        cursor["current_slot"] = (slot + 1) % self.settings.slot_count
        self._save_cursor(cursor)
        _atomic_write(self.active_path, b"")
        self.intent_path.unlink(missing_ok=True)
        return meta

    def emit(
        self,
        event_type: str,
        *,
        component: str,
        channel: str = "audit",
        severity: str = "info",
        data: dict[str, Any] | None = None,
        correlation: dict[str, Any] | None = None,
        run_id: str | None = None,
        durability_hint: str = "audit_only",
        effect: str | None = None,
    ) -> dict[str, Any] | None:
        if channel not in {"audit", "diagnostic"}:
            raise JournalError("event channel must be audit or diagnostic")
        if not diagnostic_enabled(self.settings, channel, severity):
            return None
        if not re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+", event_type):
            raise JournalError(f"invalid event type: {event_type}")
        new_gaps: list[dict[str, Any]] = []
        with self._locked():
            cursor = self._recover_intent(self._read_cursor())
            initial_gap_count = len(cursor["gaps"])
            active_data = self._complete_lines(self.active_path.read_bytes() if self.active_path.exists() else b"")
            pending_bytes = len(active_data) + sum(
                int(item.get("bytes", 0))
                for item in cursor["segments"].values()
                if not item.get("replicated", False)
            )
            audit_reserve = max(self.settings.segment_bytes, self.settings.max_local_bytes // 4)
            pending_slots = sum(
                1 for item in cursor["segments"].values() if not item.get("replicated", False)
            ) + (1 if active_data else 0)
            audit_reserve_slots = max(1, (self.settings.slot_count + 3) // 4)
            diagnostic_slot_limit = max(1, self.settings.slot_count - audit_reserve_slots)
            if channel == "diagnostic" and (
                pending_bytes >= self.settings.max_local_bytes - audit_reserve
                or pending_slots >= diagnostic_slot_limit
            ):
                # Persist the transition once, then drop without a cursor
                # rewrite per raw line. The next audit event remains free to
                # consume the physically reserved slots.
                if not int(cursor.get("diagnostics_suppressed", 0)):
                    cursor["diagnostics_suppressed"] = 1
                    self._save_cursor(cursor)
                return None
            try:
                active_events = self._parse_events(active_data)
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                self._quarantine(self.active_path)
                active_data = b""
                active_events = []
            max_seen = max(
                [int(item["end_sequence"]) for item in cursor["segments"].values()]
                + [int(item["sequence"]) for item in active_events]
                + [0]
            )
            sequence = max(int(cursor.get("next_sequence", 1)), max_seen + 1)
            recorded = utc_now()
            event = {
                "schema_version": SCHEMA_VERSION,
                "event_id": f"evt_{self.install_id}_{cursor['epoch']}_{sequence:012d}",
                "sequence": sequence,
                "occurred_at": recorded,
                "recorded_at": recorded,
                "stream": {
                    "profile_id": self.profile_id,
                    "machine_id": self.machine_id,
                    "install_id": self.install_id,
                    "epoch": cursor["epoch"],
                },
                "run_id": safe_id(run_id or f"process-{os.getpid()}"),
                "component": safe_id(component),
                "channel": channel,
                "severity": severity,
                "event_type": event_type,
                "correlation": redact(
                    correlation or {},
                    home=self.home,
                    path_detail=self.settings.path_detail,
                    install_id=self.install_id,
                ),
                "durability_hint": durability_hint,
                "data": redact(
                    data or {},
                    home=self.home,
                    path_detail=self.settings.path_detail,
                    install_id=self.install_id,
                ),
            }
            if effect is not None:
                event["effect"] = effect
            line = _json_bytes(event)
            if len(line) > self.settings.segment_bytes:
                event["data"] = {
                    "payload_truncated": True,
                    "original_bytes": len(line),
                }
                line = _json_bytes(event)
                if len(line) > self.settings.segment_bytes:
                    raise JournalError("logging segment_bytes is too small for the event envelope")
            if active_data and len(active_data) + len(line) > self.settings.segment_bytes:
                self._seal_locked(cursor)
                active_data = b""
            self.active_path.parent.mkdir(parents=True, exist_ok=True)
            with self.active_path.open("ab") as handle:
                handle.write(line)
                handle.flush()
                if channel == "audit" or severity in {"error", "warning"}:
                    os.fsync(handle.fileno())
            cursor["next_sequence"] = sequence + 1
            self._save_cursor(cursor)
            new_gaps = list(cursor["gaps"][initial_gap_count:])
        if new_gaps and event_type != "logging.events_dropped":
            self.emit(
                "logging.events_dropped",
                component="logging",
                channel="audit",
                severity="warning",
                data={"gaps": new_gaps, "dropped_event_count": sum(
                    int(gap["end_sequence"]) - int(gap["start_sequence"]) + 1 for gap in new_gaps
                )},
                run_id=run_id,
            )
        return event

    def seal_active(self) -> dict[str, Any] | None:
        new_gaps: list[dict[str, Any]] = []
        with self._locked():
            cursor = self._recover_intent(self._read_cursor())
            initial_gap_count = len(cursor["gaps"])
            sealed = self._seal_locked(cursor)
            new_gaps = list(cursor["gaps"][initial_gap_count:])
        if new_gaps:
            self.emit(
                "logging.events_dropped",
                component="logging",
                channel="audit",
                severity="warning",
                data={
                    "gaps": new_gaps,
                    "dropped_event_count": sum(
                        int(gap["end_sequence"]) - int(gap["start_sequence"]) + 1 for gap in new_gaps
                    ),
                },
            )
        return sealed

    def segment_records(self) -> list[dict[str, Any]]:
        with self._locked():
            cursor = self._recover_intent(self._read_cursor())
            return sorted(
                [dict(item) for item in cursor["segments"].values()],
                key=lambda item: (str(item["epoch"]), int(item["start_sequence"])),
            )

    def segment_text(self, segment: dict[str, Any]) -> str:
        path = self.slots_dir / str(segment["path"])
        data = path.read_bytes()
        if _sha256(data) != segment["sha256"]:
            raise JournalError(f"event segment hash mismatch: {path}")
        return data.decode("utf-8")

    def mark_replicated(
        self,
        hashes: set[str],
        *,
        manifest_hash: str,
        error: str | None = None,
    ) -> None:
        with self._locked():
            cursor = self._recover_intent(self._read_cursor())
            for segment in cursor["segments"].values():
                if segment.get("sha256") in hashes:
                    segment["replicated"] = True
                    segment["replicated_at"] = utc_now()
            cursor["replication"]["last_success_at"] = utc_now() if error is None else cursor["replication"].get("last_success_at")
            cursor["replication"]["last_error"] = error
            if error is None:
                cursor["replication"]["remote_manifest_hash"] = manifest_hash
            self._save_cursor(cursor)

    def mark_replication_error(self, error: str) -> None:
        with self._locked():
            cursor = self._recover_intent(self._read_cursor())
            cursor["replication"]["last_error"] = str(
                redact(error, home=self.home, path_detail=self.settings.path_detail, install_id=self.install_id)
            )
            self._save_cursor(cursor)

    def events(
        self,
        *,
        limit: int | None = 200,
        event_type: str | None = None,
        folder_id: str | None = None,
        severity: str | None = None,
        since: dt.datetime | None = None,
    ) -> list[dict[str, Any]]:
        with self._locked():
            cursor = self._recover_intent(self._read_cursor())
            records: dict[tuple[str, int], dict[str, Any]] = {}
            for segment in cursor["segments"].values():
                path = self.slots_dir / str(segment["path"])
                try:
                    data = path.read_bytes()
                    if _sha256(data) != segment["sha256"]:
                        continue
                    for item in self._parse_events(data):
                        records[(str(item["stream"]["epoch"]), int(item["sequence"]))] = item
                except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                    continue
            try:
                for item in self._parse_events(self._complete_lines(self.active_path.read_bytes())):
                    records[(str(item["stream"]["epoch"]), int(item["sequence"]))] = item
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                pass
            values = sorted(records.values(), key=lambda item: (str(item["stream"]["epoch"]), int(item["sequence"])))
        if event_type:
            values = [item for item in values if item.get("event_type") == event_type]
        if severity:
            values = [item for item in values if item.get("severity") == severity]
        if folder_id:
            values = [
                item
                for item in values
                if str((item.get("correlation") or {}).get("folder_id") or "") == folder_id
            ]
        if since:
            values = [
                item
                for item in values
                if dt.datetime.fromisoformat(str(item["occurred_at"]).replace("Z", "+00:00")) >= since
            ]
        if limit is not None and limit >= 0:
            values = values[-limit:]
        return values

    def status(self) -> dict[str, Any]:
        with self._locked():
            cursor = self._recover_intent(self._read_cursor())
            active_bytes = self.active_path.stat().st_size if self.active_path.exists() else 0
            segments = sorted(cursor["segments"].values(), key=lambda item: int(item["start_sequence"]))
            used = active_bytes + sum(int(item["bytes"]) for item in segments)
            pending = [item for item in segments if not item.get("replicated", False)]
            return {
                "schema_version": SCHEMA_VERSION,
                "profile_id": self.profile_id,
                "machine_id": self.machine_id,
                "install_id": self.install_id,
                "epoch": cursor["epoch"],
                "level": self.settings.level,
                "path_detail": self.settings.path_detail,
                "cloud_enabled": self.settings.cloud_enabled,
                "used_local_bytes": used,
                "max_local_bytes": self.settings.max_local_bytes,
                "segment_bytes": self.settings.segment_bytes,
                "segment_count": len(segments),
                "slot_count": self.settings.slot_count,
                "oldest_sequence": int(segments[0]["start_sequence"]) if segments else None,
                "newest_sequence": int(cursor["next_sequence"]) - 1,
                "pending_cloud_segments": len(pending),
                "diagnostics_suppressed": int(cursor.get("diagnostics_suppressed", 0)),
                "audit_reserve_bytes": max(self.settings.segment_bytes, self.settings.max_local_bytes // 4),
                "gaps": list(cursor["gaps"]),
                "history_complete": not bool(cursor["gaps"]),
                "history_gap_count": len(cursor["gaps"]),
                "replication": dict(cursor["replication"]),
                # Current health answers whether logging is working now.
                # Permanent wrap/corruption gaps describe retained-history
                # completeness separately and must not create a warning forever.
                "health": "degraded" if cursor["replication"].get("last_error") else "ok",
            }

    def cloud_manifest(self) -> dict[str, Any]:
        status = self.status()
        segments = self.segment_records()
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": utc_now(),
            "stream": {
                "profile_id": self.profile_id,
                "machine_id": self.machine_id,
                "install_id": self.install_id,
                "epoch": status["epoch"],
            },
            "segments": [
                {
                    key: segment[key]
                    for key in ("epoch", "start_sequence", "end_sequence", "event_count", "bytes", "sha256")
                }
                for segment in segments
            ],
            "gaps": status["gaps"],
            "max_cloud_bytes": self.settings.max_cloud_bytes,
        }


def journal_from_config(config: dict[str, Any]) -> EventJournal:
    state_root = config.get("state_root") or Path.home() / ".local" / "state" / "safe-sync"
    return EventJournal(
        state_root=state_root,
        profile_id=str(config.get("profile_id") or config.get("active_profile_id") or "system"),
        machine_id=str(config.get("machine_id") or config.get("machine") or "unknown"),
        install_id=str(config.get("install_id") or "unconfigured"),
        settings=settings_from_config(config),
    )
