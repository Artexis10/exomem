"""One word of a longer name, said as an ordinary word, resolves no one.

A person titled "Nora Hedge" (file `Nora.md`) was resolved by a turn about a
garden that said "hedge" as an ordinary word: `rare_term` on "hedge", plus a
recall hit on her page that the same word produced. The page carries its own
title, so the hit counted "hedge" as one of the two content words the
lexical lane requires, and "garden" on her page completed it. Her identity
unit was served. A word that is only part of an anchor's names, written as an
ordinary word by a turn whose casing carries a signal, now keeps the recall
hit only when the page still matches the turn without that word. Otherwise
the anchor is a `partial` lead. The file name is a registered name, so a turn
that says "nora" still reaches her.

Invented names and a synthetic vault throughout.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_working_set_carry import seed_ordinary_notes

from exomem import commands, lexstore, working_set_index, working_set_runtime

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


def test_one_word_of_a_longer_name_said_as_an_ordinary_word_resolves_no_one(
    garden_vault: Path,
) -> None:
    packet = commands.op_activate_context(
        garden_vault,
        turn=(
            "The garden got away from me this summer, the hedge is taller than the"
            " shed now and its roots are lifting the path by the gate"
        ),
    )

    assert "resolved" not in _statuses(packet, PERSON), packet["anchors"]
    assert IDENTITY not in json.dumps(packet), "the person's identity was served"


def test_a_capital_that_only_opens_a_sentence_does_not_make_the_word_a_name(
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
        turn="The hedge needs hedging before the garden party, it is taller than the shed now",
    )

    assert "resolved" not in _statuses(packet, PERSON), packet["anchors"]
    assert IDENTITY not in json.dumps(packet), "the person's identity was served"


def test_the_file_name_in_lower_case_still_reaches_the_person(garden_vault: Path) -> None:
    packet = commands.op_activate_context(garden_vault, turn="What did nora say about payments?")

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
