"""Registry `remove`: the one-call undo of a vocabulary promotion.

The server decides from current usage. An unused vault key leaves the overlay; a
used one is deprecated so the pages that carry it keep resolving. Each case drives
the real tool over a vault whose graph projection has converged, because usage is
read from that projection.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_vocabulary_registries import _VENUE, _served

from exomem import commands, graph_sync


def _save(call, subject: str, proposal: dict, why: str, *, detail: str = "full") -> dict:
    current = call("schema_memory", {"subject": subject, "operation": "inspect"})
    result = call(
        "schema_memory",
        {
            "subject": subject,
            "operation": "save",
            "proposal": proposal,
            "expected_hash": current["content_hash"],
            "why": why,
            "response_detail": detail,
        },
    )
    # A full response keeps the registry's own report under `diagnostics`.
    return {**result, **result.get("diagnostics", {})}


def _venue_page(call, name: str) -> str:
    return call(
        "connect_memory",
        {
            "operation": "create-entity",
            "entity_type": "venue",
            "name": name,
            "summary": "A concert hall by the harbour that hosts the spring series.",
        },
    )["path"]


def _entity_type_keys(call) -> dict[str, str]:
    """Status of the two vault-added types these tests use, as the registry lists them."""
    entries = call("schema_memory", {"subject": "entity-types", "operation": "inspect", "limit": 64})
    return {
        entry["key"]: entry["status"]
        for entry in entries["entries"]
        if entry["key"] in ("venue", "arena")
    }


def test_an_unused_key_is_removed_and_a_later_save_stays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    _save(call, "entity-types", {"upsert": {"venue": _VENUE}}, "promote a place type")
    _save(
        call,
        "entity-types",
        {"upsert": {"arena": {**_VENUE, "label": "Arena", "attributes": {"folder": "Arenas"}}}},
        "a later save by another agent",
    )
    assert graph_sync.drain_active_rebuilds(timeout=60)

    removed = _save(call, "entity-types", {"remove": ["venue"]}, "the user undid the promotion")

    assert removed["valid"] is True
    assert removed["removal"]["venue"]["action"] == "removed"
    assert _entity_type_keys(call) == {"arena": "active"}, "only the reverted key left"
    history = call("schema_memory", {"subject": "entity-types", "operation": "history"})
    assert [version["why"] for version in history["versions"][:3]] == [
        "the user undid the promotion",
        "a later save by another agent",
        "promote a place type",
    ], "history keeps the promotion and its removal"


def test_a_used_key_is_deprecated_and_its_pages_are_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    _save(call, "entity-types", {"upsert": {"venue": _VENUE}}, "promote a place type")
    hall, pier = _venue_page(call, "Harbour Hall"), _venue_page(call, "Pier Stage")
    saved = {path: (vault / path).read_bytes() for path in (hall, pier)}
    assert graph_sync.drain_active_rebuilds(timeout=60)

    removed = _save(call, "entity-types", {"remove": ["venue"]}, "the user undid the promotion")

    outcome = removed["removal"]["venue"]
    assert outcome["action"] == "deprecated"
    assert outcome["dependants"]["count"] == 2
    assert sorted(outcome["dependants"]["pages"]) == sorted([hall, pier])
    assert _entity_type_keys(call) == {"venue": "deprecated"}
    assert {path: (vault / path).read_bytes() for path in (hall, pier)} == saved, "pages untouched"


def _author_edge(vault: Path, subject: str, bullet: str) -> None:
    from exomem.vault import parse_frontmatter

    _meta, body, _text = parse_frontmatter((vault / subject).read_text(encoding="utf-8"))
    commands.op_edit_memory(
        vault,
        subject,
        "Author a synthetic relation.",
        operation={
            "kind": "replace_body",
            "new_body": f"{body.rstrip()}\n\n## Relations\n{bullet}\n",
        },
    )


def test_an_unused_relation_is_removed_although_definitions_are_normally_permanent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    proposal = {"upsert": {"vault.hosts": {"parent": "relates_to", "description": "A host."}}}
    _save(call, "relations", proposal, "promote a relation")
    assert graph_sync.drain_active_rebuilds(timeout=60)

    removed = _save(call, "relations", {"remove": ["vault.hosts"]}, "the user undid it")

    assert removed["valid"] is True, removed.get("findings")
    assert removed["removal"]["vault.hosts"]["action"] == "removed"
    keys = {entry["key"] for entry in call("schema_memory", {"subject": "relations", "operation": "inspect"})["entries"]}
    assert "vault.hosts" not in keys


def test_a_used_relation_is_deprecated_to_its_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    proposal = {"upsert": {"vault.hosts": {"parent": "relates_to", "description": "A host."}}}
    _save(call, "relations", proposal, "promote a relation")
    hall, pier = _venue_page_pair(call, vault)
    _author_edge(vault, hall, f"- vault.hosts [[{pier.removesuffix('.md')}]]")
    assert graph_sync.drain_active_rebuilds(timeout=60)

    removed = _save(call, "relations", {"remove": ["vault.hosts"]}, "the user undid it")

    outcome = removed["removal"]["vault.hosts"]
    assert outcome["action"] == "deprecated"
    assert outcome["replaced_by"] == "relates_to"
    assert outcome["dependants"]["pages"] == [hall]


def _venue_page_pair(call, vault: Path) -> tuple[str, str]:
    _save(call, "entity-types", {"upsert": {"venue": _VENUE}}, "promote a place type")
    return _venue_page(call, "Harbour Hall"), _venue_page(call, "Pier Stage")


def test_a_registry_without_a_usage_check_refuses_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    _save(
        call, "statuses", {"upsert": {"awaiting-review": {"attributes": {"class": "pending"}}}},
        "add a status",
    )
    overlay_before = {
        path.name: path.read_bytes()
        for path in (vault / "Knowledge Base" / "_Schema").glob("*.yaml")
    }

    refused = _save(call, "statuses", {"remove": ["awaiting-review"]}, "undo")

    assert refused["error"]["code"] == "REMOVE_UNSUPPORTED"

    assert {
        path.name: path.read_bytes()
        for path in (vault / "Knowledge Base" / "_Schema").glob("*.yaml")
    } == overlay_before


def test_a_key_that_ships_with_exomem_cannot_be_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _vault, call = _served(tmp_path, monkeypatch)
    assert graph_sync.drain_active_rebuilds(timeout=60)

    refused = _save(call, "entity-types", {"remove": ["concept"]}, "undo")

    assert refused["error"]["code"] == "PACK_ENTRY_FIXED"


def test_removal_waits_for_usage_counts_instead_of_treating_unknown_as_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.vocabulary import usage

    vault, call = _served(tmp_path, monkeypatch)
    _save(call, "entity-types", {"upsert": {"venue": _VENUE}}, "promote a place type")
    assert graph_sync.drain_active_rebuilds(timeout=60)
    # The projection is warming, which `_graph_rows` reports as a reason and never as rows.
    monkeypatch.setattr(usage, "_graph_rows", lambda *_args: "graph_warming")

    refused = _save(call, "entity-types", {"remove": ["venue"]}, "undo")

    assert refused["error"]["code"] == "USAGE_UNAVAILABLE"
    assert "graph_warming" in refused["error"]["message"]
    assert _entity_type_keys(call) == {"venue": "active"}


def test_the_item_context_hands_over_the_registry_hash_and_the_call_that_reverts_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_promotion_revert import _entry_ref

    vault, call = _served(tmp_path, monkeypatch)
    _save(call, "entity-types", {"upsert": {"venue": _VENUE}}, "promote a place type")
    assert graph_sync.drain_active_rebuilds(timeout=60)
    ref = _entry_ref(vault, "registry")

    context = commands.op_review_item_context(vault, ref)

    current = call("schema_memory", {"subject": "entity-types", "operation": "inspect"})
    assert context["revert_fingerprint"] == current["content_hash"]
    assert context["dependants"] == [{"key": "venue", "action": "removed"}]
    arguments = {**context["revert"]["arguments"], "why": "the user undid it"}
    assert context["revert"]["tool"] == "schema_memory"
    assert "vocabulary_ref" not in arguments and "vocabulary_fingerprint" not in arguments

    # A later save changes the hash, so the hash the user was shown no longer applies.
    _save(
        call, "entity-types",
        {"upsert": {"arena": {**_VENUE, "label": "Arena", "attributes": {"folder": "Arenas"}}}},
        "a later save",
    )
    stale = call("schema_memory", arguments)
    assert stale["error"]["code"] == "STALE_REGISTRY"
    assert _entity_type_keys(call) == {"venue": "active", "arena": "active"}

    fresh = commands.op_review_item_context(vault, ref)["revert"]["arguments"]
    done = call("schema_memory", {**fresh, "why": "the user undid it"})
    assert done["ok"] is True
    assert _entity_type_keys(call) == {"arena": "active"}, "the later save stays"
    assert commands.op_review_item_context(vault, ref)["revert_fingerprint"] is None, "settled"
