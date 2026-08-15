"""Ingest (write) latency in the cross-run comparison report.

``OpResult.latency_ms`` was always drained to ``ingest.jsonl`` per run
(runner.py), but ``reporting._load_run`` only ever opened ``retrieval.jsonl``
— ingest latency reached no dimension, manifest, or report row, one of the
three blind surfaces that hid a 55-70s production write regression.

These tests exercise the render layer (``_ingest_latency_section``) directly
against hand-built ``_RunView`` fixtures, the same pattern already used by
``test_membench_altitude.py`` and ``test_membench_bounds.py`` for the other
report sections — deliberately independent of ``_load_run``'s manifest
validation (which shells out to Git and cannot run in every environment).
"""

from __future__ import annotations

from pathlib import Path

from membench.reporting import _ingest_latency_section, _RunView


def _run(
    provider: str,
    *,
    ingest_latencies: list[float] | None = None,
    altitude: str = "raw_source",
    invalid: bool = False,
) -> _RunView:
    return _RunView(
        run_dir=Path("/nonexistent"),
        run_id=f"run-{provider}",
        label=f"{provider} · test",
        invalid=invalid,
        invalid_reason="broken" if invalid else None,
        run_failures=0,
        dimensions={},
        per_query=[],
        latencies=[],
        judge=None,
        failure_lines=0,
        provider=provider,
        ingestion_altitude=altitude,
        ingest_latencies=ingest_latencies or [],
    )


def test_absent_without_any_ingest_data_anywhere() -> None:
    """Graceful degrade: no run in the comparison has ingest.jsonl data, so
    no ingest row is rendered at all — not a row of ``n/a`` placeholders."""

    runs = [_run("alpha"), _run("beta")]
    assert _ingest_latency_section(runs) == []


def test_renders_sibling_rows_with_altitude_caveat() -> None:
    text = "\n".join(
        _ingest_latency_section([_run("alpha", ingest_latencies=[10.0, 20.0, 30.0])])
    )
    assert "| ingest_median_ms |" in text
    assert "20.000" in text
    assert "| ingest_p95_ms |" in text
    assert "30.000" in text
    assert "| ingest_ops |" in text
    assert "| ingest_ops | 3 |" in text
    # Altitude caveat: the per-op unit of work is contender-specific, and the
    # harness's own signal for that (ingestion_altitude) must be attached
    # wherever the ingest number renders.
    assert "raw_source" in text


def test_per_run_missing_data_is_na_not_omitted() -> None:
    """When at least one run in the comparison has ingest data, a run without
    it renders ``n/a`` in that column rather than dropping the row — the
    retrieval-latency idiom this mirrors."""

    runs = [_run("alpha", ingest_latencies=[5.0]), _run("beta")]
    text = "\n".join(_ingest_latency_section(runs))
    rows = [line for line in text.splitlines() if line.startswith("| ingest_")]
    assert rows
    for row in rows:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        assert cells[-1] == "n/a"


def test_invalid_run_renders_invalid_not_a_number() -> None:
    runs = [_run("alpha", ingest_latencies=[5.0], invalid=True)]
    text = "\n".join(_ingest_latency_section(runs))
    rows = [line for line in text.splitlines() if line.startswith("| ingest_")]
    assert rows
    for row in rows:
        assert "INVALID" in row
