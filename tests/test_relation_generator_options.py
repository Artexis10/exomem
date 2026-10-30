"""Generators leave the relation choice to the agent (`promote-referents-without-nudges` task 2.9).

Where a specific relation fits both pages, a relation candidate carries those relations and
queue acceptance needs the agent to name one. Where none fits, acceptance works as before.
Cases go through the public MCP tools over an initialized vault; the success path calls the
queue's `accept` directly because the public door also needs a vocabulary review binding that
is no part of this behaviour.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_relation_endpoints import _entity, _served

from exomem import edit as edit_module
from exomem import epistemic_graph, find, freshness, relation_queue, semantic_contract

NOTES = "Knowledge Base/Notes/Insights"


def _rebuild(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the graph and the reference authority current, as the queue read needs."""
    find.clear_cache()
    semantic_contract.reset_corpus_context_cache()
    freshness.clear()
    freshness.rebaseline(vault)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    semantic_contract.build_corpus_context(vault)


def _cite(vault: Path, page: str, source: str) -> None:
    """Add `source` to the page's `sources` frontmatter, which makes the two pages share it."""
    path = vault / page
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("\n---\n", f"\nsources:\n  - {source}\n---\n", 1), encoding="utf-8"
    )


def _shared_source_item(
    call, from_page: str, to_page: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    queue = call("review_memory", {"mode": "relation-queue"})
    for group in queue["groups"]:
        for item in group["items"]:
            if item["method"] == "shared_sources" and (item["from"], item["to"]) == (
                from_page,
                to_page,
            ):
                return group, item
    raise AssertionError(f"no shared-source candidate {from_page} -> {to_page}: {queue}")


def _person_and_organization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A person and an organization that cite one source, and the queue's candidate between them."""
    vault, call = _served(tmp_path, monkeypatch)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    person = _entity(call, "person", "Ada Marsh")["path"]
    organization = _entity(call, "organization", "Birch Works")["path"]
    source = call(
        "capture_source",
        {
            "title": "Shared source",
            "source_kind": "session",
            "content": "Ada Marsh runs Birch Works.",
        },
    )["path"]
    for page in (person, organization):
        _cite(vault, page, source)
    _rebuild(vault, monkeypatch)
    return vault, call, person, organization


def _accept(call, group: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    return call(
        "change_connections",
        {
            "operation": {
                "kind": "accept-relation",
                "ref": item["ref"],
                "path": group["path"],
                "expected_hash": group["content_hash"],
                "expected_fingerprint": item["fingerprint"],
                "why": "Reviewed the shared source.",
            }
        },
    )


def test_a_candidate_between_a_person_and_an_organization_offers_relations_and_no_bullet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _vault, call, person, organization = _person_and_organization(tmp_path, monkeypatch)

    _group, item = _shared_source_item(call, person, organization)

    keys = [option["key"] for option in item["options"]]
    assert "owns" in keys
    assert len(keys) <= 4
    # The queue authors no relation for the agent: nothing to copy, only the options to choose from.
    assert item["bullet"] is None


def test_accepting_without_naming_a_relation_is_refused_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call, person, organization = _person_and_organization(tmp_path, monkeypatch)
    group, item = _shared_source_item(call, person, organization)
    before = (vault / person).read_bytes()

    refused = _accept(call, group, item)

    assert refused["error"]["code"] == "RELATION_CHOICE_REQUIRED", refused
    assert [option["key"] for option in refused["error"]["options"]] == [
        o["key"] for o in item["options"]
    ]
    assert refused["mutated"] is False
    assert (vault / person).read_bytes() == before


def test_accepting_with_an_offered_relation_writes_that_relation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call, person, organization = _person_and_organization(tmp_path, monkeypatch)
    group, item = _shared_source_item(call, person, organization)

    def edit_memory(vault_root: Path, **kw: Any) -> dict:
        return edit_module.edit(vault_root, create_missing_section=True, **kw).as_dict()

    accepted = relation_queue.accept(
        vault,
        ref=item["ref"],
        expected_hash=group["content_hash"],
        why="Reviewed the shared source.",
        expected_fingerprint=item["fingerprint"],
        requested_relation="owns",
        edit_memory=edit_memory,
    )

    assert accepted["relation_type"] == "owns"
    assert f"- owns [[{organization.removesuffix('.md')}]]" in (vault / person).read_text(
        encoding="utf-8"
    )


def test_suggest_relations_carries_the_fitting_relations_for_the_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _vault, call, person, organization = _person_and_organization(tmp_path, monkeypatch)

    suggested = call(
        "read_connections", {"operation": {"kind": "suggest-relations", "path": person}}
    )

    [candidate] = [
        c
        for c in suggested["candidates"]
        if c["to"] == organization and c["method"] == "shared_sources"
    ]
    assert "owns" in [option["key"] for option in candidate["options"]]


def test_a_candidate_between_two_plain_notes_still_accepts_without_naming_a_relation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    source = call(
        "capture_source",
        {"title": "Shared source", "source_kind": "session", "content": "Two notes cite this."},
    )["path"]
    pages = []
    for title in ("Acorn notes", "Birch notes"):
        page = f"{NOTES}/{title.lower().replace(' ', '-')}.md"
        (vault / page).parent.mkdir(parents=True, exist_ok=True)
        (vault / page).write_text(
            f"---\ntype: insight\nstatus: active\nsources:\n  - {source}\n---\n# {title}\n\n{title} are an invented note.\n\n## Observations\n\n- [finding] The {title} were reviewed.\n",
            encoding="utf-8",
        )
        pages.append(page)
    _rebuild(vault, monkeypatch)
    group, item = _shared_source_item(call, *pages)

    assert "options" not in item
    accepted = _accept(call, group, item)

    assert accepted["mutated"] is True, accepted
    assert f"- relates_to [[{pages[1].removesuffix('.md')}]]" in (vault / pages[0]).read_text(
        encoding="utf-8"
    )
