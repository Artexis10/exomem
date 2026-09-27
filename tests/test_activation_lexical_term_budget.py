"""The lexical stage of activation is bounded on a long turn (roadmap U4).

`lexical_evidence` used to hand the WHOLE turn's content words to one FTS5
`MATCH`: a four-hundred-word turn became a four-hundred-term OR, every page
holding any everyday word was read and scored, and the corroboration test ran
over every one of them. The stage now keeps at most
`ACTIVATION_LEXICAL_MAX_TERMS` query units, chosen by how rare each is in the
knowledge base, carrying at most `ACTIVATION_LEXICAL_MAX_STEMS` stems; ranking
and corroboration then read every row those units match.

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


#: Unspaced runs for a long Japanese turn: sentences whose particles are
#: hiragana, then kanji compounds whose every bigram carries content.
_JAPANESE_RUNS = (
    "今日は朝から雨が降っていたので図書館で本を読みました",
    "週末は友達と一緒に山登りに行く予定を立てています",
    "駅前の喫茶店で珈琲を飲みながら手紙を書きました",
    "図書館利用案内掲示板更新作業",
    "市民体育館夏季営業時間変更通知",
    "地域交通安全週間実施計画概要",
    "公園緑地維持管理年間予定表",
    "駅前商店街歳末大売出期間",
    "小学校運動会準備委員会名簿",
    "市立病院外来診療受付時間",
    "県道拡幅工事区間通行規制",
    "上下水道料金改定説明会場",
    "児童館春休特別企画一覧",
    "美術館常設展示作品目録",
    "河川敷清掃活動参加者募集",
)
_HIRAGANA = re.compile("[぀-ゟ]")


def test_a_long_unspaced_turn_sends_only_content_bigrams_within_the_cap(
    vault: Path, statements: list[str]
) -> None:
    """A long turn of long unspaced runs: the MATCH carries only the runs'
    content bigrams (no particle bigram), no more stems in all than the
    dense side reads, and the page the turn names is still reached."""
    from exomem import embeddings

    kb = vault / "Knowledge Base" / "Notes"
    for index in range(120):
        run = _JAPANESE_RUNS[index % len(_JAPANESE_RUNS)]
        _write(kb / f"日記-{index:03d}.md", _page(f"日記 {index:03d}", run + "。"))
    anchor = "Knowledge Base/Projects/議事録.md"
    _write(vault / anchor, _page("議事録", "議事録は翌日までに共有します。"))
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    rows = [SimpleNamespace(path=anchor, title="議事録")] + [
        SimpleNamespace(path=f"Knowledge Base/Notes/日記-{index:03d}.md", title="日記")
        for index in range(0, 120, 7)
    ]
    runs = list(_JAPANESE_RUNS)
    turn = "。".join(runs[:8]) + "。議事録 共有。" + "。".join(runs[8:]) + "。"

    hits, state = working_set_runtime.lexical_evidence(vault, turn, rows, limit=8)

    assert state == "available"
    assert anchor in [hit.path for hit in hits], [hit.path for hit in hits]
    matches = _ranking_match_terms(statements)
    assert len(matches) == 1, matches
    assert {"議事", "事録", "共有"} <= set(matches[0]), matches[0]
    assert len(matches[0]) <= embeddings.ACTIVATION_TURN_MAX_TOKENS, len(matches[0])
    assert not [term for term in matches[0] if _HIRAGANA.search(term)], matches[0]
    assert working_set_runtime.ACTIVATION_LEXICAL_MAX_STEMS == embeddings.ACTIVATION_TURN_MAX_TOKENS


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


ZORVIK_PATH = "Knowledge Base/Projects/zorvik-survey.md"
BENCH_PATH = "Knowledge Base/Projects/qa7700-bench.md"


def _two_named_pages(vault: Path) -> list[SimpleNamespace]:
    """Add a page for each rare name, each also holding the everyday word
    `garden`; return the anchor rows with both pages in them."""
    _write(vault / ZORVIK_PATH, _page("Zorvik Survey", "The zorvik survey covered the garden."))
    _write(vault / BENCH_PATH, _page("Bench", "The qa7700 bench sat in the garden."))
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    frequencies = lexstore.term_document_frequencies(vault, ["garden"], scope="kb")
    counts, pages = frequencies.value
    assert pages >= working_set_runtime.ACTIVATION_LEXICAL_COMMON_MIN_PAGES
    assert counts["garden"] > working_set_runtime.ACTIVATION_LEXICAL_COMMON_FRACTION * pages
    return _rows(vault) + [
        SimpleNamespace(path=ZORVIK_PATH, title="zorvik-survey"),
        SimpleNamespace(path=BENCH_PATH, title="qa7700-bench"),
    ]


def test_a_short_turn_with_a_common_word_ranks_exactly_as_before(long_turn_vault: Path) -> None:
    """Two rare names on different pages plus one everyday word: each page
    holds one name and the everyday word, so only the everyday word lets
    either corroborate. Under the budget it is not dropped for being common,
    and both pages come back exactly as the unbounded query returns them."""
    zorvik, model = ZORVIK_PATH, BENCH_PATH
    rows = _two_named_pages(long_turn_vault)
    turn = "zorvik qa7700 garden"

    unbounded = _unbounded_paths(long_turn_vault, turn, rows)
    hits, state = working_set_runtime.lexical_evidence(long_turn_vault, turn, rows, limit=len(rows))

    assert set(unbounded) == {zorvik, model}, unbounded
    assert state == "available"
    assert [hit.path for hit in hits] == unbounded


def test_a_medium_turn_still_corroborates_on_its_dropped_everyday_words(
    long_turn_vault: Path, statements: list[str]
) -> None:
    """The same two names and `garden`, padded with ten more everyday words
    to thirteen units: over the budget, so the everyday words leave the
    MATCH, but they still count toward corroboration and both named pages
    come back as the unbounded query returns them.

    The unbounded query also returns filler pages holding two or more of the
    everyday words and neither name. The bounded MATCH asks only for the
    names, so it never reads those rows: that is the bound, and it is the
    whole difference between the two answers."""
    rows = _two_named_pages(long_turn_vault)
    padding = [word for word in COMMON_WORDS if word != "garden"][:10]
    turn = "zorvik qa7700 garden " + " ".join(padding)
    unbounded = _unbounded_paths(long_turn_vault, turn, rows)
    statements.clear()

    hits, state = working_set_runtime.lexical_evidence(long_turn_vault, turn, rows, limit=len(rows))

    assert state == "available"
    matches = _ranking_match_terms(statements)
    assert len(matches) == 1 and set(matches[0]) == {"zorvik", "qa7700"}, matches
    bounded = [hit.path for hit in hits]
    assert set(bounded) == {ZORVIK_PATH, BENCH_PATH}, bounded
    assert [path for path in unbounded if path in set(bounded)] == bounded
    for path in set(unbounded) - set(bounded):
        words = set(re.findall(r"[a-z0-9]+", (long_turn_vault / path).read_text().lower()))
        assert not words & {"zorvik", "qa7700"}, path


def test_the_bounded_query_corroborates_in_rank_order_and_stops_at_k(
    long_turn_vault: Path, statements: list[str]
) -> None:
    """A short turn of twelve everyday words matches almost every page. The
    ranked rows must reach the corroboration test already in bm25 order, so
    it can stop once `k` rows qualify. A flattened plan runs the test on
    every matched row and sorts afterwards: 86-105 ms against 15-17 ms for
    such a turn on the 2000-page latency harness."""
    turn = " ".join(COMMON_WORDS[40:52])

    working_set_runtime.lexical_evidence(long_turn_vault, turn, _rows(long_turn_vault), limit=8)

    bounded = [sql for sql in statements if ") AS c JOIN fts" in sql]
    assert len(bounded) == 1, statements
    conn = lexstore.get_store(long_turn_vault)._connect()
    try:
        plan = conn.execute("EXPLAIN QUERY PLAN " + bounded[0]).fetchall()
    finally:
        conn.close()
    top = [detail for _id, parent, _unused, detail in plan if parent == 0]
    assert any(detail.startswith("CO-ROUTINE") for detail in top), plan
    assert "USE TEMP B-TREE FOR ORDER BY" not in top, plan


def test_a_corroborated_page_below_many_single_unit_pages_is_still_found(vault: Path) -> None:
    """More pages than any fixed window outrank the one page that holds both
    words, each on a single word: the bounded stage still finds it, exactly as
    the unbounded query does. Ranking and corroboration read the same rows."""
    decoys = 150
    notes = vault / "Knowledge Base" / "Notes" / "Decoys"
    for index in range(decoys):
        _write(notes / f"brastle-{index:03d}.md", _page(f"Decoy {index:03d}", "brastle " * 4))
        _write(notes / f"quorran-{index:03d}.md", _page(f"Decoy {index:03d}", "quorran " * 4))
    anchor = "Knowledge Base/Projects/brastle-quorran.md"
    _write(
        vault / anchor,
        _page("Joint Record", "brastle quorran " + " ".join(COMMON_WORDS * 4)),
    )
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    rows = [SimpleNamespace(path=anchor, title="brastle-quorran")] + [
        SimpleNamespace(path=f"Knowledge Base/Notes/Decoys/{word}-{index:03d}.md", title=word)
        for word in ("brastle", "quorran")
        for index in range(decoys)
    ]
    turn = "brastle quorran"
    ranked = lexstore.search_bm25_result(
        vault,
        turn,
        len(rows),
        scope="kb",
        allowed_paths={row.path for row in rows},
        allow_delta=False,
    )
    # Every single-word page outranks the page holding both words.
    assert [path for path, _ in ranked.value][-1] == anchor

    hits, state = working_set_runtime.lexical_evidence(vault, turn, rows, limit=len(rows))

    assert state == "available"
    assert [hit.path for hit in hits] == _unbounded_paths(vault, turn, rows) == [anchor]


def test_activation_timings_report_the_selection_as_counts_only(vault: Path) -> None:
    """The kept and dropped unit counts reach the activation diagnostics, and
    nothing of the turn's own text does."""
    from test_working_set_index import _seed_planning, _seed_structure

    from exomem import commands, working_set_index

    _seed_structure(vault)
    _seed_planning(vault)
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    turn = "I'm planning to tow the Cargo Sled north past the quorvint ridge"

    packet = commands.op_activate_context(vault, turn=turn, include_timings=True)

    reported = packet["timings"]["profile"]["working_set.lexical"]
    assert set(reported) == {"terms_kept", "terms_dropped"}
    assert all(isinstance(value, int) for value in reported.values())
    assert reported["terms_kept"] + reported["terms_dropped"] > 0
    assert "quorvint" not in repr(packet["timings"])


def test_a_word_held_only_outside_the_knowledge_base_takes_no_place(
    long_turn_vault: Path, statements: list[str]
) -> None:
    """The stage searches the knowledge base, so rarity is measured there: a
    word only pages outside it hold can match nothing the stage returns and
    never takes one of the budget's places."""
    for index in range(3):
        _write(
            long_turn_vault / "Reference" / f"plovendar-{index}.md",
            _page(f"Outside {index}", "The plovendar log lives outside the knowledge base."),
        )
    lexstore.ensure_fresh(long_turn_vault)
    working_set_runtime.reset_caches_for_tests()
    rng = random.Random(15)
    turn = _long_turn(rng, 400, "kelvane", "throughput", "plovendar")

    hits, state = working_set_runtime.lexical_evidence(
        long_turn_vault, turn, _rows(long_turn_vault), limit=8
    )

    assert state == "available"
    assert hits and hits[0].path == ANCHOR_PATH, [hit.path for hit in hits]
    matches = _ranking_match_terms(statements)
    assert len(matches) == 1, matches
    assert {"kelvan", "throughput"} <= set(matches[0]), matches[0]
    assert "plovendar" not in matches[0], matches[0]


@pytest.mark.parametrize("replacement", ["atomic_publish", "in_place_rebuild"])
def test_replacing_the_live_catalogue_forgets_its_term_frequencies(
    long_turn_vault: Path, replacement: str
) -> None:
    """A rebuilt catalogue is a new corpus: frequencies read from the one it
    replaced are not kept, whatever its stored checkpoints say."""
    rng = random.Random(16)
    turn = _long_turn(rng, 200, "kelvane", "throughput")
    working_set_runtime.lexical_evidence(long_turn_vault, turn, _rows(long_turn_vault), limit=8)
    store = lexstore.get_store(long_turn_vault)
    assert store._term_frequency_cache is not None

    if replacement == "atomic_publish":
        assert store.rebuild_atomic() is True
    else:
        with store._publication_lock():
            conn = store._connect_setup()
            try:
                store._rebuild(conn)
            finally:
                conn.close()

    assert store._term_frequency_cache is None

