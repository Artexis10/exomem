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
from test_governance_egress import (
    OPEN_PATH,
    RESTRICTED_PATH,
    _external,
    _reset_caches,
    write_rule,
    write_scope,
)
from test_working_set_carry import seed_ordinary_notes

from exomem import commands, lexstore, memory_refs, working_set_index, working_set_runtime
from exomem.governance import egress
from exomem.governance.principal import request_scope

TALIA_ID = "5b1d7c2e-8f4a-4c61-9e3b-7a2d6f0c9e14"
TALIA = "Knowledge Base/Entities/People/Talia Verenko.md"
OREN = "Knowledge Base/Entities/People/Oren Haldane.md"
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
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    return vault


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


def test_a_person_the_reader_may_not_see_is_not_listed(linked_vault: Path) -> None:
    write_scope(linked_vault, paths=TALIA, name="Withheld")
    write_rule(linked_vault, ceiling=0)
    _reset_caches()
    working_set_runtime.reset_caches_for_tests()

    with request_scope(_external()):
        packet = commands.op_activate_context(linked_vault, turn=TURN)

    anchors = _anchors(packet)
    assert NOTE in anchors, anchors
    assert memory_refs.memory_ref(TALIA_ID) not in anchors and TALIA not in str(anchors)


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
