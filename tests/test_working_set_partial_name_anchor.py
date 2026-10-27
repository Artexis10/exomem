"""A partial name and a lexical hit that needs it are one contact.

The page must match two remaining content units without the partial name's
stems. A word and an unspaced run each count once, under the lexical budget.
Casing does not decide intent. A partial surname stays visible so the agent
can choose it or name it in conversation focus. Prior naming alone cannot
distinguish that fragment from a different unindexed name.

Invented names and a synthetic vault throughout.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_connector_boundary import PRIVATE
from test_connector_boundary import configured_boundary as configured_boundary
from test_governance_egress import _external, write_rule, write_scope
from test_working_set_carry import CARRY_PAGE, seed_ordinary_notes
from test_working_set_carry import budget_free as budget_free
from test_working_set_carry import carry_vault as carry_vault
from test_working_set_topic_turns import workshop_vault as workshop_vault

from exomem import (
    commands,
    lexstore,
    working_set,
    working_set_heat,
    working_set_index,
    working_set_runtime,
)
from exomem.governance.principal import owner_principal, request_scope

# This module tests the `anchors` packet, the rollback mode; `maps` is the shipped default.
pytestmark = pytest.mark.usefixtures("anchors_packet")

KB = "Knowledge Base"
PERSON = f"{KB}/Entities/People/Nora.md"
NAMESAKE = f"{KB}/Entities/People/Nora Quill.md"
PROJECT = f"{KB}/Entities/Projects/Kelvane Rollout.md"
RENOVATION = f"{KB}/Entities/Projects/Tessaly Renovation.md"
GARDENER = f"{KB}/Entities/People/Oskar.md"
GARDENER_IDENTITY = "Keeps the 剪定計画書 for the estate"
PLACE = f"{KB}/Entities/Places/Allotment.md"
IDENTITY = "Former engineering colleague from the payments team"
NAMESAKE_IDENTITY = "Runs the payments audit for the regional office"


def _write(vault: Path, rel: str, text: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _entity(vault: Path, rel: str, title: str, fact: str, *, person: bool = False) -> None:
    kind = "entity_type: person\n" if person else ""
    _write(
        vault,
        rel,
        f"---\ntype: entity\n{kind}status: active\nupdated: 2026-09-01\n---\n\n"
        f"# {title}\n\n## Summary\n\n- [fact] {fact} ^e-1\n",
    )


def _reindex(vault: Path) -> Path:
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    return vault


@pytest.fixture
def garden_vault(vault: Path) -> Path:
    seed_ordinary_notes(vault)
    _entity(
        vault,
        PERSON,
        "Nora Hedge",
        f"{IDENTITY}; her garden backs onto the office car park.",
        person=True,
    )
    _entity(vault, PLACE, "Allotment", "My garden plot by the railway; the shed leaks.")
    garden_notes = ("Hedge trimming", "Beech hedge planting", "Winter hedge care")
    for index, title in enumerate(garden_notes):
        _write(
            vault,
            f"{KB}/Notes/Research/{title.lower().replace(' ', '-')}.md",
            f"---\ntype: research-note\nstatus: active\nupdated: 2026-09-0{index + 2}\n---\n\n"
            f"# {title}\n\n## Summary\n\n- [note] Trim the hedge after the nesting season;"
            f" keep the base wider than the top. ^g-{index}\n",
        )
    return _reindex(vault)


def _statuses(packet: dict, path: str) -> list[str]:
    return [entry["status"] for entry in packet.get("anchors") or () if entry["path"] == path]


@pytest.mark.parametrize("surname", ["hedge", "Hedge", "HEDGE"])
def test_retrieval_carry_cannot_serve_a_dependent_surname(
    workshop_vault: Path, surname: str,
) -> None:
    """Rare surname and topic words can name a carry phrase without independent identity contact."""
    _entity(workshop_vault, PERSON, "Nora Hedge", IDENTITY, person=True)
    _reindex(workshop_vault)

    packet = commands.op_activate_context(
        workshop_vault, turn=f"What did {surname} say about payments?"
    )

    assert _statuses(packet, PERSON) == ["partial"], packet["anchors"]
    assert packet["abstention"] == {"reason": "unresolved"}
    assert packet["units"] == packet["pointers"] == packet["current_state"] == []
    assert IDENTITY not in json.dumps(packet)


@pytest.mark.parametrize("initial_hit", [False, True])
def test_carry_only_surname_checks_independence_once_per_request(
    workshop_vault: Path, monkeypatch: pytest.MonkeyPatch, initial_hit: bool,
) -> None:
    """An initial hit cap must not bypass dependence; repeated entrances must not repeat its query."""
    _entity(workshop_vault, PERSON, "Nora Hedge", IDENTITY, person=True)
    _reindex(workshop_vault)
    lexical_evidence = working_set_runtime.lexical_evidence
    checks: list[tuple[frozenset[str], frozenset[str]]] = []
    independent = working_set_runtime.lexical_hits_without

    def first_pass(*args, **kwargs):
        hits, state = lexical_evidence(*args, **kwargs)
        return [hit for hit in hits if initial_hit or hit.path != PERSON], state

    def checked(root, turn, pages, words, **kwargs):
        checks.append((pages, words))
        return independent(root, turn, pages, words, **kwargs)

    monkeypatch.setattr(working_set_runtime, "lexical_evidence", first_pass)
    monkeypatch.setattr(working_set_runtime, "lexical_hits_without", checked)

    packet = commands.op_activate_context(workshop_vault, turn="What did hedge say about payments?")

    assert _statuses(packet, PERSON) == ["partial"]
    assert IDENTITY not in json.dumps(packet)
    assert checks == [(frozenset({PERSON}), frozenset({"hedge"}))]


def test_a_withheld_whole_name_leaves_the_dependent_surname_packet_identical(
    workshop_vault: Path, configured_boundary, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hidden whole-name alias and a primed broader cache must look absent to the limited caller."""
    _, authenticate = configured_boundary
    _entity(workshop_vault, PERSON, "Nora Hedge", IDENTITY, person=True)
    _entity(workshop_vault, PRIVATE, "Hedge", "Private canary: payments ledger", person=True)
    private = workshop_vault / PRIVATE
    private.write_text(
        private.read_text().replace("type: entity\n", "type: entity\nproject: private-project\n"),
        encoding="utf-8",
    )
    with request_scope(owner_principal(surface="library")):
        _reindex(workshop_vault)
    limited, full = authenticate("limited"), authenticate("full")
    turn = "What did hedge say about payments?"
    with request_scope(full):
        commands.op_activate_context(workshop_vault, turn=turn, anchor=PRIVATE, client="full")
    # Compare the same caller-visible activity: activation itself records later activity.
    with request_scope(limited):
        heat = working_set_heat.profile(workshop_vault)
        released = working_set_runtime._heat_release_filter(workshop_vault, heat)
        if released is not None:
            heat = working_set_heat.released_view(heat, released)
    monkeypatch.setattr(working_set_heat, "profile", lambda _root: heat)
    with request_scope(limited):
        present = commands.op_activate_context(workshop_vault, turn=turn, client="full")
    with request_scope(owner_principal(surface="library")):
        private.unlink()
        _reindex(workshop_vault)
    with request_scope(limited):
        absent = commands.op_activate_context(workshop_vault, turn=turn, client="full")

    present.pop("continuity", None)
    absent.pop("continuity", None)
    assert present == absent
    assert _statuses(present, PERSON) == ["partial"]
    assert IDENTITY not in json.dumps(present) and "Private canary" not in json.dumps(present)


def test_shared_page_owners_check_their_partial_names_separately(
    workshop_vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unioning two owners' exclusions would withhold a page that independently matches each owner."""
    _entity(workshop_vault, PERSON, "Nora Hedge", "Thorn payments", person=True)
    _reindex(workshop_vault)
    index = working_set_index.WorkingSetIndex(workshop_vault)
    rows = index.anchors()
    person = next(row for row in rows if row.path == PERSON)
    # Supply a second authored owner at the catalogue boundary, sharing the real page.
    second = replace(person, anchor_id="shared-owner", title="Oskar Thorn", aliases=(), terms=("thorn",))
    counts = index.term_anchor_counts()
    monkeypatch.setattr(index, "anchors", lambda: (*rows, second))
    monkeypatch.setattr(index, "term_anchor_counts", lambda: {**counts, "thorn": 1})
    independent = working_set_runtime.lexical_hits_without
    checks: list[frozenset[str]] = []

    def checked(root, turn, pages, words, **kwargs):
        checks.append(words)
        return independent(root, turn, pages, words, **kwargs)

    monkeypatch.setattr(working_set_runtime, "lexical_hits_without", checked)

    packet = working_set.compile_packet(
        workshop_vault, turn="What did hedge say about thorn payments?", index=index,
    )

    assert set(checks) == {frozenset({"hedge"}), frozenset({"thorn"})}
    assert len(checks) == 2
    assert any("Thorn payments" in item["text"] for item in packet["units"])


def test_a_resolved_context_cannot_carry_a_dependent_surname_beside_it(
    workshop_vault: Path,
) -> None:
    """The additive carry must preserve a resolved context without appending another person's identity."""
    _entity(workshop_vault, PERSON, "Nora Hedge", IDENTITY, person=True)
    _entity(workshop_vault, PROJECT, "Kelvane Rollout", "The booking move uses two waves.")
    _reindex(workshop_vault)

    packet = commands.op_activate_context(
        workshop_vault, turn="Check Kelvane Rollout. What did hedge say about payments?"
    )

    assert _statuses(packet, PROJECT) == ["resolved"]
    assert _statuses(packet, PERSON) == ["partial"]
    assert any("booking move" in item["text"] for item in packet["units"])
    assert IDENTITY not in json.dumps(packet)


def test_explicit_and_full_name_choices_still_serve_a_dependent_surname(
    workshop_vault: Path,
) -> None:
    """The dependence boundary applies to retrieval carry, while authored naming still decides."""
    _entity(workshop_vault, PERSON, "Nora Hedge", IDENTITY, person=True)
    _reindex(workshop_vault)

    chosen = commands.op_activate_context(
        workshop_vault, turn="What did hedge say about payments?", anchor=PERSON,
    )
    named = commands.op_activate_context(workshop_vault, turn="What did Nora Hedge say about payments?")

    for packet in (chosen, named):
        assert _statuses(packet, PERSON) == ["resolved"]
        assert IDENTITY in json.dumps(packet)


def test_an_unavailable_dependence_check_withholds_only_the_affected_carry(
    workshop_vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed surname check must not suppress an unrelated independently named ordinary page."""
    from test_working_set_topic_turns import TRUING

    _entity(workshop_vault, PERSON, "Nora Hedge", IDENTITY, person=True)
    _reindex(workshop_vault)
    query = working_set_runtime._lexical_query

    def unavailable(*args, **kwargs):
        if kwargs.get("excluded_corroboration_stems"):
            raise OSError("independence catalogue unavailable")
        return query(*args, **kwargs)

    monkeypatch.setattr(working_set_runtime, "_lexical_query", unavailable)
    packet = commands.op_activate_context(
        workshop_vault,
        turn="What did hedge say about payments? Check the spoke tension on the truing stand.",
    )

    assert any(anchor["path"] == TRUING for anchor in packet["anchors"])
    assert any("spoke tension" in item["text"] for item in packet["units"])
    assert IDENTITY not in json.dumps(packet)


def test_a_semantic_band_cannot_yield_to_a_dependent_surname_carry(
    workshop_vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rejecting dependent rival material must preserve the band-resolved context and reuse its check."""
    _entity(workshop_vault, PERSON, "Nora Hedge", IDENTITY, person=True)
    _entity(workshop_vault, PROJECT, "Kelvane Rollout", "The rollout calendar uses two waves.")
    _reindex(workshop_vault)
    monkeypatch.setattr(working_set, "signature_evidence", lambda *_: ({PROJECT: True}, "ready", None))
    independent = working_set_runtime.lexical_hits_without
    surname_checks: list[frozenset[str]] = []

    def checked(root, turn, pages, words, **kwargs):
        if words == frozenset({"hedge"}):
            surname_checks.append(pages)
        return independent(root, turn, pages, words, **kwargs)

    monkeypatch.setattr(working_set_runtime, "lexical_hits_without", checked)
    packet = commands.op_activate_context(
        workshop_vault, turn="What did hedge say about payments for the rollout?"
    )

    assert _statuses(packet, PROJECT) == ["resolved"]
    assert _statuses(packet, PERSON) == ["partial"]
    assert any("rollout calendar" in item["text"] for item in packet["units"])
    assert IDENTITY not in json.dumps(packet)
    assert surname_checks == [frozenset({PERSON})]


@pytest.mark.parametrize("word", ["hedge", "Hedge", "HEDGE"])
def test_a_partial_name_and_a_hit_that_needs_it_resolve_no_one(
    garden_vault: Path, word: str,
) -> None:
    packet = commands.op_activate_context(
        garden_vault,
        turn=(
            f"The garden got away from me this summer, the {word} is taller than the"
            " shed now and its roots are lifting the path by the gate"
        ),
    )

    assert "resolved" not in _statuses(packet, PERSON), packet["anchors"]
    assert IDENTITY not in json.dumps(packet), "the person's identity was served"


def test_a_sentence_opening_capital_does_not_supply_independent_contact(
    garden_vault: Path,
) -> None:
    packet = commands.op_activate_context(
        garden_vault,
        turn=(
            "Hedge trimming took all weekend. The garden looks better, but the hedge"
            " by the gate is still taller than the shed"
        ),
    )

    assert "resolved" not in _statuses(packet, PERSON), packet["anchors"]
    assert IDENTITY not in json.dumps(packet), "the person's identity was served"


def test_an_inflected_form_of_the_word_does_not_count_as_another_match(
    garden_vault: Path,
) -> None:
    """The lexical lane matches by stem: "hedging" is "hedge" again."""
    packet = commands.op_activate_context(
        garden_vault,
        turn="The hedge needs hedging; the hedged hedge is taller than the garden shed now",
    )

    assert "resolved" not in _statuses(packet, PERSON), packet["anchors"]
    assert IDENTITY not in json.dumps(packet), "the person's identity was served"


def test_the_file_name_in_lower_case_still_reaches_the_person(garden_vault: Path) -> None:
    packet = commands.op_activate_context(garden_vault, turn="What did nora say about payments?")

    assert _statuses(packet, PERSON) == ["resolved"], packet["anchors"]


@pytest.mark.parametrize("turn", [
    "what did hedge say about payments?",
    "What did Hedge say about payments?",
    "WHAT DID HEDGE SAY ABOUT PAYMENTS?",
])
def test_a_surname_with_one_topical_match_stays_a_visible_lead(
    garden_vault: Path, turn: str,
) -> None:
    packet = commands.op_activate_context(garden_vault, turn=turn)

    assert _statuses(packet, PERSON) == ["partial"], packet["anchors"]
    person = next(entry for entry in packet["anchors"] if entry["path"] == PERSON)
    assert "rare_term" in person["evidence"] and "retrieval" not in person["evidence"]
    assert IDENTITY not in json.dumps(packet), "the person's identity was served"


def test_the_agent_can_resolve_a_visible_surname_lead_without_widening_the_audience(
    garden_vault: Path,
) -> None:
    _entity(garden_vault, NAMESAKE, "Nora Quill", NAMESAKE_IDENTITY, person=True)
    write_scope(garden_vault, paths="Entities/People/Nora Quill.md")
    write_rule(garden_vault, ceiling=0)
    _reindex(garden_vault)

    with request_scope(_external()):
        lead = commands.op_activate_context(
            garden_vault, turn="What did hedge say about payments?"
        )
        chosen = commands.op_activate_context(
            garden_vault, turn="What did hedge say about payments?", anchor=PERSON
        )
        focused = commands.op_activate_context(
            garden_vault,
            turn="What did hedge say about payments?",
            conversation={"focus": "Nora Hedge"},
        )

    assert _statuses(lead, PERSON) == ["partial"], lead["anchors"]
    assert IDENTITY not in json.dumps(lead)
    for packet in (chosen, focused):
        assert _statuses(packet, PERSON) == ["resolved"], packet["anchors"]
        assert IDENTITY in json.dumps(packet), "the chosen person's identity was not served"
    for packet in (lead, chosen, focused):
        assert NAMESAKE not in json.dumps(packet) and NAMESAKE_IDENTITY not in json.dumps(packet)


def test_prior_naming_keeps_a_partial_surname_selectable_without_serving_its_identity(
    garden_vault: Path,
) -> None:
    named = commands.op_activate_context(garden_vault, turn="What did Nora Hedge say?")
    continued = commands.op_activate_context(
        garden_vault,
        turn="What did hedge say about payments?",
        continuity=named["continuity"],
    )
    remembered = commands.op_activate_context(
        garden_vault,
        turn="What did hedge say about payments?",
        conversation={"recent": [{"role": "user", "text": "Nora Hedge joined the call"}]},
    )

    for packet in (continued, remembered):
        assert _statuses(packet, PERSON) == ["partial"], packet["anchors"]
        assert IDENTITY not in json.dumps(packet)

    chosen = commands.op_activate_context(
        garden_vault, turn="What did hedge say about payments?", anchor=PERSON,
        continuity=named["continuity"],
    )
    assert _statuses(chosen, PERSON) == ["resolved"], chosen["anchors"]
    assert IDENTITY in json.dumps(chosen)


@pytest.mark.parametrize("turn", [
    "A separate parcel is from Vera Hedge. What do we know about its origin?",
    "What did Vera Hedge say?",
])
def test_old_naming_does_not_resolve_a_fragment_of_an_unindexed_name(
    garden_vault: Path, turn: str,
) -> None:
    named = commands.op_activate_context(garden_vault, turn="What did Nora Hedge say?")
    packet = commands.op_activate_context(
        garden_vault, turn=turn, continuity=named["continuity"],
        conversation={"recent": [{"role": "user", "text": "Nora Hedge joined the call"}]},
    )

    assert _statuses(packet, PERSON) == ["partial"], packet["anchors"]
    assert IDENTITY not in json.dumps(packet)


def test_full_name_focus_beats_a_historical_subphrase(vault: Path) -> None:
    seed_ordinary_notes(vault)
    short = "Knowledge Base/Entities/Organizations/Short.md"
    long = "Knowledge Base/Entities/Organizations/Long.md"
    _entity(vault, short, "The Hedge", "The original supplier")
    _entity(vault, long, "The Hedge Collective", "The replacement supplier")
    _reindex(vault)

    packet = commands.op_activate_context(vault, turn="Tell me about hedge.", conversation={
        "focus": "The Hedge Collective",
        "recent": [{"role": "user", "text": "The Hedge"}],
    })

    assert _statuses(packet, short) == ["partial"], packet["anchors"]
    assert _statuses(packet, long) == ["resolved"], packet["anchors"]


def test_prior_entity_fragment_does_not_hide_its_band_dependency(
    carry_vault: Path, budget_free, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entity = "Knowledge Base/Entities/Organizations/Copper Hedge.md"
    _entity(carry_vault, entity, "Copper Hedge", "The original supplier")
    _reindex(carry_vault)
    monkeypatch.setattr(working_set, "signature_evidence",
        lambda _index, _turn: ({entity: True}, "ready", None))

    packet = commands.op_activate_context(carry_vault,
        turn="What did we decide about the quillon vantry window for the hedge?",
        conversation={"recent": [{"role": "user", "text": "Copper Hedge"}]})

    assert packet["generation"].get("carried_by") == "retrieval", packet
    assert _statuses(packet, CARRY_PAGE) == ["resolved"], packet["anchors"]
    assert _statuses(packet, entity) != ["resolved"], packet["anchors"]
    assert not any(u["provenance"]["path"] == entity for u in packet["units"])


def test_a_registered_alias_still_resolves_a_surname(garden_vault: Path) -> None:
    path = garden_vault / PERSON
    path.write_text(
        path.read_text(encoding="utf-8").replace("status: active\n", "status: active\naliases: [Hedge]\n"),
        encoding="utf-8",
    )
    _reindex(garden_vault)

    packet = commands.op_activate_context(garden_vault, turn="What did hedge say about payments?")

    assert _statuses(packet, PERSON) == ["resolved"], packet["anchors"]


def test_a_first_name_two_people_share_still_asks_which_one(garden_vault: Path) -> None:
    """Only one of the two is stored under the bare first name."""
    _entity(garden_vault, NAMESAKE, "Nora Quill", f"{NAMESAKE_IDENTITY}.", person=True)
    _reindex(garden_vault)

    packet = commands.op_activate_context(garden_vault, turn="What did nora say about payments?")

    assert packet.get("abstention") == {"reason": "ambiguous"}, packet["anchors"]
    served = json.dumps(packet)
    assert IDENTITY not in served and NAMESAKE_IDENTITY not in served


def test_a_page_that_matches_the_turn_without_the_shared_word_still_resolves(
    garden_vault: Path,
) -> None:
    _entity(
        garden_vault,
        PROJECT,
        "Kelvane Rollout",
        "The rollout moves the booking system to the new host in two waves.",
    )
    _reindex(garden_vault)

    packet = commands.op_activate_context(
        garden_vault, turn="When does the rollout move the booking system to the new host?"
    )

    assert _statuses(packet, PROJECT) == ["resolved"], packet["anchors"]


@pytest.mark.parametrize("independent", ["障害対応 and 工程管理", "障害対応 for the estate"])
def test_independent_unspaced_units_keep_retrieval_contact(
    garden_vault: Path, independent: str,
) -> None:
    _entity(garden_vault, PROJECT, "Kelvane Rollout", f"The rollout tracks {independent}.")
    _reindex(garden_vault)

    packet = commands.op_activate_context(
        garden_vault, turn=f"When does the rollout handle {independent}?"
    )

    assert _statuses(packet, PROJECT) == ["resolved"], packet["anchors"]
    project = next(entry for entry in packet["anchors"] if entry["path"] == PROJECT)
    assert {"rare_term", "retrieval"} <= set(project["evidence"])


def test_hyphenated_content_supplies_two_units_without_the_partial_name(
    garden_vault: Path,
) -> None:
    _entity(
        garden_vault, PROJECT, "Kelvane Budget Hub", "The budget covers the kitchen renovation."
    )
    _reindex(garden_vault)

    packet = commands.op_activate_context(
        garden_vault, turn="Where does the kitchen-renovation budget stand?"
    )

    assert _statuses(packet, PROJECT) == ["resolved"], packet["anchors"]


def test_one_anchors_word_still_counts_for_another_anchors_page(garden_vault: Path) -> None:
    """Each check leaves out only its own anchor's word. "hedge" is one word
    of the person's name, and it is also on the project's page."""
    _entity(
        garden_vault,
        RENOVATION,
        "Tessaly Renovation",
        "The renovation is waiting on the skip; the hedge must be cut back first.",
    )
    _reindex(garden_vault)

    packet = commands.op_activate_context(
        garden_vault,
        turn="The renovation is stalled because the hedge blocks the skip by the garden",
    )

    assert _statuses(packet, RENOVATION) == ["resolved"], packet["anchors"]
    assert "resolved" not in _statuses(packet, PERSON), packet["anchors"]
    assert IDENTITY not in json.dumps(packet), "the person's identity was served"


def test_one_shared_unspaced_phrase_is_not_two_more_matches(garden_vault: Path) -> None:
    """An unspaced phrase yields several bigram stems; it is one match."""
    _entity(garden_vault, GARDENER, "Oskar Thorn", f"{GARDENER_IDENTITY}.", person=True)
    _reindex(garden_vault)

    packet = commands.op_activate_context(
        garden_vault, turn="The thorn bush needs the 剪定計画書 before spring"
    )

    assert "resolved" not in _statuses(packet, GARDENER), packet["anchors"]
    assert GARDENER_IDENTITY not in json.dumps(packet, ensure_ascii=False)


def test_budget_dropped_words_do_not_restore_a_dependent_hit(garden_vault: Path) -> None:
    """A long topic list must not restore a vote from outside the selected units."""
    _entity(garden_vault, GARDENER, "Oskar Thorn", "Keeps the archive with the heritage register.")
    padding = " ".join(f"qword{index:03d}" for index in range(20))
    _write(
        garden_vault,
        f"{KB}/Notes/Research/catalogue.md",
        "---\ntype: research-note\nstatus: active\n---\n\n# Catalogue\n\n"
        f"- [fact] The catalogue holds {padding}. ^c-1\n",
    )
    _reindex(garden_vault)

    turn = f"The thorn archive needs {padding} before heritage"
    selected: dict = {}
    hits, state = working_set_runtime.lexical_evidence(
        garden_vault,
        turn,
        working_set_index.WorkingSetIndex(garden_vault).anchors(),
        limit=8,
        selection=selected,
    )
    independent = working_set_runtime.lexical_hits_without(
        garden_vault, turn, frozenset({GARDENER}), frozenset({"thorn"})
    )

    assert state == "available" and GARDENER in {hit.path for hit in hits}
    assert independent == frozenset()
    assert selected["terms_kept"] == working_set_runtime.ACTIVATION_LEXICAL_MAX_TERMS
    assert selected["terms_dropped"] > 0


def test_an_excluded_stem_removes_its_whole_unspaced_unit(garden_vault: Path) -> None:
    """Other bigrams from the excluded run must not reconstitute a second contact."""
    _entity(garden_vault, GARDENER, "Oskar Thorn", f"{GARDENER_IDENTITY}.", person=True)
    _reindex(garden_vault)
    arguments = {"allowed_paths": {GARDENER}, "allow_delta": False, "min_matched_terms": 2}

    before = lexstore.search_bm25_result(garden_vault, "剪定計画書 estate", 1, **arguments)
    without = lexstore.search_bm25_result(
        garden_vault,
        "剪定計画書 estate",
        1,
        excluded_corroboration_stems=frozenset({"計画"}),
        **arguments,
    )

    assert before.readiness.complete and without.readiness.complete
    assert [path for path, _score in before.value] == [GARDENER]
    assert without.value == []
