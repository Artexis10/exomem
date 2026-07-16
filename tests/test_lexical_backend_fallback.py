"""Lexical backend ladder through the REAL call sites — bm25.search() and the
find() keyword lane — plus the exact-parity suite for the keyword contract.

LEAN-SAFE: no extras, no models. The vector/CLIP lanes import-fail silently on
lean installs (that is their design), leaving bm25/keyword/graph — precisely the
lanes this backend serves.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from exomem import bm25, lexstore
from exomem import find as find_module


def _write_page(
    root: Path,
    rel: str,
    body: str,
    *,
    title: str | None = None,
    updated: str = "2026-01-01",
) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    t = title or Path(rel).stem
    p.write_text(
        f"---\ntype: insight\ntitle: {t}\nupdated: {updated}\n---\n# {t}\n\n{body}\n",
        encoding="utf-8",
    )
    return p


def _fill_corpus(root: Path, n: int = 8) -> None:
    """Filler pages with disjoint vocabulary. rank_bm25's IDF is zero at
    df == N/2 and negative above (those docs then drop as zero-score), so
    assertions about the python rung need query terms rare in the corpus —
    exactly the regime real vaults are in."""
    fillers = [
        "granite cliffs weather slowly",
        "sourdough hydration ratios",
        "violin bow rosin technique",
        "tidepool anemone feeding",
        "letterpress ink viscosity",
        "orbital mechanics refresher",
        "mushroom spore prints",
        "marathon taper schedule",
    ]
    for i in range(n):
        _write_page(root, f"Knowledge Base/filler-{i}.md", fillers[i % len(fillers)])


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch):
    lexstore.reset_memo()
    lexstore.clear_stores()
    bm25.clear_cache()
    find_module.clear_cache()
    find_module.reset_degradation_counts()
    monkeypatch.delenv("EXOMEM_LEXICAL_BACKEND", raising=False)
    yield
    lexstore.reset_memo()
    lexstore.clear_stores()
    bm25.clear_cache()
    find_module.clear_cache()
    find_module.reset_degradation_counts()


_HAS_FTS5 = lexstore.fts5_available()
needs_fts5 = pytest.mark.skipif(not _HAS_FTS5, reason="SQLite build lacks FTS5")


# ---------------------------------------------------------------- bm25 ladder


@needs_fts5
def test_fts5_serves_bm25_lane_with_unchanged_interface(tmp_path):
    """Under `auto` the FTS5 rung answers bm25.search() with the same return
    contract: top-k (rel_path, positive_score), sidecar materialized."""
    _write_page(tmp_path, "Knowledge Base/target.md", "kubernetes ingress configuration")
    _write_page(tmp_path, "Knowledge Base/other.md", "gardening tips for spring")
    hits = bm25.search(tmp_path, "kubernetes ingress", k=5, scope="kb")
    assert hits and hits[0][0] == "Knowledge Base/target.md"
    for p, s in hits:
        assert isinstance(p, str) and isinstance(s, float) and s > 0
    assert lexstore.lexical_path(tmp_path).exists()  # served by the sidecar


@needs_fts5
def test_kill_switch_forces_python_rung(tmp_path, monkeypatch):
    """EXOMEM_LEXICAL_BACKEND=python restores today's behavior wholesale —
    results identical to the historical path, and no sidecar is created."""
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    _write_page(tmp_path, "Knowledge Base/a.md", "postgres index tuning")
    _write_page(tmp_path, "Knowledge Base/b.md", "postgres vacuum settings")
    _fill_corpus(tmp_path)
    hits = bm25.search(tmp_path, "postgres index", k=5, scope="kb")
    assert hits and hits[0][0] == "Knowledge Base/a.md"
    assert not lexstore.lexical_path(tmp_path).exists()


def test_unavailable_fts5_serves_python_rung_without_degradation(tmp_path, monkeypatch):
    """Forced FTS5-unavailable (the custom-build shape): bm25.search answers
    from rank-bm25, find() records NO lane degradation for the fallback, and
    the keyword lane still works. Runs on every install."""
    def _boom(conn):
        raise sqlite3.OperationalError("no such module: fts5")

    monkeypatch.setattr(lexstore, "_probe_fts5", _boom)
    lexstore.reset_memo()
    _write_page(tmp_path, "Knowledge Base/doc.md", "terraform state locking")
    _fill_corpus(tmp_path)

    hits = bm25.search(tmp_path, "terraform locking", k=5, scope="kb")
    assert hits and hits[0][0] == "Knowledge Base/doc.md"

    before = dict(find_module.degradation_counts())
    degraded: list[str] = []
    failed: list[str] = []
    out = find_module.find(
        tmp_path, query="terraform locking", mode="hybrid", limit=5,
        degraded_out=degraded, failed_out=failed,
    )
    assert out and out[0].path == "Knowledge Base/doc.md"
    assert find_module.degradation_counts() == before  # fallback is silent
    assert "bm25" not in failed and "keyword" not in failed
    assert not lexstore.lexical_path(tmp_path).exists()


@needs_fts5
def test_hybrid_find_end_to_end_under_fts5(tmp_path):
    """The lane feeds RRF exactly as before: hybrid hits carry bm25_rank, the
    bm25/keyword stages time cleanly (no error key), nothing degrades."""
    _write_page(tmp_path, "Knowledge Base/hit.md", "distributed tracing spans")
    _write_page(tmp_path, "Knowledge Base/miss.md", "sourdough starter feeding")
    t = find_module.FindTimings()
    degraded: list[str] = []
    failed: list[str] = []
    out = find_module.find(
        tmp_path, query="distributed tracing", mode="hybrid", limit=5,
        timings=t, degraded_out=degraded, failed_out=failed,
    )
    assert out and out[0].path == "Knowledge Base/hit.md"
    assert out[0].bm25_rank == 1
    stages = t.as_dict()["stages"]
    assert "ms" in stages["bm25"] and "error" not in stages["bm25"]
    assert "ms" in stages["keyword"] and "error" not in stages["keyword"]
    assert failed == []


@needs_fts5
def test_stemming_pin_holds_under_both_backends(tmp_path, monkeypatch):
    """The morphological-variant pin: 'regulation' must rank the page that says
    'regulator' first under BOTH backends — byte-identical pre-stemming."""
    _write_page(tmp_path, "Knowledge Base/reg.md", "the regulator issued a decision")
    _write_page(tmp_path, "Knowledge Base/noise.md", "issued tickets for parking")
    _fill_corpus(tmp_path)

    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    py = bm25.search(tmp_path, "regulation decision", k=5, scope="kb")
    bm25.clear_cache()
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "fts5")
    ft = bm25.search(tmp_path, "regulation decision", k=5, scope="kb")

    assert py and py[0][0] == "Knowledge Base/reg.md"
    assert ft and ft[0][0] == "Knowledge Base/reg.md"


@needs_fts5
def test_bm25_match_sets_agree_between_backends(tmp_path, monkeypatch):
    """Scoring differs (FTS5 bm25() vs BM25Okapi — floors-gated, not
    rank-identical) but with query terms rare in the corpus (df << N, the
    real-vault regime) the MATCH SET at k >= corpus is identical: both
    return exactly the docs containing at least one query term."""
    _write_page(tmp_path, "Knowledge Base/a.md", "alpha beta gamma")
    _write_page(tmp_path, "Knowledge Base/b.md", "beta delta")
    _write_page(tmp_path, "Knowledge Base/c.md", "epsilon zeta")
    _write_page(tmp_path, "Knowledge Base/d.md", "alpha alpha alpha")
    _fill_corpus(tmp_path)

    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    py = {p for p, _ in bm25.search(tmp_path, "alpha beta", k=50, scope="kb")}
    bm25.clear_cache()
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "fts5")
    ft = {p for p, _ in bm25.search(tmp_path, "alpha beta", k=50, scope="kb")}
    assert ft == py == {"Knowledge Base/a.md", "Knowledge Base/b.md", "Knowledge Base/d.md"}


@pytest.mark.parametrize("backend", ["python", "fts5"])
def test_bm25_allowed_paths_are_applied_before_top_k(
    tmp_path, monkeypatch, backend
):
    """An eligible lower-ranked page must not be buried by excluded hits."""
    if backend == "fts5" and not _HAS_FTS5:
        pytest.skip("SQLite build lacks FTS5")
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", backend)
    excluded = "Knowledge Base/excluded.md"
    allowed = "Knowledge Base/allowed.md"
    _write_page(tmp_path, excluded, "needle " * 20)
    _write_page(
        tmp_path,
        allowed,
        "needle with a deliberately longer body of unrelated lexical terms",
    )
    _fill_corpus(tmp_path)

    unfiltered = bm25.search(tmp_path, "needle", k=1, scope="kb")
    assert unfiltered and unfiltered[0][0] == excluded

    filtered = bm25.search(
        tmp_path,
        "needle",
        k=1,
        scope="kb",
        allowed_paths={allowed},
    )
    assert [path for path, _score in filtered] == [allowed]


# ---------------------------------------------------------------- keyword parity


def _parity_corpus(root: Path) -> None:
    """Pages engineered to stress every clause of the substring contract."""
    _write_page(root, "Knowledge Base/plain.md", "employment contract terms", updated="2026-03-01")
    _write_page(root, "Knowledge Base/midword.md", "the xylophones sang loudly", updated="2026-02-01")
    _write_page(root, "Knowledge Base/title-only.md", "unrelated body", title="Budget Overview", updated="2026-04-01")
    _write_page(root, "Knowledge Base/short.md", "xq marks the spot", updated="2026-01-15")
    _write_page(root, "Knowledge Base/meta.md", "growth was 42% in snake_case", updated="2026-01-10")
    _write_page(root, "Knowledge Base/uni.md", "tere tulemast Tallinnasse sõbrad", updated="2026-01-05")
    _write_page(root, "Knowledge Base/sub/nested.md", "employment law contract precedent", updated="2026-05-01")
    _write_page(root, "Knowledge Base/index.md", "employment xylophones budget xq", updated="2026-06-01")
    _write_page(root, "Knowledge Base/punct.md", "+++ ~~~ !!!", updated="2026-01-02")
    _write_page(root, "Knowledge Base/same-date-b.md", "twin content marker", updated="2026-02-02")
    _write_page(root, "Knowledge Base/same-date-a.md", "twin content marker", updated="2026-02-02")


_PARITY_QUERIES = [
    "contract employment",     # multi-token, order-free
    "ylophon",                 # mid-word
    "budget",                  # title-only match
    "xq",                      # 2-char needle
    "q",                       # 1-char needle
    "42%",                     # LIKE metachar %
    "e_c",                     # LIKE metachar _
    "sõbra",                   # non-ASCII
    "tallinnasse",             # case-folding
    "~~~",                     # punctuation-only page
    "twin marker",             # tie on updated → path tie-break
    "xq spot",                 # short + indexable token mix
    "employment",              # multiple matches, ordering
    "zzz-no-such-token",       # empty result
    "",                        # empty query → empty lane
]


@needs_fts5
@pytest.mark.parametrize("query", _PARITY_QUERIES)
def test_keyword_lane_parity_with_reference_scan(tmp_path, monkeypatch, query):
    """THE keyword gate: for every query shape, the FTS5/trigram-served lane
    returns the IDENTICAL ordered list the reference scan produces."""
    _parity_corpus(tmp_path)
    query_norm = query.lower().strip()

    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    reference = find_module._keyword_match_paths(tmp_path, query_norm, "kb")
    find_module.clear_cache()
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "fts5")
    indexed = find_module._keyword_match_paths(tmp_path, query_norm, "kb")

    assert indexed == reference


@needs_fts5
def test_keyword_parity_vault_scope(tmp_path, monkeypatch):
    _parity_corpus(tmp_path)
    _write_page(tmp_path, "Projects/outside.md", "employment beyond the kb", updated="2026-07-01")
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    reference = find_module._keyword_match_paths(tmp_path, "employment", "vault")
    find_module.clear_cache()
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "fts5")
    indexed = find_module._keyword_match_paths(tmp_path, "employment", "vault")
    assert indexed == reference
    assert "Projects/outside.md" in indexed


@needs_fts5
def test_keyword_parity_after_edit_and_delete(tmp_path, monkeypatch):
    """Parity holds across a write/delete cycle driven through the hooks —
    freshness, not just cold builds."""
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "fts5")
    _parity_corpus(tmp_path)
    assert find_module._keyword_match_paths(tmp_path, "contract", "kb")

    p = _write_page(tmp_path, "Knowledge Base/late.md", "contract addendum", updated="2026-09-09")
    lexstore.upsert_after_write(tmp_path, [p])
    (tmp_path / "Knowledge Base/plain.md").unlink()
    lexstore.delete_after_remove(tmp_path, ["Knowledge Base/plain.md"])
    find_module.clear_cache()

    indexed = find_module._keyword_match_paths(tmp_path, "contract", "kb")
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    find_module.clear_cache()
    reference = find_module._keyword_match_paths(tmp_path, "contract", "kb")
    assert indexed == reference
    assert "Knowledge Base/late.md" in indexed
    assert "Knowledge Base/plain.md" not in indexed
