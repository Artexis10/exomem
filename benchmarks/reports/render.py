"""Cross-lane comparative rendering: LME + Epistemic State Bench, composed
under one offline network guard, per-ability x per-variant only, never an
aggregate.

Each input is rendered by its OWN lane's existing, tested renderer -- this
module never re-implements verdict, bounds, or contamination logic. A
non-terminal manifest or an unknown ``schema_version`` refuses (raised by
``protocol.manifest.load_manifest``, reached through
``lme.report.render_run_report``) before anything renders. A blocked row
renders whatever its own lane renderer already renders for that state
(``blocked: <reason>`` / ``INVALID`` / ``READINESS_UNVERIFIABLE`` -- see
``lme.report.manifest_banner``) -- never a loss, and this module adds nothing
on top of it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from epistemic.report import render_epistemic_report
from lme.report import render_run_report
from protocol.offline import offline_guard

from .latency import ProviderLatency, assert_no_cross_provider_latency


@dataclass(frozen=True)
class LmeRun:
    """An LME/MemoryBench run directory, rendered by the LME lane renderer."""

    run_dir: Path


@dataclass(frozen=True)
class EpistemicCohort:
    """A stored, validated Epistemic State Bench cohort artifact."""

    cohort_path: Path
    run_root: Path


ReportInput = LmeRun | EpistemicCohort


class ReportRefused(ValueError):
    """A rendered report would violate the no-aggregate invariant."""


def render_all(
    inputs: Sequence[ReportInput],
    *,
    latency: Sequence[ProviderLatency] = (),
) -> str:
    """Compose the LME and Epistemic lane renderers plus a latency section.

    Runs entirely under one ``offline_guard``; a non-terminal manifest or
    unknown ``schema_version`` anywhere in ``inputs`` raises before this
    returns -- the same refusal each lane's own report entry point already
    raises, never reimplemented here. Never emits an aggregate across
    abilities, variants, or lanes.
    """

    sections: list[str] = []
    with offline_guard():
        for item in inputs:
            if isinstance(item, LmeRun):
                sections.append(render_run_report(item.run_dir, offline=False))
            elif isinstance(item, EpistemicCohort):
                sections.append(
                    render_epistemic_report(item.cohort_path, run_root=item.run_root, offline=False)
                )
            else:
                raise TypeError(f"unsupported report input: {item!r}")
        latency_section = assert_no_cross_provider_latency(latency)
    if latency_section:
        sections.append(latency_section)
    report = "\n".join(sections)
    if "aggregate" in report.lower():
        raise ReportRefused("rendered report must never contain the word aggregate")
    return report


__all__ = ["LmeRun", "EpistemicCohort", "ReportInput", "ReportRefused", "render_all"]
