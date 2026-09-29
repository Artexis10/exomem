"""The live-K3s suite is the only check of the platform's admission and
network policy logic: chart tests pin their text, and a mutation inverting a
CEL rule passes every one of them (harden-exomem-cloud-operator-access 2.7).
So the workflow runs the live job on pull requests that touch those
templates or the live suite itself, and on manual dispatch."""

from __future__ import annotations

import os
import subprocess
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github/workflows/cloud-cellctl.yml"

LIVE_PATHS = {
    "infra/helm/platform/templates/operator-access.yaml",
    "infra/helm/platform/templates/cloud-ingress.yaml",
    "infra/helm/platform/templates/cloud-gateway.yaml",
    "infra/helm/platform/templates/namespaces.yaml",
    "infra/helm/platform/templates/cellctl.yaml",
    "infra/helm/platform/values.schema.json",
    "infra/helm/platform/values.validation.yaml",
    "infra/helm/platform/Chart.lock",
    "infra/cellctl/src/cellctl/manifests.py",
    "infra/cellctl/tests/test_k3s_integration.py",
}


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_the_live_suite_runs_on_pull_requests_that_touch_the_policy_templates() -> None:
    workflow = _workflow()
    triggers = workflow.get("on", workflow.get(True))
    assert {"pull_request", "workflow_dispatch"} <= set(triggers)
    # The workflow-level filter must admit every live path, or the scope job
    # never sees the pull request.
    workflow_paths = triggers["pull_request"]["paths"]
    for path in LIVE_PATHS:
        assert any(fnmatch(path, pattern) for pattern in workflow_paths), path

    jobs = workflow["jobs"]
    scope = jobs["live-k3s-scope"]
    (step,) = [step for step in scope["steps"] if step.get("id") == "scope"]
    # Release bumps touch values.yaml; they must not drag in a 15-minute job.
    assert "values.yaml" not in step["env"]["LIVE_K3S_PATTERN"].replace("values.validation.yaml", "")
    assert scope["outputs"]["run"] == "${{ steps.scope.outputs.run }}"

    live = jobs["live-k3s"]
    assert set(live["needs"]) == {"checks", "live-k3s-scope"}
    assert " ".join(str(live["if"]).split()) == "needs.live-k3s-scope.outputs.run == 'true'"


def _scope_step() -> dict[str, Any]:
    (step,) = [
        step for step in _workflow()["jobs"]["live-k3s-scope"]["steps"] if step.get("id") == "scope"
    ]
    return step


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test"}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def _run_scope(repo: Path, event: str, base: str, head: str, tmp_path: Path) -> str:
    """Run the step exactly as GitHub's `shell: bash` does (-eo pipefail)."""
    step = _scope_step()
    output = tmp_path / "github_output"
    output.write_text("")
    env = {**os.environ, **{k: str(v) for k, v in step["env"].items()},
           "EVENT": event, "BASE": base, "HEAD": head, "GITHUB_OUTPUT": str(output)}
    subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
                   cwd=repo, env=env, check=True, capture_output=True, text=True)
    (line,) = [line for line in output.read_text().splitlines() if line.startswith("run=")]
    return line.removeprefix("run=")


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    templates = root / "infra/helm/platform/templates"
    templates.mkdir(parents=True)
    (templates / "operator-access.yaml").write_text("kind: ValidatingAdmissionPolicy\n")
    (root / "infra/helm/platform/values.yaml").write_text("a: 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root, _git(root, "rev-parse", "HEAD")


def _commit(root: Path) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "change")
    return _git(root, "rev-parse", "HEAD")


def test_a_renamed_policy_template_still_runs_the_live_suite(repo, tmp_path) -> None:
    root, base = repo
    templates = root / "infra/helm/platform/templates"
    (templates / "operator-access.yaml").rename(templates / "policies.yaml")
    head = _commit(root)
    assert _run_scope(root, "pull_request", base, head, tmp_path) == "true"


def test_a_large_pull_request_touching_a_template_still_runs_the_live_suite(repo, tmp_path) -> None:
    # grep -q exiting early used to SIGPIPE the diff under pipefail. The bulk
    # files sort after infra/, so the match comes early and the writer still
    # has output left when grep exits.
    root, base = repo
    bulk = root / "zz/bulk"
    bulk.mkdir(parents=True)
    for index in range(3000):
        (bulk / f"f{index:04}.md").write_text(f"{index}\n")
    (root / "infra/helm/platform/templates/operator-access.yaml").write_text("changed\n")
    head = _commit(root)
    assert _run_scope(root, "pull_request", base, head, tmp_path) == "true"


def test_an_uncomputable_diff_runs_the_live_suite(repo, tmp_path) -> None:
    root, base = repo
    assert _run_scope(root, "pull_request", "0" * 40, base, tmp_path) == "true"


def test_a_values_bump_alone_does_not_run_the_live_suite(repo, tmp_path) -> None:
    root, base = repo
    (root / "infra/helm/platform/values.yaml").write_text("a: 2\n")
    head = _commit(root)
    assert _run_scope(root, "pull_request", base, head, tmp_path) == "false"
    assert _run_scope(root, "workflow_dispatch", "", "", tmp_path) == "true"
    assert _run_scope(root, "push", base, head, tmp_path) == "false"


@pytest.mark.parametrize("name", ["pölicies.yaml", 'a"b.yaml', "tab\tname.yaml"])
def test_a_template_whose_path_git_quotes_still_runs_the_live_suite(repo, tmp_path, name) -> None:
    root, base = repo
    (root / "infra/helm/platform/templates" / name).write_text("kind: ConfigMap\n")
    head = _commit(root)
    assert _run_scope(root, "pull_request", base, head, tmp_path) == "true"


@pytest.mark.parametrize("path", sorted(LIVE_PATHS))
def test_every_live_path_matches_the_pattern(path: str) -> None:
    import re

    assert re.search(_scope_step()["env"]["LIVE_K3S_PATTERN"], path), path
