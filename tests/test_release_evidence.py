"""Release evidence follows tested trees and failed preflights never merge."""

from __future__ import annotations

import importlib.util
import io
import json
import os
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
        ("pull_request", False, "source", True, False),
        ("pull_request", False, evidence.RELEASE_BRANCH, False, True),
        ("push", False, "main", False, False),
        ("schedule", False, "main", True, False),
        ("workflow_dispatch", False, "source", True, False),
    ],
)
def test_scope_promotes_ready_source_prs_and_preserves_release_targeting(
    event, draft, branch, full, release
):
    assert evidence.ci_scope(
        event, {"pull_request": {"draft": draft, "head": {"ref": branch}}}
    ) == {
        "full": full,
        "release": release,
    }


@pytest.mark.parametrize(
    "producer_event,issuing_attempt,change",
    [
        (event, attempt, change)
        for event, attempt in [("workflow_dispatch", 2), ("pull_request", 1)]
        for change in [None, "tree", "scope", "attempt", "repository", "expired"]
    ]
    + [("pull_request", 1, "base")],
)
def test_squash_reuses_only_complete_matching_full_receipt(
    monkeypatch, change, producer_event, issuing_attempt
):
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
        "event": producer_event,
        "run_started_at": "2026-01-01T00:02:00Z"
        if issuing_attempt == 1
        else "2026-01-01T00:00:00Z",
        "status": "completed",
        "conclusion": "success",
        "repository": {"full_name": repository},
        "head_repository": {"full_name": repository},
    }
    receipt = {
        "repository": repository,
        "workflow": evidence.WORKFLOW,
        "run_id": "12",
        "run_attempt": str(issuing_attempt),
        "event": producer_event,
        "scope": "full",
        "tested_commit": head,
        "tested_tree": tree,
    }
    artifact = {
        "id": 99,
        "name": f"full-ci-evidence-12-{issuing_attempt}",
        "created_at": "2026-01-01T00:00:30Z",
        "expired": False,
        "workflow_run": {"id": 12, "head_sha": head},
    }
    tested = "d" * 40 if producer_event == "pull_request" else head
    receipt["tested_commit"] = tested
    if producer_event == "pull_request":
        receipt["pull_request"] = {
            "draft": False,
            "head": {"sha": head, "ref": "source"},
            "base": {"sha": "e" * 40},
        }
    if change == "expired":
        artifact["expired"] = True
    elif change == "base":
        receipt["pull_request"]["base"]["sha"] = "f" * 40
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
        if path == f"commits/{tested}":
            return {
                "commit": {"tree": {"sha": tree}},
                "parents": [{"sha": "e" * 40}, {"sha": head}],
            }
        if path == "actions/runs/12/attempts/2":
            return run
        if path == "actions/runs/12/attempts/1":
            return {
                **run,
                "run_attempt": 1,
                "conclusion": "failure",
                "run_started_at": "2026-01-01T00:00:00Z",
            }
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
        assert selected["tested_commit"] == tested
        assert selected["run_attempt"] == str(issuing_attempt)
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


@pytest.mark.parametrize(
    "event,advance_base,shallow",
    [
        ("workflow_dispatch", False, False),
        ("pull_request", False, False),
        ("pull_request", True, False),
        ("pull_request", True, True),
    ],
)
def test_record_cli_uses_the_real_checkout_tree(tmp_path, event, advance_base, shallow):
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
    payload = {}
    if event == "pull_request":
        for key, value in (
            ("user.name", "Test"),
            ("user.email", "test@example.invalid"),
            ("commit.gpgsign", "false"),
            ("core.hooksPath", "/dev/null"),
        ):
            subprocess.run(["git", "config", key, value], cwd=tmp_path, check=True)
        base = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
        ).strip()
        subprocess.run(["git", "checkout", "--quiet", "-b", "topic"], cwd=tmp_path, check=True)
        (tmp_path / "source.txt").write_text("ready source\n")
        subprocess.run(["git", "commit", "--quiet", "-am", "source"], cwd=tmp_path, check=True)
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
        ).strip()
        subprocess.run(["git", "checkout", "--quiet", "main"], cwd=tmp_path, check=True)
        if advance_base:
            (tmp_path / "target.txt").write_text("new target source\n")
            subprocess.run(["git", "add", "target.txt"], cwd=tmp_path, check=True)
            subprocess.run(["git", "commit", "--quiet", "-m", "target"], cwd=tmp_path, check=True)
        tested_base = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
        ).strip()
        subprocess.run(
            ["git", "merge", "--quiet", "--no-ff", "topic", "-m", "merge"], cwd=tmp_path, check=True
        )
        payload = {
            "number": 7,
            "pull_request": {
                "draft": False,
                "head": {"sha": head, "ref": "topic"},
                "base": {"sha": base},
            },
        }
    checkout = tmp_path
    if shallow:
        checkout = tmp_path / "shallow"
        subprocess.run(
            [
                "git", "-c", "core.hooksPath=/dev/null", "clone", "--quiet",
                "--depth=2", tmp_path.as_uri(), str(checkout),
            ],
            check=True,
        )
        assert (checkout / ".git" / "shallow").is_file()
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(payload))
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
        "GITHUB_EVENT_NAME": event,
        "GITHUB_EVENT_PATH": str(event_path),
        "GITHUB_RUN_ID": "12",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_SHA": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=checkout, text=True
        ).strip(),
    }
    wrong = subprocess.run(
        record, cwd=checkout, env={**env, "GITHUB_SHA": "0" * 40}, capture_output=True, text=True
    )
    assert wrong.returncode != 0 and "actual checkout differs" in wrong.stderr
    if event == "pull_request" and not advance_base:
        wrong_payload = json.loads(json.dumps(payload))
        wrong_payload["pull_request"]["head"]["sha"] = "0" * 40
        event_path.write_text(json.dumps(wrong_payload))
        wrong_head = subprocess.run(record, cwd=checkout, env=env, capture_output=True, text=True)
        assert wrong_head.returncode != 0 and "PR merge parents" in wrong_head.stderr
        assert not output.exists()
        event_path.write_text(json.dumps(payload))
    subprocess.run(record, cwd=checkout, env=env, check=True)
    receipt = json.loads(output.read_text())
    for key, expression in (("tested_commit", "HEAD"), ("tested_tree", "HEAD^{tree}")):
        expected = subprocess.check_output(
            ["git", "rev-parse", expression], cwd=checkout, text=True
        ).strip()
        assert receipt[key] == expected
    assert receipt["scope"] == "full"
    if event == "pull_request":
        assert receipt["pull_request"]["head"]["sha"] == head
        assert receipt["pull_request"]["base"]["sha"] == tested_base
        assert receipt["tested_commit"] != head
        if not advance_base:
            subprocess.run(["git", "checkout", "--quiet", "--detach", head], cwd=checkout, check=True)
            non_merge = subprocess.run(
                record, cwd=checkout, env={**env, "GITHUB_SHA": head}, capture_output=True, text=True
            )
            assert non_merge.returncode != 0 and "PR merge parents" in non_merge.stderr


def test_merge_cli_stops_when_current_main_moved(tmp_path):
    """The real command must stop before gh merge after a failed current-main preflight."""
    log = tmp_path / "gh.jsonl"
    gh = tmp_path / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['CALLS'], 'a') as output:\n"
        "    output.write(json.dumps(args) + '\\n')\n"
        "if args[:2] == ['api', 'repos/example/project/commits/main']:\n"
        "    print(json.dumps({'sha': 'b' * 40}))\n"
        "else:\n"
        "    sys.exit('unexpected call after failed preflight')\n"
    )
    gh.chmod(0o755)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/release_evidence.py"),
            "merge",
            "--pr",
            "42",
            "--expected-base",
            "a" * 40,
            "--expected-head",
            "c" * 40,
        ],
        env={
            **os.environ,
            "GH_REPO": "example/project",
            "CALLS": str(log),
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        },
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "main moved" in result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls == [["api", "repos/example/project/commits/main"]]
