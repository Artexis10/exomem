"""A bare shared name asks instead of carrying a page (close-memory-loop, activation
quality), compiled end to end over a real vault.

The resolver tests in `test_working_set_resolve_senses.py` decide the status
from facts. This module proves the packet-level clause: when the turn's bare
name is a question between two people, the retrieval carry is not asked, even
though the same turn names a carryable note by a distinctive phrase. And the
reviewer's control: an ordinary noun two businesses are named after asks
nothing, so the carry still serves that note. Invented names only.
"""

from __future__ import annotations

from pathlib import Path

from test_working_set_carry import _write, seed_ordinary_notes
from test_working_set_index import _seed_planning, _seed_structure

from exomem import lexstore, working_set, working_set_index, working_set_runtime

KB = "Knowledge Base"
NOTE = f"{KB}/Notes/Research/quarry-valve-inspection.md"


def _entity(vault: Path, folder: str, slug: str, title: str, entity_type: str) -> str:
    rel = f"{KB}/Entities/{folder}/{slug}.md"
    _write(
        vault / rel,
        f"---\ntype: entity\ntitle: {title}\nentity_type: {entity_type}\nstatus: active\n"
        f"updated: 2026-09-01\n---\n\n# {title}\n\n## Summary\n\nKnown to the team.\n",
    )
    return rel


def _seed(vault: Path, *, entities: tuple[tuple[str, str, str, str], ...]) -> tuple[str, ...]:
    _seed_structure(vault)
    _seed_planning(vault)
    paths = tuple(_entity(vault, *entity) for entity in entities)
    _write(
        vault / NOTE,
        "---\ntype: research-note\nstatus: active\nupdated: 2026-09-12\n---\n\n"
        "# Quarry valve inspection\n\n## Summary\n\n"
        "- [decision] The quarry valve inspection moved to the second week of the month.\n",
    )
    seed_ordinary_notes(vault)
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    return paths


PEOPLE = (
    ("People", "priya-nandakumar", "Priya Nandakumar", "person"),
    ("People", "priya-oduya", "Priya Oduya", "person"),
)
BUSINESSES = (
    ("Organizations", "harbour-bakery", "Harbour Bakery", "organization"),
    ("Organizations", "harbour-clinic", "Harbour Clinic", "organization"),
)


def test_a_bare_first_name_asks_and_the_carry_is_not_asked(vault: Path) -> None:
    people = _seed(vault, entities=PEOPLE)
    turn = "Priya mentioned the quarry valve inspection again."
    hits, _state = working_set_runtime.carry_candidates(vault, turn)
    assert [path for path, _score in hits] == [NOTE], "the note is carryable by this turn"

    packet = working_set.compile_packet(vault, turn=turn, max_chars=4000)

    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "ambiguous"}
    assert {anchor["path"] for anchor in packet["anchors"]} == set(people)
    assert len(packet["ambiguity"]) == 2
    assert packet["generation"].get("carried_by") is None
    assert packet["units"] == [] and packet["pointers"] == []
    assert NOTE not in {anchor["path"] for anchor in packet["anchors"]}


def test_an_ordinary_noun_two_businesses_share_still_lets_the_note_carry(vault: Path) -> None:
    """Reviewer's control: "harbour" is a place here, not a question."""

    _seed(vault, entities=BUSINESSES)
    turn = "the harbour crew mentioned the quarry valve inspection again"

    packet = working_set.compile_packet(vault, turn=turn, max_chars=4000)

    assert packet["ambiguity"] == []
    assert packet["generation"].get("carried_by") == "retrieval"
    assert [anchor["path"] for anchor in packet["anchors"]] == [NOTE]


def test_the_index_records_each_entitys_own_type(vault: Path) -> None:
    people = _seed(vault, entities=(*PEOPLE, BUSINESSES[0]))
    index = working_set_index.WorkingSetIndex(vault)
    try:
        rows = index.anchors()
    finally:
        index.close()
    by_path = {row.path: row.entity_type for row in rows}

    assert [by_path[path] for path in people] == ["person", "person", "organization"]
    assert {row.entity_type for row in rows if row.kind != "entity"} == {""}
