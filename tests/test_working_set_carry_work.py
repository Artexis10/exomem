"""The carry's phrase query costs what its phrases match, not what its words match.

A long voice turn names one thing in a sea of everyday words. The carry then
asks for the pages whose title holds a phrase of the turn: a distinctive word
beside its neighbours. That query ranked every page holding ANY word of the
turn, which on such a turn is nearly every page, and ran its all-words-of-a-
phrase test on each of them once per phrase. A live turn spent seconds there.
The index is now asked for the pages that hold a phrase's words, and the test
runs on those alone; the pages returned and their scores are unchanged.

Work is the number of phrase tests the carry's ranking queries run: no clock in
it, and no filler page holds a phrase, so four times the filler must not
multiply it. Invented, generic vocabulary throughout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import lexstore, working_set_runtime

#: Everyday words: every filler page holds all of them, so none is distinctive.
EVERYDAY = tuple(
    """
    garden window market river table paper school letter summer winter
    morning evening kitchen station bridge corner ticket travel weather
    coffee bottle basket pencil blanket carpet ladder mirror pillow rocket
    circle square number record signal method machine engine vessel harbor
    island valley forest desert meadow stream canyon summit border village
    review castle temple tower palace cottage cabin shelter garage office
    """.split()
)
#: The page the turn names: one distinctive word beside two everyday ones.
NAMED_PAGE = "Knowledge Base/Notes/Research/kelvane-harbor-review.md"
#: The turn: one name inside a long run of everyday words, as dictated.
TURN = (
    "so yeah " + " ".join(EVERYDAY[:30]) + " and you know the kelvane harbor review "
    + " ".join(EVERYDAY[30:]) + " anyway"
)
SMALL_CORPUS = 120
LARGE_CORPUS = 4 * SMALL_CORPUS


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _grow_filler(vault: Path, total: int) -> None:
    """Filler pages up to `total`, each holding every everyday word."""
    folder = vault / "Knowledge Base" / "Notes" / "Journal"
    for index in range(total):
        path = folder / f"filler-{index:04d}.md"
        if not path.exists():
            _write(
                path,
                f"---\ntype: note\nstatus: active\n---\n\n# Filler {index:04d}\n\n"
                + " ".join(EVERYDAY) + "\n",
            )
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()


def _phrase_tests(vault: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[int, tuple]:
    """How many times one warm carry tests a page for a phrase, and its groups.

    The test is SQLite's `instr` over a page's stems, so the count is the
    calls to it while the carry's ranking queries run: pages examined, times
    the phrases each is tested for. The first call fills the per-snapshot
    corpus statistics the ranking reads; the second is the steady state of a
    served turn.
    """
    tests = [0]
    counting = [False]
    connect = lexstore.LexicalStore._connect

    def instr(text, needle):
        if counting[0]:
            tests[0] += 1
        if text is None or needle is None:
            return None
        return str(text).find(str(needle)) + 1

    def counted_connect(self, *args, **kwargs):
        conn = connect(self, *args, **kwargs)
        conn.create_function("instr", 2, instr, deterministic=True)
        return conn

    search = lexstore.search_bm25_result

    def counted_search(*args, **kwargs):
        counting[0] = True
        try:
            return search(*args, **kwargs)
        finally:
            counting[0] = False

    with monkeypatch.context() as patch:
        patch.setattr(lexstore.LexicalStore, "_connect", counted_connect)
        patch.setattr(lexstore, "search_bm25_result", counted_search)
        working_set_runtime.carry_named_groups(vault, TURN)
        tests[0] = 0
        groups, state = working_set_runtime.carry_named_groups(vault, TURN)
    assert state == "available"
    return tests[0], tuple(tuple(path for path, _score in group) for group in groups)


@pytest.mark.timeout(300)
def test_the_carry_phrase_query_does_not_grow_with_the_pages_its_words_match(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(
        vault / NAMED_PAGE,
        "---\ntype: research-note\nstatus: active\n---\n\n# Kelvane Harbor Review\n\n"
        "The kelvane harbor review raised the ceiling. " + " ".join(EVERYDAY) + "\n",
    )
    _grow_filler(vault, SMALL_CORPUS)
    small_tests, small_groups = _phrase_tests(vault, monkeypatch)
    _grow_filler(vault, LARGE_CORPUS)
    large_tests, large_groups = _phrase_tests(vault, monkeypatch)

    assert small_groups == large_groups == ((NAMED_PAGE,),)
    assert small_tests > 0, "the carry no longer tests pages for a phrase; re-target this guard"
    assert large_tests <= 1.5 * small_tests, (
        f"the carry ran {small_tests} phrase tests over {SMALL_CORPUS} filler pages and "
        f"{large_tests} over {LARGE_CORPUS}: it examines the pages the turn's words "
        "match, not only the pages that hold a phrase"
    )
