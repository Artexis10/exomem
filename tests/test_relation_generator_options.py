"""Generators leave the relation choice to the agent (`promote-referents-without-nudges` task 2.9).

A relation candidate that could only say two pages are related, whatever their types, carries
the ranked relations for the pair and the plain link, and queue acceptance needs the agent to
name one of them.
Cases go through the public MCP tools over an initialized vault. Naming one of the offered
relations needs no further step: `suggest-relations` returns the accept fields and the accept
follows. Naming a relation the candidate does not offer takes the agent's recorded vocabulary
choice: a meaning question, a reuse decision, then the accept.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from test_curation_execution import _propose
from test_relation_endpoints import _entity, _extension, _save_extension, _served

from exomem import (
    command_surface,
    commands,
    curation,
    epistemic_graph,
    find,
    freshness,
    semantic_contract,
)

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
    assert "member_of" in keys
    assert len(keys) <= 8
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


def _decided_choice(
    call, group: dict[str, Any], item: dict[str, Any], relation: str
) -> dict[str, str]:
    """The agent's recorded choice of `relation` for the candidate, as the accept route needs it.

    Returns the `vocabulary_ref` and `vocabulary_fingerprint` of the decided meaning question.
    """
    asked = call(
        "triage_memory",
        {
            "operation": {
                "kind": "question",
                "path": group["path"],
                "query": f"Which relation fits {item['from']} and {item['to']}?",
                "family": "relation-type/v1",
                "ref": item["ref"],
            }
        },
    )
    reviewed = asked["item"]
    decided = call(
        "triage_memory",
        {
            "operation": {
                "kind": "decision",
                "ref": reviewed["ref"],
                "action": "decide-vocabulary",
                "decision": {
                    "item_ref": reviewed["ref"],
                    "fingerprint": reviewed["fingerprint"],
                    "family": reviewed["family"],
                    "registry_hashes": reviewed["registry_hashes"],
                    "target_versions": reviewed["target_versions"],
                    "outcome": "reuse",
                    "choice": {"canonical": relation},
                    "rationale": "The offered relation states what the source says.",
                },
            }
        },
    )
    assert decided["state"] == "proposed", decided
    return {
        "vocabulary_ref": reviewed["ref"],
        "vocabulary_fingerprint": reviewed["fingerprint"],
    }


def _accept_as(
    call, group: dict[str, Any], item: dict[str, Any], relation: str, binding: dict[str, str]
) -> dict[str, Any]:
    """Accept with a chosen relation. A vocabulary-bound write needs an explicit idempotency key."""
    arguments = {
        "operation": {
            "kind": "accept-relation",
            "ref": item["ref"],
            "path": group["path"],
            "expected_hash": group["content_hash"],
            "expected_fingerprint": item["fingerprint"],
            "why": "Reviewed the shared source.",
            "requested_relation": relation,
            **binding,
        }
    }

    async def keyed() -> dict[str, Any]:
        async with Client(call.mcp) as client:
            result = await client.call_tool(
                "change_connections",
                arguments,
                raise_on_error=False,
                meta={command_surface.MCP_IDEMPOTENCY_META_KEY: "accept-owns-1"},
            )
        content = result.structured_content
        return content["result"] if set(content) == {"result"} else content

    return asyncio.run(keyed())


def test_an_offered_relation_the_agent_decided_is_written_as_one_typed_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call, person, organization = _person_and_organization(tmp_path, monkeypatch)
    group, item = _shared_source_item(call, person, organization)
    binding = _decided_choice(call, group, item, "member_of")

    accepted = _accept_as(call, group, item, "member_of", binding)

    assert "error" not in accepted, accepted
    relations = [
        line
        for line in (vault / person).read_text(encoding="utf-8").splitlines()
        if f"[[{organization.removesuffix('.md')}]]" in line
    ]
    assert relations == [f"- member_of [[{organization.removesuffix('.md')}]]"]


def test_a_choice_recorded_before_the_registry_changed_is_refused_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call, person, organization = _person_and_organization(tmp_path, monkeypatch)
    group, item = _shared_source_item(call, person, organization)
    binding = _decided_choice(call, group, item, "member_of")
    before = (vault / person).read_bytes()
    _save_extension(call, "vault.keeps", _extension("owns"))

    refused = _accept_as(call, group, item, "member_of", binding)

    assert refused["error"]["code"] == "VOCABULARY_DECISION_STALE", refused
    assert (vault / person).read_bytes() == before


def _suggested_candidate(call, page: str, target: str) -> dict[str, Any]:
    """The shared-source candidate that `suggest-relations` returns from `page` to `target`."""
    suggested = call(
        "read_connections", {"operation": {"kind": "suggest-relations", "path": page}}
    )
    [candidate] = [
        c for c in suggested["candidates"] if c["to"] == target and c["method"] == "shared_sources"
    ]
    return candidate


def _accept_suggested(
    call, page: str, candidate: dict[str, Any], relation: str
) -> dict[str, Any]:
    """Accept from the candidate's `review` fields, naming `relation`, with no recorded choice."""
    review = candidate["review"]
    return call(
        "change_connections",
        {
            "operation": {
                "kind": "accept-relation",
                "ref": review["ref"],
                "path": page,
                "expected_hash": review["source_content_hash"],
                "expected_fingerprint": review["fingerprint"],
                "why": "The user confirmed that exact relation.",
                "requested_relation": relation,
            }
        },
    )


def test_an_offered_relation_is_accepted_from_its_suggestion_in_one_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call, person, organization = _person_and_organization(tmp_path, monkeypatch)
    candidate = _suggested_candidate(call, person, organization)

    accepted = _accept_suggested(call, person, candidate, "member_of")

    assert accepted["mutated"] is True, accepted
    relations = [
        line
        for line in (vault / person).read_text(encoding="utf-8").splitlines()
        if f"[[{organization.removesuffix('.md')}]]" in line
    ]
    assert relations == [f"- member_of [[{organization.removesuffix('.md')}]]"]
    queue = call("review_memory", {"mode": "relation-queue"})
    held = [
        item
        for group in queue["groups"]
        for item in group["items"]
        if (item["from"], item["to"], item["method"]) == (person, organization, "shared_sources")
    ]
    assert held == []


def test_a_relation_the_candidate_does_not_offer_still_needs_a_recorded_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call, person, organization = _person_and_organization(tmp_path, monkeypatch)
    candidate = _suggested_candidate(call, person, organization)
    # `owns` is active and joins a person to an organization, but only eight relations are offered.
    assert "owns" not in [option["key"] for option in candidate["options"]]
    before = (vault / person).read_bytes()

    refused = _accept_suggested(call, person, candidate, "owns")

    assert refused["error"]["code"] == "VOCABULARY_APPLICATION_INVALID", refused
    assert "vocabulary question" in refused["error"]["remediation"]
    assert (vault / person).read_bytes() == before


def test_suggest_relations_carries_no_review_for_a_candidate_the_queue_has_dismissed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _vault, call, person, organization = _person_and_organization(tmp_path, monkeypatch)
    group, item = _shared_source_item(call, person, organization)
    assert "review" in _suggested_candidate(call, person, organization)
    dismissed = call(
        "triage_memory",
        {
            "operation": {
                "kind": "decision",
                "ref": item["ref"],
                "action": "dismiss",
                "source_path": group["path"],
                "why": "false_positive: the pair is only incidental.",
                "expected_fingerprint": item["fingerprint"],
            }
        },
    )
    assert dismissed["state"] == "dismissed", dismissed

    candidate = _suggested_candidate(call, person, organization)

    assert "review" not in candidate


def test_suggest_relations_says_so_when_the_queue_cannot_confirm_a_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import relation_queue

    _vault, call, person, organization = _person_and_organization(tmp_path, monkeypatch)
    # The reference authority moved while the queue was read, so no candidate can be confirmed.
    monkeypatch.setattr(relation_queue, "_hinted_candidate_refs", lambda *args, **kwargs: None)

    suggested = call(
        "read_connections", {"operation": {"kind": "suggest-relations", "path": person}}
    )

    [candidate] = [c for c in suggested["candidates"] if c["to"] == organization]
    assert "review" not in candidate
    assert any("review" in warning for warning in suggested["warnings"]), suggested["warnings"]


def test_suggest_relations_does_not_echo_the_cause_when_the_queue_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import relation_queue

    _vault, call, person, organization = _person_and_organization(tmp_path, monkeypatch)

    def unreadable(*_args: object, **_kwargs: object) -> None:
        raise OSError(13, "Permission denied", "/private/state/review-state.json")

    monkeypatch.setattr(relation_queue, "open_reviews", unreadable)

    suggested = call(
        "read_connections", {"operation": {"kind": "suggest-relations", "path": person}}
    )

    assert any(c["to"] == organization for c in suggested["candidates"])
    assert any("review fields unavailable" in w for w in suggested["warnings"]), suggested
    assert any("(PermissionError)" in w for w in suggested["warnings"]), suggested["warnings"]
    assert not any("/private" in w or "denied" in w for w in suggested["warnings"])


def test_suggest_relations_does_not_warn_for_edges_the_page_already_authors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    person = _entity(call, "person", "Ada Marsh")["path"]
    organization = _entity(call, "organization", "Birch Works")["path"]
    other = _entity(call, "organization", "Cedar Works")["path"]
    source = call(
        "capture_source",
        {"title": "Shared source", "source_kind": "session", "content": "Three pages cite this."},
    )["path"]
    for page in (person, organization, other):
        _cite(vault, page, source)
    _rebuild(vault, monkeypatch)
    accepted = _accept_suggested(
        call, person, _suggested_candidate(call, person, organization), "member_of"
    )
    assert accepted["mutated"] is True, accepted
    # The author also writes a generic edge to the other organization by hand.
    page = vault / person
    generic = f"- relates_to [[{other.removesuffix('.md')}]]"
    text = page.read_text(encoding="utf-8").rstrip("\n")
    page.write_text(f"{text}\n{generic}\n", encoding="utf-8")
    _rebuild(vault, monkeypatch)

    suggested = call(
        "read_connections", {"operation": {"kind": "suggest-relations", "path": person}}
    )

    # Both edges sit under `## Relations`, which the suggestion generators read as links and the
    # queue leaves out as authored: a candidate the queue omits on purpose is not a failed lookup.
    assert not [w for w in suggested["warnings"] if "review fields" in w], suggested["warnings"]
    assert all(
        "review" not in c for c in suggested["candidates"] if c["to"] in {organization, other}
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
    assert "member_of" in [option["key"] for option in candidate["options"]]


def test_a_pair_the_other_page_already_relates_specifically_is_not_suggested_as_generic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    person = _entity(call, "person", "Ada Marsh")["path"]
    # The organization, not the person, authors the specific relation toward the person.
    typed = _entity(
        call,
        "organization",
        "Birch Works",
        relations=[
            {"kind": "supplies", "target": person.removesuffix(".md")}
        ],
    )
    assert typed["state"] == "committed", typed
    untyped = _entity(call, "organization", "Cedar Works")
    source = call(
        "capture_source",
        {"title": "Shared source", "source_kind": "session", "content": "Three pages cite this."},
    )["path"]
    for page in (person, typed["path"], untyped["path"]):
        _cite(vault, page, source)
    _rebuild(vault, monkeypatch)

    suggested = call(
        "read_connections", {"operation": {"kind": "suggest-relations", "path": person}}
    )

    shared = {c["to"] for c in suggested["candidates"] if c["method"] == "shared_sources"}
    assert untyped["path"] in shared
    assert typed["path"] not in shared


def _two_plain_notes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Two notes that cite one source, and the queue's candidate between them."""
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
    return vault, call, pages, group, item


def _accept_requesting(call, group: dict[str, Any], item: dict[str, Any], relation: str | None):
    operation = {
        "kind": "accept-relation",
        "ref": item["ref"],
        "path": group["path"],
        "expected_hash": group["content_hash"],
        "expected_fingerprint": item["fingerprint"],
        "why": "Reviewed the shared source.",
    }
    if relation is not None:
        operation["requested_relation"] = relation
    return call("change_connections", {"operation": operation})


def test_a_candidate_between_two_plain_notes_offers_relations_and_the_plain_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call, pages, group, item = _two_plain_notes(tmp_path, monkeypatch)
    before = (vault / pages[0]).read_bytes()

    assert 0 < len(item["options"]) <= 8
    assert item["bullet"] is None
    assert item["relation_type"] is None
    assert item["plain_link"] == "links_to"
    refused = _accept_requesting(call, group, item, None)

    assert refused["error"]["code"] == "RELATION_CHOICE_REQUIRED", refused
    assert refused["error"]["plain_link"] == item["plain_link"]
    assert (vault / pages[0]).read_bytes() == before


def test_an_offered_relation_between_two_plain_notes_is_accepted_without_a_recorded_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call, pages, group, item = _two_plain_notes(tmp_path, monkeypatch)
    chosen = item["options"][0]["key"]

    accepted = _accept_requesting(call, group, item, chosen)

    assert accepted["mutated"] is True, accepted
    assert f"- {chosen} [[{pages[1].removesuffix('.md')}]]" in (vault / pages[0]).read_text(
        encoding="utf-8"
    )


def test_the_plain_link_between_two_plain_notes_is_accepted_as_the_last_resort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call, pages, group, item = _two_plain_notes(tmp_path, monkeypatch)

    accepted = _accept_requesting(call, group, item, item["plain_link"])

    assert accepted["mutated"] is True, accepted
    assert f"- links_to [[{pages[1].removesuffix('.md')}]]" in (vault / pages[0]).read_text(
        encoding="utf-8"
    )


def test_naming_the_generic_relation_for_a_candidate_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call, pages, group, item = _two_plain_notes(tmp_path, monkeypatch)
    before = (vault / pages[0]).read_bytes()

    refused = _accept_requesting(call, group, item, "relates_to")

    assert refused["error"]["code"] == "RELATION_CHOICE_REQUIRED", refused
    assert (vault / pages[0]).read_bytes() == before


def _entity_pair_in_fixture_vault(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, str, dict, str]:
    """A person and an organization that cite one source, and the queue's candidate between them."""
    source = "Knowledge Base/Sources/Sessions/2026-10-31-curation-shared-source.md"
    pages = {
        source: "---\ntype: source\ntitle: Curation shared source\n---\n# Curation shared source\n\nRaw notes.\n",
        "Knowledge Base/Entities/People/Ada Marsh.md": _entity_page("Ada Marsh", "person", source),
        "Knowledge Base/Entities/Organizations/Birch Works.md": _entity_page(
            "Birch Works", "organization", source
        ),
    }
    for rel, text in pages.items():
        (vault / rel).parent.mkdir(parents=True, exist_ok=True)
        (vault / rel).write_text(text, encoding="utf-8")
    person, organization = list(pages)[1:]
    _rebuild(vault, monkeypatch)
    review = commands.op_review_memory(vault, mode="relation-queue")
    [(group, item)] = [
        (group, item)
        for group in review["groups"]
        for item in group["items"]
        if (item["from"], item["to"], item["method"]) == (person, organization, "shared_sources")
    ]
    return person, organization, item, group["content_hash"]


def _entity_page(title: str, entity_type: str, source: str) -> str:
    return (
        f"---\ntype: entity\nentity_type: {entity_type}\ntitle: {title}\nstatus: active\n"
        f"sources:\n  - {source}\n---\n# {title}\n\n## Summary\n\n{title} is an invented entity.\n"
    )


def _accept_step(item: dict[str, Any], expected_hash: str, **extra: str) -> dict[str, Any]:
    return {
        "step_id": "relation",
        "kind": "accept-relation",
        "args": {
            "ref": item["ref"],
            "expected_hash": expected_hash,
            "why": "Accept the reviewed relation candidate.",
            "expected_fingerprint": item["fingerprint"],
            **extra,
        },
    }


def test_a_curation_plan_that_leaves_the_generic_relation_where_a_specific_one_fits_is_refused(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    person, _organization, item, expected_hash = _entity_pair_in_fixture_vault(vault, monkeypatch)
    before = (vault / person).read_bytes()

    with pytest.raises(curation.CurationError) as refused:
        _propose(vault, _accept_step(item, expected_hash))

    assert refused.value.code == "RELATION_CHOICE_REQUIRED"
    assert '"key":"member_of"' in str(refused.value)
    assert (vault / person).read_bytes() == before


def test_a_curation_step_cannot_choose_the_relation_itself(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The choice is a recorded vocabulary decision; a plan step carries no such binding.
    person, _organization, item, expected_hash = _entity_pair_in_fixture_vault(vault, monkeypatch)
    before = (vault / person).read_bytes()

    with pytest.raises(curation.CurationError) as refused:
        _propose(vault, _accept_step(item, expected_hash, requested_relation="owns"))

    assert refused.value.code == "CURATION_UNKNOWN_FIELD"
    assert (vault / person).read_bytes() == before
