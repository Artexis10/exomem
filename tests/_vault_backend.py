"""Run release-boundary fixtures against files and real canonical store rows."""

from contextlib import contextmanager
from contextvars import ContextVar

import pytest
from conftest import initialize_vault_state_offline
from test_managed_collections_migration import coordinator_url as coordinator_url
from test_vault_store_migration import _lease_environment

from exomem import epistemic_graph, lexstore, memory_refs, state_migration
from exomem.collection_store import connection, preview
from exomem.collection_store.writer import CollectionWriter
from exomem.governance.principal import library_scope
from exomem.vault_store import availability, migrate, mode, read, rederive, write

_BACKEND = ContextVar("test_vault_backend", default="file")


@pytest.fixture(params=("file", "store"), autouse=True)
def vault_backend(request, monkeypatch):
    token = _BACKEND.set(request.param)
    if request.param == "store":
        url = request.getfixturevalue("coordinator_url")
        for key, value in _lease_environment(request.getfixturevalue("tmp_path"), url).items():
            monkeypatch.setenv(key, value)
        # PR 1 proves readers while the rollout guard still forbids a production store-mode start.
        monkeypatch.setattr(availability, "require_servable", lambda _root: None)
    try:
        yield request.param
    finally:
        _BACKEND.reset(token)


def prepare_vault(root):
    """Activate a constructed fixture with the production migration row builders."""
    if _BACKEND.get() == "file" or mode.store_mode_for_reads(root):
        return
    with library_scope():
        # Migration needs SQL rows even when the case exercises the Python reader.
        with pytest.MonkeyPatch.context() as construction:
            construction.setenv("EXOMEM_LEXICAL_BACKEND", "fts5")
            assert lexstore.get_store(root).rebuild_atomic()
        if not epistemic_graph.sidecar_path(root).exists():
            epistemic_graph.EpistemicGraphIndex(root).rebuild_all()
        memory_refs.ReferenceIndex(root).rebuild_all()
        with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as handle:
            migrate.stage(root, handle.connection)
            with handle.transaction() as tx:
                tx.execute(
                    "INSERT INTO store_mode(id, mode, revision, changed_at, release) "
                    "VALUES (1, 'store', 1, '2026-01-01T00:00:00Z', 'test')"
                )
                rederive.record_derived_from_files(tx, 1)
    state_migration.record_vault_store_compatibility(root, authority_check=lambda: True, active=True)


def initialize_backend(root):
    if _BACKEND.get() == "store" and not mode.store_mode_for_reads(root):
        root.mkdir(parents=True, exist_ok=True)
        initialize_vault_state_offline(root, source="store read privacy fixture")


@contextmanager
def backend_writer(root):
    """Bind the fixture's writer for governed calls, as the service binds its store writer."""
    if not mode.store_mode_for_reads(root):
        yield
        return
    with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as handle:
        token = preview._BOUND.set((root.resolve(), CollectionWriter(root, handle), False))
        try:
            yield
        finally:
            preview._BOUND.reset(token)


def write_page(root, rel, body):
    """Commit a fixture edit to the canonical backend when it already serves this vault."""
    if mode.store_mode_for_reads(root):
        with backend_writer(root):
            write.commit(preview.bound_writer(root), write.StoreChange(
                write.Origin.GOVERNED, "privacy fixture edit", puts=(write.PagePut(rel, body),),
            ))


def delete_page(root, rel):
    if mode.store_mode_for_reads(root):
        with backend_writer(root):
            write.commit(preview.bound_writer(root), write.StoreChange(
                write.Origin.GOVERNED, "privacy fixture delete", deletes=(write.PageDelete(rel),),
            ))
    (root / rel).unlink()


def page_bytes(root, rel):
    if mode.store_mode_for_reads(root):
        with read.snapshot(root) as view:
            return view.page(rel).body
    return (root / rel).read_bytes()
