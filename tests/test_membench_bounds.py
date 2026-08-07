"""Floor/ceiling bounds in the comparison report.

A pass count without bounds is not a measurement. These tests pin the three
readings the section exists to make impossible to miss: where a contender sits
in the usable range, a dimension nothing can pass, and a dimension nothing
exercised — the last two look identical in the tallies and mean opposite things.
"""

from __future__ import annotations

from pathlib import Path

from membench.reporting import (
    CEILING_PROVIDER,
    FLOOR_PROVIDER,
    _bounds_section,
    _RunView,
)


def _run(
    provider: str,
    dimensions: dict,
    *,
    invalid: bool = False,
    altitude: str = "compiled",
) -> _RunView:
    """Default to `compiled` here.

    These tests exercise the floor/ceiling/VOID logic, which is orthogonal to
    ingestion altitude. At raw-source altitude the altitude gate correctly
    withholds `provenance` and `contradiction_uncertainty` — the two dimensions
    several of these cases use — so declaring compiled keeps each test measuring
    the one thing it is about.
    """

    return _RunView(
        run_dir=Path("/nonexistent"),
        run_id=f"run-{provider}",
        label=f"{provider} · test",
        invalid=invalid,
        invalid_reason="broken" if invalid else None,
        run_failures=0,
        dimensions=dimensions,
        per_query=[],
        latencies=[],
        judge=None,
        failure_lines=0,
        provider=provider,
        ingestion_altitude=altitude,
    )


def _counts(passed: int, failed: int = 0, na: int = 0, unsupported: int = 0) -> dict:
    return {
        "pass": passed,
        "fail": failed,
        "not_applicable": na,
        "unsupported": unsupported,
    }


def _table(runs) -> str:
    return "\n".join(_bounds_section(runs))


def test_a_contender_is_placed_in_the_usable_range() -> None:
    """82% is (180-52)/(208-52) — position between floor and ceiling, not 180/236."""

    text = _table(
        [
            _run(FLOOR_PROVIDER, {"abstention": _counts(52, 184)}),
            _run(CEILING_PROVIDER, {"abstention": _counts(208, 28)}),
            _run("exomem-local", {"abstention": _counts(180, 56)}),
        ]
    )
    assert "| abstention | 52 | 208 | 156 |" in text
    assert "180 (82%)" in text


def test_an_attempted_but_unpassable_dimension_is_void() -> None:
    """Floor == ceiling with real verdicts recorded is a defect, and says so.

    Reported as a shared capability gap it would read as a finding about both
    products. It is a finding about the gate.
    """

    text = _table(
        [
            _run(FLOOR_PROVIDER, {"contradiction_uncertainty": _counts(0, 20)}),
            _run(CEILING_PROVIDER, {"contradiction_uncertainty": _counts(0, 20)}),
            _run("exomem-local", {"contradiction_uncertainty": _counts(0, 20)}),
        ]
    )
    assert "**VOID** (0)" in text
    assert "not measurable" in text
    assert "VOID dimensions: `contradiction_uncertainty`" in text
    assert "must" in text and "not be reported as a shared product capability gap" in text


def test_a_never_exercised_dimension_is_not_called_void() -> None:
    """All-n/a is a dimension this run set does not cover — Track C rows in a
    Track B run. Calling it VOID would bury the real defect among the noise."""

    text = _table(
        [
            _run(FLOOR_PROVIDER, {"behavior": _counts(0, 0, na=236)}),
            _run(CEILING_PROVIDER, {"behavior": _counts(0, 0, na=236)}),
            _run("exomem-local", {"behavior": _counts(0, 0, na=236)}),
        ]
    )
    assert "not exercised" in text
    assert "VOID" not in text


def test_a_contender_at_the_floor_is_flagged() -> None:
    """exomem's provenance 0 against a ceiling of 198 is the 4b.31 signature."""

    text = _table(
        [
            _run(FLOOR_PROVIDER, {"provenance": _counts(0, 180)}),
            _run(CEILING_PROVIDER, {"provenance": _counts(198, 6)}),
            _run("exomem-local", {"provenance": _counts(0, 208)}),
        ]
    )
    assert "at/below floor" in text


def test_reference_contenders_never_appear_as_products() -> None:
    """They are instruments. A column for them would invite reading the
    ceiling as a competitor that beat everyone."""

    runs = [
        _run(FLOOR_PROVIDER, {"factual_qa": _counts(0, 180)}),
        _run(CEILING_PROVIDER, {"factual_qa": _counts(172, 8)}),
        _run("exomem-local", {"factual_qa": _counts(148, 32)}),
    ]
    header = next(line for line in _bounds_section(runs) if line.startswith("| dimension"))
    assert "exomem-local" in header
    assert CEILING_PROVIDER not in header
    assert FLOOR_PROVIDER not in header


def test_missing_references_refuse_to_imply_a_scale() -> None:
    """Without a reference run the counts have no scale, and the section says
    so rather than rendering a table that looks like one."""

    text = _table([_run("exomem-local", {"factual_qa": _counts(148, 32)})])
    assert "Not available" in text
    assert "count without a scale" in text
    assert "| dimension | floor" not in text


def test_an_invalid_reference_does_not_supply_bounds() -> None:
    """An INVALID run renders INVALID everywhere else; it must not quietly
    become the ceiling here."""

    text = _table(
        [
            _run(CEILING_PROVIDER, {"factual_qa": _counts(172, 8)}, invalid=True),
            _run("exomem-local", {"factual_qa": _counts(148, 32)}),
        ]
    )
    assert "Not available" in text


def test_governance_is_kept_out_of_the_bounds_table() -> None:
    """Retrieving nothing cannot leak, so the floor posts the best governance
    sheet in the suite and a floor-to-ceiling scale would run backwards."""

    text = _table(
        [
            _run(FLOOR_PROVIDER, {"governance": _counts(16, 0)}),
            _run(CEILING_PROVIDER, {"governance": _counts(8, 8)}),
            _run("exomem-local", {"governance": _counts(0, 16)}),
        ]
    )
    assert "| governance |" not in text
