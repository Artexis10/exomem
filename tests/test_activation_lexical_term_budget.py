"""The lexical stage of activation is bounded on a long turn (roadmap U4).

`lexical_evidence` used to hand the WHOLE turn's content words to one FTS5
`MATCH`: a four-hundred-word turn became a four-hundred-term OR, every page
holding any everyday word was read and scored, and the corroboration test ran
over every one of them. The stage now keeps at most
`ACTIVATION_LEXICAL_MAX_TERMS` query units, chosen by how rare each is in the
catalogue, and reads a capped candidate window ranked inside SQL.

Invented, generic vocabulary throughout.
"""

from __future__ import annotations

import random
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import lexstore, working_set_runtime

#: Everyday words for the filler corpus and the long turns. None is a
#: stopword, so every one reaches the query as a content word.
COMMON_WORDS = tuple(
    """
    garden window market river table paper school letter summer winter
    morning evening kitchen station bridge corner ticket travel weather
    coffee bottle basket pencil blanket carpet ladder mirror pillow rocket
    circle square number record signal method machine engine vessel harbor
    island valley forest desert meadow stream canyon summit border village
    castle temple tower palace cottage cabin shelter garage office factory
    bakery library museum theater stadium arena lobby hallway balcony porch
    orange yellow purple silver golden copper marble velvet cotton leather
    button pocket collar sleeve jacket sweater helmet glove boots scarf
    """.split()
)
#: The anchor's own name: two words no filler page contains.
ANCHOR_TITLE = "Kelvane Throughput Review"
ANCHOR_PATH = "Knowledge Base/Projects/kelvane-throughput-review.md"
MODEL_PATH = "Knowledge Base/Projects/relay-controller.md"
FILLER_PAGES = 140


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _page(title: str, body: str) -> str:
    return f"---\ntype: note\nstatus: active\n---\n\n# {title}\n\n{body}\n"


def _long_turn(rng: random.Random, words: int, *extra: str) -> str:
    """About `words` everyday words with `extra` placed in the middle."""
    picked = [rng.choice(COMMON_WORDS) for _ in range(words)]
    middle = len(picked) // 2
    return " ".join(picked[:middle] + list(extra) + picked[middle:])


@pytest.fixture
def long_turn_vault(vault: Path) -> Path:
    """Filler pages that make every word of `COMMON_WORDS` everyday, one
    anchor named by two rare words, and one naming a rare model number."""
    rng = random.Random(4)
    kb = vault / "Knowledge Base" / "Notes" / "Journal"
    for index in range(FILLER_PAGES):
        body = " ".join(rng.sample(COMMON_WORDS, 40))
        _write(kb / f"filler-{index:04d}.md", _page(f"Filler {index:04d}", body))
    _write(
        vault / ANCHOR_PATH,
        _page(
            ANCHOR_TITLE,
            "The kelvane throughput ceiling was raised after the review. "
            + " ".join(COMMON_WORDS[:20]),
        ),
    )
    _write(
        vault / MODEL_PATH,
        _page(
            "Relay Controller",
            "The zq7400 relay controller replaced the old unit. "
            + " ".join(COMMON_WORDS[20:40]),
        ),
    )
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    return vault


def _rows(vault: Path) -> list[SimpleNamespace]:
    """An anchor catalogue: the two named pages plus a slice of filler."""
    paths = [ANCHOR_PATH, MODEL_PATH] + [
        f"Knowledge Base/Notes/Journal/filler-{index:04d}.md" for index in range(0, FILLER_PAGES, 4)
    ]
    return [SimpleNamespace(path=path, title=Path(path).stem) for path in paths]


@pytest.fixture
def statements(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every SQL statement the lexical store runs, with its parameters bound."""
    seen: list[str] = []
    original = lexstore.LexicalStore._connect

    def traced(self, *args, **kwargs):
        conn = original(self, *args, **kwargs)
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(lexstore.LexicalStore, "_connect", traced)
    return seen


_MATCH = re.compile(r"fts MATCH '((?:[^']|'')*)'")


def _ranking_match_terms(seen: list[str]) -> list[list[str]]:
    """The terms of each ranking MATCH (the statements that score by bm25)."""
    found = []
    for sql in seen:
        if "bm25(fts)" not in sql:
            continue
        match = _MATCH.search(sql)
        assert match is not None, sql
        found.append(re.findall(r'"([^"]+)"', match.group(1)))
    return found


def _unbounded_paths(vault: Path, turn: str, rows) -> list[str]:
    """What the pre-U4 query returned: the whole turn, one OR, no budget."""
    result = lexstore.search_bm25_result(
        vault,
        working_set_runtime.content_words(turn),
        len(rows),
        scope="kb",
        allowed_paths={row.path for row in rows},
        allow_delta=False,
        min_matched_terms=2,
    )
    assert result.readiness.complete
    return [path for path, _score in result.value or ()]


def test_a_long_turn_issues_a_bounded_match_and_keeps_its_anchor(
    long_turn_vault: Path, statements: list[str]
) -> None:
    """(a) Four hundred everyday words and one rare name: the MATCH holds at
    most the budget, and the anchor the unbounded query found is still found.

    Unbounded, it is found BELOW filler pages that merely share more everyday
    words with the turn; bounded, the rare name leads."""
    rng = random.Random(11)
    turn = _long_turn(rng, 400, "kelvane", "throughput")
    rows = _rows(long_turn_vault)
    unbounded = _unbounded_paths(long_turn_vault, turn, rows)
    assert ANCHOR_PATH in unbounded, unbounded
    statements.clear()

    selection: dict = {}
    hits, state = working_set_runtime.lexical_evidence(
        long_turn_vault, turn, rows, limit=8, selection=selection
    )

    assert state == "available"
    assert hits and hits[0].path == ANCHOR_PATH, [hit.path for hit in hits]
    matches = _ranking_match_terms(statements)
    assert len(matches) == 1, matches
    assert 0 < len(matches[0]) <= working_set_runtime.ACTIVATION_LEXICAL_MAX_TERMS
    assert {"kelvan", "throughput"} <= set(matches[0]), matches[0]
    assert selection["terms_kept"] == len(matches[0])
    assert selection["terms_dropped"] > 0
    assert set(selection) == {"terms_kept", "terms_dropped"}


def test_a_rare_model_number_is_kept(long_turn_vault: Path, statements: list[str]) -> None:
    """(b) The one discriminating token is a model number: digits never cost a
    term its place, rarity decides."""
    rng = random.Random(12)
    turn = _long_turn(rng, 300, "zq7400")
    rows = _rows(long_turn_vault)

    hits, state = working_set_runtime.lexical_evidence(long_turn_vault, turn, rows, limit=8)

    assert state == "available"
    matches = _ranking_match_terms(statements)
    assert len(matches) == 1, matches
    assert "zq7400" in matches[0], matches[0]
    assert len(matches[0]) <= working_set_runtime.ACTIVATION_LEXICAL_MAX_TERMS
    # The rarest term leads the kept set: nothing in the corpus is rarer.
    assert all(hit.path in {row.path for row in rows} for hit in hits)


def test_a_cjk_turn_still_matches_by_unit(vault: Path, statements: list[str]) -> None:
    """(c) Unspaced runs are counted as units: a run's bigrams are kept or
    dropped together, and a long Japanese turn still reaches its page."""
    kb = vault / "Knowledge Base" / "Notes"
    everyday = ["天気", "散歩", "電車", "買物", "料理", "映画", "音楽", "写真"]
    for index in range(120):
        _write(
            kb / f"日記-{index:03d}.md",
            _page(f"日記 {index:03d}", "。".join(everyday) + "。"),
        )
    anchor = "Knowledge Base/Projects/議事録.md"
    _write(vault / anchor, _page("議事録", "議事録は翌日までに共有します。"))
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    rows = [SimpleNamespace(path=anchor, title="議事録")] + [
        SimpleNamespace(path=f"Knowledge Base/Notes/日記-{index:03d}.md", title="日記")
        for index in range(0, 120, 12)
    ]
    turn = "。".join(everyday * 3) + "。議事録 共有。" + "。".join(everyday * 3)

    hits, state = working_set_runtime.lexical_evidence(vault, turn, rows, limit=8)

    assert state == "available"
    assert anchor in [hit.path for hit in hits], [hit.path for hit in hits]
    matches = _ranking_match_terms(statements)
    assert len(matches) == 1, matches
    assert {"議事", "事録", "共有"} <= set(matches[0]), matches[0]


@pytest.mark.parametrize("turn", ["", "the and of it is", "   ", "!!! ???"])
def test_a_turn_with_no_content_words_abstains_without_error(
    long_turn_vault: Path, statements: list[str], turn: str
) -> None:
    """(d) Nothing left after the stopwords: no MATCH, no error, no hits."""
    hits, state = working_set_runtime.lexical_evidence(
        long_turn_vault, turn, _rows(long_turn_vault), limit=8
    )
    assert (hits, state) == ([], "available")
    assert _ranking_match_terms(statements) == []


def test_a_turn_of_only_everyday_words_stays_bounded_and_does_not_error(
    long_turn_vault: Path, statements: list[str]
) -> None:
    """(d) Every term is common in this corpus: the stage still answers, with a
    MATCH no wider than the budget."""
    rng = random.Random(13)
    turn = _long_turn(rng, 400)

    hits, state = working_set_runtime.lexical_evidence(
        long_turn_vault, turn, _rows(long_turn_vault), limit=8
    )

    assert state == "available"
    assert isinstance(hits, list)
    for terms in _ranking_match_terms(statements):
        assert len(terms) <= working_set_runtime.ACTIVATION_LEXICAL_MAX_TERMS


def test_a_turn_of_words_the_catalogue_never_saw_issues_no_match(
    long_turn_vault: Path, statements: list[str]
) -> None:
    """A word no page holds cannot rank or corroborate anything, so it never
    takes a place in the MATCH; a turn made only of them asks nothing."""
    hits, state = working_set_runtime.lexical_evidence(
        long_turn_vault, "vorblat quindex smarrow", _rows(long_turn_vault), limit=8
    )
    assert (hits, state) == ([], "available")
    assert _ranking_match_terms(statements) == []


def test_a_short_turn_ranks_exactly_as_before(long_turn_vault: Path) -> None:
    """Under the budget nothing is dropped: the bounded stage returns what the
    unbounded query returned, in the same order."""
    turn = "what happened to the kelvane throughput ceiling"
    rows = _rows(long_turn_vault)

    hits, state = working_set_runtime.lexical_evidence(long_turn_vault, turn, rows, limit=len(rows))

    assert state == "available"
    assert [hit.path for hit in hits] == _unbounded_paths(long_turn_vault, turn, rows)


def test_corroboration_is_counted_on_the_capped_candidate_window(
    long_turn_vault: Path,
) -> None:
    """The candidate cap bounds the rows the corroboration test reads: rows
    outside the top of the bm25 ranking are never examined."""
    rows = _rows(long_turn_vault)
    budget = lexstore.QueryTermBudget(
        max_units=working_set_runtime.ACTIVATION_LEXICAL_MAX_TERMS,
        common_fraction=1.0,
        common_min_pages=0,
        candidate_cap=1,
    )
    turn = "kelvane throughput zq7400 relay"
    wide = lexstore.search_bm25_result(
        long_turn_vault,
        turn,
        8,
        scope="kb",
        allowed_paths={row.path for row in rows},
        allow_delta=False,
        min_matched_terms=2,
    )
    capped = lexstore.search_bm25_result(
        long_turn_vault,
        turn,
        8,
        scope="kb",
        allowed_paths={row.path for row in rows},
        allow_delta=False,
        min_matched_terms=2,
        term_budget=budget,
    )
    assert {path for path, _ in wide.value} == {ANCHOR_PATH, MODEL_PATH}
    assert len(capped.value) == 1
    assert capped.value[0][0] == wide.value[0][0]
