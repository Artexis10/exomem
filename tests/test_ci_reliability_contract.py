from __future__ import annotations

import heapq
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]

FULL_CI_JOBS = {
    "retrieval-quality",
    "retrieval-latency",
    "governance-projection-wire",
    "governance-projection-wire-vector-cpu",
    "semantic-write-latency",
    "graph-convergence",
    "docker",
}

#: The two PR test tiers: (job id, shard count, EXOMEM_TEST_TIER value).
#: Together with the nightly-owned tests/test_latency_gate.py (the
#: `retrieval-latency` job) they cover exactly the previous lean suite —
#: pinned below by test_pr_tiers_partition_the_lean_suite_exactly.
PR_TIER_JOBS = (
    ("core-tests", 6, "core"),
    ("harness-tests", 4, "harness"),
)

HARNESS_MODULES_FILE = ROOT / "tests" / "harness_modules.txt"
NIGHTLY_OWNED_MODULE = "tests/test_latency_gate.py"


def _triggers(workflow: dict) -> dict:
    # PyYAML 1.1 parses the unquoted workflow key `on` as boolean true.
    return workflow.get("on") or workflow[True]


def _workflow_text() -> str:
    return (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")


def _workflow() -> dict:
    return yaml.safe_load(_workflow_text())


def _cross_platform_workflow() -> dict:
    return yaml.safe_load(
        (ROOT / ".github/workflows/cross-platform.yml").read_text(encoding="utf-8")
    )


def _run_step(job: dict) -> dict:
    return next(step for step in job["steps"] if step.get("name", "").startswith("Run tests"))


def _harness_modules_from_file() -> list[str]:
    lines = HARNESS_MODULES_FILE.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


@pytest.mark.parametrize("job_id,shards,tier", PR_TIER_JOBS)
def test_pr_tier_lanes_have_time_bounds_and_versioned_timing_evidence(
    job_id: str, shards: int, tier: str
) -> None:
    workflow = _workflow()
    job = workflow["jobs"][job_id]

    assert job["timeout-minutes"] == 30
    run_step = _run_step(job)
    assert "--session-timeout=900" in run_step["run"]
    assert "--durations=50" in run_step["run"]
    assert "--durations-min=1" in run_step["run"]
    assert (
        f"--junitxml=test-results/junit-{tier}-"
        "${{ matrix.python-version }}-shard-${{ matrix.shard }}.xml"
    ) in run_step["run"]
    assert run_step["env"]["EXOMEM_TEST_TIER"] == tier

    junit_step = next(
        step for step in job["steps"] if step.get("name") == "Upload test timing evidence"
    )
    assert junit_step["uses"] == "actions/upload-artifact@v4"
    assert junit_step["if"] == "always()"
    assert junit_step["with"]["name"] == (
        f"{tier}-py${{{{ matrix.python-version }}}}-shard-${{{{ matrix.shard }}}}-junit"
    )
    assert junit_step["with"]["path"] == (
        f"test-results/junit-{tier}-"
        "${{ matrix.python-version }}-shard-${{ matrix.shard }}.xml"
    )
    assert junit_step["with"]["if-no-files-found"] == "warn"


@pytest.mark.parametrize("job_id,shards,tier", PR_TIER_JOBS)
def test_pr_tier_lanes_use_duration_balanced_isolated_shards(
    job_id: str, shards: int, tier: str
) -> None:
    workflow = _workflow()
    job = workflow["jobs"][job_id]
    matrix = job["strategy"]["matrix"]

    # PRs pay for the newest runtime only; the compatibility runtime joins on
    # nightly/manual full runs. The release PR runs this same PR tier — the
    # release-please clause is deliberately gone (see
    # test_expensive_ci_runs_nightly_and_manually_only).
    versions = " ".join(str(matrix["python-version"]).split())
    assert "github.event_name == 'schedule'" in versions
    assert "github.event_name == 'workflow_dispatch'" in versions
    assert "release-please" not in versions
    assert "head_ref" not in versions
    assert '["3.11", "3.13"]' in versions
    assert '["3.13"]' in versions

    assert matrix["shard"] == list(range(1, shards + 1))
    assert job["name"] == (
        f"{tier} tests (py${{{{ matrix.python-version }}}}, "
        f"shard ${{{{ matrix.shard }}}}/{shards})"
    )

    command = _run_step(job)["run"]
    assert f"--splits={shards}" in command
    assert "--group=${{ matrix.shard }}" in command
    assert "--splitting-algorithm=least_duration" in command
    assert "--durations-path=.test_durations.json" in command

    # The tier suites share the environment the lean lane always had: full
    # history for the benchmark contract receipts, the pinned Bun runtime,
    # the bounded bubblewrap install, and a current lockfile.
    checkout = next(step for step in job["steps"] if step.get("uses") == "actions/checkout@v4")
    assert checkout["with"]["fetch-depth"] == 0
    assert any(step.get("name") == "Install pinned Bun runtime" for step in job["steps"])
    assert any(
        step.get("name") == "Install process-isolation runtime" for step in job["steps"]
    )
    assert any(step.get("name") == "Verify lockfile is current" for step in job["steps"])

    assert (ROOT / ".test_durations.json").is_file()
    assert HARNESS_MODULES_FILE.is_file()

    smoke_steps = [
        step for step in job["steps"] if step.get("name", "").startswith("Sample vault")
    ]
    if job_id == "core-tests":
        assert len(smoke_steps) == 1
        assert smoke_steps[0]["if"] == "matrix.shard == 1"
    else:
        assert smoke_steps == []


@pytest.mark.parametrize("job_id,shards,tier", PR_TIER_JOBS)
def test_nightly_full_runs_store_and_upload_refreshed_durations(
    job_id: str, shards: int, tier: str
) -> None:
    """The durations file must be refreshable from evidence, never hand-edited.

    The file froze for two weeks once (#425): 42% of cases were unrecorded and
    pytest-split predicted 388 s for a shard that ran 970 s. Nightly/dispatch
    full runs now measure durations (`--store-durations`) and upload the
    per-shard result as an artifact; `scripts/refresh_test_durations.py`
    applies downloaded artifacts locally as a reviewed change. CI itself never
    commits durations.
    """
    job = _workflow()["jobs"][job_id]
    command = _run_step(job)["run"]
    store = " ".join(command.split())
    assert (
        "${{ (github.event_name == 'schedule' || "
        "github.event_name == 'workflow_dispatch') && '--store-durations' || '' }}"
    ) in store

    durations_step = next(
        step for step in job["steps"] if step.get("name") == "Upload refreshed test durations"
    )
    assert durations_step["uses"] == "actions/upload-artifact@v4"
    condition = " ".join(str(durations_step["if"]).split())
    assert "github.event_name == 'schedule'" in condition
    assert "github.event_name == 'workflow_dispatch'" in condition
    assert durations_step["with"]["name"] == (
        f"test-durations-{tier}-py${{{{ matrix.python-version }}}}"
        "-shard-${{ matrix.shard }}"
    )
    assert durations_step["with"]["path"] == ".test_durations.json"

    assert (ROOT / "scripts/refresh_test_durations.py").is_file()


def test_harness_module_list_is_pinned_to_the_import_derivation() -> None:
    """tests/harness_modules.txt cannot silently diverge from the imports.

    The harness tier runs exactly the benchmark-importing test modules; the
    core tier ignores exactly that list. The list is checked in (CI must not
    depend on a generator running), and this pin re-derives it by import
    inspection so a new benchmark-importing test module fails here — loudly,
    with the regeneration command — instead of silently landing in the wrong
    tier.
    """
    script = ROOT / "scripts" / "generate_harness_modules.py"
    assert script.is_file(), "the harness-module derivation script must exist"
    spec = importlib.util.spec_from_file_location("generate_harness_modules_under_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    derived = module.derive_harness_modules()
    assert HARNESS_MODULES_FILE.is_file(), (
        "tests/harness_modules.txt is missing; generate it with "
        "`uv run python scripts/generate_harness_modules.py --write`"
    )
    assert _harness_modules_from_file() == derived, (
        "tests/harness_modules.txt is stale; regenerate with "
        "`uv run python scripts/generate_harness_modules.py --write`"
    )
    # ~109 modules at introduction. A collapse of the derivation (a rename of
    # the benchmarks dir, a broken AST walk) must read as red, not as an
    # empty-but-green tier.
    assert len(derived) >= 100
    assert NIGHTLY_OWNED_MODULE not in derived
    for module_path in derived:
        assert (ROOT / module_path).is_file(), f"{module_path} is listed but does not exist"


def _collect_node_ids(label: str, tier: str | None, extra_args: list[str]) -> set[str]:
    env = os.environ.copy()
    env.pop("EXOMEM_TEST_TIER", None)
    env.pop("EXOMEM_VAULT_PATH", None)
    if tier is not None:
        env["EXOMEM_TEST_TIER"] = tier
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            *extra_args,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert result.returncode == 0, (
        f"{label} collection failed (rc={result.returncode}):\n"
        f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
    )
    nodes = {
        line.strip()
        for line in result.stdout.splitlines()
        if line.startswith("tests/") and "::" in line
    }
    assert nodes, f"{label} collection yielded no test nodes"
    return nodes


@pytest.mark.timeout(1800)
def test_pr_tiers_partition_the_lean_suite_exactly() -> None:
    """core ∪ harness ∪ (nightly-owned latency file) == the previous lean suite.

    The tier split must lose nothing and duplicate nothing: every test the
    lean lane used to run is in exactly one of the core tier, the harness
    tier, or the nightly `retrieval-latency` job's file. Asserted over real
    pytest collections — the same mechanism CI runs — rather than over the
    module lists that configure them, so a tier-selection defect in
    tests/conftest.py fails here and not in coverage.
    """
    full = _collect_node_ids("full lean suite", None, [])
    core = _collect_node_ids("core tier", "core", [])
    harness = _collect_node_ids("harness tier", "harness", [])
    latency = _collect_node_ids("nightly latency file", None, [NIGHTLY_OWNED_MODULE])

    assert core | harness | latency == full, (
        f"tier union misses {len(full - (core | harness | latency))} node(s) and "
        f"adds {len((core | harness | latency) - full)} node(s)"
    )
    assert not core & harness, f"{len(core & harness)} node(s) collected by both PR tiers"
    assert not core & latency, "the core tier still collects the nightly-owned latency file"
    assert not harness & latency, "the harness tier collects the nightly-owned latency file"


def test_an_unknown_test_tier_fails_collection_loudly() -> None:
    """A typo'd EXOMEM_TEST_TIER must refuse to collect, not run some suite.

    Silently ignoring an unknown tier value would run the full suite in a job
    that believes it ran one tier — green, with the split quietly gone.
    """
    env = os.environ.copy()
    env.pop("EXOMEM_VAULT_PATH", None)
    env["EXOMEM_TEST_TIER"] = "bogus"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            "tests/test_uv_lock_policy.py",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode != 0, "an unknown tier value collected anyway"
    assert "not a CI tier" in result.stdout + result.stderr


def _predicted_max_shard_seconds(
    durations: dict[str, float], members: set[str], splits: int
) -> float:
    """pytest-split least_duration over the recorded durations of one tier."""
    items = sorted(
        ((node, seconds) for node, seconds in durations.items() if node.split("::", 1)[0] in members),
        key=lambda pair: pair[1],
        reverse=True,
    )
    assert items, "no recorded durations matched the tier; the prediction would be vacuous"
    heap: list[tuple[float, int]] = [(0.0, index) for index in range(splits)]
    heapq.heapify(heap)
    for _node, seconds in items:
        total, index = heapq.heappop(heap)
        heapq.heappush(heap, (total + seconds, index))
    return max(total for total, _index in heap)


def test_session_timeouts_carry_headroom_over_predicted_tier_runtime() -> None:
    """Each tier's --session-timeout holds ≥1.5x its predicted busiest shard.

    Derived from the canonical inputs — the refreshed durations file, the
    harness module list, and the shard counts in ci.yml — rather than
    restated, so re-sharding or a durations refresh that outgrows the timeout
    fails here instead of as a mid-run session kill.
    """
    durations = json.loads((ROOT / ".test_durations.json").read_text(encoding="utf-8"))
    harness_members = set(_harness_modules_from_file())
    all_members = {node.split("::", 1)[0] for node in durations}
    core_members = all_members - harness_members - {NIGHTLY_OWNED_MODULE}

    workflow = _workflow()
    for job_id, shards, tier in PR_TIER_JOBS:
        command = _run_step(workflow["jobs"][job_id])["run"]
        match = re.search(r"--session-timeout=(\d+)", command)
        assert match, f"{job_id} sets no --session-timeout"
        timeout = int(match.group(1))
        members = harness_members if tier == "harness" else core_members
        predicted = _predicted_max_shard_seconds(durations, members, shards)
        assert timeout >= 1.5 * predicted, (
            f"{job_id}: --session-timeout={timeout}s is under 1.5x the predicted "
            f"busiest shard ({predicted:.0f}s); raise the timeout or add shards"
        )
        assert timeout < workflow["jobs"][job_id]["timeout-minutes"] * 60, (
            f"{job_id}: the session timeout must stay inside the job budget"
        )


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


@pytest.mark.parametrize("job_id", [job_id for job_id, _shards, _tier in PR_TIER_JOBS])
def test_the_install_helper_budget_fits_inside_its_caller_timeout(job_id: str) -> None:
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
        for step in workflow["jobs"][job_id]["steps"]
        if step.get("name") == "Install process-isolation runtime"
    )
    cap_seconds = step["timeout-minutes"] * 60
    assert cap_seconds > worst_case_seconds, (
        f"the step cap is {cap_seconds}s but the helper can legitimately spend "
        f"{worst_case_seconds}s, so a healthy retry would be killed as a stall"
    )
    assert cap_seconds < workflow["jobs"][job_id]["timeout-minutes"] * 60, (
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


def test_governance_vector_cpu_wire_characterization_is_exact_and_required() -> None:
    workflow = _workflow()
    job = workflow["jobs"]["governance-projection-wire-vector-cpu"]

    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == 12
    assert job["strategy"]["fail-fast"] is False
    assert set(job["strategy"]["matrix"]["route"]) == {
        "keyword",
        "bm25",
        "vector-live",
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
    step = job["steps"][-1]
    command = step["run"]
    assert "--extra embeddings" in command
    assert "--python 3.13" in command
    assert "tests/test_governance_projection_wire_gate.py" in command
    assert step["env"] == {
        "EXOMEM_GOVERNANCE_TIMING_PROFILE": "vectors-cpu-torch-v1",
        "EXOMEM_GOVERNANCE_TIMING_ROUTE": "${{ matrix.route }}",
        "EXOMEM_DISABLE_MEDIA_EXTRACTION": "1",
        "EXOMEM_DISABLE_CLIP": "1",
        "EXOMEM_DISABLE_RANKING": "1",
        "EXOMEM_DEVICE": "cpu",
        "EXOMEM_EMBED_BACKEND": "torch",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    assert "governance-projection-wire-vector-cpu" in workflow["jobs"]["gate"][
        "needs"
    ]


def test_expensive_ci_runs_nightly_and_manually_only() -> None:
    """Full-CI evidence comes from the schedule and explicit dispatch — only.

    The guard used to include `head_ref == 'release-please--branches--main'`,
    which re-ran the full 46-job tier on every push to main (release-please
    force-updates its PR each time): 61% of a measured day's runner-minutes
    went to those stampedes while ordinary PRs queued behind a 20-concurrent-
    job account cap. The release PR now runs the normal PR tier like any other
    PR; release evidence is one green manually-dispatched full run on main,
    documented as a required step in docs/release.md.
    """
    workflow = _workflow()
    triggers = _triggers(workflow)

    assert triggers["schedule"] == [{"cron": "17 2 * * *"}]
    assert "workflow_dispatch" in triggers

    jobs = workflow["jobs"]
    assert FULL_CI_JOBS < set(jobs)
    conditions = {
        " ".join(str(jobs[name].get("if", "")).split()) for name in FULL_CI_JOBS
    }
    assert len(conditions) == 1
    condition = conditions.pop()
    assert "github.event_name == 'schedule'" in condition
    assert "github.event_name == 'workflow_dispatch'" in condition
    assert "pull_request" not in condition
    assert "head_ref" not in condition
    assert "release-please" not in condition

    # The clause must not survive anywhere in the workflow — not in a job
    # guard, not in the python matrix, not in the gate's FULL_CI expression.
    assert "release-please--branches--main" not in _workflow_text()

    fast_jobs = set(jobs) - FULL_CI_JOBS - {"gate"}
    assert fast_jobs
    assert all("if" not in jobs[name] for name in fast_jobs)


def test_superseded_pr_runs_cancel_and_one_stable_gate_covers_both_tiers() -> None:
    workflow = _workflow()

    assert workflow["concurrency"] == {
        "group": (
            "ci-${{ (github.event_name == 'schedule' || "
            "github.event_name == 'workflow_dispatch') && "
            "format('full-{0}', github.ref) || "
            "github.event.pull_request.number || github.ref }}"
        ),
        "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
    }

    gate = workflow["jobs"]["gate"]
    assert gate["name"] == "required CI gate"
    assert gate["if"] == "${{ !cancelled() }}"
    assert set(gate["needs"]) == set(workflow["jobs"]) - {"gate"}
    assert {"core-tests", "harness-tests"} <= set(gate["needs"])
    assert "test" not in gate["needs"]
    assert gate["steps"][0]["env"]["RESULTS"] == "${{ join(needs.*.result, ' ') }}"
    full_ci = " ".join(str(gate["steps"][0]["env"]["FULL_CI"]).split())
    assert "github.event_name == 'schedule'" in full_ci
    assert "github.event_name == 'workflow_dispatch'" in full_ci
    assert "release-please" not in full_ci
    command = gate["steps"][0]["run"]
    assert 'test "$result" = success' in command
    assert 'test "$result" = skipped' in command
    assert 'test "$FULL_CI" != true' in command


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


def test_cross_platform_matrix_runs_nightly_and_manually_only() -> None:
    workflow = _cross_platform_workflow()
    triggers = _triggers(workflow)

    assert "push" not in triggers
    assert triggers["schedule"] == [{"cron": "41 2 * * *"}]
    assert "workflow_dispatch" in triggers
    assert "pull_request" in triggers

    job = workflow["jobs"]["suite"]
    condition = " ".join(job["if"].split())
    assert "github.event_name == 'schedule'" in condition
    assert "github.event_name == 'workflow_dispatch'" in condition
    assert "github.event_name == 'pull_request'" not in condition
    assert "release-please--branches--main" not in condition
    assert workflow["concurrency"] == {
        "group": "cross-platform-${{ github.event.pull_request.number || github.ref }}",
        "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
    }
    assert job["strategy"]["matrix"]["os"] == [
        "windows-latest",
        "macos-latest",
    ]
