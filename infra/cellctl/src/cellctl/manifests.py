"""Renders the D5 per-cell Kubernetes manifests as plain dicts.

cellctl applies these with server-side apply (see reconcile.py). This module
has no I/O: every function is a pure render from a `CellManifestSpec` to one
or more manifest dicts, which is what makes task 3.2's tests exercisable
without a cluster.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .backup_source import VAULT_CHECK_COMMAND
from .storage_config import LEGACY_CLASS

# D4: part of every cell's render digest. Bump it whenever the manifests this
# module renders change, so a new cellctl release reaches cells that have
# already converged.
RENDER_VERSION = 1

NAMESPACE_PREFIX = "exo-cell-"
GATEWAY_NAMESPACE = "exomem-cloud"
GATEWAY_POD_LABEL = "app.kubernetes.io/name"
GATEWAY_POD_LABEL_VALUE = "exomem-cloud-gateway"
CELL_PORT = 8765
ARTIFACT_BROKER_PORT = 8767
ARTIFACT_BROKER_POD_LABEL_VALUE = "exomem-artifact-broker"
STORAGE_CLASS = LEGACY_CLASS
RUNTIME_UID = 10001
RUNTIME_GID = 10001
JOB_KIND_LABEL = "exomem.io/cell-job"
INIT_CONTAINER_NAME = "cell-init"
POD_SECURITY_VERSION = "v1.35"

# D5: the Secret key that tells cell-init the cell has a recorded backup.
BACKED_UP_KEY = "backed-up"

HOLD_ANNOTATION = "exomem.io/hold"
HOLD_STARTED_ANNOTATION = "exomem.io/hold-started-at"
PREVIOUS_IMAGE_ANNOTATION = "exomem.io/previous-image"
PRE_UPGRADE_SNAPSHOT_ANNOTATION = "exomem.io/pre-upgrade-snapshot"
TARGET_APPLIED_ANNOTATION = "exomem.io/target-applied-at"
RESTORED_SNAPSHOT_ANNOTATION = "exomem.io/restored-snapshot"
# Not in D6's own annotation list: bookkeeping so the D6/D8 backoff can
# double per consecutive failure and reset on success, without a DB column.
BACKUP_RETRY_AFTER_ANNOTATION = "exomem.io/backup-retry-after"
BACKUP_RETRY_MINUTES_ANNOTATION = "exomem.io/backup-retry-minutes"
# D3 (move-cloud-cells-to-local-storage): an hourly hold's outcome, kept on the
# StatefulSet while the hold removes its clone and snapshot.
BACKUP_OUTCOME_ANNOTATION = "exomem.io/backup-outcome"
# D4: a restore hold that relocates the cell, naming the volume it leaves.
RELOCATION_ANNOTATION = "exomem.io/relocation-volume"
# D10: the size an hourly hold grows its cell to once its clone is gone.
GROW_STORAGE_ANNOTATION = "exomem.io/grow-storage-gib"
# D4: the render digest, compared against on every pass so a change that is
# not a row change (bearer rotation, chart-level cell settings) still
# reaches a converged cell.
RENDER_DIGEST_ANNOTATION = "exomem.io/render-digest"
# D4: when a digest-only re-apply last landed. The next digest candidate waits
# only while a cell re-applied less than 10 minutes ago is not yet Ready.
RENDER_DIGEST_APPLIED_AT_ANNOTATION = "exomem.io/render-digest-applied-at"
# D4: the row generation this StatefulSet was rendered for. A pass changes
# the live StatefulSet when this or the render digest differs from the row,
# and such a pass never also records convergence.
ROW_GENERATION_ANNOTATION = "exomem.io/row-generation"


def namespace_name(cell_id: str) -> str:
    return f"{NAMESPACE_PREFIX}{cell_id}"


def check_artifact_broker_url(endpoint: str) -> None:
    """The preflighted Service IPv4 address needs neither runtime DNS nor discovery."""
    if not endpoint:
        return
    try:
        parsed = urlsplit(endpoint)
        address = ipaddress.IPv4Address(parsed.hostname or "")
        private = any(address in ipaddress.IPv4Network(cidr) for cidr in (
            "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
        ))
        valid = private and endpoint in (
            f"http://{address}:{ARTIFACT_BROKER_PORT}",
            f"http://{address}:{ARTIFACT_BROKER_PORT}/",
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("artifact broker endpoint must be literal RFC1918 IPv4 HTTP on port 8767")


@dataclass(frozen=True)
class ResourceSettings:
    cpu_request: str = "250m"
    cpu_limit: str = "2"
    memory_request: str = "1Gi"
    memory_limit: str = "3Gi"


# limits.memory of one backup/restore/init Job pod (`_job_pod_spec` and the
# cell-init container). The quota counts every non-terminal pod, so limits.memory
# carries this much headroom over the serving pod's own limit. Only that key has
# it: requests.cpu, requests.memory and limits.cpu still equal the serving pod's
# values, so a Job next to the serving pod is bounded by those.
JOB_MEMORY_LIMIT_MIB = 1024
# Requests of one backup/restore Job pod.
JOB_CPU_REQUEST = "100m"
JOB_MEMORY_REQUEST = "256Mi"
JOB_CPU_LIMIT = "1"


def _memory_mib(quantity: str) -> int:
    """A Gi/Mi memory quantity in MiB; anything else is a chart-value error."""
    for suffix, factor in (("Gi", 1024), ("Mi", 1)):
        if quantity.endswith(suffix) and quantity.removesuffix(suffix).isdigit():
            return int(quantity.removesuffix(suffix)) * factor
    raise ValueError(f"memory limit must be a whole number of Gi or Mi, got {quantity!r}")


def _quota_memory_limit(memory_limit: str) -> str:
    return _memory(_memory_mib(memory_limit) + JOB_MEMORY_LIMIT_MIB)


@dataclass(frozen=True)
class CellManifestSpec:
    """Everything the D5 renderers need for one cell, for one reconcile pass."""

    cell_id: str
    image: str
    replicas: int
    read_only: bool
    # The size the claim and quota render at: CellRow.size_gib for a local
    # claim, storage_gib for a Hetzner one (reconcile.py).
    storage_gib: int = 10
    resources: ResourceSettings = field(default_factory=ResourceSettings)
    model_env: dict[str, str] = field(default_factory=dict)
    dedicated_node: bool = False
    placement: dict = field(default_factory=dict)
    # D7: the class of the cell's claim, kept until the cell migrates.
    storage_class: str = STORAGE_CLASS
    # A cell on a node-local volume (move-cloud-cells-to-local-storage): its
    # cell-init carries the empty-vault guard (D5) and its quota admits the
    # hourly backup's clone and Job (D3). Cells on Hetzner volumes render as
    # before.
    local_volume: bool = False
    # D5: the cell has a recorded backup. Written into the Secret only, so
    # the first backup changes neither the pod template nor the digest.
    backed_up: bool = False

    # Secret material (D7). cellctl renders these from the row and its master
    # keys on every pass; it never reads the Secret back.
    bearer_current: str = ""
    bearer_previous: str | None = None
    backup_password: str = ""
    b2_key_id: str = ""
    b2_key_secret: str = ""

    # D4/D6 hold and rollout-attempt annotations, or None outside a hold.
    hold_kind: str | None = None
    hold_started_at: str | None = None
    previous_image: str | None = None
    pre_upgrade_snapshot: str | None = None
    target_applied_at: str | None = None
    restored_snapshot: str | None = None
    backup_outcome: str | None = None
    relocation_volume: str | None = None
    grow_storage_gib: int | None = None

    # D6/D8: the backup-retry-after backoff. Unlike the hold annotations
    # above, these must survive a hold ending, so render_statefulset writes
    # them unconditionally rather than only while spec.hold_kind is set.
    backup_retry_after: str | None = None
    backup_retry_minutes: int | None = None

    # D4: the render digest for this pass's non-secret inputs, always
    # written so the next pass can detect drift even outside a hold.
    render_digest: str | None = None
    render_digest_applied_at: str | None = None
    row_generation: int | None = None

    # D5: CIDR ranges the job-egress NetworkPolicy excepts from its
    # otherwise-0.0.0.0/0 rule to object storage on 443.
    job_egress_except: tuple[str, ...] = ()
    artifact_broker_url: str = ""

    def __post_init__(self) -> None:
        check_artifact_broker_url(self.artifact_broker_url)

    @property
    def namespace(self) -> str:
        return namespace_name(self.cell_id)

    @property
    def secret_name(self) -> str:
        return "cell-credentials"

    @property
    def pvc_name(self) -> str:
        return "cell-data"

    @property
    def statefulset_name(self) -> str:
        return "cell"


def _labels(spec: CellManifestSpec) -> dict[str, str]:
    return {
        "app.kubernetes.io/name": "exomem-cell",
        "app.kubernetes.io/part-of": "exomem-cloud",
        "exomem.io/cell": spec.cell_id,
    }


def _selector_labels(spec: CellManifestSpec) -> dict[str, str]:
    return {
        "app.kubernetes.io/name": "exomem-cell",
        "exomem.io/cell": spec.cell_id,
    }


def render_namespace(spec: CellManifestSpec) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": spec.namespace,
            "labels": {
                **_labels(spec),
                "exomem.io/cloud-cell": spec.cell_id,
                "pod-security.kubernetes.io/enforce": "restricted",
                "pod-security.kubernetes.io/enforce-version": POD_SECURITY_VERSION,
                "pod-security.kubernetes.io/audit": "restricted",
                "pod-security.kubernetes.io/audit-version": POD_SECURITY_VERSION,
                "pod-security.kubernetes.io/warn": "restricted",
                "pod-security.kubernetes.io/warn-version": POD_SECURITY_VERSION,
            },
        },
    }


def _cpu_millicores(quantity: str) -> int:
    """A whole-millicore CPU quantity; anything else is a chart-value error."""
    if quantity.endswith("m") and quantity.removesuffix("m").isdigit():
        return int(quantity.removesuffix("m"))
    if quantity.isdigit():
        return int(quantity) * 1000
    raise ValueError(f"cpu quantity must be whole cores or millicores, got {quantity!r}")


def _cpu(millicores: int) -> str:
    return str(millicores // 1000) if millicores % 1000 == 0 else f"{millicores}m"


def _memory(mib: int) -> str:
    return f"{mib // 1024}Gi" if mib % 1024 == 0 else f"{mib}Mi"


def render_resource_quota(spec: CellManifestSpec) -> dict:
    r = spec.resources
    hard = {
        "persistentvolumeclaims": "1",
        "requests.storage": f"{spec.storage_gib}Gi",
        # One serving pod, plus a second pod slot for a backup/restore Job
        # that briefly overlaps it during a hold transition.
        "pods": "2",
        "requests.cpu": r.cpu_request,
        "requests.memory": r.memory_request,
        "limits.cpu": r.cpu_limit,
        "limits.memory": _quota_memory_limit(r.memory_limit),
    }
    if spec.local_volume:
        # D3: the hourly backup runs beside the serving pod, so the quota
        # admits its clone claim (a second claim of the same size) and one
        # backup Job's requests and CPU limit on top of the serving pod's.
        hard.update({
            "persistentvolumeclaims": "2",
            "requests.storage": f"{2 * spec.storage_gib}Gi",
            "requests.cpu": _cpu(_cpu_millicores(r.cpu_request) + _cpu_millicores(JOB_CPU_REQUEST)),
            "requests.memory": _memory(_memory_mib(r.memory_request) + _memory_mib(JOB_MEMORY_REQUEST)),
            "limits.cpu": _cpu(_cpu_millicores(r.cpu_limit) + _cpu_millicores(JOB_CPU_LIMIT)),
        })
    return {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": {"name": "cell-quota", "namespace": spec.namespace, "labels": _labels(spec)},
        "spec": {"hard": hard},
    }


def render_network_policies(spec: CellManifestSpec) -> list[dict]:
    """Default deny, gateway-only runtime ingress, optional broker-only egress,
    and backup/restore Jobs limited to TCP 443 plus DNS (D5, D11)."""

    default_deny = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": "default-deny",
            "namespace": spec.namespace,
            "labels": _labels(spec),
        },
        "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
    }
    runtime_ingress = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": "runtime-ingress",
            "namespace": spec.namespace,
            "labels": _labels(spec),
        },
        "spec": {
            "podSelector": {"matchLabels": _selector_labels(spec)},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [
                {
                    "from": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": GATEWAY_NAMESPACE
                                }
                            },
                            "podSelector": {
                                "matchLabels": {GATEWAY_POD_LABEL: GATEWAY_POD_LABEL_VALUE}
                            },
                        }
                    ],
                    "ports": [{"protocol": "TCP", "port": CELL_PORT}],
                }
            ],
            "egress": [],
        },
    }
    if spec.artifact_broker_url:
        runtime_ingress["spec"]["egress"] = [{
            "to": [{
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": GATEWAY_NAMESPACE}},
                "podSelector": {"matchLabels": {GATEWAY_POD_LABEL: ARTIFACT_BROKER_POD_LABEL_VALUE}},
            }],
            "ports": [{"protocol": "TCP", "port": ARTIFACT_BROKER_PORT}],
        }]
    # L4: a bare `ports: [443]` rule with no `to:` reaches any in-cluster
    # listener and the node's hostPort on 443, not just object storage. An
    # ipBlock of 0.0.0.0/0 that excepts the cluster/private/CGNAT/link-local
    # ranges (chart value cells.jobEgressExcept) keeps the intended "object
    # storage on the public internet" scope while blocking in-cluster and
    # metadata-service targets. The local rehearsal overrides the value so
    # its in-cluster S3 double stays reachable.
    egress_443_to: list[dict] = [{"ipBlock": {"cidr": "0.0.0.0/0", "except": list(spec.job_egress_except)}}]
    job_egress = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": "job-egress",
            "namespace": spec.namespace,
            "labels": _labels(spec),
        },
        "spec": {
            "podSelector": {"matchExpressions": [{"key": JOB_KIND_LABEL, "operator": "Exists"}]},
            "policyTypes": ["Egress"],
            "egress": [
                {"to": egress_443_to, "ports": [{"protocol": "TCP", "port": 443}]},
                {
                    "to": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                            },
                            "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                        }
                    ],
                    "ports": [
                        {"protocol": "UDP", "port": 53},
                        {"protocol": "TCP", "port": 53},
                    ],
                },
            ],
        },
    }
    return [default_deny, runtime_ingress, job_egress]


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def render_secret(spec: CellManifestSpec) -> dict:
    data = {
        "cell-token": _b64(spec.bearer_current),
        "backup-password": _b64(spec.backup_password),
        "b2-key-id": _b64(spec.b2_key_id),
        "b2-key-secret": _b64(spec.b2_key_secret),
    }
    if spec.bearer_previous:
        data["cell-token-previous"] = _b64(spec.bearer_previous)
    if spec.local_volume and spec.backed_up:
        data[BACKED_UP_KEY] = _b64("true")
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": spec.secret_name,
            "namespace": spec.namespace,
            "labels": _labels(spec),
        },
        "type": "Opaque",
        "data": data,
    }


def render_pvc(spec: CellManifestSpec) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": spec.pvc_name,
            "namespace": spec.namespace,
            "labels": _labels(spec),
            "annotations": {"helm.sh/resource-policy": "keep"},
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "storageClassName": spec.storage_class,
            "resources": {"requests": {"storage": f"{spec.storage_gib}Gi"}},
        },
    }


def render_service(spec: CellManifestSpec) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "cell", "namespace": spec.namespace, "labels": _labels(spec)},
        "spec": {
            "type": "ClusterIP",
            "selector": _selector_labels(spec),
            "ports": [{"name": "http", "port": CELL_PORT, "targetPort": "http", "protocol": "TCP"}],
        },
    }


def _pod_security_context() -> dict:
    return {
        "runAsNonRoot": True,
        "runAsUser": RUNTIME_UID,
        "runAsGroup": RUNTIME_GID,
        "fsGroup": RUNTIME_GID,
        "fsGroupChangePolicy": "OnRootMismatch",
        "seccompProfile": {"type": "RuntimeDefault"},
    }


def _container_security_context() -> dict:
    return {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }


# NEW-2: model_env is operator-only chart configuration, so this stays a
# denylist rather than an allowlist (which would make every new model setting
# a cellctl change). It refuses what relocates state, config or a writable
# directory in src/exomem -- cloud-mode state roots follow HOME and XDG_*,
# so the image's passwd home stays in charge -- and every variable cellctl
# renders itself, which a duplicate entry would silently shadow.
MODEL_ENV_FORBIDDEN_KEYS = frozenset(
    {
        "HOME",
        "TMPDIR",
        "EXOMEM_VAULT_PATH",
        "EXOMEM_LOG_DIR",
        "EXOMEM_STATE_ROOT",
        "EXOMEM_HOSTED_STATE_ROOT",
        "EXOMEM_WRITER_LEASE_STATE_DIR",
        "EXOMEM_CONFIG_PATH",
        "EXOMEM_CALL_LEDGER_DIR",
        "EXOMEM_KB_DIRNAME",
        "EXOMEM_LEASE_COORDINATOR_DB",
        "EXOMEM_RANKING_CONFIG",
        "EXOMEM_HOOK_HOME",
        "EXOMEM_SERVICE_ENV",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "HF_HOME",
        "HF_HUB_CACHE",
    }
)
# Also refused: anything that picks the code the cell runs (the loader, the
# interpreter, helper executables), changes the process's mode or carries a
# credential or an endpoint.
MODEL_ENV_FORBIDDEN_KEYS = MODEL_ENV_FORBIDDEN_KEYS | {"PATH", "EXOMEM_UV", "EXOMEM_MODE"}
MODEL_ENV_FORBIDDEN_PREFIXES = (
    "XDG_",
    "LD_",
    "PYTHON",
    "EXOMEM_CLOUD_",
    "EXOMEM_HOSTED_",
    "EXOMEM_WRITER_LEASE_",
    "EXOMEM_LEASE_",
    "EXOMEM_OAUTH_",
    "EXOMEM_AUTH_",
)
MODEL_ENV_FORBIDDEN_SUFFIXES = (
    "_CMD",
    "_PYTHON",
    "_BIN",
    "_KEY",
    "_TOKEN",
    "_SECRET",
    "_URL",
    "_URLS",
    "_FILE",
    "_PATH",
    "_DIR",
    "_HOME",
    "_ROOT",
    "_DB",
)
_MODEL_ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]*")


def check_model_env(model_env: object) -> None:
    """Raises ValueError naming the first model_env entry cellctl refuses.
    It must be a map of upper-case names to strings. Checked when cellctl
    loads its settings, and again on every render."""

    if not isinstance(model_env, dict):
        raise ValueError("cell model environment must be a JSON object")
    for key in sorted(model_env, key=str):
        if not isinstance(key, str) or not _MODEL_ENV_NAME_RE.fullmatch(key):
            raise ValueError(f"cell model environment has an invalid name: {key!r}")
        if not isinstance(model_env[key], str):
            raise ValueError(f"cell model environment value for {key} must be a string")
        if (
            key in MODEL_ENV_FORBIDDEN_KEYS
            or key.startswith(MODEL_ENV_FORBIDDEN_PREFIXES)
            or key.endswith(MODEL_ENV_FORBIDDEN_SUFFIXES)
        ):
            raise ValueError(f"cell model environment may not set {key}")


def _runtime_env(spec: CellManifestSpec) -> list[dict]:
    env = [
        {"name": "EXOMEM_CLOUD_CELL", "value": "1"},
        {"name": "EXOMEM_CLOUD_CELL_ID", "value": spec.cell_id},
        {
            "name": "EXOMEM_CLOUD_CELL_TOKEN",
            "valueFrom": {"secretKeyRef": {"name": spec.secret_name, "key": "cell-token"}},
        },
    ]
    if spec.bearer_previous:
        # Optional: inside a hold the StatefulSet is applied even when this
        # pass's Secret was refused, and a required reference to a key the
        # live Secret lacks would keep the pod from starting at all.
        env.append(
            {
                "name": "EXOMEM_CLOUD_CELL_TOKEN_PREVIOUS",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": spec.secret_name,
                        "key": "cell-token-previous",
                        "optional": True,
                    }
                },
            }
        )
    if spec.read_only:
        env.append({"name": "EXOMEM_CLOUD_READ_ONLY", "value": "1"})
    if spec.artifact_broker_url:
        env.append({"name": "EXOMEM_CLOUD_ARTIFACT_BROKER_URL", "value": spec.artifact_broker_url})
    env.extend(
        [
            {"name": "EXOMEM_VAULT_PATH", "value": "/data/vault"},
            {"name": "TMPDIR", "value": "/tmp"},
            {"name": "EXOMEM_LOG_DIR", "value": "/tmp/exomem-logs"},
        ]
    )
    # Defence in depth: settings load already refused these (main.py).
    check_model_env(spec.model_env)
    for key, value in sorted(spec.model_env.items()):
        env.append({"name": key, "value": value})
    return env


def _init_env(spec: CellManifestSpec) -> list[dict]:
    env = [
        # Cloud mode makes cell-init create the /data/host
        # custody root and redact its own output.
        {"name": "EXOMEM_CLOUD_CELL", "value": "1"},
        {"name": "EXOMEM_CLOUD_CELL_ID", "value": spec.cell_id},
        {"name": "TMPDIR", "value": "/tmp"},
    ]
    if spec.local_volume:
        # D5: optional, and present from the cell's first render, so the key
        # appearing after the first backup changes nothing here.
        env.append({
            "name": "EXOMEM_CLOUD_CELL_BACKED_UP",
            "valueFrom": {"secretKeyRef": {"name": spec.secret_name, "key": BACKED_UP_KEY, "optional": True}},
        })
    return env


def dedicated_placement(cell_id: str) -> dict:
    return {
        "nodeSelector": {"exomem.io/dedicated-cell": cell_id},
        "tolerations": [{"key": "exomem.io/dedicated-cell", "operator": "Equal",
                         "value": cell_id, "effect": "NoSchedule"}],
    }


def _dedicated_placement(spec: CellManifestSpec) -> dict:
    return spec.placement or (dedicated_placement(spec.cell_id) if spec.dedicated_node else {})


def render_statefulset(spec: CellManifestSpec) -> dict:
    r = spec.resources
    annotations: dict[str, str] = {}
    if spec.hold_kind:
        annotations[HOLD_ANNOTATION] = spec.hold_kind
        if spec.hold_started_at:
            annotations[HOLD_STARTED_ANNOTATION] = spec.hold_started_at
        if spec.previous_image:
            annotations[PREVIOUS_IMAGE_ANNOTATION] = spec.previous_image
        if spec.pre_upgrade_snapshot:
            annotations[PRE_UPGRADE_SNAPSHOT_ANNOTATION] = spec.pre_upgrade_snapshot
        if spec.target_applied_at:
            annotations[TARGET_APPLIED_ANNOTATION] = spec.target_applied_at
        if spec.restored_snapshot:
            annotations[RESTORED_SNAPSHOT_ANNOTATION] = spec.restored_snapshot
        if spec.backup_outcome:
            annotations[BACKUP_OUTCOME_ANNOTATION] = spec.backup_outcome
        if spec.relocation_volume:
            annotations[RELOCATION_ANNOTATION] = spec.relocation_volume
        if spec.grow_storage_gib is not None:
            annotations[GROW_STORAGE_ANNOTATION] = str(spec.grow_storage_gib)

    # D6/D8: the backup-retry-after backoff must survive a hold ending, so
    # it is written unconditionally rather than only inside the `if
    # spec.hold_kind` block above.
    if spec.backup_retry_after:
        annotations[BACKUP_RETRY_AFTER_ANNOTATION] = spec.backup_retry_after
    if spec.backup_retry_minutes is not None:
        annotations[BACKUP_RETRY_MINUTES_ANNOTATION] = str(spec.backup_retry_minutes)
    # D4: the render digest is always current for this pass's inputs.
    if spec.render_digest:
        annotations[RENDER_DIGEST_ANNOTATION] = spec.render_digest
    if spec.render_digest_applied_at:
        annotations[RENDER_DIGEST_APPLIED_AT_ANNOTATION] = spec.render_digest_applied_at
    if spec.row_generation is not None:
        annotations[ROW_GENERATION_ANNOTATION] = str(spec.row_generation)

    return {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {
            "name": spec.statefulset_name,
            "namespace": spec.namespace,
            "labels": _labels(spec),
            "annotations": annotations,
        },
        "spec": {
            "replicas": spec.replicas,
            "serviceName": "cell",
            "podManagementPolicy": "OrderedReady",
            "updateStrategy": {"type": "RollingUpdate"},
            "selector": {"matchLabels": _selector_labels(spec)},
            "template": {
                # D4: the digest is also a pod-template annotation, so a digest
                # change restarts the pod and the new environment reaches it.
                "metadata": {
                    "labels": _selector_labels(spec),
                    "annotations": {RENDER_DIGEST_ANNOTATION: spec.render_digest} if spec.render_digest else {},
                },
                "spec": {
                    **_dedicated_placement(spec),
                    "automountServiceAccountToken": False,
                    "restartPolicy": "Always",
                    "terminationGracePeriodSeconds": 30,
                    "securityContext": _pod_security_context(),
                    "initContainers": [
                        {
                            "name": INIT_CONTAINER_NAME,
                            "image": spec.image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["exomem", "cell-init"],
                            "args": ["--vault", "/data/vault", "--json"],
                            "env": _init_env(spec),
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "256Mi"},
                                "limits": {"cpu": "1", "memory": "1Gi"},
                            },
                            "securityContext": {
                                "runAsNonRoot": True,
                                "runAsUser": RUNTIME_UID,
                                "runAsGroup": RUNTIME_GID,
                                **_container_security_context(),
                            },
                            "volumeMounts": [
                                {"name": "data", "mountPath": "/data"},
                                {"name": "tmp", "mountPath": "/tmp"},
                            ],
                        }
                    ],
                    "containers": [
                        {
                            "name": "exomem",
                            "image": spec.image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": [
                                "exomem",
                                "--transport",
                                "http",
                                "--host",
                                "0.0.0.0",
                                "--port",
                                str(CELL_PORT),
                            ],
                            "ports": [
                                {"name": "http", "containerPort": CELL_PORT, "protocol": "TCP"}
                            ],
                            "env": _runtime_env(spec),
                            "resources": {
                                "requests": {
                                    "cpu": r.cpu_request,
                                    "memory": r.memory_request,
                                },
                                "limits": {
                                    "cpu": r.cpu_limit,
                                    "memory": r.memory_limit,
                                },
                            },
                            "securityContext": {
                                "runAsNonRoot": True,
                                "runAsUser": RUNTIME_UID,
                                "runAsGroup": RUNTIME_GID,
                                **_container_security_context(),
                            },
                            "readinessProbe": {
                                "httpGet": {"path": "/health/ready", "port": "http"},
                                "periodSeconds": 5,
                                "timeoutSeconds": 2,
                                "failureThreshold": 2,
                            },
                            "livenessProbe": {
                                "httpGet": {"path": "/health", "port": "http"},
                                "periodSeconds": 10,
                                "timeoutSeconds": 2,
                                "failureThreshold": 3,
                            },
                            # D5: 5 minutes (period 10s x failure threshold
                            # 30) before liveness applies, so a slow cold
                            # start is not killed and misread as a failed
                            # canary.
                            "startupProbe": {
                                "httpGet": {"path": "/health", "port": "http"},
                                "periodSeconds": 10,
                                "timeoutSeconds": 2,
                                "failureThreshold": 30,
                            },
                            "volumeMounts": [
                                {"name": "data", "mountPath": "/data"},
                                {"name": "tmp", "mountPath": "/tmp"},
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "data",
                            "persistentVolumeClaim": {"claimName": spec.pvc_name},
                        },
                        {"name": "tmp", "emptyDir": {}},
                    ],
                },
            },
        },
    }


def render_cell_manifests(spec: CellManifestSpec) -> list[dict]:
    """All D5 manifests for one cell, in an apply-safe order (namespace first)."""

    return [
        render_namespace(spec),
        render_resource_quota(spec),
        *render_network_policies(spec),
        render_secret(spec),
        render_pvc(spec),
        render_statefulset(spec),
        render_service(spec),
    ]


# --- D8 backup and restore Jobs ---------------------------------------------

JOB_KIND_BACKUP = "backup"
JOB_KIND_RESTORE = "restore"
BACKUP_JOB_NAME = "cell-backup"
RESTORE_JOB_NAME = "cell-restore"
# D3: an hourly hold's VolumeSnapshot and the read-only clone claim made from it.
SNAPSHOT_NAME = "cell-snapshot"
CLONE_CLAIM_NAME = "cell-clone"
RETENTION_ARGS = ["--keep-daily", "7", "--keep-weekly", "4"]
# D3: cells backed up hourly also keep the last day's hourly snapshots.
HOURLY_RETENTION_ARGS = ["--keep-hourly", "24", *RETENTION_ARGS]
# D8: what a backup covers and a restore rewrites. The volume root, including
# lost+found, is never in scope.
BACKUP_PATHS = ("/data/vault", "/data/host")

# move-cloud-cells-to-local-storage D10: the backed-up filesystem's used and
# total bytes, appended to the termination message after the snapshot id. Every
# cell image is a Python image; statvfs needs no privilege and no tool whose
# output format differs between images.
FILESYSTEM_USE_COMMAND = (
    "python3 -c 'import os; s = os.statvfs(\"/data\"); "
    "print(\" %d %d\" % ((s.f_blocks - s.f_bfree) * s.f_frsize, s.f_blocks * s.f_frsize), end=\"\")'"
)
SNAPSHOT_ID_RE = re.compile(r"[0-9a-f]{64}")  # always fullmatch: `$` accepts a trailing newline


def hold_job_name(base: str, hold_started_at: str) -> str:
    """The backup/restore Job name for one hold: `base` plus a short hash of
    the hold's start time.

    A finished Job lingers for `ttlSecondsAfterFinished`, so under one fixed
    name a new hold's apply landed on the previous hold's finished Job (a
    no-op) and observe() read that stale result back -- a "successful"
    backup or restore that never ran. `hold_started_at` is constant for the
    life of one hold and new for every hold, so each hold gets its own Job.
    It is hashed rather than embedded so the name stays DNS-1123-safe.
    """

    digest = hashlib.sha256(hold_started_at.encode("utf-8")).hexdigest()[:10]
    return f"{base}-{digest}"


def _job_name_for_hold(spec: CellManifestSpec, base: str) -> str:
    # decide() only ever asks for a Job inside a hold, so a missing start
    # time is a caller bug; falling back to a bare name would bring back
    # stale-Job reuse.
    if not spec.hold_started_at:
        raise ValueError(f"{base} Job rendered outside a hold")
    return hold_job_name(base, spec.hold_started_at)


def _restic_repo(spec: CellManifestSpec, bucket_name: str, endpoint: str) -> str:
    # D8 amendment: restic reaches B2 through its S3-compatible endpoint, not
    # its native "b2:" backend, so a local S3 server (MinIO in 3.10) is a
    # faithful test double for the real thing.
    return f"s3:{endpoint}/{bucket_name}/cells/{spec.cell_id}"


def _restic_env(spec: CellManifestSpec, repo: str) -> list[dict]:
    return [
        # The repository carries chart values (endpoint, bucket); it reaches
        # restic through its environment, never through the `sh -c` string.
        {"name": "RESTIC_REPOSITORY", "value": repo},
        {
            # The per-cell B2 application key id/secret double as the S3
            # access key id/secret against B2's S3-compatible endpoint.
            "name": "AWS_ACCESS_KEY_ID",
            "valueFrom": {"secretKeyRef": {"name": spec.secret_name, "key": "b2-key-id"}},
        },
        {
            "name": "AWS_SECRET_ACCESS_KEY",
            "valueFrom": {"secretKeyRef": {"name": spec.secret_name, "key": "b2-key-secret"}},
        },
        {
            "name": "RESTIC_PASSWORD",
            "valueFrom": {"secretKeyRef": {"name": spec.secret_name, "key": "backup-password"}},
        },
        {"name": "RESTIC_CACHE_DIR", "value": "/cache"},
        {"name": "TMPDIR", "value": "/tmp"},
    ]


def render_volume_snapshot(spec: CellManifestSpec, *, snapshot_class: str) -> dict:
    """D3: a crash-consistent snapshot of the cell's own volume, named for its hold."""

    return {
        "apiVersion": "snapshot.storage.k8s.io/v1",
        "kind": "VolumeSnapshot",
        "metadata": {"name": _job_name_for_hold(spec, SNAPSHOT_NAME), "namespace": spec.namespace, "labels": _labels(spec)},
        "spec": {"volumeSnapshotClassName": snapshot_class, "source": {"persistentVolumeClaimName": spec.pvc_name}},
    }


def render_clone_claim(spec: CellManifestSpec, *, clone_class: str) -> dict:
    """D3: the claim the backup Job reads, cloned from this hold's snapshot.
    Its class binds at once, and TopoLVM pins the clone's PV to the source's
    node, so the Job lands there with no node selector of its own."""

    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": _job_name_for_hold(spec, CLONE_CLAIM_NAME), "namespace": spec.namespace, "labels": _labels(spec)},
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "storageClassName": clone_class,
            "resources": {"requests": {"storage": f"{spec.storage_gib}Gi"}},
            "dataSourceRef": {
                "apiGroup": "snapshot.storage.k8s.io",
                "kind": "VolumeSnapshot",
                "name": _job_name_for_hold(spec, SNAPSHOT_NAME),
            },
        },
    }


def _job_volumes(*, data_read_only: bool, pvc_name: str) -> list[dict]:
    return [
        {"name": "data", "persistentVolumeClaim": {"claimName": pvc_name, "readOnly": data_read_only}},
        {"name": "cache", "emptyDir": {}},
        {"name": "tmp", "emptyDir": {}},
    ]


def _job_volume_mounts(*, data_read_only: bool) -> list[dict]:
    return [
        {"name": "data", "mountPath": "/data", "readOnly": data_read_only},
        {"name": "cache", "mountPath": "/cache"},
        {"name": "tmp", "mountPath": "/tmp"},
    ]


def _job_pod_spec(
    spec: CellManifestSpec, *, job_kind: str, command: list[str], data_read_only: bool, repo: str,
    claim_name: str | None = None,
) -> dict:
    return {
        **_dedicated_placement(spec),
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "securityContext": _pod_security_context(),
        "containers": [
            {
                "name": job_kind,
                "image": spec.image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["sh", "-c", " && ".join(command)],
                "env": _restic_env(spec, repo),
                "resources": {
                    "requests": {"cpu": JOB_CPU_REQUEST, "memory": JOB_MEMORY_REQUEST},
                    "limits": {"cpu": JOB_CPU_LIMIT, "memory": "1Gi"},
                },
                "securityContext": {
                    "runAsNonRoot": True,
                    "runAsUser": RUNTIME_UID,
                    "runAsGroup": RUNTIME_GID,
                    **_container_security_context(),
                },
                "volumeMounts": _job_volume_mounts(data_read_only=data_read_only),
            }
        ],
        "volumes": _job_volumes(data_read_only=data_read_only, pvc_name=claim_name or spec.pvc_name),
    }


def render_backup_job(
    spec: CellManifestSpec,
    *,
    bucket_name: str,
    endpoint: str,
    claim_name: str | None = None,
    retention: list[str] | None = RETENTION_ARGS,
) -> dict:
    """`claim_name` is the hourly hold's clone; by default the Job reads the
    stopped cell's own claim. `retention` None skips `forget --prune`, the
    expensive call that needs restic's exclusive lock (D3)."""

    job_name = _job_name_for_hold(spec, BACKUP_JOB_NAME)
    repo = _restic_repo(spec, bucket_name, endpoint)
    # D8 amendment: the Job has no ServiceAccount token and cannot call back
    # to the API to report the snapshot id it produced, so it writes the id
    # restic's own `--json` summary line reports to its termination message
    # (the default /dev/termination-log path, writable even under
    # readOnlyRootFilesystem), and cellctl reads it from
    # containerStatuses[].state.terminated.message (k8s_client.py). This is
    # a real snapshot id, never the literal string "latest".
    #
    # M1/M2: `SNAPSHOT_ID=$(... | grep | tail | cut)` takes its exit status
    # from `cut`, so a failed `restic backup` still let the Job exit 0 with
    # an empty termination message -- decide() then recorded a "successful"
    # backup with no real snapshot. The backup now fails the shell (and the
    # Job) directly on either a non-zero `restic backup` or an id that does
    # not match a real 64-hex-character snapshot id.
    commands = [
        # D5: no backup of a source that holds no vault (backup_source.py).
        # The Job fails, the hold records BACKUP_FAILED, and cellctl logs the
        # Job's code. An emptied volume's cell then leaves serving, and its
        # row shows cell-init's refusal.
        VAULT_CHECK_COMMAND,
        "restic snapshots || restic init",
        f"restic backup --json {' '.join(BACKUP_PATHS)} > /tmp/backup.json || exit 1",
        (
            "SNAPSHOT_ID=$(grep -o '\"snapshot_id\":\"[a-f0-9]\\{64\\}\"' /tmp/backup.json "
            "| tail -n 1 | cut -d'\"' -f4)"
        ),
        '[ -n "$SNAPSHOT_ID" ] || exit 1',
        *([f"restic forget {' '.join(retention)} --prune"] if retention is not None else []),
        "printf '%s' \"$SNAPSHOT_ID\" > /dev/termination-log",
        # D10: "<id> <used bytes> <total bytes>". A failed measurement leaves
        # the id alone and the Job successful: the backup itself succeeded.
        f"{{ {FILESYSTEM_USE_COMMAND} >> /dev/termination-log || true; }}",
    ]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": spec.namespace,
            "labels": {**_labels(spec), JOB_KIND_LABEL: JOB_KIND_BACKUP},
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 900,  # D6/D8's 15-minute backup deadline
            "ttlSecondsAfterFinished": 300,
            "template": {
                "metadata": {"labels": {**_selector_labels(spec), JOB_KIND_LABEL: JOB_KIND_BACKUP}},
                "spec": _job_pod_spec(
                    spec, job_kind=JOB_KIND_BACKUP, command=commands, data_read_only=True, repo=repo,
                    claim_name=claim_name,
                ),
            },
        },
    }


def render_restore_job(
    spec: CellManifestSpec,
    *,
    bucket_name: str,
    endpoint: str,
    snapshot_id: str,
) -> dict:
    if not SNAPSHOT_ID_RE.fullmatch(snapshot_id):
        # SR-L12: validated before it ever reaches the shell string below,
        # not merely by the Job's own arguments -- a tenant file name can
        # never forge this, since it never becomes the snapshot id, but a
        # caller bug must not get the chance to build an injectable command.
        raise ValueError(f"not a restic snapshot id: {snapshot_id!r}")
    job_name = _job_name_for_hold(spec, RESTORE_JOB_NAME)
    repo = _restic_repo(spec, bucket_name, endpoint)
    # restic (0.17+) rejects `--delete` without a scoping filter -- "must be
    # combined with an include or exclude filter" -- since without one it
    # would consider deleting anything under the target root. The filter is
    # exactly the backup's own paths, not /data: `--include /data` let
    # `--delete` reach the root-owned lost+found, which restic running as
    # UID 10001 cannot remove (exit 1, measured with restic 0.19.1).
    includes = " ".join(f"--include {path}" for path in BACKUP_PATHS)
    commands = [f"restic restore {snapshot_id} --target / --delete {includes}"]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": spec.namespace,
            "labels": {**_labels(spec), JOB_KIND_LABEL: JOB_KIND_RESTORE},
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 900,
            "ttlSecondsAfterFinished": 300,
            "template": {
                "metadata": {"labels": {**_selector_labels(spec), JOB_KIND_LABEL: JOB_KIND_RESTORE}},
                "spec": _job_pod_spec(
                    spec, job_kind=JOB_KIND_RESTORE, command=commands, data_read_only=False, repo=repo
                ),
            },
        },
    }
