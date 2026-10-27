"""A restricted caller's admitted ranking, read from kept word postings, equals the full ranking.

The full ranking is Okapi BM25 over the admitted pages' own text, as `rank_bm25.BM25Okapi` scores
it: every admitted page that holds a query word is scored and sorted by score, then path. The
admitted ranking must return the same paths, scores, order and tie-breaks, whether the postings it
reads were counted for this query or kept from an earlier one, and a withheld page must change none
of it.
"""

from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path

import pytest
from rank_bm25 import BM25Okapi

from exomem import bm25, lexstore
from exomem import find as find_module

pytestmark = pytest.mark.skipif(not lexstore.fts5_available(), reason="this SQLite build lacks FTS5")

KB = "Knowledge Base"
#: Words every page holds, words about half the pages hold, and words a page or two hold.
COMMON = ("harbor", "lantern", "river")
MIDDLE = ("copper", "meadow", "signal", "timber", "velvet")
RARE = ("obsidian", "quorum", "zephyr")


@pytest.fixture(autouse=True)
def _fresh_lexical_state():
    lexstore.reset_memo()
    lexstore.clear_stores()
    find_module.clear_cache()
    yield
    lexstore.reset_memo()
    lexstore.clear_stores()


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntype: insight\ntitle: {path.stem}\n---\n# {path.stem}\n\n{body}\n", encoding="utf-8")


def _random_vault(root: Path, seed: int, pages: int) -> list[str]:
    """Pages of seeded random text. Every fifth page repeats the one before it, so ties are frequent."""
    draw = random.Random(seed)
    paths, body = [], ""
    for index in range(pages):
        rel = f"{KB}/Notes/page-{index:03d}.md"
        if index % 5 != 4:
            words = [draw.choice(COMMON) for _ in range(draw.randint(1, 4))]
            words += list(COMMON)  # every page holds every common word
            words += [word for word in MIDDLE if draw.random() < 0.5 for _ in range(draw.randint(1, 3))]
            words += [draw.choice(RARE)] if draw.random() < 0.08 else []
            words += ["filler"] * draw.randint(0, 30)
            draw.shuffle(words)
            body = " ".join(words)
        _write(root, rel, body)
        paths.append(rel)
    lexstore.ensure_fresh(root)
    return paths


def _full_ranking(root: Path, admitted: set[str], query: str, k: int, min_matched_terms: int) -> list[tuple[str, float]]:
    """Okapi over the admitted pages' stored text, scored by `rank_bm25`, independent of the code under test."""
    connection = sqlite3.connect(lexstore.lexical_path(root))
    try:
        text = dict(connection.execute(
            "SELECT p.path, fts.stemmed FROM fts JOIN pages p ON p.rowid = fts.rowid WHERE p.in_kb = 1"
        ))
    finally:
        connection.close()
    corpus = {path: text[path].split() for path in sorted(admitted) if path in text}
    tokens = [stem for unit in bm25.token_units(query, query=True) for stem in unit.stems]
    if not corpus:
        return []
    scores = BM25Okapi(list(corpus.values())).get_scores(tokens)
    ranked = [
        (path, float(score))
        for (path, document), score in zip(corpus.items(), scores, strict=True)
        if len(set(tokens).intersection(document)) >= min_matched_terms
    ]
    return sorted(ranked, key=lambda row: (-row[1], row[0]))[:k]


def _admitted_ranking(root: Path, admitted: set[str], query: str, k: int, min_matched_terms: int) -> list[tuple[str, float]]:
    result = lexstore.search_bm25_result(
        root, query, k, allow_delta=False, admitted_paths=admitted, min_matched_terms=min_matched_terms,
    )
    assert result.readiness.complete
    return result.value


#: Query kinds: every word on every page, only rare words, a mix, two words required, and a word no page holds.
QUERIES = (
    ("all common", "harbor lantern river", 1),
    ("rare", "obsidian zephyr", 1),
    ("mixed", "harbor copper quorum", 1),
    ("two words required", "lantern meadow velvet", 2),
    ("no page holds it", "nonesuch harbor", 1),
)


def test_kept_postings_rank_exactly_as_the_full_ranking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Seeded random vaults and admissions: every pruned ranking, cold and from kept postings, equals the full one."""
    documents_read = []
    documents = bm25.CorpusPostings.documents
    monkeypatch.setattr(bm25.CorpusPostings, "documents", lambda self, *a: documents_read.append(1) or documents(self, *a))
    compared = 0
    for seed in range(4):
        root = tmp_path / f"vault-{seed}"
        paths = _random_vault(root, seed, pages=60)
        draw = random.Random(seed)
        admissions = {
            "everything": set(paths),
            "random half": set(draw.sample(paths, len(paths) // 2)),
            "withheld-heavy": set(draw.sample(paths, 6)),
            "empty": set(),
        }
        for admission, admitted in admissions.items():
            for kind, query, required in QUERIES:
                expected = _full_ranking(root, admitted, query, 10, required)
                for warmth in ("counted", "kept"):
                    assert _admitted_ranking(root, admitted, query, 10, required) == expected, (
                        seed, admission, kind, warmth,
                    )
                    compared += 1
    assert compared == 4 * 4 * len(QUERIES) * 2
    assert documents_read  # the control: rankings were read from postings, not from each page's text


def test_a_withheld_page_moves_no_score_of_a_kept_postings_ranking(tmp_path: Path) -> None:
    """The twin vault adds pages that hold every query word but are not admitted: the ranking is unchanged."""
    vault, twin = tmp_path / "vault", tmp_path / "twin"
    paths = _random_vault(vault, 7, pages=40)
    _random_vault(twin, 7, pages=40)
    for index in range(20):
        _write(vault, f"{KB}/Private/withheld-{index:02d}.md", "harbor lantern river obsidian " * 5)
    lexstore.ensure_fresh(vault)
    admitted = set(paths[::2])
    for _kind, query, required in QUERIES:
        assert _admitted_ranking(vault, admitted, query, 10, required) == _admitted_ranking(twin, admitted, query, 10, required)


def test_an_edited_page_is_ranked_from_its_new_text_after_postings_were_kept(tmp_path: Path) -> None:
    """Postings kept before an edit never score the page after it: a word that no page held is kept with no
    pages, and the page that the edit gives it must still rank on it."""
    paths = _random_vault(tmp_path, 3, pages=30)
    admitted = set(paths)
    query = "quillon harbor"
    assert _admitted_ranking(tmp_path, admitted, query, 5, 1) == _full_ranking(tmp_path, admitted, query, 5, 1)
    _write(tmp_path, paths[11], "quillon harbor lantern river")
    lexstore.ensure_fresh(tmp_path)
    after = _admitted_ranking(tmp_path, admitted, query, 5, 1)
    assert after == _full_ranking(tmp_path, admitted, query, 5, 1)
    assert after[0][0] == paths[11], json.dumps(after)
