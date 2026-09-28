"""The live-K3s suite is the only check of the platform's admission and
network policy logic: chart tests pin their text, and a mutation inverting a
CEL rule passes every one of them (harden-exomem-cloud-operator-access 2.7).
So the workflow runs the live job on pull requests that touch those
templates or the live suite itself, and on manual dispatch."""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github/workflows/cloud-cellctl.yml"

LIVE_PATHS = {
    "infra/helm/platform/templates/operator-access.yaml",
    "infra/helm/platform/templates/cloud-ingress.yaml",
    "infra/helm/platform/templates/cloud-gateway.yaml",
    "infra/helm/platform/templates/namespaces.yaml",
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
    listed = {line for line in step["env"]["LIVE_K3S_PATHS"].splitlines() if line}
    assert listed == LIVE_PATHS
    # Release bumps touch values.yaml; they must not drag in a 15-minute job.
    assert not any(path.endswith("values.yaml") for path in listed)
    script = step["run"]
    assert 'grep -Fxq -f <(printf \'%s\\n\' "$LIVE_K3S_PATHS")' in script
    assert "workflow_dispatch) run=true" in script
    assert 'git diff --name-only "$BASE...$HEAD"' in script
    assert scope["outputs"]["run"] == "${{ steps.scope.outputs.run }}"

    live = jobs["live-k3s"]
    assert set(live["needs"]) == {"checks", "live-k3s-scope"}
    assert " ".join(str(live["if"]).split()) == "needs.live-k3s-scope.outputs.run == 'true'"
