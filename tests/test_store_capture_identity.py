"""Capture identity uses canonical store bytes before projection and across requests."""
# Imported fixtures are requested by test arguments.
# ruff: noqa: F811
from __future__ import annotations

import sys
from contextvars import copy_context

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_embeddings import encoder  # noqa: F401
from test_vault_store_migration import vault  # noqa: F401
from test_vault_store_read_write import stored, writer  # noqa: F401
from test_vault_store_write_path import served  # noqa: F401

from exomem import commands, schema, source_capture
from exomem.vault_store import read

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="The offline-owner lease is Linux-only")


def test_repeated_capture_reuses_unprojected_canonical_source(served, encoder):
    inputs = dict(title="Stored receipt", source_kind="session", content="The lantern burns oil.",
        source_identity={"system": "archive", "record_id": "receipt", "revision": "one"})
    first = commands.op_capture_source(served, schema.load_source_schema(served), **inputs)
    assert not (served / first["source"]["path"]).exists()
    second = commands.op_capture_source(served, schema.load_source_schema(served), **inputs)
    assert first["capture_outcome"] == "created"
    assert second["capture_outcome"] == "reused"
    assert second["source"]["ref"] == first["source"]["ref"]
    classified = source_capture.canonical_pages(served)
    with read.snapshot(served) as view:
        assert classified.store_revision == view.revision
    assert first["source"]["path"] in classified.canonical


def test_overlapping_capture_requests_recheck_after_both_read_snapshots_open(served, encoder):
    inputs = dict(title="Shared receipt", source_kind="session", content="The lantern burns oil.",
        source_identity={"system": "archive", "record_id": "overlapping", "revision": "one"})
    first_context, second_context = copy_context(), copy_context()
    first_scope, second_scope = read.request_snapshot(served), read.request_snapshot(served)
    first_context.run(first_scope.__enter__)
    second_context.run(second_scope.__enter__)
    try:
        first = first_context.run(commands.op_capture_source, served, schema.load_source_schema(served), **inputs)
        second = second_context.run(commands.op_capture_source, served, schema.load_source_schema(served), **inputs)
        assert first["capture_outcome"] == "created" and second["capture_outcome"] == "reused"
        assert second["source"]["ref"] == first["source"]["ref"]
    finally:
        second_context.run(second_scope.__exit__, None, None, None)
        first_context.run(first_scope.__exit__, None, None, None)
