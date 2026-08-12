from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from safe_sync.event_journal import EventJournal, JournalError, JournalSettings, redact, settings_from_config
from safe_sync.cli import cmd_logs, default_config, normalized_config, parser, record_event, replicate_event_journal, run_command


def journal(tmp_path: Path, *, max_bytes: int = 4096, segment_bytes: int = 1024, level: str = "normal") -> EventJournal:
    return EventJournal(
        state_root=tmp_path,
        profile_id="profile-a",
        machine_id="machine-a",
        install_id="install-a",
        settings=JournalSettings(
            level=level,
            max_local_bytes=max_bytes,
            segment_bytes=segment_bytes,
            max_cloud_bytes=max_bytes,
            cloud_flush_interval_seconds=10,
        ),
        home=tmp_path / "home",
    )


def test_audit_is_always_recorded_but_diagnostics_follow_level(tmp_path: Path) -> None:
    value = journal(tmp_path, level="quiet")
    assert value.emit("backup.started", component="backup", channel="audit")
    assert value.emit("backup.detail", component="backup", channel="diagnostic", severity="info") is None
    assert value.emit("backup.failed", component="backup", channel="diagnostic", severity="error")
    assert [item["event_type"] for item in value.events()] == ["backup.started", "backup.failed"]


def test_event_envelope_sequence_and_redaction(tmp_path: Path) -> None:
    value = journal(tmp_path)
    first = value.emit(
        "backup.path_result",
        component="backup",
        data={
            "path": "project/file.txt",
            "access_token": "secret",
            "absolute": "/private/data",
            "error": "failed while reading /private/other/file.txt",
        },
        correlation={"folder_id": "projects", "operation_id": "op-a"},
    )
    second = value.emit("backup.completed", component="backup")
    assert first and second
    assert first["sequence"] == 1
    assert second["sequence"] == 2
    assert first["data"]["access_token"] == "<redacted>"
    assert first["data"]["absolute"] == "<absolute-path-redacted>"
    assert "/private" not in first["data"]["error"]
    assert first["event_id"].endswith("000000000001")


def test_segment_wrap_is_bounded_and_records_unreplicated_gap(tmp_path: Path) -> None:
    value = journal(tmp_path, max_bytes=8192, segment_bytes=2048)
    for index in range(40):
        value.emit("backup.path_result", component="backup", data={"path": f"file-{index:03d}.txt", "result": "modified"})
    value.seal_active()
    status = value.status()
    assert status["segment_count"] <= 4
    assert status["used_local_bytes"] <= 8192
    assert status["gaps"]
    events = value.events(limit=None)
    assert events == sorted(events, key=lambda item: item["sequence"])
    assert events[-1]["sequence"] >= 40
    assert events[0]["sequence"] > 1
    assert any(event["event_type"] == "logging.events_dropped" for event in events)


def test_mark_replicated_prevents_wrap_gap_for_uploaded_segment(tmp_path: Path) -> None:
    value = journal(tmp_path, max_bytes=8192, segment_bytes=2048)
    value.emit("backup.started", component="backup")
    value.seal_active()
    segments = value.segment_records()
    value.mark_replicated({segments[0]["sha256"]}, manifest_hash="manifest-a")
    assert value.status()["pending_cloud_segments"] == 0
    assert value.status()["replication"]["remote_manifest_hash"] == "manifest-a"


def test_manual_seal_emits_dropped_event_when_ring_wraps(tmp_path: Path) -> None:
    value = journal(tmp_path, max_bytes=8192, segment_bytes=2048)
    for index in range(4):
        value.emit("backup.path_result", component="backup", data={"path": f"file-{index}.txt"})
        value.seal_active()

    assert value.status()["gaps"]
    assert any(event["event_type"] == "logging.events_dropped" for event in value.events(limit=None))


def test_capacity_resize_replaces_the_correct_retained_slot(tmp_path: Path) -> None:
    original = journal(tmp_path, max_bytes=12288, segment_bytes=2048)
    for index in range(7):
        original.emit("backup.path_result", component="backup", data={"path": f"before-{index}.txt"})
        original.seal_active()

    resized = journal(tmp_path, max_bytes=8192, segment_bytes=2048)
    resized.emit("backup.completed", component="backup")
    resized.seal_active()

    status = resized.status()
    assert status["segment_count"] <= status["slot_count"] == 3
    assert status["used_local_bytes"] <= status["max_local_bytes"]
    assert resized.events(limit=None)[-1]["event_type"] in {"backup.completed", "logging.events_dropped"}


def test_truncated_active_tail_is_repaired(tmp_path: Path) -> None:
    value = journal(tmp_path)
    value.emit("runtime.started", component="runtime")
    with value.active_path.open("ab") as handle:
        handle.write(b'{"partial":')
    recovered = journal(tmp_path)
    assert [item["event_type"] for item in recovered.events()] == ["runtime.started"]
    assert recovered.active_path.read_bytes().endswith(b"\n")


def test_cursor_rebuild_from_segments(tmp_path: Path) -> None:
    value = journal(tmp_path)
    value.emit("runtime.started", component="runtime")
    value.seal_active()
    value.cursor_path.write_text("not json")
    recovered = journal(tmp_path)
    assert recovered.status()["newest_sequence"] == 1
    assert recovered.events()[0]["event_type"] == "runtime.started"


def test_cloud_manifest_contains_only_integrity_metadata(tmp_path: Path) -> None:
    value = journal(tmp_path)
    value.emit("backup.completed", component="backup")
    value.seal_active()
    manifest = value.cloud_manifest()
    assert manifest["stream"]["profile_id"] == "profile-a"
    assert manifest["segments"][0]["sha256"]
    assert "path" not in manifest["segments"][0]


def test_query_filters_folder_event_and_limit(tmp_path: Path) -> None:
    value = journal(tmp_path)
    for index in range(5):
        value.emit(
            "backup.path_result" if index % 2 else "backup.started",
            component="backup",
            correlation={"folder_id": "a" if index < 4 else "b"},
        )
    result = value.events(event_type="backup.path_result", folder_id="a", limit=1)
    assert len(result) == 1
    assert result[0]["sequence"] == 4


def test_historical_gaps_do_not_report_current_logging_failure(tmp_path: Path) -> None:
    value = journal(tmp_path, max_bytes=3072, segment_bytes=1024)
    for index in range(100):
        value.emit("diagnostic.sample", component="test", channel="diagnostic", data={"index": index, "detail": "x" * 160})
    value.seal_active()

    status = value.status()

    assert status["gaps"]
    assert status["history_complete"] is False
    assert status["history_gap_count"] == len(status["gaps"])
    assert status["health"] == "ok"


def test_settings_validate_capacity_and_temporary_level() -> None:
    with pytest.raises(JournalError):
        settings_from_config({"logging": {"level": "verbose"}})
    with pytest.raises(JournalError):
        settings_from_config({"logging": {"segment_bytes": 65536, "max_local_bytes": 131072}})
    settings = settings_from_config(
        {
            "logging": {
                "level": "normal",
                "temporary_level": "debug",
                "temporary_until": "2999-01-01T00:00:00Z",
            }
        }
    )
    assert settings.level == "debug"


def test_hashed_path_policy_is_stable(tmp_path: Path) -> None:
    first = redact(
        {"path": "project/private/file.txt"},
        home=tmp_path,
        path_detail="hashed",
        install_id="install-a",
    )
    second = redact(
        {"path": "project/private/file.txt"},
        home=tmp_path,
        path_detail="hashed",
        install_id="install-a",
    )
    assert first == second
    assert first["path"].startswith("path:")
    assert "private" not in first["path"]


def test_segment_text_rejects_tampering(tmp_path: Path) -> None:
    value = journal(tmp_path)
    value.emit("runtime.started", component="runtime")
    value.seal_active()
    segment = value.segment_records()[0]
    path = value.slots_dir / segment["path"]
    path.write_text(path.read_text() + "{}\n")
    with pytest.raises(JournalError):
        value.segment_text(segment)


def test_events_are_valid_json_lines(tmp_path: Path) -> None:
    value = journal(tmp_path)
    value.emit("runtime.started", component="runtime")
    raw = value.active_path.read_text().splitlines()
    assert len(raw) == 1
    assert json.loads(raw[0])["event_type"] == "runtime.started"


def test_commands_use_journal_without_creating_parallel_text_log(tmp_path: Path) -> None:
    config = default_config("machine-a")
    config["state_root"] = str(tmp_path / "state")
    config["log_dir"] = str(tmp_path / "logs")
    config = normalized_config(config)

    assert run_command(config, [sys.executable, "-c", "print('hello')"]) == 0

    assert not (tmp_path / "logs").exists()
    events = EventJournal(
        state_root=tmp_path / "state",
        profile_id=str(config["profile_id"]),
        machine_id="machine-a",
        install_id=str(config["install_id"]),
        settings=settings_from_config(config),
    ).events(limit=None)
    assert [event["event_type"] for event in events] == ["command.completed"]


def test_cloud_replication_uses_profile_owned_remote_and_marks_segments(monkeypatch, tmp_path: Path) -> None:
    config = default_config("machine-a")
    config["state_root"] = str(tmp_path / "state")
    config["log_dir"] = str(tmp_path / "logs")
    config["profiles"][0]["id"] = "profile-a"
    config["profiles"][0]["remote_base"] = "dropbox:backups/profile-a"
    config["active_profile_id"] = "profile-a"
    config = normalized_config(config)
    calls: list[tuple[list[str], str | None]] = []

    def fake_capture(_config, command, input_text=None):
        calls.append((command, input_text))
        if command[0] == "lsjson":
            output = "[]"
        elif command[0] == "cat":
            output = next(
                body or ""
                for previous_command, body in calls
                if previous_command[0] == "rcat" and previous_command[1] == command[1]
            )
        else:
            output = ""
        return subprocess.CompletedProcess(command, 0, output)

    monkeypatch.setattr("safe_sync.cli.rclone_capture", fake_capture)
    record_event(config, "runtime.started", component="runtime")
    status = replicate_event_journal(config)

    assert status["pending_cloud_segments"] == 0
    segment_upload = next(command for command, body in calls if command[0] == "rcat" and body and '"event_type"' in body)
    assert "dropbox:backups/profile-a/.audit/profile-a/machine-a/" in segment_upload[1]
    assert any(command[0] == "moveto" and command[-1].endswith("/manifest.json") for command, _body in calls)


def test_cloud_replication_failure_is_visible_without_raising(monkeypatch, tmp_path: Path) -> None:
    config = default_config("machine-a")
    config["state_root"] = str(tmp_path / "state")
    config["log_dir"] = str(tmp_path / "logs")
    config = normalized_config(config)
    record_event(config, "runtime.started", component="runtime")
    monkeypatch.setattr(
        "safe_sync.cli.rclone_capture",
        lambda _config, command, input_text=None: subprocess.CompletedProcess(command, 1, "offline"),
    )

    status = replicate_event_journal(config)

    assert status["health"] == "degraded"
    assert "segment upload failed" in status["replication"]["last_error"]
    assert status["pending_cloud_segments"] >= 1


def test_cloud_replication_rejects_unverified_segment(monkeypatch, tmp_path: Path) -> None:
    config = default_config("machine-a")
    config["state_root"] = str(tmp_path / "state")
    config["log_dir"] = str(tmp_path / "logs")
    config = normalized_config(config)
    record_event(config, "runtime.started", component="runtime")

    def corrupt_remote(_config, command, input_text=None):
        output = "corrupt" if command[0] == "cat" else ""
        return subprocess.CompletedProcess(command, 0, output)

    monkeypatch.setattr("safe_sync.cli.rclone_capture", corrupt_remote)
    status = replicate_event_journal(config)

    assert status["health"] == "degraded"
    assert "segment verification failed" in status["replication"]["last_error"]
    assert status["pending_cloud_segments"] >= 1


def test_logs_cli_sets_temporary_level_and_queries_json(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.json"
    config = default_config("machine-a")
    config["state_root"] = str(tmp_path / "state")
    config["log_dir"] = str(tmp_path / "logs")
    config["status_path"] = str(tmp_path / "state" / "status.json")
    config_path.write_text(json.dumps(config))

    level_args = parser().parse_args(["--config", str(config_path), "logs", "level", "debug", "--for", "2h"])
    assert cmd_logs(level_args) == 0
    written = json.loads(config_path.read_text())
    assert written["logging"]["temporary_level"] == "debug"
    capsys.readouterr()

    show_args = parser().parse_args(["--config", str(config_path), "logs", "show", "--json", "--event", "logging.level_changed"])
    assert cmd_logs(show_args) == 0
    events = json.loads(capsys.readouterr().out)
    assert events[-1]["event_type"] == "logging.level_changed"
    assert events[-1]["data"]["new_level"] == "debug"


def test_logs_status_reports_bounded_capacity(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.json"
    config = default_config("machine-a")
    config["state_root"] = str(tmp_path / "state")
    config["log_dir"] = str(tmp_path / "logs")
    config_path.write_text(json.dumps(config))
    args = parser().parse_args(["--config", str(config_path), "logs", "status"])
    assert cmd_logs(args) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["max_local_bytes"] == 64 * 1024 * 1024
    assert status["slot_count"] == 63
    assert status["remote_path"].startswith("dropbox:computer-backups/.audit/")
