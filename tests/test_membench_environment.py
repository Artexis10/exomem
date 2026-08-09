"""Environment capture and the two-tier comparison that was missing.

The incident these tests exist for: ``environment.json`` recorded
``python: 3.12.3`` on 2026-08-01 and ``3.14.6`` on 2026-08-05, retrieval went
452 hits → 0 with byte-identical product source, and nothing ever compared
the two files. Every fixture below whose name starts with ``AUG1_``/``AUG5_``
is copied verbatim from those two run directories.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from membench.environment import (
    BLOCKING,
    REPORTED,
    EnvironmentComparison,
    capture_environment,
    compare_environments,
    installed_distributions,
    load_environment,
    runtime_closure,
    verify_run_environment,
)

RUNS_ROOT = Path(__file__).resolve().parents[1] / "benchmarks" / "runs"

#: `benchmarks/runs/20260801T115138Z-exomem-local-postfix-lexical-v2-30586b/
#: environment.json` — the run that recorded 452 retrieval hits.
AUG1_ENVIRONMENT: dict[str, object] = {
    "env_knobs": {"EXOMEM_DISABLE_EMBEDDINGS": "1"},
    "exomem_version": "0.36.0",
    "generator_version": "membench-gen/0.1.0",
    "machine": "x86_64",
    "platform": "Linux-6.6.87.2-microsoft-standard-WSL2-x86_64-with-glibc2.39",
    "python": "3.12.3 (main, Jun 19 2026, 12:46:00) [GCC 13.3.0]",
    "repos": {
        "exomem": {
            "dirty": False,
            "head": "bc6cfac468280fd433b0f821bebed1d44085a439",
            "path": "/srv/checkout/exomem-worktree",
        }
    },
}

#: `benchmarks/runs/20260805T082324Z-exomem-local-zerohit-s1-probe-f5dc4f/
#: environment.json` — the replay that recorded 0 hits on the same 236 queries.
AUG5_ENVIRONMENT: dict[str, object] = {
    "env_knobs": {"EXOMEM_DISABLE_EMBEDDINGS": "1"},
    "exomem_version": "0.36.0",
    "generator_version": "membench-gen/0.1.0",
    "machine": "x86_64",
    "platform": "Linux-6.6.87.2-microsoft-standard-WSL2-x86_64-with-glibc2.39",
    "python": "3.14.6 (main, Jun 23 2026, 15:18:23) [Clang 22.1.3 ]",
    "repos": {
        "exomem": {
            "dirty": False,
            "head": "25c37b0e50eabbef67c0f3a5b88d85628f021994",
            "path": "/srv/checkout/exomem-worktree",
        }
    },
}

AUG1_RUN = RUNS_ROOT / "20260801T115138Z-exomem-local-postfix-lexical-v2-30586b"
AUG5_RUN = RUNS_ROOT / "20260805T082324Z-exomem-local-zerohit-s1-probe-f5dc4f"


def _synthetic(**overrides: object) -> dict[str, object]:
    """A fully-populated capture, so a single field can be varied alone."""

    base: dict[str, object] = {
        "generator_version": "membench-gen/0.1.0",
        "python": "3.12.3 (main, Jun 19 2026, 12:46:00) [GCC 13.3.0]",
        "python_version": "3.12.3",
        "python_implementation": "CPython",
        "platform": "Linux-6.6.87.2-microsoft-standard-WSL2-x86_64-with-glibc2.39",
        "machine": "x86_64",
        "exomem_version": "0.36.0",
        "repos": {"exomem": {"path": "/repo", "head": "a" * 40, "dirty": False}},
        "env_knobs": {"EXOMEM_DISABLE_EMBEDDINGS": "1"},
        "distributions": {"exomem": "0.36.0", "rank-bm25": "0.2.2", "pytest": "8.0.0"},
        "runtime_closure": ["exomem", "rank-bm25"],
    }
    base.update(overrides)
    return base


def _fields(comparison: EnvironmentComparison, tier: str) -> set[str]:
    return {d.field for d in comparison.differences if d.tier == tier}


# --------------------------------------------------------------------------
# Capture: the whole installed set, not a summary of it.
# --------------------------------------------------------------------------


def test_capture_records_every_installed_distribution() -> None:
    environment = capture_environment()
    distributions = environment["distributions"]
    assert isinstance(distributions, dict) and distributions
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in distributions.items())
    # Sorted and JSON-serialisable: the artifact has to be diffable.
    assert list(distributions) == sorted(distributions)
    assert json.loads(json.dumps(environment)) == environment
    installed = installed_distributions()
    assert distributions == installed
    # Everything importable, not just the product's own dependencies.
    assert "pytest" in distributions, "capture must record test/tooling packages too"
    assert len(distributions) > 20


def test_capture_records_the_interpreter_version_apart_from_the_build_string() -> None:
    environment = capture_environment()
    assert environment["python_version"] in str(environment["python"])
    assert environment["python_implementation"]


def test_capture_is_deterministic_within_a_process() -> None:
    first, second = capture_environment(), capture_environment()
    assert first["distributions"] == second["distributions"]
    assert first["runtime_closure"] == second["runtime_closure"]


def test_runtime_closure_holds_product_dependencies_and_not_tooling() -> None:
    closure = set(runtime_closure())
    # Declared in [project].dependencies, and squarely in the retrieval path.
    for name in ("exomem", "rank-bm25", "snowballstemmer", "numpy"):
        assert name in closure, f"{name} must be inside the product runtime closure"
    # Requested extras of a dependency ARE part of the product: the product
    # depends on `pyjwt[crypto]`, so cryptography can be imported by it.
    assert "cryptography" in closure
    assert closure <= set(installed_distributions())
    assert list(runtime_closure()) == sorted(closure)


def test_runtime_closure_does_not_follow_a_dependency_own_dev_extra() -> None:
    """The rule that keeps "blocking" from meaning "the whole venv".

    ``pyjwt`` declares ``pytest; extra == "dev"``. Following every extra
    everywhere pulled 81 of 81 installed distributions into the closure —
    making a pytest patch bump blocking, which is how a guard gets switched
    off instead of fixed.
    """

    closure = set(runtime_closure())
    installed = installed_distributions()
    assert "pyjwt" in closure and "pytest" in installed
    for tooling in ("pytest", "pytest-timeout", "pluggy", "iniconfig"):
        assert tooling not in closure, f"{tooling} is test tooling, not a product import"


# --------------------------------------------------------------------------
# The real artifacts: the comparison nobody ever ran.
# --------------------------------------------------------------------------


def test_aug1_versus_aug5_is_a_blocking_interpreter_mismatch() -> None:
    comparison = compare_environments(AUG1_ENVIRONMENT, AUG5_ENVIRONMENT)

    assert comparison.blocked
    assert _fields(comparison, BLOCKING) == {"python_version", "repos.exomem.head"}
    interpreter = next(d for d in comparison.blocking if d.field == "python_version")
    assert (interpreter.reference, interpreter.observed) == ("3.12.3", "3.14.6")
    assert "3.12.3" in comparison.summary() and "3.14.6" in comparison.summary()
    # Neither run recorded its installed set, and that is not agreement.
    unverifiable = {d.field for d in comparison.unverifiable}
    assert "distributions" in unverifiable
    assert comparison.status != "match"


def test_committed_run_artifacts_still_match_the_inlined_fixtures() -> None:
    for run_dir, fixture in ((AUG1_RUN, AUG1_ENVIRONMENT), (AUG5_RUN, AUG5_ENVIRONMENT)):
        if not (run_dir / "environment.json").is_file():
            pytest.skip(f"run artifacts absent (benchmarks/runs is not tracked): {run_dir}")
        loaded = load_environment(run_dir)
        # The recorded worktree path is machine-specific by nature; the inlined
        # fixtures carry a placeholder so the public-artifact privacy gate can
        # hold. Pin everything else verbatim, and the path only structurally.
        repos = loaded.get("repos")
        assert isinstance(repos, dict)
        for repo in repos.values():
            assert isinstance(repo["path"], str) and repo["path"].startswith("/")
            repo["path"] = "/srv/checkout/exomem-worktree"
        assert loaded == fixture


# --------------------------------------------------------------------------
# Tier boundary: blocking.
# --------------------------------------------------------------------------


def test_interpreter_patch_bump_is_blocking() -> None:
    comparison = compare_environments(
        _synthetic(), _synthetic(python_version="3.12.4", python="3.12.4 (main) [GCC]")
    )
    assert comparison.blocked
    assert _fields(comparison, BLOCKING) == {"python_version"}


def test_interpreter_implementation_swap_is_blocking() -> None:
    comparison = compare_environments(
        _synthetic(), _synthetic(python_implementation="PyPy")
    )
    assert "python_implementation" in _fields(comparison, BLOCKING)


def test_product_head_and_version_are_blocking() -> None:
    head = compare_environments(
        _synthetic(), _synthetic(repos={"exomem": {"path": "/repo", "head": "b" * 40, "dirty": False}})
    )
    assert _fields(head, BLOCKING) == {"repos.exomem.head"}
    version = compare_environments(_synthetic(), _synthetic(exomem_version="0.37.0"))
    assert _fields(version, BLOCKING) == {"exomem_version"}


def test_dirty_tree_is_blocking_even_when_both_sides_are_dirty() -> None:
    dirty = {"exomem": {"path": "/repo", "head": "a" * 40, "dirty": True}}
    comparison = compare_environments(_synthetic(repos=dirty), _synthetic(repos=dirty))
    assert comparison.blocked
    assert _fields(comparison, BLOCKING) == {"repos.exomem.dirty"}


def test_knob_difference_is_blocking() -> None:
    comparison = compare_environments(_synthetic(), _synthetic(env_knobs={}))
    assert _fields(comparison, BLOCKING) == {"env_knobs.EXOMEM_DISABLE_EMBEDDINGS"}


def test_runtime_closure_distribution_bump_is_blocking() -> None:
    comparison = compare_environments(
        _synthetic(),
        _synthetic(distributions={"exomem": "0.36.0", "rank-bm25": "0.2.3", "pytest": "8.0.0"}),
    )
    assert _fields(comparison, BLOCKING) == {"distributions.rank-bm25"}


def test_closure_distribution_appearing_or_vanishing_is_blocking() -> None:
    comparison = compare_environments(
        _synthetic(),
        _synthetic(
            distributions={"exomem": "0.36.0", "pytest": "8.0.0"},
            runtime_closure=["exomem", "rank-bm25"],
        ),
    )
    difference = next(d for d in comparison.blocking if d.field == "distributions.rank-bm25")
    assert difference.observed is None
    assert "reference environment" in difference.detail


# --------------------------------------------------------------------------
# Tier boundary: reported. Too strict is a failure mode too — a guard that
# invalidates real runs over an unrelated patch bump gets switched off.
# --------------------------------------------------------------------------


def test_tooling_distribution_bump_is_reported_not_blocking() -> None:
    comparison = compare_environments(
        _synthetic(),
        _synthetic(distributions={"exomem": "0.36.0", "rank-bm25": "0.2.2", "pytest": "8.4.1"}),
    )
    assert not comparison.blocked
    assert _fields(comparison, REPORTED) == {"distributions.pytest"}
    assert comparison.status == "reported_differences"


def test_kernel_and_build_string_churn_is_reported_not_blocking() -> None:
    comparison = compare_environments(
        _synthetic(),
        _synthetic(
            platform="Linux-6.6.99.9-microsoft-standard-WSL2-x86_64-with-glibc2.39",
            python="3.12.3 (main, Jul 30 2026, 09:00:00) [GCC 14.1.0]",
        ),
    )
    assert not comparison.blocked
    assert _fields(comparison, REPORTED) == {"platform", "python"}


def test_generator_version_is_reported_because_corpus_hashes_prove_identity() -> None:
    # The 4b.7 disposition, applied: corpus identity has an independent check
    # (dual artifact hashes), so its version strings are provenance, not a gate.
    comparison = compare_environments(
        _synthetic(), _synthetic(generator_version="membench-gen/0.2.0")
    )
    assert not comparison.blocked
    assert "generator_version" in _fields(comparison, REPORTED)


# --------------------------------------------------------------------------
# Absence of data is never reported as agreement.
# --------------------------------------------------------------------------


def test_identical_environments_match() -> None:
    comparison = compare_environments(_synthetic(), _synthetic())
    assert comparison.status == "match"
    assert comparison.differences == ()
    assert not comparison.blocked
    assert "matches the reference" in comparison.summary()


def test_unrecorded_distributions_are_unverifiable_never_a_match() -> None:
    reference = _synthetic()
    reference.pop("distributions")
    comparison = compare_environments(reference, _synthetic())
    assert comparison.status == "reported_differences"
    assert not comparison.blocked
    entry = next(d for d in comparison.unverifiable if d.field == "distributions")
    assert entry.tier == REPORTED
    assert "reference" in entry.detail


def test_unrecorded_closure_demotes_every_distribution_difference() -> None:
    reference = _synthetic()
    observed = _synthetic(distributions={"exomem": "0.36.0", "rank-bm25": "0.2.3"})
    reference.pop("runtime_closure")
    observed.pop("runtime_closure")
    comparison = compare_environments(reference, observed)
    assert not comparison.blocked
    assert "runtime_closure" in {d.field for d in comparison.unverifiable}


def test_a_repo_recorded_by_only_one_side_is_blocking() -> None:
    reference = _synthetic()
    observed = _synthetic(repos={"exomem": None})
    comparison = compare_environments(reference, observed)
    assert comparison.blocked
    assert "repos.exomem.head" in _fields(comparison, BLOCKING)


# --------------------------------------------------------------------------
# Loading and verifying a run directory.
# --------------------------------------------------------------------------


def test_verify_run_environment_reads_run_directories(tmp_path: Path) -> None:
    reference_dir = tmp_path / "reference"
    observed_dir = tmp_path / "observed"
    for directory, payload in (
        (reference_dir, AUG1_ENVIRONMENT),
        (observed_dir, AUG5_ENVIRONMENT),
    ):
        directory.mkdir()
        (directory / "environment.json").write_text(json.dumps(payload), encoding="utf-8")
    comparison = verify_run_environment(observed_dir, reference_dir)
    assert comparison.blocked
    assert "python_version" in _fields(comparison, BLOCKING)
    payload = comparison.as_dict()
    assert payload["status"] == "blocking_mismatch"
    assert {entry["field"] for entry in payload["blocking"]} == {
        "python_version",
        "repos.exomem.head",
    }


def test_load_environment_accepts_mapping_path_and_directory(tmp_path: Path) -> None:
    path = tmp_path / "environment.json"
    path.write_text(json.dumps(AUG1_ENVIRONMENT), encoding="utf-8")
    assert load_environment(AUG1_ENVIRONMENT) == AUG1_ENVIRONMENT
    assert load_environment(path) == AUG1_ENVIRONMENT
    assert load_environment(tmp_path) == AUG1_ENVIRONMENT
