"""The reverse export of a migrated vault back to files (P1b.4), over the real store and coordinator.

The vault is ``legacy_vault``'s: four migrated collections in every shape the migration
carries. A service writes through the store, then the owner exports with the service
stopped. File mode must then read each collection as the store served it.
"""

from __future__ import annotations

import json
import os
import shutil
from contextlib import closing
from dataclasses import replace

import pytest
from test_collection_store_migration import (
    CID,
    KEY,
    LEDGER_ID,
    MIGRATED,
    OPERATOR,
    OTHER,
    PLANNING,
    WORK,
    X3,
    X3_ID,
    env,  # noqa: F401 - the fixture
    fence,
    legacy_vault,
    migrate,
    printed,
    rows,
    s1,  # noqa: F401 - the fixture
    start_service,
    vault_bytes,
    wait_out_the_lease,
)
from test_collection_store_s1_gate import coordinator
from test_collection_store_service import DAILY, Service, call

from exomem import __main__ as cli
from exomem import record_formats, records, state_migration, state_paths, writer_lease
from exomem.cli_ops import OpError
from exomem.collection_store import authority, connection, export
from exomem.collection_store.connection import CollectionStoreError
from exomem.governance.principal import library_scope

COLLECTIONS = "Knowledge Base/_Collections"
#: A Records log row in the shape of the invented X3 fixture.
SESSION = {"occurred_on": "2026-08-03", "title": "Pull", "status": "completed",
           "movements": [{"movement": "Deadlift", "band": "grey", "repetitions": "22"}]}


def run_export(root, *flags, operator=True):
    """`exomem collections export`, the operator credential set only in the apply shell."""
    with pytest.MonkeyPatch.context() as shell:
        if operator:
            shell.setenv(OPERATOR, "operator")
        return cli._collections_main(["export", "--vault", str(root), *flags])


def write_through_the_store(root):
    """A service writes a new Records item, corrects an imported one, appends a log row and plans an area."""
    service = start_service(root)
    try:
        call(root, "record_memory", action="append", collection=WORK, item={"title": "Store row", "count": 5},
             why="a store-mode write")
        original = next(row for row in call(root, "record_memory", action="query", collection=WORK)["rows"]
                        if row["record_id"] == KEY)
        guards = call(root, "record_memory", action="inspect", collection=WORK)["lifecycle_guards"]
        call(root, "record_memory", action="update", collection=WORK, item_key=KEY, changes={"count": 4},
             expected_item_version=original["item_version"],
             expected_container_hash=guards["expected_container_hash"], why="a store-mode correction")
        call(root, "record_memory", action="append", collection=X3, item=SESSION, why="a store-mode session")
        call(root, "plan_memory", action="add", collection=PLANNING, item={"title": "Operations", "kind": "area"},
             why="a store-mode plan")
        return {cid: rows(root, path) for cid, path in MIGRATED.items()}
    finally:
        service.stop()


def migrated_and_written(root, capsys):
    """The legacy vault migrated, then written through the store; its bytes before and what it served."""
    legacy_vault(root)
    before = vault_bytes(root)
    assert migrate(root, "apply") == 0 and printed(capsys)["status"] == "marker_admitted"
    return before, write_through_the_store(root)


def preview_id(root, capsys):
    assert run_export(root, "--dry-run", operator=False) == 0
    previewed = printed(capsys)
    assert previewed["verdict"] == "exportable", previewed
    return previewed["plan_id"]


def test_export_returns_a_migrated_vault_to_files_file_mode_reads_as_the_store_served(env, capsys):  # noqa: F811
    """An export that loses or changes a row the store served, rewrites a collection the store
    never wrote, leaves a written collection without its acknowledged checkpoint, or leaves
    anything that routes, fences or adopts the store: the marker, the fence, the descriptors,
    the replica or the live store under its adoptable name."""
    root = env
    before, served = migrated_and_written(root, capsys)
    store_id = authority.parse_marker(root, authority.read_marker(root))["store_id"]
    plan_id = preview_id(root, capsys)

    assert run_export(root, "--plan-id", plan_id, "--why", "roll back to files") == 0
    applied = printed(capsys)

    assert (applied["status"], applied["store_id"], applied["retired_fence_generation"]) == (
        "exported", store_id, 1)
    assert {row["collection_id"]: row["status"] for row in applied["collections"]} == {
        CID: "written", X3_ID: "written", LEDGER_ID: "unwritten", OTHER: "written"}
    at = applied["exported_at"]
    assert authority.read_marker(root) is None
    assert sorted(path.name for path in (root / COLLECTIONS).iterdir()) == [
        f"collections.sqlite.exported-{at}", f"mode.json.exported-{at}"]
    assert not connection.store_path(root).exists()
    assert (state_paths.vault_state_dir(root) / f"collections.sqlite.exported-{at}").is_file()
    assert (fence().enrolled, fence().generation) == (False, 1)
    assert not {"collections-store-v1", state_migration.COLLECTION_MARKER_COMPATIBILITY_ID} & set(
        state_migration.recorded_descriptor_ids(root))
    unwritten = "Knowledge Base/Records/Publications/"
    assert {name: digest for name, digest in vault_bytes(root).items() if name.startswith(unwritten)} == {
        name: digest for name, digest in before.items() if name.startswith(unwritten)}
    # Without a marker, the dispatcher reads every collection from its files.
    assert {cid: rows(root, path) for cid, path in MIGRATED.items()} == served
    audit = {cid: call(root, "plan_memory" if path == PLANNING else "record_memory", action="inspect",
                       collection=path)["audit"]["status"] for cid, path in MIGRATED.items()}
    assert audit == {CID: "acknowledged_gap", X3_ID: "acknowledged_gap", LEDGER_ID: "ok",
                     OTHER: "acknowledged_gap"}
    with closing(connection.open_reader(state_paths.vault_state_dir(root) / f"collections.sqlite.exported-{at}")) \
            as tombstone:
        intent = json.loads(tombstone.execute("SELECT value FROM store_meta WHERE key=?",
                                              (authority.PENDING_CREATE,)).fetchone()[0])
    assert (intent["kind"], intent["plan_id"]) == (authority.EXPORT, plan_id)
    assert export.EXPORTED in applied["marker"]


def exported(root, capsys):
    """The migrated, written vault exported by its owner; the apply's report."""
    migrated_and_written(root, capsys)
    assert run_export(root, "--plan-id", preview_id(root, capsys), "--why", "roll back to files") == 0
    return printed(capsys)


def file_service(root):
    """A service started on a vault that routes nothing to a store, as after an export."""
    return Service(root, writer_lease.start_server_lifecycle())


def test_the_exporting_host_serves_files_and_never_reopens_its_tombstoned_store(env, capsys):  # noqa: F811
    """An export after which the service reopens, recreates or serves the store, or refuses a
    file-mode write because a fence or marker still names the store."""
    root = env
    at = exported(root, capsys)["exported_at"]
    tombstones = vault_bytes(root, outside="Knowledge Base/Records")
    service = file_service(root)
    try:
        written = call(root, "record_memory", action="append", collection=WORK, item={"title": "File row"},
                       why="a file-mode write after the export")
        assert written["outcome"] == "committed" and (root / written["paths"][0]).is_file()
        assert "File row" in {row["title"] for row in call(root, "record_memory", action="query",
                                                             collection=WORK)["rows"]}
    finally:
        service.stop()
    assert not connection.store_path(root).exists()
    assert (state_paths.vault_state_dir(root) / f"collections.sqlite.exported-{at}").is_file()
    assert {path: digest for path, digest in vault_bytes(root, outside="Knowledge Base/Records").items()
            if path.startswith(COLLECTIONS)} == {path: digest for path, digest in tombstones.items()
                                                 if path.startswith(COLLECTIONS)}


def test_a_copied_vault_on_fresh_state_adopts_no_tombstoned_replica(env, capsys, tmp_path, monkeypatch):  # noqa: F811
    """A host meeting the exported vault for the first time that serves it through a store, by
    adopting the tombstoned replica or refusing a stale marker, instead of reading the files."""
    root = env
    exported(root, capsys)
    copy = tmp_path / "copy"
    shutil.copytree(root, copy)
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "fresh-state"))
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(copy))
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "lease-copy"))
    state_migration.require_vault_state_ready(copy)
    service = file_service(copy)
    try:
        assert {row["title"] for row in call(copy, "record_memory", action="query", collection=WORK)["rows"]} == {
            "One", "Store row"}
    finally:
        service.stop()
    assert not connection.store_path(copy).exists()
    assert authority.read_marker(copy) is None


def test_a_host_with_a_stale_store_serves_files_and_retire_local_waits_for_the_exported_vault(
        env, capsys, tmp_path, monkeypatch):  # noqa: F811
    """A second host that keeps serving or adopting its pre-export copy of the store, or a
    retire-local that tombstones a store its vault copy still routes to."""
    root = env
    migrated_and_written(root, capsys)
    other = tmp_path / "other"
    shutil.copytree(root, other)
    host = {"EXOMEM_STATE_ROOT": str(tmp_path / "other-state"), "EXOMEM_VAULT_PATH": str(other),
            "EXOMEM_WRITER_LEASE_STATE_DIR": str(tmp_path / "lease-other"),
            "EXOMEM_WRITER_LEASE_REPLICA_ID": "other"}
    with pytest.MonkeyPatch.context() as there:
        for name, value in host.items():
            there.setenv(name, value)
        start_service(other).stop()  # it adopts the replica: this host now keeps a store of its own
        assert connection.store_path(other).exists()
    # Sync carries the other host's last replica back, so the exporting host continues its tenure.
    shutil.copy2(other / COLLECTIONS / "collections.sqlite", root / COLLECTIONS / "collections.sqlite")
    assert run_export(root, "--plan-id", preview_id(root, capsys), "--why", "roll back to files") == 0
    at = printed(capsys)["exported_at"]
    for name, value in host.items():
        monkeypatch.setenv(name, value)
    assert run_export(other, "--retire-local", operator=False) == 1  # the exported vault has not reached here yet
    assert "still routes" in capsys.readouterr().err and connection.store_path(other).exists()
    shutil.rmtree(other)
    shutil.copytree(root, other)  # the exported vault reaches the other host
    service = file_service(other)
    try:
        assert {row["title"] for row in call(other, "record_memory", action="query", collection=WORK)["rows"]} == {
            "One", "Store row"}
    finally:
        service.stop()
    assert connection.store_path(other).exists()  # served from files, never opened

    assert run_export(other, "--retire-local", operator=False) == 0  # the writer credential suffices
    assert printed(capsys)["status"] == "retired"
    assert not connection.store_path(other).exists()
    assert (state_paths.vault_state_dir(other) / f"collections.sqlite.exported-{at}").is_file()
    assert not {"collections-store-v1", state_migration.COLLECTION_MARKER_COMPATIBILITY_ID} & set(
        state_migration.recorded_descriptor_ids(other))


def _export_and_crash(environment, database, cut, plan_id):
    """`collections export --plan-id` in a fresh interpreter that dies at ``cut``, as a killed process would."""
    os.environ.clear()
    os.environ.update(environment)
    records._capture_sweep_carrier = records._due_state_carrier = lambda *a, **kw: None

    def die(*args, **kwargs):
        os._exit(73)

    def after(original):
        def run(*args, **kwargs):
            original(*args, **kwargs)
            os._exit(73)
        return run

    {"after-begin": lambda: setattr(export, "_begin", after(export._begin)),
     "before-files": lambda: setattr(export, "_write_collection", die),
     "between-files-and-checkpoint": lambda: setattr(records, "export_checkpoint", die),
     "after-fence-retire": lambda: setattr(export, "_detach", die),
     "after-marker-retire": lambda: setattr(export, "_retire_marker", after(export._retire_marker)),
     "before-store-tombstone": lambda: setattr(export, "_tombstone_store", die)}[cut]()
    with coordinator(database), library_scope():
        cli._collections_main(["export", "--vault", environment["EXOMEM_VAULT_PATH"], "--plan-id", plan_id,
                               "--why", "roll back to files"])
    os._exit(0)


def crash_export(tmp_path, cut, plan_id):
    """Run one export apply that dies at ``cut``, then wait out the dead owner's lease."""
    import multiprocessing

    child = multiprocessing.get_context("spawn").Process(target=_export_and_crash, args=(
        {**os.environ, OPERATOR: "operator"}, tmp_path / "coordinator.sqlite", cut, plan_id))
    child.start()
    child.join(120)
    assert child.exitcode == 73
    wait_out_the_lease()


@pytest.mark.parametrize("cut", ["after-begin", "before-files", "between-files-and-checkpoint", "after-fence-retire",
                                 "after-marker-retire", "before-store-tombstone"])
def test_an_export_cut_at_any_step_resumes_from_its_intent_to_the_same_files(env, capsys, tmp_path, cut):  # noqa: F811
    """An export killed between two steps that cannot finish, finishes differently, or, cut after
    its fence retire, leaves the retired fence beside a marker that still routes collections."""
    root = env
    _, served = migrated_and_written(root, capsys)
    plan_id = preview_id(root, capsys)
    crash_export(tmp_path, cut, plan_id)
    if cut == "after-fence-retire":
        assert not fence().enrolled and authority.read_marker(root) is not None
    assert run_export(root, "--dry-run", operator=False) == 0
    assert (printed(capsys)["verdict"], connection.store_path(root).exists()) == ("export_pending", True)

    assert run_export(root, "--plan-id", plan_id, "--why", "roll back to files") == 0
    resumed = printed(capsys)

    assert (resumed["status"], resumed["plan_id"]) == ("exported", plan_id)
    assert authority.read_marker(root) is None and not connection.store_path(root).exists()
    assert (fence().enrolled, fence().generation) == (False, 1)
    assert not {"collections-store-v1", state_migration.COLLECTION_MARKER_COMPATIBILITY_ID} & set(
        state_migration.recorded_descriptor_ids(root))
    assert {cid: rows(root, path) for cid, path in MIGRATED.items()} == served
    assert call(root, "record_memory", action="inspect", collection=WORK)["audit"]["status"] == "acknowledged_gap"
    checkpoints = [line for line in (root / "Knowledge Base/log.md").read_text().splitlines()
                   if "rebaseline" in line and "collections-store-history" in line]
    assert len(checkpoints) == 3  # one per written collection, never repeated by the resume


def test_a_resume_after_another_host_wrote_through_the_replica_refuses_before_it_writes(
        env, capsys, tmp_path):  # noqa: F811
    """A resume that publishes, renews and exports a store behind the coordinator's head: a host
    that adopted the replica before it carried the intent committed a row the files then lack,
    and that row survives only in a store that retire-local later tombstones."""
    root = env
    migrated_and_written(root, capsys)
    plan_id = preview_id(root, capsys)
    crash_export(tmp_path, "after-begin", plan_id)  # the intent is in the live store, not the replica
    other = tmp_path / "other"
    shutil.copytree(root, other)
    with pytest.MonkeyPatch.context() as there:
        for name, value in {"EXOMEM_STATE_ROOT": str(tmp_path / "other-state"), "EXOMEM_VAULT_PATH": str(other),
                            "EXOMEM_WRITER_LEASE_STATE_DIR": str(tmp_path / "lease-other"),
                            "EXOMEM_WRITER_LEASE_REPLICA_ID": "other"}.items():
            there.setenv(name, value)
        service = start_service(other)  # it adopts the replica, which carries no intent
        try:
            call(other, "record_memory", action="append", collection=WORK, item={"title": "Other host row"},
                 why="a store-mode write on another host")
        finally:
            service.stop()
    client = writer_lease.LeaseCoordinatorClient(writer_lease.LeaseConfig.from_env())
    recorded = client.status().collection_store_head
    before = vault_bytes(root)

    assert run_export(root, "--plan-id", plan_id, "--why", "roll back to files") == 1

    assert "COLLECTION_STORE_SYNC_PENDING" in capsys.readouterr().err
    assert client.status().collection_store_head == recorded
    assert vault_bytes(root) == before


def test_a_rerun_after_a_hand_retired_fence_refuses_before_it_retires_the_marker(env, capsys, tmp_path):  # noqa: F811
    """A rerun that takes a retired fence as proof that the export wrote and verified its files,
    then retires the marker and tombstones the store while the vault still holds the store's
    views, so the rows written in store mode survive only in the tombstone."""
    root = env
    migrated_and_written(root, capsys)
    plan_id = preview_id(root, capsys)
    store_id = authority.parse_marker(root, authority.read_marker(root))["store_id"]
    crash_export(tmp_path, "before-files", plan_id)  # the intent is recorded, no file is written
    with pytest.MonkeyPatch.context() as shell:
        shell.setenv(OPERATOR, "operator")
        writer_lease.configured_schema_fence_operator_client().retire_collection_store_fence(
            expected_generation=1, store_id=store_id)
    views = vault_bytes(root)

    assert run_export(root, "--plan-id", plan_id, "--why", "roll back to files") == 1

    assert export.UNVERIFIED in capsys.readouterr().err
    assert authority.parse_marker(root, authority.read_marker(root))["store_id"] == store_id
    assert connection.store_path(root).exists()
    assert vault_bytes(root) == views


def test_a_host_that_lost_its_store_after_the_fence_retire_finishes_with_renames_and_retire_local(
        env, capsys, tmp_path):  # noqa: F811
    """The documented recovery for an exporting host whose live store is lost after the fence
    retire: retire-local that keeps the store's descriptors because no store is left, so a
    release from before the store still refuses this state."""
    root = env
    _, served = migrated_and_written(root, capsys)
    crash_export(tmp_path, "after-fence-retire", preview_id(root, capsys))
    for path in state_paths.vault_state_dir(root).glob("collections*.sqlite*"):
        path.unlink()
    at = "20261016T120000Z"
    (root / COLLECTIONS / "mode.json").rename(root / COLLECTIONS / f"mode.json.exported-{at}")
    (root / COLLECTIONS / "collections.sqlite").rename(root / COLLECTIONS / f"collections.sqlite.exported-{at}")

    assert run_export(root, "--retire-local", operator=False) == 0

    assert printed(capsys)["status"] == "retired"
    assert not {"collections-store-v1", state_migration.COLLECTION_MARKER_COMPATIBILITY_ID} & set(
        state_migration.recorded_descriptor_ids(root))
    assert {cid: rows(root, path) for cid, path in MIGRATED.items()} == served


def test_the_retire_probe_needs_the_operator_credential_and_moves_no_fence(env, capsys, monkeypatch):  # noqa: F811
    """A probe that passes on a credential the coordinator rejects, so an export writes every
    file and then cannot retire the fence, or a probe that changes the fence it checks."""
    root = env
    legacy_vault(root)
    assert migrate(root, "apply") == 0 and printed(capsys)["status"] == "marker_admitted"
    enrolled = fence()
    monkeypatch.setenv(OPERATOR, "operator")
    writer_lease.configured_schema_fence_operator_client().probe_collection_store_fence_retire()
    monkeypatch.setenv(OPERATOR, "not-the-operator")

    with pytest.raises(OpError) as refused:
        writer_lease.configured_schema_fence_operator_client().probe_collection_store_fence_retire()

    assert refused.value.code == "SCHEMA_FENCE_OPERATOR_UNAVAILABLE"
    assert fence() == enrolled and enrolled.enrolled


def test_a_log_over_the_file_mode_bound_keeps_the_vault_in_the_store(env, capsys, monkeypatch):  # noqa: F811
    """An export that writes a log file mode then refuses to read, or that names the log's
    bound as a value with no file form, so the owner cannot tell what to shrink."""
    root = env
    migrated_and_written(root, capsys)
    store_id = authority.parse_marker(root, authority.read_marker(root))["store_id"]
    before = vault_bytes(root)
    monkeypatch.setattr(record_formats, "_MAX_LOG_BYTES", 64)

    assert run_export(root, "--dry-run", operator=False) == 1

    previewed = printed(capsys)
    assert previewed["verdict"] == "blocked" and "plan_id" not in previewed
    assert {row["collection_id"]: row["blocker"]["code"] for row in previewed["collections"]
            if row["status"] == "blocked"} == {X3_ID: export.OVER_BOUND}
    nothing_changed(root, before, store_id)


def nothing_changed(root, before, store_id):
    """The vault after a refused apply: still routed to the same store, fenced, its files untouched."""
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        assert reader.execute("SELECT 1 FROM store_meta WHERE key=?", (authority.PENDING_CREATE,)).fetchone() is None
    assert authority.parse_marker(root, authority.read_marker(root))["store_id"] == store_id
    assert (fence().enrolled, fence().store_id) == (True, store_id)
    assert vault_bytes(root) == before


def test_a_view_edited_by_hand_blocks_the_export_and_names_its_path(env, capsys):  # noqa: F811
    """An export that overwrites a person's edit to a store view, which P2 has not classified yet."""
    root = env
    migrated_and_written(root, capsys)
    plan_id = preview_id(root, capsys)
    view = root / WORK.replace("_collection.md", "Items/Original.md")
    view.write_text(view.read_text() + "\nA note added by hand\n")
    store_id = authority.parse_marker(root, authority.read_marker(root))["store_id"]
    before = vault_bytes(root)

    assert run_export(root, "--dry-run", operator=False) == 1
    blocked = {row["path"]: row for row in printed(capsys)["collections"] if row["status"] == "blocked"}
    assert run_export(root, "--plan-id", plan_id, "--why", "roll back to files") == 1

    assert list(blocked) == [WORK] and blocked[WORK]["blocker"]["code"] == export.VIEW_EDITED
    assert view.relative_to(root).as_posix() in blocked[WORK]["blocker"]["reason"]
    assert export.VIEW_EDITED in capsys.readouterr().err
    nothing_changed(root, before, store_id)


def test_a_store_write_after_the_preview_makes_its_plan_stale(env, capsys):  # noqa: F811
    """An apply that exports a plan older than the store, losing the write made after its preview."""
    root = env
    migrated_and_written(root, capsys)
    plan_id = preview_id(root, capsys)
    service = start_service(root)
    try:
        call(root, "record_memory", action="append", collection=WORK, item={"title": "After preview"},
             why="a write after the preview")
    finally:
        service.stop()
    store_id = authority.parse_marker(root, authority.read_marker(root))["store_id"]
    before = vault_bytes(root)

    assert run_export(root, "--plan-id", plan_id, "--why", "roll back to files") == 1

    assert export.STALE in capsys.readouterr().err
    nothing_changed(root, before, store_id)


def test_a_collection_the_store_created_keeps_the_whole_vault_in_the_store(s1, capsys):  # noqa: F811
    """An export that drops a store-native summary collection, which file mode never held, or that
    exports the rest and leaves that one behind a retired fence (the current conservative rule)."""
    root = s1
    assert migrate(root, "apply") == 0 and printed(capsys)["status"] == "marker_admitted"
    store_id = authority.parse_marker(root, authority.read_marker(root))["store_id"]
    before = vault_bytes(root)

    assert run_export(root, "--dry-run", operator=False) == 1
    previewed = printed(capsys)

    blocked = {row["collection_id"]: row["blocker"]["code"] for row in previewed["collections"]
               if row["status"] == "blocked"}
    assert previewed["verdict"] == "blocked" and "plan_id" not in previewed
    assert blocked == {DAILY: export.STORE_NATIVE}
    nothing_changed(root, before, store_id)


def test_an_apply_refuses_while_a_service_holds_the_vault(env, capsys):  # noqa: F811
    """An apply that takes the lease beside a service another host runs, or a service whose
    maintain_memory applies the export itself and so retires the fence it writes under."""
    root = env
    migrated_and_written(root, capsys)
    plan_id = preview_id(root, capsys)
    store_id = authority.parse_marker(root, authority.read_marker(root))["store_id"]
    before = vault_bytes(root)
    elsewhere = writer_lease.LeaseCoordinatorClient(
        replace(writer_lease.LeaseConfig.from_env(), replica_id="another-host"))
    held = elsewhere.acquire(collection_store_capability=writer_lease.COLLECTION_STORE_CAPABILITY)
    assert held.holder == "another-host"

    assert run_export(root, "--plan-id", plan_id, "--why", "roll back to files") == 1

    assert "COLLECTION_STORE_SERVICE_ACTIVE" in capsys.readouterr().err
    elsewhere.release(held.fencing_token)
    service = start_service(root)
    try:
        with pytest.raises((OpError, ValueError, CollectionStoreError)) as refused:
            call(root, "maintain_memory", mode="collections-store-export", apply=True, plan_id=plan_id,
                 why="roll back to files")
        assert "COLLECTION_STORE_SERVICE_ACTIVE" in str(refused.value)
    finally:
        service.stop()
    nothing_changed(root, before, store_id)


def test_a_vault_migrates_again_after_its_export_at_the_next_fence_generation(env, capsys):  # noqa: F811
    """A re-migration that the retired fence or the tombstones block, that reuses the exported store's
    identity or generation, or that serves rows other than the exported files hold."""
    root = env
    old = exported(root, capsys)["store_id"]
    files = {cid: rows(root, path) for cid, path in MIGRATED.items()}

    assert migrate(root, "apply") == 0
    applied = printed(capsys)

    routed = authority.parse_marker(root, authority.read_marker(root))
    assert applied["status"] == "marker_admitted" and routed["store_id"] != old
    assert routed["collection_store_fence"]["generation"] == 2
    assert (fence().enrolled, fence().generation, fence().store_id) == (True, 2, routed["store_id"])
    service = start_service(root)
    try:
        assert {cid: rows(root, path) for cid, path in MIGRATED.items()} == files
    finally:
        service.stop()
