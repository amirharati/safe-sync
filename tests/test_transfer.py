import json
import shutil
from pathlib import Path

import pytest

import safe_sync.transfer as transfer
import safe_sync.cli as cli
from safe_sync.api import DaemonApiState
from safe_sync.transfer import (
    JobConflictError,
    JobStore,
    LinkStore,
    ScopeError,
    compare_inventories,
    generation_record,
    local_inventory,
    normalize_subpath,
    parse_combined_report,
    parse_rclone_inventory,
    resolve_local_scope,
    scopes_overlap,
)


def write_tree(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def stage_job(store: JobStore, job: dict, source: Path) -> dict:
    stage = Path(job["paths"]["staging"])
    shutil.copytree(source, stage, dirs_exist_ok=True)
    return store.mark_staged(job["id"])


def test_scope_normalization_overlap_and_escape(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "project").mkdir()

    assert normalize_subpath("/project/app/") == "project/app"
    assert scopes_overlap("project", "project/app")
    assert not scopes_overlap("project-a", "project-b")
    assert resolve_local_scope(root, "project") == root / "project"
    with pytest.raises(ScopeError):
        normalize_subpath("../outside")

    (root / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ScopeError):
        resolve_local_scope(root, "escape")


def test_local_inventory_ignores_work_directory_and_does_not_follow_symlinks(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    write_tree(root, {"keep.txt": "yes", ".safe-sync-work/jobs/a.json": "no"})
    write_tree(outside, {"secret.txt": "secret"})
    (root / "external").symlink_to(outside, target_is_directory=True)

    inventory = local_inventory(root)

    assert "keep.txt" in inventory
    assert not any(path.startswith(".safe-sync-work") for path in inventory)
    assert inventory["external"]["type"] == "symlink"
    assert "external/secret.txt" not in inventory


def test_rclone_inventory_and_combined_report_are_normalized():
    inventory = parse_rclone_inventory(
        json.dumps(
            [
                {"Path": "docs", "IsDir": True, "Size": -1, "ModTime": "2026-01-01T00:00:00Z"},
                {"Path": "docs/a.txt", "IsDir": False, "Size": 3, "Hashes": {"DropboxHash": "abc"}, "ID": "rev-1"},
                {"Path": ".safe-sync-work/jobs/a.json", "IsDir": False, "Size": 1},
            ]
        )
    )

    assert inventory["docs"]["type"] == "directory"
    assert inventory["docs/a.txt"]["hashes"] == {"dropboxhash": "abc"}
    assert inventory["docs/a.txt"]["id"] == "rev-1"
    assert ".safe-sync-work/jobs/a.json" not in inventory
    assert parse_combined_report("+ added.txt\n* changed.txt\n- deleted.txt\n! failed.txt\n= same.txt\n") == [
        {"path": "added.txt", "operation": "added"},
        {"path": "changed.txt", "operation": "modified"},
        {"path": "deleted.txt", "operation": "removed"},
        {"path": "failed.txt", "operation": "error"},
    ]


def test_two_way_and_three_way_comparison_categories(tmp_path):
    baseline_root = tmp_path / "baseline"
    local_root = tmp_path / "local"
    peer_root = tmp_path / "peer"
    for root in (baseline_root, local_root, peer_root):
        root.mkdir()
    write_tree(
        baseline_root,
        {
            "same.txt": "same",
            "local.txt": "base",
            "peer.txt": "base",
            "same-change.txt": "base",
            "conflict.txt": "base",
            "local-delete.txt": "base",
        },
    )
    shutil.copytree(baseline_root, local_root, dirs_exist_ok=True)
    shutil.copytree(baseline_root, peer_root, dirs_exist_ok=True)
    write_tree(local_root, {"local.txt": "local", "same-change.txt": "both", "conflict.txt": "left"})
    write_tree(peer_root, {"peer.txt": "peer", "same-change.txt": "both", "conflict.txt": "right"})
    (local_root / "local-delete.txt").unlink()

    comparison = compare_inventories(
        local_inventory(local_root),
        local_inventory(peer_root),
        local_inventory(baseline_root),
    )
    categories = {item["path"]: item["category"] for item in comparison["results"]}

    assert categories["same.txt"] == "same"
    assert categories["local.txt"] == "local_only"
    assert categories["peer.txt"] == "peer_only"
    assert categories["same-change.txt"] == "same_change"
    assert categories["conflict.txt"] == "conflict"
    assert categories["local-delete.txt"] == "local_only"


def test_generation_record_is_complete_and_deterministic():
    record = generation_record(
        machine_id="machine-a",
        install_id="install-a",
        profile_id="profile-a",
        folder_id="folder-a",
        filter_policy="sha256-filter",
        changes=[{"path": "z.txt", "operation": "added"}, {"path": "a.txt", "operation": "removed"}],
        generation_id="gen-a",
        completed_at="2026-08-08T00:00:00+00:00",
    )

    assert record["complete"] is True
    assert record["generation_id"] == "gen-a"
    assert [item["path"] for item in record["changes"]] == ["a.txt", "z.txt"]


def test_receive_add_moves_staged_file_and_rollback_removes_it(tmp_path):
    state = tmp_path / "state"
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    write_tree(source, {"new.txt": "new"})
    store = JobStore(state)
    job = store.create(source="remote:test", destination=destination, source_inventory=local_inventory(source))
    stage_job(store, job, source)

    completed = store.apply(job["id"])

    assert completed["status"] == "complete"
    assert (destination / "new.txt").read_text() == "new"
    assert not (Path(job["paths"]["staging"]) / "new.txt").exists()

    rolled_back = store.rollback(job["id"])
    assert rolled_back["status"] == "rolled_back"
    assert not (destination / "new.txt").exists()


def test_receive_differing_file_requires_decision_and_replace_is_checkpointed(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    write_tree(source, {"item.txt": "peer"})
    write_tree(destination, {"item.txt": "local"})
    store = JobStore(tmp_path / "state")
    job = store.create(source="remote:test", destination=destination, source_inventory=local_inventory(source))
    stage_job(store, job, source)

    with pytest.raises(JobConflictError, match="explicit decisions required"):
        store.apply(job["id"])

    completed = store.apply(job["id"], {"item.txt": "replace"})
    action = next(item for item in completed["actions"] if item["path"] == "item.txt")
    assert (destination / "item.txt").read_text() == "peer"
    assert Path(action["checkpoint_path"]).read_text() == "local"

    rolled_back = store.rollback(job["id"])
    assert rolled_back["status"] == "rolled_back"
    assert (destination / "item.txt").read_text() == "local"


def test_destination_change_after_review_blocks_apply(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    write_tree(source, {"new.txt": "new"})
    store = JobStore(tmp_path / "state")
    job = store.create(source="remote:test", destination=destination, source_inventory=local_inventory(source))
    stage_job(store, job, source)
    write_tree(destination, {"surprise.txt": "later"})

    with pytest.raises(JobConflictError, match="destination changed after review"):
        store.apply(job["id"])
    assert store.load(job["id"])["status"] == "needs_refresh"


def test_staging_change_after_verification_blocks_apply(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    write_tree(source, {"new.txt": "peer"})
    store = JobStore(tmp_path / "state")
    job = store.create(source="remote:test", destination=destination, source_inventory=local_inventory(source))
    stage_job(store, job, source)
    (Path(job["paths"]["staging"]) / "new.txt").write_text("evil")

    with pytest.raises(JobConflictError, match="staged files changed"):
        store.apply(job["id"])
    assert not (destination / "new.txt").exists()


def test_rollback_never_overwrites_a_later_local_edit(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    write_tree(source, {"item.txt": "peer"})
    write_tree(destination, {"item.txt": "local"})
    store = JobStore(tmp_path / "state")
    job = store.create(source="remote:test", destination=destination, source_inventory=local_inventory(source))
    stage_job(store, job, source)
    store.apply(job["id"], {"item.txt": "replace"})
    (destination / "item.txt").write_text("edited-after-apply")

    rolled_back = store.rollback(job["id"])

    assert rolled_back["status"] == "rollback_conflict"
    assert (destination / "item.txt").read_text() == "edited-after-apply"
    recovered = list(destination.glob("item.from-recovered-*.txt"))
    assert len(recovered) == 1
    assert recovered[0].read_text() == "local"


def test_interrupted_replace_reconciles_from_durable_journal(tmp_path, monkeypatch):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    write_tree(source, {"item.txt": "peer"})
    write_tree(destination, {"item.txt": "local"})
    store = JobStore(tmp_path / "state")
    job = store.create(source="remote:test", destination=destination, source_inventory=local_inventory(source))
    stage_job(store, job, source)
    real_move = transfer._move_atomic
    calls = 0

    def interrupt_second_move(source_path, destination_path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated process interruption")
        real_move(source_path, destination_path)

    monkeypatch.setattr(transfer, "_move_atomic", interrupt_second_move)
    with pytest.raises(RuntimeError, match="simulated process interruption"):
        store.apply(job["id"], {"item.txt": "replace"})
    assert store.load(job["id"])["status"] == "interrupted"

    monkeypatch.setattr(transfer, "_move_atomic", real_move)
    reconciled = store.reconcile(job["id"])
    assert reconciled["status"] == "complete"
    assert (destination / "item.txt").read_text() == "peer"


def test_startup_reconcile_recovers_moves_made_before_journal_save(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    write_tree(source, {"item.txt": "peer"})
    write_tree(destination, {"item.txt": "local"})
    store = JobStore(tmp_path / "state")
    job = store.create(source="remote:test", destination=destination, source_inventory=local_inventory(source))
    stage_job(store, job, source)
    job = store.load(job["id"])
    job["actions"] = store._build_actions(job, {"item.txt": "replace"})
    job["status"] = "applying"
    job["actions"][0]["installed_path"] = str(destination / "item.txt")
    store.save(job)

    old = Path(job["paths"]["checkpoint"]) / "old/item.txt"
    transfer._move_atomic(destination / "item.txt", old)

    reconciled = store.reconcile(job["id"])
    assert reconciled["status"] == "complete"
    assert (destination / "item.txt").read_text() == "peer"
    assert old.read_text() == "local"


def test_rollback_of_checkpoint_only_interruption_restores_original_path(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    write_tree(source, {"item.txt": "peer"})
    write_tree(destination, {"item.txt": "local"})
    store = JobStore(tmp_path / "state")
    job = store.create(source="remote:test", destination=destination, source_inventory=local_inventory(source))
    stage_job(store, job, source)
    job = store.load(job["id"])
    job["actions"] = store._build_actions(job, {"item.txt": "replace"})
    old = Path(job["paths"]["checkpoint"]) / "old/item.txt"
    transfer._move_atomic(destination / "item.txt", old)
    job["actions"][0].update({"state": "old_checkpointed", "checkpoint_path": str(old), "installed_path": str(destination / "item.txt")})
    job["status"] = "interrupted"
    store.save(job)

    rolled_back = store.rollback(job["id"])
    assert rolled_back["status"] == "rolled_back"
    assert (destination / "item.txt").read_text() == "local"


def test_clone_requires_empty_destination_and_rolls_back_conditionally(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "clone"
    source.mkdir()
    write_tree(source, {"nested/a.txt": "a"})
    store = JobStore(tmp_path / "state")
    job = store.create(
        source="remote:test",
        destination=destination,
        mode="clone",
        source_inventory=local_inventory(source),
    )
    stage_job(store, job, source)

    completed = store.commit_clone(job["id"])
    assert completed["status"] == "complete"
    assert (destination / "nested/a.txt").read_text() == "a"
    assert store.rollback(job["id"])["status"] == "rolled_back"
    assert not destination.exists()


def test_link_store_enforces_convergence_filters_overlap_and_peer_identity(tmp_path):
    store = LinkStore(tmp_path / "state")
    inventory = {"a.txt": {"type": "file", "size": 1, "hashes": {"sha256": "a"}}}
    link = store.add(
        label="App",
        local_profile_id="local-profile",
        local_folder_id="projects",
        local_subpath="app",
        peer_machine_id="peer-machine",
        peer_install_id="peer-install",
        peer_folder_id="projects",
        peer_subpath="app",
        local_filter_fingerprint="filter-a",
        peer_filter_fingerprint="filter-a",
        local_inventory_value=inventory,
        peer_inventory_value=inventory,
    )

    assert store.status(
        link,
        local_inventory_value=inventory,
        peer_inventory_value=inventory,
        peer_install_id="peer-install",
        peer_filter_fingerprint="filter-a",
    )["status"] == "up_to_date"
    assert store.status(
        link,
        local_inventory_value=inventory,
        peer_inventory_value=inventory,
        peer_install_id="new-install",
        peer_filter_fingerprint="filter-a",
    )["status"] == "peer_replaced"

    with pytest.raises(ScopeError, match="may not overlap"):
        store.add(
            label="Nested",
            local_profile_id="local-profile",
            local_folder_id="projects",
            local_subpath="app/subfolder",
            peer_machine_id="peer-machine",
            peer_install_id="peer-install",
            peer_folder_id="projects",
            peer_subpath="other",
            local_filter_fingerprint="filter-a",
            peer_filter_fingerprint="filter-a",
            local_inventory_value=inventory,
            peer_inventory_value=inventory,
        )

    with pytest.raises(JobConflictError, match="different filter policies"):
        LinkStore(tmp_path / "other-state").add(
            label="Mismatch",
            local_profile_id="local-profile",
            local_folder_id="projects",
            local_subpath="app",
            peer_machine_id="peer-machine",
            peer_install_id="peer-install",
            peer_folder_id="projects",
            peer_subpath="app",
            local_filter_fingerprint="filter-a",
            peer_filter_fingerprint="filter-b",
            local_inventory_value=inventory,
            peer_inventory_value=inventory,
        )


def test_daemon_api_serializes_safe_receive_and_job_operations():
    state = DaemonApiState()
    assert state.request_receive("remote:source", "/tmp/destination", ["a.txt"], "Laptop", "clone")
    assert not state.request_receive("remote:other", "/tmp/other")
    request = state.consume_pull_request()
    assert request == {
        "source": "remote:source",
        "destination": "/tmp/destination",
        "dry_run": False,
        "selected_paths": ["a.txt"],
        "source_label": "Laptop",
        "mode": "clone",
        "safe_receive": True,
        "baseline_inventory": None,
        "source_generation": None,
        "link_id": None,
    }
    assert state.request_job_operation("apply", "job-1", {"a.txt": "replace"})
    assert not state.request_job_operation("rollback", "job-2")
    assert state.consume_job_operation() == {
        "operation": "apply",
        "job_id": "job-1",
        "policies": {"a.txt": "replace"},
    }
    ticket = state.request_query("compare", {"source": "remote:a", "destination": "/tmp/a"})
    assert ticket is not None
    assert state.request_query("compare", {}) is None
    assert state.consume_query() is ticket
    state.complete_query(ticket, {"ok": True, "comparison": {"equal": True}})
    assert ticket["event"].is_set()
    assert ticket["response"]["comparison"]["equal"] is True


def test_parser_exposes_safe_transfer_surfaces():
    assert cli.parser().parse_args(["compare", "remote:folder", "/tmp/local"]).func is cli.cmd_compare
    receive = cli.parser().parse_args(["receive", "remote:folder", "/tmp/local", "--clone"])
    assert receive.func is cli.cmd_receive
    assert receive.clone is True
    apply = cli.parser().parse_args(["jobs", "apply", "job-1", "--policy", "a.txt=replace"])
    assert apply.func is cli.cmd_jobs
    assert apply.policy == ["a.txt=replace"]
    assert cli.parser().parse_args(["links", "status"]).func is cli.cmd_links
    assert cli.parser().parse_args(["links", "review", "link-1"]).func is cli.cmd_links
    assert cli.parser().parse_args(["recovery", "revisions", "folder-a", "file.txt"]).func is cli.cmd_recovery


def test_generation_publication_writes_immutable_before_latest(tmp_path, monkeypatch):
    filter_path = tmp_path / "filter.txt"
    filter_path.write_text("")
    report = tmp_path / "report.txt"
    report.write_text("+ new.txt\n* changed.txt\n")
    config = {
        "machine_id": "machine-a",
        "machine": "machine-a",
        "install_id": "install-a",
        "profile_id": "profile-a",
        "folder_id": "folder-a",
        "remote_base": "remote:backups",
        "filter_file": str(filter_path),
        "state_root": str(tmp_path / "state"),
        "status_path": str(tmp_path / "state" / "status.json"),
    }
    calls = []
    remote_objects = {}

    def fake_capture(_config, command, input_text=None):
        calls.append((command, input_text))
        if command[0] == "cat":
            if command[1] in remote_objects:
                return cli.subprocess.CompletedProcess(command, 0, remote_objects[command[1]])
            return cli.subprocess.CompletedProcess(command, 1, "not found")
        if command[0] == "rcat":
            remote_objects[command[1]] = input_text
        return cli.subprocess.CompletedProcess(command, 0, "")

    monkeypatch.setattr(cli, "rclone_capture", fake_capture)
    assert cli.publish_generation(config, report) == 0
    uploads = [command for command, _body in calls if command[0] == "rcat"]
    assert "/generations/gen_" in uploads[0][1]
    assert uploads[1][1].endswith("/latest.json")
    latest = json.loads((tmp_path / "state/generations/machine-a/folder-a/latest.json").read_text())
    assert latest["install_id"] == "install-a"
    assert [change["path"] for change in latest["changes"]] == ["changed.txt", "new.txt"]


def test_generation_publication_reuses_pending_id_after_failed_upload(tmp_path, monkeypatch):
    filter_path = tmp_path / "filter.txt"
    filter_path.write_text("")
    report = tmp_path / "report.txt"
    report.write_text("+ new.txt\n")
    config = {
        "machine_id": "machine-a",
        "machine": "machine-a",
        "install_id": "install-a",
        "profile_id": "profile-a",
        "folder_id": "folder-a",
        "remote_base": "remote:backups",
        "filter_file": str(filter_path),
        "state_root": str(tmp_path / "state"),
        "status_path": str(tmp_path / "state" / "status.json"),
    }
    remote_objects = {}
    immutable_attempts = []
    fail_first_immutable = True

    def fake_capture(_config, command, input_text=None):
        nonlocal fail_first_immutable
        if command[0] == "cat":
            if command[1] in remote_objects:
                return cli.subprocess.CompletedProcess(command, 0, remote_objects[command[1]])
            return cli.subprocess.CompletedProcess(command, 1, "not found")
        if command[0] == "rcat":
            if "/generations/" in command[1]:
                immutable_attempts.append(command[1])
                if fail_first_immutable:
                    fail_first_immutable = False
                    return cli.subprocess.CompletedProcess(command, 5, "connection reset by peer")
            remote_objects[command[1]] = input_text
        return cli.subprocess.CompletedProcess(command, 0, "")

    monkeypatch.setattr(cli, "rclone_capture", fake_capture)
    assert cli.publish_generation(config, report) == 5
    pending = cli.pending_generation_path(config)
    first_id = json.loads(pending.read_text())["generation_id"]

    assert cli.publish_generation(config) == 0
    assert not pending.exists()
    assert len(immutable_attempts) == 2
    assert immutable_attempts[0] == immutable_attempts[1]
    assert first_id in immutable_attempts[1]


def test_generation_publication_accepts_ambiguous_verified_upload(tmp_path, monkeypatch):
    filter_path = tmp_path / "filter.txt"
    filter_path.write_text("")
    config = {
        "machine_id": "machine-a",
        "machine": "machine-a",
        "install_id": "install-a",
        "profile_id": "profile-a",
        "folder_id": "folder-a",
        "remote_base": "remote:backups",
        "filter_file": str(filter_path),
        "state_root": str(tmp_path / "state"),
        "status_path": str(tmp_path / "state" / "status.json"),
    }
    remote_objects = {}

    def fake_capture(_config, command, input_text=None):
        if command[0] == "cat":
            if command[1] in remote_objects:
                return cli.subprocess.CompletedProcess(command, 0, remote_objects[command[1]])
            return cli.subprocess.CompletedProcess(command, 1, "not found")
        if command[0] == "rcat":
            remote_objects[command[1]] = input_text
            return cli.subprocess.CompletedProcess(command, 5, "timeout awaiting response headers")
        return cli.subprocess.CompletedProcess(command, 0, "")

    monkeypatch.setattr(cli, "rclone_capture", fake_capture)
    changes = [{"path": "new.txt", "operation": "added"}]
    assert cli.publish_generation(config, changes=changes) == 0
    assert not cli.pending_generation_path(config).exists()


def test_generation_publication_skips_no_change_cycle(tmp_path, monkeypatch):
    filter_path = tmp_path / "filter.txt"
    filter_path.write_text("")
    config = {
        "machine_id": "machine-a",
        "machine": "machine-a",
        "install_id": "install-a",
        "profile_id": "profile-a",
        "folder_id": "folder-a",
        "remote_base": "remote:backups",
        "filter_file": str(filter_path),
        "state_root": str(tmp_path / "state"),
        "status_path": str(tmp_path / "state" / "status.json"),
    }
    monkeypatch.setattr(cli, "rclone_capture", lambda *_args, **_kwargs: pytest.fail("remote should not be called"))
    assert cli.publish_generation(config, changes=[]) == 0


def test_generation_publication_does_not_break_chain_when_parent_lookup_times_out(tmp_path, monkeypatch):
    filter_path = tmp_path / "filter.txt"
    filter_path.write_text("")
    config = {
        "machine_id": "machine-a",
        "machine": "machine-a",
        "install_id": "install-a",
        "profile_id": "profile-a",
        "folder_id": "folder-a",
        "remote_base": "remote:backups",
        "filter_file": str(filter_path),
        "state_root": str(tmp_path / "state"),
        "status_path": str(tmp_path / "state" / "status.json"),
    }
    calls = []

    def fake_capture(_config, command, input_text=None):
        calls.append((command, input_text))
        return cli.subprocess.CompletedProcess(command, 5, "timeout awaiting response headers")

    monkeypatch.setattr(cli, "rclone_capture", fake_capture)
    assert cli.publish_generation(config, changes=[{"path": "new.txt", "operation": "added"}]) == 5
    assert [command[0] for command, _body in calls] == ["cat"]
    assert not cli.pending_generation_path(config).exists()


def test_change_accumulation_preserves_net_effect_across_attempts():
    changes = cli.merge_backup_changes([], [{"path": "a.txt", "operation": "added"}])
    changes = cli.merge_backup_changes(changes, [{"path": "a.txt", "operation": "modified"}])
    assert changes == [{"path": "a.txt", "operation": "added"}]
    changes = cli.merge_backup_changes(changes, [{"path": "a.txt", "operation": "removed"}])
    assert changes == []
    changes = cli.merge_backup_changes([], [{"path": "b.txt", "operation": "removed"}])
    changes = cli.merge_backup_changes(changes, [{"path": "b.txt", "operation": "added"}])
    assert changes == [{"path": "b.txt", "operation": "modified"}]


def test_generation_detection_notifies_only_for_linked_scope(tmp_path, monkeypatch):
    store = LinkStore(tmp_path / "state")
    inventory = {"a.txt": {"type": "file", "size": 1, "hashes": {"sha256": "a"}}}
    link = store.add(
        label="App",
        local_profile_id="local",
        local_folder_id="projects",
        local_subpath="app",
        peer_machine_id="peer",
        peer_install_id="peer-install",
        peer_folder_id="work",
        peer_subpath="projects/app",
        local_filter_fingerprint="filter-a",
        peer_filter_fingerprint="filter-a",
        local_inventory_value=inventory,
        peer_inventory_value=inventory,
        peer_generation="gen-0",
    )
    config = {"state_root": str(tmp_path / "state"), "status_path": str(tmp_path / "state/status.json")}
    monkeypatch.setattr(
        cli,
        "fetch_latest_generation",
        lambda *_args: {
            "complete": True,
            "generation_id": "gen-1",
            "install_id": "peer-install",
            "filter_fingerprint": "filter-a",
            "changes": [{"path": "projects/app/file.txt", "operation": "modified"}],
        },
    )

    notifications = cli.detect_link_generations(config)

    assert notifications == [{"link_id": link["id"], "label": "App", "generation_id": "gen-1"}]
    updated = store.get(link["id"])
    assert updated["status"] == "peer_changes"
    assert updated["pending_peer_generation"] == "gen-1"

    monkeypatch.setattr(
        cli,
        "fetch_latest_generation",
        lambda *_args: {
            "complete": True,
            "generation_id": "gen-2",
            "install_id": "peer-install",
            "filter_fingerprint": "filter-a",
            "changes": [{"path": "unrelated/file.txt", "operation": "modified"}],
        },
    )
    assert cli.detect_link_generations(config) == []
    assert store.get(link["id"])["last_seen_peer_generation"] == "gen-2"


@pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone is not installed")
def test_real_local_rclone_receive_stages_before_apply(tmp_path):
    source = tmp_path / "remote-source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    write_tree(source, {"nested/a.txt": "alpha", "skip.txt": "skip"})
    filter_path = tmp_path / "filter.txt"
    filter_path.write_text("")
    config = {
        "rclone_bin": shutil.which("rclone"),
        "filter_file": str(filter_path),
        "state_root": str(tmp_path / "state"),
        "status_path": str(tmp_path / "state/status.json"),
        "log_dir": str(tmp_path / "logs"),
    }

    code, job = cli.create_receive_job(
        config,
        source=str(source),
        destination=str(destination),
        selected_paths=["nested"],
    )

    assert code == 0
    assert job["status"] == "ready"
    assert not (destination / "nested/a.txt").exists()
    assert (Path(job["paths"]["staging"]) / "nested/a.txt").read_text() == "alpha"
    assert not (Path(job["paths"]["staging"]) / "skip.txt").exists()
    completed = JobStore(tmp_path / "state").apply(job["id"])
    assert completed["status"] == "complete"
    assert (destination / "nested/a.txt").read_text() == "alpha"
