"""Fixture-data unit tests for `exomem.eval_report` — model-free and fast.

These run in the lean (embedding-free) pytest job like any other test: the
module under test is pure (no torch, no live vault/model access), so the corpus
walk runs against the committed `tests/fixtures/` tree and the renderer runs on
synthetic aggregate inputs. The privacy-guard test mirrors
`tests/test_scaffold_no_leak.py`'s denylist posture: it asserts none of the
golden set's query strings or target paths appear in the rendered report.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from exomem import eval_report

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOLDEN = Path(__file__).resolve().parent / "golden" / "queries.yaml"


# Synthetic aggregate inputs — invented numbers only, never real query/path data.
_SYNTH_PER_MODE = {
    "keyword": {
        "ndcg5": 0.4211, "ndcg10": 0.4567, "mrr": 0.5123, "recall10": 0.6001,
        "latency_median_ms": 12.3, "latency_p90_ms": 25.7,
    },
    "hybrid": {
        "ndcg5": 0.7012, "ndcg10": 0.7333, "mrr": 0.8001, "recall10": 0.8888,
        "latency_median_ms": 41.9, "latency_p90_ms": 88.2,
    },
    "hybrid+rerank": {
        "ndcg5": 0.7810, "ndcg10": 0.8004, "mrr": 0.8502, "recall10": 0.9101,
        "latency_median_ms": 130.5, "latency_p90_ms": 260.4,
    },
}
_SYNTH_CORPUS = {"files": 1200, "notes": 900, "media": 300}
_SYNTH_GOLDEN_N = 9
_SYNTH_META = {
    "exomem_version": "9.9.9-test",
    "embedding_model": "BAAI/bge-base-en-v1.5",
    "reranker_model": "BAAI/bge-reranker-base",
    "hardware": "synthetic test host",
    "date": "2026-07-03",
}


def _render_synthetic() -> str:
    return eval_report.render_benchmark_report(
        corpus=_SYNTH_CORPUS,
        per_mode=_SYNTH_PER_MODE,
        golden_n=_SYNTH_GOLDEN_N,
        meta=_SYNTH_META,
    )


def _golden_strings() -> tuple[list[str], list[str]]:
    """Return (query strings, target paths) from the golden set."""
    raw = yaml.safe_load(GOLDEN.read_text(encoding="utf-8")) or []
    queries: list[str] = []
    paths: list[str] = []
    for entry in raw:
        q = entry.get("query")
        if q:
            queries.append(q)
        for p in entry.get("expect_any_of", []) or []:
            paths.append(p)
        for p in (entry.get("graded") or {}):
            paths.append(p)
    return queries, paths


def test_count_corpus_stats_against_fixtures() -> None:
    """Corpus walk over tests/fixtures returns rounded, internally-consistent counts.

    The fixture tree currently holds 30 markdown files vault-wide (28 KB notes
    outside `_Schema` + 2 read-only `Reference/` pages) and no media binaries.
    Rounded DOWN to the nearest 10 that is files=30, notes=20, media=0. We assert
    the rounding contract and the bucket definitions, not exact filenames.
    """
    stats = eval_report.count_corpus_stats(FIXTURES)

    assert set(stats) == {"files", "notes", "media"}
    for key, value in stats.items():
        assert isinstance(value, int), key
        assert value >= 0, key
        assert value % 10 == 0, f"{key} must be rounded to the nearest 10, got {value}"

    # No binary media in the fixture tree.
    assert stats["media"] == 0
    # "files" (whole-vault markdown) is a superset of "notes" (KB-scoped markdown).
    assert stats["files"] >= stats["notes"]
    # Current fixture tree: 30 files / 28 notes -> floor to 30 / 20.
    assert stats["files"] == 30
    assert stats["notes"] == 20


def test_render_benchmark_report_shape() -> None:
    """Rendered markdown carries every metric + latency per mode, corpus, limits."""
    md = _render_synthetic()

    # A row per mode, each with all four metrics + both latency percentiles.
    for mode, row in _SYNTH_PER_MODE.items():
        assert mode in md
        assert f"{row['ndcg5']:.4f}" in md
        assert f"{row['ndcg10']:.4f}" in md
        assert f"{row['mrr']:.4f}" in md
        assert f"{row['recall10']:.4f}" in md
        assert f"{row['latency_median_ms']:.1f}" in md
        assert f"{row['latency_p90_ms']:.1f}" in md

    # Column headers present.
    for header in ("NDCG@5", "NDCG@10", "MRR", "recall@10"):
        assert header in md

    # Corpus counts and golden-set size present.
    assert str(_SYNTH_CORPUS["files"]) in md
    assert str(_SYNTH_CORPUS["notes"]) in md
    assert str(_SYNTH_CORPUS["media"]) in md
    assert str(_SYNTH_GOLDEN_N) in md

    # Meta methodology lines present.
    assert _SYNTH_META["embedding_model"] in md
    assert _SYNTH_META["reranker_model"] in md

    # A limitations note is present.
    assert "Limitations" in md


def test_render_benchmark_report_omits_per_query_rows() -> None:
    """Exactly one result row per mode — row count scales with modes, not queries."""
    md = _render_synthetic()
    data_rows = [
        line for line in md.splitlines()
        if line.startswith("|")
        and "Mode" not in line          # skip the header row
        and set(line.replace("|", "").strip()) - {"-", " "}  # skip the |---| separator
    ]
    assert len(data_rows) == len(_SYNTH_PER_MODE)

    # Sanity: a golden set far larger than the mode count must not add rows.
    md_big_golden = eval_report.render_benchmark_report(
        corpus=_SYNTH_CORPUS,
        per_mode=_SYNTH_PER_MODE,
        golden_n=9999,
        meta=_SYNTH_META,
    )
    big_rows = [
        line for line in md_big_golden.splitlines()
        if line.startswith("|")
        and "Mode" not in line
        and set(line.replace("|", "").strip()) - {"-", " "}
    ]
    assert len(big_rows) == len(_SYNTH_PER_MODE)


def test_report_has_no_leaked_golden_content() -> None:
    """Privacy guard: no golden query string or target path leaks into the report.

    Renders both the synthetic report and a report that reads the real golden
    file's size (the closest-to-production shape available without a live vault),
    then asserts none of the golden query strings or `expect_any_of` / `graded`
    target paths appear anywhere in the rendered markdown.
    """
    queries, paths = _golden_strings()
    assert queries, "golden set produced no query strings — wrong path?"
    assert paths, "golden set produced no target paths — wrong path?"

    renders = [
        _render_synthetic(),
        eval_report.render_benchmark_report(
            corpus=eval_report.count_corpus_stats(FIXTURES),
            per_mode=_SYNTH_PER_MODE,
            golden_n=len(queries),
            meta=_SYNTH_META,
        ),
    ]
    for md in renders:
        for q in queries:
            assert q not in md, f"leaked golden query text: {q!r}"
        for p in paths:
            assert p not in md, f"leaked golden target path: {p!r}"
