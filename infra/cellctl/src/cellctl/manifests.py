"""Renders the D5 per-cell Kubernetes manifests as plain dicts.

cellctl applies these with server-side apply (see reconcile.py). This module
has no I/O: every function is a pure render from a `CellManifestSpec` to one
or more manifest dicts, which is what makes task 3.2's tests exercisable
without a cluster.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field

# D4: part of every cell's render digest. Bump it whenever the manifests this
# module renders change, so a new cellctl release reaches cells that have
# already converged.
RENDER_VERSION = 1

NAMESPACE_PREFIX = "exo-cell-"
GATEWAY_NAMESPACE = "exomem-cloud"
GATEWAY_POD_LABEL = "app.kubernetes.io/name"
GATEWAY_POD_LABEL_VALUE = "exomem-cloud-gateway"
CELL_PORT = 8765
STORAGE_CLASS = "exomem-cloud-encrypted"
RUNTIME_UID = 10001
RUNTIME_GID = 10001
JOB_KIND_LABEL = "exomem.io/cell-job"
INIT_CONTAINER_NAME = "cell-init"
POD_SECURITY_VERSION = "v1.35"

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


@dataclass(frozen=True)
class ResourceSettings:
    cpu_request: str = "250m"
    cpu_limit: str = "2"
    memory_request: str = "1Gi"
    memory_limit: str = "1536Mi"


@dataclass(frozen=True)
class CellManifestSpec:
    """Everything the D5 renderers need for one cell, for one reconcile pass."""

    cell_id: str
    image: str
    replicas: int
    read_only: bool
    storage_gib: int = 10
    resources: ResourceSettings = field(default_factory=ResourceSettings)
    model_env: dict[str, str] = field(default_factory=dict)

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


def render_resource_quota(spec: CellManifestSpec) -> dict:
    r = spec.resources
    return {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": {"name": "cell-quota", "namespace": spec.namespace, "labels": _labels(spec)},
        "spec": {
            "hard": {
                "persistentvolumeclaims": "1",
                "requests.storage": f"{spec.storage_gib}Gi",
                # One serving pod, plus headroom for a backup/restore Job that
                # briefly overlaps it during a hold transition.
                "pods": "2",
                "requests.cpu": r.cpu_request,
                "requests.memory": r.memory_request,
                "limits.cpu": r.cpu_limit,
                "limits.memory": r.memory_limit,
            }
        },
    }


def render_network_policies(spec: CellManifestSpec) -> list[dict]:
    """Default deny, gateway-only runtime ingress, no runtime egress at all,
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
            # No egress rules at all: the runtime pod cannot reach anything,
            # not even DNS.
            "egress": [],
        },
    }
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
            "storageClassName": STORAGE_CLASS,
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
MODEL_ENV_FORBIDDEN_PREFIXES = ("XDG_", "EXOMEM_CLOUD_")


def check_model_env(model_env: dict[str, str]) -> None:
    """Raises ValueError naming the first model_env key cellctl refuses.
    Checked when cellctl loads its settings, and again on every render."""

    for key in sorted(model_env):
        if key in MODEL_ENV_FORBIDDEN_KEYS or key.startswith(MODEL_ENV_FORBIDDEN_PREFIXES):
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
        env.append(
            {
                "name": "EXOMEM_CLOUD_CELL_TOKEN_PREVIOUS",
                "valueFrom": {
                    "secretKeyRef": {"name": spec.secret_name, "key": "cell-token-previous"}
                },
            }
        )
    if spec.read_only:
        env.append({"name": "EXOMEM_CLOUD_READ_ONLY", "value": "1"})
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
                            "env": [
                                # Cloud mode makes cell-init create the /data/host
                                # custody root and redact its own output.
                                {"name": "EXOMEM_CLOUD_CELL", "value": "1"},
                                {"name": "EXOMEM_CLOUD_CELL_ID", "value": spec.cell_id},
                                {"name": "TMPDIR", "value": "/tmp"},
                            ],
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
RETENTION_ARGS = ["--keep-daily", "7", "--keep-weekly", "4"]
# D8: what a backup covers and a restore rewrites. The volume root, including
# lost+found, is never in scope.
BACKUP_PATHS = ("/data/vault", "/data/host")
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


def _restic_env(spec: CellManifestSpec) -> list[dict]:
    return [
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


def _job_pod_spec(spec: CellManifestSpec, *, job_kind: str, command: list[str], data_read_only: bool) -> dict:
    return {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "securityContext": _pod_security_context(),
        "containers": [
            {
                "name": job_kind,
                "image": spec.image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["sh", "-c", " && ".join(command)],
                "env": _restic_env(spec),
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
                "volumeMounts": _job_volume_mounts(data_read_only=data_read_only),
            }
        ],
        "volumes": _job_volumes(data_read_only=data_read_only, pvc_name=spec.pvc_name),
    }


def render_backup_job(spec: CellManifestSpec, *, bucket_name: str, endpoint: str) -> dict:
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
        f"restic -r {repo} snapshots || restic -r {repo} init",
        f"restic -r {repo} backup --json {' '.join(BACKUP_PATHS)} > /tmp/backup.json || exit 1",
        (
            "SNAPSHOT_ID=$(grep -o '\"snapshot_id\":\"[a-f0-9]\\{64\\}\"' /tmp/backup.json "
            "| tail -n 1 | cut -d'\"' -f4)"
        ),
        '[ -n "$SNAPSHOT_ID" ] || exit 1',
        f"restic -r {repo} forget {' '.join(RETENTION_ARGS)} --prune",
        "printf '%s' \"$SNAPSHOT_ID\" > /dev/termination-log",
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
                    spec, job_kind=JOB_KIND_BACKUP, command=commands, data_read_only=True
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
    commands = [f"restic -r {repo} restore {snapshot_id} --target / --delete {includes}"]
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
                    spec, job_kind=JOB_KIND_RESTORE, command=commands, data_read_only=False
                ),
            },
        },
    }
