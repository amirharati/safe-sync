"""Local daemon API for Safe Sync."""

from __future__ import annotations

import json
import os
import socket
import socketserver
import threading
from pathlib import Path
from typing import Any


class DaemonApiState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status: dict[str, Any] = {"state": "starting"}
        self._backup_requested = False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def update(self, **updates: Any) -> dict[str, Any]:
        with self._lock:
            self._status.update(updates)
            return dict(self._status)

    def request_backup(self) -> None:
        with self._lock:
            self._backup_requested = True

    def consume_backup_request(self) -> bool:
        with self._lock:
            requested = self._backup_requested
            self._backup_requested = False
            return requested


class _DaemonApiHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline()
        if not raw:
            return
        try:
            request = json.loads(raw.decode("utf-8"))
            command = str(request.get("command") or "")
            if command == "status":
                response = {"ok": True, "status": self.server.api_state.snapshot()}
            elif command == "backup":
                self.server.api_state.request_backup()
                response = {"ok": True, "queued": True}
            elif command == "ping":
                response = {"ok": True, "pong": True}
            else:
                response = {"ok": False, "error": f"unknown command: {command}"}
        except Exception as exc:  # pragma: no cover - defensive server path
            response = {"ok": False, "error": str(exc)}
        self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))


class _UnixJsonServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, socket_path: str, api_state: DaemonApiState):
        self.api_state = api_state
        super().__init__(socket_path, _DaemonApiHandler)


class DaemonApiServer:
    def __init__(self, socket_path: Path, api_state: DaemonApiState):
        self.socket_path = socket_path
        self.api_state = api_state
        self._server: _UnixJsonServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        self._server = _UnixJsonServer(str(self.socket_path), self.api_state)
        self._thread = threading.Thread(target=self._server.serve_forever, name="safe-sync-api", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self.socket_path.exists():
            self.socket_path.unlink()
        self._thread = None


def api_request(socket_path: Path, payload: dict[str, Any], timeout_seconds: float = 5.0) -> dict[str, Any]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout_seconds)
    try:
        client.connect(os.fspath(socket_path))
        client.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        data = b""
        while not data.endswith(b"\n"):
            chunk = client.recv(65536)
            if not chunk:
                break
            data += chunk
    finally:
        client.close()
    if not data:
        raise RuntimeError("daemon API returned no data")
    return json.loads(data.decode("utf-8"))
