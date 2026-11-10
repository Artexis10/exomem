"""The cloud-deploy workflow's script (automate-cloud-releases task 1.12), on recorded
API answers (tests/fixtures/cloud-deploy/answers.json).

Each case runs the script's `main` with a transport that serves recorded GitHub and
Substrate answers, and reads what the public Actions log would show.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cloud_deploy", ROOT / "infra/scripts/cloud_deploy.py")
assert spec and spec.loader
cloud_deploy = importlib.util.module_from_spec(spec)
sys.modules["cloud_deploy"] = cloud_deploy  # dataclasses resolve their module by name
spec.loader.exec_module(cloud_deploy)

ANSWERS = json.loads((ROOT / "tests/fixtures/cloud-deploy/answers.json").read_text(encoding="utf-8"))
DEPLOY_TOKEN = "deploy-token-value-that-must-never-be-printed"  # noqa: S105 - a test value
ALERT_URL = "https://app.example.test/api/exomem/alerts/alert-capability-that-must-never-be-printed"
DIGEST = "sha256:" + "2" * 64
IMAGE = f"ghcr.io/artexis10/exomem@{DIGEST}"
ENV = {
    "GITHUB_REPOSITORY": "example-owner/example-repo",
    "GITHUB_API_URL": "https://api.github.test",
    "GH_TOKEN": "github-token-that-must-never-be-printed",
    "EXOMEM_SUBSTRATE_URL": "https://app.example.test",
    "EXOMEM_DEPLOY_TOKEN": DEPLOY_TOKEN,
    "EXOMEM_CLOUD_DEPLOY_ALERT_URL": ALERT_URL,
    "CLOUD_DEPLOY_POLL_INTERVAL_SECONDS": "15",
    "CLOUD_DEPLOY_POLL_TIMEOUT_SECONDS": "600",
}
# Only the chart and controller's test and Markdown files changed since the platform's release.
TESTS_AND_DOCS = ["infra/cellctl/tests/test_reconcile.py", "infra/helm/platform/OWNERSHIP.md", "src/exomem/server.py"]


class Recorded:
    """Serves recorded answers by method and path, in order; the last answer repeats."""

    def __init__(self, rollout: list[str], put: str = "substrate_put_ok", deployments: list | None = None) -> None:
        self.rollout = [ANSWERS[name] for name in rollout]
        self.put = ANSWERS[put]
        self.deployments = ANSWERS["github_platform_deployments"] if deployments is None else deployments
        self.calls: list[tuple[str, str, bytes | None]] = []

    def __call__(self, method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, bytes]:
        self.calls.append((method, url, body))
        path = url.split(".test", 1)[1]
        if path == "/repos/example-owner/example-repo/releases/latest":
            return 200, json.dumps(ANSWERS["github_release_latest"]).encode()
        if path.startswith("/repos/example-owner/example-repo/deployments?"):
            return 200, json.dumps(self.deployments).encode()
        if path.startswith("/repos/example-owner/example-repo/deployments/9002/statuses"):
            return 200, json.dumps(ANSWERS["github_statuses_success"]).encode()
        if path.startswith("/repos/example-owner/example-repo/deployments/9001/statuses"):
            return 200, json.dumps(ANSWERS["github_statuses_failure"]).encode()
        if method == "GET" and path == cloud_deploy.ROLLOUT_PATH:
            answer = self.rollout.pop(0) if len(self.rollout) > 1 else self.rollout[0]
            return 200, json.dumps(answer).encode()
        if method == "PUT" and path == cloud_deploy.RELEASE_PATH:
            return (200 if self.put.get("success") else 409), json.dumps(self.put).encode()
        if method == "POST" and url == ALERT_URL:
            return 202, b'{"accepted":true}'
        raise AssertionError(f"unrecorded call {method} {path}")

    def puts(self) -> list[dict]:
        return [json.loads(body) for method, _, body in self.calls if method == "PUT"]

    def alerts(self) -> list[tuple[str, bool]]:
        return [(json.loads(body)["alert"], json.loads(body)["active"]) for method, _, body in self.calls
                if method == "POST"]


def _run(recorded: Recorded, capsys, *, attested: bool = True, changed: list[str] | None = None,
         env: dict | None = None) -> tuple[int, str, list[float]]:
    slept: list[float] = []
    clock = iter(range(0, 10_000, 15))
    tools = cloud_deploy.Tools(
        transport=recorded,
        digest=lambda tag: DIGEST,
        attest=lambda repository, image, tag: attested,
        changed_paths=lambda base, head: TESTS_AND_DOCS if changed is None else changed,
        sleep=slept.append,
        monotonic=lambda: next(clock),
    )
    code = cloud_deploy.main(env or ENV, tools)
    out = capsys.readouterr()
    log = out.out + out.err
    # The Actions log is public: no secret, no cell ID and no tenant ID, in any case.
    for secret in (DEPLOY_TOKEN, ENV["GH_TOKEN"], ALERT_URL, "alert-capability", "zq7xk2mfp4wn8rtd",
                   "6b1f0c9e-2d3a-4b5c-8e7f-0a1b2c3d4e5f", "app.example.test"):
        assert secret not in log
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", log)
    return code, log, slept


def _barrier_file(tmp_path: Path, scope: str) -> Path:
    document = json.loads(cloud_deploy.BARRIER_FILE.read_text(encoding="utf-8"))
    document["barriers"].append({"id": "new-step", "version": "0.171.0", "scope": scope, "kind": "operator-step",
                                 "runbook": "docs/runbooks/cloud-platform-deploy.md#apply-the-active-secrets",
                                 "summary": "A step for this release."})
    path = tmp_path / "barriers.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_an_attested_release_that_passes_every_gate_sets_the_image_and_waits_for_the_canary(capsys) -> None:
    # Catches a run that reports success before the canary is Ready, and a body that
    # carries more than the image and its release (a pause clear or a cell pin).
    recorded = Recorded(["substrate_rollout_behind", "substrate_rollout_canary_behind",
                         "substrate_rollout_canary_behind", "substrate_rollout_canary_on_target"])

    code, log, slept = _run(recorded, capsys)

    assert code == 0
    assert recorded.puts() == [{"cellImage": IMAGE, "release": "v0.171.0"}]
    assert slept == [15.0, 15.0]
    assert log.splitlines()[-1] == "cloud-deploy: CANARY_ON_TARGET"
    assert recorded.alerts() == [(cloud_deploy.PLATFORM_NEEDED_ALERT, False), (cloud_deploy.BARRIER_ALERT, False)]


def test_an_unattested_digest_sets_nothing(capsys) -> None:
    # Catches a pushed image that reaches the fleet.
    recorded = Recorded(["substrate_rollout_behind"])

    code, log, _ = _run(recorded, capsys, attested=False)

    assert (code, recorded.puts()) == (1, [])
    assert "ATTESTATION_UNVERIFIED" in log


@pytest.mark.parametrize("answer", ["substrate_error_image_seen", "substrate_error_release_older"])
def test_a_release_not_newer_than_the_fleet_ends_as_success_with_nothing_set(capsys, answer: str) -> None:
    # Catches a red run on every merge after a withdrawal.
    recorded = Recorded(["substrate_rollout_behind"], put=answer)

    code, log, slept = _run(recorded, capsys)

    assert (code, slept) == (0, [])
    assert log.splitlines()[-1] == f"cloud-deploy: {ANSWERS[answer]['error']['code']}"


def test_a_paused_rollout_sets_nothing_and_fails_with_the_pause_code(capsys) -> None:
    # Catches automation that resumes a paused fleet.
    recorded = Recorded(["substrate_rollout_paused"])

    code, log, _ = _run(recorded, capsys)

    assert (code, recorded.puts()) == (1, [])
    assert log.splitlines()[-1] == "cloud-deploy: ROLLOUT_PAUSED UPGRADE_READINESS_TIMEOUT"


def test_a_pause_during_the_poll_fails_with_the_pause_code(capsys) -> None:
    recorded = Recorded(["substrate_rollout_behind", "substrate_rollout_canary_behind", "substrate_rollout_paused"])

    code, log, _ = _run(recorded, capsys)

    assert code == 1
    assert log.splitlines()[-1] == "cloud-deploy: ROLLOUT_PAUSED UPGRADE_READINESS_TIMEOUT"


def test_a_chart_template_change_sets_nothing_and_raises_the_platform_alert(capsys) -> None:
    # Catches a cell image on a platform without its chart.
    recorded = Recorded(["substrate_rollout_behind"])

    code, log, _ = _run(recorded, capsys, changed=["infra/helm/platform/templates/cellctl.yaml", *TESTS_AND_DOCS])

    assert (code, recorded.puts()) == (1, [])
    assert recorded.alerts() == [(cloud_deploy.PLATFORM_NEEDED_ALERT, True)]
    assert log.splitlines()[-1] == "cloud-deploy: cloud-release-platform-needed 1 platform input(s) changed"


def test_no_platform_deployment_sets_nothing_and_raises_the_platform_alert(capsys) -> None:
    recorded = Recorded(["substrate_rollout_behind"], deployments=[])

    code, log, _ = _run(recorded, capsys)

    assert (code, recorded.puts()) == (1, [])
    assert recorded.alerts() == [(cloud_deploy.PLATFORM_NEEDED_ALERT, True)]
    assert "NO_PLATFORM_DEPLOYMENT" in log


def test_controller_tests_and_markdown_alone_pass_the_platform_gate(capsys) -> None:
    # Catches a test-only release that waits for a person.
    recorded = Recorded(["substrate_rollout_behind", "substrate_rollout_canary_on_target"])

    code, _, _ = _run(recorded, capsys, changed=TESTS_AND_DOCS)

    assert code == 0
    assert recorded.puts() == [{"cellImage": IMAGE, "release": "v0.171.0"}]


@pytest.mark.parametrize("scope", ["platform", "cell"])
def test_an_uncrossed_barrier_of_either_scope_sets_nothing(capsys, monkeypatch, tmp_path: Path, scope: str) -> None:
    # Catches a one-way change that the paths miss: the release changes no build input.
    recorded = Recorded(["substrate_rollout_behind"])
    monkeypatch.setattr(cloud_deploy, "BARRIER_FILE", _barrier_file(tmp_path, scope))

    code, log, _ = _run(recorded, capsys)

    assert (code, recorded.puts()) == (1, [])
    assert (cloud_deploy.BARRIER_ALERT, True) in recorded.alerts()
    assert log.splitlines()[-1] == "cloud-deploy: cloud-release-barrier new-step"


def test_the_fleet_already_on_the_release_succeeds_before_any_gate(capsys) -> None:
    # Catches a spurious platform alert for a release the fleet already runs: the
    # chart changed since the platform's release, but the fleet's tag is this release,
    # set through another digest of it.
    recorded = Recorded(["substrate_rollout_release_set_by_tag"])

    code, log, _ = _run(recorded, capsys, changed=["infra/helm/platform/values.yaml"])

    assert (code, recorded.puts(), recorded.alerts()) == (0, [], [])
    assert log.splitlines()[-1] == "cloud-deploy: CELL_IMAGE_CURRENT"


def test_without_its_deploy_token_the_workflow_does_nothing(capsys) -> None:
    recorded = Recorded(["substrate_rollout_behind"])

    code, log, _ = _run(recorded, capsys, env={**ENV, "EXOMEM_DEPLOY_TOKEN": ""})

    assert (code, recorded.calls) == (0, [])
    assert "CLOUD_DEPLOY_NOT_CONFIGURED" in log


def test_the_gate_globs_select_the_chart_and_controller_build_inputs() -> None:
    include, exclude = cloud_deploy.load_gate()

    assert cloud_deploy.platform_inputs([
        "infra/helm/platform/templates/cloud-gateway.yaml", "infra/cellctl/src/cellctl/reconcile.py",
        "infra/cellctl/tests/test_rollout.py", "infra/helm/platform/OWNERSHIP.md", "src/exomem/server.py",
        "infra/helm/platform/files/upgrade-barriers-v1.json",
    ], include, exclude) == [
        "infra/helm/platform/templates/cloud-gateway.yaml", "infra/cellctl/src/cellctl/reconcile.py",
        "infra/helm/platform/files/upgrade-barriers-v1.json",
    ]


def test_the_workflow_runs_only_after_a_release_tag_and_only_main_holds_its_token() -> None:
    # Catches a run on every merge to main, and a deploy token that a branch can reach.
    workflow = yaml.safe_load((ROOT / ".github/workflows/cloud-deploy.yml").read_text(encoding="utf-8"))
    triggers = workflow[True]  # YAML 1.1 reads the `on` key as true.
    (job,) = workflow["jobs"].values()

    assert triggers["workflow_run"] == {"workflows": ["release-please"], "types": ["completed"],
                                        "branches": ["v*"]}
    assert triggers["workflow_dispatch"] is None
    assert "github.event.workflow_run.conclusion == 'success'" in job["if"]
    assert "github.ref == 'refs/heads/main'" in job["if"]
    assert job["environment"] == "cloud-deploy"
    assert workflow["concurrency"] == {"group": "cloud-deploy", "cancel-in-progress": False}
    assert set(workflow["permissions"].values()) == {"read"}


@pytest.mark.reads_repository_docs
def test_every_release_and_rollout_alert_has_a_runbook_step() -> None:
    # Catches an alert that fires with no step for the person who receives it.
    import ast

    tree = ast.parse((ROOT / "infra/cellctl/src/cellctl/alerts.py").read_text(encoding="utf-8"))
    rollout_alerts = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
                      and node.targets[0].id.startswith("ROLLOUT_") and node.targets[0].id.endswith("_ALERT")]
    runbook = (ROOT / "docs/runbooks/cloud-platform-deploy.md").read_text(encoding="utf-8")
    rows = {line.split("|")[1].strip().strip("`") for line in runbook.splitlines() if line.startswith("| `")}

    assert len(rollout_alerts) == 4
    for alert in (cloud_deploy.PLATFORM_NEEDED_ALERT, cloud_deploy.BARRIER_ALERT, *rollout_alerts):
        assert alert in rows, alert
