"""Display marks on the units that `recall_context` serves.

Slice 5b gave a search's dimension groups their marks: `open`, `replaced` and `origin`, and the
order each group's display behaviours set. This module tests the same marks and order on the
units of a context packet, which carries them with no request parameter.

The module began as the before-picture: `test_the_packet_this_vault_serves` recorded, on the code
before the marks, the units one `recall_context` call serves, their order and their fields. A line
marked `# FLIPS marks` is one that the marks changed on purpose, and its comment names the value
it held before. Every other line of that test held before and still holds.

The fixture vault is invented. A hub page holds a decision that a later decision supersedes, two
open questions of which another page answers one, a claim with its holder and four sources, and
two timeline events written in the reverse order of their dates.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import (
    commands,
    epistemic_graph,
    lexstore,
    working_set_index,
    working_set_runtime,
)
from exomem import find as find_module

pytestmark = pytest.mark.usefixtures("anchors_packet")  # marks ride on the anchors packet's units

NOTES = "Knowledge Base/Notes"
HUB = f"{NOTES}/Orchard launch.md"
HUB_REF = "exomem://memory/11111111-1111-4111-8111-111111111111"
SOURCES = ("Orchard survey", "Orchard interviews", "Orchard tickets", "Orchard forum")
HUB_PAGE = (
    "---\ntype: insight\nstatus: active\ntags: [hub]\n"
    "exomem_id: 11111111-1111-4111-8111-111111111111\n---\n# Orchard launch\n\n"
    "The orchard launch brings the cider app to stores.\n\n"
    "## Decision\n- id: before\n\nThe orchard launch ships in January.\n\n"
    "## Decision\n- id: after\n- relations: supersedes: [[#before]]\n\n"
    "The orchard launch ships in March.\n\n"
    "## Open question\n- id: beta\n\nDoes the orchard launch need a beta?\n\n"
    "## Open question\n- id: support\n\nWho owns orchard launch support?\n\n"
    "## Claim\n- id: exports\n- asserted_by: Research team\n"
    + "".join(f"- relations: derived_from: [[{source}]]\n" for source in SOURCES)
    + "\nOrchard launch exports come first on the roadmap.\n\n"
    "## Timeline event\n- id: retro\n- occurred_at: 2026-03-01\n\nThe kickoff retrospective is held.\n\n"
    "## Timeline event\n- id: kickoff\n- occurred_at: 2026-01-10\n\nThe kickoff meeting opens the work.\n"
)
ANSWER_PAGE = (
    "---\ntype: insight\n---\n# Orchard answers\n\n"
    "## Finding\n- relations: answers: [[Orchard launch#beta]]\n\n"
    "The orchard launch needs a two week beta.\n"
)
TURN = "Remind me about the orchard launch: what changed since kickoff, and what about exports?"


def _vault(tmp_path: Path, *, answer: str | None = f"{NOTES}/Orchard answers.md") -> Path:
    """The fixture vault, with its answering page at `answer`, or without it when `answer` is
    None, and every index the packet reads built."""
    from conftest import initialize_vault_state_offline

    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    initialize_vault_state_offline(vault, source="display marks fixture")
    pages = {HUB: HUB_PAGE, **{
        f"{NOTES}/{source}.md": f"---\ntype: insight\n---\n# {source}\n\n## Claim\n\n{source} were read in May.\n"
        for source in SOURCES
    }}
    if answer is not None:
        pages[answer] = ANSWER_PAGE
    for rel, body in pages.items():
        (vault / rel).parent.mkdir(parents=True, exist_ok=True)
        (vault / rel).write_text(body, encoding="utf-8")
    find_module.clear_cache()
    lexstore.ensure_fresh(vault)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    return vault


def _recall(vault: Path, turn: str = TURN, **kwargs) -> dict:
    return commands.op_activate_context(vault, turn=turn, max_chars=6000, **kwargs)


def _units(packet: dict) -> dict[str, dict]:
    """Each served unit, by its text."""
    return {unit["text"]: unit for unit in packet["units"]}


def test_the_packet_this_vault_serves(tmp_path: Path) -> None:
    """The units, order and fields one `recall_context` call serves on the fixture vault.

    Catches a mark that removes, adds or moves a unit it should not, or changes a field the
    packet served before the marks.
    """
    packet = _recall(_vault(tmp_path))

    assert packet["abstained"] is False
    assert [unit["text"] for unit in packet["units"]] == [
        "The orchard launch ships in March.",
        "The orchard launch needs a two week beta.",
        "The orchard launch ships in January.",
        "The orchard launch brings the cider app to stores.",
        "Orchard launch exports come first on the roadmap.",
        "The kickoff retrospective is held.",
        "The kickoff meeting opens the work.",
        "Does the orchard launch need a beta?",
        "Who owns orchard launch support?",
    ]
    units = _units(packet)
    assert units["The orchard launch ships in January."]["lifecycle"] == "superseded"
    assert units["The orchard launch ships in January."]["history"] is True
    assert [unit["role"] for unit in packet["units"]] == [
        "recent_change", "recent_change", "recent_change", "identity",
        "material", "material", "material", "open_questions", "open_questions",
    ]
    assert {unit["text"]: sorted(unit) for unit in packet["units"]} == {
        "The orchard launch ships in March.": [
            "lifecycle", "provenance", "ref", "role", "text", "updated"],
        "The orchard launch needs a two week beta.": [
            "lifecycle", "provenance", "ref", "role", "text", "updated"],
        "The orchard launch ships in January.": [
            "history", "lifecycle", "provenance", "ref", "role", "text", "updated"],
        "The orchard launch brings the cider app to stores.": [
            "lifecycle", "provenance", "ref", "role", "text", "updated"],
        "Orchard launch exports come first on the roadmap.": [
            "lifecycle", "provenance", "ref", "role", "text", "updated"],
        "The kickoff retrospective is held.": [
            "lifecycle", "provenance", "ref", "role", "text", "updated"],
        "The kickoff meeting opens the work.": [
            "lifecycle", "provenance", "ref", "role", "text", "updated"],
        "Does the orchard launch need a beta?": [
            "lifecycle", "provenance", "ref", "role", "text", "updated"],
        "Who owns orchard launch support?": [
            "lifecycle", "provenance", "ref", "role", "text", "updated"],
    }
    assert [pointer["ref"] for pointer in packet["pointers"]] == [f"{HUB_REF}#before"]
    assert "display_edges" not in packet["generation"]
