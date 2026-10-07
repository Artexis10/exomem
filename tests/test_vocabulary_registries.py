"""The vocabulary registry contract, end to end (`add-vocabulary-registries`).

An agent promotes an entity type, sees it in bootstrap with a count, uses it,
and the owner reverts the promotion. The revert restores the registry bytes and
touches no page: the page that used the type is left as unregistered debt.
The cases after that one each pin a rule the end-to-end path cannot reach: a
stale hash, a restricted principal's save, and a hand-edited overlay.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from exomem import commands, entity_types, epistemic_graph, registry_history

_VENUE = {
    "label": "Venue",
    "guidance": "A stable place where recurring events happen.",
    "parent": "concept",
    "attributes": {"folder": "Venues"},
}


def _served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An initialized vault and an in-process MCP server over it."""
    from conftest import initialize_vault_state_offline

    from exomem import server as server_module
    from exomem.init import init_vault

    vault = tmp_path / "vault"
    init_vault(vault)
    initialize_vault_state_offline(vault, source="vocabulary registries")
    monkeypatch.setattr(server_module, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_RELEVANCE_CHECK", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    monkeypatch.setenv("EXOMEM_DISABLE_FILE_WATCHER", "1")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-lease"))
    mcp = server_module.build_server(require_auth=False)

    def call(tool: str, arguments: dict) -> dict:
        result = asyncio.run(mcp.call_tool(tool, arguments, run_middleware=True))
        return result.structured_content

    return vault, call


def _entity_types_in_bootstrap(call) -> dict:
    served = call("bootstrap", {"section": "vocabulary"})
    return served["vocabulary"]["registries"]["entity-types"]


def test_a_reverted_promotion_leaves_its_page_as_debt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call = _served(tmp_path, monkeypatch)

    # Promote a type through the generic contract.
    before = call("schema_memory", {"subject": "entity-types", "operation": "inspect"})
    assert "venue" not in {entry["key"] for entry in before["entries"]}
    promoted = call(
        "schema_memory",
        {
            "subject": "entity-types",
            "operation": "save",
            "proposal": {"upsert": {"venue": _VENUE}},
            "expected_hash": before["content_hash"],
            "why": "recurring event places need their own identity",
        },
    )
    assert promoted["saved"]["content_hash"] != before["content_hash"]

    # Bootstrap lists it with a counted zero once the projection is current.
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    listed = _entity_types_in_bootstrap(call)
    assert listed["top"]["venue"] == 0

    # Use it on a page; the count follows the projection.
    created = call(
        "connect_memory",
        {
            "operation": "create-entity",
            "entity_type": "venue",
            "name": "Harbour Hall",
            "summary": "A concert hall by the harbour that hosts the spring series.",
        },
    )
    page = vault / created["path"]
    page_bytes = page.read_bytes()
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    assert _entity_types_in_bootstrap(call)["top"]["venue"] == 1

    # The owner restores the version the promotion replaced.
    history = call("schema_memory", {"subject": "entity-types", "operation": "history"})
    newest = history["versions"][0]
    assert newest["operation"] == "save"
    assert newest["why"] == "recurring event places need their own identity"
    restored = call(
        "schema_memory",
        {
            "subject": "entity-types",
            "operation": "restore",
            "version": newest["version"],
            "expected_hash": history["content_hash"],
            "why": "the owner keeps places as concepts",
        },
    )
    assert restored["removed_keys"] == ["venue"]
    assert entity_types.load_entity_types(vault).resolve("venue") is None

    # The page keeps its bytes and is reported as unregistered debt.
    assert page.read_bytes() == page_bytes
    audit = call(
        "maintain_memory", {"mode": "audit", "categories": ["entity_type_unregistered"]}
    )
    debt = [
        finding
        for finding in audit["findings"]
        if finding["category"] == "entity_type_unregistered"
    ]
    assert [finding["path"] for finding in debt] == [created["path"]]


def test_a_stale_hash_refuses_the_second_save(vault: Path) -> None:
    current = commands.op_schema_memory(vault, subject="entity-types", operation="inspect")
    commands.op_schema_memory(
        vault,
        subject="entity-types",
        operation="save",
        proposal={"upsert": {"venue": _VENUE}},
        expected_hash=current["content_hash"],
        why="first agent",
    )
    overlay = entity_types.extension_registry_path(vault)
    first = overlay.read_bytes()

    with pytest.raises(ValueError, match="STALE"):
        commands.op_schema_memory(
            vault,
            subject="entity-types",
            operation="save",
            proposal={"upsert": {"arena": {**_VENUE, "label": "Arena",
                                            "attributes": {"folder": "Arenas"}}}},
            expected_hash=current["content_hash"],
            why="second agent, same read",
        )
    assert overlay.read_bytes() == first

    # A fresh proposal for the same idea sees the first agent's entry.
    proposed = commands.op_schema_memory(
        vault,
        subject="entity-types",
        operation="propose",
        proposal={"upsert": {"venues": {**_VENUE, "attributes": {"folder": "Venue Places"}}}},
    )
    assert "venue" in {item["key"] for item in proposed["collisions"]}


def _govern(vault: Path) -> None:
    """A governed policy: one withheld folder for the `external` audience."""
    from exomem.governance import egress

    governance = vault / "Knowledge Base" / "_Governance"
    (governance / "scopes").mkdir(parents=True, exist_ok=True)
    (governance / "rules").mkdir(parents=True, exist_ok=True)
    (governance / "scopes" / "withheld.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\nname: Withheld\n"
        'paths: ["Notes/Withheld/**"]\n',
        encoding="utf-8",
    )
    (governance / "rules" / "withheld-external.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\n'
        f"audience: external\nceiling: {egress.LEVEL_NONE}\n",
        encoding="utf-8",
    )


def _reset_governance() -> None:
    from exomem import find as find_module
    from exomem.governance import egress, membership, policy

    policy._CACHE.clear()
    membership.clear_memo()
    egress.clear_decision_memo()
    find_module.clear_cache()


def test_a_restricted_save_becomes_a_pending_proposal(vault: Path) -> None:
    from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope

    _govern(vault)
    _reset_governance()
    before = entity_types.load_entity_types(vault).extension_hash
    with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
        pending = commands.op_schema_memory(
            vault,
            subject="entity-types",
            operation="save",
            proposal={"upsert": {"venue": _VENUE}},
            expected_hash=before,
            why="the delegate wants a place type",
        )
        # Counts and reasons are the owner's.
        inspected = commands.op_schema_memory(vault, subject="entity-types", operation="inspect")
    assert pending["state"] == "pending_review"
    assert entity_types.load_entity_types(vault).extension_hash == before
    assert entity_types.load_entity_types(vault).resolve("venue") is None
    assert inspected["counts"] == "unavailable"
    assert inspected["reason"] == "audience_restricted"

    _reset_governance()
    with request_scope(owner_principal(surface="mcp")):
        queue = commands.op_review_memory(vault, mode="vocabulary")
    items = [item for item in queue["items"] if item["ref"] == pending["item_ref"]]
    assert len(items) == 1
    assert "the delegate wants a place type" in items[0]["question"]
    assert '"venue"' in items[0]["question"]


def test_a_hand_edited_overlay_is_read_with_findings_and_kept_on_the_next_save(
    vault: Path,
) -> None:
    overlay = entity_types.extension_registry_path(vault)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "entity_types": {
                    "site": {
                        "folder": "Sites",
                        "label": "Site",
                        "aliases": [],
                        "capture_guidance": "A stable physical site identity.",
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    assert entity_types.load_entity_types(vault).resolve("site") is not None

    # The owner adds an invalid entry by hand; the loaded registry follows the
    # edit without a restart, keeps the valid entry and reports the other.
    hand_edited = overlay.read_text(encoding="utf-8") + (
        "  person-two:\n    folder: People\n    label: Person\n"
    )
    overlay.write_text(hand_edited, encoding="utf-8")
    inspected = commands.op_schema_memory(vault, subject="entity-types", operation="inspect")
    assert "site" in {entry["key"] for entry in inspected["entries"]}
    assert "person-two" not in {entry["key"] for entry in inspected["entries"]}
    assert any(finding.get("entity_type") == "person-two" for finding in inspected["findings"])

    # The next governed save snapshots the hand-edited bytes exactly.
    commands.op_schema_memory(
        vault,
        subject="entity-types",
        operation="save",
        proposal={"upsert": {"venue": _VENUE}},
        expected_hash=inspected["content_hash"],
        why="add a place type",
    )
    newest = registry_history.versions(vault, stem="entity-types")[0]
    assert (
        registry_history.read_version(vault, stem="entity-types", version=newest["version"])
        == hand_edited
    )
