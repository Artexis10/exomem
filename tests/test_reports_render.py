"""D3 reports/render.py: render_all composes the LME and Epistemic lane
renderers under one offline_guard, per-ability x per-variant only, refusing
non-terminal manifests and unknown schema_versions, and never an aggregate.

Fixtures are built with the real writers: `lme.runner.execute_run` for LME
run directories (mirrors tests/test_report_offline.py's `_run`), and the real
epistemic cohort/evidence writers for a validated cohort artifact (mirrors
tests/test_epistemic_report.py's `_stored_cohort`) -- never hand-written JSON
that merely "looks like" a manifest or cohort.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
from pathlib import Path
from unittest import mock

import pytest

FIXTURE = Path("benchmarks/lme/fixtures/mini.json")


def _lme_run(out: Path, run_id: str, provider: str = "hybrid-rag-control"):
    from lme.reader import StubReader
    from lme.runner import RunConfig, execute_run

    with mock.patch.dict(os.environ, {"PROTOCOL_FIXTURE_EMBEDDER": "1", "EXOMEM_DISABLE_EMBEDDINGS": "1"}):
        return execute_run(
            RunConfig(dataset=FIXTURE, out=out, reader_name="stub", run_id=run_id, provider=provider),
            reader=StubReader(),
        )


def _started_manifest(run_dir: Path) -> None:
    from protocol.manifest import start_manifest

    start_manifest(
        run_dir, run_id="started",
        dataset={"id": "fixture", "variant": "mini", "source": "local", "revision": "1", "sha256": "a" * 64, "case_count": 0},
        started_at="2026-01-01T00:00:00Z",
    )


def _epistemic_cohort(tmp_path: Path) -> Path:
    """A minimal validated cohort: one product row, two negative controls.

    Copied from tests/test_epistemic_report.py::_stored_cohort's shape (same
    real writers: persist_assertion_evidence, validate_epistemic_cohort,
    persist_validated_cohort) -- this file does not import that test module,
    matching the repo's convention of a small local fixture per test file.
    """

    from epistemic.assertions import AssertionContext, no_retired_state_served_as_current
    from epistemic.cohort import (
        CohortAssertionResult,
        CohortExpectationIdentity,
        EpistemicCohortRow,
        persist_validated_cohort,
        validate_epistemic_cohort,
    )
    from epistemic.evidence import persist_assertion_evidence
    from epistemic.snapshot import (
        EpistemicStateSnapshot,
        FieldDeclaration,
        ProjectorMeta,
        StateItem,
    )

    def snapshot(provider: str, *, current: str) -> EpistemicStateSnapshot:
        return EpistemicStateSnapshot(
            provider=provider, variant="native" if provider == "prod|uct" else "control",
            phase="p1", taken_at="2026-08-11T00:00:00Z",
            items=(
                StateItem(
                    id="retired", kind="claim", title="retired", text="stale",
                    current=current, retired_reason="superseded",
                ),
            ),
            declarations=(
                FieldDeclaration(field="current", status="declared", evidence="https://example.invalid/current"),
            ),
            projector=ProjectorMeta(
                name="fixture", version="1", author="test", endpoints_used=("broker:state.read",), loc=1,
            ),
        )

    identity = CohortExpectationIdentity(
        scenario_id="scenario|one", scenario_sha256="1" * 64,
        phase_id="p1", expectation_ordinal=1, assertion="no_retired_state_served_as_current",
        subject="retired", counterpart=None, tolerance=0.0, freshness_bound_s=None,
    )

    def cell(provider: str, *, current: str) -> CohortAssertionResult:
        context = AssertionContext(snapshot=snapshot(provider, current=current), subject="retired")
        result = no_retired_state_served_as_current(context)
        evidence_ref = persist_assertion_evidence(
            run_root=tmp_path, scenario_id="scenario|one", scenario_sha256="1" * 64,
            family_id="f01", phase_id="p1", expectation_ordinal=1,
            assertion=result.name, context=context, result=result,
        )
        return CohortAssertionResult(identity=identity, result=result, evidence_ref=evidence_ref)

    rows = (
        EpistemicCohortRow(provider="prod|uct", variant="native", assertions=(cell("prod|uct", current="no"),)),
        EpistemicCohortRow(provider="grep-markdown", variant="control", assertions=(cell("grep-markdown", current="no"),)),
        EpistemicCohortRow(provider="no-memory", variant="control", assertions=(cell("no-memory", current="no"),)),
    )
    cohort = validate_epistemic_cohort(run_id="run-1", rows=rows, run_root=tmp_path)
    return persist_validated_cohort(tmp_path / "validated-cohort.v1.json", cohort, run_root=tmp_path)


def test_composes_an_lme_run_per_ability_table(tmp_path: Path) -> None:
    from reports.render import LmeRun, render_all

    result = _lme_run(tmp_path, "compose-lme")
    rendered = render_all([LmeRun(run_dir=result.run_dir)])
    assert "| Ability | Variant | Questions |" in rendered
    assert "aggregate" not in rendered.lower()


def test_composes_lme_and_epistemic_sections_together(tmp_path: Path) -> None:
    from reports.render import EpistemicCohort, LmeRun, render_all

    lme_dir = tmp_path / "lme"
    result = _lme_run(lme_dir, "compose-both")
    cohort_root = tmp_path / "epistemic"
    cohort_root.mkdir()
    cohort_path = _epistemic_cohort(cohort_root)

    rendered = render_all(
        [
            LmeRun(run_dir=result.run_dir),
            EpistemicCohort(cohort_path=cohort_path, run_root=cohort_root),
        ]
    )
    assert "| Ability | Variant | Questions |" in rendered
    assert "Epistemic State Bench" in rendered
    assert "Signal disposition" in rendered
    assert "aggregate" not in rendered.lower()


def test_r1_a_non_terminal_manifest_refuses(tmp_path: Path) -> None:
    from reports.render import LmeRun, render_all

    _started_manifest(tmp_path)
    with pytest.raises(ValueError, match="non-terminal"):
        render_all([LmeRun(run_dir=tmp_path)])


def test_r2_an_unknown_schema_version_refuses(tmp_path: Path) -> None:
    from reports.render import LmeRun, render_all

    run_root = tmp_path / "run"
    result = _lme_run(run_root, "schema-drift")
    mutated = tmp_path / "schema-drift-mutated"
    shutil.copytree(result.run_dir, mutated)
    manifest = json.loads((mutated / "manifest.json").read_text(encoding="utf-8"))
    manifest["schema_version"] = 99
    (mutated / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="schema_version"):
        render_all([LmeRun(run_dir=mutated)])


def test_r7_aggregate_is_never_rendered_even_if_a_composed_section_tries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import reports.render as render_module

    monkeypatch.setattr(render_module, "render_run_report", lambda run_dir, offline: "Aggregate: 0.9")
    with pytest.raises(render_module.ReportRefused, match="aggregate"):
        render_module.render_all([render_module.LmeRun(run_dir=tmp_path)])


def test_r5_render_all_offline_guard_actually_wraps_the_composition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R5: a renderer that tries to reach the network is refused -- the outer
    offline_guard genuinely wraps the composition, not just decoratively."""

    import reports.render as render_module

    def _sneaky(run_dir, offline):  # type: ignore[no-untyped-def]
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(1.0)
        with probe:
            probe.connect(("127.0.0.1", 9))
        return "unreachable"

    monkeypatch.setattr(render_module, "render_run_report", _sneaky)
    with pytest.raises(OSError, match="offline report generation forbids socket.connect"):
        render_module.render_all([render_module.LmeRun(run_dir=tmp_path)])


def test_r5_no_real_socket_call_is_ever_reached_by_a_genuinely_unpatched_render(tmp_path: Path) -> None:
    """R5, other half: F3 -- the prior version of this test claimed "genuine
    (non-monkeypatched)" while actually patching `render_run_report` with a
    tracking wrapper, which never exercises the real code path. This one
    patches nothing in `reports.render` at all: it runs a real LME fixture
    run through the real `render_run_report`, and proves the real path never
    reaches a live socket by installing a raising sentinel on
    `socket.socket.connect` *before* the call -- if anything inside
    render_all's real, unpatched composition ever reached a real connect, the
    sentinel (not offline_guard's own patch) would be hit and raise."""

    from reports.render import LmeRun, render_all

    result = _lme_run(tmp_path, "r5-genuinely-unpatched")
    original_connect = socket.socket.connect
    sentinel_calls: list[tuple] = []

    def _sentinel(self, address):  # type: ignore[no-untyped-def]
        sentinel_calls.append(address)
        raise AssertionError(f"reached the real socket stack: connect({address!r})")

    socket.socket.connect = _sentinel
    try:
        rendered = render_all([LmeRun(run_dir=result.run_dir)])
    finally:
        socket.socket.connect = original_connect

    assert "| Ability | Variant | Questions |" in rendered
    assert not sentinel_calls, "render_all must never reach outside offline_guard's own patch"


def test_sole_reused_offline_guard_identity() -> None:
    """RM8-style pin: render.py must reuse protocol.offline.offline_guard,
    never a lookalike copy."""

    from protocol.offline import offline_guard as shared_offline_guard
    from reports import render as render_module

    assert render_module.offline_guard is shared_offline_guard
