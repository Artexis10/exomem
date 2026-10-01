"""A resolved entity is served the conclusions linked to it.

The turn names an organisation and the compiler resolves the entity, but the
decision note written about it ("the relationship is settled", "priorities for
the quarter") sat one wikilink away, unread: entity anchors read the entity's
own facets and preferences, and a decision or finding is selected only by a
hub, a project or a turn cue such as "last time". The agent then re-litigated
what the vault had already settled. Now an entity anchor also reads the
`decision`, `insight` and `finding` units of the pages linked to it, with each
unit's own provenance, inside the packet's budget.

Invented names throughout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import lexstore, working_set, working_set_index, working_set_runtime

ORG = "Knowledge Base/Entities/Organisations/Larkspur Cooperative.md"
SETTLED = "Knowledge Base/Notes/Decisions/larkspur-relationship-status.md"
PRIORITIES = "Knowledge Base/Notes/Decisions/larkspur-priorities.md"
UNLINKED = "Knowledge Base/Notes/Decisions/quayside-tender-outcome.md"
TURN = "Larkspur Cooperative sent over a fresh tender proposal this morning."


def _write(vault: Path, rel: str, text: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _note(title: str, unit: str, *, link: bool, block: str) -> str:
    links = f"\nRelated: [[{ORG.removesuffix('.md')}]]\n" if link else "\n"
    return (
        "---\ntype: note\nstatus: active\nupdated: 2026-09-20\n---\n\n"
        f"# {title}\n\n## Summary\n\n{unit} ^{block}\n{links}"
    )


@pytest.fixture
def entity_vault(vault: Path) -> Path:
    _write(
        vault,
        ORG,
        "---\ntype: entity\nentity_type: organisation\nstatus: active\n---\n\n"
        "# Larkspur Cooperative\n\n## Summary\n\nA supplier cooperative on the coast.\n",
    )
    _write(
        vault,
        SETTLED,
        _note(
            "Larkspur relationship status",
            "- [decision] The Larkspur relationship is settled: renewals stay annual and no "
            "further tender is opened.",
            link=True,
            block="l-settled",
        ),
    )
    _write(
        vault,
        PRIORITIES,
        _note(
            "Larkspur priorities",
            "- [insight] Larkspur priorities for the quarter are delivery reliability first, "
            "price second.",
            link=True,
            block="l-priorities",
        ),
    )
    _write(
        vault,
        UNLINKED,
        _note(
            "Quayside tender outcome",
            "- [decision] The quayside tender was awarded to the lowest bidder.",
            link=False,
            block="q-outcome",
        ),
    )
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    return vault


def _unit_paths(packet: dict) -> set[str]:
    return {u["provenance"]["path"] for u in packet["units"]}


def test_a_resolved_entity_is_served_the_conclusions_linked_to_it(entity_vault: Path) -> None:
    packet = working_set.compile_packet(entity_vault, turn=TURN, max_chars=6000)

    assert [a["path"] for a in packet["anchors"] if a["status"] == "resolved"] == [ORG]
    assert {SETTLED, PRIORITIES} <= _unit_paths(packet)
    settled = next(u for u in packet["units"] if u["provenance"]["path"] == SETTLED)
    assert "settled" in settled["text"]
    assert settled["provenance"]["category"] == "decision"


def test_a_conclusion_that_is_not_linked_to_the_entity_is_not_served(entity_vault: Path) -> None:
    packet = working_set.compile_packet(entity_vault, turn=TURN, max_chars=6000)

    assert UNLINKED not in _unit_paths(packet)


def test_the_conclusions_stay_inside_the_lane_and_packet_bounds(entity_vault: Path) -> None:
    for index in range(working_set.UNIT_LANE_LIMIT + 5):
        _write(
            entity_vault,
            f"Knowledge Base/Notes/Decisions/larkspur-extra-{index:02d}.md",
            _note(
                f"Larkspur extra {index:02d}",
                f"- [decision] Larkspur extra decision number {index:02d} was recorded.",
                link=True,
                block=f"l-extra-{index}",
            ),
        )
    lexstore.ensure_fresh(entity_vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(entity_vault).rebuild()

    packet = working_set.compile_packet(entity_vault, turn=TURN, max_chars=3000)

    served = [u for u in packet["units"] if u["provenance"].get("category") in {"decision", "insight"}]
    assert 0 < len(served) <= 2 * working_set.UNIT_LANE_LIMIT
    assert len(str(packet["units"])) <= 3000 * 2
