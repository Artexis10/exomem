"""ClusterClient.observe() against stubbed Kubernetes APIs: which backup and
restore Jobs count as the current hold's."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

from kubernetes.client.rest import ApiException

from cellctl.k8s_client import CELL_LABEL, ClusterClient
from cellctl.manifests import (
    HOLD_ANNOTATION,
    HOLD_STARTED_ANNOTATION,
    JOB_KIND_LABEL,
    hold_job_name,
)

CELL_ID = "aaaaaaaaaaaaaaaa"
NAMESPACE = f"exo-cell-{CELL_ID}"
EARLIER_HOLD = "2026-01-01T00:00:00+00:00"
CURRENT_HOLD = "2026-01-01T00:04:00+00:00"


def _matches(labels: dict[str, str], selector: str) -> bool:
    for term in selector.split(","):
        if term.startswith("!"):
            if term[1:] in labels:
                return False
        else:
            key, _, value = term.partition("=")
            if labels.get(key) != value:
                return False
    return True


def _job(*, succeeded: bool = False, failed: bool = False, active: bool = False):
    return NS(status=NS(succeeded=1 if succeeded else None, failed=1 if failed else None,
                        active=1 if active else None, start_time=None))


def _job_pod(job_name: str, *, snapshot_id: str):
    terminated = NS(exit_code=0, message=snapshot_id)
    return NS(
        metadata=NS(labels={JOB_KIND_LABEL: "backup", "batch.kubernetes.io/job-name": job_name},
                    deletion_timestamp=None),
        spec=NS(node_name="node-1"),
        status=NS(phase="Succeeded", conditions=[], init_container_statuses=None,
                  container_statuses=[NS(state=NS(terminated=terminated))]),
    )


class _Core:
    def __init__(self, pods: list) -> None:
        self._pods = pods

    def read_namespace(self, name):
        return NS(metadata=NS(labels={CELL_LABEL: CELL_ID}))

    def read_namespaced_persistent_volume_claim(self, name, namespace):
        raise ApiException(status=404)

    def list_namespaced_pod(self, namespace, label_selector):
        return NS(items=[pod for pod in self._pods if _matches(pod.metadata.labels, label_selector)])


class _Apps:
    def __init__(
        self, hold_started_at: str | None, *, annotations: dict | None = None, generation: int = 1, observed_generation: int = 1
    ) -> None:
        annotations = dict(annotations or {})
        if hold_started_at:
            annotations.update({HOLD_ANNOTATION: "upgrade", HOLD_STARTED_ANNOTATION: hold_started_at})
        self._statefulset = NS(
            metadata=NS(annotations=annotations, generation=generation),
            spec=NS(replicas=0, template=NS(spec=NS(containers=[NS(image="registry.example/cell@sha256:" + "a" * 64)]))),
            status=NS(update_revision="cell-rev-1", observed_generation=observed_generation),
        )

    def read_namespaced_stateful_set(self, name, namespace):
        return self._statefulset


class _Batch:
    def __init__(self, jobs: dict) -> None:
        self._jobs = jobs

    def read_namespaced_job(self, name, namespace):
        if name not in self._jobs:
            raise ApiException(status=404)
        return self._jobs[name]


def _client(*, hold_started_at: str | None, jobs: dict, pods: list) -> ClusterClient:
    client = ClusterClient.__new__(ClusterClient)
    client._core = _Core(pods)
    client._apps = _Apps(hold_started_at)
    client._batch = _Batch(jobs)
    return client


def test_a_finished_job_from_an_earlier_hold_is_not_this_holds_result() -> None:
    # The live 3.10 regression: the previous hold's succeeded backup and
    # restore Jobs were still inside their TTL and got read back as the new
    # hold's outcome, so an object-storage outage never showed BACKUP_FAILED.
    earlier_backup = hold_job_name("cell-backup", EARLIER_HOLD)
    jobs = {
        earlier_backup: _job(succeeded=True),
        hold_job_name("cell-restore", EARLIER_HOLD): _job(succeeded=True),
    }
    pods = [_job_pod(earlier_backup, snapshot_id="stale-snapshot")]
    observation = _client(hold_started_at=CURRENT_HOLD, jobs=jobs, pods=pods).observe(CELL_ID, NAMESPACE)
    assert not observation.backup_job_succeeded
    assert observation.backup_job_snapshot_id is None
    assert not observation.restore_job_succeeded


def test_the_current_holds_job_and_only_its_own_pod_answer() -> None:
    earlier_backup = hold_job_name("cell-backup", EARLIER_HOLD)
    current_backup = hold_job_name("cell-backup", CURRENT_HOLD)
    jobs = {earlier_backup: _job(succeeded=True), current_backup: _job(succeeded=True)}
    # The earlier hold's pod is listed first, so an unscoped lookup would
    # return its snapshot id.
    pods = [
        _job_pod(earlier_backup, snapshot_id="stale-snapshot"),
        _job_pod(current_backup, snapshot_id="fresh-snapshot"),
    ]
    observation = _client(hold_started_at=CURRENT_HOLD, jobs=jobs, pods=pods).observe(CELL_ID, NAMESPACE)
    assert observation.backup_job_succeeded
    assert observation.backup_job_snapshot_id == "fresh-snapshot"


def test_the_current_holds_failed_backup_is_observed() -> None:
    jobs = {
        hold_job_name("cell-backup", EARLIER_HOLD): _job(succeeded=True),
        hold_job_name("cell-backup", CURRENT_HOLD): _job(failed=True),
    }
    observation = _client(hold_started_at=CURRENT_HOLD, jobs=jobs, pods=[]).observe(CELL_ID, NAMESPACE)
    assert observation.backup_job_failed
    assert not observation.backup_job_succeeded


def test_no_job_state_is_read_outside_a_hold() -> None:
    jobs = {hold_job_name("cell-backup", EARLIER_HOLD): _job(succeeded=True)}
    observation = _client(hold_started_at=None, jobs=jobs, pods=[]).observe(CELL_ID, NAMESPACE)
    assert not (observation.backup_job_succeeded or observation.backup_job_failed or observation.backup_job_running)
    assert not (observation.restore_job_succeeded or observation.restore_job_failed or observation.restore_job_running)


def _cell_pod(
    *, init_state, last_state=None, created: str = "2026-01-01T00:00:00+00:00", revision: str = "cell-rev-1"
):
    return NS(
        metadata=NS(labels={"controller-revision-hash": revision}, deletion_timestamp=None, creation_timestamp=created),
        spec=NS(node_name="node-1", containers=[NS(image="registry.example/cell@sha256:" + "a" * 64)]),
        status=NS(
            phase="Running",
            conditions=[NS(type="Ready", status="False")],
            init_container_statuses=[NS(name="cell-init", state=init_state, last_state=last_state)],
            container_statuses=[],
        ),
    )


def test_observe_reports_whether_the_current_pods_cell_init_completed() -> None:
    done = NS(terminated=NS(exit_code=0, message=None), running=None, waiting=None)
    crashed = NS(terminated=NS(exit_code=1, message=None), running=None, waiting=None)
    running = NS(terminated=None, running=NS(started_at=None), waiting=None)

    completed = _client(hold_started_at=None, jobs={}, pods=[_cell_pod(init_state=done)]).observe(CELL_ID, NAMESPACE)
    assert completed.pod_init_completed is True
    assert completed.pod_created_at == "2026-01-01T00:00:00+00:00"
    for state in (crashed, running):
        pending = _client(hold_started_at=None, jobs={}, pods=[_cell_pod(init_state=state)]).observe(CELL_ID, NAMESPACE)
        assert pending.pod_init_completed is False


def test_observe_reports_a_cell_init_re_run_in_a_recreated_sandbox_and_when_it_started() -> None:
    # N5: after a node reboot the pod keeps its creation time, cell-init's
    # previous run (exit 0) is its last state, and it runs again.
    rerun_start = "2026-01-04T00:00:00+00:00"
    completed_before = NS(terminated=NS(exit_code=0, message=None, started_at="2026-01-01T00:00:05+00:00"),
                          running=None, waiting=None)
    crashed_before = NS(terminated=NS(exit_code=1, message=None, started_at="2026-01-01T00:00:05+00:00"),
                        running=None, waiting=None)
    running = NS(terminated=None, running=NS(started_at=rerun_start), waiting=None)
    waiting = NS(terminated=None, running=None, waiting=NS(reason="PodInitializing"))

    def observed(state, last_state):
        pod = _cell_pod(init_state=state, last_state=last_state)
        return _client(hold_started_at=None, jobs={}, pods=[pod]).observe(CELL_ID, NAMESPACE)

    rerun = observed(running, completed_before)
    assert (rerun.init_rerun, rerun.init_started_at) == (True, rerun_start)
    assert (observed(waiting, completed_before).init_rerun, observed(waiting, completed_before).init_started_at) == (True, None)
    # A first run and a crash loop are measured from the pod's creation.
    assert observed(running, None).init_rerun is False
    assert observed(waiting, crashed_before).init_rerun is False


class _StorageWithNullDrivers:
    """CSINode as K3s serves it without any CSI driver: `spec.drivers` is null.

    The preloaded path uses the real client's deserializer, which rejects a
    null `drivers`, exactly as the live K3s run did."""

    def __init__(self, items: list[dict]) -> None:
        self._body = json.dumps({"apiVersion": "storage.k8s.io/v1", "kind": "CSINodeList", "items": items}).encode()

    def list_csi_node(self, _preload_content: bool = True):
        if _preload_content:
            from kubernetes import client as k8s

            return k8s.ApiClient().deserialize(NS(data=self._body), "V1CSINodeList")
        return NS(data=self._body)

    def list_volume_attachment(self):
        return NS(items=[])


class _CoreWithoutVolumes:
    def list_persistent_volume(self):
        return NS(items=[])


def test_capacity_inputs_read_a_csinode_whose_drivers_are_null() -> None:
    client = ClusterClient.__new__(ClusterClient)
    client._core = _CoreWithoutVolumes()
    client._storage = _StorageWithNullDrivers(
        [
            {"metadata": {"name": "k3s-node"}, "spec": {"drivers": None}},
            {
                "metadata": {"name": "hetzner-node"},
                "spec": {"drivers": [{"name": "csi.hetzner.cloud", "nodeID": "1", "allocatable": {"count": 16}}]},
            },
        ]
    )

    allocatable, attachments_used, non_cell = client.capacity_inputs(csi_driver="csi.hetzner.cloud")

    assert allocatable == {"k3s-node": None, "hetzner-node": 16}
    assert attachments_used == {} and non_cell == {}


def _ready_cell_pod(revision: str = "cell-rev-1"):
    pod = _cell_pod(init_state=NS(terminated=NS(exit_code=0, message=None), running=None, waiting=None), revision=revision)
    pod.status.conditions = [NS(type="Ready", status="True")]
    return pod


def test_update_revision_is_trusted_only_once_the_controller_observed_the_generation() -> None:
    # D4: status.updateRevision is stale until status.observedGeneration
    # catches up with metadata.generation, so a Ready pod carrying that stale
    # revision must not count as Ready on the update revision.
    client = _client(hold_started_at=None, jobs={}, pods=[_ready_cell_pod()])
    client._apps = _Apps(None, generation=3, observed_generation=2)
    assert client.observe(CELL_ID, NAMESPACE).pod_ready is False

    client._apps = _Apps(None, generation=3, observed_generation=3)
    assert client.observe(CELL_ID, NAMESPACE).pod_ready is True


def test_observe_reads_the_row_generation_annotation() -> None:
    from cellctl.manifests import ROW_GENERATION_ANNOTATION

    client = _client(hold_started_at=None, jobs={}, pods=[])
    client._apps = _Apps(None, annotations={ROW_GENERATION_ANNOTATION: "4"})
    assert client.observe(CELL_ID, NAMESPACE).statefulset_row_generation == 4


def test_observe_reads_the_render_digest_applied_at_annotation() -> None:
    from datetime import UTC, datetime

    from cellctl.manifests import RENDER_DIGEST_APPLIED_AT_ANNOTATION

    client = _client(hold_started_at=None, jobs={}, pods=[])
    client._apps = _Apps(None, annotations={RENDER_DIGEST_APPLIED_AT_ANNOTATION: "2026-01-01T00:05:00+00:00"})
    observed = client.observe(CELL_ID, NAMESPACE).statefulset_render_digest_applied_at
    assert observed == datetime(2026, 1, 1, 0, 5, tzinfo=UTC)


def test_delete_job_propagates_to_its_pods_and_tolerates_an_absent_job() -> None:
    calls: list[tuple] = []

    class _DeletingBatch:
        def delete_namespaced_job(self, name, namespace, propagation_policy=None):
            calls.append((name, namespace, propagation_policy))
            if name == "gone":
                raise ApiException(status=404)

    client = ClusterClient.__new__(ClusterClient)
    client._batch = _DeletingBatch()
    client.delete_job(NAMESPACE, "cell-backup-0123456789")
    client.delete_job(NAMESPACE, "gone")
    assert calls[0] == ("cell-backup-0123456789", NAMESPACE, "Background")


def test_list_cell_namespaces_selects_by_the_cell_label() -> None:
    selectors: list[str] = []

    class _ListingCore:
        def list_namespace(self, label_selector):
            selectors.append(label_selector)
            return NS(items=[NS(metadata=NS(name=f"exo-cell-{CELL_ID}", labels={CELL_LABEL: CELL_ID}))])

    client = ClusterClient.__new__(ClusterClient)
    client._core = _ListingCore()
    assert client.list_cell_namespaces() == {f"exo-cell-{CELL_ID}": CELL_ID}
    assert selectors == [CELL_LABEL]


class _Admission:
    def __init__(self, *, policy_name: str, actions: list[str] | None, match_resources=None) -> None:
        self._binding = NS(spec=NS(policy_name=policy_name, validation_actions=actions, match_resources=match_resources))

    def read_validating_admission_policy(self, name):
        return NS(metadata=NS(name=name))

    def read_validating_admission_policy_binding(self, name):
        return self._binding


def test_the_self_check_requires_a_deny_binding_that_names_the_policy() -> None:
    # D4: a binding downgraded to Audit, or bound to another policy,
    # confines nothing.
    def present(**binding) -> bool:
        client = ClusterClient.__new__(ClusterClient)
        client._admission = _Admission(**binding)
        return client.admission_policy_present("exomem-cellctl-scope", "exomem-cellctl-scope")

    assert present(policy_name="exomem-cellctl-scope", actions=["Deny"]) is True
    assert present(policy_name="exomem-cellctl-scope", actions=["Deny", "Audit"]) is True
    assert present(policy_name="exomem-cellctl-scope", actions=["Audit"]) is False
    assert present(policy_name="exomem-cellctl-scope", actions=None) is False
    assert present(policy_name="something-else", actions=["Deny"]) is False


def test_the_self_check_rejects_a_binding_narrowed_by_match_resources() -> None:
    # NEW-4: matchResources on the binding can exclude namespaces or
    # objects from the policy, so a narrowed binding confines less than the
    # policy says.
    client = ClusterClient.__new__(ClusterClient)
    narrowed = NS(namespace_selector=NS(match_labels={"exomem.io/confined": "true"}))
    client._admission = _Admission(policy_name="exomem-cellctl-scope", actions=["Deny"], match_resources=narrowed)
    assert client.admission_policy_present("exomem-cellctl-scope", "exomem-cellctl-scope") is False
