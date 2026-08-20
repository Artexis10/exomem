"""The delegation contract between Claude Code and Codex lane workers.

This repo hands implementation work to Codex CLI workers and takes it back
through a merge gate. That handoff is a workflow, not a habit, and the parts of
it that have already failed in practice are pinned here so a regression fails
CI instead of costing an afternoon.

Two failures are on record:

1. `cmd_start` hardcoded `-s workspace-write`. On Windows that is Codex's
   unelevated restricted token, which cannot touch the private DACL exomem puts
   on its own state directories -- so pytest dies clearing its tmpdir and the
   worker cannot run a single test. A lane burned roughly an hour and 18M
   tokens producing zero commits before anyone knew.

2. The remedy was written into a throwaway lane script rather than the
   committed runner, so it evaporated and the next lane inherited the same
   broken sandbox four days later.

The lesson from (2) is why this file exists: the fix belongs where it is
enforced, not where it was convenient.
"""

from __future__ import annotations

from pathlib import Path

import pytest

RUNNER = Path(__file__).parents[1] / "scripts" / "codex_task.sh"


@pytest.fixture(scope="module")
def runner() -> str:
    assert RUNNER.is_file(), f"the lane runner is missing at {RUNNER}"
    return RUNNER.read_text(encoding="utf-8")


def _executable_lines(runner: str) -> list[tuple[int, str]]:
    """The lines the shell will actually run.

    The runner's own comments explain the `workspace-write` failure by name, so
    a whole-file substring search matches the explanation and can never go red.
    A guard that cannot fail is not a guard -- this one was caught doing exactly
    that before it shipped.
    """
    return [
        (number, line)
        for number, line in enumerate(runner.splitlines(), start=1)
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_the_worker_sandbox_is_not_hardcoded_to_one_that_cannot_run_tests(
    runner: str,
) -> None:
    """Every brief ends in "run the tests"; a worker that cannot is worse than none.

    `workspace-write` is not a tighter version of the same worker -- on this
    platform it is a worker with no ability to do the job, which does not
    surface until it has already spent the budget.
    """
    for number, line in _executable_lines(runner):
        assert "-s workspace-write" not in line, (
            "scripts/codex_task.sh pins a Codex worker to `workspace-write`. On "
            "Windows that is the unelevated restricted token, which exomem's own "
            "private state-directory DACL denies by design -- pytest cannot clear "
            "its tmpdir, so the worker cannot run one test. This is not "
            "fixable by widening writable_roots; the path moves, the ACL does "
            "not. Use $CODEX_SANDBOX.\n\n"
            f"offending line {number}:\n{line}"
        )


def test_the_sandbox_default_is_overridable_rather_than_baked_in(runner: str) -> None:
    """An operator must be able to tighten a specific lane without editing the runner."""
    assert "CODEX_SANDBOX=${CODEX_SANDBOX:-" in runner, (
        "the sandbox mode must come from an overridable CODEX_SANDBOX default"
    )
    assert 'codex exec --profile "$profile" -s "$CODEX_SANDBOX"' in runner, (
        "codex exec must take its sandbox from $CODEX_SANDBOX, not a literal"
    )


def test_a_lane_proves_it_can_run_a_test_before_it_is_given_a_brief(
    runner: str,
) -> None:
    """The check that converts a silent hour of thrashing into a loud early failure.

    Defaults drift and platforms change. What makes that survivable is not the
    default being right, it is finding out in seconds rather than in an hour.
    """
    assert "preflight_sandbox()" in runner, (
        "scripts/codex_task.sh must define preflight_sandbox"
    )

    preflight_call = runner.find('preflight_sandbox "$wt"')
    assert preflight_call != -1, "cmd_start must call preflight_sandbox"

    worker_launch = runner.find('--json -o "$wt/.task/codex-run.jsonl"')
    assert worker_launch != -1, "cmd_start must launch the worker with a run log"
    assert preflight_call < worker_launch, (
        "preflight_sandbox must run BEFORE the worker is handed its brief -- "
        "after is just a slower way to learn the same thing"
    )

    preflight_body = runner[runner.index("preflight_sandbox()") : preflight_call]
    assert "pytest" in preflight_body, (
        "the preflight must actually run a test, not merely start a process: "
        "the sandbox failure appears in pytest's own tmpdir teardown"
    )
    assert "exit 1" in preflight_body, (
        "a preflight that cannot run a test must refuse to launch the lane"
    )


def test_a_worker_can_never_be_pointed_at_the_shared_primary_checkout(
    runner: str,
) -> None:
    """Containment comes from the worktree, which is why full access is affordable.

    If this guard goes, the sandbox default becomes the only thing standing
    between a worker and another session's uncommitted work.
    """
    assert "require_linked_worktree" in runner
    start = runner.index("cmd_start()")
    launch = runner.index('--json -o "$wt/.task/codex-run.jsonl"')
    assert "require_linked_worktree" in runner[start:launch], (
        "cmd_start must verify the worktree is a linked one before launching a "
        "worker into it"
    )


def test_the_merge_gate_still_checks_what_the_sandbox_no_longer_does(
    runner: str,
) -> None:
    """Full access is granted against a verify step, so the verify step is load-bearing."""
    verify = runner[runner.index("cmd_verify()") :]
    assert "status --porcelain" in verify, "verify must require a clean tree"
    assert "diff --name-only origin/main...HEAD" in verify, (
        "verify must show the lane's diff against the base it branched from"
    )
    for guarded in ("tests/golden/", "tests/test_latency_gate.py", ".github/"):
        assert guarded in verify, (
            f"verify must flag changes to the guarded path {guarded}; a worker "
            "with full access can edit its own gates"
        )
