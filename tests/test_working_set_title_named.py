"""A page named by its title is carried even when only one word of it is rare.

The retrieval carry admits a page on a phrase of two distinctive words. A page
called "Quorra review cycle" is named by "the quorra review" all the same:
`quorra` is distinctive, `review` is an ordinary word, and the title says the
two belong together. Now, when no two distinctive words name a page, one
distinctive word beside an ordinary neighbour does, if a current page's own
TITLE carries both. An ordinary word the title does not carry, or a title of
ordinary words alone, names nothing.

Invented pages, the carry suite's corpus.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_working_set_carry import _seed_prose_corpus

from exomem import lexstore, working_set, working_set_index, working_set_runtime

NAMED = "Knowledge Base/Notes/Research/quorra-review-cycle.md"
BODY_ONLY = "Knowledge Base/Notes/Research/quorra-scratch.md"


def _page(vault: Path, rel: str, title: str, unit: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntype: research-note\nstatus: active\nupdated: 2026-09-12\n---\n\n"
        f"# {title}\n\n## Summary\n\n{unit}\n",
        encoding="utf-8",
    )


@pytest.fixture
def title_vault(vault: Path) -> Path:
    _seed_prose_corpus(vault)
    _page(
        vault,
        NAMED,
        "Quorra review cycle",
        "- [decision] The quorra cadence was fixed at nine days. ^q-1",
    )
    _page(
        vault,
        BODY_ONLY,
        "Quorra scratch",
        "- [decision] The quorra scratch was cleared after the weekly review. ^q-2",
    )
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    return vault


def _carried(packet: dict) -> set[str]:
    # Every carried page is kind `page`; one the turn names by two title words
    # is `resolved` (the 2026-10-05 ruling), any other `retrieval_carried`.
    return {a["path"] for a in packet["anchors"] if a["kind"] == "page"}


def test_one_distinctive_word_beside_a_title_word_names_the_page(title_vault: Path) -> None:
    packet = working_set.compile_packet(
        title_vault, turn="what did the quorra review conclude", max_chars=4000
    )

    assert packet["abstained"] is False, packet["anchors"]
    # `Quorra scratch` says `review` only in its body: not a name word, so it
    # is neither carried nor a rival that would make the phrase a question.
    assert _carried(packet) == {NAMED}


def test_ordinary_words_alone_never_name_a_page(title_vault: Path) -> None:
    packet = working_set.compile_packet(
        title_vault, turn="how did the review cycle go this week", max_chars=4000
    )

    assert _carried(packet) == set()


def test_full_title_excludes_a_shared_suffix_sibling(title_vault: Path) -> None:
    """A shared suffix must not hide the page whose complete title was stated."""
    sibling = "Knowledge Base/Notes/Research/meeting-quorra-cycle.md"
    _page(title_vault, NAMED, "Tuesday quorra cycle", "- [finding] The cycle lasts nine days.")
    _page(title_vault, sibling, "Meeting quorra cycle", "- [finding] The cycle lasts two days.")
    lexstore.ensure_fresh(title_vault)

    packet = working_set.compile_packet(
        title_vault, turn="what did the Tuesday quorra cycle settle", max_chars=4000
    )

    assert _carried(packet) == {NAMED}
    assert {unit["provenance"]["path"] for unit in packet["units"]} == {NAMED}


def test_disjoint_full_titles_with_a_shared_word_are_both_carried(title_vault: Path) -> None:
    """Sharing a rare word must not merge separately stated complete titles."""
    other = "Knowledge Base/Notes/Research/quorra-meeting.md"
    _page(title_vault, other, "Quorra meeting", "- [finding] The meeting lasts two days.")
    lexstore.ensure_fresh(title_vault)

    packet = working_set.compile_packet(
        title_vault,
        turn="what did the quorra review cycle and the quorra meeting settle",
        max_chars=6000,
    )

    assert _carried(packet) == {NAMED, other}
    assert {unit["provenance"]["path"] for unit in packet["units"]} >= {NAMED, other}


def test_a_separate_shorter_phrase_survives_a_nested_title(title_vault: Path) -> None:
    """Consuming one nested mention must not consume the same words elsewhere."""
    shorter = "Knowledge Base/Notes/Research/quorra-cycle.md"
    _page(title_vault, NAMED, "Tuesday quorra cycle", "- [finding] The cycle lasts nine days.")
    _page(title_vault, shorter, "Quorra cycle", "- [finding] The cycle lasts two days.")
    lexstore.ensure_fresh(title_vault)

    groups, state = working_set_runtime.carry_named_groups(
        title_vault, "what did Tuesday quorra cycle settle? What did quorra cycle settle?"
    )

    assert state == "available"
    assert {frozenset(path for path, _score in group) for group in groups} == {
        frozenset({NAMED}),
        frozenset({NAMED, shorter}),
    }


def test_equal_complete_titles_remain_contested(title_vault: Path) -> None:
    """A complete title must not pick one of two current equal namesakes."""
    namesake = "Knowledge Base/Notes/Research/quorra-review-copy.md"
    _page(title_vault, namesake, "Quorra review cycle", "- [finding] The cycle lasts two days.")
    lexstore.ensure_fresh(title_vault)

    packet = working_set.compile_packet(
        title_vault, turn="what did the quorra review cycle settle", max_chars=4000
    )

    assert _carried(packet) == set()
    assert {anchor["path"] for anchor in packet["anchors"]} == {NAMED, namesake}


def test_a_longer_title_supporting_the_whole_phrase_remains_contested(title_vault: Path) -> None:
    """An unstated extension is still a namesake of the whole stated title."""
    namesake = "Knowledge Base/Notes/Research/quorra-review-week.md"
    _page(
        title_vault, namesake, "Quorra review cycle week", "- [finding] The cycle lasts two days."
    )
    lexstore.ensure_fresh(title_vault)

    packet = working_set.compile_packet(
        title_vault, turn="what did the quorra review cycle settle", max_chars=4000
    )

    assert _carried(packet) == set()
    assert {anchor["path"] for anchor in packet["anchors"]} == {NAMED, namesake}


def test_a_sentence_break_in_a_longer_namesake_does_not_make_the_phrase_unique(
    title_vault: Path,
) -> None:
    """A title's extra sentence must not hide its support for the stated phrase."""
    namesake = "Knowledge Base/Notes/Research/quorra-review-week.md"
    _page(
        title_vault, namesake, "Quorra review cycle; week", "- [finding] The cycle lasts two days."
    )
    lexstore.ensure_fresh(title_vault)

    packet = working_set.compile_packet(
        title_vault, turn="what did the quorra review cycle settle", max_chars=4000
    )

    assert _carried(packet) == set()
    assert {anchor["path"] for anchor in packet["anchors"]} == {NAMED, namesake}


def test_noncontained_overlapping_titles_remain_contested(title_vault: Path) -> None:
    """Overlapping title interpretations must not be served as independent domains."""
    overlapping = "Knowledge Base/Notes/Research/review-cycle-quorra.md"
    _page(title_vault, overlapping, "Review cycle quorra", "- [finding] The cycle lasts two days.")
    lexstore.ensure_fresh(title_vault)

    packet = working_set.compile_packet(
        title_vault, turn="what did quorra review cycle quorra settle", max_chars=4000
    )

    assert _carried(packet) == set()
    assert {anchor["path"] for anchor in packet["anchors"]} == {NAMED, overlapping}


def test_a_bounded_candidate_prefix_cannot_prove_a_unique_title(title_vault: Path) -> None:
    """A query limit hiding a namesake must not establish a unique full title."""
    _page(title_vault, BODY_ONLY, "Quorra review cycle", "- [finding] The cycle lasts two days.")
    lexstore.ensure_fresh(title_vault)

    groups, state = working_set_runtime.carry_named_groups(
        title_vault, "what did the quorra review cycle settle", limit=1
    )

    assert state == "available"
    domains, _contested = working_set.named_domains(groups)
    assert domains == ()
