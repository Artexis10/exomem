"""The real Kubernetes surface cellctl touches: server-side apply, deletion,
and reading observations (D4). Never opens a connection to a cell itself.

This is the only module in cellctl that imports the `kubernetes` client
library. decide.py never sees it; reconcile.py is the only caller.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from datetime import UTC, datetime

import httpx
from kubernetes import client as k8s
from kubernetes.client.rest import ApiException
from kubernetes.dynamic import DynamicClient

from .backup_source import FAILURE_CODES as BACKUP_FAILURE_CODES
from .capacity import (
    AttachmentReservation,
    CapacityObservation,
    LocalCapacityObservation,
    LogicalVolumeRecord,
    NodeObservation,
    PodReservation,
    SharedWorkerPolicy,
    effective_pod_requests,
    parse_budget_frame,
    quantity,
)
from .manifests import (
    BACKUP_FAMILY_ANNOTATION,
    BACKUP_JOB_NAME,
    BACKUP_OUTCOME_ANNOTATION,
    BACKUP_RETRY_AFTER_ANNOTATION,
    BACKUP_RETRY_MINUTES_ANNOTATION,
    CLONE_CLAIM_NAME,
    GATEWAY_NAMESPACE,
    GROW_STORAGE_ANNOTATION,
    HOLD_ANNOTATION,  # re-exported for reconcile.py convenience
    HOLD_STARTED_ANNOTATION,
    INIT_CONTAINER_NAME,
    JOB_KIND_LABEL,
    NAMESPACE_PREFIX,
    PRE_UPGRADE_SNAPSHOT_ANNOTATION,
    PREVIOUS_IMAGE_ANNOTATION,
    RELOCATION_ANNOTATION,
    RENDER_DIGEST_ANNOTATION,
    RENDER_DIGEST_APPLIED_AT_ANNOTATION,
    RESTORE_JOB_NAME,
    RESTORED_SNAPSHOT_ANNOTATION,
    ROW_GENERATION_ANNOTATION,
    SNAPSHOT_NAME,
    TARGET_APPLIED_ANNOTATION,
    hold_job_name,
    namespace_name,
    snapshot_backup_name,
)
from .media_edge import ACTIVE_SET_NAME, read_generation
from .state import INIT_REFUSAL_CODES, SNAPSHOT_BACKUP, ClusterObservation, MediaCensus
from .storage.topolvm import FamilyRequest, LocalVolumeState, LogicalVolume
from .storage_config import DEFAULT_STORAGE, LocalStorage, StorageConfig

FIELD_MANAGER = "cellctl"
SNAPSHOT_GROUP = "snapshot.storage.k8s.io"
# The pinned snapshotter uses these fixed annotations before DeleteSnapshot.
DELETION_SECRET_NAME_ANNOTATION = "snapshot.storage.kubernetes.io/deletion-secret-name"
DELETION_SECRET_NAMESPACE_ANNOTATION = "snapshot.storage.kubernetes.io/deletion-secret-namespace"
CELL_LABEL = "exomem.io/cloud-cell"
REVISION_LABEL = "controller-revision-hash"
logger = logging.getLogger(__name__)


# D4: every Kubernetes call is bounded (connect, read seconds). A half-open
# connection to the API server surfaces as an error the pass handles, well
# inside the 120 s liveness bound, instead of blocking the loop until
# liveness restarts cellctl and discards its in-memory refusal parks.
API_REQUEST_TIMEOUT = (5, 30)

# Tasks 5.5 and 5.6: a host alert reads one small /metrics page from each node's
# pod (about 5 KB from node-exporter's mdadm collector, tens of KB from TopoLVM).
# Every pod is read at once, each inside METRICS_TIMEOUT seconds of wall clock for
# its connect, response head and body together, so one call takes that long at
# most. A socket timeout would restart with every byte a pod drips. A pod that
# misses the bound, or sends more than METRICS_MAX_BYTES, is an unreadable node,
# never a clean one.
METRICS_TIMEOUT = 3.0
METRICS_MAX_BYTES = 256 * 1024


async def _read_metrics(targets: dict[str, str], port: int, scraped: dict[str, str]) -> None:
    """Fills `scraped` with the page of each node in `targets` (node -> pod IP)
    that answered in time; the others are left out."""

    async def read(client: httpx.AsyncClient, node: str, address: str) -> None:
        try:
            async with asyncio.timeout(METRICS_TIMEOUT):
                async with client.stream("GET", f"http://{address}:{port}/metrics") as response:
                    response.raise_for_status()
                    body = b""
                    # Raw bytes, so the cap counts what the pod sent: a decoded
                    # stream would inflate a small compressed body before the check.
                    async for chunk in response.aiter_raw():
                        body += chunk
                        if len(body) > METRICS_MAX_BYTES:
                            raise ValueError("metrics page too large")
            scraped[node] = body.decode("utf-8")
        except (httpx.HTTPError, httpx.InvalidURL, ValueError, TimeoutError):
            pass

    # timeout=None: the asyncio.timeout above is the one bound, covering the phases a socket timeout misses.
    # Accept-Encoding identity: the exporters then send plain text, and a page that is
    # compressed anyway fails to decode or parse and reads unreadable.
    async with httpx.AsyncClient(
        trust_env=False, timeout=None, headers={"Accept-Encoding": "identity"}
    ) as client:
        await asyncio.gather(*(read(client, node, address) for node, address in targets.items()))


def _bound_requests(api_client: k8s.ApiClient) -> None:
    """Gives every request through `api_client` API_REQUEST_TIMEOUT unless
    the caller passed its own. Typed and dynamic clients both go through
    ApiClient.request."""

    unbounded = api_client.request

    def request(*args, **kwargs):
        if kwargs.get("_request_timeout") is None:
            kwargs["_request_timeout"] = API_REQUEST_TIMEOUT
        return unbounded(*args, **kwargs)

    api_client.request = request


def _not_found(error: ApiException) -> bool:
    return error.status == 404


class ClusterClient:
    _storage_config: StorageConfig = DEFAULT_STORAGE

    def __init__(self, api_client: k8s.ApiClient, *, storage_config: StorageConfig = DEFAULT_STORAGE) -> None:
        _bound_requests(api_client)
        self._storage_config = storage_config
        self._api = api_client
        self._dynamic = DynamicClient(api_client)
        self._core = k8s.CoreV1Api(api_client)
        self._apps = k8s.AppsV1Api(api_client)
        self._batch = k8s.BatchV1Api(api_client)
        self._storage = k8s.StorageV1Api(api_client)
        self._custom = k8s.CustomObjectsApi(api_client)
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

    def delete_secret(self, namespace: str, name: str, *, cell_id: str) -> bool:
        """Mutate a Secret only after the namespace API proves its expected cell ownership."""
        if namespace != namespace_name(cell_id):
            return False
        try:
            owner = self._core.read_namespace(namespace)
        except ApiException as error:
            if error.status == 404:
                return True
            raise
        metadata = getattr(owner, "metadata", None)
        if (metadata is None or getattr(metadata, "name", None) != namespace
                or (getattr(metadata, "labels", None) or {}).get(CELL_LABEL) != cell_id):
            return False
        try:
            self._core.delete_namespaced_secret(name, namespace)
        except ApiException as error:
            if error.status != 404:
                raise
        return True

    def delete_family_secret(self, local: LocalStorage, *, cell_id: str, request: FamilyRequest) -> bool:
        """Delete only this request's immutable family capability; an API failure remains pending."""
        name = snapshot_backup_name(SNAPSHOT_NAME, cell_id, None, request)
        try:
            self._core.delete_namespaced_secret(name, local.reservation_namespace)
        except ApiException as error:
            return error.status == 404
        return True

    def delete_cell_secrets(self, namespace: str, *, cell_id: str) -> bool:
        """Retire the cell's children without waiting for namespace or provider cleanup."""
        return self.delete_secret(namespace, "cell-credentials", cell_id=cell_id)

    def delete_snapshot_backup(self, namespace: str, snapshot: str, claim: str) -> None:
        """D3: an hourly hold's clone claim and snapshot. Already gone is done."""

        try:
            self._core.delete_namespaced_persistent_volume_claim(claim, namespace)
        except ApiException as error:
            if not _not_found(error):
                raise
        try:
            self._custom.delete_namespaced_custom_object(SNAPSHOT_GROUP, "v1", namespace, "volumesnapshots", snapshot)
        except ApiException as error:
            if not _not_found(error):
                raise

    def retain_volume(self, name: str) -> None:
        """D4: the only write cellctl makes to a PersistentVolume, which
        admission confines to this field, on PVs claimed from a cell namespace."""

        self._core.patch_persistent_volume(name, {"spec": {"persistentVolumeReclaimPolicy": "Retain"}})

    def delete_claim(self, namespace: str) -> None:
        """D4: admission allows this only while the bound PV is Retain."""

        try:
            self._core.delete_namespaced_persistent_volume_claim("cell-data", namespace)
        except ApiException as error:
            if not _not_found(error):
                raise

    def read_secret_value(self, namespace: str, name: str, key: str) -> str:
        """Task 2.8: one key of one platform Secret, the alert-delivery URL. The
        chart grants `get` on that Secret alone."""

        data = self._core.read_namespaced_secret(name, namespace).data or {}
        if key not in data:
            raise KeyError(f"secret {namespace}/{name} has no key {key}")
        return base64.b64decode(data[key]).decode("utf-8").strip()

    def read_media_edge_active_set(self) -> dict[str, str] | None:
        """add-cloud-media-store D6: the edge's active-set ConfigMap data, None before the first publish.
        The chart grants `get` on that ConfigMap alone."""

        try:
            return dict(self._core.read_namespaced_config_map(ACTIVE_SET_NAME, GATEWAY_NAMESPACE).data or {})
        except ApiException as error:
            if _not_found(error):
                return None
            raise

    def apply_media_edge_active_set(self, manifest: dict) -> None:
        """Publish the active set. Admission confines cellctl to this one ConfigMap."""

        metadata = manifest["metadata"]
        if manifest["kind"] != "ConfigMap" or (metadata["namespace"], metadata["name"]) != (GATEWAY_NAMESPACE, ACTIVE_SET_NAME):
            raise ValueError("cellctl publishes only the media edge active set")
        self.apply(manifest)

    def media_edge_generation(self, url: str) -> int:
        return read_generation(url)

    def scrape_pod_metrics(self, namespace: str, selector: str, port: int) -> dict[str, str]:
        """Tasks 5.5 and 5.6: the `/metrics` text of each node's pod that
        matches `selector`, read from the pod's own IP, keyed by the node the
        pod runs on. A pod that is not running, is being deleted, or does not
        answer leaves its node out: an unreadable node, never a clean one. A
        failed pod listing raises. The chart grants `list` on pods."""

        targets = {
            pod.spec.node_name: pod.status.pod_ip
            for pod in self._core.list_namespaced_pod(namespace, label_selector=selector).items
            if pod.status.phase == "Running" and not pod.metadata.deletion_timestamp
            and pod.spec.node_name and pod.status.pod_ip
        }
        scraped: dict[str, str] = {}
        asyncio.run(_read_metrics(targets, port, scraped))
        return scraped

    def observe_import_jobs(self, namespace: str, import_id: str):
        import json

        from .imports import PHASES, ImportJob, job_name

        result = {}
        for phase in PHASES:
            name = job_name(import_id, phase)
            job = self._get_job(namespace, name)
            pods = self._core.list_namespaced_pod(
                namespace, label_selector=f"batch.kubernetes.io/job-name={name}"
            ).items
            # Terminating pods still hold the volume until Kubernetes removes them.
            active = any(p.metadata.deletion_timestamp is not None or
                         p.status.phase not in ("Succeeded", "Failed") for p in pods)
            succeeded = job is not None and bool(job.status.succeeded)
            failed = job is not None and bool(job.status.failed)
            report = {}
            if succeeded or failed:
                try:
                    report = json.loads(self._job_pod_termination_message(namespace, name, succeeded=succeeded) or "{}")
                except ValueError:
                    pass
            result[phase] = ImportJob(job is not None, active, succeeded, failed, report, bool(pods))
        return result

    def observe_export_job(self, namespace: str, export_id: str, *, verify: bool = False):
        """C6: the export's one Job, its pods and its termination report."""
        from .exports import ExportJob, job_name

        name = job_name(export_id, verify=verify)
        job = self._get_job(namespace, name)
        pods = self._core.list_namespaced_pod(namespace, label_selector=f"batch.kubernetes.io/job-name={name}").items
        # Terminating pods still hold the volume until Kubernetes removes them.
        active = any(p.metadata.deletion_timestamp is not None or
                     p.status.phase not in ("Succeeded", "Failed") for p in pods)
        succeeded = job is not None and bool(job.status.succeeded)
        failed = job is not None and bool(job.status.failed)
        report = {}
        if succeeded or failed:
            try:
                parsed = json.loads(self._job_pod_termination_message(namespace, name, succeeded=succeeded) or "{}")
            except ValueError:
                parsed = {}
            report = parsed if isinstance(parsed, dict) else {}
        if failed:
            detail = report.get("diagnostic")
            safe = {}
            if isinstance(detail, dict):
                # The closed diagnostic wire fields carry code identifiers, never free-form error text.
                for key in ("type", "function", "code", "limit", "cause"):
                    value = detail.get(key)
                    if isinstance(value, str) and len(value) <= 80 and value.isascii() and value.isidentifier():
                        safe[key] = value
                errno = detail.get("errno")
                if type(errno) is int and errno > 0:
                    safe["errno"] = errno
            # The controller removes terminal Jobs; retain the private-safe reason before it does.
            logger.warning("cellctl: export %s failed: %s", export_id, json.dumps(safe, sort_keys=True))
        finished_at = _parse_timestamp(job.status.completion_time) if succeeded else None
        predecessor = None
        if verify:
            # The packing Job's Pod must release its volume before verification can finish the hold.
            predecessor_name = job_name(export_id)
            predecessor = self._get_job(namespace, predecessor_name)
            pods += self._core.list_namespaced_pod(namespace,
                label_selector=f"batch.kubernetes.io/job-name={predecessor_name}").items
        return ExportJob(job is not None or predecessor is not None, active, succeeded, failed, report,
                         bool(pods), finished_at)

    def delete_job(self, namespace: str, name: str) -> None:
        # Background propagation removes the Job's pods too; a batch/v1 Job
        # deleted without a policy orphans them.
        try:
            self._batch.delete_namespaced_job(name, namespace, propagation_policy="Background")
        except ApiException as error:
            if not _not_found(error):
                raise

    # -- self-check (D4): confirm cellctl's own admission confinement exists --

    def admission_policy_present(
        self, policy_name: str, binding_name: str, *, param_name: str | None = None, param_selects_all: bool = False
    ) -> bool:
        """True only when the policy fails closed and validates something,
        and its binding names it with a Deny action and no matchResources: a
        policy set to Ignore or stripped of its validations, a binding
        downgraded to Audit, or one narrowed by matchResources all confine
        less than the policy says (D4). `param_selects_all` is the
        retained-delete guard's binding, which must check every PV."""

        try:
            policy = self._admission.read_validating_admission_policy(policy_name)
            binding = self._admission.read_validating_admission_policy_binding(binding_name)
        except ApiException as error:
            if _not_found(error):
                return False
            raise
        policy_spec = policy.spec
        if policy_spec is None or policy_spec.failure_policy != "Fail" or not policy_spec.validations:
            return False
        spec = binding.spec
        if param_name is not None:
            # The isolation binding: its param is `param_name`, looked up in
            # the request's own namespace (no namespace, no selector), and a
            # missing one denies.
            param_ref = getattr(spec, "param_ref", None) if spec is not None else None
            if (
                param_ref is None
                or param_ref.name != param_name
                or param_ref.namespace
                or param_ref.selector is not None
                or param_ref.parameter_not_found_action != "Deny"
            ):
                return False
        if param_selects_all:
            param_ref = getattr(spec, "param_ref", None) if spec is not None else None
            selector = getattr(param_ref, "selector", None)
            if (
                selector is None
                or param_ref.name
                or param_ref.namespace
                or selector.match_labels
                or selector.match_expressions
            ):
                return False
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
        result = self.observe_cells({cell_id: namespace})[cell_id]
        if isinstance(result, Exception):
            raise result
        return result

    def observe_cells(self, cells: dict[str, str]) -> dict[str, ClusterObservation | Exception]:
        """Every cell in `cells` (cell id -> namespace) from one list per kind,
        so a pass costs the same few requests however many cells there are
        (D4 API budget). Objects are attributed to a cell only by its exact
        namespace (and, within it, by the name the cell renders); cellctl's
        ClusterRole already lists each kind cluster-wide. A list that fails
        raises for the whole pass;
        a cell whose own objects cannot be read is returned as its error."""

        namespaces = {ns.metadata.name: ns for ns in self._core.list_namespace().items}
        pvcs = {
            pvc.metadata.namespace: pvc
            for pvc in self._core.list_persistent_volume_claim_for_all_namespaces(
                field_selector="metadata.name=cell-data"
            ).items
        }
        pvs = {pv.metadata.name: pv for pv in self._core.list_persistent_volume().items}
        statefulsets = {
            sts.metadata.namespace: sts
            for sts in self._apps.list_stateful_set_for_all_namespaces(field_selector="metadata.name=cell").items
        }
        # Backup/restore Job pods run in the same namespace (labelled with
        # JOB_KIND_LABEL) and must never be mistaken for the StatefulSet's own
        # replica, or a backup hold can never clear once its Job's pod exists
        # (found live in 3.10). Every other pod in the namespace counts as
        # holding the RWO volume, labelled or not: an operator's owner-restore
        # pod (docs/runbooks/cloud-operator-import.md) carries no cell label.
        pods: dict[str, list] = {}
        for pod in self._core.list_pod_for_all_namespaces(label_selector=f"!{JOB_KIND_LABEL}").items:
            pods.setdefault(pod.metadata.namespace, []).append(pod)
        # D4: lost nodes, only read once local storage is configured, since
        # only a local volume is relocated: those the operator confirmed
        # stopped, and any a local volume names that the API no longer has.
        nodes = self._core.list_node().items if self._storage_config.local is not None else None
        node_presence = _node_presence(nodes)

        family_secret_references: frozenset[tuple[str, str]] | None = frozenset()
        if any((sts.metadata.annotations or {}).get(BACKUP_FAMILY_ANNOTATION) for sts in statefulsets.values()):
            try:
                contents = self._custom.list_cluster_custom_object(SNAPSHOT_GROUP, "v1", "volumesnapshotcontents")
                if (not isinstance(contents.get("items"), list)
                        or (contents.get("metadata") or {}).get("continue")):
                    raise ValueError("snapshot content observation is incomplete")
                family_secret_references = frozenset(
                    (annotations[DELETION_SECRET_NAMESPACE_ANNOTATION], annotations[DELETION_SECRET_NAME_ANNOTATION])
                    for content in contents["items"]
                    if (annotations := (content.get("metadata") or {}).get("annotations") or {})
                    and annotations.get(DELETION_SECRET_NAMESPACE_ANNOTATION) and annotations.get(DELETION_SECRET_NAME_ANNOTATION))
            except Exception as error:  # noqa: BLE001 - unknown references delay only family cleanup
                logger.warning("cellctl: family deletion references are unknown: %s", type(error).__name__)
                family_secret_references = None
        observations: dict[str, ClusterObservation | Exception] = {}
        for cell_id, namespace in cells.items():
            namespace_obj = namespaces.get(namespace)
            if namespace_obj is None:
                observations[cell_id] = ClusterObservation()
                continue
            pvc = pvcs.get(namespace)
            pv_name = pvc.spec.volume_name if pvc and pvc.spec else None
            try:
                observations[cell_id] = self._observation(
                    namespace,
                    namespace_obj,
                    pvc,
                    pvs.get(pv_name) if pv_name else None,
                    statefulsets.get(namespace),
                    pods.get(namespace, []),
                    node_presence,
                    family_secret_references,
                )
            except Exception as error:  # noqa: BLE001 - H4: one cell's failure is that cell's alone
                observations[cell_id] = error
        return observations

    def _observation(
        self, namespace: str, namespace_obj, pvc, pv, statefulset, pods: list, node_presence=None,
        family_secret_references: frozenset[tuple[str, str]] | None = frozenset(),
    ) -> ClusterObservation:
        namespace_cell_label = namespace_obj.metadata.labels.get(CELL_LABEL) if namespace_obj.metadata.labels else None
        namespace_annotations = getattr(namespace_obj.metadata, "annotations", None) or {}
        namespace_volume_lost = namespace_annotations.get(VOLUME_LOST_ANNOTATION)
        namespace_move_volume = namespace_annotations.get(MOVE_VOLUME_ANNOTATION)

        pvc_bound = bool(pvc and pvc.status and pvc.status.phase == "Bound")
        pvc_uid = pvc.metadata.uid if pvc and pvc.metadata else None
        pvc_version = getattr(pvc.metadata, "resource_version", None) if pvc and pvc.metadata else None
        resources = getattr(pvc.spec, "resources", None) if pvc and pvc.spec else None
        requested = (getattr(resources, "requests", None) or {}).get("storage")
        try:
            pvc_requested = int(quantity(str(requested))) if requested is not None else None
        except (ValueError, ArithmeticError):
            pvc_requested = None
        raw_capacity = (getattr(pvc.status, "capacity", None) or {}).get("storage") if pvc and pvc.status else None
        try:
            pvc_capacity = int(quantity(str(raw_capacity))) if raw_capacity is not None else None
        except (ValueError, ArithmeticError):
            pvc_capacity = None
        # Kubernetes defines these conditions while controller or filesystem expansion is incomplete.
        resize_pending = any(
            condition.type in {"Resizing", "FileSystemResizePending"} and condition.status == "True"
            for condition in (getattr(pvc.status, "conditions", None) or [])
        ) if pvc and pvc.status else False
        # Expansion conditions may lag an accepted request; keep its mount until actual capacity catches up.
        resize_pending = resize_pending or (pvc_requested is not None and pvc_capacity is not None
                                            and pvc_requested > pvc_capacity)
        pvc_volume_id = None  # the underlying Hetzner volume id (PV spec.csi.volumeHandle), not the PV's own K8s name
        pv_claim_ref_uid = None
        pv_storage_class = None
        pv_node = None
        pvc_storage_class = getattr(pvc.spec, "storage_class_name", None) if pvc and pvc.spec else None
        pvc_terminating = bool(pvc and pvc.metadata and getattr(pvc.metadata, "deletion_timestamp", None))
        pv_name = pv.metadata.name if pv is not None else None
        pv_reclaim_policy = getattr(pv.spec, "persistent_volume_reclaim_policy", None) if pv is not None and pv.spec else None
        if pv is not None:
            if pv.spec and pv.spec.claim_ref:
                pv_claim_ref_uid = pv.spec.claim_ref.uid
            if pv.spec:
                pv_storage_class = pv.spec.storage_class_name
            if pv.spec and pv.spec.csi:
                pvc_volume_id = pv.spec.csi.volume_handle
            local = self._storage_config.local
            if local is not None and pv_storage_class == local.class_name:
                pv_node = _pinned_node(pv, local.topology_key)

        statefulset_exists = statefulset is not None
        statefulset_image = None
        statefulset_replicas = None
        update_revision = None
        hold_kind = hold_started_at = previous_image = pre_upgrade_snapshot = None
        target_applied_at = restored_snapshot = backup_retry_after = backup_outcome = relocation_volume = None
        backup_family_request = None
        backup_retry_minutes = render_digest = render_digest_applied_at = row_generation = grow_storage_gib = None
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
            backup_family_request = annotations.get(BACKUP_FAMILY_ANNOTATION) or None
            started_raw = annotations.get(HOLD_STARTED_ANNOTATION)
            hold_started_at = _parse_timestamp(started_raw) if started_raw else None
            previous_image = annotations.get(PREVIOUS_IMAGE_ANNOTATION) or None
            pre_upgrade_snapshot = annotations.get(PRE_UPGRADE_SNAPSHOT_ANNOTATION) or None
            target_applied_at = _parse_timestamp(annotations.get(TARGET_APPLIED_ANNOTATION))
            restored_snapshot = annotations.get(RESTORED_SNAPSHOT_ANNOTATION) or None
            backup_outcome = annotations.get(BACKUP_OUTCOME_ANNOTATION) or None
            relocation_volume = annotations.get(RELOCATION_ANNOTATION) or None
            grow_raw = annotations.get(GROW_STORAGE_ANNOTATION) or ""
            grow_storage_gib = int(grow_raw) if grow_raw.isdecimal() else None
            backup_retry_after = _parse_timestamp(annotations.get(BACKUP_RETRY_AFTER_ANNOTATION))
            retry_minutes_raw = annotations.get(BACKUP_RETRY_MINUTES_ANNOTATION)
            backup_retry_minutes = int(retry_minutes_raw) if retry_minutes_raw else None
            render_digest = annotations.get(RENDER_DIGEST_ANNOTATION) or None
            render_digest_applied_at = _parse_timestamp(annotations.get(RENDER_DIGEST_APPLIED_AT_ANNOTATION))
            row_generation_raw = annotations.get(ROW_GENERATION_ANNOTATION)
            row_generation = int(row_generation_raw) if row_generation_raw and row_generation_raw.isdigit() else None

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
        init_error_code = _init_refusal(current_pod) if current_pod is not None else None

        # Only the current hold's Jobs count. Their names derive from the
        # hold-started annotation, which is the same string the Jobs were
        # named from when this StatefulSet was applied (manifests.py
        # hold_job_name). A finished Job from an earlier hold keeps a
        # different name until its TTL expires and is never read as this
        # hold's result. Outside a hold no decision reads Job state.
        backup_job = restore_job = None
        backup_snapshot_id = backup_used_bytes = backup_total_bytes = backup_failure_code = None
        backup_media_census = None
        snapshot_steps: dict[str, bool] = {}
        if backup_family_request or (started_raw and hold_kind == SNAPSHOT_BACKUP):
            snapshot_steps = self._snapshot_steps(namespace, started_raw, backup_family_request)
        if started_raw:
            backup_name = hold_job_name(BACKUP_JOB_NAME, started_raw)
            backup_job = self._get_job(namespace, backup_name)
            restore_job = self._get_job(namespace, hold_job_name(RESTORE_JOB_NAME, started_raw))
            if backup_job is not None and bool(backup_job.status.succeeded):
                report = self._job_pod_termination_message(namespace, backup_name)
                backup_snapshot_id, backup_used_bytes, backup_total_bytes = _backup_report(report)
                backup_media_census = _media_census(report)
            elif backup_job is not None and bool(backup_job.status.failed):
                message = self._job_pod_termination_message(namespace, backup_name, succeeded=False)
                backup_failure_code = message if message in BACKUP_FAILURE_CODES else None

        secret_referenced = False
        if backup_family_request:
            local = self._storage_config.local
            request = FamilyRequest.parse(backup_family_request)
            name = snapshot_backup_name(SNAPSHOT_NAME, namespace.removeprefix(NAMESPACE_PREFIX), None, request)
            secret_referenced = (None if family_secret_references is None or local is None else
                                 (local.reservation_namespace, name) in family_secret_references)
        return ClusterObservation(
            namespace_exists=True,
            namespace_cell_label=namespace_cell_label,
            pvc_bound=pvc_bound,
            pvc_volume_id=pvc_volume_id,
            pvc_uid=pvc_uid,
            pvc_resource_version=pvc_version,
            pvc_capacity_bytes=pvc_capacity,
            pvc_requested_bytes=pvc_requested,
            pvc_resize_pending=resize_pending,
            pv_claim_ref_uid=pv_claim_ref_uid,
            pv_storage_class=pv_storage_class,
            pvc_storage_class=pvc_storage_class,
            pvc_exists=pvc is not None,
            pvc_terminating=pvc_terminating,
            pv_name=pv_name,
            pv_reclaim_policy=pv_reclaim_policy,
            pv_node=pv_node,
            pv_node_stop_confirmed=pv_node is not None and node_presence is not None and pv_node in node_presence[1],
            pv_node_absent=pv_node is not None and node_presence is not None and pv_node not in node_presence[0],
            namespace_volume_lost=namespace_volume_lost,
            namespace_move_volume=namespace_move_volume,
            namespace_move_storage_class=namespace_annotations.get("exomem.io/move-storage-class"),
            namespace_move_accepted_volume=namespace_annotations.get("exomem.io/move-accepted-volume"),
            statefulset_exists=statefulset_exists,
            statefulset_image=statefulset_image,
            statefulset_replicas=statefulset_replicas,
            statefulset_hold_kind=hold_kind,
            statefulset_hold_started_at=hold_started_at,
            statefulset_previous_image=previous_image,
            statefulset_pre_upgrade_snapshot=pre_upgrade_snapshot,
            statefulset_target_applied_at=target_applied_at,
            statefulset_restored_snapshot=restored_snapshot,
            statefulset_backup_outcome=backup_outcome,
            statefulset_backup_family_request=backup_family_request,
            backup_family_secret_referenced=secret_referenced,
            statefulset_relocation_volume=relocation_volume,
            statefulset_grow_storage_gib=grow_storage_gib,
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
            init_error_code=init_error_code,
            ready_pod_image=ready_pod_image,
            **_job_observation(backup_job, prefix="backup_job", snapshot_id=backup_snapshot_id),
            backup_job_used_bytes=backup_used_bytes,
            backup_job_total_bytes=backup_total_bytes,
            backup_job_media_census=backup_media_census,
            backup_job_failure_code=backup_failure_code,
            **_job_observation(restore_job, prefix="restore_job"),
            **snapshot_steps,
        )

    def _snapshot_steps(self, namespace: str, started_raw: str | None, family_raw: str | None = None) -> dict[str, object]:
        """D3: the current hourly hold's snapshot and clone, by the names its
        start gave them; an earlier hold's never answer."""

        request = FamilyRequest.parse(family_raw) if family_raw else None
        cell_id = namespace.removeprefix(NAMESPACE_PREFIX)
        try:
            snapshot = self._custom.get_namespaced_custom_object(
                SNAPSHOT_GROUP, "v1", namespace, "volumesnapshots", snapshot_backup_name(SNAPSHOT_NAME, cell_id, started_raw, request)
            )
        except ApiException as error:
            if not _not_found(error):
                raise
            snapshot = None
        try:
            clone = self._core.read_namespaced_persistent_volume_claim(
                snapshot_backup_name(CLONE_CLAIM_NAME, cell_id, started_raw, request), namespace)
        except ApiException as error:
            if not _not_found(error):
                raise
            clone = None
        return {
            "snapshot_exists": snapshot is not None,
            "snapshot_ready": bool(snapshot and (snapshot.get("status") or {}).get("readyToUse")),
            "snapshot_created_at": _parse_timestamp(((snapshot or {}).get("status") or {}).get("creationTime")),
            "clone_exists": clone is not None,
            "clone_bound": bool(clone and clone.status and clone.status.phase == "Bound"),
        }

    def _job_pod_termination_message(self, namespace: str, job_name: str, *, succeeded: bool = True) -> str | None:
        """D8 amendment: the backup Job has no ServiceAccount token, so it
        writes the real restic snapshot id it produced to its own
        termination message; this reads it back from the pod status rather
        than trusting a hardcoded "latest". Scoped to this Job's own pods, so
        a lingering pod from an earlier hold's backup cannot answer. With
        `succeeded` False it reads a failed container's message instead, for
        the D5 vault check's code."""

        pods = self._core.list_namespaced_pod(
            namespace, label_selector=f"batch.kubernetes.io/job-name={job_name}"
        ).items
        for pod in pods:
            for status in pod.status.container_statuses or []:
                terminated = status.state.terminated if status.state else None
                if terminated and (terminated.exit_code == 0) == succeeded and terminated.message:
                    return terminated.message
        return None

    def _get_namespace(self, name: str):
        try:
            return self._core.read_namespace(name)
        except ApiException as error:
            if _not_found(error):
                return None
            raise

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
        self, *, csi_driver: str, shared_policy: SharedWorkerPolicy | None = None
    ) -> CapacityObservation:
        """One complete CSI observation, with scheduler resources for shared placement."""

        # Read CSINodes as raw JSON: a node with no CSI driver at all (K3s
        # without Hetzner CSI) serves `spec.drivers: null`, which the client's
        # generated model refuses to deserialize.
        allocatable: dict[str, int | None] = {}
        topology_keys: dict[str, list[str]] = {}
        csi_nodes = json.loads(self._storage.list_csi_node(_preload_content=False).data)
        for csi_node in csi_nodes.get("items") or []:
            allocatable_count = None
            for driver_info in (csi_node.get("spec") or {}).get("drivers") or []:
                if driver_info.get("name") == csi_driver:
                    allocatable_count = (driver_info.get("allocatable") or {}).get("count")
                    topology_keys[csi_node["metadata"]["name"]] = driver_info.get("topologyKeys") or []
            allocatable[csi_node["metadata"]["name"]] = allocatable_count

        # Observe Nodes after CSI coordinates: registration can otherwise add
        # a reserved CSINode between reads and briefly inflate general slots.
        # Unknown Nodes also contribute zero slots until positively observed
        # unreserved. Existing attachment counts remain visible below.
        nodes = self._core.list_node().items
        general_nodes = {node.metadata.name for node in nodes if _general_node(node)}

        pv_namespace: dict[str, str] = {}
        pvs = self._core.list_persistent_volume().items
        for pv in pvs:
            if pv.spec and pv.spec.claim_ref and pv.spec.claim_ref.namespace:
                pv_namespace[pv.metadata.name] = pv.spec.claim_ref.namespace

        attachments: list[AttachmentReservation] = []
        attachments_used: dict[str, int] = {}
        non_cell_attachments: dict[str, int] = {}
        for attachment in self._storage.list_volume_attachment().items:
            if attachment.spec.attacher != csi_driver:
                continue
            attached = bool(attachment.status and attachment.status.attached)
            if not shared_policy and not attached:
                continue
            node = attachment.spec.node_name
            if attached:
                attachments_used[node] = attachments_used.get(node, 0) + 1
            pv_name = (
                attachment.spec.source.persistent_volume_name
                if attachment.spec and attachment.spec.source
                else None
            )
            namespace = pv_namespace.get(pv_name) if pv_name else None
            attachments.append(AttachmentReservation(node=node, volume=pv_name or attachment.metadata.name,
                                                     cell_id=_capacity_cell_id(namespace)))
            if namespace is None or not namespace.startswith(NAMESPACE_PREFIX):
                non_cell_attachments[node] = non_cell_attachments.get(node, 0) + 1

        reserved_nodes = (set(allocatable) | set(attachments_used)) - general_nodes
        observations: dict[str, NodeObservation] = {}
        pods: list[PodReservation] = []
        incompatible: set[str] = set()
        if shared_policy:
            for node in nodes:
                status = node.status
                conditions = {condition.type: condition.status for condition in (status.conditions or [])} if status else {}
                resources = status.allocatable or {} if status else {}
                observations[node.metadata.name] = NodeObservation(
                    name=node.metadata.name,
                    attachment_limit=allocatable.get(node.metadata.name) if shared_policy.topology_key in topology_keys.get(node.metadata.name, []) else None,
                    cpu=quantity(resources.get("cpu", "0")), memory=quantity(resources.get("memory", "0")),
                    labels=node.metadata.labels or {}, ready=conditions.get("Ready") == "True",
                    schedulable=not node.spec.unschedulable,
                    pressure=any(conditions.get(name) != "False" for name in ("MemoryPressure", "DiskPressure", "PIDPressure")),
                    taints=tuple((taint.key, taint.value or "", taint.effect) for taint in node.spec.taints or []))
            workers = [node for node in nodes if (node.metadata.labels or {}).get("exomem.io/shared-profile") == shared_policy.profile]
            for pv in pvs:
                cell_id = _capacity_cell_id(pv_namespace.get(pv.metadata.name))
                if not cell_id or not shared_policy.selects(cell_id):
                    continue
                if (not pv.spec.csi or pv.spec.csi.driver != csi_driver
                        or not workers or not all(_pv_matches_node(pv, worker) for worker in workers)):
                    incompatible.add(cell_id)
            claims = {(pv.spec.claim_ref.namespace, pv.spec.claim_ref.name): pv
                      for pv in pvs if pv.spec and pv.spec.claim_ref}
            for pod in self._core.list_pod_for_all_namespaces().items:
                if pod.status and pod.status.phase in {"Succeeded", "Failed"}:
                    continue
                if (not pod.spec.node_name and workers
                        and not any(_pod_matches_node(pod, worker) for worker in workers)):
                    continue
                cpu, memory = effective_pod_requests(pod)
                pods.append(PodReservation(node=pod.spec.node_name or None,
                                           cell_id=_capacity_cell_id(pod.metadata.namespace), cpu=cpu, memory=memory))
                target = pod.spec.node_name or (workers[0].metadata.name if len(workers) == 1 else None)
                if target:
                    for volume in pod.spec.volumes or []:
                        claim = volume.persistent_volume_claim
                        if not claim:
                            continue
                        pv = claims.get((pod.metadata.namespace, claim.claim_name))
                        if pv and (not pv.spec.csi or pv.spec.csi.driver != csi_driver):
                            continue
                        # Pending mounts consume future attachment capacity too.
                        # A not-yet-bound claim is conservatively one volume.
                        attachments.append(AttachmentReservation(
                            node=target, volume=pv.metadata.name if pv else f"pvc:{pod.metadata.namespace}/{claim.claim_name}",
                            cell_id=_capacity_cell_id(pod.metadata.namespace)))
        return CapacityObservation(nodes=observations, pods=tuple(pods), attachments=tuple(attachments),
                                   incompatible_cells=frozenset(incompatible), allocatable=allocatable,
                                   attachments_used=attachments_used, non_cell_attachments=non_cell_attachments,
                                   reserved_nodes=frozenset(reserved_nodes))


    def local_capacity_inputs(self, local: LocalStorage) -> LocalCapacityObservation:
        """Read provider frames and current cell bindings for each node.

        Unguarded mode also exposes the earlier free-space observation.
        Nodes reserved from general cells publish no general slots.
        """

        nodes = self._core.list_node().items
        free: dict[str, int | None] = {}
        for node in nodes:
            raw = (node.metadata.annotations or {}).get(local.capacity_annotation)
            try:
                free[node.metadata.name] = int(quantity(raw)) if raw else None
            except (ValueError, ArithmeticError):
                free[node.metadata.name] = None
        listed = self._custom.list_cluster_custom_object(local.driver, "v1", "logicalvolumes")
        volumes = tuple(
            LogicalVolumeRecord(
                node=(item.get("spec") or {}).get("nodeName") or "",
                size=int(quantity(str((item.get("spec") or {}).get("size") or "0"))),
                device_class=(item.get("spec") or {}).get("deviceClass") or "",
                created=bool((item.get("status") or {}).get("volumeID")),
                deleting=bool((item.get("metadata") or {}).get("deletionTimestamp")),
            )
            for item in listed.get("items") or []
        )
        budgets = {node.metadata.name: parse_budget_frame(
            (node.metadata.annotations or {}).get(local.reservation_annotation),
            node_uid=getattr(node.metadata, "uid", "") or "", now=datetime.now(UTC)) for node in nodes}
        current_claims = ({(claim.metadata.namespace, claim.metadata.name): claim
                           for claim in self._core.list_persistent_volume_claim_for_all_namespaces(
                               field_selector="metadata.name=cell-data").items} if local.reservations else {})
        cell_allocations = {}
        cell_volume_ids = {}
        cell_nodes: dict[str, str] = {}
        for pv in self._core.list_persistent_volume().items:
            claim = pv.spec.claim_ref if pv.spec else None
            cell_id = _capacity_cell_id(claim.namespace) if claim else None
            node = _pinned_node(pv, local.topology_key) if pv.spec.storage_class_name == local.class_name else None
            if cell_id and node and claim.name == "cell-data":
                current = current_claims.get((claim.namespace, claim.name))
                if (local.reservations and (current is None or current.spec.volume_name != pv.metadata.name
                        or not current.metadata.uid or current.metadata.uid != getattr(claim, "uid", None))):
                    continue
                csi = getattr(pv.spec, "csi", None)
                if local.reservations and getattr(csi, "driver", None) != local.driver:
                    continue
                cell_nodes[cell_id] = node
                handle = getattr(csi, "volume_handle", None)
                matches = [item for item in listed.get("items") or []
                           if (item.get("status") or {}).get("volumeID") == handle and handle
                           and (item.get("spec") or {}).get("nodeName") == node
                           and (item.get("spec") or {}).get("deviceClass") == local.device_class]
                if len(matches) == 1:
                    item = matches[0]
                    physical_uuid = (item.get("status") or {}).get("physicalUUID")
                    if isinstance(physical_uuid, str) and physical_uuid:
                        cell_allocations[cell_id] = (item["metadata"]["name"], physical_uuid, node)
                        cell_volume_ids[cell_id] = handle
        return LocalCapacityObservation(
            free_bytes=free,
            budgets=budgets,
            cell_allocations=cell_allocations,
            cell_volume_ids=cell_volume_ids,
            volumes=volumes,
            reserved_nodes=frozenset(node.metadata.name for node in nodes if not _general_node(node)),
            cell_nodes=cell_nodes,
        )


    def source_logical_volume(self, local: LocalStorage, volume_id: str, node: str | None) -> dict:
        items = self._custom.list_cluster_custom_object(local.driver, "v1", "logicalvolumes").get("items") or []
        matches = [item for item in items if (item.get("status") or {}).get("volumeID") == volume_id
                   and (item.get("spec") or {}).get("nodeName") == node
                   and (item.get("spec") or {}).get("deviceClass") == local.device_class]
        if len(matches) != 1:
            raise ValueError("local source identity is unavailable or ambiguous")
        return matches[0]

    def logical_volume(self, local: LocalStorage, name: str) -> dict:
        return self._custom.get_cluster_custom_object(local.driver, "v1", "logicalvolumes", name)

    def patch_logical_volume(self, local: LocalStorage, name: str, spec: dict) -> None:
        self._custom.patch_cluster_custom_object(local.driver, "v1", "logicalvolumes", name, {"spec": spec})

    def local_volume_state(self, local: LocalStorage) -> LocalVolumeState:
        """D4/2.5: every object that can still hold a deleted cell's local
        data, from cluster-wide lists. A failed list raises: never absence."""

        nodes = self._core.list_node().items
        claimed = tuple(
            (pv.spec.claim_ref.namespace,
             _pinned_node(pv, local.topology_key) if pv.spec.storage_class_name == local.class_name else None,
             pv.spec.csi.volume_handle if pv.spec.csi else None)
            for pv in self._core.list_persistent_volume().items
            if pv.spec and pv.spec.claim_ref
        )
        volumes = tuple(
            LogicalVolume(
                name=item["metadata"]["name"],
                volume_id=(item.get("status") or {}).get("volumeID"),
                node=(item.get("spec") or {}).get("nodeName") or "",
                source=(item.get("spec") or {}).get("source") or None,
            )
            for item in self._custom.list_cluster_custom_object(local.driver, "v1", "logicalvolumes").get("items") or []
        )
        node_of = {volume.volume_id: volume.node for volume in volumes if volume.volume_id}
        contents = tuple(
            (((item.get("spec") or {}).get("volumeSnapshotRef") or {}).get("namespace") or "",
             node_of.get((item.get("status") or {}).get("snapshotHandle")))
            for item in self._custom.list_cluster_custom_object(SNAPSHOT_GROUP, "v1", "volumesnapshotcontents").get("items")
            or []
        )
        return LocalVolumeState(
            claimed_pvs=claimed,
            snapshot_contents=contents,
            volumes=volumes,
            present_nodes=frozenset(node.metadata.name for node in nodes),
        )


OUT_OF_SERVICE_TAINT = "node.kubernetes.io/out-of-service"
# D4: set by the operator on a cell's namespace, naming the recorded volume
# that no disk holds, so cellctl relocates the cell from its backup.
VOLUME_LOST_ANNOTATION = "exomem.io/volume-lost"
# Tasks 6.4 and 7.3: set by the operator, or by remove-agent.yml, on a cell's
# namespace, naming the volume a healthy cell is on, so cellctl moves the cell
# to a new claim through a fresh stopped backup. The role default
# k3s_remove_move_annotation repeats it; test_planned_move checks they agree.
MOVE_VOLUME_ANNOTATION = "exomem.io/move-volume"


def _node_presence(nodes) -> tuple[frozenset[str], frozenset[str]] | None:
    """D4: (the nodes in the API, those the operator confirmed stopped). None
    with no node list read: then no node is stopped or absent."""

    if nodes is None:
        return None
    return (frozenset(node.metadata.name for node in nodes),
            frozenset(node.metadata.name for node in nodes if _stop_confirmed(node)))


def _stop_confirmed(node) -> bool:
    """D4: the operator confirmed this node stopped by the node-removal rule,
    which ends in Kubernetes' own out-of-service taint, and it is not Ready.
    A Ready node carrying the taint is a mistake, never a confirmation."""

    tainted = any(taint.key == OUT_OF_SERVICE_TAINT for taint in (node.spec.taints or []))
    ready = node.status is not None and any(
        condition.type == "Ready" and condition.status == "True" for condition in (node.status.conditions or [])
    )
    return tainted and not ready


def _general_node(node) -> bool:
    """Open to general cells: no dedicated reservation, schedulable, untainted, Ready."""

    taints = node.spec.taints or []
    return (
        "exomem.io/dedicated-cell" not in (node.metadata.labels or {})
        and not any(taint.key == "exomem.io/dedicated-cell" for taint in taints)
        and not node.spec.unschedulable
        and not any(taint.effect in {"NoSchedule", "NoExecute"} for taint in taints)
        and node.status is not None
        and any(condition.type == "Ready" and condition.status == "True" for condition in (node.status.conditions or []))
    )


def _capacity_cell_id(namespace: str | None) -> str | None:
    if namespace and re.fullmatch(r"exo-cell-[a-z2-7]{16}", namespace):
        return namespace[len(NAMESPACE_PREFIX):]
    return None


def _pinned_node(pv, topology_key: str) -> str | None:
    """The one node a local PV's node affinity pins it to on `topology_key`,
    or None when it does not name exactly one."""

    affinity = getattr(pv.spec, "node_affinity", None)
    required = affinity.required if affinity else None
    nodes = {
        value
        for term in (required.node_selector_terms or [] if required else [])
        for expression in term.match_expressions or []
        if expression.key == topology_key and expression.operator == "In"
        for value in expression.values or []
    }
    return nodes.pop() if len(nodes) == 1 else None


def _pv_matches_node(pv, node) -> bool:
    affinity = pv.spec.node_affinity
    if not affinity or not affinity.required:
        return False
    return _node_selector_matches_node(affinity.required, node)


def _pod_matches_node(pod, node) -> bool:
    labels = node.metadata.labels or {}
    if any(labels.get(key) != value for key, value in (pod.spec.node_selector or {}).items()):
        return False
    affinity = pod.spec.affinity.node_affinity if pod.spec.affinity else None
    required = affinity.required_during_scheduling_ignored_during_execution if affinity else None
    if required and not _node_selector_matches_node(required, node, unknown_matches=True):
        return False
    # Unmodelled constraints/operators cannot prove incompatibility. Bound
    # Pods bypass this filter, since their demand is already on the node.
    return all(any((not item.effect or item.effect == taint.effect)
                   and (not item.key or item.key == taint.key)
                   and ((item.operator or "Equal") != "Equal" or (item.value or "") == (taint.value or ""))
                   for item in pod.spec.tolerations or [])
               for taint in node.spec.taints or [] if taint.effect in {"NoSchedule", "NoExecute"})


def _node_selector_matches_node(selector, node, *, unknown_matches: bool = False) -> bool:
    labels = node.metadata.labels or {}

    def matches(expression, values) -> bool:
        value = values.get(expression.key)
        options = expression.values or []
        if expression.operator == "In":
            return value is not None and value in options
        if expression.operator == "NotIn":
            return value is None or value not in options
        if expression.operator == "Exists":
            return value is not None
        if expression.operator == "DoesNotExist":
            return value is None
        if expression.operator in {"Gt", "Lt"}:
            if value is None or len(options) != 1:
                return False
            try:
                return int(value) > int(options[0]) if expression.operator == "Gt" else int(value) < int(options[0])
            except ValueError:
                return False
        return unknown_matches

    return any((term.match_expressions or term.match_fields)
               and all(matches(expression, labels) for expression in term.match_expressions or [])
               and all((unknown_matches and expression.key != "metadata.name")
                       or matches(expression, {"metadata.name": node.metadata.name}) for expression in term.match_fields or [])
               for term in selector.node_selector_terms or [])


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


def _init_refusal(pod) -> str | None:
    """D5: the refusal code cell-init wrote as its termination message, from
    its current or (while it crash-loops) its last run. Anything that is not
    one of cell-init's own codes is ignored, never copied to the row."""

    for status in pod.status.init_container_statuses or []:
        if status.name != INIT_CONTAINER_NAME:
            continue
        for state in (status.state, status.last_state):
            terminated = state.terminated if state else None
            if terminated is not None and terminated.message in INIT_REFUSAL_CODES:
                return terminated.message
    return None


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


# D10: a backup that measured its filesystem appends "<used> <total>" bytes to
# the snapshot id. Anything else is the snapshot id alone, which decide.py
# still checks is a real one.
_BACKUP_REPORT = re.compile(r"(?P<snapshot>[0-9a-f]{64}) (?P<used>[0-9]{1,19}) (?P<total>[0-9]{1,19})")


def _backup_report(message: str | None) -> tuple[str | None, int | None, int | None]:
    """The snapshot id, and the used and total bytes when the Job measured
    them consistently. A use report that does not add up is dropped; the
    backup it came with still counts."""

    # add-cloud-media-store D12: the media census follows on a line of its own.
    report = None if message is None else message.partition("\n")[0]
    match = _BACKUP_REPORT.fullmatch(report or "")
    if match is None:
        return report, None, None
    used, total = int(match["used"]), int(match["total"])
    if not 0 <= used <= total or total == 0:
        return match["snapshot"], None, None
    return match["snapshot"], used, total


# add-cloud-media-store D12: the backup Job's second line, "<evicted count> <evicted bytes>
# <capture time>", a format manifests.render_backup_job fixes. "unknown" is an incomplete census.
_MEDIA_CENSUS = re.compile(r"(?P<evicted>[0-9]{1,19}|unknown) (?P<bytes>[0-9]{1,19}|unknown) (?P<captured>\S+)")


def _media_census(message: str | None) -> MediaCensus | None:
    """The census a backup Job reported, or None when it reported none that cellctl can read.
    None keeps a turned-off cell's key and egress, as no report does."""

    match = _MEDIA_CENSUS.fullmatch((message or "").partition("\n")[2])
    if match is None:
        return None
    try:
        captured_at = datetime.fromisoformat(match["captured"])
    except ValueError:
        return None
    if captured_at.tzinfo is None:
        return None
    evicted, evicted_bytes = (None if match[key] == "unknown" else int(match[key]) for key in ("evicted", "bytes"))
    return MediaCensus(evicted=evicted, evicted_bytes=evicted_bytes, captured_at=captured_at)


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

