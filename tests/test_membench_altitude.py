"""Ingestion altitude: the layer a run measured the product at.

Track B bulk-loads every corpus document as a raw source and never compiles.
Measured on the 2026-08-07 full-strength vault: 205 pages under `Sources/`,
zero compiled notes, `ingested_into: []` on 204 of 204, zero `derived_from`.

Two dimensions are *structurally* unmeasurable in that state, because the thing
they score does not exist rather than merely scoring badly:

- **provenance** — the chain is a compiled conclusion declaring the sources it
  drew from. With nothing compiled there is no chain, and the column degenerates
  to "which documents did you return", which is the same shallow thing for every
  contender.
- **contradiction_uncertainty** — detection runs over compiled conclusions that
  disagree. A pile of independent raw sources has nothing to detect. Both floor
  and ceiling are 0.

`abstention` is deliberately NOT on that list. It is *affected* by altitude — a
dense raw dump always matches something — but declining when you should is
measurable at any altitude, so calling it structurally void would overclaim.

Altitude is a declared property with a raw-source default, following the
governance three-state precedent exactly: score everything, exclude at report
time with the reason visible. A benchmark may measure a product at a shallow
altitude; it may not publish that number as though it measured a deep one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from membench.adapters.base import INGESTION_ALTITUDES, Capability
from membench.reporting import (
    ALTITUDE_DEPENDENT_DIMENSIONS,
    _altitude_conflict,
    _bounds_section,
    _RunView,
)


def _run(provider: str, dimensions: dict, altitude: str = "raw_source") -> _RunView:
    return _RunView(
        run_dir=Path("/nonexistent"),
        run_id=f"run-{provider}",
        label=f"{provider} · test",
        invalid=False,
        invalid_reason=None,
        run_failures=0,
        dimensions=dimensions,
        per_query=[],
        latencies=[],
        judge=None,
        failure_lines=0,
        provider=provider,
        ingestion_altitude=altitude,
    )


def test_the_vocabulary_is_closed() -> None:
    assert INGESTION_ALTITUDES == frozenset({"raw_source", "compiled"})


def test_only_structurally_void_dimensions_are_altitude_dependent() -> None:
    """abstention stays off the list on purpose — see the module docstring."""

    assert ALTITUDE_DEPENDENT_DIMENSIONS == frozenset(
        {"provenance", "contradiction_uncertainty"}
    )


def test_mismatched_altitudes_are_a_conflict() -> None:
    assert _altitude_conflict(["raw_source", "compiled"]) is True
    assert _altitude_conflict(["compiled", "compiled"]) is False
    assert _altitude_conflict(["raw_source"]) is False


def test_an_adapter_defaults_to_raw_source() -> None:
    """The honest default: a bulk load is a raw-source load unless declared."""

    from membench.adapters import create_adapter

    assert create_adapter("null-abstain").ingestion_altitude == "raw_source"
    assert create_adapter("exomem-local").ingestion_altitude == "raw_source"


def test_an_unknown_altitude_is_refused() -> None:
    from membench.runner import _ingestion_altitude

    class Bogus:
        name = "bogus"
        ingestion_altitude = "somewhere"

        def capabilities(self):
            return frozenset({Capability.SEARCH})

    with pytest.raises(ValueError, match="unknown ingestion_altitude"):
        _ingestion_altitude(Bogus())


def test_bounds_marks_altitude_void_dimensions_rather_than_scoring_them() -> None:
    """A zero here must not read as a product result.

    Without the label, provenance 0 and contradiction 0 look like findings about
    the contender instead of statements that the benchmark never built the
    structure they score.
    """

    counts = lambda p, f: {  # noqa: E731
        "pass": p,
        "fail": f,
        "not_applicable": 0,
        "unsupported": 0,
    }
    text = "\n".join(
        _bounds_section(
            [
                _run("null-abstain", {"provenance": counts(0, 180)}),
                _run("oracle-retrieval", {"provenance": counts(198, 6)}),
                _run("exomem-local", {"provenance": counts(0, 208)}),
            ]
        )
    )
    assert "raw-source altitude" in text
    assert "not measurable at this altitude" in text


def test_a_compiled_run_scores_the_altitude_dependent_rows_normally() -> None:
    counts = lambda p, f: {  # noqa: E731
        "pass": p,
        "fail": f,
        "not_applicable": 0,
        "unsupported": 0,
    }
    text = "\n".join(
        _bounds_section(
            [
                _run("null-abstain", {"provenance": counts(0, 180)}, altitude="compiled"),
                _run(
                    "oracle-retrieval", {"provenance": counts(198, 6)}, altitude="compiled"
                ),
                _run("exomem-local", {"provenance": counts(97, 111)}, altitude="compiled"),
            ]
        )
    )
    assert "not measurable at this altitude" not in text
    assert "97 (49%)" in text
