"""Safe cross-computer comparison, receive jobs, and linked-folder state."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import stat
import tempfile
import unicodedata
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable


SCHEMA_VERSION = 1
WORK_DIR_NAME = ".safe-sync-work"
INTERNAL_WORK_PARTS = {WORK_DIR_NAME}


class TransferError(RuntimeError):
    """Base error for safe receive operations."""


class ScopeError(TransferError):
    """A folder scope is unsafe or escapes its configured root."""


class JobConflictError(TransferError):
    """A job needs explicit conflict decisions or a refreshed comparison."""

    def __init__(self, message: str, paths: Iterable[str] = ()) -> None:
        self.paths = sorted(set(paths))
        detail = f": {', '.join(self.paths)}" if self.paths else ""
        super().__init__(f"{message}{detail}")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def utc_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def new_id(prefix: str) -> str:
    return f"{prefix}_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:10]}"


def atomic_write_text(path: Path, text: str) -> None:
    """Durably replace a small text file without exposing partial JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
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


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def normalize_subpath(value: str | None) -> str:
    """Return a safe POSIX relative path, or an empty string for a root scope."""
    raw = (value or "").strip().replace("\\", "/")
    raw = raw.strip("/")
    if not raw or raw == ".":
        return ""
    if "\x00" in raw:
        raise ScopeError("folder scope contains NUL")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ScopeError(f"unsafe folder scope: {value}")
    return path.as_posix()


def join_remote_scope(remote_root: str, subpath: str | None) -> str:
    scope = normalize_subpath(subpath)
    return remote_root.rstrip("/") if not scope else f"{remote_root.rstrip('/')}/{scope}"


def is_path_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_local_scope(root: str | Path, subpath: str | None = "", *, require_exists: bool = True) -> Path:
    base = Path(root).expanduser().resolve()
    scope = normalize_subpath(subpath)
    candidate = base if not scope else base.joinpath(*PurePosixPath(scope).parts)
    resolved = candidate.resolve(strict=require_exists)
    if not is_path_within(resolved, base):
        raise ScopeError(f"folder scope escapes configured root: {subpath}")
    return resolved


def scopes_overlap(left: str | None, right: str | None) -> bool:
    left_parts = PurePosixPath(normalize_subpath(left)).parts
    right_parts = PurePosixPath(normalize_subpath(right)).parts
    shorter = min(len(left_parts), len(right_parts))
    return left_parts[:shorter] == right_parts[:shorter]


def filter_fingerprint(path: str | Path) -> str:
    target = Path(path).expanduser()
    data = target.read_bytes()
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dropbox_content_hash(path: Path, block_size: int = 4 * 1024 * 1024) -> str:
    """Return Dropbox's SHA-256-of-4MiB-block-digests content hash."""
    overall = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(block_size):
            overall.update(hashlib.sha256(block).digest())
    return overall.hexdigest()


def content_hashes(path: Path, block_size: int = 4 * 1024 * 1024) -> dict[str, str]:
    """Compute hashes used by local, SFTP, and Dropbox rclone backends once."""
    sha256 = hashlib.sha256()
    sha1 = hashlib.sha1()
    md5 = hashlib.md5()
    dropbox = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(block_size):
            sha256.update(block)
            sha1.update(block)
            md5.update(block)
            dropbox.update(hashlib.sha256(block).digest())
    return {
        "sha256": sha256.hexdigest(),
        "sha1": sha1.hexdigest(),
        "md5": md5.hexdigest(),
        "dropboxhash": dropbox.hexdigest(),
    }


def _entry_for_path(path: Path, *, include_hashes: bool = True) -> dict[str, Any] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if path.is_symlink():
        return {
            "type": "symlink",
            "target": os.readlink(path),
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
        }
    if path.is_dir():
        return {"type": "directory", "size": 0, "mtime_ns": info.st_mtime_ns}
    entry: dict[str, Any] = {
        "type": "file",
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
    }
    if include_hashes:
        entry["hashes"] = content_hashes(path)
    return entry


def local_inventory(
    root: str | Path,
    subpath: str | None = "",
    *,
    include_hashes: bool = True,
    ignore: Callable[[str], bool] | None = None,
    missing_ok: bool = True,
) -> dict[str, dict[str, Any]]:
    """Inventory a local scope without following symlinks."""
    base = Path(root).expanduser()
    if not base.exists():
        if missing_ok:
            return {}
        raise ScopeError(f"local folder does not exist: {base}")
    scope_root = resolve_local_scope(base, subpath, require_exists=True)
    if not scope_root.is_dir():
        raise ScopeError(f"local scope is not a directory: {scope_root}")

    inventory: dict[str, dict[str, Any]] = {}
    for current, dirs, files in os.walk(scope_root, topdown=True, followlinks=False):
        current_path = Path(current)
        kept_dirs: list[str] = []
        for name in sorted(dirs):
            child = current_path / name
            relative = child.relative_to(scope_root).as_posix()
            if name in INTERNAL_WORK_PARTS or (ignore and ignore(relative)):
                continue
            entry = _entry_for_path(child, include_hashes=include_hashes)
            if entry:
                inventory[relative] = entry
            if not child.is_symlink():
                kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in sorted(files):
            child = current_path / name
            relative = child.relative_to(scope_root).as_posix()
            if ignore and ignore(relative):
                continue
            entry = _entry_for_path(child, include_hashes=include_hashes)
            if entry:
                inventory[relative] = entry
    return inventory


def validate_inventory_collisions(inventory: dict[str, dict[str, Any]]) -> None:
    """Reject names that can alias on case-folding or normalizing filesystems."""
    canonical: dict[str, str] = {}
    for path in inventory:
        key = unicodedata.normalize("NFC", path).casefold()
        previous = canonical.get(key)
        if previous is not None and previous != path:
            raise ScopeError(f"filesystem name collision: {previous} and {path}")
        canonical[key] = path


def select_inventory(
    inventory: dict[str, dict[str, Any]], selected_paths: Iterable[str]
) -> dict[str, dict[str, Any]]:
    """Limit an inventory to selections plus their required parent directories."""
    selected = [normalize_subpath(path) for path in selected_paths]
    selected = [path for path in selected if path]
    if not selected:
        return dict(inventory)
    included: set[str] = set()
    for path in inventory:
        if any(path == item or path.startswith(f"{item}/") for item in selected):
            included.add(path)
            parent = PurePosixPath(path).parent
            while parent.as_posix() not in {"", "."}:
                parent_path = parent.as_posix()
                if parent_path in inventory:
                    included.add(parent_path)
                parent = parent.parent
    return {path: inventory[path] for path in sorted(included)}


def parse_rclone_inventory(value: str | list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Normalize `rclone lsjson --recursive --hash` output."""
    entries = json.loads(value) if isinstance(value, str) else value
    if not isinstance(entries, list):
        raise TransferError("rclone inventory is not a JSON list")
    inventory: dict[str, dict[str, Any]] = {}
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        relative = normalize_subpath(str(raw.get("Path") or raw.get("Name") or ""))
        if not relative or relative.split("/", 1)[0] in INTERNAL_WORK_PARTS:
            continue
        if raw.get("IsDir"):
            inventory[relative] = {
                "type": "directory",
                "size": 0,
                "mtime": raw.get("ModTime"),
            }
            continue
        hashes = raw.get("Hashes") if isinstance(raw.get("Hashes"), dict) else {}
        inventory[relative] = {
            "type": "file",
            "size": int(raw.get("Size") or 0),
            "mtime": raw.get("ModTime"),
            "hashes": {str(key).lower(): str(item) for key, item in hashes.items() if item},
            **({"id": str(raw["ID"])} if raw.get("ID") else {}),
        }
    return inventory


def _common_hash(left: dict[str, Any], right: dict[str, Any]) -> tuple[str, str, str] | None:
    left_hashes = left.get("hashes") if isinstance(left.get("hashes"), dict) else {}
    right_hashes = right.get("hashes") if isinstance(right.get("hashes"), dict) else {}
    for name in sorted(set(left_hashes) & set(right_hashes)):
        if left_hashes[name] and right_hashes[name]:
            return name, str(left_hashes[name]), str(right_hashes[name])
    return None


def entries_equal(left: dict[str, Any] | None, right: dict[str, Any] | None) -> tuple[bool, str]:
    if left is None or right is None:
        return left is right, "missing"
    if left.get("type") != right.get("type"):
        return False, "type"
    if left.get("type") == "directory":
        return True, "type"
    if left.get("type") == "symlink":
        return left.get("target") == right.get("target"), "symlink"
    if int(left.get("size", -1)) != int(right.get("size", -1)):
        return False, "size"
    common = _common_hash(left, right)
    if common:
        name, left_hash, right_hash = common
        return left_hash == right_hash, f"hash:{name}"
    left_mtime = left.get("mtime_ns", left.get("mtime"))
    right_mtime = right.get("mtime_ns", right.get("mtime"))
    if left_mtime is not None and right_mtime is not None:
        return left_mtime == right_mtime, "metadata"
    return True, "size-only"


def inventories_equal(left: dict[str, dict[str, Any]], right: dict[str, dict[str, Any]]) -> bool:
    if set(left) != set(right):
        return False
    return all(entries_equal(left[path], right[path])[0] for path in left)


def compare_inventories(
    local: dict[str, dict[str, Any]],
    peer: dict[str, dict[str, Any]],
    baseline: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a structured two-way or three-way folder comparison."""
    results: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    paths = sorted(set(local) | set(peer) | set(baseline or {}))
    for path in paths:
        local_entry = local.get(path)
        peer_entry = peer.get(path)
        peer_equal, confidence = entries_equal(local_entry, peer_entry)
        if baseline is None:
            if peer_equal:
                category = "same"
            elif local_entry is None:
                category = "peer_only"
            elif peer_entry is None:
                category = "local_only"
            else:
                category = "different"
        else:
            base_entry = baseline.get(path)
            local_base, local_confidence = entries_equal(local_entry, base_entry)
            peer_base, peer_confidence = entries_equal(peer_entry, base_entry)
            confidence = confidence if peer_equal else f"local:{local_confidence},peer:{peer_confidence}"
            if local_base and peer_base:
                category = "same"
            elif not local_base and peer_base:
                category = "local_only"
            elif local_base and not peer_base:
                category = "peer_only"
            elif peer_equal:
                category = "same_change"
            else:
                category = "conflict"
        counts[category] = counts.get(category, 0) + 1
        results.append(
            {
                "path": path,
                "category": category,
                "confidence": confidence,
                "baseline": (baseline or {}).get(path),
                "local": local_entry,
                "peer": peer_entry,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "three_way" if baseline is not None else "two_way",
        "counts": counts,
        "results": results,
        "equal": all(item["category"] in {"same", "same_change"} for item in results),
    }


def parse_combined_report(lines: str | Iterable[str]) -> list[dict[str, str]]:
    """Parse rclone combined-report symbols into generation operations."""
    source = lines.splitlines() if isinstance(lines, str) else lines
    operations = {"+": "added", "*": "modified", "-": "removed", "!": "error"}
    parsed: list[dict[str, str]] = []
    for raw in source:
        line = raw.rstrip("\n")
        if len(line) < 3 or line[1] != " " or line[0] not in operations:
            continue
        try:
            path = normalize_subpath(line[2:])
        except ScopeError:
            continue
        if path:
            parsed.append({"path": path, "operation": operations[line[0]]})
    return parsed


def generation_record(
    *,
    machine_id: str,
    install_id: str,
    profile_id: str,
    folder_id: str,
    filter_policy: str,
    changes: Iterable[dict[str, Any]],
    parent_generation: str | None = None,
    generation_id: str | None = None,
    completed_at: str | None = None,
) -> dict[str, Any]:
    normalized_changes: list[dict[str, Any]] = []
    for raw_change in changes:
        change = dict(raw_change)
        change["path"] = normalize_subpath(str(change.get("path") or ""))
        if not change["path"]:
            raise ScopeError("generation change path is required")
        normalized_changes.append(change)
    return {
        "schema_version": SCHEMA_VERSION,
        "generation_id": generation_id or new_id("gen"),
        "parent_generation": parent_generation,
        "completed_at": completed_at or now_iso(),
        "machine_id": machine_id,
        "install_id": install_id,
        "profile_id": profile_id,
        "folder_id": folder_id,
        "filter_fingerprint": filter_policy,
        "changes": sorted(normalized_changes, key=lambda item: str(item["path"])),
        "complete": True,
    }


def generation_remote_dir(remote_base: str, machine_id: str, folder_id: str) -> str:
    return f"{remote_base.rstrip('/')}/.manifests/{machine_id}/{folder_id}"


def _path_depth(path: str) -> int:
    return len(PurePosixPath(path).parts)


def _has_prefix(path: str, prefixes: set[str]) -> bool:
    parts = PurePosixPath(path).parts
    return any(parts[: len(PurePosixPath(prefix).parts)] == PurePosixPath(prefix).parts for prefix in prefixes)


def _move_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)


def _safe_suffix(path: Path, source_label: str, timestamp: str) -> Path:
    safe_source = "-".join(part for part in source_label.replace("_", "-").split("-") if part) or "peer"
    suffix = f".from-{safe_source}-{timestamp}"
    candidate = path.with_name(f"{path.stem}{suffix}{path.suffix}") if path.suffix else path.with_name(f"{path.name}{suffix}")
    counter = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}{suffix}-{counter}{path.suffix}") if path.suffix else path.with_name(f"{path.name}{suffix}-{counter}")
        counter += 1
    return candidate


class JobStore:
    """Durable receive-job store with adjacent same-filesystem work data."""

    def __init__(self, state_root: str | Path):
        self.state_root = Path(state_root).expanduser()
        self.index_dir = self.state_root / "jobs"

    def _index_path(self, job_id: str) -> Path:
        return self.index_dir / f"{job_id}.json"

    @staticmethod
    def _adjacent_path(job: dict[str, Any]) -> Path:
        return Path(job["paths"]["job"])

    def save(self, job: dict[str, Any]) -> dict[str, Any]:
        job["updated_at"] = now_iso()
        atomic_write_json(self._adjacent_path(job), job)
        atomic_write_json(self._index_path(str(job["id"])), job)
        return job

    def load(self, job_id: str) -> dict[str, Any]:
        path = self._index_path(job_id)
        if not path.exists():
            raise TransferError(f"receive job not found: {job_id}")
        return json.loads(path.read_text())

    def list(self) -> list[dict[str, Any]]:
        if not self.index_dir.exists():
            return []
        jobs = []
        for path in self.index_dir.glob("*.json"):
            try:
                jobs.append(json.loads(path.read_text()))
            except (OSError, json.JSONDecodeError):
                continue
        return sorted(jobs, key=lambda item: str(item.get("created_at", "")), reverse=True)

    def create(
        self,
        *,
        source: str,
        destination: str | Path,
        source_label: str = "peer",
        selected_paths: Iterable[str] = (),
        mode: str = "receive",
        source_inventory: dict[str, dict[str, Any]] | None = None,
        destination_inventory: dict[str, dict[str, Any]] | None = None,
        baseline_inventory: dict[str, dict[str, Any]] | None = None,
        source_generation: str | None = None,
        link_id: str | None = None,
    ) -> dict[str, Any]:
        raw_target = Path(destination).expanduser()
        if raw_target.is_symlink():
            raise ScopeError(f"destination may not be a symlink: {raw_target}")
        target = raw_target.resolve(strict=False)
        if target.exists() and not target.is_dir():
            raise TransferError(f"destination is not a directory: {target}")
        parent = target.parent
        if not parent.exists() or not parent.is_dir():
            raise TransferError(f"destination parent does not exist: {parent}")
        work_root = parent / WORK_DIR_NAME
        work_root.mkdir(parents=True, exist_ok=True)
        if work_root.is_symlink():
            raise ScopeError(f"Safe Sync work directory may not be a symlink: {work_root}")
        destination_device = target.stat().st_dev if target.exists() else parent.stat().st_dev
        if work_root.stat().st_dev != destination_device:
            raise TransferError("Safe Sync work directory is not on the destination filesystem")
        job_id = new_id("job")
        stage = work_root / "staging" / job_id / "payload"
        checkpoint = work_root / "checkpoints" / job_id
        adjacent_job = work_root / "jobs" / f"{job_id}.json"
        stage.mkdir(parents=True, exist_ok=False)
        checkpoint.mkdir(parents=True, exist_ok=False)
        normalized_selections = [normalize_subpath(path) for path in selected_paths]
        source_entries = select_inventory(source_inventory or {}, normalized_selections)
        validate_inventory_collisions(source_entries)
        destination_entries = destination_inventory
        if destination_entries is None:
            destination_entries = local_inventory(target, include_hashes=True) if target.exists() else {}
        comparison_destination = select_inventory(destination_entries, normalized_selections)
        comparison_baseline = select_inventory(baseline_inventory or {}, normalized_selections) if baseline_inventory is not None else None
        comparison = compare_inventories(comparison_destination, source_entries, comparison_baseline)
        job = {
            "schema_version": SCHEMA_VERSION,
            "id": job_id,
            "mode": mode,
            "status": "planned",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "source": source,
            "source_label": source_label,
            "source_generation": source_generation,
            "link_id": link_id,
            "destination": str(target),
            "selected_paths": normalized_selections,
            "source_inventory": source_entries,
            "destination_inventory": destination_entries,
            "baseline_inventory": baseline_inventory,
            "comparison": comparison,
            "staged_inventory": {},
            "actions": [],
            "paths": {
                "work_root": str(work_root),
                "staging": str(stage),
                "checkpoint": str(checkpoint),
                "job": str(adjacent_job),
            },
        }
        return self.save(job)

    def mark_staged(self, job_id: str) -> dict[str, Any]:
        job = self.load(job_id)
        stage = Path(job["paths"]["staging"])
        staged = local_inventory(stage, include_hashes=True, missing_ok=False)
        job["staged_inventory"] = staged
        source_inventory = job.get("source_inventory") or {}
        missing = []
        different = []
        for path, source_entry in source_inventory.items():
            if job.get("selected_paths") and not any(
                path == selected or path.startswith(f"{selected.rstrip('/')}/")
                for selected in job["selected_paths"]
            ):
                continue
            staged_entry = staged.get(path)
            if staged_entry is None:
                if source_entry.get("type") != "directory":
                    missing.append(path)
                continue
            equal, confidence = entries_equal(source_entry, staged_entry)
            if not equal and confidence.startswith("hash:"):
                different.append(path)
            elif source_entry.get("type") == "file" and int(source_entry.get("size", -1)) != int(staged_entry.get("size", -1)):
                different.append(path)
        if missing or different:
            job["status"] = "verification_failed"
            job["verification"] = {"missing": missing, "different": different}
        else:
            job["status"] = "ready"
            job["verification"] = {"missing": [], "different": [], "verified_at": now_iso()}
        return self.save(job)

    def _revalidate_destination(self, job: dict[str, Any]) -> None:
        destination = Path(job["destination"])
        current = local_inventory(destination, include_hashes=True) if destination.exists() else {}
        if not inventories_equal(current, job.get("destination_inventory") or {}):
            job["status"] = "needs_refresh"
            self.save(job)
            refreshed = compare_inventories(current, job.get("source_inventory") or {}, job.get("baseline_inventory"))
            paths = [item["path"] for item in refreshed["results"] if item["category"] not in {"same", "same_change"}]
            raise JobConflictError("destination changed after review; refresh comparison", paths)

    def _revalidate_staging(self, job: dict[str, Any]) -> None:
        stage = Path(job["paths"]["staging"])
        current = local_inventory(stage, include_hashes=True, missing_ok=False)
        if not inventories_equal(current, job.get("staged_inventory") or {}):
            job["status"] = "needs_refresh"
            self.save(job)
            raise JobConflictError("staged files changed after verification; restage the job")

    @staticmethod
    def _default_action(item: dict[str, Any]) -> str | None:
        category = item["category"]
        local = item.get("local")
        peer = item.get("peer")
        if category in {"same", "same_change"}:
            return "same"
        if category == "local_only":
            return "keep_local"
        if category == "peer_only" and local is None and peer is not None:
            return "add"
        return None

    def _build_actions(self, job: dict[str, Any], policies: dict[str, str]) -> list[dict[str, Any]]:
        allowed = {"add", "keep_local", "keep_both", "replace", "delete", "leave_staged", "same"}
        actions: list[dict[str, Any]] = []
        unresolved: list[str] = []
        for item in job["comparison"]["results"]:
            path = item["path"]
            action = policies.get(path) or self._default_action(item)
            if not action:
                unresolved.append(path)
                continue
            if action not in allowed:
                raise TransferError(f"unknown apply policy for {path}: {action}")
            if action in {"add", "replace", "keep_both"} and item.get("peer") is None:
                raise TransferError(f"{action} requires a peer file: {path}")
            if action == "delete" and item.get("local") is None:
                raise TransferError(f"delete requires a local path: {path}")
            actions.append(
                {
                    "path": path,
                    "policy": action,
                    "state": "planned",
                    "local_before": item.get("local"),
                    "peer": item.get("peer"),
                }
            )
        if unresolved:
            raise JobConflictError("explicit decisions required", unresolved)
        return sorted(actions, key=lambda item: (_path_depth(item["path"]), item["path"]))

    def apply(self, job_id: str, policies: dict[str, str] | None = None) -> dict[str, Any]:
        job = self.load(job_id)
        if job.get("status") not in {"ready", "needs_review", "interrupted"}:
            raise TransferError(f"job is not ready to apply: {job.get('status')}")
        self._revalidate_staging(job)
        self._revalidate_destination(job)
        actions = self._build_actions(job, policies or {})
        destination = Path(job["destination"])
        destination.mkdir(parents=True, exist_ok=True)
        stage = Path(job["paths"]["staging"])
        checkpoint = Path(job["paths"]["checkpoint"])
        job["actions"] = actions
        job["status"] = "applying"
        self.save(job)

        covered: set[str] = set()
        timestamp = utc_stamp()
        try:
            for action in job["actions"]:
                relative = action["path"]
                if _has_prefix(relative, covered):
                    action["state"] = "covered"
                    action["covered_by"] = next(prefix for prefix in covered if _has_prefix(relative, {prefix}))
                    self.save(job)
                    continue
                policy = action["policy"]
                source = stage.joinpath(*PurePosixPath(relative).parts)
                target = destination.joinpath(*PurePosixPath(relative).parts)
                old = checkpoint / "old" / Path(*PurePosixPath(relative).parts)

                if policy in {"same", "keep_local", "leave_staged"}:
                    action["state"] = "skipped"
                    self.save(job)
                    continue

                if policy == "delete":
                    if target.exists() or target.is_symlink():
                        _move_atomic(target, old)
                        action["checkpoint_path"] = str(old)
                        action["state"] = "old_checkpointed"
                        if old.is_dir() and not old.is_symlink():
                            covered.add(relative)
                    else:
                        action["state"] = "skipped"
                    self.save(job)
                    continue

                if not source.exists() and not source.is_symlink():
                    raise TransferError(f"staged path is missing: {relative}")

                if policy == "keep_both":
                    installed = _safe_suffix(target, str(job.get("source_label") or "peer"), timestamp)
                    action["installed_path"] = str(installed)
                    self.save(job)
                    _move_atomic(source, installed)
                    action["installed_fingerprint"] = _entry_for_path(installed, include_hashes=True)
                    if installed.is_dir() and not installed.is_symlink():
                        action["installed_inventory"] = local_inventory(installed, include_hashes=True)
                    action["state"] = "installed"
                    if installed.is_dir() and not installed.is_symlink():
                        covered.add(relative)
                    self.save(job)
                    continue

                if policy == "add" and (target.exists() or target.is_symlink()):
                    raise JobConflictError("destination appeared before add", [relative])

                action["installed_path"] = str(target)
                self.save(job)

                if policy == "replace" and (target.exists() or target.is_symlink()):
                    _move_atomic(target, old)
                    action["checkpoint_path"] = str(old)
                    action["state"] = "old_checkpointed"
                    self.save(job)

                if source.is_dir() and not source.is_symlink() and not target.exists():
                    target.mkdir(parents=True, exist_ok=False)
                    action["state"] = "installed"
                    action["installed_path"] = str(target)
                    action["installed_fingerprint"] = _entry_for_path(target, include_hashes=True)
                    action["installed_inventory"] = {}
                    self.save(job)
                    continue

                _move_atomic(source, target)
                action["installed_path"] = str(target)
                action["installed_fingerprint"] = _entry_for_path(target, include_hashes=True)
                if target.is_dir() and not target.is_symlink():
                    action["installed_inventory"] = local_inventory(target, include_hashes=True)
                action["state"] = "installed"
                if target.is_dir() and not target.is_symlink():
                    covered.add(relative)
                self.save(job)
        except BaseException as exc:
            job["status"] = "interrupted"
            job["error"] = str(exc)
            self.save(job)
            raise

        for directory in sorted(stage.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if directory.is_dir() and not directory.is_symlink():
                try:
                    directory.rmdir()
                except OSError:
                    pass
        job["status"] = "complete"
        job["completed_at"] = now_iso()
        job.pop("error", None)
        return self.save(job)

    def commit_clone(self, job_id: str) -> dict[str, Any]:
        job = self.load(job_id)
        if job.get("mode") != "clone" or job.get("status") != "ready":
            raise TransferError("clone job is not ready")
        self._revalidate_staging(job)
        destination = Path(job["destination"])
        if destination.exists() and any(destination.iterdir()):
            raise JobConflictError("clone destination is not empty", [str(destination)])
        if destination.exists():
            destination.rmdir()
        stage = Path(job["paths"]["staging"])
        _move_atomic(stage, destination)
        job["actions"] = [
            {
                "path": "",
                "policy": "clone",
                "state": "installed",
                "installed_path": str(destination),
                "installed_inventory": local_inventory(destination, include_hashes=True),
            }
        ]
        job["status"] = "complete"
        job["completed_at"] = now_iso()
        return self.save(job)

    def reconcile(self, job_id: str) -> dict[str, Any]:
        job = self.load(job_id)
        if job.get("status") == "applying":
            job["status"] = "interrupted"
            self.save(job)
        if job.get("status") != "interrupted":
            return job
        unresolved: list[str] = []
        for action in job.get("actions") or []:
            state = action.get("state")
            policy = action.get("policy")
            if state in {"installed", "deleted", "covered", "skipped", "rolled_back"}:
                continue
            relative = action["path"]
            source = Path(job["paths"]["staging"]).joinpath(*PurePosixPath(relative).parts)
            normal_target = Path(job["destination"]).joinpath(*PurePosixPath(relative).parts)
            target = Path(action.get("installed_path") or normal_target)
            old = Path(action.get("checkpoint_path") or Path(job["paths"]["checkpoint"]) / "old" / Path(*PurePosixPath(relative).parts))

            if policy in {"same", "keep_local", "leave_staged"}:
                action["state"] = "skipped"
                self.save(job)
                continue

            if policy in {"replace", "delete"} and old.exists() and not normal_target.exists():
                action["checkpoint_path"] = str(old)
                if policy == "delete":
                    action["state"] = "deleted"
                    self.save(job)
                    continue
                if source.exists():
                    _move_atomic(source, normal_target)
                    target = normal_target
                    action["installed_path"] = str(target)

            if policy in {"add", "replace", "keep_both"} and target.exists() and not source.exists():
                current = _entry_for_path(target, include_hashes=True)
                equal, _confidence = entries_equal(current, action.get("peer"))
                if equal:
                    action["installed_fingerprint"] = current
                    if target.is_dir() and not target.is_symlink():
                        action["installed_inventory"] = local_inventory(target, include_hashes=True)
                    action["state"] = "installed"
                    self.save(job)
                    continue

            unresolved.append(relative)
        job["status"] = "needs_review" if unresolved else "complete"
        job["reconcile_unresolved"] = unresolved
        if not unresolved:
            job["completed_at"] = now_iso()
        return self.save(job)

    def rollback(self, job_id: str) -> dict[str, Any]:
        job = self.load(job_id)
        if job.get("status") not in {"complete", "interrupted", "needs_review"}:
            raise TransferError(f"job cannot be rolled back from {job.get('status')}")
        destination = Path(job["destination"])
        checkpoint = Path(job["paths"]["checkpoint"])
        conflicts: list[str] = []
        for action in reversed(job.get("actions") or []):
            if action.get("state") in {"covered", "skipped", "planned"}:
                continue
            relative = action.get("path") or ""
            policy = action.get("policy")
            installed_path = Path(action.get("installed_path") or destination.joinpath(*PurePosixPath(relative).parts))
            old_path = Path(action["checkpoint_path"]) if action.get("checkpoint_path") else None
            installed_expected = action.get("installed_fingerprint")
            installed_current = _entry_for_path(installed_path, include_hashes=True)
            installed_unchanged = entries_equal(installed_current, installed_expected)[0]
            if installed_path.is_dir() and not installed_path.is_symlink() and "installed_inventory" in action:
                installed_unchanged = inventories_equal(
                    local_inventory(installed_path, include_hashes=True),
                    action["installed_inventory"],
                )

            if policy in {"add", "keep_both", "clone"} and installed_path.exists():
                if installed_unchanged:
                    recovery = checkpoint / "applied" / (Path(*PurePosixPath(relative).parts) if relative else Path("clone"))
                    _move_atomic(installed_path, recovery)
                    action["rollback_recovery_path"] = str(recovery)
                    action["state"] = "rolled_back"
                else:
                    conflicts.append(relative or str(installed_path))
                self.save(job)
                continue

            if policy == "replace" and old_path and old_path.exists():
                if not installed_path.exists():
                    _move_atomic(old_path, destination.joinpath(*PurePosixPath(relative).parts))
                    action["state"] = "rolled_back"
                elif installed_unchanged:
                    applied = checkpoint / "applied" / Path(*PurePosixPath(relative).parts)
                    _move_atomic(installed_path, applied)
                    _move_atomic(old_path, destination.joinpath(*PurePosixPath(relative).parts))
                    action["rollback_recovery_path"] = str(applied)
                    action["state"] = "rolled_back"
                else:
                    recovered = _safe_suffix(destination.joinpath(*PurePosixPath(relative).parts), "recovered", utc_stamp())
                    _move_atomic(old_path, recovered)
                    action["rollback_recovery_path"] = str(recovered)
                    action["state"] = "rollback_conflict"
                    conflicts.append(relative)
                self.save(job)
                continue

            if policy == "delete" and old_path and old_path.exists():
                target = destination.joinpath(*PurePosixPath(relative).parts)
                if not target.exists():
                    _move_atomic(old_path, target)
                    action["state"] = "rolled_back"
                else:
                    recovered = _safe_suffix(target, "recovered", utc_stamp())
                    _move_atomic(old_path, recovered)
                    action["rollback_recovery_path"] = str(recovered)
                    action["state"] = "rollback_conflict"
                    conflicts.append(relative)
                self.save(job)

        job["status"] = "rollback_conflict" if conflicts else "rolled_back"
        job["rollback_conflicts"] = conflicts
        job["rolled_back_at"] = now_iso()
        return self.save(job)


class LinkStore:
    """Local granular link records and accepted baseline inventories."""

    def __init__(self, state_root: str | Path):
        self.root = Path(state_root).expanduser() / "links"
        self.index = self.root / "links.json"

    def list(self) -> list[dict[str, Any]]:
        if not self.index.exists():
            return []
        value = json.loads(self.index.read_text())
        return list(value.get("links") or [])

    def _save_all(self, links: list[dict[str, Any]]) -> None:
        atomic_write_json(self.index, {"schema_version": SCHEMA_VERSION, "links": links})

    def get(self, link_id: str) -> dict[str, Any]:
        for link in self.list():
            if link.get("id") == link_id:
                return link
        raise TransferError(f"linked folder not found: {link_id}")

    def update(self, link_id: str, **updates: Any) -> dict[str, Any]:
        links = self.list()
        for link in links:
            if link.get("id") == link_id:
                link.update(updates)
                link["updated_at"] = now_iso()
                self._save_all(links)
                return link
        raise TransferError(f"linked folder not found: {link_id}")

    def accept_baseline(
        self,
        link_id: str,
        inventory: dict[str, dict[str, Any]],
        *,
        local_generation: str | None = None,
        peer_generation: str | None = None,
    ) -> dict[str, Any]:
        links = self.list()
        for link in links:
            if link.get("id") != link_id:
                continue
            baseline_path = Path(link["baseline"]["inventory_path"])
            atomic_write_json(baseline_path, inventory)
            link["baseline"].update(
                {
                    "accepted_at": now_iso(),
                    "local_generation": local_generation,
                    "peer_generation": peer_generation,
                }
            )
            link["status"] = "up_to_date"
            link.pop("pending_peer_generation", None)
            link["updated_at"] = now_iso()
            self._save_all(links)
            return link
        raise TransferError(f"linked folder not found: {link_id}")

    def add(
        self,
        *,
        label: str,
        local_profile_id: str,
        local_folder_id: str,
        local_subpath: str,
        peer_machine_id: str,
        peer_install_id: str,
        peer_folder_id: str,
        peer_subpath: str,
        local_filter_fingerprint: str,
        peer_filter_fingerprint: str,
        local_inventory_value: dict[str, dict[str, Any]],
        peer_inventory_value: dict[str, dict[str, Any]],
        local_generation: str | None = None,
        peer_generation: str | None = None,
    ) -> dict[str, Any]:
        local_scope = normalize_subpath(local_subpath)
        peer_scope = normalize_subpath(peer_subpath)
        if local_filter_fingerprint != peer_filter_fingerprint:
            raise JobConflictError("linked folders use different filter policies")
        if not inventories_equal(local_inventory_value, peer_inventory_value):
            raise JobConflictError("linked folders must converge before activation")
        links = self.list()
        for current in links:
            if current["local"]["profile_id"] == local_profile_id and current["local"]["folder_id"] == local_folder_id:
                if scopes_overlap(current["local"].get("subpath"), local_scope):
                    raise ScopeError("linked local scopes may not overlap")
        link_id = new_id("link")
        baseline_path = self.root / "baselines" / f"{link_id}.json"
        atomic_write_json(baseline_path, local_inventory_value)
        link = {
            "schema_version": SCHEMA_VERSION,
            "id": link_id,
            "label": label.strip() or local_scope or local_folder_id,
            "status": "up_to_date",
            "local": {
                "profile_id": local_profile_id,
                "folder_id": local_folder_id,
                "subpath": local_scope,
            },
            "peer": {
                "machine_id": peer_machine_id,
                "install_id": peer_install_id,
                "folder_id": peer_folder_id,
                "subpath": peer_scope,
            },
            "filter_fingerprint": local_filter_fingerprint,
            "baseline": {
                "accepted_at": now_iso(),
                "local_generation": local_generation,
                "peer_generation": peer_generation,
                "inventory_path": str(baseline_path),
            },
            "notifications": True,
        }
        links.append(link)
        self._save_all(links)
        return link

    def remove(self, link_id: str) -> bool:
        links = self.list()
        kept = [link for link in links if link.get("id") != link_id]
        if len(kept) == len(links):
            return False
        self._save_all(kept)
        return True

    def status(
        self,
        link: dict[str, Any],
        *,
        local_inventory_value: dict[str, dict[str, Any]],
        peer_inventory_value: dict[str, dict[str, Any]],
        peer_install_id: str,
        peer_filter_fingerprint: str,
    ) -> dict[str, Any]:
        if peer_install_id != link["peer"]["install_id"]:
            return {"status": "peer_replaced", "comparison": None}
        if peer_filter_fingerprint != link["filter_fingerprint"]:
            return {"status": "filter_changed", "comparison": None}
        baseline = json.loads(Path(link["baseline"]["inventory_path"]).read_text())
        comparison = compare_inventories(local_inventory_value, peer_inventory_value, baseline)
        categories = comparison["counts"]
        if categories.get("conflict"):
            status = "conflict"
        elif categories.get("local_only") and categories.get("peer_only"):
            status = "changes_on_both"
        elif categories.get("peer_only"):
            status = "peer_changes"
        elif categories.get("local_only"):
            status = "local_changes"
        else:
            status = "up_to_date"
        return {"status": status, "comparison": comparison}
