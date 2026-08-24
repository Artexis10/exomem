from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def _workflow() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))


def _cross_platform_workflow() -> dict:
    return yaml.safe_load(
        (ROOT / ".github/workflows/cross-platform.yml").read_text(encoding="utf-8")
    )


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


def test_the_bounded_install_helper_bounds_each_attempt_not_just_the_loop() -> None:
    """The helper is the only thing standing between CI and an infinite fetch.

    `Acquire::*::Timeout` alone is not that bound, and this is measured: with
    those options set, a fetch still hung for the entire five minutes its caller
    allowed. They govern individual socket operations, so a mirror that dribbles
    or stalls off-socket never trips them.

    The external `timeout` is what actually ends an attempt, and the retry loop
    only means something because of it -- bound the loop instead of the attempt
    and the first stall consumes every retry, which is the bug this file's first
    version shipped. `timeout` must sit inside `sudo` so the kill reaches the
    root-owned `apt-get`; kill `sudo` instead and the child survives holding the
    dpkg lock, so the retry fails on the lock rather than reaching a mirror.
    """
    helper = ROOT / "scripts/ci-apt-install.sh"
    assert helper.is_file()
    script = helper.read_text(encoding="utf-8")

    assert "set -euo pipefail" in script
    # The bound that actually works, ordered so root can signal root.
    assert "sudo timeout --kill-after=10" in script
    assert "sudo apt-get" not in script, (
        "every apt-get must run under the bounded `sudo timeout ... apt-get` "
        "form; a bare `sudo apt-get` is the unbounded fetch again"
    )
    # Retries, which only help once an attempt can end on its own.
    assert 'CI_APT_ATTEMPTS:=3' in script
    assert 'CI_APT_UPDATE_TIMEOUT_SECONDS:=' in script
    assert 'CI_APT_INSTALL_TIMEOUT_SECONDS:=' in script
    # Kept as a cheap inner bound; useful, just never sufficient.
    assert "Acquire::Retries=3" in script
    assert "Acquire::http::Timeout=15" in script
    assert "Acquire::https::Timeout=15" in script


def test_the_install_helper_budget_fits_inside_its_caller_timeout() -> None:
    """A step cap below the helper's worst case would cut retries off again."""
    script = (ROOT / "scripts/ci-apt-install.sh").read_text(encoding="utf-8")

    def _default(name: str) -> int:
        match = re.search(rf'\$\{{{name}:=(\d+)\}}', script)
        assert match, f"{name} has no default in the helper"
        return int(match.group(1))

    worst_case_seconds = (
        _default("CI_APT_ATTEMPTS") * _default("CI_APT_UPDATE_TIMEOUT_SECONDS")
        + _default("CI_APT_INSTALL_TIMEOUT_SECONDS")
    )

    workflow = _workflow()
    step = next(
        step
        for step in workflow["jobs"]["test"]["steps"]
        if step.get("name") == "Install process-isolation runtime"
    )
    cap_seconds = step["timeout-minutes"] * 60
    assert cap_seconds > worst_case_seconds, (
        f"the step cap is {cap_seconds}s but the helper can legitimately spend "
        f"{worst_case_seconds}s, so a healthy retry would be killed as a stall"
    )
    assert cap_seconds < workflow["jobs"]["test"]["timeout-minutes"] * 60, (
        "the step cap must stay under the job budget it exists to protect"
    )


def test_retrieval_gates_run_as_three_independent_jobs() -> None:
    jobs = _workflow()["jobs"]

    assert "retrieval-eval" not in jobs
    assert "pytest -m embeddings -v" in jobs["retrieval-quality"]["steps"][-1]["run"]
    assert "tests/test_latency_gate.py -q" in jobs["retrieval-latency"]["steps"][-1]["run"]
    semantic_command = jobs["semantic-write-latency"]["steps"][-1]["run"]
    assert "scripts/semantic_write_latency.py --check" in semantic_command


def test_governance_wire_characterization_runs_every_registered_route() -> None:
    workflow = _workflow()
    job = workflow["jobs"]["governance-projection-wire"]

    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == 8
    assert job["strategy"]["fail-fast"] is False
    assert set(job["strategy"]["matrix"]["route"]) == {
        "keyword",
        "bm25",
        "vector-hard-off",
        "rerank-hard-off",
        "clip-hard-off",
        "graph",
        "graph-rerank-hard-off",
        "max-query",
        "max-limit",
        "max-shape",
        "hidden-index-missing",
        "pagination",
    }
    command = job["steps"][-1]["run"]
    assert "--python 3.13" in command
    assert "tests/test_governance_projection_wire_gate.py" in command
    assert job["steps"][-1]["env"]["EXOMEM_GOVERNANCE_TIMING_ROUTE"] == (
        "${{ matrix.route }}"
    )
    assert job["steps"][-1]["env"]["EXOMEM_DISABLE_EMBEDDINGS"] == "1"
    assert job["steps"][-1]["env"]["EXOMEM_DISABLE_CLIP"] == "1"
    assert job["steps"][-1]["env"]["EXOMEM_DISABLE_RANKING"] == "1"
    assert "governance-projection-wire" in workflow["jobs"]["gate"]["needs"]


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


def test_native_held_filesystem_has_a_required_ntfs_gate() -> None:
    workflow = _workflow()
    job = workflow["jobs"]["windows-held-filesystem"]

    assert job["runs-on"] == "windows-latest"
    assert job["timeout-minutes"] == 12
    preparation = next(
        step for step in job["steps"] if step.get("name") == "Require NTFS and 8.3 aliases"
    )["run"]
    assert "Get-Volume" in preparation
    assert 'FileSystem -ne "NTFS"' in preparation
    assert "at least two writable NTFS volumes" in preparation
    assert "fsutil.exe 8dot3name set" in preparation
    assert "$LASTEXITCODE" in preparation

    command = next(
        step for step in job["steps"] if step.get("name") == "Native held-filesystem gate"
    )["run"]
    assert "--python 3.13" in command
    assert "tests/test_held_fs_contract.py" in command
    assert "tests/test_held_fs_windows.py" in command
    assert "--session-timeout=600" in command
    assert "windows-held-filesystem" in workflow["jobs"]["gate"]["needs"]


def test_the_cross_platform_split_count_matches_its_shard_matrix() -> None:
    """A mismatch here loses coverage without losing green.

    `--splits=N` and the shard matrix are two independent numbers that have to
    agree. Raise the matrix without the flag and the extra groups error; raise
    the flag without the matrix and the tests in the missing groups are simply
    never run, on a lane that still reports success. The second failure is
    invisible, which is why it is asserted rather than reviewed.
    """
    job = _cross_platform_workflow()["jobs"]["suite"]
    shards = job["strategy"]["matrix"]["shard"]
    run_step = next(step for step in job["steps"] if step.get("name", "").startswith("Run tests"))

    assert shards == list(range(1, len(shards) + 1))
    assert f"--splits={len(shards)}" in run_step["run"]
    assert "--group=${{ matrix.shard }}" in run_step["run"]
    assert job["name"].endswith(f"shard ${{{{ matrix.shard }}}}/{len(shards)})")


def test_the_release_bot_branch_does_not_launch_the_cross_platform_matrix() -> None:
    """Its diff is a version string; `main` runs this lane on push regardless.

    The bot force-pushes that branch on every merge to main, and each push
    relaunched the whole matrix into a queue real PRs were waiting in. macOS
    runners cap at five concurrent jobs for a public repo on the free tier, so
    those launches were not spare capacity -- they were the queue.
    """
    condition = " ".join(_cross_platform_workflow()["jobs"]["suite"]["if"].split())

    assert "release-please--branches--" in condition
    # Skipped for that branch's PR, never for a push to main.
    assert "github.event_name != 'pull_request'" in condition


def test_the_capped_runner_is_paid_on_merge_not_on_every_pull_request() -> None:
    """macOS caps at five concurrent jobs; a four-shard matrix cannot fit twice.

    Two open PRs want eight macOS jobs against that cap, so the second run's
    shards queue -- and a run's jobs are scheduled together, so the whole second
    run stalls behind the first. That is a throughput cliff with no visible
    symptom: every run still goes green, just later and later, and the obvious
    reading is "CI is slow" rather than "these runs cannot overlap".

    Asserted rather than reviewed because the tempting edit -- putting macOS
    back on the pull_request arm "for parity" -- reintroduces the cliff while
    looking like strictly more coverage.
    """
    matrix = _cross_platform_workflow()["jobs"]["suite"]["strategy"]["matrix"]
    expression = " ".join(str(matrix["os"]).split())
    pull_request_arm, _, push_arm = expression.partition("||")

    assert "github.event_name == 'pull_request'" in pull_request_arm
    assert "windows-latest" in pull_request_arm
    assert "macos-latest" not in pull_request_arm, (
        "the capped runner is back on every PR; two concurrent PRs will serialise"
    )
    # A push to main still runs both, so no commit stops reaching macOS -- it
    # reaches it one merge later, on a lane that is advisory by design.
    assert "macos-latest" in push_arm
    assert "windows-latest" in push_arm
