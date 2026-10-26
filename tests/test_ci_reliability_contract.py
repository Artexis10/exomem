from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]

FULL_CI_JOBS = {
    "frozen-verifier",
    "cloud-cellctl",
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
    ("core-tests", 24, "core"),
    ("harness-tests", 6, "harness"),
)

#: Wall-clock session caps, enforced by pytest in each parallel worker lane.
CORE_SESSION_TIMEOUT = 1400
HARNESS_SESSION_TIMEOUT = 900
HARNESS_MODULES_FILE = ROOT / "tests" / "harness_modules.txt"
NIGHTLY_OWNED_MODULE = "tests/test_latency_gate.py"

#: The test jobs the release PR skips. Lint, build, specs, capabilities, the
#: wheel onboarding gate and release evidence still run on it; the full run at
#: its base is its test proof.
RELEASE_PR_SKIPPED_JOBS = {
    "timing-tests",
    "core-tests",
    "recall-structural-reference",
    "harness-tests",
    "windows-held-filesystem",
    "tui",
    "product-e2e",
}


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


@pytest.mark.parametrize("job_id", [row[0] for row in PR_TIER_JOBS])
def test_pr_tiers_exercise_native_mcp_sdk_fields(job_id: str) -> None:
    assert _run_step(_workflow()["jobs"][job_id])["env"].get(
        "FASTMCP_MCP_CAMELCASE_COMPAT"
    ) == "false"


@pytest.mark.parametrize("job_id,shards,tier", PR_TIER_JOBS)
def test_pr_tier_lanes_have_time_bounds_and_versioned_timing_evidence(
    job_id: str, shards: int, tier: str
) -> None:
    workflow = _workflow()
    job = workflow["jobs"][job_id]

    assert job["timeout-minutes"] == 30
    run_step = _run_step(job)
    # Aggregate case durations overlap across workers; enforce the wall-clock caps directly.
    session_timeout = CORE_SESSION_TIMEOUT if tier == "core" else HARNESS_SESSION_TIMEOUT
    assert f"--session-timeout={session_timeout}" in run_step["run"]
    assert session_timeout < job["timeout-minutes"] * 60
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
    assert junit_step["if"] == (
        "${{ always() && (steps.scope.outputs.docs_only != 'true' || matrix.shard == 1) }}"
    )
    assert junit_step["with"]["name"] == (
        f"{tier}-py${{{{ matrix.python-version }}}}-shard-${{{{ matrix.shard }}}}-junit"
    )
    assert junit_step["with"]["path"] == (
        f"test-results/junit-{tier}-"
        "${{ matrix.python-version }}-shard-${{ matrix.shard }}"
        f"{'*' if tier == 'core' else ''}.xml"
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
    # nightly/manual full runs. The runtime matrix never names the release
    # branch (see test_expensive_ci_runs_nightly_and_manually_only).
    versions = " ".join(str(matrix["python-version"]).split())
    assert "needs.scope.outputs.full == 'true'" in versions
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
    algorithm = "least_duration" if tier == "core" else "duration_based_chunks"
    assert f"--splitting-algorithm={algorithm}" in command
    assert '--durations-path="$EXOMEM_DURATIONS_PATH"' in command

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
        "github.event_name == 'workflow_dispatch') && '--store-durations --clean-durations' || '' }}"
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
    assert durations_step["with"]["path"] == _run_step(job)["env"]["EXOMEM_DURATIONS_PATH"]

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


def _collect_items(label: str, tier: str | None, extra_args: list[str]) -> set[str]:
    env = os.environ.copy()
    env.pop("EXOMEM_TEST_TIER", None)
    env.pop("EXOMEM_VAULT_PATH", None)
    if tier is not None:
        env["EXOMEM_TEST_TIER"] = tier
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import json
import sys
import pytest

class Collection:
    def pytest_collection_finish(self, session):
        items = [item.nodeid for item in session.items]
        print("EXOMEM_COLLECTION_ITEMS=" + json.dumps(items))

raise SystemExit(pytest.main(sys.argv[1:], plugins=[Collection()]))
""",
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
    rows = next(
        (
            json.loads(line.removeprefix("EXOMEM_COLLECTION_ITEMS="))
            for line in result.stdout.splitlines()
            if line.startswith("EXOMEM_COLLECTION_ITEMS=")
        ),
        [],
    )
    items = set(rows)
    assert items, f"{label} collection yielded no test nodes"
    return items


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
    full_items = _collect_items("full lean suite", None, [])
    workflow = _workflow()
    core_job = workflow["jobs"]["core-tests"]
    timing_job = workflow["jobs"]["timing-tests"]
    # Job-level environment values select timing cases; runtime defaults live on the steps.
    assert timing_job["env"] == core_job["env"]
    assert timing_job["strategy"]["matrix"] == {
        "python-version": core_job["strategy"]["matrix"]["python-version"]
    }
    assert timing_job["needs"] == core_job["needs"] == "scope", "timing checks must overlap the worker pools"
    serial_selections = list(core_job["env"].values())
    pooled_exclusions = []
    for variable, selection in core_job["env"].items():
        # Pytest separates a node ID from its module path with ::.
        option = "--deselect" if "::" in selection else "--ignore"
        assert f'{option}="${variable}"' in _run_step(core_job)["run"]
        pooled_exclusions.append(f"{option}={selection}")
    serial_step = _run_step(timing_job)
    assert all(f'"${variable}"' in serial_step["run"] for variable in timing_job["env"])
    assert serial_step["if"] == "steps.scope.outputs.docs_only != 'true'"
    assert "--splits" not in serial_step["run"]
    assert "-n " not in serial_step["run"]
    assert "-serial.xml" in serial_step["run"]
    assert serial_step["env"] == _run_step(core_job)["env"]
    assert any(
        step.get("with", {}).get("name") == "core-py${{ matrix.python-version }}-serial-junit"
        and step.get("if") == "always()"
        for step in timing_job["steps"]
    )
    core_items = _collect_items("pooled core tier", "core", pooled_exclusions)
    serial = _collect_items("serial core checks", "core", serial_selections)
    harness_items = _collect_items("harness tier", "harness", [])
    latency_items = _collect_items("nightly latency file", None, [NIGHTLY_OWNED_MODULE])
    latency_job = workflow["jobs"]["retrieval-latency"]
    latency_step = next(
        step for step in latency_job["steps"] if step.get("name", "").startswith("Per-lane")
    )
    assert "-n " not in latency_step["run"], "clock checks need an idle runner"
    assert "--splits=4 --group=${{ matrix.shard }}" in latency_step["run"]
    assert "--splitting-algorithm=least_duration" in latency_step["run"]
    assert latency_job["strategy"]["matrix"]["shard"] == [1, 2, 3, 4]
    latency_shards: set[str] = set()
    for shard in latency_job["strategy"]["matrix"]["shard"]:
        selected = _collect_items(
            f"latency shard {shard}", None,
            [NIGHTLY_OWNED_MODULE, "--splits=4", f"--group={shard}",
             "--splitting-algorithm=least_duration"],
        )
        assert not latency_shards & selected, "latency shards repeat cases"
        latency_shards |= selected
    assert latency_shards == latency_items, "latency shards omit or add cases"
    full, core, harness, latency = full_items, core_items, harness_items, latency_items

    assert not core & serial, "the serial checks also run in the worker pool"
    core |= serial
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
    assert any(
        "tests/test_latency_gate.py -q" in step.get("run", "")
        for step in jobs["retrieval-latency"]["steps"]
    )
    semantic_command = jobs["semantic-write-latency"]["steps"][-1]["run"]
    assert "scripts/semantic_write_latency.py --check" in semantic_command


def test_structural_recall_runs_on_the_pr_runtime_and_reference_sqlite() -> None:
    """A newer planner must not hide corpus scans on the shipped reference SQLite."""
    workflow = _workflow()
    job = workflow["jobs"]["recall-structural-reference"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == 15
    assert job["env"]["UV_NO_MANAGED_PYTHON"] == "true"
    assert job["env"]["UV_PYTHON_DOWNLOADS"] == "never"
    verify = next(step for step in job["steps"]
                  if step.get("name") == "Verify reference interpreter and SQLite")
    assert verify["run"].count('assert sqlite3.sqlite_version == "3.45.1"') == 2
    run = next(step for step in job["steps"] if step.get("name") == "Run structural recall gate")
    assert "--python /usr/bin/python3.12" in run["run"]
    assert "tests/test_ranking_catalogue_metadata.py::test_public_recall_work_does_not_scale_with_unrelated_pages" in run["run"]
    assert run["if"] == "steps.scope.outputs.docs_only != 'true'"
    assert "recall-structural-reference" in workflow["jobs"]["gate"]["needs"]
    assert "tests/test_ranking_catalogue_metadata.py" not in _harness_modules_from_file()


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
        # The profile certifies the English torch model hosted cells serve.
        "EXOMEM_RECALL_MODEL": "BAAI/bge-base-en-v1.5",
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


def test_full_jobs_share_one_scope_output_and_start_alongside_ordinary_checks() -> None:
    workflow = _workflow()
    assert _triggers(workflow)["pull_request"]["types"] == [
        "opened", "synchronize", "reopened", "ready_for_review", "converted_to_draft",
    ]
    jobs = workflow["jobs"]
    assert "needs" not in jobs["scope"] and "if" not in jobs["scope"]
    assert jobs["scope"]["steps"][-1]["run"] == "python3 scripts/release_evidence.py scope"
    for name in FULL_CI_JOBS:
        assert jobs[name]["needs"] == "scope"
        assert jobs[name]["if"] == "${{ needs.scope.outputs.full == 'true' }}"
    for name in RELEASE_PR_SKIPPED_JOBS:
        assert jobs[name]["needs"] == "scope"
        assert jobs[name]["if"] == "${{ needs.scope.outputs.release != 'true' }}"
    assert jobs["cloud-cellctl"]["with"]["full"] == "${{ needs.scope.outputs.full == 'true' }}"
    assert "if" not in jobs["release-evidence"]


def _scope_fixture_git(repository: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repository,
        env={
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        },
        capture_output=True,
        text=True,
        check=True,
    )


#: Jobs whose tests a docs-only pull request narrows (scripts/ci_change_scope.py).
SCOPED_JOBS = ("core-tests", "harness-tests", "product-e2e")


def _change_scope(repository: Path, event: str, *, full: bool = False) -> tuple[dict[str, str], str]:
    output = repository / "scope-output"
    summary = repository / "scope-summary"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "ci_change_scope.py"), "the example check"],
        cwd=repository,
        env={
            **os.environ,
            "EVENT_NAME": event,
            "FULL_CI": str(full).lower(),
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(summary),
        },
        capture_output=True,
        text=True,
        check=True,
    )
    decision = dict(line.split("=", 1) for line in output.read_text().splitlines())
    return decision, summary.read_text() if summary.exists() else ""


@pytest.mark.parametrize(
    "old_path,new_path,earlier_runtime,docs_only",
    [
        ("openspec/spec.md", "openspec/spec.md", False, True),
        ("openspec/spec.md", None, False, True),
        ("docs/guide.md", "docs/guide.md", False, True),
        ("NOTES.md", "NOTES.md", False, True),
        # The sdist readme is a package input.
        ("README.md", "README.md", False, False),
        ("tests/fixtures/note.md", "tests/fixtures/note.md", False, False),
        ("src/example.py", "src/example.py", False, False),
        ("src/example.py", None, False, False),
        ("src/example.py", "openspec/moved.py", False, False),
        ("openspec/spec.md", "src/moved.md", False, False),
        ("openspec/spec.md", "openspec/spec.md", True, False),
    ],
)
def test_change_scope_covers_the_complete_merge_and_both_rename_paths(
    tmp_path: Path, old_path: str, new_path: str | None, earlier_runtime: bool, docs_only: bool
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _scope_fixture_git(repository, "init", "-b", "main")
    for relative in {old_path, "src/runtime.py"}:
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("initial\n")
    _scope_fixture_git(repository, "add", ".")
    _scope_fixture_git(repository, "commit", "-m", "initial")
    _scope_fixture_git(repository, "checkout", "-b", "topic")
    if earlier_runtime:
        (repository / "src/runtime.py").write_text("changed runtime\n")
        _scope_fixture_git(repository, "commit", "-am", "runtime change")
    if new_path is None:
        (repository / old_path).unlink()
    elif old_path != new_path:
        destination = repository / new_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        (repository / old_path).rename(destination)
    else:
        (repository / old_path).write_text("changed\n")
    _scope_fixture_git(repository, "add", ".")
    _scope_fixture_git(repository, "commit", "-m", "final change")
    _scope_fixture_git(repository, "checkout", "main")
    _scope_fixture_git(repository, "merge", "--no-ff", "--no-edit", "topic")

    decision, summary = _change_scope(repository, "pull_request")

    if docs_only:
        assert decision == {
            "docs_only": "true",
            "pytest_args": "-m reads_repository_docs --splits=1 --group=1",
        }
        assert "shard 1 each run all their marked tests" in summary
        assert "Docs-only change: the example check did not run" in summary
    else:
        assert decision == {"docs_only": "false", "pytest_args": ""}
        assert not summary


@pytest.mark.parametrize(
    "event,has_parent,full",
    [("pull_request", False, False), ("push", True, False), ("schedule", True, False), ("workflow_dispatch", True, False), ("pull_request", True, True)],
)
def test_change_scope_runs_everything_for_unknown_history_and_non_pr_events(
    tmp_path: Path, event: str, has_parent: bool, full: bool
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _scope_fixture_git(repository, "init", "-b", "main")
    path = repository / "openspec/spec.md"
    path.parent.mkdir()
    path.write_text("initial\n")
    _scope_fixture_git(repository, "add", ".")
    _scope_fixture_git(repository, "commit", "-m", "initial")
    if has_parent:
        path.write_text("changed\n")
        _scope_fixture_git(repository, "commit", "-am", "spec change")

    decision, summary = _change_scope(repository, event, full=full)

    assert decision == {"docs_only": "false", "pytest_args": ""}
    assert not summary


@pytest.mark.parametrize("job_id", SCOPED_JOBS)
def test_change_scope_compares_the_merge_commit_with_its_base(job_id: str) -> None:
    """A checkout of the PR head would compare only its last commit."""
    job = _workflow()["jobs"][job_id]
    checkout = next(step for step in job["steps"] if step.get("uses") == "actions/checkout@v4")
    assert "ref" not in checkout["with"]
    assert checkout["with"]["fetch-depth"] in (0, 2)
    scope = next(step for step in job["steps"] if step.get("id") == "scope")
    assert "scripts/ci_change_scope.py" in scope["run"]
    assert scope["env"]["EVENT_NAME"] == "${{ github.event_name }}"


def test_change_scope_relies_on_docs_paths_being_outside_package_inputs() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    build = project["tool"]["hatch"]["build"]
    wheel = build["targets"]["wheel"]
    # These distribution roots justify omitting unchanged installed inputs.
    assert set(wheel["packages"]) == {"src/exomem", "src/kb_mcp"}
    assert "force-include" not in build and "force-include" not in wheel
    assert set(build["targets"]["sdist"]["include"]) == {"/src", "/LICENSE", "/README.md", "/pyproject.toml"}


def test_release_evidence_is_enforced_on_the_release_pr() -> None:
    """Release checks use the same tested-tree selector as evidence automation."""
    workflow = _workflow()
    job = workflow["jobs"]["release-evidence"]
    report, checkout, scope, step = job["steps"]
    assert checkout["with"]["ref"] == "${{ github.event.pull_request.base.sha }}"
    assert "scripts/release_evidence.py" in checkout["with"]["sparse-checkout"]
    assert scope["shell"] == "bash"
    assert "scripts/check_release_pr_files.py" in scope["run"]
    assert step["if"] == "needs.scope.outputs.release == 'true'"
    assert step["env"]["BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"
    assert step["env"]["GH_REPO"] == "${{ github.repository }}"
    assert step["run"] == 'python3 scripts/release_evidence.py select --source "$BASE_SHA"'
    assert "release-evidence" in workflow["jobs"]["gate"]["needs"]


def test_full_receipt_records_the_checkout_only_after_required_dependencies_pass() -> None:
    steps = _workflow()["jobs"]["gate"]["steps"]
    check, checkout, record, upload = steps
    assert check["name"] == "Require the selected CI tier to succeed"
    for step in (checkout, record, upload):
        assert step["if"] == "needs.scope.outputs.full == 'true'"
    assert record["run"] == "python3 scripts/release_evidence.py record --output full-ci-evidence.json"
    assert upload["with"]["name"] == "full-ci-evidence-${{ github.run_id }}-${{ github.run_attempt }}"
    assert upload["with"]["if-no-files-found"] == "error"


def test_superseded_feedback_runs_cancel_and_one_stable_gate_covers_both_tiers() -> None:
    workflow = _workflow()

    assert workflow["concurrency"] == {
        "group": (
            "ci-${{ (github.event_name == 'schedule' || "
            "github.event_name == 'workflow_dispatch') && "
            "format('full-{0}', github.ref) || "
            "github.event.pull_request.number || github.ref }}"
        ),
        "cancel-in-progress": (
            "${{ github.event_name == 'pull_request' || github.event_name == 'push' }}"
        ),
    }

    gate = workflow["jobs"]["gate"]
    assert gate["name"] == "required CI gate"
    assert gate["if"] == "${{ !cancelled() }}"
    # release-evidence-landed runs after the gate and depends on it.
    # quality is the shared engineering-practices check, kept out of the required gate so the
    # required checks stay unchanged; the merge guard reads it. The push-run
    # cancel is a best-effort saving that must not decide evidence.
    assert set(gate["needs"]) == set(workflow["jobs"]) - {
        "gate",
        "release-evidence-landed",
        "cancel-superseded-push-run",
        "quality",
    }
    assert {"core-tests", "harness-tests"} <= set(gate["needs"])
    assert "test" not in gate["needs"]
    assert gate["steps"][0]["env"]["RESULTS"] == "${{ toJSON(needs) }}"
    full_ci = " ".join(str(gate["steps"][0]["env"]["FULL_CI"]).split())
    assert full_ci == "${{ needs.scope.outputs.full == 'true' }}"
    assert "release-please" not in full_ci



@pytest.mark.parametrize(
    "full_ci,results,failures",
    [
        (False, {"core-tests": "success", "retrieval-quality": "skipped"}, []),
        (True, {"core-tests": "success", "retrieval-quality": "success"}, []),
        (True, {"core-tests": "success", "retrieval-quality": "skipped"}, ["retrieval-quality"]),
        (False, {"build": "failure", "core-tests": "cancelled"}, ["build", "core-tests"]),
    ],
)
def test_required_gate_reports_every_dependency_that_blocks_the_selected_tier(
    full_ci: bool, results: dict[str, str], failures: list[str]
) -> None:
    """The stable required check must identify every failed job, preserving its verdict."""
    step = _workflow()["jobs"]["gate"]["steps"][0]
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", step["run"]],
        env={
            **os.environ,
            "RESULTS": json.dumps({job: {"result": state} for job, state in results.items()}),
            "FULL_CI": json.dumps(full_ci),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == (1 if failures else 0), result.stderr
    for job in failures:
        assert f"CI dependency {job} returned {results[job]}" in result.stderr
    for job in results.keys() - set(failures):
        assert job not in result.stderr


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


def test_release_evidence_automation_supplies_proof_without_merging() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/release-evidence-automation.yml").read_text())
    assert set(_triggers(workflow)) == {"workflow_dispatch"}
    jobs = workflow["jobs"]
    intent = jobs["dispatch-evidence"]
    refresh = jobs["rerun-evidence-check"]
    assert intent["if"] == "${{ inputs.release_pr != '' }}"
    assert refresh["if"] == "${{ inputs.evidence_run != '' && inputs.release_pr == '' }}"
    assert intent["steps"][-1]["run"] == (
        'python3 scripts/release_evidence.py intent --pr "$PR_NUMBER" --expected-base "$BASE_SHA"'
    )
    assert refresh["steps"][-1]["run"] == (
        'python3 scripts/release_evidence.py refresh --run-id "$EVIDENCE_RUN" --attempt "$EVIDENCE_ATTEMPT"'
    )
    for job in (intent, refresh):
        assert job["steps"][0]["with"]["ref"] == "main"
    assert workflow["permissions"] == {
        "actions": "write", "contents": "read", "pull-requests": "read",
    }
    landed = _workflow()["jobs"]["release-evidence-landed"]
    assert landed["needs"] == "gate"
    assert landed["if"] == "${{ (github.event_name == 'schedule' || github.event_name == 'workflow_dispatch') && github.ref == 'refs/heads/main' }}"
    assert landed["steps"][0]["env"]["EVIDENCE_ATTEMPT"] == "${{ github.run_attempt }}"


def test_python_qualification_does_not_wait_for_image_publication() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/release-please.yml").read_text())
    job = workflow["jobs"]["qualify-python"]
    assert set(job["needs"]) == {"plan", "build-artifacts", "publish-pypi"}
    assert "needs.publish-pypi.result == 'success'" in job["if"]
    assert "needs.plan.outputs.pypi_present == 'true'" in job["if"]
    assert "needs.plan.outputs.dry_run != 'true'" in job["if"]
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    qualify = next(step for step in job["steps"] if step.get("name", "").startswith("Qualify official"))
    assert '--run-id "$GITHUB_RUN_ID" --attempt "$PRODUCER_ATTEMPT"' in qualify["run"]
    assert '--verification-attempt "$GITHUB_RUN_ATTEMPT"' in qualify["run"]
    assert qualify["env"]["PRODUCER_ATTEMPT"] == "${{ needs.build-artifacts.outputs.producer_attempt }}"
    build = workflow["jobs"]["build-artifacts"]
    assert build["outputs"]["producer_attempt"] == "${{ steps.producer.outputs.attempt }}"
    producer = next(step for step in build["steps"] if step.get("id") == "producer")
    assert producer["run"] == 'echo "attempt=$GITHUB_RUN_ATTEMPT" >> "$GITHUB_OUTPUT"'
    assert job["steps"][-1]["with"]["name"] == "python-qualification-${{ github.run_id }}-${{ github.run_attempt }}"


#: The worst cross-platform session observed, in seconds. No longer censored:
#: with the cap above real runtime, all six Windows shards ran to completion at
#: 1950s, 2575s, 2698s, 2788s, 2886s and 2896s. The previous value of 2705 was a
#: floor taken from a shard the old cap had truncated, which is exactly the kind
#: of number that reads as a measurement while being a bound.
#:
#: Measured rather than predicted, and the measurement is why. `.test_durations.json`
#: is Linux-recorded and this lane does not run on Linux, so a prediction from it
#: needs a platform factor -- and the factor is not a constant. Four-way sessions
#: ran 2482s, 2449s and 2595s; six-way ran 2184s, 2250s and 2644s. Fifty percent
#: more shards bought six percent of runtime, so most of a shard here is not test
#: execution that splits, and no linear model over the durations file describes it.
#: Finding that fixed cost is its own change; until then the cap clears the
#: measurement.
MEASURED_WORST_CROSS_PLATFORM_SESSION = 2896

_FOLDED_RUN_RE = re.compile(
    r"^(?P<indent>\s*)-?\s*run:\s*>[-+]?\s*$(?P<body>(?:\n(?:(?P=indent)\s+.*|\s*))*)",
    re.MULTILINE,
)


def test_the_cross_platform_cap_clears_the_runtime_actually_measured() -> None:
    """The cap must sit above what a healthy shard takes, not above a prediction.

    At 2700s it sat below: shards reported clean summaries -- `2929 passed, 0
    failed in 45:02` -- and then exit 1, so a green suite read as a red lane.

    Deliberately not derived from `.test_durations.json` the way the pull-request
    tiers are. Applying that rule here passed while the lane was failing, because
    the file records Linux times and this lane runs on Windows and macOS. The
    correction is not a constant either: six shards ran only six percent faster
    than four, so the runtime does not scale with the split and a linear model
    over the durations would keep being wrong in a new way.
    """
    workflow = _cross_platform_workflow()
    job = workflow["jobs"]["suite"]
    command = _run_step(job)["run"]

    timeout = int(re.search(r"--session-timeout=(\d+)", command).group(1))
    job_seconds = job["timeout-minutes"] * 60

    assert timeout > MEASURED_WORST_CROSS_PLATFORM_SESSION, (
        f"--session-timeout={timeout}s is at or below the worst session measured "
        f"({MEASURED_WORST_CROSS_PLATFORM_SESSION}s), so a healthy shard reports as a failure"
    )
    assert timeout >= 1.15 * MEASURED_WORST_CROSS_PLATFORM_SESSION, (
        f"--session-timeout={timeout}s leaves no margin over the worst measured "
        "session; runner variance will cross it again"
    )
    assert timeout < job_seconds, (
        "the session cap must stay inside the job budget, so a hang is reported by "
        "pytest rather than killed silently by the runner"
    )


def test_no_folded_run_script_carries_a_yaml_style_comment() -> None:
    """A `#` inside a folded `>` scalar is part of the command, not a comment.

    Folding joins the lines with spaces, so one `#` comments out every flag after
    it and the lane silently stops running what the file appears to say. Found by
    doing it: an explanation added beside `--session-timeout` would have disabled
    that flag, `--durations` and `--junitxml` on every shard.

    Reads the raw file rather than the parsed tree, because `yaml.safe_load`
    discards the scalar style that is the whole distinction -- a `#` is a correct
    shell comment in a literal `|` block.
    """
    offenders: list[str] = []
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        for body in _FOLDED_RUN_RE.finditer(path.read_text(encoding="utf-8")):
            for number, line in enumerate(body.group("body").splitlines(), start=1):
                if "#" in line:
                    offenders.append(f"{path.name}: folded run block line {number}: {line.strip()}")

    assert not offenders, (
        "a `#` inside a folded `>` run scalar is part of the command and comments "
        f"out every flag after it: {offenders}"
    )
