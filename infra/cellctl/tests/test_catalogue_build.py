"""A pre-attempt build cannot bypass placement, monopolize a node or delay a forced stop."""

from __future__ import annotations

import fnmatch
import json
import shlex
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

import pytest

from cellctl import main, reconcile
from cellctl.catalogue_build import BuildConfig, BuildJob
from cellctl.k8s_client import ClusterClient
from cellctl.manifests import (
    CATALOGUE_BACKUP_EXCLUDES,
    CATALOGUE_BUILD_NODE_ANNOTATION,
    JOB_KIND_CATALOGUE_BUILD,
    JOB_KIND_LABEL,
    CellManifestSpec,
    ResourceSettings,
    catalogue_build_name,
    namespace_name,
    render_backup_job,
    render_catalogue_build_job,
    render_network_policies,
    render_resource_quota,
)
from cellctl.state import CellRow, ClusterObservation, RolloutRow
from cellctl.upgrade_window import Idle, WindowConfig, is_due

from .test_platform_chart_headers import _find, _helm_template
from .test_reconcile import IMAGE_A, IMAGE_B, FakeClusterGateway, _cluster_config

NOW = datetime(2026, 1, 1, 16, 5, tzinfo=UTC)
WINDOWS = WindowConfig(True, 14, 0.5, 5, 2, 5, 10)
PROFILE = ResourceSettings("2", "2", "3Gi", "3Gi")
CONFIG = replace(_cluster_config(), catalogue_prewarm=BuildConfig(PROFILE, 1800))
CELL = "aaaaaaaaaaaaaaaa"


def _row(cell=CELL, **kwargs):
    return CellRow(cell, uuid.UUID(int=1), 10, 0, "running", None, 1, ready=True, **kwargs)


def _observation(**kwargs):
    fields = dict(pod_exists=True, pod_ready=True, pod_node="worker-one", ready_pod_image=IMAGE_A,
                  statefulset_exists=True, statefulset_image=IMAGE_A,
                  statefulset_upgrade_window="16 default", ready_pod_ip="10.42.0.2")
    return ClusterObservation(**{**fields, **kwargs})


class BuildCluster(FakeClusterGateway):
    def __init__(self):
        super().__init__()
        self.builds = {}

    def observe_catalogue_builds(self):
        return self.builds

    def run_job(self, manifest):
        super().run_job(manifest)
        if manifest["metadata"]["labels"].get(JOB_KIND_LABEL) != JOB_KIND_CATALOGUE_BUILD:
            return
        name, namespace = manifest["metadata"]["name"], manifest["metadata"]["namespace"]
        pod = manifest["spec"]["template"]["spec"]
        self.builds[namespace] = (BuildJob(name, pod["containers"][0]["image"],
                                         manifest["metadata"]["annotations"][CATALOGUE_BUILD_NODE_ANNOTATION],
                                         NOW, running=True, pods_exist=True),)


def test_target_build_cannot_gain_credentials_or_bypass_scheduler_capacity():
    """The target parser must share the serving node and lock namespace at full estimated peak requests."""
    spec = CellManifestSpec(CELL, IMAGE_B, 1, False, catalogue_build_resources=PROFILE)
    job = render_catalogue_build_job(spec, deadline_seconds=1800, node="worker-one")
    pod = job["spec"]["template"]["spec"]
    assert job["metadata"]["name"] == catalogue_build_name(IMAGE_B)
    assert job["spec"]["activeDeadlineSeconds"] == 1800
    assert "nodeName" not in pod and "priorityClassName" not in pod
    assert pod["affinity"]["podAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"] == [{
        "labelSelector": {"matchLabels": {"statefulset.kubernetes.io/pod-name": "cell-0"}},
        "topologyKey": "kubernetes.io/hostname"}]
    container = pod["containers"][0]
    assert container["image"] == IMAGE_B
    assert container["resources"] == {"requests": {"cpu": "2", "memory": "3Gi"},
                                      "limits": {"cpu": "2", "memory": "3Gi"}}
    assert container["command"][:4] == ["sh", "-c", 'ulimit -c 0 && exec "$@"', "cell-process"]
    assert container["command"][4:] == ["python", "-m", "exomem.catalogue_prewarm", "--vault", "/data/vault"]
    assert container["env"] == [{"name": "TMPDIR", "value": "/data/scratch/tmp"}]
    assert pod["securityContext"]["runAsUser"] == pod["securityContext"]["runAsGroup"] == 10001
    runtime = next(policy for policy in render_network_policies(spec) if policy["metadata"]["name"] == "runtime-ingress")
    assert any(job["spec"]["template"]["metadata"]["labels"].get(key) != value
               for key, value in runtime["spec"]["podSelector"]["matchLabels"].items())
    assert pod["automountServiceAccountToken"] is False
    assert pod["volumes"] == [{"name": "data", "persistentVolumeClaim": {"claimName": "cell-data", "readOnly": False}},
                               {"name": "tmp", "emptyDir": {}}]
    egress = next(p for p in render_network_policies(spec) if p["metadata"]["name"] == "job-egress")
    assert JOB_KIND_CATALOGUE_BUILD in egress["spec"]["podSelector"]["matchExpressions"][1]["values"]


@pytest.mark.parametrize("local,pods", [(False, "5"), (True, "6")])
def test_build_profile_adds_exactly_one_quota_slot_without_spending_ordinary_headroom(local, pods):
    spec = CellManifestSpec(CELL, IMAGE_B, 1, False, local_volume=local)
    before = render_resource_quota(spec)["spec"]["hard"]
    after = render_resource_quota(replace(spec, catalogue_build_resources=PROFILE))["spec"]["hard"]
    assert after["pods"] == pods
    assert int(after["pods"]) == int(before["pods"]) + 1
    assert after["persistentvolumeclaims"] == before["persistentvolumeclaims"]
    from cellctl.capacity import quantity
    for key in ("requests.cpu", "limits.cpu", "requests.memory", "limits.memory"):
        expected = "2" if key.endswith("cpu") else "3Gi"
        assert quantity(after[key]) - quantity(before[key]) == quantity(expected)


def test_chart_flag_off_keeps_build_admission_and_configuration_inert():
    """A default installation must preserve ordinary upgrade scheduling and namespace limits."""
    docs = _helm_template()
    deployment = _find(docs, "Deployment", "cellctl")
    env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    assert not any(entry["name"].startswith("CELLCTL_CATALOGUE_") for entry in env)
    scope = _find(docs, "ValidatingAdmissionPolicy", "exomem-cellctl-scope")
    assert not any("catalogue-build" in rule["expression"] for rule in scope["spec"]["validations"])
    assert main.build_catalogue_prewarm() is None
    spec = CellManifestSpec(CELL, IMAGE_A, 1, False)
    assert render_resource_quota(spec)["spec"]["hard"]["pods"] == "4"
    assert JOB_KIND_CATALOGUE_BUILD not in next(p for p in render_network_policies(spec)
        if p["metadata"]["name"] == "job-egress")["spec"]["podSelector"]["matchExpressions"][1]["values"]


def test_enabled_chart_delivers_profile_timeout_admission_and_quota_together(monkeypatch):
    """A valid build must not be stranded by missing controller configuration or quota admission."""
    docs = _helm_template("--set", "cellctl.cataloguePrewarm=true")
    deployment = _find(docs, "Deployment", "cellctl")
    env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    for entry in env:
        if entry["name"].startswith("CELLCTL_CATALOGUE_"):
            monkeypatch.setenv(entry["name"], entry["value"])
    assert main.build_catalogue_prewarm() == BuildConfig(PROFILE, 1800)
    scope = _find(docs, "ValidatingAdmissionPolicy", "exomem-cellctl-scope")
    rules = "\n".join(rule["expression"] for rule in scope["spec"]["validations"])
    assert "^catalogue-build-[0-9a-f]{16}$" in rules
    assert "'statefulset.kubernetes.io/pod-name': 'cell-0'" in rules
    assert "['2', '4', '5', '6']" in rules
    assert "'catalogue-build' in variables.np.podSelector" in rules


@pytest.mark.parametrize("read_only", [False, True])
def test_every_backup_excludes_only_state_catalogue_families_and_sidecars(read_only):
    """Hourly and stopped backups must not upload staged databases or exclude similarly named vault notes."""
    spec = CellManifestSpec(CELL, IMAGE_A, 1, False, hold_started_at=NOW.isoformat())
    job = render_backup_job(spec, bucket_name="backups", endpoint="https://storage.example", claim_name="backup-clone" if read_only else None)
    script = job["spec"]["template"]["spec"]["containers"][0]["command"][2]
    args = shlex.split(script)
    excludes = [args[i + 1] for i, arg in enumerate(args) if arg == "--exclude"]
    assert excludes == list(CATALOGUE_BACKUP_EXCLUDES)
    for name in (".lexical-staged-" + "a" * 16 + ".sqlite", ".lexical.sqlite.rebuild-" + "b" * 32 + ".tmp"):
        for suffix in ("", "-wal", "-shm", "-journal"):
            assert any(fnmatch.fnmatchcase("/data/host/.local/state/exomem/state/vault-key/" + name + suffix, p)
                       for p in excludes)
            assert not any(fnmatch.fnmatchcase("/data/vault/" + name + suffix, p) for p in excludes)
    assert not any(fnmatch.fnmatchcase("/data/host/.local/state/exomem/state/vault-key/.lexical.sqlite", p) for p in excludes)


async def test_idle_cell_running_build_defers_then_becomes_due_when_build_finishes():
    """Build activity must buy the idle cell build time, then release the ordinary due rule."""
    cluster = BuildCluster()
    obs = {CELL: _observation()}
    cluster.readiness[CELL] = json.dumps({"traffic": {"inflight": 0, "last_request_age_seconds": 900}}).encode()
    running = await reconcile._sync_catalogue_builds(cluster, [_row()], obs, RolloutRow(), IMAGE_B, NOW,
                                                    WINDOWS, CONFIG, allow_schedule=True)
    assert len(cluster.jobs) == 1
    assert await reconcile._due_cells(cluster, [_row()], running, IMAGE_B, {}, NOW, WINDOWS) == frozenset()
    job = cluster.builds[namespace_name(CELL)][0]
    cluster.builds[namespace_name(CELL)] = (replace(job, running=False, succeeded=True),)
    done = await reconcile._sync_catalogue_builds(cluster, [_row()], obs, RolloutRow(), IMAGE_B, NOW,
                                                 WINDOWS, CONFIG, allow_schedule=True)
    assert await reconcile._due_cells(cluster, [_row()], done, IMAGE_B, {}, NOW, WINDOWS) == frozenset({CELL})
    assert len(cluster.jobs) == 1


def test_forced_attempt_overrides_running_build_and_capacity_wait_never_defers():
    """A busy builder must not postpone a forced attempt, and an unscheduled pod buys no idle time."""
    idle = Idle(0, 900)
    assert not is_due(16, NOW, idle, WINDOWS, build_running=True)
    assert is_due(16, NOW.replace(minute=51), idle, WINDOWS, build_running=True)
    assert is_due(16, NOW, idle, WINDOWS, build_running=False)


async def test_second_candidate_cannot_occupy_same_nodes_build_slot():
    cluster = BuildCluster()
    other = "bbbbbbbbbbbbbbbb"
    rows = [_row(), _row(other)]
    await reconcile._sync_catalogue_builds(cluster, rows, {r.cell_id: _observation() for r in rows},
                                          RolloutRow(), IMAGE_B, NOW, WINDOWS, CONFIG, allow_schedule=True)
    assert len(cluster.jobs) == 1


@pytest.mark.parametrize("skip", ["tenant-locked", "unlocked-tenant-locked", "not-running", "flag-off", "identity-conflict", "malformed-custody"])
async def test_ineligible_cells_receive_no_build_job(skip):
    """Unlocking must not erase tenant-locked custody or let the builder consume its key channel."""
    cluster = BuildCluster()
    row = _row(custody_observation=json.dumps({"mode": "tenant-locked", "unlocked": skip.startswith("unlocked")})) \
        if "tenant-locked" in skip else _row()
    if skip == "malformed-custody":
        row = _row(custody_observation="invalid JSON")
    obs = _observation(pod_exists=False, pod_ready=False) if skip == "not-running" else _observation()
    if skip == "identity-conflict":
        obs = _observation(namespace_exists=True, namespace_cell_label="bbbbbbbbbbbbbbbb")
    await reconcile._sync_catalogue_builds(cluster, [row], {CELL: obs}, RolloutRow(), IMAGE_B, NOW, WINDOWS,
                                          _cluster_config() if skip == "flag-off" else CONFIG, allow_schedule=True)
    assert not cluster.jobs and not cluster.applied


@pytest.mark.parametrize("reason", ["timeout", "pause", "target-change", "window-close", "disable"])
async def test_obsolete_build_is_deleted_but_pods_stay_observed_until_they_end(reason):
    """Job deletion alone must never stand in for proof that its pods released the live claim."""
    cluster = BuildCluster()
    job = BuildJob(catalogue_build_name(IMAGE_B), IMAGE_B, "worker-one", NOW, running=True, pods_exist=True)
    cluster.builds[namespace_name(CELL)] = (job,)
    now = NOW + timedelta(seconds=1801) if reason == "timeout" else NOW
    if reason == "window-close":
        now = NOW.replace(hour=17)
    result = await reconcile._sync_catalogue_builds(
        cluster, [_row()], {CELL: _observation(catalogue_build_outcome="requested", catalogue_build_job=job.name)}, RolloutRow(paused=reason == "pause"),
        ("registry.example/cell@sha256:" + "c" * 64) if reason == "target-change" else IMAGE_B, now, WINDOWS,
        _cluster_config() if reason == "disable" else CONFIG, allow_schedule=True,
    )
    assert ("delete_catalogue_build_job", namespace_name(CELL), job.name) in cluster.events
    assert result[CELL].catalogue_builds == (job,)
    assert result[CELL].catalogue_build_outcome == "cancelled"


def test_bound_image_pull_counts_as_running_but_pending_node_capacity_does_not():
    """The scheduler's binding, not an empty Job status, decides whether idle attempts wait."""
    from kubernetes.client import (
        V1Container,
        V1Job,
        V1JobSpec,
        V1JobStatus,
        V1ObjectMeta,
        V1Pod,
        V1PodSpec,
        V1PodStatus,
        V1PodTemplateSpec,
    )
    pod_spec = V1PodSpec(containers=[V1Container(name="catalogue-build", image=IMAGE_B)])
    metadata = V1ObjectMeta(name=catalogue_build_name(IMAGE_B), namespace=namespace_name(CELL), creation_timestamp=NOW)
    job = V1Job(metadata=metadata, spec=V1JobSpec(template=V1PodTemplateSpec(spec=pod_spec)), status=V1JobStatus())
    pod = V1Pod(metadata=V1ObjectMeta(namespace=metadata.namespace, labels={"batch.kubernetes.io/job-name": metadata.name}),
                spec=pod_spec, status=V1PodStatus(phase="Pending"))
    client = ClusterClient.__new__(ClusterClient)
    client._batch = NS(list_job_for_all_namespaces=lambda **kwargs: NS(items=[job], metadata=None))
    client._core = NS(list_pod_for_all_namespaces=lambda **kwargs: NS(items=[pod], metadata=None))
    assert not client.observe_catalogue_builds()[metadata.namespace][0].running
    pod.spec.node_name = "worker-one"
    assert client.observe_catalogue_builds()[metadata.namespace][0].running
    pod.metadata.deletion_timestamp = NOW
    assert not client.observe_catalogue_builds()[metadata.namespace][0].running


async def test_forced_attempt_waits_for_build_pods_before_stop_without_resetting_hold_clock(cell_db):
    """The stop and backup must wait for deletion, while the original attempt deadlines keep running."""
    from .test_rollout_automation import (
        OWNER,
        TENANT,
        _annotations,
        _observe,
        _statefulset,
        _windowed_fleet,
    )

    fleet = await _windowed_fleet(cell_db, TENANT)
    cluster = BuildCluster()
    cluster.__dict__.update(fleet.cluster.__dict__)
    fleet.cluster = cluster
    try:
        cluster.readiness[TENANT] = json.dumps({"traffic": {"inflight": 0, "last_request_age_seconds": 900}}).encode()
        # Converge the enabled profile before changing the image, as the separate cellctl rollout does.
        for _ in range(3):
            await fleet.run(NOW, cluster_config=CONFIG)
            _observe(cluster, OWNER, TENANT, pod_node="worker-one")
        await fleet.admin.execute("UPDATE exomem_cloud_cells SET desired_image=$1 WHERE cell_id=$2", IMAGE_B, TENANT)
        job = BuildJob(catalogue_build_name(IMAGE_B), IMAGE_B, "worker-one", NOW, running=True, pods_exist=True)
        cluster.builds[namespace_name(TENANT)] = (job,)
        await fleet.run(NOW + timedelta(seconds=5), cluster_config=CONFIG)
        assert _statefulset(cluster, TENANT)["spec"]["replicas"] == 1
        assert "exomem.io/hold" not in _annotations(cluster, TENANT)
        force = NOW.replace(minute=51)
        await fleet.run(force, cluster_config=CONFIG)
        assert _annotations(cluster, TENANT)["exomem.io/hold"] == "upgrade"
        assert _annotations(cluster, TENANT)["exomem.io/hold-started-at"] == force.isoformat()
        assert _annotations(cluster, TENANT)["exomem.io/catalogue-build-outcome"] == "cancelled"
        assert _statefulset(cluster, TENANT)["spec"]["replicas"] == 1
        assert not cluster.jobs
        _observe(cluster, TENANT, pod_node="worker-one")
        await fleet.run(force + timedelta(seconds=5), cluster_config=CONFIG)
        assert _statefulset(cluster, TENANT)["spec"]["replicas"] == 1
        assert _annotations(cluster, TENANT)["exomem.io/hold-started-at"] == force.isoformat()
        cluster.builds.clear()  # The next API snapshot proves both Job and pods absent.
        await fleet.run(force + timedelta(seconds=10), cluster_config=CONFIG)
        assert _statefulset(cluster, TENANT)["spec"]["replicas"] == 0
        assert _annotations(cluster, TENANT)["exomem.io/hold-started-at"] == force.isoformat()
        _observe(cluster, TENANT)
        await fleet.run(force + timedelta(seconds=15), cluster_config=CONFIG)
        assert any(job["metadata"]["labels"].get(JOB_KIND_LABEL) == "backup" for job in cluster.jobs)
        assert fleet.config.backup_deadline == timedelta(minutes=15)
        assert fleet.config.upgrade_stall_bound == timedelta(minutes=30)
    finally:
        await fleet.close()


def test_build_job_uses_foreground_deletion_to_keep_pod_cleanup_observable():
    """Background deletion would hide the Job before its dependent pods release the claim."""
    calls = []
    client = ClusterClient.__new__(ClusterClient)
    client._batch = NS(delete_namespaced_job=lambda *args, **kwargs: calls.append((args, kwargs)))
    client.delete_catalogue_build_job(namespace_name(CELL), catalogue_build_name(IMAGE_B))
    assert calls == [((catalogue_build_name(IMAGE_B), namespace_name(CELL)), {"propagation_policy": "Foreground"})]


async def test_default_off_never_reads_build_inventory_or_changes_ordinary_upgrade_observations():
    """A disabled feature must not add an API failure that can delay an ordinary cell's upgrade."""
    class OrdinaryCluster(FakeClusterGateway):
        def observe_catalogue_builds(self):
            pytest.fail("the disabled controller added a build inventory dependency")

    cluster = OrdinaryCluster()
    other = "bbbbbbbbbbbbbbbb"
    observations = {CELL: _observation(), other: _observation(catalogue_build_outcome="succeeded")}
    result = await reconcile._sync_catalogue_builds(cluster, [_row(), _row(other)], observations, RolloutRow(), IMAGE_B,
                                                   NOW, WINDOWS, _cluster_config(), allow_schedule=True)
    assert result == observations
    assert not cluster.jobs and not cluster.applied


def test_build_census_cannot_lose_a_later_page_or_treat_an_operators_job_as_its_own():
    """A partial or foreign Job census could free an occupied node slot or delete an unrelated Job."""
    def job(name):
        return NS(metadata=NS(namespace=namespace_name(CELL), name=name, annotations={}, creation_timestamp=NOW),
                  spec=NS(template=NS(spec=NS(containers=[NS(image=IMAGE_B)]))),
                  status=NS(succeeded=False, failed=False, conditions=[]))

    first, second = catalogue_build_name(IMAGE_A), catalogue_build_name(IMAGE_B)
    pages = [NS(items=[job(first), job("operator-task")], metadata=NS(_continue="next-page")),
             NS(items=[job(second)], metadata=NS(_continue=None))]
    calls = []
    def list_jobs(**kwargs):
        calls.append(kwargs)
        return pages.pop(0)

    client = ClusterClient.__new__(ClusterClient)
    client._batch = NS(list_job_for_all_namespaces=list_jobs)
    client._core = NS(list_pod_for_all_namespaces=lambda **kwargs: NS(items=[], metadata=None))
    observed = client.observe_catalogue_builds()[namespace_name(CELL)]
    assert {item.name for item in observed} == {first, second}
    assert len(calls) == 2 and calls[1]["_continue"] == "next-page"
    assert all(call["limit"] == 500 for call in calls)


async def test_disabling_clears_current_job_only_after_orphan_pods_end_then_restores_the_old_path():
    """A stale historical outcome must not keep adding a build API dependency after cancellation ends."""
    cluster = BuildCluster()
    job = BuildJob(catalogue_build_name(IMAGE_B), IMAGE_B, "worker-one", NOW, pods_exist=True)
    cluster.builds[namespace_name(CELL)] = (job,)
    observations = {CELL: _observation(catalogue_build_job=job.name, catalogue_build_outcome="requested")}
    ending = await reconcile._sync_catalogue_builds(cluster, [_row()], observations, RolloutRow(), IMAGE_B,
                                                    NOW, WINDOWS, _cluster_config(), allow_schedule=True)
    assert ending[CELL].catalogue_build_job == job.name
    cluster.builds.clear()
    # A restarted controller reads the persisted request, without the prior process's cancellation result.
    ending[CELL] = replace(ending[CELL], catalogue_build_outcome="requested")
    ended = await reconcile._sync_catalogue_builds(cluster, [_row()], ending, RolloutRow(), IMAGE_B,
                                                   NOW, WINDOWS, _cluster_config(), allow_schedule=True)
    assert ended[CELL].catalogue_build_job is None
    assert ended[CELL].catalogue_build_outcome == "unavailable"
    cluster.observe_catalogue_builds = lambda: pytest.fail("completed history still gated ordinary upgrades")
    assert await reconcile._sync_catalogue_builds(cluster, [_row()], ended, RolloutRow(), IMAGE_B,
                                                  NOW, WINDOWS, _cluster_config(), allow_schedule=True) == ended
