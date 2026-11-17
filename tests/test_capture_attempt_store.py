"""Ticketed captures keep the actual store-thread writer boundary through their receipt."""
# Imported fixtures are requested by the tests.
# ruff: noqa: F811
from __future__ import annotations

import sys

import pytest
from test_capture_attempts import authored, dispatch
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import vault  # noqa: F401
from test_vault_store_read_write import stored, writer  # noqa: F401
from test_vault_store_write_path import served  # noqa: F401

from exomem import capture_attempts, schema, writer_lease
from exomem.governance.principal import owner_principal, request_scope
from exomem.vault_store import read

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="The offline-owner lease is Linux-only")


def test_store_capture_settles_its_receipt_without_markdown_projection(served, monkeypatch):
    args = authored()
    manager = writer_lease.active_manager()
    ledger = manager.idempotency.capture_attempts
    complete = ledger.complete
    held = []
    acquisitions = []
    acquire = manager._acquire_writer

    def counted(*args, **kwargs):
        acquisitions.append(True)
        return acquire(*args, **kwargs)

    monkeypatch.setattr(manager, "_acquire_writer", counted)

    def persist(ticket, receipt, **policy):
        boundary = manager._mutation_coordinator_for(served)
        held.append(boundary.current_thread_holds_boundary() and not boundary.current_thread_borrows_boundary())
        assert receipt["_terminal"]
        assert len(acquisitions) == ordinary_acquisitions
        complete(ticket, receipt, **policy)

    monkeypatch.setattr(ledger, "complete", persist)
    with request_scope(owner_principal(surface="mcp")):
        ticket = dispatch(manager, served, "reserve_source_capture", {"source_identity": args["source_identity"], "request_sha256": capture_attempts.CaptureWireV1.model_validate(args).sha256()})["reservation"]
        acquisitions.clear()
        source_schema = schema.load_source_schema(served)
        dispatch(manager, served, "capture_source", {**args, "content": "Another original.", "source_identity": {**args["source_identity"], "record_id": "ordinary"}}, source_schema)
        ordinary_acquisitions = len(acquisitions)
        acquisitions.clear()
        result = dispatch(manager, served, "capture_source", {**args, "capture_reservation": ticket}, source_schema)
        path = result["source"]["path"]
        assert not (served / path).exists()
        with read.snapshot(served) as snapshot:
            assert snapshot.page(path).body
        settled = dispatch(manager, served, "settle_source_capture", {"reservation": ticket})
    assert held == [True]
    assert settled["admission_closed_here"] is True and settled["past_outcome"] == "created"
    assert settled["capture_receipt"]["source"]["path"] == path
