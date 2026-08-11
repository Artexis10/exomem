from __future__ import annotations

from pathlib import Path

import yaml


def test_lean_python_lanes_have_time_bounds_and_versioned_timing_evidence() -> None:
    workflow = yaml.safe_load(
        (Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    )
    job = workflow["jobs"]["test"]

    assert job["timeout-minutes"] == 30
    run_step = next(step for step in job["steps"] if step.get("name", "").startswith("Run tests"))
    assert "--session-timeout=1500" in run_step["run"]
    assert "--durations=50" in run_step["run"]
    assert "--durations-min=1" in run_step["run"]
    assert "--junitxml=test-results/junit-${{ matrix.python-version }}.xml" in run_step["run"]

    artifact_step = next(step for step in job["steps"] if step.get("uses") == "actions/upload-artifact@v4")
    assert artifact_step["if"] == "always()"
    assert "${{ matrix.python-version }}" in artifact_step["with"]["name"]
    assert artifact_step["with"]["path"] == "test-results/junit-${{ matrix.python-version }}.xml"
    assert artifact_step["with"]["if-no-files-found"] == "warn"
