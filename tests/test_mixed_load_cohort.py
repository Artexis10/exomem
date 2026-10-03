"""Outcome and maximum-duration contracts for the mixed-load cohort runner."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("mixed_load_cohort", SCRIPTS / "mixed_load_cohort.py")
cohort = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cohort
assert spec.loader is not None
spec.loader.exec_module(cohort)


@pytest.mark.parametrize(
    ("status", "exit_code"),
    [("pass", 0), ("fail", 1)],
)
def test_cohort_accepts_only_declared_observed_outcomes(status: str, exit_code: int) -> None:
    assert cohort.accepted_outcome(status, exit_code) is True


@pytest.mark.parametrize(
    ("status", "exit_code"),
    [("pass", 1), ("fail", 0), ("invalid", 1), ("invalid", 2), ("pass", 2)],
)
def test_cohort_rejects_mismatched_or_invalid_outcomes(status: str, exit_code: int) -> None:
    assert cohort.accepted_outcome(status, exit_code) is False


def test_cohort_declares_budget_for_six_outer_timeouts_and_job_overhead() -> None:
    assert cohort.MAX_RUNS == 6
    assert cohort.TOTAL_MAX_BUDGET_S == cohort.MAX_RUNS * cohort.OUTER_TIMEOUT_S + cohort.OVERHEAD_BUDGET_S
    assert cohort.TOTAL_MAX_BUDGET_S < 220 * 60


def test_workflow_limits_same_repository_prs_and_retains_transport_stdio() -> None:
    workflow = (SCRIPTS.parent / ".github/workflows/mixed-load-benchmark.yml").read_text(encoding="utf-8")
    assert "github.event.pull_request.head.repo.full_name == github.repository" in workflow
    assert "startsWith(github.head_ref, 'perf/mixed-load-')" in workflow
    assert "timeout-minutes: 220" in workflow
    assert "${{ runner.temp }}/mixed-load-cohort/*/state/stdio.log" in workflow


def test_cohort_retains_a_malformed_result_as_invalid_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outputs: list[Path] = []

    def check_output(command: list[str], **_kwargs: object) -> str:
        return "revision\n" if command[1] == "rev-parse" else ""

    class Process:
        def __init__(self, command: list[str], **_kwargs: object) -> None:
            self.output = Path(command[command.index("--output") + 1])
            self.pid = 123
            outputs.append(self.output)

        def wait(self, timeout: float | None = None) -> int:
            if len(outputs) == 1:
                self.output.write_text("{", encoding="utf-8")
                return 1
            self.output.write_text('{"status": "pass", "errors": []}', encoding="utf-8")
            return 0

    monkeypatch.setattr(cohort.subprocess, "check_output", check_output)
    monkeypatch.setattr(cohort.subprocess, "Popen", Process)
    monkeypatch.setattr(cohort.os, "getloadavg", lambda: (0.0, 0.0, 0.0))

    output = tmp_path / "cohort"
    assert cohort.main(["--output", str(output), "--pairs", "1"]) == 1

    manifest = json.loads((output / "cohort.json").read_text(encoding="utf-8"))
    assert len(outputs) == 2
    assert [run["status"] for run in manifest["runs"]] == ["invalid", "pass"]
    assert manifest["collection_status"] == "invalid"
