"""A carried page's served unit names a person it links: list that person.

A turn that does not say a name ("my teammate who had the replica migration
stall") can still carry the failure note it describes, and that note's unit
names the teammate and wikilinks her page. The packet served the note and
dropped the person: a carried page's only anchor was itself. Now an anchor
row linked to or from a carried page, whose title or alias appears in a unit
that page served, is listed beside it as `partial` (`carried_link`), never
`resolved`. A row linked but not named in the served unit stays out, and so
does one the reader may not see. If the guard removes the page it was
carried through, the listed anchor goes with it.

Invented names throughout.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_governance_egress import OPEN_PATH, RESTRICTED_PATH, _external, write_rule, write_scope
from test_origin_bindings import _write as _write_input
from test_working_set_carry import (
    CARRY_PAGE,
    GENUINE_PAGE,
    _seed_carry_pages,
    _seed_prose_corpus,
    seed_ordinary_notes,
)
from test_working_set_named_domains import BOTH_TURN

from exomem import (
    commands,
    lexstore,
    memory_refs,
    provenance,
    working_set_index,
    working_set_runtime,
)
from exomem.governance import egress
from exomem.governance.principal import request_scope

TALIA_ID = "5b1d7c2e-8f4a-4c61-9e3b-7a2d6f0c9e14"
TALIA = "Knowledge Base/Entities/People/Talia Verenko.md"
OREN = "Knowledge Base/Entities/People/Oren Haldane.md"
PELL = "Knowledge Base/Entities/People/Pell Mordaunt.md"
QUILL = "Knowledge Base/Entities/People/Quill Aster.md"
NOTE = "Knowledge Base/Notes/Failures/replica-migration-stall.md"
TURN = "My teammate who had the replica migration stall last week pinged again."


def _write(vault: Path, rel: str, text: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _person(name: str, summary: str, exomem_id: str = "") -> str:
    identity = f"exomem_id: {exomem_id}\n" if exomem_id else ""
    return (
        f"---\ntype: entity\nentity_type: person\nstatus: active\n{identity}---\n\n"
        f"# {name}\n\n## Summary\n\n{summary}\n"
    )


@pytest.fixture
def linked_vault(vault: Path) -> Path:
    seed_ordinary_notes(vault)
    _write(
        vault, TALIA, _person("Talia Verenko", "Platform engineer on the storage team.", TALIA_ID)
    )
    _write(vault, OREN, _person("Oren Haldane", "Reviews storage changes."))
    _write(
        vault,
        NOTE,
        "---\ntype: note\nstatus: active\nupdated: 2026-09-28\n---\n\n"
        "# Replica migration stall\n\n## Summary\n\n"
        "- [failure] Talia Verenko's migration stalled on the staging replica last week. ^r-stall\n\n"
        "## Context\n\nAffected teammate: [[Talia Verenko]]. Reviewer: [[Oren Haldane]].\n",
    )
    _reindex(vault)
    return vault


def _reindex(vault: Path) -> None:
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()


def _anchors(packet: dict) -> dict[str, tuple[str, list[str]]]:
    return {a["ref"]: (a["status"], list(a["evidence"])) for a in packet.get("anchors") or ()}


def test_a_person_the_carried_unit_names_is_listed_partial(linked_vault: Path) -> None:
    packet = commands.op_activate_context(linked_vault, turn=TURN)

    anchors = _anchors(packet)
    # The turn also names the note by its title, so the note itself resolves.
    assert anchors.get(NOTE, ("",))[0] == "resolved", anchors
    assert anchors.get(memory_refs.memory_ref(TALIA_ID)) == ("partial", ["carried_link"]), anchors
    # Linked from the same note, but the served unit never names him.
    assert not any("Oren" in str(a) for a in packet["anchors"]), anchors


def test_the_people_a_unit_names_first_are_the_ones_listed(vault: Path) -> None:
    """Two slots go to the unit's own subject first, not to catalogue order."""
    seed_ordinary_notes(vault)
    for rel, name in (
        (TALIA, "Talia Verenko"),
        (OREN, "Oren Haldane"),
        (PELL, "Pell Mordaunt"),
        (QUILL, "Quill Aster"),
    ):
        _write(vault, rel, _person(name, "Works on storage."))
    _write(
        vault,
        NOTE,
        "---\ntype: note\nstatus: active\nupdated: 2026-09-28\n---\n\n"
        "# Replica migration stall\n\n## Summary\n\n"
        "- [failure] Talia Verenko's migration stalled on the staging replica last week; "
        "Oren Haldane, Pell Mordaunt and Quill Aster were on call. ^r-stall\n\n"
        "## Context\n\n[[Oren Haldane]] [[Pell Mordaunt]] [[Quill Aster]] [[Talia Verenko]]\n",
    )
    _reindex(vault)

    packet = commands.op_activate_context(vault, turn=TURN)

    listed = [a["path"] for a in packet["anchors"] if "carried_link" in a["evidence"]]
    assert listed == [TALIA, OREN], packet["anchors"]


def test_a_unit_too_long_to_serve_names_no_one(vault: Path) -> None:
    """A unit the packet turns into a pointer was never served, so it lists no one."""
    seed_ordinary_notes(vault)
    _write(vault, TALIA, _person("Talia Verenko", "Works on storage."))
    filler = " ".join(["the staging replica lag grew steadily"] * 30)
    _write(
        vault,
        NOTE,
        "---\ntype: note\nstatus: active\nupdated: 2026-09-28\n---\n\n"
        "# Replica migration stall\n\n## Summary\n\n"
        "- [decision] The migration moved to the staging replica last week. ^r-plain\n"
        f"- [decision] Talia Verenko's migration moved to the staging replica; {filler}. ^r-long\n\n"
        "## Context\n\nAffected teammate: [[Talia Verenko]].\n",
    )
    _reindex(vault)

    packet = commands.op_activate_context(vault, turn=TURN)

    assert any(p.get("reason") == "unit_too_long" for p in packet["pointers"]), packet["pointers"]
    assert not [a for a in packet["anchors"] if "carried_link" in a["evidence"]], packet["anchors"]


def test_a_link_only_an_origin_carrier_holds_lists_no_one(vault: Path) -> None:
    """A carrier is attribution, not a link: a person it alone links stays out."""
    seed_ordinary_notes(vault)
    _write(vault, TALIA, _person("Talia Verenko", "Platform engineer on the storage team."))
    binding = _write_input(vault, "Original evidence.\n")
    carrier = provenance.encode_origin(
        {
            "inputs": {"original": binding},
            "assessments": [
                {
                    "inputs": ["original"],
                    "basis": "agent_assessment",
                    "by": "agent",
                    "reason": "Raised by [[Talia Verenko]].",
                }
            ],
            "bindings": [],
        }
    )
    _write(
        vault,
        NOTE,
        "---\ntype: note\nstatus: active\nupdated: 2026-09-28\n---\n\n"
        f"# Replica migration stall\n\n{carrier}\n\n## Summary\n\n"
        "- [failure] Talia Verenko's migration stalled on the staging replica last week. ^r-stall\n",
    )
    _reindex(vault)

    packet = commands.op_activate_context(vault, turn=TURN)

    assert _anchors(packet).get(NOTE, ("",))[0] == "resolved", packet["anchors"]
    assert not any("carried_link" in a["evidence"] for a in packet["anchors"]), packet["anchors"]


def test_a_person_listed_through_one_carried_page_takes_no_slot_of_the_next(vault: Path) -> None:
    """Two carried pages both name Talia first. The second page's two slots go
    to the next people it names, not to Talia a second time."""
    _seed_prose_corpus(vault)
    _seed_carry_pages(vault)
    for rel, name in (
        (TALIA, "Talia Verenko"),
        (OREN, "Oren Haldane"),
        (PELL, "Pell Mordaunt"),
        (QUILL, "Quill Aster"),
    ):
        _write(vault, rel, _person(name, "Works on storage."))
    _write(
        vault,
        CARRY_PAGE,
        "---\ntype: research-note\nstatus: active\nupdated: 2026-09-10\n---\n\n"
        "# Quillon vantry window\n\n## Summary\n\n"
        "- [decision] The quillon vantry window was widened to nine minutes; "
        "Talia Verenko and Pell Mordaunt reran the queue. ^q-decision\n\n"
        "## Context\n\n[[Talia Verenko]] [[Pell Mordaunt]]\n",
    )
    _write(
        vault,
        GENUINE_PAGE,
        "---\ntype: research-note\nstatus: active\nupdated: 2026-09-10\n---\n\n"
        "# Kelvane throughput review\n\n## Summary\n\n"
        "- [decision] The kelvane throughput ceiling was raised to eleven units; "
        "Talia Verenko, Oren Haldane and Quill Aster ran the review. ^k-decision\n\n"
        "## Context\n\n[[Talia Verenko]] [[Oren Haldane]] [[Quill Aster]]\n",
    )
    _reindex(vault)

    packet = commands.op_activate_context(vault, turn=BOTH_TURN)

    listed: dict[str, list[str]] = {}
    for anchor in packet["anchors"]:
        if "carried_link" in anchor["evidence"]:
            listed.setdefault(anchor["via"], []).append(anchor["path"])
    assert listed == {CARRY_PAGE: [TALIA, PELL], GENUINE_PAGE: [OREN, QUILL]}, packet["anchors"]


def test_the_guard_removes_a_linked_anchor_with_the_page_it_came_through(vault: Path) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=0)
    packet = {
        "anchors": [
            {
                "ref": RESTRICTED_PATH,
                "path": RESTRICTED_PATH,
                "title": "Carried",
                "kind": "page",
                "lifecycle": "active",
                "status": "retrieval_carried",
                "evidence": ["retrieval"],
            },
            {
                "ref": OPEN_PATH,
                "path": OPEN_PATH,
                "title": "Open",
                "kind": "entity",
                "lifecycle": "active",
                "status": "partial",
                "evidence": ["carried_link"],
                "via": RESTRICTED_PATH,
            },
        ],
        "units": [],
        "pointers": [],
        "current_state": [],
        "missing": [],
        "ambiguity": [],
        "roles": [],
        "budget": {"limit_chars": 4000, "used_chars": 0},
        "generation": {},
        "abstained": False,
    }
    release = egress.AnnotatedHits(
        hits=[], withheld_paths=frozenset({RESTRICTED_PATH}), active=True
    )

    with request_scope(_external()):
        guarded = egress.guard_working_set(vault, packet, release)

    assert guarded is not None
    assert guarded["anchors"] == [] and guarded["abstained"] is True
