"""infra/scripts/platform_upgrade.py on recorded tool output (task 4.2).

Every case runs the command with a runner that serves recorded answers to git,
helm, kubectl and gh, and asserts on the argv the command ran -- that a review
failure stops before any change, that a one-way barrier drops --atomic, and
that a success records the platform Deployment and dispatches cloud-deploy.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("platform_upgrade", ROOT / "infra/scripts/platform_upgrade.py")
assert spec and spec.loader
pu = importlib.util.module_from_spec(spec)
sys.modules["platform_upgrade"] = pu
spec.loader.exec_module(pu)

LIVE_TAG = "v0.171.0"
TAG = "v0.175.0"
REPOSITORY = "example-owner/example-repo"

LIVE_MANIFEST = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cellctl
  namespace: exomem-cloud
spec:
  template:
    spec:
      containers:
        - name: cellctl
          image: cellctl@sha256:1111
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: platform-config
  namespace: exomem-platform
"""

NEW_MANIFEST = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cellctl
  namespace: exomem-cloud
spec:
  template:
    spec:
      containers:
        - name: cellctl
          image: cellctl@sha256:2222
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: platform-config
  namespace: exomem-platform
---
apiVersion: batch/v1
kind: Job
metadata:
  name: cell-prepare-0123456789abcdef
  namespace: exo-cell-aaaaaaaaaaaaaaaa
"""

OPERATOR_STEP = {"id": "secret-matrix-0.172.0", "version": "0.172.0", "scope": "platform",
                 "kind": "operator-step", "summary": "apply the re-signed active secrets first."}
ONE_WAY = {"id": "key-conversion-0.173.0", "version": "0.173.0", "scope": "platform",
           "kind": "one-way", "summary": "the key conversion cannot roll back."}
OLD_BARRIER = {"id": "secret-matrix-0.100.0", "version": "0.100.0", "scope": "platform",
               "kind": "operator-step", "summary": "an older step."}
CELL_BARRIER = {"id": "cell-something-0.173.0", "version": "0.173.0", "scope": "cell",
                "kind": "operator-step", "summary": "a cell-scope entry, never the command's."}
CELL_ONE_WAY = {"id": "cell-format-0.173.0", "version": "0.173.0", "scope": "cell",
                "kind": "one-way", "summary": "a cell-scope migration that cannot roll back."}


def env_for(tmp_path: Path) -> dict[str, str]:
    return {
        "EXOMEM_PLATFORM_VALUES": str(tmp_path / "values.yaml"),
        "EXOMEM_UPGRADE_DIR": str(tmp_path / "upgrade"),
        "EXOMEM_CELLCTL_DSN": "postgres://example.invalid/cells",
        "EXOMEM_REPOSITORY": REPOSITORY,
    }


class Recorded:
    """Answers each tool call by argv prefix and records everything."""

    def __init__(self, tmp_path: Path, *, barriers=(), schema_check=True, secret_changed=False,
                 deployments=None, statuses=None, detail=None, live_tag=LIVE_TAG,
                 failures=(), cronjobs=None, storage_class=True,
                 status_fails=None, manifest_exists=None) -> None:
        self.checkout = tmp_path / "upgrade" / "checkout"
        self.barriers = list(barriers)
        self.schema_check = schema_check
        self.secret_changed = secret_changed
        self.deployments = deployments if deployments is not None else [{"id": 9001, "ref": LIVE_TAG}]
        self.statuses = statuses if statuses is not None else [{"state": "success"}]
        self.detail = detail if detail is not None else {"payload": {"barriers_crossed": ["secret-matrix-0.100.0"]}}
        self.live_tag = live_tag
        self.failures = set(failures)
        self.cronjobs = ("".join(f"{name} suspend=false\n" for name in pu.SCHEDULER_CRONJOBS)
                         if cronjobs is None else cronjobs)
        self.storage_class = storage_class
        self.status_fails = (live_tag is None) if status_fails is None else status_fails
        self.manifest_exists = (live_tag is not None) if manifest_exists is None else manifest_exists
        self.calls: list[list[str]] = []
        self.stdin_bodies: list[str] = []

    def _materialize_checkout(self) -> None:
        if self.schema_check:
            (self.checkout / "infra/cellctl/src/cellctl").mkdir(parents=True, exist_ok=True)
            (self.checkout / "infra/cellctl/src/cellctl/schema_check.py").write_text("")
        (self.checkout / "infra/contracts").mkdir(parents=True, exist_ok=True)
        (self.checkout / "infra/contracts/upgrade-barriers-v1.json").write_text(
            json.dumps({"schema_version": 1, "barriers": self.barriers}))
        (self.checkout / "infra/helm/platform").mkdir(parents=True, exist_ok=True)
        (self.checkout / "infra/helm/platform/reservation-post-renderer.py").write_text("")

    def __call__(self, argv, cwd, stdin):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if stdin is not None:
            self.stdin_bodies.append(stdin)
        for marker in self.failures:
            if any(marker in arg for arg in argv):
                return pu.Completed(1, "", "refused by the recorded failure")
        match argv[0]:
            case "git" if argv[1] == "rev-parse":
                return pu.Completed(0, "abc123\n")
            case "git" if argv[1] == "worktree" and argv[2] == "add":
                self._materialize_checkout()
                return pu.Completed(0)
            case "git" if argv[1] == "worktree":
                return pu.Completed(0)
            case "git" if argv[1] == "diff":
                return pu.Completed(
                    0, "infra/contracts/active-secret-registry-v1.json\n" if self.secret_changed else "")
            case "git":
                return pu.Completed(0)
            case "uv":
                return pu.Completed(0)
            case "helm" if argv[1] == "status":
                if self.status_fails:
                    return pu.Completed(1, "", "release: not found")
                if self.live_tag is False:
                    return pu.Completed(0, json.dumps({"info": {"description": "Upgrade complete"}}))
                return pu.Completed(0, json.dumps({"info": {"description": f"platform-{self.live_tag}"}}))
            case "helm" if argv[1] == "get":
                if not self.manifest_exists:
                    return pu.Completed(1, "", "release: not found")
                return pu.Completed(0, LIVE_MANIFEST)
            case "helm" if argv[1] == "template":
                return pu.Completed(0, NEW_MANIFEST)
            case "helm" if argv[1] == "upgrade" and "--dry-run=server" in argv:
                return pu.Completed(0, NEW_MANIFEST)
            case "helm" if argv[1] == "upgrade":
                return pu.Completed(0, "Release has been upgraded.")
            case "kubectl" if "cronjob" in argv:
                return pu.Completed(0, self.cronjobs)
            case "kubectl" if "storageclass" in argv:
                return pu.Completed(
                    0 if self.storage_class else 1, "",
                    f'storageclasses.storage.k8s.io "{pu.STORAGE_CLASS}" not found')
            case "kubectl":
                return pu.Completed(0)
            case "gh" if argv[1] == "api" and any("statuses?per_page=1" in a for a in argv):
                return pu.Completed(0, json.dumps(self.statuses))
            case "gh" if argv[1] == "api" and any(a.endswith("/statuses") for a in argv):
                return pu.Completed(0, json.dumps({"state": "success"}))
            case "gh" if argv[1] == "api" and any("deployments?" in a for a in argv):
                return pu.Completed(0, json.dumps(self.deployments))
            case "gh" if argv[1] == "api" and "--input" in argv:
                return pu.Completed(0, json.dumps({"id": 4242}))
            case "gh" if argv[1] == "api":
                return pu.Completed(0, json.dumps(self.detail))
            case "gh" if argv[1] == "workflow":
                return pu.Completed(0)
        raise AssertionError(f"unrecorded call {argv}")

    def mutating(self) -> list[list[str]]:
        """Calls that change something outside the private upgrade directory."""
        return [c for c in self.calls
                if (c[0] == "helm" and c[1] == "upgrade" and "--dry-run=server" not in c)
                or (c[0] == "kubectl" and "apply" in c)
                or (c[0] == "gh" and "--input" in c)
                or (c[0] == "gh" and any(a.endswith("/statuses") for a in c))
                or (c[0] == "gh" and c[1] == "workflow")]


def run_upgrade(tmp_path: Path, runner_args=(), tag: str = TAG,
                **recorded) -> tuple[int, Recorded, list[str]]:
    env = env_for(tmp_path)
    runner = Recorded(tmp_path, **recorded)
    result = pu.platform_upgrade([tag, *runner_args], env, runner)
    return result, runner, runner.calls


def test_a_review_failure_stops_before_any_change(tmp_path: Path) -> None:
    for index, failure in enumerate(("infra/scripts/helm_dependencies.py", "--dry-run=server", "schema_check")):
        result, runner, _ = run_upgrade(tmp_path / f"case{index}", failures=[failure])
        assert result == 1, failure
        assert runner.mutating() == [], (failure, runner.calls)


def test_a_secret_contract_change_with_no_barrier_stops_the_command(tmp_path: Path) -> None:
    result, runner, _ = run_upgrade(tmp_path, secret_changed=True)
    assert result == 1
    assert runner.mutating() == []


def test_a_secret_contract_change_with_a_barrier_entry_needs_crossing(tmp_path: Path) -> None:
    result, runner, _ = run_upgrade(tmp_path, secret_changed=True, barriers=[OPERATOR_STEP])
    assert result == 1  # the entry exists but nothing passed --crossed
    assert runner.mutating() == []

    result, runner, _ = run_upgrade(tmp_path, ("--crossed", "secret-matrix-0.172.0"),
                                    secret_changed=True, barriers=[OPERATOR_STEP])
    assert result == 0
    assert any("--atomic" in c for c in runner.calls if c[:2] == ["helm", "upgrade"]
               and "--dry-run=server" not in c)


def test_an_unknown_crossed_id_is_refused(tmp_path: Path) -> None:
    result, runner, _ = run_upgrade(tmp_path, ("--crossed", "not-a-barrier"))
    assert result == 1
    assert runner.mutating() == []


def test_a_crossed_one_way_barrier_removes_atomic(tmp_path: Path) -> None:
    result, runner, _ = run_upgrade(tmp_path, ("--crossed", "key-conversion-0.173.0"),
                                    barriers=[ONE_WAY])
    assert result == 0
    upgrade = next(c for c in runner.calls if c[:2] == ["helm", "upgrade"] and "--dry-run=server" not in c)
    assert "--atomic" not in upgrade


def test_an_operator_step_barrier_keeps_atomic(tmp_path: Path) -> None:
    result, runner, _ = run_upgrade(tmp_path, ("--crossed", "secret-matrix-0.172.0"),
                                    barriers=[OPERATOR_STEP])
    assert result == 0
    upgrade = next(c for c in runner.calls if c[:2] == ["helm", "upgrade"] and "--dry-run=server" not in c)
    assert "--atomic" in upgrade


def test_a_cell_scope_barrier_between_releases_needs_no_crossed(tmp_path: Path) -> None:
    result, runner, _ = run_upgrade(tmp_path, barriers=[CELL_BARRIER])
    assert result == 0


def test_a_cell_scope_barrier_accepts_a_crossed_id_and_records_it(tmp_path: Path) -> None:
    result, runner, _ = run_upgrade(tmp_path, ("--crossed", "cell-format-0.173.0"),
                                    barriers=[CELL_ONE_WAY])
    assert result == 0
    bodies = [json.loads(b) for b in runner.stdin_bodies if b.lstrip().startswith('{')]
    deployment = next(b for b in bodies if b.get("environment") == "cloud-platform")
    assert sorted(deployment["payload"]["barriers_crossed"]) == [
        "cell-format-0.173.0", "secret-matrix-0.100.0"]
    # A cell-scope one-way changes nothing helm would roll back, so --atomic stays.
    upgrade = next(c for c in runner.calls if c[:2] == ["helm", "upgrade"]
                   and "--dry-run=server" not in c)
    assert "--atomic" in upgrade


def test_the_dry_run_and_the_upgrade_share_the_post_renderer(tmp_path: Path) -> None:
    result, runner, _ = run_upgrade(tmp_path)
    assert result == 0
    dry_run = next(c for c in runner.calls if "--dry-run=server" in c)
    upgrade = next(c for c in runner.calls if c[:2] == ["helm", "upgrade"]
                   and "--dry-run=server" not in c)
    assert "--post-renderer" in dry_run
    assert "--post-renderer" in upgrade


def test_a_tag_older_than_the_live_release_is_refused(tmp_path: Path) -> None:
    result, runner, _ = run_upgrade(tmp_path, tag="v0.170.0")
    assert result == 1
    assert runner.mutating() == []


def test_a_live_release_helm_status_cannot_read_needs_from(tmp_path: Path) -> None:
    result, runner, _ = run_upgrade(tmp_path, live_tag=None,
                                    status_fails=True, manifest_exists=True)
    assert result == 1
    assert runner.mutating() == []
    result, runner, _ = run_upgrade(tmp_path, ("--from", LIVE_TAG), live_tag=None,
                                    status_fails=True, manifest_exists=True)
    assert result == 0


def test_a_suspended_scheduler_cronjob_fails_the_verify(tmp_path: Path) -> None:
    result, runner, _ = run_upgrade(
        tmp_path,
        cronjobs="exomem-hosted-scheduler-exomem-access-delivery suspend=true\n"
                 "exomem-hosted-scheduler-exomem-reconcile suspend=false\n")
    assert result == 1
    assert any("state=failure" in " ".join(c) for c in runner.calls if "/statuses" in " ".join(c))
    assert not any(c[:3] == ["gh", "workflow", "run"] for c in runner.calls)


def test_a_missing_storage_class_fails_the_verify(tmp_path: Path) -> None:
    result, runner, _ = run_upgrade(tmp_path, storage_class=False)
    assert result == 1
    assert any("state=failure" in " ".join(c) for c in runner.calls if "/statuses" in " ".join(c))
    assert not any(c[:3] == ["gh", "workflow", "run"] for c in runner.calls)


def test_a_success_records_the_deployment_and_dispatches(tmp_path: Path) -> None:
    result, runner, _ = run_upgrade(tmp_path, ("--crossed", "secret-matrix-0.172.0"),
                                    barriers=[OPERATOR_STEP, OLD_BARRIER])
    assert result == 0

    bodies = [json.loads(b) for b in runner.stdin_bodies if b.lstrip().startswith('{')]
    deployment = next(b for b in bodies if b.get("environment") == "cloud-platform")
    assert deployment["ref"] == TAG
    assert sorted(deployment["payload"]["barriers_crossed"]) == [
        "secret-matrix-0.100.0", "secret-matrix-0.172.0"]

    statuses = [c for c in runner.calls if "deployments/4242/statuses" in " ".join(c)]
    assert any("state=success" in " ".join(c) for c in statuses)
    assert not any("state=failure" in " ".join(c) for c in statuses)
    assert any(c[:3] == ["gh", "workflow", "run"] and "cloud-deploy.yml" in c and "--ref" in c and "main" in c
               for c in runner.calls)


def test_the_first_platform_deployment_lists_every_barrier_at_or_below_the_tag(tmp_path: Path) -> None:
    result, runner, _ = run_upgrade(tmp_path, deployments=[], barriers=[OPERATOR_STEP, OLD_BARRIER],
                                    runner_args=["--crossed", "secret-matrix-0.172.0"])
    assert result == 0
    bodies = [json.loads(b) for b in runner.stdin_bodies if b.lstrip().startswith('{')]
    deployment = next(b for b in bodies if b.get("environment") == "cloud-platform")
    assert sorted(deployment["payload"]["barriers_crossed"]) == [
        "secret-matrix-0.100.0", "secret-matrix-0.172.0"]


def test_a_failed_upgrade_records_failure_and_dispatches_nothing(tmp_path: Path) -> None:
    result, runner, _ = run_upgrade(tmp_path, failures=["deployment/exomem-cloud-gateway"])
    assert result == 1
    bodies = [json.loads(b) for b in runner.stdin_bodies if b.lstrip().startswith('{')]
    deployment = next(b for b in bodies if b.get("environment") == "cloud-platform")
    assert deployment["ref"] == TAG
    assert deployment["payload"]["barriers_crossed"] == ["secret-matrix-0.100.0"]
    assert any("state=failure" in " ".join(c) for c in runner.calls if "/statuses" in " ".join(c))
    assert not any("state=success" in " ".join(c) for c in runner.calls if "/statuses" in " ".join(c))
    assert not any(c[:3] == ["gh", "workflow", "run"] for c in runner.calls)


def test_a_platform_with_no_description_needs_from(tmp_path: Path) -> None:
    result, runner, _ = run_upgrade(tmp_path, live_tag=False)
    assert result == 1
    assert runner.mutating() == []

    result, runner, _ = run_upgrade(tmp_path, ("--from", LIVE_TAG), live_tag=False)
    assert result == 0


def test_every_upgrade_writes_the_release_description(tmp_path: Path) -> None:
    result, runner, _ = run_upgrade(tmp_path)
    assert result == 0
    upgrade = next(c for c in runner.calls if c[:2] == ["helm", "upgrade"] and "--dry-run=server" not in c)
    assert f"platform-{TAG}" in upgrade
