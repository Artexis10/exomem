"""The merge queue tests the tree main gets, and a failure holds only the change that causes it."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("merge_queue", ROOT / "scripts/merge_queue.py")
assert spec and spec.loader
queue = importlib.util.module_from_spec(spec)
# dataclasses resolve their module through sys.modules.
sys.modules["merge_queue"] = queue
spec.loader.exec_module(queue)

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


def _pull(number, *, status=None, enabled_minutes_ago=30, gate=True, draft=False, auto=True):
    return queue.PullRequest(
        number=number,
        title=f"change {number}",
        head=f"{number:040d}",
        draft=draft,
        auto_merge=NOW - timedelta(minutes=enabled_minutes_ago) if auto else None,
        gate_passed=gate,
        queue=status,
    )


def _status(state, description, minutes_ago=1):
    return queue.Status(state, description, NOW - timedelta(minutes=minutes_ago))


def test_a_batch_takes_every_ready_pull_request_in_auto_merge_order():
    pulls = [
        _pull(2, enabled_minutes_ago=5),
        _pull(1, enabled_minutes_ago=50),
        _pull(3, gate=False),
        _pull(4, draft=True),
        _pull(5, auto=False),
    ]

    decision = queue.plan(pulls, NOW)

    assert decision.kind == "batch"
    assert [pull.number for pull in decision.members] == [1, 2]


def test_nothing_starts_while_a_queue_run_tests_main_or_a_passed_batch_is_landing():
    """Main moves only between batches, so a candidate is always main's next tree."""
    testing = _pull(1, status=_status("pending", "testing on main abc1234 alone", minutes_ago=20))
    landing = _pull(2, status=_status("success", "passed on main abc1234; tree x", minutes_ago=2))

    assert queue.plan([testing, _pull(3)], NOW).wait
    assert queue.plan([landing, _pull(3)], NOW).wait


def test_a_queue_run_that_never_reported_frees_the_queue_and_its_members_are_tested_again():
    lost = _pull(1, status=_status("pending", "testing on main abc1234 alone", minutes_ago=180))

    decision = queue.plan([lost], NOW)

    assert [pull.number for pull in decision.members] == [1]


def test_a_member_of_a_failed_batch_is_retested_alone_before_new_work():
    retest = _pull(7, status=_status("pending", "retest alone: the batch failed"), enabled_minutes_ago=5)

    decision = queue.plan([_pull(1, enabled_minutes_ago=60), retest], NOW)

    assert decision.kind == "retest"
    assert [pull.number for pull in decision.members] == [7]


def test_a_failed_pull_request_waits_until_auto_merge_is_enabled_on_it_again():
    failed = _pull(1, status=_status("failure", "failed alone after failing in a batch"))

    assert queue.plan([failed], NOW).wait
    assert [pull.number for pull in queue.plan([failed], NOW, requeued=1).members] == [1]


@pytest.mark.parametrize(
    "kind,result,expected",
    [
        ("batch", "success", "success"),
        ("batch", "failure", "pending"),
        ("retest", "failure", "failure"),
        ("batch", "cancelled", "pending"),
    ],
    ids=["passed", "batch-failed-retest-alone", "failed-alone", "cancelled-retest"],
)
def test_a_finished_queue_run_sets_each_members_status(kind, result, expected):
    members = [(1, "a" * 40), (2, "b" * 40)] if kind == "batch" else [(1, "a" * 40)]

    outcome = queue.verdicts(kind, members, result, tree="t" * 40, base="c" * 12)

    assert {state for _, state, _ in outcome} == {expected}
    assert [head for head, _, _ in outcome] == [head for _, head in members]
    if expected == "success":
        # `tested` lets main's push run skip shards only on this exact tree.
        assert all(description.endswith("tree " + "t" * 40) for _, _, description in outcome)
    if expected == "pending":
        # A pending description that the plan cannot read as a retest would strand the member.
        assert all(description.startswith(queue.RETEST) for _, _, description in outcome)


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
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
    ).stdout.strip()


def _branch(repository: Path, name: str, base: str, files: dict[str, str]) -> str:
    _git(repository, "checkout", "-q", "-b", name, base)
    for path, text in files.items():
        (repository / path).write_text(text)
    _git(repository, "commit", "-qam", name)
    return _git(repository, "rev-parse", "HEAD")


def test_the_candidate_is_the_tree_that_squash_merging_each_pull_request_gives_main(tmp_path, monkeypatch):
    """Catches a candidate that differs from main after the merges, which would test the wrong files."""
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    (repository / "a.txt").write_text("a\n")
    (repository / "b.txt").write_text("b\n")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "base")
    main = _git(repository, "rev-parse", "HEAD")
    first = _branch(repository, "first", main, {"a.txt": "a changed\n"})
    second = _branch(repository, "second", main, {"b.txt": "b changed\n"})
    clashing = _branch(repository, "clashing", main, {"a.txt": "a otherwise\n"})
    monkeypatch.chdir(repository)
    monkeypatch.setattr(queue, "git", lambda *args: _git(repository, *args))

    tip = queue.squash(main, main, first, "first")
    tip = queue.squash(tip, main, second, "second")
    blocked = queue.squash(tip, main, clashing, "clashing")
    alone = queue.squash(main, main, clashing, "clashing")

    _git(repository, "checkout", "-q", "main")
    _git(repository, "merge", "-q", "--squash", "first")
    _git(repository, "commit", "-qm", "first")
    _git(repository, "merge", "-q", "--squash", "second")
    _git(repository, "commit", "-qm", "second")
    assert _git(repository, "rev-parse", f"{tip}^{{tree}}") == _git(repository, "rev-parse", "HEAD^{tree}")
    # A clash with a pull request ahead of it defers the change; only a clash with main fails it.
    assert blocked is None
    assert alone is not None


def test_main_skips_the_shards_only_for_the_tree_the_queue_passed(tmp_path, monkeypatch):
    tree = "t" * 40
    statuses = {
        "commits/" + "m" * 40 + "/pulls": [{"head": {"sha": "h" * 40}}],
        "git/commits/" + "m" * 40: {"tree": {"sha": tree}},
    }
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_SHA", "m" * 40)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    for description, expected in [(f"passed on main abc1234; tree {tree}", "true"), ("passed on main abc1234; tree " + "u" * 40, "false")]:
        statuses["commits/" + "h" * 40 + "/statuses"] = [
            {"context": "merge queue", "state": "success", "description": description},
            {"context": "merge queue", "state": "pending", "description": "testing on main abc1234 alone"},
        ]
        monkeypatch.setattr(queue, "api", lambda path, *fields, method="GET": statuses[path])
        output.write_text("")

        queue.tested()

        assert output.read_text() == f"tested={expected}\n"


@pytest.mark.parametrize("tested,full,shards", [("true", "false", 0), ("false", "false", 24), ("true", "true", 48)])
def test_a_queue_tested_push_drops_only_the_core_and_harness_shards(tmp_path, tested, full, shards):
    """Catches a Full run losing shards because a queue run passed the same tree."""
    output = tmp_path / "output"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/ci_change_scope.py"), "the example check"],
        cwd=tmp_path,
        env={
            **os.environ,
            "EVENT_NAME": "push",
            "FULL": full,
            "QUEUE_TESTED": tested,
            "FULL_PYTHON_VERSIONS": '["3.11", "3.13"]',
            "CORE_SHARDS": "24",
            "HARNESS_SHARDS": "6",
            "GITHUB_OUTPUT": str(output),
        },
        check=True,
    )
    decision = dict(line.split("=", 1) for line in output.read_text().splitlines())

    assert len(json.loads(decision["core_matrix"])) == shards
    assert len(json.loads(decision["harness_matrix"])) == shards // 4


def test_the_queue_reads_the_gate_and_workflow_names_that_ci_yml_declares():
    """Catches a renamed gate job or workflow, which would leave every pull request unqueued."""
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    controller = yaml.safe_load((ROOT / ".github/workflows/merge-queue.yml").read_text())

    assert workflow["jobs"]["gate"]["name"] == queue.PR_GATE
    # PyYAML reads the bare `on` key as True.
    assert controller[True]["workflow_run"]["workflows"] == [workflow["name"]]
