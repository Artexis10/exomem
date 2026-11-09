"""A vault in store mode refuses every path that would serve stale Markdown or drop its pages."""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from exomem import commands, doctor, server_runtime, state_migration, state_paths
from exomem.cli_ops import OpError
from exomem.collection_store import (
    admission,
    connection,
    export,
    migration,
    preflight,
    schema,
    takeover,
)
from exomem.collection_store.connection import CollectionStoreError
from exomem.collection_store.writer import CollectionWriter
from exomem.governance.principal import library_scope
from exomem.vault_store import availability, migrate, ownership, write

AT = "2026-11-08T00:00:00Z"
LEASE_ENV = ("EXOMEM_WRITER_LEASE_URL", "EXOMEM_WRITER_LEASE_TOPOLOGY", "EXOMEM_CLOUD_CELL")


@pytest.fixture
def store_mode_vault(vault, monkeypatch):
    """The fixture vault with both store-mode records and its pages in the store, as activation leaves them."""
    for name in LEASE_ENV:
        monkeypatch.delenv(name, raising=False)
    with connection.open_writer(connection.store_path(vault), lease_check=lambda: True) as handle:
        with handle.transaction() as tx:
            # A store-mode store holds the owners' tables, and every commit derives rows into them.
            migrate.create_owner_schema(tx)
            tx.execute("INSERT INTO store_mode(id, mode, revision, changed_at, release) "
                       "VALUES (1, 'store', 1, ?, 'test')", (AT,))
        # In store mode a page read answers from the store, so the pages must be there.
        pages = tuple(
            write.PagePut(owned.rel, (vault / owned.rel).read_bytes(), owned.stat.st_mtime_ns)
            for owned in ownership.iter_owned(vault)
            if owned.ownership is ownership.Ownership.PAGE
        )
        write.commit(
            CollectionWriter(vault, handle),
            write.StoreChange(write.Origin.MIGRATION, "test fixture", puts=pages),
        )
    state_migration.record_vault_store_compatibility(vault, authority_check=lambda: True, active=True)
    return vault


def _coordinator(monkeypatch, url, topology=None):
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_URL", url)
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_VAULT_ID", "test-vault")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_REPLICA_ID", "test-replica")
    if topology is not None:
        monkeypatch.setenv("EXOMEM_WRITER_LEASE_TOPOLOGY", topology)


def _record_divergence(vault, key, value):
    with connection.open_writer(connection.store_path(vault), lease_check=lambda: True) as writer:
        with writer.transaction() as tx:
            tx.execute("INSERT INTO store_meta(key, value) VALUES (?, ?)", (key, value))


def _record_id_only(vault):
    state_migration.record_vault_store_compatibility(vault, authority_check=lambda: True, active=True)


def _create_store_without_mode_row(vault):
    with connection.open_writer(connection.store_path(vault), lease_check=lambda: True):
        pass


def _vault_bytes(vault):
    return {path: path.read_bytes() for path in vault.rglob("*") if path.is_file()}


def test_a_store_mode_vault_with_no_coordinator_refuses_to_start(store_mode_vault):
    """Defect: store mode starts with no coordinator, so no fence or head exchange protects the store."""
    with pytest.raises(CollectionStoreError) as refused:
        availability.require_servable(store_mode_vault)
    assert refused.value.code == admission.COORDINATION_REQUIRED


def test_a_store_mode_vault_under_the_default_multi_host_topology_refuses_to_start(
        store_mode_vault, monkeypatch):
    """Defect: a coordinator with no topology lets writer authority move to a host that holds no pages."""
    _coordinator(monkeypatch, "https://lease.example.test")
    with pytest.raises(CollectionStoreError) as refused:
        availability.require_servable(store_mode_vault)
    assert refused.value.code == availability.MULTI_HOST
    assert "EXOMEM_WRITER_LEASE_TOPOLOGY=single-host" in refused.value.reason


def test_startup_on_a_single_host_store_mode_vault_names_the_rollback(store_mode_vault, monkeypatch):
    """Defect: a build that cannot serve the store's pages starts and answers from stale Markdown."""
    _coordinator(monkeypatch, "http://127.0.0.1:8765", topology="single-host")
    with pytest.raises(CollectionStoreError) as refused:
        server_runtime.initialize_runtime(load_dotenv_func=lambda **_kwargs: None)
    assert refused.value.code == availability.NOT_SERVED
    assert "exomem store-mode rollback" in refused.value.reason


def test_a_file_mode_vault_starts_without_a_lease(vault, monkeypatch):
    """Defect: the store-mode checks reach file-mode users, who run with no coordinator."""
    for name in LEASE_ENV:
        monkeypatch.delenv(name, raising=False)
    availability.require_servable(vault)
    assert availability.page_writer(vault) is None
    assert availability.marker_inconsistency(vault) is None
    assert doctor._check_vault_store_records(vault) is None


@pytest.mark.parametrize("record", [
    (schema.META_REPLICA_DIVERGENCE, json.dumps({"reason": "the vault replica records unresolved divergence"})),
    (schema.META_VIEW_DIVERGED, "1"),
], ids=["replica divergence", "view stamp"])
def test_a_write_to_a_diverged_store_mode_vault_refuses_with_the_stores_code_and_changes_no_markdown(
        store_mode_vault, record):
    """Defect: a page write reaches disk while the store that owns the pages awaits reconciliation."""
    _record_divergence(store_mode_vault, *record)
    before = _vault_bytes(store_mode_vault)
    with pytest.raises(OpError) as refused:
        commands.op_remember(store_mode_vault, title="Pump note", status="draft",
                             content="## Observations\n\n- [finding] The pump holds pressure.\n",
                             summary="A pump finding for the test rig.")
    assert refused.value.code == "COLLECTION_STORE_DIVERGED"
    assert _vault_bytes(store_mode_vault) == before


def test_a_page_write_off_the_store_thread_refuses_and_changes_no_markdown(store_mode_vault):
    """Defect: a page write off the store thread lands in Markdown that the store ignores."""
    before = _vault_bytes(store_mode_vault)
    with pytest.raises(OpError) as refused:
        commands.op_remember(store_mode_vault, title="Pump note", status="draft",
                             content="## Observations\n\n- [finding] The pump holds pressure.\n",
                             summary="A pump finding for the test rig.")
    assert refused.value.code == availability.OFF_THREAD
    assert _vault_bytes(store_mode_vault) == before


class _Boundary:
    """A manager that offers only the mutation boundary, so any later use of the manager raises AttributeError."""

    def __init__(self):
        self.entered = []

    @contextmanager
    def consistency_guard(self, _root, *, operation):
        self.entered.append(operation)
        yield


@pytest.mark.parametrize("step, options, boundary", [
    (export.apply, {"plan_id": "any-plan", "why": "return to files"}, "collection_store_export"),
    (export.retire_local, {}, "collection_store_export_retire_local"),
], ids=["apply", "retire_local"])
def test_the_reverse_export_refuses_under_the_mutation_boundary_before_any_step(
        store_mode_vault, monkeypatch, step, options, boundary):
    """Defect: exporting the collections to files drops the store that holds the pages, or checks outside the boundary."""
    monkeypatch.setattr(admission._ProducerSession, "bind", lambda *_args: None)
    monkeypatch.setattr(admission, "_bind_fence_client", lambda *_args: None)
    manager = _Boundary()
    session = SimpleNamespace(root=store_mode_vault, verify_custody=lambda: None)
    with pytest.raises(CollectionStoreError) as refused:
        step(session, manager, None, **options)
    assert refused.value.code == availability.PAGES_AT_RISK
    assert manager.entered == [boundary]


def test_abandoning_an_export_still_runs_in_store_mode(store_mode_vault, monkeypatch):
    """Defect: the page-loss refusal blocks the step that gives up an unfinished export, which drops no store."""
    ran = []
    monkeypatch.setattr(admission, "_offline_owner",
                        lambda root, operation, work, **_options: work(SimpleNamespace(root=root), None, None))
    monkeypatch.setattr(export, "abandon", lambda *_args, **_kwargs: ran.append("abandon"))
    with library_scope():
        admission.export_route(store_mode_vault, plan_id="any-plan", why="stop", abandon=True)
    assert ran == ["abandon"]


@pytest.mark.parametrize("primitive", [
    lambda session: takeover.replace_live(session, "leaf", lambda: None),
    lambda session: export._tombstone_store(session, AT),
], ids=["replace_live", "tombstone_store"])
def test_the_steps_that_swap_or_retire_the_live_store_refuse_for_any_caller_in_store_mode(store_mode_vault, primitive):
    """Defect: a caller with no guard of its own swaps or retires the store that holds the pages."""
    with pytest.raises(CollectionStoreError) as refused:
        primitive(SimpleNamespace(root=store_mode_vault))  # the refusal comes before any use of the session
    assert refused.value.code == availability.PAGES_AT_RISK


def test_a_takeover_adopt_refuses_when_the_local_store_is_in_store_mode(store_mode_vault):
    """Defect: adopting the published replica replaces the live store with a copy that has no pages."""
    session = SimpleNamespace(root=store_mode_vault)  # the refusal comes before any use of the session
    with pytest.raises(CollectionStoreError) as refused:
        admission._settle(session, None, 1, {}, None, "adopt", "the replica is newer", None)
    assert refused.value.code == availability.PAGES_AT_RISK


def test_a_staged_migration_with_pending_collections_refuses_in_store_mode(store_mode_vault, monkeypatch):
    """Defect: staging a replacement store from collection files installs a store that holds no pages."""
    monkeypatch.setattr(preflight, "candidates", lambda root, ownership_proof=None: ([], ["pending"], []))
    with pytest.raises(CollectionStoreError) as refused:
        migration._stage(store_mode_vault)
    assert refused.value.code == availability.PAGES_AT_RISK


def _corrupt_manifest(vault):
    (state_paths.vault_state_dir(vault) / state_migration.MANIFEST_NAME).write_bytes(b"{not json")


def test_a_corrupt_manifest_refuses_a_write_and_a_start_and_changes_no_markdown(store_mode_vault):
    """Defect: a damaged manifest passes for file mode, so a write lands in Markdown the store owns."""
    _corrupt_manifest(store_mode_vault)
    before = _vault_bytes(store_mode_vault)
    with pytest.raises(state_migration.StateMigrationManifestError):
        commands.op_remember(store_mode_vault, title="Pump note", status="draft",
                             content="## Observations\n\n- [finding] The pump holds pressure.\n",
                             summary="A pump finding for the test rig.")
    assert _vault_bytes(store_mode_vault) == before
    with pytest.raises(state_migration.StateMigrationManifestError):
        availability.require_servable(store_mode_vault)


def test_the_doctor_reports_a_corrupt_manifest_as_the_manifest_error_not_as_a_marker_inconsistency(
        store_mode_vault):
    """Defect: a damaged manifest crashes the doctor, or reads as the crash state that a rollback settles."""
    _corrupt_manifest(store_mode_vault)
    check = doctor._check_vault_store_records(store_mode_vault)
    assert (check.status, check.details) == (
        "fail", {"migration_manifest": str(state_paths.vault_state_dir(store_mode_vault) / state_migration.MANIFEST_NAME)})
    assert "unreadable or invalid" in check.message
    assert "store-mode rollback" not in check.message + check.remediation


@pytest.mark.parametrize("store_exists", [False, True], ids=["no store file", "store without a store_mode row"])
def test_a_recorded_id_with_no_store_mode_warns_and_names_the_rollback_without_refusing(
        vault, monkeypatch, caplog, store_exists):
    """Defect: a crash between the two records leaves a vault that nothing names, or one that refuses to serve."""
    for name in LEASE_ENV:
        monkeypatch.delenv(name, raising=False)
    _record_id_only(vault)
    if store_exists:
        _create_store_without_mode_row(vault)
    with caplog.at_level(logging.WARNING, logger=availability.log.name):
        availability.require_servable(vault)
    # Creating the store runs Alembic, which logs at INFO whenever an earlier test left the root logger there.
    warnings = [record.getMessage() for record in caplog.records if record.name == availability.log.name]
    assert warnings == [availability.marker_inconsistency(vault)]
    assert "exomem store-mode rollback" in caplog.text
    assert availability.page_writer(vault) is None
    check = doctor._check_vault_store_records(vault)
    assert (check.status, check.message) == ("warn", availability.marker_inconsistency(vault))
    assert "exomem store-mode rollback" in check.remediation
