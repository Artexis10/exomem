"""Reverting a recent promotion: one call, bound to what the caller read.

These tests drive the real writers, the real review tools and the real curation
executor; they change only the caller identity and, for the withheld case, the policy.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import command_surface, commands, promotions, review_state
from exomem import due_state as due_state_module

ENTITIES = "Knowledge Base/Entities/Concepts"
NOTE = "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md"
OTHER_NOTE = "Knowledge Base/Notes/Insights/rrf-fusion-beats-score-normalization.md"


@pytest.fixture(autouse=True)
def _clean_emission_state():
    due_state_module.reset_emission_state()
    yield
    due_state_module.reset_emission_state()


def _as(monkeypatch: pytest.MonkeyPatch, session: str | None) -> None:
    monkeypatch.setattr(
        command_surface,
        "mcp_caller_identity",
        lambda: {
            "client_name": None, "client_version": None, "app_id": None,
            "transport": "stdio" if session else None, "session_id": session,
        },
    )


def _create_entity(vault: Path, name: str, *, ref: str | None = None, **extra) -> str:
    return commands.op_connect_memory(
        vault,
        operation="create-entity",
        entity_type="concept",
        name=name,
        summary="A synthetic accounting concept used to exercise reverts.",
        ref=ref,
        **extra,
    )["path"]


def _append_relation(vault: Path, path: str, bullet: str) -> None:
    from exomem.vault import parse_frontmatter

    _meta, body, _text = parse_frontmatter((vault / path).read_text(encoding="utf-8"))
    section = "" if "## Relations" in body else "\n## Relations\n"
    commands.op_edit_memory(
        vault, path, "Author a synthetic relation.",
        operation={"kind": "replace_body", "new_body": f"{body.rstrip()}\n{section}{bullet}\n"},
    )


def _without_stamp(text: str) -> str:
    return "".join(line for line in text.splitlines(keepends=True) if not line.startswith("updated:"))


def _entry_ref(vault: Path, kind: str) -> str:
    ledger = review_state.ReviewStateStore(vault).load()["promotions"]
    ids = [key for key, row in ledger.items() if row["kind"] == kind]
    assert ids, f"no {kind} entry in the ledger"
    return promotions.promotion_ref(ids[-1])


def _revert(vault: Path, ref: str, fingerprint: str | None, why: str = "The user asked to undo it.") -> dict:
    return commands.op_maintain_memory(
        vault, mode="curation", curation_action="revert", review_ref=ref,
        expected_plan_fingerprint=fingerprint, why=why,
    )


def _context(vault: Path, ref: str) -> dict:
    return commands.op_review_item_context(vault, ref)


def _edge_vault(vault: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str, str]:
    """Two entities and one promoted edge between them; returns (subject, target, edge ref)."""
    _as(monkeypatch, "session-a")
    target = _create_entity(vault, "Tidal Ledger")
    subject = _create_entity(vault, "Harbour Reconciliation")
    _append_relation(vault, subject, f"- supports [[{target.removesuffix('.md')}]]")
    return subject, target, _entry_ref(vault, "edge")


def test_an_edge_reverts_in_one_call_and_only_that_bullet_goes(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject, target, ref = _edge_vault(vault, monkeypatch)
    _append_relation(vault, subject, "- relates_to [[Knowledge Base/Notes/Insights/rrf-fusion-beats-score-normalization]]")
    page = vault / subject
    before = page.read_text(encoding="utf-8")
    edge_line = f"- supports [[{target.removesuffix('.md')}]]\n"
    assert edge_line in before

    _as(monkeypatch, "session-b")
    fingerprint = _context(vault, ref)["revert_fingerprint"]
    result = _revert(vault, ref, fingerprint)

    assert result["outcome"] == "reverted"
    after = page.read_text(encoding="utf-8")
    # The editor restamps `updated`; everything else is as the page was, minus the bullet.
    assert _without_stamp(after) == _without_stamp(before).replace(edge_line, ""), (
        "exactly that bullet left, the rest is byte for byte"
    )
    assert _context(vault, ref)["item"]["subjects"] == [], "the entry no longer holds"
    assert _revert(vault, ref, fingerprint)["outcome"] == "absent", "a repeat reports absent"


def test_a_revert_needs_the_current_fingerprint_and_a_stale_one_changes_nothing(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject, _target, ref = _edge_vault(vault, monkeypatch)
    page = vault / subject
    _as(monkeypatch, "session-b")
    fingerprint = _context(vault, ref)["revert_fingerprint"]

    _append_relation(vault, subject, "- relates_to [[Knowledge Base/Notes/Insights/rrf-fusion-beats-score-normalization]]")
    edited = page.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="CURATION_PLAN_IDENTITY_MISMATCH"):
        _revert(vault, ref, fingerprint)
    with pytest.raises(ValueError, match="CURATION_PLAN_IDENTITY_MISMATCH"):
        _revert(vault, ref, "0" * 64)
    assert page.read_text(encoding="utf-8") == edited, "a refused revert changes nothing"

    assert _revert(vault, ref, _context(vault, ref)["revert_fingerprint"])["outcome"] == "reverted"


def test_the_leaf_keeps_an_unrelated_edit_made_after_the_plan_was_read(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import promotion_revert

    subject, target, _ref = _edge_vault(vault, monkeypatch)
    page = vault / subject
    _append_relation(vault, subject, "- relates_to [[Knowledge Base/Notes/Insights/rrf-fusion-beats-score-normalization]]")

    promotion_revert.remove_relation(
        vault, subject=subject, predicate="supports", target=target, why="Revert it."
    )

    text = page.read_text(encoding="utf-8")
    assert "rrf-fusion-beats-score-normalization" in text, "the concurrent edit survived"
    assert f"[[{target.removesuffix('.md')}]]" not in text


def _link_from(vault: Path, note: str, entity: str) -> None:
    """Make an ordinary note link to an entity, as a later write would."""
    from exomem.vault import parse_frontmatter

    _meta, body, _text = parse_frontmatter((vault / note).read_text(encoding="utf-8"))
    commands.op_edit_memory(
        vault, note, "Mention the entity.",
        operation={
            "kind": "replace_body",
            "new_body": f"{body.rstrip()}\n\nSee also [[{entity.removesuffix('.md')}]].\n",
        },
    )


def test_an_entity_reverts_to_the_trash_and_names_every_link_but_the_one_that_made_it(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as(monkeypatch, "session-a")
    entity = _create_entity(vault, "Tidal Ledger", ref=NOTE)
    _link_from(vault, NOTE, entity)
    ref = _entry_ref(vault, "entity")

    _as(monkeypatch, "session-b")
    first = _context(vault, ref)
    assert first["dependants"] == [], "the originating note's link is not a dependant"

    _link_from(vault, OTHER_NOTE, entity)
    with pytest.raises(ValueError, match="CURATION_PLAN_IDENTITY_MISMATCH"):
        _revert(vault, ref, first["revert_fingerprint"])
    assert (vault / entity).is_file(), "a stale revert changes nothing"

    second = _context(vault, ref)
    assert [link["path"] for link in second["dependants"]] == [OTHER_NOTE]
    assert second["revert_fingerprint"] != first["revert_fingerprint"]

    result = _revert(vault, ref, second["revert_fingerprint"])

    assert result["outcome"] == "reverted"
    assert not (vault / entity).exists(), "the page left the vault"
    trashed = list((vault / "Knowledge Base" / "_trash").rglob("*Tidal Ledger.md".replace(" ", "*")))
    assert trashed, "and went to the trash, where it can be recovered"
    assert entity.removesuffix(".md") in (vault / OTHER_NOTE).read_text(encoding="utf-8"), (
        "a dependant the caller named is left as it was"
    )


def test_an_entity_revert_removes_the_edges_of_its_own_write_and_no_other_edge(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as(monkeypatch, "session-a")
    entity = _create_entity(vault, "Tidal Ledger")
    kept = _create_entity(vault, "Harbour Reconciliation")
    other = _create_entity(vault, "Ledger Audit")
    stem = entity.removesuffix(".md")
    _append_relation(vault, kept, f"- supports [[{stem}]]")
    _append_relation(vault, other, f"- supports [[{stem}]]")
    entity_id, entity_entry = next(
        (key, row) for key, row in review_state.ReviewStateStore(vault).load()["promotions"].items()
        if row["kind"] == "entity" and row["subjects"] == [{"path": entity}]
    )
    # `kept`'s bullet was authored by the write that made the entity; `other`'s was not.
    promotions.record(
        vault, "edge", [{"subject": kept, "predicate": "supports", "target": entity}],
        write=entity_entry["write"],
    )
    entity_ref = promotions.promotion_ref(entity_id)

    _as(monkeypatch, "session-b")
    context = _context(vault, entity_ref)
    assert [link["path"] for link in context["dependants"]] == [other], "only the other write's edge is named"
    _revert(vault, entity_ref, context["revert_fingerprint"])

    assert not (vault / entity).exists()
    assert f"[[{stem}]]" not in (vault / kept).read_text(encoding="utf-8"), "the write's own edge went"
    assert f"[[{stem}]]" in (vault / other).read_text(encoding="utf-8"), "another write's edge stayed"
