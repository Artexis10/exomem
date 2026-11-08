"""A vault in store mode refuses every path that would serve stale Markdown or drop its pages."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from exomem import commands, doctor, server_runtime, state_migration
from exomem.cli_ops import OpError
from exomem.collection_store import admission, connection, migration, preflight, schema
from exomem.collection_store.connection import CollectionStoreError
from exomem.governance.principal import library_scope
from exomem.vault_store import availability, mode

AT = "2026-11-08T00:00:00Z"
LEASE_ENV = ("EXOMEM_WRITER_LEASE_URL", "EXOMEM_WRITER_LEASE_TOPOLOGY", "EXOMEM_CLOUD_CELL")


@pytest.fixture
def store_mode_vault(vault, monkeypatch):
    """The fixture vault with both store-mode records written directly, as activation leaves them."""
    for name in LEASE_ENV:
        monkeypatch.delenv(name, raising=False)
    with connection.open_writer(connection.store_path(vault), lease_check=lambda: True) as writer:
        with writer.transaction() as tx:
            tx.execute("INSERT INTO store_mode(id, mode, revision, changed_at, release) "
                       "VALUES (1, 'store', 1, ?, 'test')", (AT,))
    state_migration.record_vault_store_compatibility(vault, authority_check=lambda: True, active=True)
    return vault


def _coordinator(monkeypatch, url, topology=None):
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_URL", url)
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_VAULT_ID", "test-vault")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_REPLICA_ID", "test-replica")
    if topology is not None:
        monkeypatch.setenv("EXOMEM_WRITER_LEASE_TOPOLOGY", topology)


def _record_divergence(vault, reason):
    with connection.open_writer(connection.store_path(vault), lease_check=lambda: True) as writer:
        with writer.transaction() as tx:
            tx.execute("INSERT INTO store_meta(key, value) VALUES (?, ?)",
                       (schema.META_REPLICA_DIVERGENCE, json.dumps({"reason": reason})))


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
    assert availability.write_refusal(vault) is None
    assert availability.marker_inconsistency(vault) is None
    assert doctor._check_vault_store_records(vault) is None


def test_a_write_to_a_diverged_store_mode_vault_refuses_with_the_stores_code_and_changes_no_markdown(
        store_mode_vault):
    """Defect: a page write reaches disk while the store that owns the pages awaits reconciliation."""
    _record_divergence(store_mode_vault, "the vault replica records unresolved divergence")
    before = _vault_bytes(store_mode_vault)
    with pytest.raises(OpError) as refused:
        commands.op_remember(store_mode_vault, title="Pump note", status="draft",
                             content="## Observations\n\n- [finding] The pump holds pressure.\n",
                             summary="A pump finding for the test rig.")
    assert refused.value.code == "COLLECTION_STORE_DIVERGED"
    assert _vault_bytes(store_mode_vault) == before


def test_a_write_to_a_healthy_store_mode_vault_refuses_until_the_store_takes_page_writes(
        store_mode_vault):
    """Defect: with no write path into the store, a page write lands in Markdown the store ignores."""
    before = _vault_bytes(store_mode_vault)
    with pytest.raises(OpError) as refused:
        commands.op_remember(store_mode_vault, title="Pump note", status="draft",
                             content="## Observations\n\n- [finding] The pump holds pressure.\n",
                             summary="A pump finding for the test rig.")
    assert refused.value.code == availability.WRITE_PATH_PENDING
    assert _vault_bytes(store_mode_vault) == before


@pytest.mark.parametrize("flags", [{"retire_local": True}, {"plan_id": "any-plan", "why": "return to files"}])
def test_the_reverse_export_refuses_in_store_mode(store_mode_vault, flags):
    """Defect: exporting the collections to files retires the store that holds the pages."""
    with library_scope(), pytest.raises(CollectionStoreError) as refused:
        admission.export_route(store_mode_vault, **flags)
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


def test_a_store_mode_check_that_cannot_read_the_manifest_refuses_and_changes_no_markdown(
        store_mode_vault, monkeypatch):
    """Defect: an unreadable manifest passes for file mode, so a write lands in Markdown the store owns."""
    def unreadable(vault_root):
        raise state_migration.StateMigrationManifestError(vault_root, "the manifest cannot be read")

    monkeypatch.setattr(mode, "store_mode_active", unreadable)
    before = _vault_bytes(store_mode_vault)
    with pytest.raises(state_migration.StateMigrationManifestError):
        commands.op_remember(store_mode_vault, title="Pump note", status="draft",
                             content="## Observations\n\n- [finding] The pump holds pressure.\n",
                             summary="A pump finding for the test rig.")
    assert _vault_bytes(store_mode_vault) == before
    with pytest.raises(state_migration.StateMigrationManifestError):
        availability.require_servable(store_mode_vault)


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
    assert [record.getMessage() for record in caplog.records] == [availability.marker_inconsistency(vault)]
    assert "exomem store-mode rollback" in caplog.text
    assert availability.write_refusal(vault) is None
    check = doctor._check_vault_store_records(vault)
    assert (check.status, check.message) == ("warn", availability.marker_inconsistency(vault))
    assert "exomem store-mode rollback" in check.remediation
