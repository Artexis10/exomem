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
    return {a["path"] for a in packet["anchors"] if a["status"] == "retrieval_carried"}


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
