from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def _workflow() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))


def test_lean_python_lanes_have_time_bounds_and_versioned_timing_evidence() -> None:
    workflow = _workflow()
    job = workflow["jobs"]["test"]

    assert job["timeout-minutes"] == 30
    run_step = next(step for step in job["steps"] if step.get("name", "").startswith("Run tests"))
    assert "--session-timeout=1500" in run_step["run"]
    assert "--durations=50" in run_step["run"]
    assert "--durations-min=1" in run_step["run"]
    assert (
        "--junitxml=test-results/junit-${{ matrix.python-version }}-shard-${{ matrix.shard }}.xml"
        in run_step["run"]
    )

    artifact_step = next(
        step for step in job["steps"] if step.get("uses") == "actions/upload-artifact@v4"
    )
    assert artifact_step["if"] == "always()"
    assert "${{ matrix.python-version }}" in artifact_step["with"]["name"]
    assert artifact_step["with"]["path"] == (
        "test-results/junit-${{ matrix.python-version }}-shard-${{ matrix.shard }}.xml"
    )
    assert artifact_step["with"]["if-no-files-found"] == "warn"


def test_lean_python_lanes_use_four_duration_balanced_isolated_shards() -> None:
    workflow = _workflow()
    job = workflow["jobs"]["test"]
    matrix = job["strategy"]["matrix"]

    assert matrix["python-version"] == ["3.11", "3.13"]
    assert matrix["shard"] == [1, 2, 3, 4]
    assert job["name"] == "tests (py${{ matrix.python-version }}, shard ${{ matrix.shard }}/4)"

    run_step = next(step for step in job["steps"] if step.get("name", "").startswith("Run tests"))
    command = run_step["run"]
    assert "--splits=4" in command
    assert "--group=${{ matrix.shard }}" in command
    assert "--splitting-algorithm=least_duration" in command
    assert "--durations-path=.test_durations.json" in command

    artifact_step = next(
        step for step in job["steps"] if step.get("uses") == "actions/upload-artifact@v4"
    )
    assert "${{ matrix.shard }}" in artifact_step["with"]["name"]
    assert "${{ matrix.shard }}" in artifact_step["with"]["path"]

    smoke_step = next(
        step for step in job["steps"] if step.get("name", "").startswith("Sample vault")
    )
    assert smoke_step["if"] == "matrix.shard == 1"
    assert (ROOT / ".test_durations.json").is_file()


def test_retrieval_gates_run_as_three_independent_jobs() -> None:
    jobs = _workflow()["jobs"]

    assert "retrieval-eval" not in jobs
    assert "pytest -m embeddings -v" in jobs["retrieval-quality"]["steps"][-1]["run"]
    assert "tests/test_latency_gate.py -q" in jobs["retrieval-latency"]["steps"][-1]["run"]
    semantic_command = jobs["semantic-write-latency"]["steps"][-1]["run"]
    assert "scripts/semantic_write_latency.py --check" in semantic_command


def test_superseded_pr_runs_cancel_and_one_stable_gate_requires_every_ci_job() -> None:
    workflow = _workflow()

    assert workflow["concurrency"] == {
        "group": "ci-${{ github.event.pull_request.number || github.ref }}",
        "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
    }

    gate = workflow["jobs"]["gate"]
    assert gate["name"] == "required CI gate"
    assert gate["if"] == "${{ !cancelled() }}"
    assert set(gate["needs"]) == set(workflow["jobs"]) - {"gate"}
    assert gate["steps"][0]["env"]["RESULTS"] == "${{ join(needs.*.result, ' ') }}"
