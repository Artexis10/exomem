"""Lexical recall acceptance for tokenizer v2 (make-recall-multilingual).

English non-regression (task L5). Tokenizer v1 is frozen below and patched in
for the "before" arm, so both arms run the same product `find` path over the
same tree with embeddings off:

* the golden fixture alone ranks IDENTICALLY under v1 and v2: its pages are
  English with typographic punctuation, which v2 tokenizes byte for byte as
  v1 did;
* the golden fixture plus sixty German, Russian, Japanese and Estonian pages
  (`recall_multilingual`) moves no golden relevant page by more than one rank
  and mean NDCG@10 by at most 0.01. Only those pages' tokens change, and with
  them the corpus statistics every English score reads.

Multilingual acceptance (task L6), lexical arms, on the same padded tree:
same-language Japanese and Russian queries, and German and Estonian queries
typed without diacritics, find their page (`tests/golden/queries_multilingual
.yaml`). Cross-language, Estonian morphology and language-bias rows are the
dense lane's bars; they are measured here and reported, not asserted.
"""

from __future__ import annotations

import shutil
import sys
from functools import cache
from pathlib import Path

import pytest
import snowballstemmer
from epistemic.corpora import recall_multilingual

from exomem import bm25, lexstore
from exomem import eval_metrics as metrics
from exomem import find as find_module

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FIXTURE_VAULT = _REPO_ROOT / "tests" / "fixtures"
_GOLDEN = _REPO_ROOT / "tests" / "golden" / "queries.yaml"
if str(_REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))
import eval_retrieval  # noqa: E402

_LEGACY_STEMMER = snowballstemmer.stemmer("english")

#: Lexical-arm floors on recall@10 (design §17.6.2): the measured value minus
#: 0.08, never below the design bar (ja/ru same-language 0.80, de/et typed
#: without diacritics 0.90). Measured 1.00 in every cell on 2026-09-23, on both
#: lexical backends.
_FLOORS = {
    ("ja", "same_language"): 0.92,
    ("ru", "same_language"): 0.92,
    ("de", "without_diacritics"): 0.92,
    ("et", "without_diacritics"): 0.92,
}


@cache
def _legacy_stem(word: str) -> str:
    return _LEGACY_STEMMER.stemWord(word)


def _legacy_tokenize(text: str, *, query: bool = False) -> list[str]:
    """Tokenizer v1, frozen: lowercase, `[a-z0-9]+`, English Snowball."""
    return [_legacy_stem(word) for word in bm25._TOKEN_RE.findall(text.lower())]


def _legacy_units(text: str, *, query: bool = False) -> list[bm25.TokenUnit]:
    return [bm25.TokenUnit((stem,), False) for stem in _legacy_tokenize(text)]


def _use_legacy_tokenizer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch tokenizer v1 in at `bm25`'s entry points. Limitation: names bound
    at import elsewhere (referent attributes' `stem_word`/`word_forms`), excerpt
    anchoring (`first_stem_span`) and the v11 FTS declaration stay v2 in the v1
    arm; none of them moves an English page's rank."""
    monkeypatch.setattr(bm25, "tokenize", _legacy_tokenize)
    monkeypatch.setattr(bm25, "_tokenize", _legacy_tokenize)
    monkeypatch.setattr(bm25, "token_units", _legacy_units)
    monkeypatch.setattr(bm25, "stem_word", _legacy_stem)
    monkeypatch.setattr(bm25, "word_forms", lambda word: (_legacy_stem(word),))


def _reset_lexical_state() -> None:
    lexstore.reset_memo()
    lexstore.clear_stores()
    bm25.clear_cache()
    find_module.clear_cache()


def _tree(root: Path, *, padded: bool) -> Path:
    from conftest import initialize_vault_state_offline

    vault = root / ("padded" if padded else "golden")
    shutil.copytree(_FIXTURE_VAULT, vault)
    if padded:
        recall_multilingual.render(vault)
    initialize_vault_state_offline(vault, source="multilingual recall fixture")
    # The padded tree is past find's inline-build page cap: build the catalogue
    # here, with whichever tokenizer the arm has patched in.
    _reset_lexical_state()
    lexstore.ensure_fresh(vault)
    return vault


def _ranked(vault: Path, query: str, *, scope: str = "kb-only") -> list[str]:
    hits = find_module.find(
        vault,
        query=query,
        limit=10,
        mode="hybrid",
        scope=scope,
        widen_outside_kb=scope == "kb",
        rerank=False,
    )
    return [eval_retrieval._canon(hit.path) for hit in hits]


def _golden_run(vault: Path) -> dict[str, list[str]]:
    _reset_lexical_state()
    return {
        entry["query"]: _ranked(vault, entry["query"], scope=entry.get("scope", "kb-only"))
        for entry in eval_retrieval._load_golden(_GOLDEN)
    }


def _golden_ndcg(runs: dict[str, list[str]]) -> float:
    golden = {entry["query"]: entry for entry in eval_retrieval._load_golden(_GOLDEN)}
    return metrics.mean(
        metrics.ndcg_at_k(ranked, golden[query]["relevance"], 10) for query, ranked in runs.items()
    )


def _rank(ranked: list[str], path: str) -> int:
    return ranked.index(path) + 1 if path in ranked else len(ranked) + 1


@pytest.fixture(scope="module", params=["fts5", "python"])
def arms(request, tmp_path_factory):
    """Golden runs for v1 and v2 over the golden and the padded tree."""
    backend = request.param
    if backend == "fts5" and not lexstore.fts5_available():
        pytest.skip("this SQLite build lacks FTS5")
    monkeypatch = pytest.MonkeyPatch()
    root = tmp_path_factory.mktemp(f"multilingual-{backend}")
    try:
        monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", backend)
        monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
        runs: dict[tuple[str, bool], dict[str, list[str]]] = {}
        vaults: dict[tuple[str, bool], Path] = {}
        for tokenizer in ("v1", "v2"):
            with monkeypatch.context() as patch:
                if tokenizer == "v1":
                    _use_legacy_tokenizer(patch)
                for padded in (False, True):
                    vault = _tree(root / tokenizer, padded=padded)
                    patch.setenv("EXOMEM_VAULT_PATH", str(vault))
                    runs[tokenizer, padded] = _golden_run(vault)
                    vaults[tokenizer, padded] = vault
        yield backend, runs, vaults
    finally:
        _reset_lexical_state()
        monkeypatch.undo()


def test_the_golden_fixture_ranks_identically_under_v1_and_v2(arms):
    _backend, runs, _vaults = arms
    assert runs["v2", False] == runs["v1", False]


def test_multilingual_padding_moves_no_golden_page_by_more_than_one_rank(arms):
    backend, runs, _vaults = arms
    golden = {entry["query"]: entry for entry in eval_retrieval._load_golden(_GOLDEN)}
    moved = []
    for query, entry in golden.items():
        before, after = runs["v1", True][query], runs["v2", True][query]
        for path in entry["relevant"]:
            shift = abs(_rank(after, path) - _rank(before, path))
            if shift > 1:
                moved.append((query, path, _rank(before, path), _rank(after, path)))
    ndcg_before = _golden_ndcg(runs["v1", True])
    ndcg_after = _golden_ndcg(runs["v2", True])
    print(
        f"[{backend}] golden NDCG@10 lexical-only: unpadded {_golden_ndcg(runs['v2', False]):.4f}, "
        f"padded v1 {ndcg_before:.4f}, padded v2 {ndcg_after:.4f}"
    )
    assert moved == []
    assert abs(ndcg_after - ndcg_before) <= 0.01


def _multilingual_rows(vault: Path) -> list[dict]:
    # Per-test isolation resets the freshness registry the module-scoped build
    # was proven against; re-prove the catalogue before querying it.
    _reset_lexical_state()
    lexstore.ensure_fresh(vault)
    rows = []
    for row in recall_multilingual.load_queries():
        ranked = _ranked(vault, row["query"])
        gold = {recall_multilingual.resolve_key(key) for key in row["gold"]}
        poison = {recall_multilingual.resolve_key(key) for key in row.get("poison", ())}
        rows.append(
            {
                **row,
                "tokens": bm25.tokenize(row["query"], query=True),
                "recall10": metrics.recall_at_k(ranked, gold, 10),
                "mrr": metrics.mrr(ranked, gold),
                "gold_rank": min((_rank(ranked, path) for path in gold), default=11),
                "poison_rank": min((_rank(ranked, path) for path in poison), default=11),
            }
        )
    return rows


def _cells(rows: list[dict]) -> dict[tuple[str, str], dict[str, float]]:
    cells: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        cells.setdefault((row["language"], row["kind"]), []).append(row)
    return {
        cell: {
            "n": len(members),
            "recall10": metrics.mean(row["recall10"] for row in members),
            "mrr": metrics.mean(row["mrr"] for row in members),
        }
        for cell, members in sorted(cells.items())
    }


def test_multilingual_lexical_rows_clear_their_bars(arms):
    backend, _runs, vaults = arms
    rows = _multilingual_rows(vaults["v2", True])
    cells = _cells(rows)
    for (language, kind), cell in cells.items():
        print(
            f"[{backend}] {language} {kind}: n={cell['n']} recall@10={cell['recall10']:.2f} "
            f"MRR={cell['mrr']:.2f}"
        )
    for row in rows:
        assert row["tokens"], f"a {row['language']} query yielded no token: {row['query']}"
    failing = {
        cell: cells[cell]["recall10"]
        for cell, floor in _FLOORS.items()
        if cells[cell]["recall10"] < floor
    }
    assert failing == {}


def test_russian_inflections_and_yo_spelling_meet_their_page(arms):
    _backend, _runs, vaults = arms
    rows = [
        row
        for row in _multilingual_rows(vaults["v2", True])
        if row["language"] == "ru" and row["kind"] in {"morphology", "without_diacritics"}
    ]
    assert rows
    assert all(row["recall10"] == 1.0 for row in rows), [
        (row["query"], row["gold_rank"]) for row in rows
    ]


def test_tokenizer_v1_misses_what_v2_finds(arms):
    """The gain the bars measure: the same rows under tokenizer v1."""
    backend, _runs, vaults = arms
    with pytest.MonkeyPatch.context() as patch:
        _use_legacy_tokenizer(patch)
        before = _cells(_multilingual_rows(vaults["v1", True]))
    after = _cells(_multilingual_rows(vaults["v2", True]))
    for cell in _FLOORS:
        print(
            f"[{backend}] {cell[0]} {cell[1]}: recall@10 v1 {before[cell]['recall10']:.2f} "
            f"-> v2 {after[cell]['recall10']:.2f}"
        )
    assert all(before[cell]["recall10"] < after[cell]["recall10"] for cell in _FLOORS)


# ---------------------------------------------------------------- Japanese-only vault


#: Measured 2026-09-23 on both lexical backends over 23 queries: recall@10
#: 1.00, MRR 1.00 (a run is present on most of its content bigrams). Floors are
#: the measured value minus 0.08, never below the design bars (0.85 and 0.70).
_JAPANESE_RECALL_FLOOR = 0.92
_JAPANESE_MRR_FLOOR = 0.92


@pytest.fixture(scope="module")
def japanese_vault(tmp_path_factory):
    from conftest import initialize_vault_state_offline
    from epistemic.corpora import recall_japanese_vault

    vault = tmp_path_factory.mktemp("japanese") / "vault"
    key_to_path = recall_japanese_vault.build_corpus(vault)
    initialize_vault_state_offline(vault, source="japanese recall fixture")
    return vault, key_to_path


@pytest.mark.parametrize("backend", ["fts5", "python"])
def test_japanese_vault_lexical_recall_clears_its_bars(japanese_vault, monkeypatch, backend):
    from epistemic.corpora import recall_japanese_vault

    vault, key_to_path = japanese_vault
    if backend == "fts5" and not lexstore.fts5_available():
        pytest.skip("this SQLite build lacks FTS5")
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", backend)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    _reset_lexical_state()
    lexstore.ensure_fresh(vault)
    recalls, reciprocal_ranks = [], []
    for query in recall_japanese_vault.QUERIES:
        assert bm25.tokenize(query.query, query=True), query.query
        ranked = [hit.path for hit in find_module.find(
            vault, query=query.query, limit=10, mode="hybrid", rerank=False
        )]
        gold = {key_to_path[query.gold]}
        recalls.append(metrics.recall_at_k(ranked, gold, 10))
        reciprocal_ranks.append(metrics.mrr(ranked, gold))
    recall, mrr = metrics.mean(recalls), metrics.mean(reciprocal_ranks)
    print(f"[{backend}] Japanese vault lexical-only: recall@10 {recall:.2f}, MRR {mrr:.2f}")
    assert recall >= _JAPANESE_RECALL_FLOOR
    assert mrr >= _JAPANESE_MRR_FLOOR


def test_a_japanese_substring_finds_its_page_in_keyword_mode(japanese_vault, monkeypatch):
    vault, key_to_path = japanese_vault
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    _reset_lexical_state()
    lexstore.ensure_fresh(vault)
    hits = find_module.find(vault, query="弦楽四重奏", limit=10, mode="keyword")
    assert [hit.path for hit in hits] == [key_to_path["concert"]]


@pytest.mark.parametrize("backend", ["fts5", "python"])
def test_japanese_x_no_y_questions_find_their_page_and_particles_find_nothing(
    japanese_vault, monkeypatch, backend
):
    """A run is present when most of its content bigrams are: "抹茶の用意" and a
    whole question find the tea-ceremony note, and the design's own example
    finds "議事録は翌日までに共有します". A query made only of particles has no
    content and still finds nothing."""
    vault, key_to_path = japanese_vault
    if backend == "fts5" and not lexstore.fts5_available():
        pytest.skip("this SQLite build lacks FTS5")
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", backend)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    _reset_lexical_state()
    lexstore.ensure_fresh(vault)
    rows = {
        "抹茶の用意": "tea-ceremony",
        "茶道教室で抹茶を用意するのは誰ですか": "tea-ceremony",
        "会議の議事録はいつ共有": "minutes",
    }
    for query, gold in rows.items():
        ranked = [hit.path for hit in find_module.find(
            vault, query=query, limit=10, mode="hybrid", rerank=False
        )]
        assert ranked[:1] == [key_to_path[gold]], (query, ranked[:3])
    from epistemic.corpora import recall_japanese_vault

    for query in recall_japanese_vault.PARTICLE_QUERIES:
        assert find_module.find(vault, query=query, limit=10, mode="hybrid", rerank=False) == [], query
