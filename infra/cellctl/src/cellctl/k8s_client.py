"""The real Kubernetes surface cellctl touches: server-side apply, deletion,
and reading observations (D4). Never opens a connection to a cell itself.

This is the only module in cellctl that imports the `kubernetes` client
library. decide.py never sees it; reconcile.py is the only caller.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from kubernetes import client as k8s
from kubernetes.client.rest import ApiException
from kubernetes.dynamic import DynamicClient

from .manifests import (
    BACKUP_JOB_NAME,
    BACKUP_RETRY_AFTER_ANNOTATION,
    BACKUP_RETRY_MINUTES_ANNOTATION,
    HOLD_ANNOTATION,  # re-exported for reconcile.py convenience
    HOLD_STARTED_ANNOTATION,
    INIT_CONTAINER_NAME,
    JOB_KIND_LABEL,
    NAMESPACE_PREFIX,
    PRE_UPGRADE_SNAPSHOT_ANNOTATION,
    PREVIOUS_IMAGE_ANNOTATION,
    RENDER_DIGEST_ANNOTATION,
    RENDER_DIGEST_APPLIED_AT_ANNOTATION,
    RESTORE_JOB_NAME,
    RESTORED_SNAPSHOT_ANNOTATION,
    ROW_GENERATION_ANNOTATION,
    TARGET_APPLIED_ANNOTATION,
    hold_job_name,
)
from .state import ClusterObservation

FIELD_MANAGER = "cellctl"
CELL_LABEL = "exomem.io/cloud-cell"
REVISION_LABEL = "controller-revision-hash"


def _not_found(error: ApiException) -> bool:
    return error.status == 404


class ClusterClient:
    def __init__(self, api_client: k8s.ApiClient) -> None:
        self._api = api_client
        self._dynamic = DynamicClient(api_client)
        self._core = k8s.CoreV1Api(api_client)
        self._apps = k8s.AppsV1Api(api_client)
        self._batch = k8s.BatchV1Api(api_client)
        self._storage = k8s.StorageV1Api(api_client)
        self._admission = k8s.AdmissionregistrationV1Api(api_client)

    # -- apply / delete (D4 imperative shell) --

    def apply(self, manifest: dict) -> None:
        resource = self._dynamic.resources.get(
            api_version=manifest["apiVersion"], kind=manifest["kind"]
        )
        resource.server_side_apply(
            body=manifest,
            name=manifest["metadata"]["name"],
            namespace=manifest["metadata"].get("namespace"),
            field_manager=FIELD_MANAGER,
            content_type="application/apply-patch+yaml",
            force_conflicts=True,
        )

    def apply_all(self, manifests: list[dict]) -> None:
        for manifest in manifests:
            self.apply(manifest)

    def delete_namespace(self, name: str) -> None:
        try:
            self._core.delete_namespace(name)
        except ApiException as error:
            # D10: a namespace already Terminating returns 409 while it
            # tears down, and a re-delete after it is gone returns 404.
            # Both count as progress, never as an error worth raising.
            if error.status not in (404, 409):
                raise

    def run_job(self, manifest: dict) -> None:
        self.apply(manifest)

    def delete_job(self, namespace: str, name: str) -> None:
        # Background propagation removes the Job's pods too; a batch/v1 Job
        # deleted without a policy orphans them.
        try:
            self._batch.delete_namespaced_job(name, namespace, propagation_policy="Background")
        except ApiException as error:
            if not _not_found(error):
                raise

    # -- self-check (D4): confirm cellctl's own admission confinement exists --

    def admission_policy_present(self, policy_name: str, binding_name: str) -> bool:
        """True only when the policy exists and its binding names it with a
        Deny action and no matchResources: a binding downgraded to Audit
        confines nothing, and one narrowed by matchResources confines less
        than the policy says (D4)."""

        try:
            self._admission.read_validating_admission_policy(policy_name)
            binding = self._admission.read_validating_admission_policy_binding(binding_name)
        except ApiException as error:
            if _not_found(error):
                return False
            raise
        spec = binding.spec
        return (
            spec is not None
            and spec.policy_name == policy_name
            and "Deny" in (spec.validation_actions or [])
            and spec.match_resources is None
        )

    def list_cell_namespaces(self) -> dict[str, str]:
        """D4 orphan check: every namespace carrying the cell label, mapped
        to the cell id it names."""

        namespaces = self._core.list_namespace(label_selector=CELL_LABEL).items
        return {ns.metadata.name: (ns.metadata.labels or {}).get(CELL_LABEL, "") for ns in namespaces}

    # -- observation (D4: pods/events/namespaces get+list only) --

    def observe(self, cell_id: str, namespace: str) -> ClusterObservation:
        namespace_obj = self._get_namespace(namespace)
        if namespace_obj is None:
            return ClusterObservation()

        namespace_cell_label = namespace_obj.metadata.labels.get(CELL_LABEL) if namespace_obj.metadata.labels else None

        pvc = self._get_pvc(namespace, "cell-data")
        pvc_bound = bool(pvc and pvc.status and pvc.status.phase == "Bound")
        pvc_uid = pvc.metadata.uid if pvc and pvc.metadata else None
        pv_name = pvc.spec.volume_name if pvc and pvc.spec else None
        pvc_volume_id = None  # the underlying Hetzner volume id (PV spec.csi.volumeHandle), not the PV's own K8s name
        pv_claim_ref_uid = None
        pv_storage_class = None
        if pv_name:
            pv = self._get_pv(pv_name)
            if pv is not None:
                if pv.spec and pv.spec.claim_ref:
                    pv_claim_ref_uid = pv.spec.claim_ref.uid
                if pv.spec:
                    pv_storage_class = pv.spec.storage_class_name
                if pv.spec and pv.spec.csi:
                    pvc_volume_id = pv.spec.csi.volume_handle

        statefulset = self._get_statefulset(namespace, "cell")
        statefulset_exists = statefulset is not None
        statefulset_image = None
        statefulset_replicas = None
        update_revision = None
        hold_kind = hold_started_at = previous_image = pre_upgrade_snapshot = None
        target_applied_at = restored_snapshot = backup_retry_after = None
        backup_retry_minutes = render_digest = render_digest_applied_at = row_generation = None
        started_raw = None
        if statefulset is not None:
            containers = statefulset.spec.template.spec.containers
            statefulset_image = containers[0].image if containers else None
            statefulset_replicas = statefulset.spec.replicas
            # D4: status.updateRevision is trusted only once the controller
            # has observed the current spec; until then it names the revision
            # the pods are leaving, and a pod on it would count as Ready.
            status = statefulset.status
            generation = getattr(statefulset.metadata, "generation", None)
            if status is not None and getattr(status, "observed_generation", None) == generation:
                update_revision = status.update_revision
            annotations = statefulset.metadata.annotations or {}
            hold_kind = annotations.get(HOLD_ANNOTATION) or None
            started_raw = annotations.get(HOLD_STARTED_ANNOTATION)
            hold_started_at = _parse_timestamp(started_raw) if started_raw else None
            previous_image = annotations.get(PREVIOUS_IMAGE_ANNOTATION) or None
            pre_upgrade_snapshot = annotations.get(PRE_UPGRADE_SNAPSHOT_ANNOTATION) or None
            target_applied_at = _parse_timestamp(annotations.get(TARGET_APPLIED_ANNOTATION))
            restored_snapshot = annotations.get(RESTORED_SNAPSHOT_ANNOTATION) or None
            backup_retry_after = _parse_timestamp(annotations.get(BACKUP_RETRY_AFTER_ANNOTATION))
            retry_minutes_raw = annotations.get(BACKUP_RETRY_MINUTES_ANNOTATION)
            backup_retry_minutes = int(retry_minutes_raw) if retry_minutes_raw else None
            render_digest = annotations.get(RENDER_DIGEST_ANNOTATION) or None
            render_digest_applied_at = _parse_timestamp(annotations.get(RENDER_DIGEST_APPLIED_AT_ANNOTATION))
            row_generation_raw = annotations.get(ROW_GENERATION_ANNOTATION)
            row_generation = int(row_generation_raw) if row_generation_raw and row_generation_raw.isdigit() else None

        # Backup/restore Jobs run their own pods in this same namespace
        # (labelled with JOB_KIND_LABEL); those must never be mistaken for
        # the StatefulSet's own replica when deciding whether the cell pod
        # still holds the RWO volume, or a backup hold can never clear once
        # its Job's pod exists (found live in 3.10: the hold got stuck
        # forever because a completed backup Job's pod is not garbage
        # collected on its own).
        pods = self._list_cell_pods(namespace)
        pod_exists = len(pods) > 0
        live_pods = [pod for pod in pods if pod.metadata.deletion_timestamp is None]
        pod_terminating = len(live_pods) < len(pods)
        # D4: ready and observed_image come only from a pod whose revision
        # matches the StatefulSet's current updateRevision -- a pod from the
        # previous revision, mid rolling-update, never counts.
        current_revision_pods = [
            pod
            for pod in live_pods
            if update_revision is not None
            and (pod.metadata.labels or {}).get(REVISION_LABEL) == update_revision
        ]
        ready_pod = next((pod for pod in current_revision_pods if _pod_ready(pod)), None)
        pod_ready = ready_pod is not None
        ready_pod_image = None
        if ready_pod is not None and ready_pod.spec.containers:
            ready_pod_image = ready_pod.spec.containers[0].image
        pod_uses_volume = pod_exists  # single PVC per namespace, ReadWriteOnce
        pod_node = next((pod.spec.node_name for pod in live_pods if pod.spec.node_name), None)
        # D4: the init deadline is measured from the current pod's creation,
        # and applies only while that pod's cell-init has not completed.
        current_pod = next(iter(current_revision_pods), None) or next(iter(live_pods), None)
        pod_created_at = current_pod.metadata.creation_timestamp if current_pod is not None else None
        pod_init_completed = current_pod is not None and _init_completed(current_pod)
        init_rerun, init_started_at = _init_rerun(current_pod) if current_pod is not None else (False, None)

        # Only the current hold's Jobs count. Their names derive from the
        # hold-started annotation, which is the same string the Jobs were
        # named from when this StatefulSet was applied (manifests.py
        # hold_job_name). A finished Job from an earlier hold keeps a
        # different name until its TTL expires and is never read as this
        # hold's result. Outside a hold no decision reads Job state.
        backup_job = restore_job = None
        backup_snapshot_id = None
        if started_raw:
            backup_name = hold_job_name(BACKUP_JOB_NAME, started_raw)
            backup_job = self._get_job(namespace, backup_name)
            restore_job = self._get_job(namespace, hold_job_name(RESTORE_JOB_NAME, started_raw))
            if backup_job is not None and bool(backup_job.status.succeeded):
                backup_snapshot_id = self._job_pod_termination_message(namespace, backup_name)

        return ClusterObservation(
            namespace_exists=True,
            namespace_cell_label=namespace_cell_label,
            pvc_bound=pvc_bound,
            pvc_volume_id=pvc_volume_id,
            pvc_uid=pvc_uid,
            pv_claim_ref_uid=pv_claim_ref_uid,
            pv_storage_class=pv_storage_class,
            statefulset_exists=statefulset_exists,
            statefulset_image=statefulset_image,
            statefulset_replicas=statefulset_replicas,
            statefulset_hold_kind=hold_kind,
            statefulset_hold_started_at=hold_started_at,
            statefulset_previous_image=previous_image,
            statefulset_pre_upgrade_snapshot=pre_upgrade_snapshot,
            statefulset_target_applied_at=target_applied_at,
            statefulset_restored_snapshot=restored_snapshot,
            statefulset_backup_retry_after=backup_retry_after,
            statefulset_backup_retry_minutes=backup_retry_minutes,
            statefulset_render_digest=render_digest,
            statefulset_render_digest_applied_at=render_digest_applied_at,
            statefulset_row_generation=row_generation,
            pod_exists=pod_exists,
            pod_ready=pod_ready,
            pod_terminating=pod_terminating,
            pod_uses_volume=pod_uses_volume,
            pod_node=pod_node,
            pod_created_at=pod_created_at,
            pod_init_completed=pod_init_completed,
            init_rerun=init_rerun,
            init_started_at=init_started_at,
            ready_pod_image=ready_pod_image,
            **_job_observation(backup_job, prefix="backup_job", snapshot_id=backup_snapshot_id),
            **_job_observation(restore_job, prefix="restore_job"),
        )

    def _job_pod_termination_message(self, namespace: str, job_name: str) -> str | None:
        """D8 amendment: the backup Job has no ServiceAccount token, so it
        writes the real restic snapshot id it produced to its own
        termination message; this reads it back from the pod status rather
        than trusting a hardcoded "latest". Scoped to this Job's own pods, so
        a lingering pod from an earlier hold's backup cannot answer."""

        pods = self._core.list_namespaced_pod(
            namespace, label_selector=f"batch.kubernetes.io/job-name={job_name}"
        ).items
        for pod in pods:
            for status in pod.status.container_statuses or []:
                terminated = status.state.terminated if status.state else None
                if terminated and terminated.exit_code == 0 and terminated.message:
                    return terminated.message
        return None

    def _get_namespace(self, name: str):
        try:
            return self._core.read_namespace(name)
        except ApiException as error:
            if _not_found(error):
                return None
            raise

    def _get_pvc(self, namespace: str, name: str):
        try:
            return self._core.read_namespaced_persistent_volume_claim(name, namespace)
        except ApiException as error:
            if _not_found(error):
                return None
            raise

    def _get_pv(self, name: str):
        try:
            return self._core.read_persistent_volume(name)
        except ApiException as error:
            if _not_found(error):
                return None
            raise

    def _get_statefulset(self, namespace: str, name: str):
        try:
            return self._apps.read_namespaced_stateful_set(name, namespace)
        except ApiException as error:
            if _not_found(error):
                return None
            raise

    def _list_cell_pods(self, namespace: str) -> list:
        """Pods belonging to the cell's own StatefulSet, excluding any
        backup/restore Job pod running in the same namespace."""

        return self._core.list_namespaced_pod(
            namespace, label_selector=f"!{JOB_KIND_LABEL}"
        ).items

    def _get_job(self, namespace: str, name: str):
        try:
            return self._batch.read_namespaced_job(name, namespace)
        except ApiException as error:
            if _not_found(error):
                return None
            raise

    # -- absence checks (D10) --

    def namespace_absent(self, name: str) -> bool:
        return self._get_namespace(name) is None

    def pv_absent_for_namespace(self, namespace: str) -> bool:
        """True when no PersistentVolume's `claimRef` points at `namespace`
        (cluster-scoped list, using the existing persistentvolumes get/list
        right — D10 amendment)."""

        volumes = self._core.list_persistent_volume().items
        return not any(
            pv.spec and pv.spec.claim_ref and pv.spec.claim_ref.namespace == namespace
            for pv in volumes
        )

    # -- capacity (D9): Kubernetes-native, never a Hetzner server id --

    def capacity_inputs(
        self, *, csi_driver: str
    ) -> tuple[dict[str, int | None], dict[str, int], dict[str, int]]:
        """Returns (allocatable_by_node, attachments_used_by_node,
        non_cell_attachments_by_node), all keyed by Kubernetes node name."""

        # Read CSINodes as raw JSON: a node with no CSI driver at all (K3s
        # without Hetzner CSI) serves `spec.drivers: null`, which the client's
        # generated model refuses to deserialize.
        allocatable: dict[str, int | None] = {}
        csi_nodes = json.loads(self._storage.list_csi_node(_preload_content=False).data)
        for csi_node in csi_nodes.get("items") or []:
            allocatable_count = None
            for driver_info in (csi_node.get("spec") or {}).get("drivers") or []:
                if driver_info.get("name") == csi_driver:
                    allocatable_count = (driver_info.get("allocatable") or {}).get("count")
            allocatable[csi_node["metadata"]["name"]] = allocatable_count

        pv_namespace: dict[str, str] = {}
        for pv in self._core.list_persistent_volume().items:
            if pv.spec and pv.spec.claim_ref and pv.spec.claim_ref.namespace:
                pv_namespace[pv.metadata.name] = pv.spec.claim_ref.namespace

        attachments_used: dict[str, int] = {}
        non_cell_attachments: dict[str, int] = {}
        for attachment in self._storage.list_volume_attachment().items:
            if attachment.spec.attacher != csi_driver:
                continue
            if not (attachment.status and attachment.status.attached):
                continue
            node = attachment.spec.node_name
            attachments_used[node] = attachments_used.get(node, 0) + 1
            pv_name = (
                attachment.spec.source.persistent_volume_name
                if attachment.spec and attachment.spec.source
                else None
            )
            namespace = pv_namespace.get(pv_name) if pv_name else None
            if namespace is None or not namespace.startswith(NAMESPACE_PREFIX):
                non_cell_attachments[node] = non_cell_attachments.get(node, 0) + 1

        return allocatable, attachments_used, non_cell_attachments


def _pod_ready(pod) -> bool:
    if pod.status.phase != "Running":
        return False
    for condition in pod.status.conditions or []:
        if condition.type == "Ready":
            return condition.status == "True"
    return False


def _init_completed(pod) -> bool:
    """True when the pod's cell-init init container terminated with exit 0."""

    for status in pod.status.init_container_statuses or []:
        if status.name == INIT_CONTAINER_NAME:
            terminated = status.state.terminated if status.state else None
            return terminated is not None and terminated.exit_code == 0
    return False


def _init_rerun(pod) -> tuple[bool, datetime | None]:
    """N5/D4: whether cell-init runs again after it already completed in
    an earlier pod sandbox, and when this run started. A node reboot
    recreates the sandbox and re-runs init containers while the pod keeps
    its creation time; cell-init's last state is then that completed run.
    A first run or a crash loop (a failed last state) is not a re-run."""

    for status in pod.status.init_container_statuses or []:
        if status.name != INIT_CONTAINER_NAME:
            continue
        previous = status.last_state.terminated if status.last_state else None
        if previous is None or previous.exit_code != 0:
            return False, None
        state = status.state
        current = (state.running or state.terminated) if state else None
        return True, (current.started_at if current is not None else None)
    return False, None


def _parse_timestamp(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _job_observation(job, *, prefix: str, snapshot_id: str | None = None) -> dict:
    if job is None:
        return {f"{prefix}_running": False, f"{prefix}_succeeded": False, f"{prefix}_failed": False}
    status = job.status
    running = bool(status.active)
    succeeded = bool(status.succeeded)
    failed = bool(status.failed)
    result = {f"{prefix}_running": running, f"{prefix}_succeeded": succeeded, f"{prefix}_failed": failed}
    if prefix == "backup_job":
        # D8 amendment: this is the real restic snapshot id, read from the
        # Job pod's termination message by the caller (observe()), never a
        # hardcoded placeholder.
        result["backup_job_snapshot_id"] = snapshot_id if succeeded else None
        result["backup_job_started_at"] = job.status.start_time
    return result


def now_utc() -> datetime:
    return datetime.now(UTC)
