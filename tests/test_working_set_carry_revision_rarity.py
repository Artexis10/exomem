"""A revised page stays nameable (close-memory-loop, activation quality).

The carry admits a page on a phrase of words DISTINCTIVE in the corpus, and
counts distinctiveness as the number of pages a word occurs on. A page revised
three times is the same subject written four times: its retired revisions carry
its name words too, so the words stop counting as distinctive and the current
page can no longer be named by them, however plainly the turn says them.
Retired revisions are not counted, exactly as the carry already never serves
them. Invented names only.
"""

from __future__ import annotations

from pathlib import Path

from test_working_set_carry import _write, seed_ordinary_notes
from test_working_set_index import _seed_planning, _seed_structure

from exomem import lexstore, working_set, working_set_index, working_set_runtime

RESEARCH = "Knowledge Base/Notes/Research"
CURRENT = f"{RESEARCH}/brask-ferry-timetable.md"


def _revision(name: str, *, status: str, body: str, successor: str | None) -> str:
    successor_line = f"superseded_by: [{successor}]\n" if successor else ""
    return (
        f"---\ntype: research-note\nstatus: {status}\nupdated: 2026-09-01\n"
        f"{successor_line}---\n\n# Brask ferry timetable ({name})\n\n## Summary\n\n"
        f"- [decision] The brask ferry timetable {body}\n"
    )


def _seed(vault: Path, *, retired: int) -> None:
    _seed_structure(vault)
    _seed_planning(vault)
    kb = vault / "Knowledge Base"
    _write(
        vault / CURRENT,
        "---\ntype: research-note\nstatus: active\nupdated: 2026-09-20\n---\n\n"
        "# Brask ferry timetable\n\n## Summary\n\n"
        "- [decision] The brask ferry timetable now runs hourly from the north pier.\n",
    )
    for index in range(retired):
        _write(
            kb / "Notes" / "Research" / f"brask-ferry-timetable-v{index + 1}.md",
            _revision(
                f"v{index + 1}",
                status="superseded",
                body=f"ran every {index + 2} hours from the south pier.",
                successor=CURRENT,
            ),
        )
    seed_ordinary_notes(vault)
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()


TURN = "What is the current brask ferry timetable?"


def test_a_page_revised_three_times_is_still_carried_by_its_name(vault: Path) -> None:
    _seed(vault, retired=3)

    hits, state = working_set_runtime.carry_candidates(vault, TURN)

    assert state == "available"
    assert [path for path, _score in hits] == [CURRENT]
    assert working_set.dominant_carry(hits) is not None


def test_retired_revisions_do_not_count_toward_a_words_rarity(vault: Path) -> None:
    _seed(vault, retired=3)
    stems = working_set_runtime.pairable_stems(TURN)

    rare, _pages, state = working_set_runtime.rare_turn_terms(vault, stems)

    assert state == "available"
    assert set(working_set_runtime.pairable_stems("brask ferry timetable")) <= set(rare)


def test_current_pages_sharing_the_name_still_make_it_ordinary(vault: Path) -> None:
    """Only retirement is discounted. Four CURRENT pages sharing the words keep
    them ordinary, and the turn names no single page."""

    _seed(vault, retired=0)
    for index in range(3):
        _write(
            vault / RESEARCH / f"brask-ferry-timetable-copy-{index}.md",
            _revision(f"copy {index}", status="active", body="is posted at the pier.", successor=None),
        )
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()

    hits, _state = working_set_runtime.carry_candidates(vault, TURN)

    assert working_set.dominant_carry(hits) is None
