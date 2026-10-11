"""One word of a longer name, said as an ordinary word, resolves no one.

A person titled "Nora Hedge" (file `Nora.md`) was resolved by a turn about a
garden that said "hedge" as an ordinary word: `rare_term` on "hedge", plus a
recall hit on her page that the same word produced. The page carries its own
title, so the hit counted "hedge" as one of the two content words the
lexical lane requires, and "garden" on her page completed it. Her identity
unit was served. A word that is only part of an anchor's names, written in
lower case by a turn whose casing carries a signal, is now a `partial` lead
that the anchor's own recall hit cannot resolve. The file name is a
registered name, so a turn that says "nora" still reaches her.

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
PLACE = f"{KB}/Entities/Places/Allotment.md"
IDENTITY = "Former engineering colleague from the payments team"


def _write(vault: Path, rel: str, text: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def garden_vault(vault: Path) -> Path:
    seed_ordinary_notes(vault)
    _write(
        vault,
        PERSON,
        "---\ntype: entity\nentity_type: person\nstatus: active\nupdated: 2026-09-01\n---\n\n"
        f"# Nora Hedge\n\n## Summary\n\n- [fact] {IDENTITY}; her garden backs onto the"
        " office car park. ^n-1\n",
    )
    _write(
        vault,
        PLACE,
        "---\ntype: entity\nstatus: active\nupdated: 2026-09-01\n---\n\n# Allotment\n\n"
        "## Summary\n\n- [fact] My garden plot by the railway; the shed leaks. ^p-1\n",
    )
    garden_notes = ("Hedge trimming", "Beech hedge planting", "Winter hedge care")
    for index, title in enumerate(garden_notes):
        _write(
            vault,
            f"{KB}/Notes/Research/{title.lower().replace(' ', '-')}.md",
            f"---\ntype: research-note\nstatus: active\nupdated: 2026-09-0{index + 2}\n---\n\n"
            f"# {title}\n\n## Summary\n\n- [note] Trim the hedge after the nesting season;"
            f" keep the base wider than the top. ^g-{index}\n",
        )
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    return vault


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


def test_the_file_name_in_lower_case_still_reaches_the_person(garden_vault: Path) -> None:
    packet = commands.op_activate_context(garden_vault, turn="What did nora say about payments?")

    assert _statuses(packet, PERSON) == ["resolved"], packet["anchors"]
