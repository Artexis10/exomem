"""Shared collection keeps caller disclosure separate from maintenance."""

import socket
from pathlib import Path

import pytest
from test_planning_governance import _write_planning_rule
from test_working_set_index import _seed_planning

from exomem import planning, working_set_index
from exomem.governance import egress
from exomem.governance.principal import (
    RequestPrincipal,
    current_principal,
    library_scope,
    request_scope,
)


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXOMEM_MODEL_OFFLINE", "1")

    def refuse_connection(*args, **kwargs):
        raise AssertionError("producer fixture attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse_connection)
    monkeypatch.setattr(socket, "create_connection", refuse_connection)


def test_library_maintenance_keeps_governed_anchors_without_appending_audit(vault: Path) -> None:
    with library_scope():
        collection = _seed_planning(vault)
    _write_planning_rule(vault, ceiling=6)
    audit = vault / "Knowledge Base" / "_Governance" / "events"
    before = {path: path.read_bytes() for path in audit.rglob("*") if path.is_file()}
    index = working_set_index.WorkingSetIndex(vault)
    try:
        with library_scope(), egress.disclosure_boundary(vault, "caller") as collector:
            who = current_principal()
            index.rebuild()
            index.update()
            assert current_principal() == who
            assert egress._collector() is collector
            assert collector.outcomes == []
        assert any(row.kind == "plan" for row in index.anchors())
        assert {path: path.read_bytes() for path in audit.rglob("*") if path.is_file()} == before

        with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
            assert len(planning.query(vault, collection)["rows"]) == 1
        after = {path: path.read_bytes() for path in audit.rglob("*") if path.is_file()}
        assert after != before
        _write_planning_rule(vault, ceiling=0)
        with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
            with pytest.raises(ValueError, match="COLLECTION_NOT_FOUND"):
                planning.query(vault, collection)
    finally:
        index.close()


def test_failed_collection_restores_the_callers_principal_and_disclosure(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = working_set_index.WorkingSetIndex(vault)

    def interrupted(**kwargs):
        raise RuntimeError("collection interrupted")

    monkeypatch.setattr(index, "_collect", interrupted)
    caller = RequestPrincipal(audience_id="external", surface="mcp")
    try:
        with request_scope(caller), egress.disclosure_boundary(vault, "caller") as collector:
            with pytest.raises(RuntimeError, match="collection interrupted"):
                index.update()
            assert current_principal() == caller
            assert egress._collector() is collector
    finally:
        index.close()
