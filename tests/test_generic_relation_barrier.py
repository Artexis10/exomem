"""The generic-relation barrier, end to end (`refuse-generic-relation-authoring`).

A write that adds a generic edge, between two notes as much as to an entity, stops before
it commits. The refusal hands the writer a ranked menu of specific relations and the three
routes that end it: retry with a specific relation, propose a new relation type, or write
a plain link. Every case goes through the public tools over an initialized vault; the
restricted-caller case calls the same writer in process.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from test_relation_endpoints import _entity, _extension, _page_path, _save_extension, _served

from exomem import (
    agent_refusal,
    note,
    relation_barrier,
    relation_registry,
    semantic_contract,
    semantic_language_registry,
)

CODE = "GENERIC_RELATION_REFUSED"
# Page ids are random per vault, and the digests taken over them differ with them.
_RANDOM_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{64}")


def _source(call, title: str) -> str:
    captured = call(
        "capture_source",
        {
            "title": f"{title} source",
            "source_kind": "session",
            "content": f"{title} was discussed.",
            "response_detail": "full",
        },
    )
    return f"{_page_path(captured)}#Capture"


def _body(title: str, *rows: str) -> str:
    relations = "".join(f"- {row}\n" for row in rows)
    return f"## Observations\n\n- [fact] {title} was settled.\n" + (
        f"\n## Relations\n\n{relations}" if rows else ""
    )


def _create(call, title: str, source: str, *rows: str, **extra) -> dict:
    return call(
        "create_note",
        {
            "title": title,
            "note_type": "insight",
            "content": _body(title, *rows),
            "sources": [source],
            "response_detail": "full",
            **extra,
        },
    )


def _shown(result: dict, vault: Path) -> str:
    """A result as text that two vaults share when they disclose the same thing.

    The vault root and the page ids differ per vault by construction, and the writer lists
    its facts in the order of those ids, so list order is ignored too; nothing else may differ.
    """

    def unordered(value):
        if isinstance(value, dict):
            return {key: unordered(item) for key, item in value.items()}
        if isinstance(value, list):
            return sorted(
                (unordered(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True)
            )
        return value

    text = json.dumps(result, sort_keys=True, default=str).replace(str(vault), "<vault>")
    return json.dumps(unordered(json.loads(_RANDOM_ID.sub("<id>", text))), sort_keys=True)


def _target(entity: dict) -> str:
    return _page_path(entity).removesuffix(".md")


def _page(vault: Path, path: str) -> str:
    return (vault / path).read_text(encoding="utf-8")


def _anchored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A served vault whose first compiled note exists, so later notes face the full contract."""
    vault, call = _served(tmp_path, monkeypatch)
    anchor = _create(call, "Anchor", _source(call, "Anchor"))
    assert "error" not in anchor, anchor
    return vault, call, _page_path(anchor).removesuffix(".md")


def test_a_generic_edge_between_two_notes_is_refused_with_a_ranked_menu_and_three_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call, anchor = _anchored(tmp_path, monkeypatch)

    refused = _create(
        call, "Crane", _source(call, "Crane"), f"relates_to [[{anchor}]]", response_detail="full"
    )

    assert refused["error"]["code"] == CODE
    assert refused["error"]["message"].endswith("Nothing was saved.")
    error = refused["diagnostics"]["error"]
    assert 0 < len(error["options"]) <= 8
    assert all(set(option) == {"key", "label", "description", "parent"} for option in error["options"])
    assert "relates_to" not in [option["key"] for option in error["options"]]
    # The agent reads the top of the ranked menu as `candidates`, with the plain link last.
    assert refused["error"]["candidates"] == [
        *(option["key"] for option in error["options"][:4]),
        "links_to",
    ]
    routes = error["routes"]
    assert set(routes) == {"specific", "propose", "plain_link"}
    assert f"- {error['options'][0]['key']} [[{anchor}]]" in routes["specific"]["do"]
    assert f"- links_to [[{anchor}]]" in routes["plain_link"]["do"]
    assert routes["propose"]["propose"]["args"]["operation"] == "schema.propose_relation"
    assert routes["propose"]["save"]["args"]["operation"] == "schema.save_relations"
    assert not (vault / "Knowledge Base/Notes/Insights/crane.md").exists()


def test_the_plain_link_is_named_once_and_keeps_the_last_candidate_slot() -> None:
    ranked = [{"key": key} for key in ("supports", "links_to", "refines", "extends", "cites", "informs")]

    candidates = agent_refusal.ranked_with_link(ranked, "links_to")

    assert candidates == ["supports", "refines", "extends", "cites", "links_to"]


def test_a_generic_edge_inside_a_unit_without_an_anchor_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The writing guide teaches unit-level relations; an unanchored one is still a new edge."""
    vault, call, anchor = _anchored(tmp_path, monkeypatch)

    refused = call(
        "create_note",
        {
            "title": "Crane",
            "note_type": "insight",
            "content": (
                "## Observations\n\n- [fact] Crane was settled.\n\n"
                f"## Finding\n- relations: relates_to: [[{anchor}]]\n\nCrane lifts the load.\n"
            ),
            "sources": [_source(call, "Crane")],
        },
    )

    assert refused["error"]["code"] == CODE
    assert not (vault / "Knowledge Base/Notes/Insights/crane.md").exists()


def test_a_retry_with_the_menus_top_relation_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call, anchor = _anchored(tmp_path, monkeypatch)
    source = _source(call, "Crane")
    top = _create(call, "Crane", source, f"relates_to [[{anchor}]]")["error"]["candidates"][0]

    committed = _create(call, "Crane", source, f"{top} [[{anchor}]]")

    assert "error" not in committed, committed
    assert f"- {top} [[{anchor}]]" in _page(vault, _page_path(committed))


def test_a_plain_link_bullet_commits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, call, anchor = _anchored(tmp_path, monkeypatch)
    source = _source(call, "Crane")
    refused = _create(call, "Crane", source, f"relates_to [[{anchor}]]", response_detail="full")
    plain = refused["diagnostics"]["error"]["routes"]["plain_link"]
    bullet = re.search(r"`- (\S+ \[\[[^\]]+\]\])`", plain["do"]).group(1)

    committed = _create(call, "Crane", source, bullet)

    assert "error" not in committed, committed
    assert f"- {bullet}" in _page(vault, _page_path(committed))


def test_a_relation_the_vault_already_authors_between_these_note_types_ranks_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _vault, call, anchor = _anchored(tmp_path, monkeypatch)
    refused = _create(
        call, "Probe", _source(call, "Probe"), f"relates_to [[{anchor}]]", response_detail="full"
    )
    unused = [option["key"] for option in refused["diagnostics"]["error"]["options"]]
    # Pick a relation the registry order would not rank first, and author it between two insights.
    chosen = unused[-1]
    assert chosen != unused[0]
    for title in ("Crane", "Hoist"):
        committed = _create(call, title, _source(call, title), f"{chosen} [[{anchor}]]")
        assert "error" not in committed, committed

    ranked = _create(call, "Probe", _source(call, "Probe"), f"relates_to [[{anchor}]]")

    assert ranked["error"]["candidates"][0] == chosen


def test_an_edit_adding_a_generic_edge_is_refused_and_leaves_the_page_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call, anchor = _anchored(tmp_path, monkeypatch)
    page = _page_path(_create(call, "Crane", _source(call, "Crane"), f"supports [[{anchor}]]"))
    before = _page(vault, page)

    refused = call(
        "edit_note",
        {
            "path": page,
            "why": "link the anchor again",
            "operation": {
                "kind": "replace_string",
                "old_string": f"- supports [[{anchor}]]",
                "new_string": f"- supports [[{anchor}]]\n- relates_to [[{anchor}]]",
            },
        },
    )

    assert refused["error"]["code"] == CODE
    assert refused["error"]["candidates"]
    assert _page(vault, page) == before


def test_an_edit_that_leaves_a_legacy_generic_edge_untouched_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call, anchor = _anchored(tmp_path, monkeypatch)
    page = _page_path(_create(call, "Crane", _source(call, "Crane"), f"supports [[{anchor}]]"))
    # A generic edge written before the barrier existed.
    legacy = _page(vault, page).replace(f"- supports [[{anchor}]]", f"- relates_to [[{anchor}]]")
    (vault / page).write_text(legacy, encoding="utf-8")

    edited = call(
        "edit_note",
        {
            "path": page,
            "why": "fix the wording",
            "operation": {
                "kind": "replace_string",
                "old_string": "was settled.",
                "new_string": "was settled for good.",
            },
        },
    )

    assert "error" not in edited, edited
    assert f"- relates_to [[{anchor}]]" in _page(vault, page)


def test_an_edit_that_leaves_a_legacy_generic_unit_relation_untouched_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A unit-level generic relation from before the barrier must not lock the page."""
    vault, call, anchor = _anchored(tmp_path, monkeypatch)
    created = call(
        "create_note",
        {
            "title": "Crane",
            "note_type": "insight",
            "content": (
                "## Observations\n\n- [fact] Crane was settled.\n\n"
                f"## Finding\n- relations: supports: [[{anchor}]]\n\nCrane lifts the load.\n"
            ),
            "sources": [_source(call, "Crane")],
            "response_detail": "full",
        },
    )
    page = _page_path(created)
    legacy = _page(vault, page).replace(f"supports: [[{anchor}]]", f"relates_to: [[{anchor}]]")
    (vault / page).write_text(legacy, encoding="utf-8")

    edited = call(
        "edit_note",
        {
            "path": page,
            "why": "fix the wording",
            "operation": {
                "kind": "replace_string",
                "old_string": "Crane lifts the load.",
                "new_string": "Crane lifts the whole load.",
            },
        },
    )

    assert "error" not in edited, edited
    assert f"relates_to: [[{anchor}]]" in _page(vault, page)


def test_a_page_that_is_not_live_and_an_adoption_import_are_never_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call, anchor = _anchored(tmp_path, monkeypatch)
    source = _source(call, "Crane")
    row = f"relates_to [[{anchor}]]"

    draft = _create(call, "Draft crane", _source(call, "Draft crane"), row, status="draft")
    assert "error" not in draft, draft

    # `adoption_compile` has no tool door; the same page is checked under both operations.
    def validate(operation: str):
        return note.note(
            vault,
            content=_body("Adopted crane", row),
            note_type="insight",
            title="Adopted crane",
            sources=[source],
            validate_only=True,
            _preflight_operation=operation,
        )

    assert validate("adoption_compile").draft_hash
    with pytest.raises(note.NoteError) as refused:
        validate("create")
    assert refused.value.code == CODE


def test_a_successor_is_refused_for_a_generic_edge_it_adds_but_not_for_one_it_carries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call, anchor = _anchored(tmp_path, monkeypatch)
    other = _target(_entity(call, "person", "Ben Quay"))
    source = _source(call, "Crane")
    old = _page_path(_create(call, "Crane", source, f"supports [[{anchor}]]"))
    # A generic edge written before the barrier existed.
    (vault / old).write_text(
        _page(vault, old).replace(f"- supports [[{anchor}]]", f"- relates_to [[{anchor}]]"), encoding="utf-8"
    )

    def supersede(title: str, *rows: str) -> dict:
        return call(
            "supersede_note",
            {
                "old_path": old,
                "why": "revise the crane note",
                "title": title,
                "note_type": "insight",
                "content": _body(title, *rows),
                "sources": [source],
            },
        )

    added = supersede("Crane revised", f"relates_to [[{anchor}]]", f"relates_to [[{other}]]")
    assert added["error"]["code"] == CODE, added

    carried = supersede("Crane revised", f"relates_to [[{anchor}]]")
    assert "error" not in carried, carried


def test_a_withheld_target_never_shapes_the_menu_a_restricted_writer_gets(tmp_path: Path) -> None:
    """The writer sees a refusal either way; only a page it may see can rank the menu."""
    from dataclasses import replace

    def entity(path: str, title: str, kind: str, body: str = "") -> semantic_contract.SemanticPageState:
        source = (
            f"---\ntitle: {title}\ntype: entity\nentity_type: {kind}\nstatus: active\n---\n\n"
            f"## Observations\n\n- [fact] {title} exists.\n{body}"
        )
        return semantic_contract.build_page_state(
            tmp_path,
            path,
            source,
            relation_registry=relation_registry.core_registry(),
            language_registry=semantic_language_registry.core_registry(),
            review_fingerprint="review-v1",
        )

    def corpus(*states):
        return semantic_contract.SemanticCorpusContext.from_states(
            tmp_path,
            states,
            registry=relation_registry.core_registry(),
            identity_census=semantic_contract.StableIdentityCensus(
                tuple(semantic_contract.StableIdentityEntry(state.path, None) for state in states)
            ),
        )

    works = entity("Knowledge Base/Entities/Organizations/quay-works.md", "Quay Works", "organization")
    cy = entity(
        "Knowledge Base/Entities/People/cy-dock.md",
        "Cy Dock",
        "person",
        "\n## Relations\n\n- relates_to [[Knowledge Base/Entities/Organizations/quay-works]]\n",
    )

    def menu(*, others, may_see_target: bool) -> list[str]:
        after = replace(
            corpus(cy, *others),
            writer_visibility=lambda path: may_see_target or path != works.path,
        )
        refusal = relation_barrier.evaluate(
            cy, after, prior=None, prior_corpus=corpus(*others), operation="create"
        )
        assert refusal is not None
        return [option["key"] for option in refusal.options]

    seen = menu(others=(works,), may_see_target=True)
    withheld = menu(others=(works,), may_see_target=False)
    absent = menu(others=(), may_see_target=False)

    assert "member_of" in seen
    assert withheld == absent
    assert "member_of" not in withheld


def test_create_entity_refuses_a_generic_item_and_commits_a_specific_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    works = _target(_entity(call, "organization", "Quay Works"))

    refused = _entity(call, "person", "Cy Dock", relations=[{"kind": "relates_to", "target": works}])
    assert refused["error"]["code"] == CODE
    assert "member_of" in refused["error"]["candidates"]
    assert not (vault / "Knowledge Base/Entities/People/Cy Dock.md").exists()

    committed = _entity(call, "person", "Cy Dock", relations=[{"kind": "member_of", "target": works}])
    assert "error" not in committed, committed


def test_create_entity_no_longer_takes_a_reason_for_a_generic_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    works = _target(_entity(call, "organization", "Quay Works"))
    item = {"kind": "relates_to", "target": works, "reason": "They crossed paths once."}

    refused = _entity(call, "person", "Cy Dock", relations=[item])

    assert refused["error"]["code"] == "INVALID_LINK"
    assert not (vault / "Knowledge Base/Entities/People/Cy Dock.md").exists()


def test_bare_connections_render_as_plain_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, call = _served(tmp_path, monkeypatch)
    ben = _target(_entity(call, "person", "Ben Quay"))

    committed = _entity(call, "person", "Cy Dock", connections=[f"[[{ben}]]"])

    assert "error" not in committed, committed
    page = _page(vault, _page_path(committed))
    assert f"- links_to [[{ben}]]" in page
    assert "relates_to" not in page


def test_identical_bullets_to_one_page_count_as_one_refused_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _vault, call, anchor = _anchored(tmp_path, monkeypatch)
    ben = _target(_entity(call, "person", "Ben Quay"))
    twice = f"relates_to [[{anchor}]]"

    refused = _create(
        call, "Crane", _source(call, "Crane"), twice, twice, f"relates_to [[{ben}]]", response_detail="full"
    )

    assert refused["error"]["code"] == CODE
    assert refused["diagnostics"]["error"]["refused_edges"] == 2


def _offered(call, source_type: str, target: str) -> list[str]:
    """The ranked relation keys a refused generic edge from a new `source_type` entity to `target` offers."""
    refused = _entity(
        call, source_type, "Cy Dock", relations=[{"kind": "relates_to", "target": target}]
    )
    return [option["key"] for option in refused["diagnostics"]["error"]["options"]]


def test_relations_whose_endpoints_name_both_pages_come_before_those_that_name_neither(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _vault, call = _served(tmp_path, monkeypatch)
    works = _target(_entity(call, "organization", "Quay Works"))
    ben = _target(_entity(call, "person", "Ben Quay"))

    person_to_organization = _offered(call, "person", works)
    person_to_person = _offered(call, "person", ben)

    assert person_to_organization[:4] == ["member_of", "affiliated_with", "operates", "supplies"]
    assert person_to_person[:2] == ["kin_of", "supplies"]
    assert "kin_of" not in person_to_organization
    assert len(person_to_organization) <= 8


def test_a_promoted_relation_with_its_own_endpoints_is_offered_ahead_of_its_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _vault, call = _served(tmp_path, monkeypatch)
    works = _target(_entity(call, "organization", "Quay Works"))
    employed = {"subject": ["entity:person"], "object": ["entity:organization"]}
    saved = _save_extension(call, "vault.employed_by", _extension("affiliated_with", employed))
    assert saved["diagnostics"]["operational"]["state"] == "committed", saved

    offered = _offered(call, "person", works)

    assert offered[0] == "vault.employed_by"
    assert offered.index("vault.employed_by") < offered.index("affiliated_with")


def test_a_promoted_relation_that_only_inherits_its_parents_endpoints_ranks_behind_the_relations_that_name_both_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _vault, call = _served(tmp_path, monkeypatch)
    works = _target(_entity(call, "organization", "Quay Works"))
    saved = _save_extension(call, "vault.works_with", _extension("affiliated_with"))
    assert saved["diagnostics"]["operational"]["state"] == "committed", saved

    offered = _offered(call, "person", works)

    assert offered[:5] == ["member_of", "affiliated_with", "operates", "supplies", "vault.works_with"]
