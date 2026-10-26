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
        if path == "actions/runs/12/attempts/2":
            return run
        if path == "actions/runs/12/artifacts?per_page=100":
            return [{"artifacts": [artifact]}]
        raise AssertionError(path)

    monkeypatch.setattr(evidence, "api", api)
    monkeypatch.setattr(evidence, "command", lambda *args: archive.getvalue())
    selected = evidence.select_evidence(source, repository)
    assert (selected is not None) == (change is None)
    if selected:
        assert selected["tested_commit"] == head
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
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
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
    output = tmp_path / "receipt.json"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/release_evidence.py"),
            "record",
            "--output",
            str(output),
        ],
        cwd=tmp_path,
        check=True,
        env={
            **os.environ,
            "GH_REPO": "example/project",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_RUN_ID": "12",
            "GITHUB_RUN_ATTEMPT": "2",
            "GITHUB_SHA": "not-the-checkout",
        },
    )
    receipt = json.loads(output.read_text())
    for key, expression in (("tested_commit", "HEAD"), ("tested_tree", "HEAD^{tree}")):
        expected = subprocess.check_output(
            ["git", "rev-parse", expression], cwd=tmp_path, text=True
        ).strip()
        assert receipt[key] == expected
    assert receipt["scope"] == "full"


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
