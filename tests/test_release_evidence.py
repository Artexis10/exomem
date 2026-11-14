"""Release evidence follows tested trees and failed preflights never merge."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import runpy
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "release_evidence", ROOT / "scripts/release_evidence.py"
)
assert spec and spec.loader
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


@pytest.mark.parametrize(
    "event,draft,branch,full,release",
    [
        ("pull_request", True, "source", False, False),
        ("pull_request", False, "source", False, False),
        ("pull_request", False, evidence.RELEASE_BRANCH, False, True),
        ("push", False, "main", False, False),
        ("schedule", False, "main", True, False),
        ("workflow_dispatch", False, "source", True, False),
    ],
)
def test_scope_runs_full_only_on_schedule_and_dispatch_and_targets_release_prs(
    event, draft, branch, full, release
):
    assert evidence.ci_scope(
        event, {"pull_request": {"draft": draft, "head": {"ref": branch}}}
    ) == {
        "full": full,
        "release": release,
    }


@pytest.mark.parametrize("change", [None, "tree", "scope", "attempt", "repository", "expired"])
def test_squash_reuses_only_complete_matching_full_receipt(monkeypatch, change):
    """A green source dispatch survives squash only with its own matching full receipt."""
    repository = "example/project"
    source, head, tree = "a" * 40, "b" * 40, "c" * 40
    run = {
        "id": 12,
        "run_attempt": 2,
        "workflow_id": 8,
        "path": evidence.WORKFLOW,
        "head_sha": head,
        "head_branch": "source",
        "event": "workflow_dispatch",
        "run_started_at": "2026-01-01T00:00:00Z",
        "status": "completed",
        "conclusion": "success",
        "repository": {"full_name": repository},
        "head_repository": {"full_name": repository},
    }
    receipt = {
        "repository": repository,
        "workflow": evidence.WORKFLOW,
        "run_id": "12",
        "run_attempt": "2",
        "event": "workflow_dispatch",
        "scope": "full",
        "tested_commit": head,
        "tested_tree": tree,
    }
    artifact = {
        "id": 99,
        "name": "full-ci-evidence-12-2",
        "created_at": "2026-01-01T00:00:30Z",
        "expired": False,
        "workflow_run": {"id": 12, "head_sha": head},
    }
    if change == "expired":
        artifact["expired"] = True
    elif change:
        key = {
            "tree": "tested_tree",
            "scope": "scope",
            "attempt": "run_attempt",
            "repository": "repository",
        }[change]
        receipt[key] = "different"
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("full-ci-evidence.json", json.dumps(receipt))

    def api(path, *, pages=False):
        if path == "actions/workflows/ci.yml":
            return {"id": 8}
        if path == f"commits/{source}":
            return {"sha": source, "commit": {"tree": {"sha": tree}}}
        if path == f"commits/{source}/pulls":
            return [
                {
                    "merged_at": "2026-01-01",
                    "merge_commit_sha": source,
                    "base": {"ref": "main"},
                    "head": {"sha": head, "repo": {"full_name": repository}},
                }
            ]
        if "runs?" in path:
            return [{"workflow_runs": [run] if f"head_sha={head}" in path else []}]
        if path == f"commits/{head}":
            return {"commit": {"tree": {"sha": tree}}}
        if path == "actions/runs/12/attempts/2":
            return run
        if "/jobs?" in path:
            return [
                {
                    "jobs": [
                        {
                            "name": "required CI gate",
                            "conclusion": "success",
                            "started_at": "2026-01-01T00:00:00Z",
                            "completed_at": "2026-01-01T00:01:00Z",
                        }
                    ]
                }
            ]
        if path == "actions/runs/12/artifacts?per_page=100":
            return [{"artifacts": [artifact]}]
        raise AssertionError(path)

    monkeypatch.setattr(evidence, "api", api)
    monkeypatch.setattr(evidence, "command", lambda *args: archive.getvalue())
    selected = evidence.select_evidence(source, repository)
    assert (selected is not None) == (change is None)
    if selected:
        assert selected["tested_commit"] == head
        assert selected["run_attempt"] == "2"
        assert selected["successful_attempt"] == "2"
        assert selected["source_commit"] == source
        assert selected["artifact_id"] == 99


def test_partial_pr_run_cannot_supply_full_evidence(monkeypatch):
    """A successful reduced PR run cannot substitute for missing Full evidence."""
    source = "a" * 40

    def api(path, *, pages=False):
        if path == "actions/workflows/ci.yml":
            return {"id": 8}
        if path == f"commits/{source}":
            return {"sha": source, "commit": {"tree": {"sha": "c" * 40}}}
        if path.endswith("/pulls"):
            return []
        return [
            {
                "workflow_runs": [
                    {
                        "id": 12,
                        "event": "pull_request",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            }
        ]

    monkeypatch.setattr(evidence, "api", api)
    assert evidence.select_evidence(source, "example/project") is None


@pytest.mark.parametrize("legacy", [True, False])
def test_missing_receipt_keeps_only_the_legacy_exact_main_contract(monkeypatch, legacy):
    """New workflows cannot pass on missing artifacts; historical exact-main Full runs can."""
    source, tree, repository = "a" * 40, "b" * 40, "example/project"
    run = {
        "id": 12,
        "run_attempt": 1,
        "workflow_id": 8,
        "path": evidence.WORKFLOW,
        "head_sha": source,
        "head_branch": "main",
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
        "repository": {"full_name": repository},
        "head_repository": {"full_name": repository},
    }

    def api(path, *, pages=False):
        if path == "actions/workflows/ci.yml":
            return {"id": 8}
        if path == f"commits/{source}":
            return {"sha": source, "commit": {"tree": {"sha": tree}}}
        if path.endswith("/pulls"):
            return []
        if "/runs?" in path:
            return [{"workflow_runs": [run]}]
        if path == "actions/runs/12/attempts/1":
            return run
        if "/artifacts?" in path:
            return [{"artifacts": []}]
        if path == f"git/trees/{source}":
            return {"tree": [{"path": "scripts", "sha": tree}]}
        if path == f"git/trees/{tree}":
            return {"tree": [] if legacy else [{"path": "release_evidence.py"}]}
        raise AssertionError(path)

    monkeypatch.setattr(evidence, "api", api)
    selected = evidence.select_evidence(source, repository)
    assert (selected is not None) == legacy
    if selected:
        assert selected["contract"] == "legacy-exact-main"


def test_record_cli_uses_the_real_checkout_tree(tmp_path):
    """A receipt describes the actual Git checkout, independently of an event's claimed SHA."""
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(tmp_path)], check=True)
    (tmp_path / "source.txt").write_text("tested source\n")
    subprocess.run(["git", "add", "source.txt"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--quiet",
            "-m",
            "test source",
        ],
        cwd=tmp_path,
        check=True,
    )
    event_path = tmp_path / "event.json"
    event_path.write_text("{}")
    output = tmp_path / "receipt.json"
    record = [
        sys.executable,
        str(ROOT / "scripts/release_evidence.py"),
        "record",
        "--output",
        str(output),
    ]
    env = {
        **os.environ,
        "GH_REPO": "example/project",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_EVENT_PATH": str(event_path),
        "GITHUB_RUN_ID": "12",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_SHA": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
        ).strip(),
    }
    wrong = subprocess.run(
        record, cwd=tmp_path, env={**env, "GITHUB_SHA": "0" * 40}, capture_output=True, text=True
    )
    assert wrong.returncode != 0 and "actual checkout differs" in wrong.stderr
    subprocess.run(record, cwd=tmp_path, env=env, check=True)
    receipt = json.loads(output.read_text())
    for key, expression in (("tested_commit", "HEAD"), ("tested_tree", "HEAD^{tree}")):
        expected = subprocess.check_output(
            ["git", "rev-parse", expression], cwd=tmp_path, text=True
        ).strip()
        assert receipt[key] == expected
    assert receipt["scope"] == "full"
    assert receipt["package_checks"] == evidence.PACKAGE_CHECKS


@pytest.mark.parametrize(
    ("run", "message"),
    [
        ({"status": "in_progress", "conclusion": None}, "had not completed"),
        ({"status": "completed", "conclusion": "failure"}, "concluded failure"),
        (
            {"status": "completed", "conclusion": "success", "event": "pull_request", "id": 7},
            "not a trusted Full run",
        ),
    ],
    ids=["not-completed", "completed-failure", "untrusted-run"],
)
def test_refresh_fails_loudly_without_a_trusted_successful_full_run(
    tmp_path, monkeypatch, capsys, run, message
):
    """Only a trusted, successful Full run may refresh release checks; any other exit must be red."""
    calls = tmp_path / "gh.jsonl"
    gh = tmp_path / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['CALLS'], 'a') as output:\n"
        "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print(os.environ['RUN'])\n"
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("GH_REPO", "example/project")
    monkeypatch.setenv("CALLS", str(calls))
    monkeypatch.setenv("RUN", json.dumps(run))
    monkeypatch.setattr(
        sys, "argv", ["release_evidence.py", "refresh", "--run-id", "12", "--attempt", "1"]
    )
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(ROOT / "scripts/release_evidence.py"), run_name="__main__")

    assert exit_info.value.code == 1
    assert message in capsys.readouterr().err
    assert not any(
        json.loads(line)[:2] == ["run", "rerun"] for line in calls.read_text().splitlines()
    )


BASE, HEAD = "a" * 40, "c" * 40


def test_fixed_cut_does_not_follow_advancing_main(monkeypatch):
    """Unrelated main changes must not invalidate the source on a frozen cut branch."""
    branch = f"release-cuts/{BASE}"
    pr = {
        "number": 42,
        "state": "open",
        "base": {"ref": branch, "sha": BASE},
        "head": {
            "sha": HEAD,
            "repo": {"full_name": "example/project"},
            "ref": f"release-please--branches--{branch}",
        },
    }
    monkeypatch.setenv("GH_REPO", "example/project")
    monkeypatch.setattr(
        evidence, "api", lambda path, **kw: pr if path == "pulls/42" else {"sha": "d" * 40}
    )
    assert evidence.release_pr(42, BASE, HEAD) == pr


@pytest.mark.parametrize("missing", [True, False], ids=["missing-gate", "wrong-app"])
def test_inherited_policy_cannot_accept_partial_or_foreign_checks(monkeypatch, missing):
    """Cut branches have no protection: the main policy and App identity still apply."""
    policy = [{"context": "required CI gate", "app_id": 15368}]
    checks = (
        []
        if missing
        else [
            {
                "name": "required CI gate",
                "app": {"id": 7},
                "status": "completed",
                "conclusion": "success",
            }
        ]
    )
    monkeypatch.setattr(
        evidence,
        "api",
        lambda path, **kw: [{"check_runs": checks}] if "check-runs" in path else [{"statuses": []}],
    )
    assert evidence.inherited_checks(HEAD, policy) is None


def test_empty_main_check_policy_cannot_authorize_a_cut(monkeypatch):
    monkeypatch.setattr(evidence, "api", lambda path, **kw: {"protection": {}})
    with pytest.raises(ValueError, match="required-check policy"):
        evidence.main_check_policy()


def test_retry_keeps_selected_full_producer():
    """Once the intent binds a producer, a newer run must not replace it."""
    selected = {"source_commit": BASE, "tested_tree": HEAD, "run_id": "12", "run_attempt": "1"}
    assert evidence.bind_evidence({"evidence": selected}, selected) == selected
    with pytest.raises(ValueError, match="producer"):
        evidence.bind_evidence({"evidence": selected}, {**selected, "run_id": "13"})


@pytest.mark.parametrize("stale", ["version", "ancestry"])
def test_old_source_cannot_skip_predecessor_reconciliation(monkeypatch, stale):
    """A second cut must contain the previous metadata merge and its published version."""

    def command(*args, **kwargs):
        if stale == "ancestry":
            raise subprocess.CalledProcessError(1, args)
        return b""

    monkeypatch.setattr(evidence, "command", command)
    with pytest.raises(ValueError, match="preceding reconciliation"):
        evidence.validate_predecessor(
            BASE,
            "1.0.0" if stale == "version" else "1.1.0",
            {"version": "1.1.0", "reconciliation_commit": HEAD},
        )


def test_pending_identical_source_pr_full_prevents_duplicate_fallback(monkeypatch):
    """A squash changes the commit SHA while its source PR's Full producer is still running."""
    repository, source, head, tree = "example/project", "a" * 40, "b" * 40, "c" * 40
    monkeypatch.setattr(evidence, "evidence_sources", lambda source, repo: (tree, {source, head}))
    monkeypatch.setattr(
        evidence,
        "api",
        lambda path: {"id": 8} if path.endswith("ci.yml") else {"commit": {"tree": {"sha": tree}}},
    )
    monkeypatch.setattr(
        evidence,
        "runs",
        lambda commit: (
            [
                {
                    "head_sha": head,
                    "status": "in_progress",
                    "event": "workflow_dispatch",
                    "path": evidence.WORKFLOW,
                    "workflow_id": 8,
                    "repository": {"full_name": repository},
                    "head_repository": {"full_name": repository},
                }
            ]
            if commit == head
            else []
        ),
    )
    assert evidence.full_inflight(source, repository)
