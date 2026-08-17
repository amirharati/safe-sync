"""Minimal Dropbox revision client backed by Safe Sync's rclone authorization."""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import PurePosixPath
from typing import Any, Callable


API_ROOT = "https://api.dropboxapi.com/2"
CONTENT_ROOT = "https://content.dropboxapi.com/2"
REVISION_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class DropboxHistoryError(RuntimeError):
    """A safe, non-secret-bearing Dropbox history failure."""


def split_remote(remote: str) -> tuple[str, str]:
    if ":" not in remote:
        raise DropboxHistoryError("Dropbox remote must use rclone remote:path syntax")
    name, path = remote.split(":", 1)
    if not name or name.startswith(":"):
        raise DropboxHistoryError("Dropbox history requires a named rclone remote")
    return name, path.strip("/")


def dropbox_path(remote_root: str, relative_path: str) -> str:
    _name, root = split_remote(remote_root)
    relative = relative_path.strip().replace("\\", "/").strip("/")
    candidate = PurePosixPath(relative)
    if not relative or candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise DropboxHistoryError("a safe relative file path is required")
    joined = "/".join(part for part in (root, candidate.as_posix()) if part)
    return f"/{joined}"


def dropbox_root_path(remote_root: str) -> str:
    _name, root = split_remote(remote_root)
    return f"/{root}" if root else ""


def _run_rclone_json(
    rclone_binary: str,
    environment: dict[str, str] | None,
    arguments: list[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    result = runner(
        [rclone_binary, *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=180,
        env=environment,
    )
    if result.returncode != 0:
        raise DropboxHistoryError("Dropbox authorization refresh failed; reconnect Dropbox and try again")
    try:
        value = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise DropboxHistoryError("rclone returned invalid Dropbox authorization data") from exc
    if not isinstance(value, dict):
        raise DropboxHistoryError("rclone returned invalid Dropbox authorization data")
    return value


def credentials_from_rclone(
    rclone_binary: str,
    environment: dict[str, str] | None,
    remote: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, str]:
    """Refresh rclone's OAuth token, then read it without logging the secret."""
    remote_name, _path = split_remote(remote)
    refresh = runner(
        [rclone_binary, "about", f"{remote_name}:", "--json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=180,
        env=environment,
    )
    if refresh.returncode != 0:
        raise DropboxHistoryError("Dropbox authorization refresh failed; reconnect Dropbox and try again")
    config = _run_rclone_json(rclone_binary, environment, ["config", "dump"], runner=runner)
    section = config.get(remote_name)
    if not isinstance(section, dict) or section.get("type") != "dropbox":
        raise DropboxHistoryError(f"rclone remote {remote_name!r} is not a Dropbox remote")
    raw_token = section.get("token")
    try:
        token = json.loads(raw_token) if isinstance(raw_token, str) else raw_token
    except json.JSONDecodeError as exc:
        raise DropboxHistoryError("Dropbox token in rclone configuration is invalid") from exc
    access_token = token.get("access_token") if isinstance(token, dict) else None
    if not isinstance(access_token, str) or not access_token:
        raise DropboxHistoryError("Dropbox access token is unavailable; reconnect Dropbox and try again")
    result = {"access_token": access_token}
    namespace = section.get("root_namespace")
    if isinstance(namespace, str) and namespace:
        result["root_namespace"] = namespace
    return result


def _headers(credentials: dict[str, str]) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {credentials['access_token']}"}
    namespace = credentials.get("root_namespace")
    if namespace:
        headers["Dropbox-API-Path-Root"] = json.dumps({".tag": "namespace_id", "namespace_id": namespace})
    return headers


def _safe_http_error(exc: urllib.error.HTTPError) -> DropboxHistoryError:
    retry_after = exc.headers.get("Retry-After") if exc.headers else None
    suffix = f"; retry after {retry_after} seconds" if retry_after else ""
    detail = ""
    try:
        payload = json.loads(exc.read().decode("utf-8", errors="replace"))
        summary = payload.get("error_summary") if isinstance(payload, dict) else None
        if isinstance(summary, str):
            detail = f": {summary.split('/', 1)[0]}"
    except (OSError, ValueError):
        pass
    return DropboxHistoryError(f"Dropbox history request failed ({exc.code}){detail}{suffix}")


def list_revisions(
    credentials: dict[str, str],
    path: str,
    *,
    limit: int = 30,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    payload = json.dumps(
        {
            "path": path,
            "mode": {".tag": "path"},
            "limit": max(1, min(int(limit), 100)),
            "include_restorable_info": True,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{API_ROOT}/files/list_revisions",
        data=payload,
        headers={**_headers(credentials), "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener(request, timeout=60) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise _safe_http_error(exc) from exc
    except (OSError, ValueError) as exc:
        raise DropboxHistoryError(f"Dropbox history request failed: {type(exc).__name__}") from exc
    entries = []
    for raw in value.get("entries") or []:
        if not isinstance(raw, dict) or not raw.get("rev"):
            continue
        entries.append(
            {
                "rev": str(raw["rev"]),
                "name": str(raw.get("name") or ""),
                "path_display": str(raw.get("path_display") or path),
                "id": str(raw.get("id") or ""),
                "server_modified": raw.get("server_modified"),
                "client_modified": raw.get("client_modified"),
                "size": int(raw.get("size") or 0),
                "content_hash": str(raw.get("content_hash") or ""),
                "is_downloadable": bool(raw.get("is_downloadable", True)),
                "is_restorable": bool(raw.get("is_restorable", True)),
            }
        )
    return {
        "path": path,
        "is_deleted": bool(value.get("is_deleted", False)),
        "server_deleted": value.get("server_deleted"),
        "has_more": bool(value.get("has_more", False)),
        "entries": entries,
    }


def list_folder_snapshot(
    credentials: dict[str, str],
    path: str,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    """List a complete current Dropbox folder with immutable file revisions."""
    endpoint = f"{API_ROOT}/files/list_folder"
    payload: dict[str, Any] = {
        "path": path,
        "recursive": True,
        "include_deleted": False,
        "include_non_downloadable_files": False,
    }
    entries: dict[str, dict[str, Any]] = {}
    cursor: str | None = None
    while True:
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={**_headers(credentials), "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with opener(request, timeout=120) as response:
                value = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise _safe_http_error(exc) from exc
        except (OSError, ValueError) as exc:
            raise DropboxHistoryError(f"Dropbox snapshot listing failed: {type(exc).__name__}") from exc
        for raw in value.get("entries") or []:
            if not isinstance(raw, dict):
                continue
            display = str(raw.get("path_display") or "")
            prefix = path.rstrip("/")
            if prefix and display.casefold().startswith(f"{prefix}/".casefold()):
                relative = display[len(prefix) + 1 :]
            elif not prefix:
                relative = display.lstrip("/")
            else:
                continue
            relative = PurePosixPath(relative).as_posix().strip("/")
            if not relative or any(part in {"", ".", ".."} for part in PurePosixPath(relative).parts):
                continue
            tag = str(raw.get(".tag") or "")
            if tag == "folder":
                entries[relative] = {"type": "directory", "size": 0, "mtime": None}
            elif tag == "file" and raw.get("rev"):
                content_hash = str(raw.get("content_hash") or "")
                entries[relative] = {
                    "type": "file",
                    "size": int(raw.get("size") or 0),
                    "mtime": raw.get("server_modified"),
                    "hashes": {"dropboxhash": content_hash} if content_hash else {},
                    "id": str(raw.get("id") or ""),
                    "revision": str(raw["rev"]),
                }
        cursor = str(value.get("cursor") or "") or cursor
        if not value.get("has_more"):
            break
        if not cursor:
            raise DropboxHistoryError("Dropbox snapshot listing returned no continuation cursor")
        endpoint = f"{API_ROOT}/files/list_folder/continue"
        payload = {"cursor": cursor}
    return {"path": path, "cursor": cursor, "entries": {key: entries[key] for key in sorted(entries)}}


def download_revision(
    credentials: dict[str, str],
    revision: str,
    destination: Any,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    if not REVISION_RE.fullmatch(revision):
        raise DropboxHistoryError("invalid Dropbox revision identity")
    request = urllib.request.Request(
        f"{CONTENT_ROOT}/files/download",
        headers={
            **_headers(credentials),
            "Dropbox-API-Arg": json.dumps({"path": f"rev:{revision}"}),
        },
        method="POST",
    )
    try:
        with opener(request, timeout=300) as response:
            metadata = json.loads(response.headers.get("Dropbox-API-Result", "{}"))
            with destination.open("wb") as handle:
                while chunk := response.read(1024 * 1024):
                    handle.write(chunk)
    except urllib.error.HTTPError as exc:
        raise _safe_http_error(exc) from exc
    except (OSError, ValueError) as exc:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise DropboxHistoryError(f"Dropbox revision download failed: {type(exc).__name__}") from exc
    return metadata if isinstance(metadata, dict) else {}
