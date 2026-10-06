"""A restricted caller's packet equals the packet of a world without what it may not see.

Each test asks one turn twice as the same external caller: once in a vault
where a page is withheld from that caller, and once in the same vault with
that page (or the unit naming it) taken out. Any difference between the two
packets tells the caller something about what was withheld: a person listed
through a unit it was never served, a cap slot spent on a hidden row, a
character count, or a phrase contested by a page it cannot see.

Invented names throughout.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from test_governance_egress import _external, _reset_caches, write_rule, write_scope
from test_working_set_carry import seed_ordinary_notes

from exomem import commands, lexstore, working_set_index, working_set_runtime
from exomem.governance.principal import request_scope

PEOPLE = "Knowledge Base/Entities/People"
TALIA = f"{PEOPLE}/Talia Verenko.md"
OREN = f"{PEOPLE}/Oren Haldane.md"
PELL = f"{PEOPLE}/Pell Mordaunt.md"
QUILL = f"{PEOPLE}/Quill Aster.md"
NOTE = "Knowledge Base/Notes/Failures/replica-migration-stall.md"
SECRET = "Knowledge Base/Notes/Restricted/secret-cluster.md"
TURN = "My teammate who had the replica migration stall last week pinged again."


def _write(vault: Path, rel: str, text: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _person(name: str) -> str:
    return (
        "---\ntype: entity\nentity_type: person\nstatus: active\n---\n\n"
        f"# {name}\n\n## Summary\n\nWorks on storage.\n"
    )


def _note(title: str, units: str, context: str = "") -> str:
    return (
        "---\ntype: note\nstatus: active\nupdated: 2026-09-28\n---\n\n"
        f"# {title}\n\n## Summary\n\n{units}\n"
        + (f"\n## Context\n\n{context}\n" if context else "")
    )


def _withhold(vault: Path, path: str, ceiling: int) -> None:
    write_scope(vault, paths=path, name="Withheld")
    write_rule(vault, ceiling=ceiling)


def _ask(vault: Path, turn: str) -> dict[str, Any]:
    """The external caller's packet, without the fields that name a generation."""
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    _reset_caches()
    working_set_runtime.reset_caches_for_tests()
    with request_scope(_external()):
        packet = commands.op_activate_context(vault, turn=turn)
    packet = json.loads(json.dumps(packet, default=str))
    packet.pop("continuity", None)
    for key in ("freshness_key", "index_generation"):
        packet.get("generation", {}).pop(key, None)
    return packet


def test_a_unit_the_guard_removes_lists_no_one(vault: Path) -> None:
    """The guard drops a unit that links a withheld page. A person that unit
    named must not stay listed through the page it came from."""
    seed_ordinary_notes(vault)
    _write(vault, TALIA, _person("Talia Verenko"))
    _write(vault, SECRET, _note("Secret cluster", "- [fact] Hidden. ^s-1"))
    _withhold(vault, SECRET, ceiling=0)
    plain = "- [failure] The staging replica migration stalled twice last week. ^r-plain"
    naming = (
        "- [failure] Talia Verenko's migration stalled on the staging replica last week "
        "near [[Secret cluster]]. ^r-secret"
    )
    context = "Affected teammate: [[Talia Verenko]]."
    _write(vault, NOTE, _note("Replica migration stall", f"{plain}\n{naming}", context))
    restricted = _ask(vault, TURN)

    _write(vault, NOTE, _note("Replica migration stall", plain, context))
    clean = _ask(vault, TURN)

    assert restricted == clean


def test_a_withheld_person_is_listed_exactly_as_an_absent_one(vault: Path) -> None:
    """At a notice level the guard would mark a removed anchor, so a withheld
    row that reached the packet shows. It must never be listed at all."""
    seed_ordinary_notes(vault)
    _write(vault, TALIA, _person("Talia Verenko"))
    _write(vault, OREN, _person("Oren Haldane"))
    _withhold(vault, TALIA, ceiling=1)
    _write(
        vault,
        NOTE,
        _note(
            "Replica migration stall",
            "- [failure] Talia Verenko's migration stalled on the staging replica last week; "
            "Oren Haldane was on call. ^r-stall",
            "Affected: [[Talia Verenko]]. Reviewer: [[Oren Haldane]].",
        ),
    )
    restricted = _ask(vault, TURN)

    (vault / TALIA).unlink()
    absent = _ask(vault, TURN)

    assert restricted == absent
    assert [a["path"] for a in absent["anchors"] if "carried_link" in a["evidence"]] == [OREN]


def test_a_withheld_person_takes_no_listing_slot(vault: Path) -> None:
    """The two listing slots go to people the caller may see."""
    seed_ordinary_notes(vault)
    for rel, name in (
        (TALIA, "Talia Verenko"),
        (OREN, "Oren Haldane"),
        (PELL, "Pell Mordaunt"),
        (QUILL, "Quill Aster"),
    ):
        _write(vault, rel, _person(name))
    _withhold(vault, OREN, ceiling=0)
    _write(
        vault,
        NOTE,
        _note(
            "Replica migration stall",
            "- [failure] Talia Verenko's migration stalled on the staging replica last week; "
            "Oren Haldane, Pell Mordaunt and Quill Aster were on call. ^r-stall",
            "[[Oren Haldane]] [[Pell Mordaunt]] [[Quill Aster]] [[Talia Verenko]]",
        ),
    )
    restricted = _ask(vault, TURN)

    (vault / OREN).unlink()
    absent = _ask(vault, TURN)

    assert restricted == absent
    assert [a["path"] for a in absent["anchors"] if "carried_link" in a["evidence"]] == [
        TALIA,
        PELL,
    ]


def test_a_withheld_unit_costs_the_caller_no_characters(vault: Path) -> None:
    """`budget.used_chars` must not count a unit the guard removed: a resolved
    entity's linked decision note, one of whose units links a withheld page."""
    org = "Knowledge Base/Entities/Organisations/Larkspur Cooperative.md"
    decisions = "Knowledge Base/Notes/Decisions/larkspur-terms.md"
    _write(
        vault,
        org,
        "---\ntype: entity\nentity_type: organisation\nstatus: active\n---\n\n"
        "# Larkspur Cooperative\n\n## Summary\n\nA supplier cooperative on the coast.\n",
    )
    _write(vault, SECRET, _note("Secret cluster", "- [fact] Hidden. ^s-1"))
    _withhold(vault, SECRET, ceiling=0)
    kept = "- [decision] Larkspur renewals stay annual. ^l-annual"
    hidden = "- [decision] Larkspur pallets wait at the [[Secret cluster]] dock. ^l-dock"
    related = "Related: [[Larkspur Cooperative]]"
    _write(vault, decisions, _note("Larkspur terms", f"{kept}\n{hidden}", related))
    turn = "Larkspur Cooperative sent over a fresh tender proposal this morning."
    restricted = _ask(vault, turn)

    _write(vault, decisions, _note("Larkspur terms", kept, related))
    clean = _ask(vault, turn)

    assert restricted["budget"] == clean["budget"]
    assert restricted == clean


def test_a_withheld_namesake_does_not_make_a_phrase_a_question(vault: Path) -> None:
    """A page the caller may not see must not contest the phrase that names
    the page it may see."""
    seed_ordinary_notes(vault)
    plan = "Knowledge Base/Notes/Research/vantry-window-plan.md"
    retro = "Knowledge Base/Notes/Restricted/vantry-window-retro.md"
    _write(
        vault,
        plan,
        _note("Vantry window plan", "- [decision] The vantry window opens at six. ^v-1"),
    )
    _write(
        vault, retro, _note("Vantry window retro", "- [finding] The vantry window ran late. ^v-2")
    )
    _withhold(vault, retro, ceiling=0)
    turn = "Where did we land on the vantry window?"
    restricted = _ask(vault, turn)

    (vault / retro).unlink()
    absent = _ask(vault, turn)

    assert restricted == absent
    assert [a["path"] for a in absent["anchors"]] == [plan]
