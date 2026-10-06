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

from test_governance_egress import SCOPE_ID, _external, _gov_dir, _reset_caches, write_rule
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


def _withhold(vault: Path, *paths: str, ceiling: int) -> None:
    scope = _gov_dir(vault) / "scopes" / "patterns.yaml"
    scope.parent.mkdir(parents=True, exist_ok=True)
    listed = ", ".join(f'"{path}"' for path in paths)
    scope.write_text(
        f"governance_version: 1\nid: {SCOPE_ID}\nname: Withheld\npaths: [{listed}]\n",
        encoding="utf-8",
    )
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


def test_a_person_a_surviving_unit_names_stays_listed(vault: Path) -> None:
    """The guard drops a unit that links a withheld page, but another unit of
    the same note names Talia. She stays listed, as in a note without the
    dropped unit: losing her would show that the note lost a unit."""
    seed_ordinary_notes(vault)
    _write(vault, TALIA, _person("Talia Verenko"))
    _write(vault, SECRET, _note("Secret cluster", "- [fact] Hidden. ^s-1"))
    _withhold(vault, SECRET, ceiling=0)
    survivor = (
        "- [failure] The staging replica migration stalled twice last week; "
        "Talia Verenko reran it. ^r-talia"
    )
    hidden = (
        "- [failure] The migration stalled on the staging replica last week "
        "near [[Secret cluster]]. ^r-secret"
    )
    context = "Affected teammate: [[Talia Verenko]]."
    _write(vault, NOTE, _note("Replica migration stall", f"{survivor}\n{hidden}", context))
    restricted = _ask(vault, TURN)

    _write(vault, NOTE, _note("Replica migration stall", survivor, context))
    clean = _ask(vault, TURN)

    assert restricted == clean
    assert [a["path"] for a in clean["anchors"] if "carried_link" in a["evidence"]] == [TALIA]


def test_a_slot_the_removed_unit_held_goes_to_the_next_person_named(vault: Path) -> None:
    """The removed unit is served first and names Oren and Pell, filling both
    slots. Without it, a slot goes to Talia, whom the surviving unit names, as
    in a note without the removed unit."""
    seed_ordinary_notes(vault)
    for rel, name in ((TALIA, "Talia Verenko"), (OREN, "Oren Haldane"), (PELL, "Pell Mordaunt")):
        _write(vault, rel, _person(name))
    _write(vault, SECRET, _note("Secret cluster", "- [fact] Hidden. ^s-1"))
    _withhold(vault, SECRET, ceiling=0)
    hidden = (
        "- [decision] Oren Haldane and Pell Mordaunt moved the replica migration "
        "off [[Secret cluster]]. ^r-secret"
    )
    survivor = "- [decision] Talia Verenko reran the staging migration the next week. ^r-talia"
    context = "[[Oren Haldane]] [[Pell Mordaunt]] [[Talia Verenko]]"
    _write(vault, NOTE, _note("Replica migration stall", f"{hidden}\n{survivor}", context))
    restricted = _ask(vault, TURN)

    _write(vault, NOTE, _note("Replica migration stall", survivor, context))
    clean = _ask(vault, TURN)

    assert restricted == clean
    assert [a["path"] for a in clean["anchors"] if "carried_link" in a["evidence"]] == [TALIA]


def test_a_person_the_guard_lists_again_is_listed_only_if_visible(vault: Path) -> None:
    """The guard removes Talia's unit and lists again from the unit left, which
    names Oren and Pell. Pell is withheld and was never in the packet the guard
    decided, so the guard must decide him before listing him."""
    seed_ordinary_notes(vault)
    for rel, name in ((TALIA, "Talia Verenko"), (OREN, "Oren Haldane"), (PELL, "Pell Mordaunt")):
        _write(vault, rel, _person(name))
    _write(vault, SECRET, _note("Secret cluster", "- [fact] Hidden. ^s-1"))
    _withhold(vault, SECRET, PELL, ceiling=0)
    naming = (
        "- [failure] Talia Verenko's migration stalled on the staging replica last week "
        "near [[Secret cluster]]. ^r-a"
    )
    survivor = (
        "- [failure] The staging replica migration stalled twice last week; "
        "Oren Haldane and Pell Mordaunt reran it. ^r-b"
    )
    context = "[[Talia Verenko]] [[Oren Haldane]] [[Pell Mordaunt]]"
    _write(vault, NOTE, _note("Replica migration stall", f"{naming}\n{survivor}", context))
    restricted = _ask(vault, TURN)

    _write(vault, NOTE, _note("Replica migration stall", survivor, context))
    clean = _ask(vault, TURN)

    assert restricted == clean
    assert [a["path"] for a in clean["anchors"] if "carried_link" in a["evidence"]] == [OREN]


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


def test_a_withheld_units_category_chooses_no_lens(vault: Path) -> None:
    """The removed unit is the note's only decision. The lenses that read
    decisions must not be listed, as in a note without it."""
    seed_ordinary_notes(vault)
    _write(vault, SECRET, _note("Secret cluster", "- [fact] Hidden. ^s-1"))
    _withhold(vault, SECRET, ceiling=0)
    plain = "- [failure] The staging replica migration stalled twice last week. ^r-plain"
    hidden = "- [decision] The replica migration moved off [[Secret cluster]] last week. ^r-secret"
    _write(vault, NOTE, _note("Replica migration stall", f"{plain}\n{hidden}"))
    restricted = _ask(vault, TURN)

    _write(vault, NOTE, _note("Replica migration stall", plain))
    clean = _ask(vault, TURN)

    assert restricted == clean
    assert [role["id"] for role in clean["roles"]] == ["material"]


def test_a_carried_page_left_with_nothing_abstains_like_its_twin(vault: Path) -> None:
    """The note's only unit links a withheld page. A note with nothing to serve
    abstains `unresolved`; the restricted packet must say the same."""
    seed_ordinary_notes(vault)
    _write(vault, SECRET, _note("Secret cluster", "- [fact] Hidden. ^s-1"))
    _withhold(vault, SECRET, ceiling=0)
    hidden = (
        "- [failure] The staging replica migration stalled near [[Secret cluster]] "
        "last week. ^r-secret"
    )
    _write(vault, NOTE, _note("Replica migration stall", hidden))
    restricted = _ask(vault, TURN)

    _write(vault, NOTE, _note("Replica migration stall", ""))
    clean = _ask(vault, TURN)

    assert restricted == clean
    assert clean["abstention"] == {"reason": "unresolved"}


def test_a_withheld_unit_takes_no_role_slot(vault: Path) -> None:
    """The removed unit is served first and fills one of a role's three slots.
    The fourth decision must be served whole, not pointed at as `role_cap`."""
    seed_ordinary_notes(vault)
    _write(vault, SECRET, _note("Secret cluster", "- [fact] Hidden. ^s-1"))
    _withhold(vault, SECRET, ceiling=0)
    hidden = "- [decision] The replica migration moved off [[Secret cluster]] last week. ^r-a"
    kept = "\n".join(
        f"- [decision] The replica migration retry {number} moved to the staging window. ^r-{ref}"
        for number, ref in ((1, "b"), (2, "c"), (3, "d"))
    )
    _write(vault, NOTE, _note("Replica migration stall", f"{hidden}\n{kept}"))
    restricted = _ask(vault, TURN)

    _write(vault, NOTE, _note("Replica migration stall", kept))
    clean = _ask(vault, TURN)

    assert restricted == clean
    assert [unit["ref"][-4:] for unit in clean["units"]] == ["#r-b", "#r-c", "#r-d"]
    assert clean["pointers"] == []


def test_a_withheld_unit_takes_no_material_slot(vault: Path) -> None:
    """The material lane serves three units and reads the first one off this
    note. With it removed, the next unit takes its slot."""
    seed_ordinary_notes(vault)
    _write(vault, SECRET, _note("Secret cluster", "- [fact] Hidden. ^s-1"))
    _withhold(vault, SECRET, ceiling=0)
    hidden = (
        "- [failure] The staging replica migration stalled near [[Secret cluster]] "
        "last week. ^r-a"
    )
    kept = "\n".join(
        f"- [failure] The staging replica migration stalled on attempt {number} last week. ^r-{ref}"
        for number, ref in ((1, "b"), (2, "c"), (3, "d"), (4, "e"))
    )
    _write(vault, NOTE, _note("Replica migration stall", f"{hidden}\n{kept}"))
    restricted = _ask(vault, TURN)

    _write(vault, NOTE, _note("Replica migration stall", kept))
    clean = _ask(vault, TURN)

    assert restricted == clean
    assert [unit["ref"][-4:] for unit in clean["units"]] == ["#r-b", "#r-c", "#r-d"]


def test_a_withheld_unit_past_the_cap_is_not_reported_as_cut(vault: Path) -> None:
    """The removed unit is never served: it would be the material lane's fourth.
    Reporting the lane as truncated would say that a fourth unit exists."""
    seed_ordinary_notes(vault)
    _write(vault, SECRET, _note("Secret cluster", "- [fact] Hidden. ^s-1"))
    _withhold(vault, SECRET, ceiling=0)
    kept = "\n".join(
        f"- [failure] The staging replica migration stalled on attempt {number} last week. ^r-{ref}"
        for number, ref in ((1, "a"), (2, "b"), (3, "c"))
    )
    hidden = (
        "- [failure] The staging replica migration stalled near [[Secret cluster]] "
        "last week. ^r-z"
    )
    _write(vault, NOTE, _note("Replica migration stall", f"{kept}\n{hidden}"))
    restricted = _ask(vault, TURN)

    _write(vault, NOTE, _note("Replica migration stall", kept))
    clean = _ask(vault, TURN)

    assert restricted == clean
    assert clean["missing"] == []


def test_a_withheld_conclusion_takes_no_conclusion_page_slot(vault: Path) -> None:
    """Past an entity's link cap, its six newest pages holding a conclusion are
    read. The newest holds only a withheld one, so the seventh newest must be
    read in its place, as in a vault where the newest page holds no conclusion."""
    person = f"{PEOPLE}/Ilse Vandermeer.md"
    _write(vault, person, _person("Ilse Vandermeer"))
    _write(vault, SECRET, _note("Secret cluster", "- [fact] Hidden. ^s-1"))
    _withhold(vault, SECRET, ceiling=0)
    related = "Related: [[Ilse Vandermeer]]"
    for index in range(45):
        _write(
            vault,
            f"Knowledge Base/Notes/Research/aa-gauge-log-{index:02d}.md",
            _note(f"Gauge log {index:02d}", f"- [fact] Gauge batch {index:02d} was filed. ^g-{index}", related),
        )

    def ruling(day: int, units: str) -> str:
        return (
            f"---\ntype: note\nstatus: active\nupdated: 2026-09-{day:02d}\n---\n\n"
            f"# Gauge ruling {day}\n\n## Summary\n\n{units}\n\n## Context\n\n{related}\n"
        )

    for day in range(1, 8):
        _write(
            vault,
            f"Knowledge Base/Notes/Decisions/zz-ruling-{day}.md",
            ruling(day, f"- [decision] Gauge ruling {day} keeps the weekly review. ^z-{day}"),
        )
    newest = "Knowledge Base/Notes/Decisions/zz-ruling-9.md"
    _write(vault, newest, ruling(9, "- [decision] Gauge reviews move to the [[Secret cluster]] rota. ^z-9"))
    turn = "Ilse Vandermeer asked whether the harbour gauges look healthy this week."
    restricted = _ask(vault, turn)

    _write(vault, newest, ruling(9, ""))
    clean = _ask(vault, turn)

    assert restricted == clean
    read = [entry["ref"] for entry in (*clean["units"], *clean["pointers"])]
    assert any(ref.endswith("zz-ruling-2.md#z-2") for ref in read)


def test_a_unit_withheld_at_a_notice_level_is_still_marked(vault: Path) -> None:
    """At a notice level the caller may know that a section lost something,
    so a unit linking a page released only at that level is removed and the
    units section is marked `withheld`, as for any other notice-level item."""
    seed_ordinary_notes(vault)
    _write(vault, SECRET, _note("Secret cluster", "- [fact] Hidden. ^s-1"))
    _withhold(vault, SECRET, ceiling=1)
    plain = "- [failure] The staging replica migration stalled twice last week. ^r-plain"
    naming = (
        "- [failure] The migration stalled on the staging replica last week "
        "near [[Secret cluster]]. ^r-secret"
    )
    _write(vault, NOTE, _note("Replica migration stall", f"{plain}\n{naming}"))

    restricted = _ask(vault, TURN)

    assert [unit["ref"][-8:] for unit in restricted["units"]] == ["#r-plain"]
    assert {"role": "units", "reason": "withheld"} in restricted["missing"]
