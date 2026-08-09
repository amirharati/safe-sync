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
        self._wake_event = threading.Event()
        self._status: dict[str, Any] = {"state": "starting"}
        self._backup_requested = False
        self._pull_request: dict[str, Any] | None = None
        self._job_operation_request: dict[str, Any] | None = None
        self._query_request: dict[str, Any] | None = None

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
        self.wake()

    def consume_backup_request(self) -> bool:
        with self._lock:
            requested = self._backup_requested
            self._backup_requested = False
            return requested

    def request_pull(self, source: str, destination: str, dry_run: bool, selected_paths: list[str] | None = None) -> bool:
        """Queue one explicit remote-to-local transfer for the daemon."""
        with self._lock:
            if self._pull_request is not None:
                return False
            self._pull_request = {
                "source": source,
                "destination": destination,
                "dry_run": dry_run,
                "selected_paths": list(selected_paths or []),
            }
            self._status["queued_transfer"] = True
        self.wake()
        return True

    def request_receive(
        self,
        source: str,
        destination: str,
        selected_paths: list[str] | None = None,
        source_label: str = "peer",
        mode: str = "receive",
        baseline_inventory: dict[str, Any] | None = None,
        source_generation: str | None = None,
        link_id: str | None = None,
    ) -> bool:
        """Queue a safe staged receive in the same single transfer lane."""
        with self._lock:
            if self._pull_request is not None:
                return False
            self._pull_request = {
                "source": source,
                "destination": destination,
                "dry_run": False,
                "selected_paths": list(selected_paths or []),
                "source_label": source_label,
                "mode": mode,
                "safe_receive": True,
                "baseline_inventory": baseline_inventory,
                "source_generation": source_generation,
                "link_id": link_id,
            }
            self._status["queued_transfer"] = True
        self.wake()
        return True

    def consume_pull_request(self) -> dict[str, Any] | None:
        with self._lock:
            request = self._pull_request
            self._pull_request = None
            self._status["queued_transfer"] = False
            return request

    def has_pull_request(self) -> bool:
        with self._lock:
            return self._pull_request is not None

    def request_job_operation(self, operation: str, job_id: str, policies: dict[str, str] | None = None) -> bool:
        with self._lock:
            if self._job_operation_request is not None:
                return False
            self._job_operation_request = {
                "operation": operation,
                "job_id": job_id,
                "policies": dict(policies or {}),
            }
            self._status["queued_job_operation"] = True
        self.wake()
        return True

    def consume_job_operation(self) -> dict[str, Any] | None:
        with self._lock:
            request = self._job_operation_request
            self._job_operation_request = None
            self._status["queued_job_operation"] = False
            return request

    def has_job_operation(self) -> bool:
        with self._lock:
            return self._job_operation_request is not None

    def request_query(self, query: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            if self._query_request is not None:
                return None
            ticket = {
                "query": query,
                "payload": payload,
                "event": threading.Event(),
                "response": None,
            }
            self._query_request = ticket
        self.wake()
        return ticket

    def consume_query(self) -> dict[str, Any] | None:
        with self._lock:
            ticket = self._query_request
            self._query_request = None
            return ticket

    @staticmethod
    def complete_query(ticket: dict[str, Any], response: dict[str, Any]) -> None:
        ticket["response"] = response
        ticket["event"].set()

    def wake(self) -> None:
        self._wake_event.set()

    def wait(self, timeout_seconds: float) -> None:
        self._wake_event.wait(timeout_seconds)
        self._wake_event.clear()


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
            elif command == "pull":
                source = str(request.get("source") or "")
                destination = str(request.get("destination") or "")
                selected_paths = request.get("selected_paths") or []
                if not source or not destination:
                    response = {"ok": False, "error": "source and destination are required"}
                elif not isinstance(selected_paths, list) or not all(isinstance(path, str) and path.strip() for path in selected_paths):
                    response = {"ok": False, "error": "selected paths must be a list of non-empty paths"}
                elif self.server.api_state.request_pull(source, destination, bool(request.get("dry_run")), selected_paths):
                    response = {"ok": True, "queued": True}
                else:
                    response = {"ok": False, "error": "another transfer is already queued"}
            elif command == "receive":
                source = str(request.get("source") or "")
                destination = str(request.get("destination") or "")
                selected_paths = request.get("selected_paths") or []
                mode = str(request.get("mode") or "receive")
                if not source or not destination:
                    response = {"ok": False, "error": "source and destination are required"}
                elif mode not in {"receive", "clone"}:
                    response = {"ok": False, "error": "mode must be receive or clone"}
                elif not isinstance(selected_paths, list) or not all(isinstance(path, str) and path.strip() for path in selected_paths):
                    response = {"ok": False, "error": "selected paths must be a list of non-empty paths"}
                elif self.server.api_state.request_receive(
                    source,
                    destination,
                    selected_paths,
                    str(request.get("source_label") or "peer"),
                    mode,
                    request.get("baseline_inventory") if isinstance(request.get("baseline_inventory"), dict) else None,
                    str(request.get("source_generation") or "") or None,
                    str(request.get("link_id") or "") or None,
                ):
                    response = {"ok": True, "queued": True}
                else:
                    response = {"ok": False, "error": "another transfer is already queued"}
            elif command == "job_operation":
                operation = str(request.get("operation") or "")
                job_id = str(request.get("job_id") or "")
                policies = request.get("policies") or {}
                if operation not in {"apply", "reconcile", "rollback"} or not job_id:
                    response = {"ok": False, "error": "valid operation and job id are required"}
                elif not isinstance(policies, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in policies.items()):
                    response = {"ok": False, "error": "policies must be a string map"}
                elif self.server.api_state.request_job_operation(operation, job_id, policies):
                    response = {"ok": True, "queued": True}
                else:
                    response = {"ok": False, "error": "another job operation is already queued"}
            elif command == "compare":
                source = str(request.get("source") or "")
                destination = str(request.get("destination") or "")
                selected_paths = request.get("selected_paths") or []
                if not source or not destination:
                    response = {"ok": False, "error": "source and destination are required"}
                elif not isinstance(selected_paths, list) or not all(isinstance(path, str) and path.strip() for path in selected_paths):
                    response = {"ok": False, "error": "selected paths must be a list of non-empty paths"}
                else:
                    ticket = self.server.api_state.request_query(
                        "compare",
                        {"source": source, "destination": destination, "selected_paths": selected_paths},
                    )
                    if ticket is None:
                        response = {"ok": False, "error": "another remote query is already queued"}
                    elif not ticket["event"].wait(300):
                        response = {"ok": False, "error": "comparison timed out while waiting for the daemon work lane"}
                    else:
                        response = ticket["response"] or {"ok": False, "error": "comparison produced no response"}
            elif command == "audit_sync":
                ticket = self.server.api_state.request_query("audit_sync", {})
                if ticket is None:
                    response = {"ok": False, "error": "another remote query is already queued"}
                elif not ticket["event"].wait(300):
                    response = {"ok": False, "error": "audit sync timed out while waiting for the daemon work lane"}
                else:
                    response = ticket["response"] or {"ok": False, "error": "audit sync produced no response"}
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
