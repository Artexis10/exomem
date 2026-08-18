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


def test_every_package_install_is_bounded_and_retried() -> None:
    """A stalled mirror must fail fast and by name, not eat a job's budget.

    `apt-get` blocks forever on a mirror that accepts the connection and then
    stops sending. The isolation-runtime step in `ci.yml` had no bound of its
    own, so one dark mirror held it for 29m26s, exhausted the job's 30-minute
    timeout, and ended the shard `cancelled`. `gate` requires every dependency
    to be `success`, so that read as a failing required check and blocked a
    release twice -- for an outage that never reached a test.

    Both halves are load-bearing and this pins both. `scripts/ci-apt-install.sh`
    bounds each request and retries the fetch; the step timeout is the backstop
    for anything the helper's own bounds miss. A direct `apt-get` in a workflow
    reintroduces the unbounded case, so it fails here rather than in a release.
    """
    offenders: list[str] = []
    workflows = sorted(
        path
        for suffix in ("*.yml", "*.yaml")
        for path in (ROOT / ".github/workflows").glob(suffix)
    )
    assert workflows, "no workflows found; this contract would pass vacuously"

    installs = 0
    for path in workflows:
        workflow = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job_name, job in (workflow.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                run = step.get("run") or ""
                if "apt-get" not in run and "ci-apt-install.sh" not in run:
                    continue
                installs += 1
                where = f"{path.name}::{job_name}::{step.get('name', '<unnamed step>')}"
                if "apt-get" in run:
                    offenders.append(
                        f"{where} calls apt-get directly; route it through "
                        "scripts/ci-apt-install.sh so the fetch is bounded"
                    )
                if not isinstance(step.get("timeout-minutes"), int):
                    offenders.append(
                        f"{where} installs packages with no step timeout-minutes; "
                        "without one it inherits the job budget and a stall "
                        "cancels the job instead of failing it"
                    )

    assert installs, "no package installs found; this contract would pass vacuously"
    assert not offenders, "unbounded package installs:\n  " + "\n  ".join(offenders)


def test_the_bounded_install_helper_sets_request_timeouts_and_retries() -> None:
    """The helper is the only thing standing between CI and an infinite fetch."""
    helper = ROOT / "scripts/ci-apt-install.sh"
    assert helper.is_file()
    script = helper.read_text(encoding="utf-8")

    # A per-request bound, so a silent stall cannot outlast one timeout.
    assert "Acquire::http::Timeout=15" in script
    assert "Acquire::https::Timeout=15" in script
    # ...and a retry, because a mirror that refuses outright is a different
    # failure from one that stalls, and the per-request bound does not fix it.
    assert "Acquire::Retries=3" in script
    assert "for attempt in 1 2 3" in script
    assert "set -euo pipefail" in script


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
