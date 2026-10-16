"""Restricted stable-identity edits retain exact operational lifecycle state."""

import asyncio
import datetime as dt
import json
import shutil
from pathlib import Path

import pytest
from test_connector_boundary import configured_boundary as configured_boundary

from exomem import (
    compiled_history,
    memory_refs,
    relation_review,
    semantic_index,
    server,
    state_migration,
    temporal,
)
from exomem import vault as vault_module
from exomem.governance import principal

PAGE = "Knowledge Base/Notes/Insights/public-round.md"
IDENTITY = "11111111-1111-4111-8111-111111111111"
SOURCE = (
    "---\ntype: insight\ntitle: Public round\nstatus: active\n"
    f"exomem_id: {IDENTITY}\nproject: public-project\n---\n"
    "# Public round\n\n## Observations\n- [planning] An earlier round.\n\n"
    "## Relations\n- relates_to [[Knowledge Base/Entities/Organizations/General Context.md]]\n"
)


def test_restricted_mcp_edit_commits_stable_identity_and_prepared_state(
    configured_boundary, vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary stable-ID edits must not fail capture-only auxiliary admission."""
    _, authenticate = configured_boundary
    page = vault / PAGE
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(SOURCE, encoding="utf-8")
    anchor = vault / "Knowledge Base/Entities/Organizations/General Context.md"
    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_text(
        "---\ntype: entity\nentity_type: organization\ntitle: General Context\n"
        "status: active\nproject: public-project\n---\n# General Context\n",
        encoding="utf-8",
    )
    stop = state_migration.assert_offline_migration_authority(source="isolated lifecycle setup")
    state_migration.arm_connector_boundary_offline(vault, authority=stop)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    mcp = server.build_server(require_auth=False)
    authenticate("limited")

    async def invoke():
        written = await mcp.call_tool("edit_memory", {
            "path": PAGE, "why": "Record the completed round",
            "operation": {"kind": "replace_string", "old_string": "An earlier round.",
                          "new_string": "The completed round."},
        }, run_middleware=True)
        read = await mcp.call_tool("read_memory", {"path": PAGE, "include_history": True}, run_middleware=True)
        unit = semantic_index.current_parent_index_state(vault, PAGE).document.units[0]
        unit_read = await mcp.call_tool("read_memory", {
            "path": PAGE, "unit_ref": unit.unit_ref, "include_history": True,
        }, run_middleware=True)
        return written, read, unit_read

    written_response, read_response, unit_response = asyncio.run(invoke())
    response = written_response.structured_content
    written = response.get("result", response)
    assert written["status"] == "committed", json.dumps(written)
    final_source = page.read_text(encoding="utf-8")
    assert "The completed round." in final_source
    assert "An earlier round." not in final_source
    assert memory_refs.ref_from_markdown(final_source) == memory_refs.memory_ref(IDENTITY)
    prepared = relation_review.load_lifecycle_prepared(vault, IDENTITY)
    assert prepared is not None
    assert prepared.page_identity == IDENTITY
    assert prepared.after_path == PAGE
    assert prepared.before_source_hash == vault_module.content_hash(SOURCE)
    assert prepared.after_source_hash == vault_module.content_hash(final_source)
    assert prepared.decision_reference is None
    assert relation_review.lifecycle_prepared_path(vault, IDENTITY).read_bytes() == (
        relation_review.serialize_lifecycle_prepared(prepared).encode("utf-8")
    )
    read = read_response.structured_content
    read = read.get("result", read)
    assert "revision_history" in read, json.dumps(read)
    revision, = read["revision_history"]["revisions"]
    assert revision["parent_ref"] == memory_refs.memory_ref(IDENTITY)
    assert revision["prior"]["source"] == SOURCE
    assert revision["reason"] == "Record the completed round"
    unit_read = unit_response.structured_content
    unit_read = unit_read.get("result", unit_read)
    assert unit_read["status"] == "found"
    unit_revision, = unit_read["revision_history"]["revisions"]
    assert unit_revision["prior"]["source"] == SOURCE


def test_restricted_mcp_edit_matches_absent_and_withheld_entity_worlds(
    configured_boundary, vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A withheld entity must not change the admitted edit or its advice."""
    _, authenticate = configured_boundary
    page = vault / PAGE
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(SOURCE, encoding="utf-8")
    anchor = vault / "Knowledge Base/Entities/Organizations/General Context.md"
    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_text(
        "---\ntype: entity\nentity_type: organization\ntitle: General Context\n"
        "status: active\nproject: public-project\n---\n# General Context\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(temporal, "now", lambda: dt.datetime(2025, 1, 1, tzinfo=dt.UTC))
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    observations = []
    for world in ("absent", "withheld"):
        root = tmp_path / world
        shutil.copytree(vault, root)
        if world == "withheld":
            hidden = root / "Knowledge Base/Entities/People/withheld-visitor.md"
            hidden.parent.mkdir(parents=True, exist_ok=True)
            hidden.write_text(
                "---\ntype: entity\nentity_type: person\ntitle: Hidden Visitor\n"
                "status: active\nproject: private-project\n---\n# Hidden Visitor\n"
                "A protected identity.\n", encoding="utf-8",
            )
        stop = state_migration.assert_offline_migration_authority(source="isolated lifecycle twin setup")
        state_migration.arm_connector_boundary_offline(root, authority=stop)
        monkeypatch.setenv("EXOMEM_VAULT_PATH", str(root))
        mcp = server.build_server(require_auth=False)
        authenticate("limited")

        async def invoke(mcp=mcp):
            return await mcp.call_tool("edit_memory", {
                "path": PAGE, "why": "Record the new visitor",
                "operation": {"kind": "replace_string", "old_string": "An earlier round.",
                              "new_string": "[[Hidden Visitor]] joined."},
            }, run_middleware=True)

        response = asyncio.run(invoke()).structured_content
        written = response.get("result", response)
        assert written["status"] == "committed", json.dumps(written)
        assert "protected identity" not in json.dumps(written)
        prepared = relation_review.load_lifecycle_prepared(root, IDENTITY)
        assert prepared is not None
        assert prepared.after_source_hash == vault_module.content_hash((root / PAGE).read_text(encoding="utf-8"))
        with principal.request_scope(authenticate("limited")):
            history = compiled_history.read(root, path=PAGE)
        revision, = history["revisions"]
        assert revision["prior"]["source"] == SOURCE
        assert revision["before_hash"] == vault_module.content_hash(SOURCE)
        assert revision["after_hash"] == prepared.after_source_hash
        # Each invocation owns its clock and transition identifier.
        history["revisions"] = [{
            key: value for key, value in revision.items()
            if key not in ("at", "transition_id", "version")
        }]
        observations.append((
            (root / PAGE).read_bytes(), prepared.after_source_hash,
            {key: written.get(key) for key in (
                "status", "path", "relation_advisory", "promotion_notices", "entity_candidate",
            )}, history,
        ))
    assert observations[0] == observations[1]
