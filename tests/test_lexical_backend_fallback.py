"""Lexical backend ladder through the REAL call sites — bm25.search() and the
find() keyword lane — plus the exact-parity suite for the keyword contract.

LEAN-SAFE: no extras, no models. The vector/CLIP lanes import-fail silently on
lean installs (that is their design), leaving bm25/keyword/graph — precisely the
lanes this backend serves.
"""

from __future__ import annotations

import sqlite3
import threading
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


def test_unavailable_fts5_keeps_primary_python_rung_but_marks_widening_unavailable(
    tmp_path, monkeypatch
):
    """Forced FTS5-unavailable (the custom-build shape): bm25.search answers
    from rank-bm25 and the keyword lane still works. The optional outside-KB
    lane reports its maintained index as unavailable instead of launching a
    second whole-vault Python build. Runs on every install."""

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
        tmp_path,
        query="terraform locking",
        mode="hybrid",
        limit=5,
        widen_outside_kb=True,
        degraded_out=degraded,
        failed_out=failed,
    )
    assert out and out[0].path == "Knowledge Base/doc.md"
    after = find_module.degradation_counts()
    assert after.get("outside_kb_lexical", 0) == before.get("outside_kb_lexical", 0) + 1
    assert failed == ["outside_kb_lexical"]
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
        tmp_path,
        query="distributed tracing",
        mode="hybrid",
        limit=5,
        timings=t,
        degraded_out=degraded,
        failed_out=failed,
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
def test_bm25_allowed_paths_are_applied_before_top_k(tmp_path, monkeypatch, backend):
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
    _write_page(
        root, "Knowledge Base/midword.md", "the xylophones sang loudly", updated="2026-02-01"
    )
    _write_page(
        root,
        "Knowledge Base/title-only.md",
        "unrelated body",
        title="Budget Overview",
        updated="2026-04-01",
    )
    _write_page(root, "Knowledge Base/short.md", "xq marks the spot", updated="2026-01-15")
    _write_page(
        root, "Knowledge Base/meta.md", "growth was 42% in snake_case", updated="2026-01-10"
    )
    _write_page(
        root, "Knowledge Base/uni.md", "tere tulemast Tallinnasse sõbrad", updated="2026-01-05"
    )
    _write_page(
        root,
        "Knowledge Base/sub/nested.md",
        "employment law contract precedent",
        updated="2026-05-01",
    )
    _write_page(
        root, "Knowledge Base/index.md", "employment xylophones budget xq", updated="2026-06-01"
    )
    _write_page(root, "Knowledge Base/punct.md", "+++ ~~~ !!!", updated="2026-01-02")
    _write_page(root, "Knowledge Base/same-date-b.md", "twin content marker", updated="2026-02-02")
    _write_page(root, "Knowledge Base/same-date-a.md", "twin content marker", updated="2026-02-02")


_PARITY_QUERIES = [
    "contract employment",  # multi-token, order-free
    "ylophon",  # mid-word
    "budget",  # title-only match
    "xq",  # 2-char needle
    "q",  # 1-char needle
    "42%",  # LIKE metachar %
    "e_c",  # LIKE metachar _
    "sõbra",  # non-ASCII
    "tallinnasse",  # case-folding
    "~~~",  # punctuation-only page
    "twin marker",  # tie on updated → path tie-break
    "xq spot",  # short + indexable token mix
    "employment",  # multiple matches, ordering
    "zzz-no-such-token",  # empty result
    "",  # empty query → empty lane
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
@pytest.mark.parametrize("k", [0, 1, 2, 5, 100])
def test_a_bounded_keyword_lane_is_the_unbounded_prefix_on_both_backends(
    tmp_path, monkeypatch, k
):
    """The bound may not change the contract, only how much of it is returned.

    The lane answers in `updated` desc order, so a caller cannot bound it by
    slicing what it gets back -- that would be slicing recency order after the
    fact only if the order had already been applied, which is the whole reason
    the bound lives inside the lane. Asserting the bounded result is a literal
    prefix of the unbounded one is what says it was applied in the right place.

    Both backends are checked at the same `k`, because the sidecar applies it
    as SQL `LIMIT` and the reference scan applies it as a slice, and the parity
    gate is worth nothing if it only holds when unbounded.
    """
    _parity_corpus(tmp_path)

    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    reference_all = find_module._keyword_match_paths(tmp_path, "employment", "kb")
    reference_k = find_module._keyword_match_paths(tmp_path, "employment", "kb", k=k)
    find_module.clear_cache()
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "fts5")
    indexed_all = find_module._keyword_match_paths(tmp_path, "employment", "kb")
    indexed_k = find_module._keyword_match_paths(tmp_path, "employment", "kb", k=k)

    assert reference_k == reference_all[:k]
    assert indexed_k == indexed_all[:k]
    assert indexed_k == reference_k


@needs_fts5
def test_an_unbounded_keyword_lane_is_unchanged(tmp_path, monkeypatch):
    """`k=None` is the existing contract, and every other caller still passes it."""
    _parity_corpus(tmp_path)
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "fts5")

    assert find_module._keyword_match_paths(
        tmp_path, "employment", "kb", k=None
    ) == find_module._keyword_match_paths(tmp_path, "employment", "kb")


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


# ------------------------------------------------ foreground repair boundary


@needs_fts5
def test_nonrepairing_searches_return_none_and_schedule_one_background_repair(
    tmp_path, monkeypatch
):
    """A missing sidecar never turns a foreground request into a corpus build.

    All three search APIs share one per-vault repair flight, so a burst of
    callers remains cheap while the maintained sidecar catches up once.
    """
    _write_page(tmp_path, "Knowledge Base/a.md", "one searchable decision")
    store = lexstore.get_store(tmp_path)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls = 0

    def _background_repair():
        nonlocal calls
        calls += 1
        started.set()
        release.wait(timeout=5)
        finished.set()

    monkeypatch.setattr(store, "rebuild_atomic", _background_repair)

    assert lexstore.search_bm25(tmp_path, "searchable", 5, repair=False) is None
    assert started.wait(timeout=2)
    assert lexstore.search_substring(tmp_path, "searchable", repair=False) is None
    assert lexstore.search_semantic_units(tmp_path, "decision", 5, repair=False) is None
    assert calls == 1
    assert not lexstore.lexical_path(tmp_path).exists()

    release.set()
    assert finished.wait(timeout=2)


@needs_fts5
def test_nonrepairing_search_does_not_heal_a_stale_sidecar(tmp_path, monkeypatch):
    """Detected drift is handed to the repair worker, never healed inline."""
    _write_page(tmp_path, "Knowledge Base/a.md", "stable searchable payload")
    assert lexstore.search_bm25(tmp_path, "searchable", 5)
    store = lexstore.get_store(tmp_path)

    _write_page(tmp_path, "Knowledge Base/b.md", "new drifted payload")
    current = bm25.corpus_key(tmp_path, "kb")
    repairs: list[str] = []
    repair_started = threading.Event()
    repair_release = threading.Event()
    repair_finished = threading.Event()

    def _forbidden_rebuild(*_args, **_kwargs):
        repairs.append("rebuild")
        raise AssertionError("foreground search rebuilt the lexical sidecar")

    def _forbidden_heal(*_args, **_kwargs):
        repairs.append("heal")
        raise AssertionError("foreground search healed the lexical sidecar")

    def _background_repair():
        repair_started.set()
        repair_release.wait(timeout=5)
        repair_finished.set()

    monkeypatch.setattr(store, "_rebuild", _forbidden_rebuild)
    monkeypatch.setattr(store, "_heal_delta", _forbidden_heal)
    monkeypatch.setattr(store, "rebuild_atomic", _background_repair)

    assert (
        lexstore.search_bm25(
            tmp_path,
            "drifted",
            5,
            freshness=current,
            repair=False,
        )
        is None
    )
    assert repairs == []
    assert repair_started.wait(timeout=2)
    repair_release.set()
    assert repair_finished.wait(timeout=2)


@needs_fts5
def test_nonrepairing_search_serves_an_already_fresh_sidecar(tmp_path):
    _write_page(tmp_path, "Knowledge Base/a.md", "stable searchable payload")
    assert lexstore.search_bm25(tmp_path, "searchable", 5)
    current = bm25.corpus_key(tmp_path, "kb")

    hits = lexstore.search_bm25(
        tmp_path,
        "searchable",
        5,
        freshness=current,
        repair=False,
    )

    assert hits and hits[0][0] == "Knowledge Base/a.md"


# ------------------------------------------- a declined sidecar is not "empty"


def _declining_sidecar(monkeypatch: pytest.MonkeyPatch) -> None:
    """The catalog says "I could not check", on a backend that is not python.

    Patched at `lexstore.search_substring` rather than deeper, because None is
    that function's whole documented contract ("None -> fall back") and every
    deeper cause -- a retired store, a missing catalog file, a sqlite error, a
    sync that could not take the publication lock -- funnels into it.
    """
    monkeypatch.setattr(lexstore, "backend", lambda: "fts5")
    monkeypatch.setattr(lexstore, "search_substring", lambda *a, **k: None)


def test_a_declined_sidecar_answers_from_the_scan_not_with_an_empty_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The false empty (#526 ask 2), at the seam where it was produced.

    A governed write's lexical upsert defers while the write still holds the
    vault lock, so the catalog is most likely to decline in the moment right
    after a write -- on exactly the page the caller is most likely to want.
    Returning [] there reports "no such memory" for content that was just
    committed, which is the worst answer this system can give.
    """
    _write_page(tmp_path, "Knowledge Base/just-written.md", "kubernetes ingress configuration")
    _fill_corpus(tmp_path)
    _declining_sidecar(monkeypatch)

    paths = find_module._keyword_match_paths(tmp_path, "kubernetes ingress", "kb")

    assert paths == ["Knowledge Base/just-written.md"]


def test_a_declined_sidecar_is_still_reported_as_a_degradation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Correct AND loud, not correct instead of loud.

    The lane really did fall off its fast rung, and the counter is how a
    persistently broken sidecar becomes visible rather than merely slow. The
    marker was the only thing the old branch got right; the fix keeps it and
    changes the answer, not the reporting.
    """
    _write_page(tmp_path, "Knowledge Base/just-written.md", "kubernetes ingress configuration")
    _declining_sidecar(monkeypatch)
    failed: list[str] = []

    paths = find_module._keyword_match_paths(
        tmp_path, "kubernetes ingress", "kb", failed_out=failed
    )

    assert paths == ["Knowledge Base/just-written.md"]
    assert failed == ["keyword_lexical"]
    assert find_module.degradation_counts().get("keyword_lexical") == 1


def test_the_python_rung_is_not_a_degradation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On `EXOMEM_LEXICAL_BACKEND=python` the scan IS the lane, not a fallback.

    Both rungs now reach the same scan, so the marker is the only thing that
    still distinguishes them -- and counting the configured backend as a
    degradation would make the counter fire constantly and mean nothing.
    """
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    _write_page(tmp_path, "Knowledge Base/just-written.md", "kubernetes ingress configuration")
    failed: list[str] = []

    paths = find_module._keyword_match_paths(
        tmp_path, "kubernetes ingress", "kb", failed_out=failed
    )

    assert paths == ["Knowledge Base/just-written.md"]
    assert failed == []
    assert "keyword_lexical" not in find_module.degradation_counts()


@needs_fts5
def test_a_page_written_this_second_is_visible_when_the_catalog_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read-after-write on the fts5 lane (#526 ask 3), through the real store.

    The write gate pins `EXOMEM_LEXICAL_BACKEND=python` to keep its visibility
    assertion deterministic, so no CI surface measured this window on the
    backend that actually ships. Patched at `_serve_synced_live_catalog` rather
    than at the module function so the None travels the production route --
    `LexicalStore.search_substring`'s own `sqlite3.Error` handler, which retires
    the store for this process and is one of the documented causes.
    """
    _write_page(tmp_path, "Knowledge Base/older.md", "gardening tips for spring")
    # Materialize a real catalog first, so the store is healthy up to this read.
    assert bm25.search(tmp_path, "gardening tips", k=5, scope="kb")
    assert lexstore.lexical_path(tmp_path).exists()

    _write_page(tmp_path, "Knowledge Base/just-written.md", "kubernetes ingress configuration")

    def erroring(self, *args, **kwargs):  # noqa: ANN001, ANN202, ARG001
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(lexstore.LexicalStore, "_serve_synced_live_catalog", erroring)

    assert lexstore.search_substring(tmp_path, "kubernetes ingress", scope="kb") is None
    assert find_module._keyword_match_paths(tmp_path, "kubernetes ingress", "kb") == [
        "Knowledge Base/just-written.md"
    ]
