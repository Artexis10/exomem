"""The cloud-deploy workflow's script (automate-cloud-releases task 1.12), on recorded
API answers (tests/fixtures/cloud-deploy/answers.json).

Each case runs the script's `main` with a transport that serves recorded GitHub and
Substrate answers, and reads what the public Actions log would show.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import sys
from datetime import UTC, datetime
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
IMAGE = f"ghcr.io/substrate-systems/evermatter@{DIGEST}"
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
# The run's clock: two hours after the newest recorded release's publish time (2026-11-10T09:00:00Z).
NOW = datetime(2026, 11, 10, 11, 0, tzinfo=UTC)
# What a run that finds no lag sends once it has read the view: `cloud-release-behind` and `cloud-cell-pinned`, both resolved.
LAG_RESOLVED = [(cloud_deploy.RELEASE_BEHIND_ALERT, False), (cloud_deploy.CELL_PINNED_ALERT, False)]
FAILING_RESOLVED = (cloud_deploy.FAILING_ALERT, False)
# Only the chart and controller's test and Markdown files changed since the platform's release.
TESTS_AND_DOCS = ["infra/cellctl/tests/test_reconcile.py", "infra/helm/platform/OWNERSHIP.md", "src/exomem/server.py"]


class Recorded:
    """Serves recorded answers by method and path, in order; the last answer repeats."""

    def __init__(self, rollout: list[str], put: str = "substrate_put_ok", deployments: list | None = None,
                 releases: str = "github_releases") -> None:
        self.rollout = [copy.deepcopy(ANSWERS[name]) for name in rollout]
        self.releases = releases if releases in ("unavailable", "garbled") else copy.deepcopy(ANSWERS[releases])
        self.put = ANSWERS[put]
        self.deployments = ANSWERS["github_platform_deployments"] if deployments is None else deployments
        self.calls: list[tuple[str, str, bytes | None]] = []

    def __call__(self, method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, bytes]:
        self.calls.append((method, url, body))
        path = url.split(".test", 1)[1]
        if path == "/repos/example-owner/example-repo/releases/latest":
            return 200, json.dumps(ANSWERS["github_release_latest"]).encode()
        if path == f"/repos/example-owner/example-repo/releases?per_page={cloud_deploy.RELEASE_SCAN_LIMIT}":
            if self.releases == "unavailable":
                return 502, b"{}"
            if self.releases == "garbled":
                return 200, b"<html>busy</html>"
            return 200, json.dumps(self.releases).encode()
        if path.startswith("/repos/example-owner/example-repo/deployments?"):
            return 200, json.dumps(self.deployments).encode()
        if path.startswith("/repos/example-owner/example-repo/deployments/") and "/statuses?" in path:
            succeeded = path.split("/")[5] == "9002"
            return 200, json.dumps(ANSWERS["github_statuses_success" if succeeded else "github_statuses_failure"]).encode()
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
        image=lambda tag: IMAGE,
        attest=lambda image, tag: attested,
        changed_paths=lambda base, head: TESTS_AND_DOCS if changed is None else changed,
        sleep=slept.append,
        monotonic=lambda: next(clock),
        now=lambda: NOW,
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
    assert recorded.alerts() == [*LAG_RESOLVED, (cloud_deploy.PLATFORM_NEEDED_ALERT, False),
                                 (cloud_deploy.BARRIER_ALERT, False), FAILING_RESOLVED]


def test_an_unattested_digest_sets_nothing(capsys) -> None:
    # Catches a pushed image that reaches the fleet.
    recorded = Recorded(["substrate_rollout_behind"])

    code, log, _ = _run(recorded, capsys, attested=False)

    assert (code, recorded.puts()) == (1, [])
    assert "ATTESTATION_UNVERIFIED" in log


def test_an_image_is_verified_against_the_repository_that_built_it_and_no_other(monkeypatch) -> None:
    # Catches a rollback to a release cut before the repository moved that no builder
    # vouches for, a new release checked against the old repository, and an image from
    # outside the two repositories that ever built these images.
    commands: list[list[str]] = []

    def run(command, **_):
        commands.append(command)
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(cloud_deploy.subprocess, "run", run)

    assert cloud_deploy.verify_attestation(f"ghcr.io/artexis10/exomem@{DIGEST}", "v0.200.0")
    assert cloud_deploy.verify_attestation(IMAGE, "v0.201.0")
    builders = [(command[command.index("--repo") + 1], command[command.index("--signer-workflow") + 1])
                for command in commands]
    assert builders == [
        ("Artexis10/exomem", "Artexis10/exomem/.github/workflows/release-please.yml"),
        ("substrate-systems/evermatter", "substrate-systems/evermatter/.github/workflows/release-please.yml"),
    ]

    commands.clear()
    for foreign in (f"ghcr.io/example-owner/evermatter@{DIGEST}", f"ghcr.io/artexis10/exomem-cellctl@{DIGEST}"):
        with pytest.raises(cloud_deploy.Stop) as refused:
            cloud_deploy.verify_attestation(foreign, "v0.201.0")
        assert refused.value.code == "CELL_IMAGE_REPOSITORY_UNKNOWN"
    assert commands == []


def test_the_deploy_chart_and_release_name_the_same_cell_image_repositories() -> None:
    # Catches a repository move that updates one copy of the cell image repositories and
    # not the others: the deploy would resolve an image the chart refuses, or the release
    # would publish where the deploy never looks.
    chart = ROOT / "infra/helm/platform"
    schema = json.loads((chart / "values.schema.json").read_text(encoding="utf-8"))
    platform_jobs_image = re.compile(schema["properties"]["platformJobs"]["properties"]["image"]["pattern"])
    for repository in cloud_deploy.CELL_IMAGE_BUILDERS:
        assert platform_jobs_image.fullmatch(f"{repository}@{DIGEST}"), repository
    assert not platform_jobs_image.fullmatch(f"ghcr.io/example-owner/evermatter@{DIGEST}")

    validation = yaml.safe_load((chart / "values.validation.yaml").read_text(encoding="utf-8"))
    pairs = {source["repository"]: source["builder"] for source in validation["cellctl"]["cellImageRepositories"]}
    assert pairs == cloud_deploy.CELL_IMAGE_BUILDERS

    workflow = yaml.safe_load((ROOT / ".github/workflows/release-please.yml").read_text(encoding="utf-8"))
    steps = [step for job in workflow["jobs"].values() for step in job.get("steps", [])]
    # A release publishes where its build outputs push and its attestations name the subject.
    # The build-push `outputs` value is a comma-separated key=value list fixed by the action.
    published = {
        field.removeprefix("name=")
        for step in steps
        for field in str(step.get("with", {}).get("outputs", "")).split(",")
        if field.startswith("name=")
    } | {
        step["with"]["subject-name"]
        for step in steps
        if str(step.get("with", {}).get("subject-name", "")).startswith("ghcr.io/")
    }
    newest = next(iter(cloud_deploy.CELL_IMAGE_BUILDERS))
    assert published == {newest, f"{newest}-cellctl"}


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


def test_a_rerun_for_the_release_the_rollout_paused_on_fails_with_the_pause_code(capsys) -> None:
    # Catches a re-run that reports success while the fleet is paused on this very release.
    recorded = Recorded(["substrate_rollout_paused_on_the_release"])

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
    assert recorded.alerts() == [*LAG_RESOLVED, (cloud_deploy.PLATFORM_NEEDED_ALERT, True), FAILING_RESOLVED]
    assert log.splitlines()[-1] == "cloud-deploy: cloud-release-platform-needed 1 platform input(s) changed"


def test_no_platform_deployment_sets_nothing_and_raises_the_platform_alert(capsys) -> None:
    recorded = Recorded(["substrate_rollout_behind"], deployments=[])

    code, log, _ = _run(recorded, capsys)

    assert (code, recorded.puts()) == (1, [])
    assert recorded.alerts() == [*LAG_RESOLVED, (cloud_deploy.PLATFORM_NEEDED_ALERT, True), FAILING_RESOLVED]
    assert "NO_PLATFORM_DEPLOYMENT" in log


def test_the_platform_gate_reads_statuses_only_down_to_the_newest_successful_deployment(capsys) -> None:
    # Catches one status call per platform Deployment ever recorded: the newest one
    # failed, the next one succeeded, and nothing older is read.
    recorded = Recorded(["substrate_rollout_behind", "substrate_rollout_canary_on_target"])

    code, log, _ = _run(recorded, capsys)

    assert code == 0 and "CANARY_ON_TARGET" in log
    statuses = [url.split("/")[-2] for _, url, _ in recorded.calls if "/statuses?" in url]
    assert statuses == ["9003", "9002"]


def test_a_full_page_of_unsuccessful_deployments_fails_visibly_and_sets_nothing(capsys) -> None:
    # Catches a scan that runs out of reads and treats the platform as absent or crossed.
    failed = [{"id": 8000 + index, "ref": "v0.171.0", "payload": {}} for index in range(cloud_deploy.PLATFORM_SCAN_LIMIT)]
    recorded = Recorded(["substrate_rollout_behind"], deployments=failed)

    code, log, _ = _run(recorded, capsys)

    assert code == 1
    assert "PLATFORM_DEPLOYMENT_SCAN_LIMIT" in log
    assert recorded.puts() == [] and recorded.alerts() == [*LAG_RESOLVED, (cloud_deploy.FAILING_ALERT, True)]


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

    assert (code, recorded.puts(), recorded.alerts()) == (0, [], [*LAG_RESOLVED, FAILING_RESOLVED])
    assert log.splitlines()[-1] == "cloud-deploy: CELL_IMAGE_CURRENT"


@pytest.mark.parametrize(("rollout", "run", "last_line"), [
    pytest.param("substrate_rollout_behind", {"changed": ["infra/helm/platform/templates/cellctl.yaml"]},
                 "cloud-deploy: cloud-release-platform-needed 1 platform input(s) changed", id="platform gate"),
    pytest.param("substrate_rollout_paused", {}, "cloud-deploy: ROLLOUT_PAUSED UPGRADE_READINESS_TIMEOUT",
                 id="paused rollout"),
])
def test_a_fleet_that_missed_a_release_for_over_two_days_raises_the_alert_whichever_stop_ends_the_run(
        capsys, rollout: str, run: dict, last_line: str) -> None:
    # Catches an alert measured from the newest release: it is 2 hours old, as releases come every few
    # hours, so it would never fire. The fleet missed v0.170.1 49 hours ago and three releases since.
    recorded = Recorded([rollout], releases="github_releases_fleet_several_behind")

    code, log, _ = _run(recorded, capsys, **run)

    assert code == 1
    assert (cloud_deploy.RELEASE_BEHIND_ALERT, True) in recorded.alerts()
    assert "cloud-deploy: RELEASE_BEHIND hours=49" in log
    assert log.splitlines()[-1] == last_line


def test_the_days_before_a_missed_release_counts_as_behind_come_from_the_repository_variable(capsys) -> None:
    # Catches a threshold that only a code change can move: with 72 hours, the release missed 49
    # hours ago is not behind yet.
    recorded = Recorded(["substrate_rollout_behind", "substrate_rollout_canary_on_target"],
                        releases="github_releases_fleet_several_behind")

    _, log, _ = _run(recorded, capsys, env={**ENV, "CLOUD_DEPLOY_RELEASE_BEHIND_HOURS": "72"})

    assert recorded.alerts()[0] == (cloud_deploy.RELEASE_BEHIND_ALERT, False)
    assert "RELEASE_BEHIND" not in log


@pytest.mark.parametrize("hours", ["0", "-3", "1.5", "two"])
def test_a_release_behind_threshold_that_is_not_a_positive_whole_number_stops_the_run_at_start(hours: str) -> None:
    # Catches a zero or negative threshold that would raise the alert on every run, as the poll settings
    # stop a run on a value they cannot read.
    with pytest.raises(ValueError, match="CLOUD_DEPLOY_RELEASE_BEHIND_HOURS|invalid literal"):
        cloud_deploy.Settings.from_env({**ENV, "CLOUD_DEPLOY_RELEASE_BEHIND_HOURS": hours})


def test_a_fleet_that_missed_only_a_young_release_is_not_behind_yet(capsys) -> None:
    # Catches an alert on every release's first hours, before its rollout can finish.
    recorded = Recorded(["substrate_rollout_behind", "substrate_rollout_canary_on_target"])

    _, log, _ = _run(recorded, capsys)

    assert recorded.alerts()[0] == (cloud_deploy.RELEASE_BEHIND_ALERT, False)
    assert "RELEASE_BEHIND" not in log


@pytest.mark.parametrize(("cell_image", "release"), [
    pytest.param("ghcr.io/example/other@sha256:" + "4" * 64, "v0.171.0", id="newest release by tag"),
    pytest.param(IMAGE, "v0.170.0", id="newest release by digest"),
    pytest.param("ghcr.io/example/other@sha256:" + "4" * 64, "v0.172.0", id="ahead of the newest release"),
    pytest.param("ghcr.io/example/other@sha256:" + "4" * 64, "not-a-release", id="a tag that is not a release"),
])
def test_a_fleet_that_is_current_or_ahead_or_unreadable_is_not_behind(capsys, cell_image: str, release: str) -> None:
    # Catches an alert on a fleet that has everything the newest release has, with five missed
    # releases on the page that would make a wrong comparison fire it.
    recorded = Recorded(["substrate_rollout_behind", "substrate_rollout_canary_on_target"],
                        releases="github_releases_fleet_several_behind")
    fleet = recorded.rollout[0]["view"]
    fleet["cell_image"], fleet["release"] = cell_image, release

    _, log, _ = _run(recorded, capsys)

    assert recorded.alerts()[0] == (cloud_deploy.RELEASE_BEHIND_ALERT, False)
    assert "RELEASE_BEHIND" not in log


def test_a_fleet_release_that_substrate_does_not_report_sends_no_lag_transition_and_says_so(capsys) -> None:
    # Catches an unknown fleet release read as current, which would resolve an alert no run could raise.
    recorded = Recorded(["substrate_rollout_behind", "substrate_rollout_canary_on_target"],
                        releases="github_releases_fleet_several_behind")
    recorded.rollout[0]["view"]["release"] = None

    code, log, _ = _run(recorded, capsys)

    assert code == 0
    assert cloud_deploy.RELEASE_BEHIND_ALERT not in [alert for alert, _ in recorded.alerts()]
    assert log.splitlines().count("cloud-deploy: RELEASE_BEHIND_UNKNOWN") == 1


@pytest.mark.parametrize("published_at", ["not a time", None, "2026-11-08T10:00:00"],
                         ids=["unparseable", "missing", "no zone"])
def test_a_publish_time_that_cannot_be_read_sends_no_lag_transition_and_the_run_goes_on(
        capsys, published_at: str | None) -> None:
    # Catches an unknown publish time read as "not behind", and a GitHub oddity that blocks every rollout.
    recorded = Recorded(["substrate_rollout_behind", "substrate_rollout_canary_on_target"])
    recorded.releases[0]["published_at"] = published_at

    code, log, _ = _run(recorded, capsys)

    assert code == 0
    assert cloud_deploy.RELEASE_BEHIND_ALERT not in [alert for alert, _ in recorded.alerts()]
    assert "cloud-deploy: RELEASE_TIME_UNREADABLE" in log
    assert log.splitlines()[-1] == "cloud-deploy: CANARY_ON_TARGET"


@pytest.mark.parametrize(("run", "failing"), [
    pytest.param({"attested": False}, True, id="unattested image"),
    pytest.param({"changed": ["infra/helm/platform/templates/cellctl.yaml"]}, False, id="platform gate has its own alert"),
    pytest.param({}, False, id="run ends ok"),
])
def test_a_run_that_ends_on_a_failing_stop_without_an_alert_of_its_own_sends_the_failing_alert(
        capsys, run: dict, failing: bool) -> None:
    # Catches a daily run that goes red for a cause no alert names, and an alert that stays active
    # after the cause is fixed or after another alert took it over.
    recorded = Recorded(["substrate_rollout_behind", "substrate_rollout_canary_on_target"])

    _run(recorded, capsys, **run)

    assert recorded.alerts()[-1] == (cloud_deploy.FAILING_ALERT, failing)


@pytest.mark.parametrize(("rollout", "active"), [("substrate_rollout_cell_pinned", True),
                                                 ("substrate_rollout_behind", False)])
def test_cells_pinned_off_the_fleet_image_raise_the_alert_and_none_resolve_it(
        capsys, rollout: str, active: bool) -> None:
    # Catches a cell that stays on an old image, outside every rollout, with no one told.
    recorded = Recorded([rollout, "substrate_rollout_canary_on_target"])

    _, log, _ = _run(recorded, capsys)

    assert (cloud_deploy.CELL_PINNED_ALERT, active) in recorded.alerts()
    assert ("cloud-deploy: CELLS_PINNED 1" in log) is active
    assert "PINNED_UNKNOWN" not in log


@pytest.mark.parametrize("count", ["absent", -1, True], ids=["absent", "negative", "boolean"])
def test_a_pin_count_that_substrate_does_not_report_sends_nothing_and_says_so(capsys, count: object) -> None:
    # Catches Substrate before the companion change reading as "no pinned cells", which would
    # resolve an alert that no run could have raised.
    recorded = Recorded(["substrate_rollout_behind", "substrate_rollout_canary_on_target"])
    cells = recorded.rollout[0]["view"]["cells"]
    if count == "absent":
        del cells["pinned_off_fleet"]
    else:
        cells["pinned_off_fleet"] = count

    _, log, _ = _run(recorded, capsys)

    assert cloud_deploy.CELL_PINNED_ALERT not in [alert for alert, _ in recorded.alerts()]
    assert log.splitlines().count("cloud-deploy: PINNED_UNKNOWN") == 1


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


def test_the_release_tag_run_dispatches_the_workflow_on_main_only() -> None:
    # Catches a release that publishes and never deploys (v0.172.0-v0.174.0: the tag
    # run's completion fired no workflow_run), a deploy token that a branch can reach, and a
    # dropped daily schedule, which leaves a fleet behind with nothing to re-check it.
    workflow = yaml.safe_load((ROOT / ".github/workflows/cloud-deploy.yml").read_text(encoding="utf-8"))
    triggers = workflow[True]  # YAML 1.1 reads the `on` key as true.
    (job,) = workflow["jobs"].values()
    release = yaml.safe_load((ROOT / ".github/workflows/release-please.yml").read_text(encoding="utf-8"))
    dispatch = release["jobs"]["deploy-cells"]

    assert set(triggers) == {"workflow_dispatch", "schedule"}  # No trigger that a branch or pull request reaches.
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert job["environment"] == "cloud-deploy"
    assert workflow["concurrency"] == {"group": "cloud-deploy", "cancel-in-progress": False}
    assert set(workflow["permissions"].values()) == {"read"}
    assert "publish-image" in dispatch["needs"]
    assert "needs.publish-image.result == 'success'" in dispatch["if"]
    assert "needs.plan.outputs.dry_run != 'true'" in dispatch["if"]
    assert dispatch["steps"][-1]["run"].startswith("gh workflow run cloud-deploy.yml --ref main")


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
    for alert in (cloud_deploy.PLATFORM_NEEDED_ALERT, cloud_deploy.BARRIER_ALERT, cloud_deploy.RELEASE_BEHIND_ALERT,
                  cloud_deploy.CELL_PINNED_ALERT, cloud_deploy.FAILING_ALERT, *rollout_alerts):
        assert alert in rows, alert


def test_a_changed_chart_template_with_a_non_ascii_name_still_reaches_the_platform_gate(tmp_path, monkeypatch) -> None:
    # Catches git's quoting of a non-ASCII path, which then matched no platform glob and raised no alert.
    import subprocess

    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-q")
    git("-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-q", "--allow-empty", "-m", "base")
    base = git("rev-parse", "HEAD")
    template = tmp_path / "infra/helm/platform/templates/café.yaml"
    template.parent.mkdir(parents=True)
    template.write_text("kind: ConfigMap\n", encoding="utf-8")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-q", "-m", "chart")
    monkeypatch.setattr(cloud_deploy, "ROOT", tmp_path)

    paths = cloud_deploy.git_changed_paths(base, git("rev-parse", "HEAD"))

    assert paths == ["infra/helm/platform/templates/café.yaml"]
    assert cloud_deploy.platform_inputs(paths, ["infra/helm/platform/**"], [])


@pytest.mark.parametrize("releases", ["unavailable", "garbled"])
def test_a_release_list_that_cannot_be_read_still_lets_the_rollout_go_on(capsys, releases) -> None:
    # Catches a read that only feeds the lag alert stopping a deploy for a day, whether it fails or answers 200 with no JSON.
    recorded = Recorded(["substrate_rollout_behind", "substrate_rollout_canary_on_target"], releases=releases)

    code, log, _ = _run(recorded, capsys)

    assert code == 0
    assert recorded.puts()
    assert "cloud-deploy: RELEASE_BEHIND_UNKNOWN" in log
    assert log.splitlines()[-1] == "cloud-deploy: CANARY_ON_TARGET"


def test_the_run_that_sets_the_newest_release_resolves_the_lag_it_reported(capsys) -> None:
    # Catches an "active" email at the moment the fix lands, with "resolved" a day later.
    recorded = Recorded(["substrate_rollout_behind", "substrate_rollout_canary_on_target"],
                        releases="github_releases_fleet_several_behind")

    code, _, _ = _run(recorded, capsys)

    lag = [active for alert, active in recorded.alerts() if alert == cloud_deploy.RELEASE_BEHIND_ALERT]
    assert code == 0
    assert lag == [True, False]


def test_a_paused_rollout_leaves_the_failing_alert_to_cellctls_pause_alert(capsys) -> None:
    # Catches a deliberate hold emailing twice a day for one cause: cellctl already raises cloud-rollout-paused.
    recorded = Recorded(["substrate_rollout_paused"])

    _run(recorded, capsys)

    assert recorded.alerts()[-1] == (cloud_deploy.FAILING_ALERT, False)
