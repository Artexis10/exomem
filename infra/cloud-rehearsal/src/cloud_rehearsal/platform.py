"""The platform chart's Exomem Cloud entries, rendered and applied.

`helm template` runs from a pinned Helm image against a copy of
`infra/helm/platform` with its three subchart dependencies removed (the
rehearsal renders only `templates/cellctl.yaml`, `cloud-gateway.yaml` and
`cloud-storage-class.yaml`, none of which reads a subchart). Everything the
chart renders is applied as rendered, except the overlays below. Each
overlay is returned so the report lists it:

- the StorageClass keeps its name and policy but uses K3s's local-path
  provisioner, since there is no Hetzner CSI driver here;
- the cellctl container runs `rehearsal_cellctl` (build.py), the real loop
  with B2 key management and Hetzner doubles, and gets the S3 double's
  credential.

The gateway Deployment is applied exactly as rendered. If it omits any
variable the pinned Substrate gateway requires at startup, the platform stage
fails naming them: the chart is under test and is not patched.

The chart's Traefik and cert-manager dependencies are replaced by one
Traefik stand-in in the edge namespace `exomem-edge`, carrying the
`exomem.io/ingress: traefik` label the gateway NetworkPolicy admits from that
namespace only, terminating TLS for the MCP hostname with the run's CA on
NodePort 30443.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import re
import secrets
import shutil
from dataclasses import dataclass, field
from typing import Any

import yaml

from . import images, tls
from .infra import BACKUP_BUCKET, K3S_POD_CIDR, K3S_SERVICE_CIDR, S3_PORT, Stack
from .shell import run, wait_for

PLATFORM_CHART = images.REPO_ROOT / "infra/helm/platform"
CLOUD_NAMESPACE = "exomem-cloud"
PLATFORM_NAMESPACE = "exomem-platform"
# The chart's Traefik namespaceOverride: the gateway admits ingress only from here.
EDGE_NAMESPACE = "exomem-edge"
INGRESS_NODE_PORT = 30443
TRUSTED_INGRESS_HEADER = "x-exomem-ingress-source"
# These workload identities and the snapshot API are fixed by the platform chart.
LOCAL_STORAGE_CONTROLLERS = ("exomem-platform-topolvm-controller", "snapshot-controller")
SNAPSHOT_CLASS_API = "snapshot.storage.k8s.io/v1/VolumeSnapshotClass"


@dataclass
class PlatformConfig:
    cellctl_image: str
    gateway_image: str
    cell_repository: str
    backup_window: str
    public_base_url: str
    mcp_path: str
    trusted_ingress_source_value: str
    attachments_limit_fallback: int = 20
    local_storage_coexistence: bool = False


@dataclass
class Platform:
    rendered: list[dict[str, Any]]
    hetzner_class: str
    overlays: list[str] = field(default_factory=list)
    storage_evidence: dict[str, Any] = field(default_factory=dict)


def cellctl_values(stack: Stack, *, image: str, cell_repository: str) -> dict[str, Any]:
    """The chart's `cellctl` values for a rehearsal stack: its image, the S3
    double as B2, and the control database's one address."""

    return {
        "enabled": True,
        "image": image,
        "cellImageRepository": cell_repository,
        "b2BucketName": BACKUP_BUCKET,
        "b2BucketId": "rehearsal-bucket-id",
        "b2AccountId": "rehearsal-account-id",
        "b2Endpoint": stack.object_store.endpoint(from_host=False),
        "databaseEgressCidrs": [f"{stack.postgres.ip}/32"],
    }


def _render(stack: Stack, config: PlatformConfig) -> Platform:
    pg_cidr = f"{stack.postgres.ip}/32"
    values = {
        "cellctl": cellctl_values(stack, image=config.cellctl_image, cell_repository=config.cell_repository),
        "capacity": {"attachmentsLimitFallback": config.attachments_limit_fallback},
        "cells": {
            "backupWindow": config.backup_window,
            # The backup Jobs' 443 egress must reach the S3 double on the
            # Docker network, so only the cluster's own ranges are excepted.
            "jobEgressExcept": [K3S_POD_CIDR, K3S_SERVICE_CIDR],
        },
        "cloudGateway": {
            "enabled": True,
            "image": config.gateway_image,
            "hostname": tls.MCP_HOST,
            "publicBaseUrl": config.public_base_url,
            "mcpPath": config.mcp_path,
            "trustedIngressSourceHeader": TRUSTED_INGRESS_HEADER,
            "trustedIngressSourceValue": config.trusted_ingress_source_value,
            "databaseEgressCidrs": [pg_cidr],
        },
        "cloudIngress": {"enabled": False},
        "cert-manager": {"enabled": False},
        # Both P3 configurations keep existing cells on the Hetzner domain.
        "cellStorage": {"domain": "hetzner", "local": {"enabled": config.local_storage_coexistence}},
    }
    show_only = (
        "templates/cellctl.yaml", "templates/cloud-gateway.yaml", "templates/cloud-storage-class.yaml",
        "templates/namespaces.yaml",
    )
    rendered = render_chart(
        stack, values,
        show_only=() if config.local_storage_coexistence else show_only,
        dependencies=config.local_storage_coexistence,
        api_versions=(SNAPSHOT_CLASS_API,) if config.local_storage_coexistence else (),
    )
    documents = [
        doc for source, doc in rendered
        if (source in show_only
            or (config.local_storage_coexistence
                and (source == "templates/cell-local-storage.yaml" or source.startswith("charts/topolvm/"))))
        and (source != "templates/namespaces.yaml" or doc["metadata"]["name"] == PLATFORM_NAMESPACE)
    ]
    hetzner = next(doc for source, doc in rendered if source == "templates/cloud-storage-class.yaml")
    return Platform(rendered=documents, hetzner_class=hetzner["metadata"]["name"])


def render_chart(
    stack: Stack,
    values: dict[str, Any],
    *,
    show_only: tuple[str, ...] = (),
    dependencies: bool = False,
    api_versions: tuple[str, ...] = (),
) -> list[tuple[str, dict[str, Any]]]:
    """`helm template` of the platform chart over values.validation.yaml and
    `values`, as (source template, document) pairs.

    Without `dependencies` the subcharts are dropped, which only templates
    that read no subchart tolerate. With them, `helm dependency build`
    fetches every subchart Chart.lock pins, as infra/scripts/validate.sh does.
    """

    chart = stack.workdir / "platform-chart"
    if chart.exists():
        shutil.rmtree(chart)
    shutil.copytree(PLATFORM_CHART, chart, ignore=shutil.ignore_patterns("charts", "*.tgz"))
    chart_yaml = yaml.safe_load((chart / "Chart.yaml").read_text(encoding="utf-8"))
    if not dependencies:
        chart_yaml.pop("dependencies", None)
        (chart / "Chart.yaml").write_text(yaml.safe_dump(chart_yaml, sort_keys=False), encoding="utf-8")
        (chart / "Chart.lock").unlink(missing_ok=True)
    (stack.workdir / "platform-values.yaml").write_text(yaml.safe_dump(values), encoding="utf-8")
    template = [
        "helm", "template", "exomem-platform", "/chart",
        "--namespace", PLATFORM_NAMESPACE,
        "--values", "/chart/values.validation.yaml",
        "--values", "/values.yaml",
        *(arg for path in show_only for arg in ("--show-only", path)),
        *(arg for version in api_versions for arg in ("--api-versions", version)),
    ]
    # Only the template goes to stdout; the fetches report on stderr.
    script = [
        *([f"helm repo add {dep['name']} {dep['repository']} >&2" for dep in chart_yaml.get("dependencies", [])]
          if dependencies else []),
        *(["helm dependency build /chart >&2"] if dependencies else []),
        " ".join(template),
    ]
    rendered = run(
        [
            # As this user, so the chart copy stays removable; Helm keeps its
            # repositories and cache in the container's /tmp.
            "docker", "run", "--rm", "--entrypoint", "sh", "--user", f"{os.getuid()}:{os.getgid()}", "--env", "HOME=/tmp",
            "--mount", f"type=bind,source={chart},target=/chart",
            "--mount", f"type=bind,source={stack.workdir / 'platform-values.yaml'},target=/values.yaml,readonly",
            images.HELM, "-ec", " && ".join(script),
        ],
        timeout=600,
    ).stdout
    return documents_by_source(rendered)


def documents_by_source(rendered: str) -> list[tuple[str, dict[str, Any]]]:
    """Splits `helm template` output into (source template, document) pairs.
    The source is the path after the chart name, e.g. `templates/cellctl.yaml`
    or `charts/topolvm/templates/node/daemonset.yaml`."""

    pairs = []
    for chunk in re.split(r"^---\s*$", rendered, flags=re.MULTILINE):
        source = re.search(r"^# Source: [^/]+/(.+)$", chunk, flags=re.MULTILINE)
        document = yaml.safe_load(chunk)
        if source and isinstance(document, dict):
            pairs.append((source.group(1).strip(), document))
    return pairs


def _find(documents: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return next(doc for doc in documents if doc["kind"] == kind and doc["metadata"]["name"] == name)


# What the pinned Substrate gateway requires at startup: REQUIRED_GATEWAY_ENV
# (validateGatewayEnvironment) and what loadExomemCloudConfig reads.
GATEWAY_REQUIRED_ENV = (
    "DATABASE_URL",
    "EXOMEM_PUBLIC_BASE_URL",
    "EXOMEM_CLOUD_MCP_URL",
    "EXOMEM_CLOUD_MCP_PATH",
    "EXOMEM_GATEWAY_TRUSTED_INGRESS_SOURCE_HEADER",
    "EXOMEM_GATEWAY_TRUSTED_INGRESS_SOURCE_VALUE",
    "EXOMEM_CELL_PROTOCOL_VERSION",
    "EXOMEM_GATEWAY_CONTROL_HOSTNAME",
    "EXOMEM_GATEWAY_INTERNAL_ORIGIN",
    "EXOMEM_CONTROL_PLANE_KEY",
    "EXOMEM_CLOUD_CELL_TOKEN_KEY",
)


def missing_gateway_env(container: dict[str, Any]) -> list[str]:
    present = {entry["name"] for entry in container.get("env", [])}
    return [name for name in GATEWAY_REQUIRED_ENV if name not in present]


def apply(stack: Stack, config: PlatformConfig, *, cellctl_secrets: dict[str, dict[str, str]],
          pki: tls.RehearsalPki, ingress_source_value: str, ingress_image: str) -> Platform:
    platform = _render(stack, config)
    documents = copy.deepcopy(platform.rendered)

    storage = _find(documents, "StorageClass", platform.hetzner_class)
    storage["provisioner"] = "rancher.io/local-path"
    storage.pop("parameters", None)
    platform.overlays.append(
        f"StorageClass {storage['metadata']['name']}: provisioner rancher.io/local-path (no Hetzner CSI); "
        f"reclaimPolicy {storage.get('reclaimPolicy')} and volumeBindingMode {storage.get('volumeBindingMode')} kept"
    )

    platform.overlays.append(overlay_cellctl(documents))

    gateway = _find(documents, "Deployment", "exomem-cloud-gateway")
    gateway_container = gateway["spec"]["template"]["spec"]["containers"][0]
    missing = missing_gateway_env(gateway_container)
    if missing:
        raise RuntimeError(
            "infra/helm/platform/templates/cloud-gateway.yaml renders a gateway without environment the pinned "
            f"Substrate gateway requires at startup: {', '.join(missing)}"
        )

    # Namespaces and the policy first, then Secrets, then workloads.
    order = {"Namespace": 0, "StorageClass": 1, "ServiceAccount": 2, "ClusterRole": 3, "ClusterRoleBinding": 4,
             "ValidatingAdmissionPolicy": 5, "ValidatingAdmissionPolicyBinding": 6, "NetworkPolicy": 7,
             "Service": 8, "Deployment": 9}
    documents.sort(key=lambda doc: order.get(doc["kind"], 50))
    crds = [doc for doc in documents if doc["kind"] == "CustomResourceDefinition"]
    if crds:
        _kubectl_apply(stack, crds)
        stack.k3s.kubectl("wait", "--for=condition=Established", "crd", "--all", "--timeout=120s")
    workloads = [doc for doc in documents if doc["kind"] in ("Deployment", "DaemonSet")]
    others = [doc for doc in documents if doc not in workloads and doc not in crds]
    _kubectl_apply(stack, others)

    _kubectl_apply(stack, cellctl_secret_documents(stack, cellctl_secrets))
    _apply_ingress(stack, pki, ingress_source_value, ingress_image)
    _kubectl_apply(stack, workloads)
    if config.local_storage_coexistence:
        for deployment in LOCAL_STORAGE_CONTROLLERS:
            wait_rollout(stack, PLATFORM_NAMESPACE, deployment, timeout=420)
        observed = json.loads(stack.k3s.kubectl("get", "deployment", "cellctl", "-n", CLOUD_NAMESPACE, "-o", "json").stdout)
        env = observed["spec"]["template"]["spec"]["containers"][0]["env"]
        storage_config = json.loads(next(entry["value"] for entry in env if entry["name"] == "CELLCTL_CELL_STORAGE"))
        if storage_config["domain"] != platform.hetzner_class or not storage_config.get("local"):
            raise RuntimeError("coexistence requires local storage enabled with the Hetzner domain selected")
        logical = json.loads(stack.k3s.kubectl("get", "logicalvolumes.topolvm.io", "-o", "json").stdout)
        daemonsets = json.loads(stack.k3s.kubectl("get", "daemonsets", "-n", PLATFORM_NAMESPACE, "-o", "json").stdout)
        platform.storage_evidence = {
            "cellctl_storage": storage_config,
            "logical_volume_api": logical["apiVersion"],
            "logical_volume_count": len(logical["items"]),
            "controllers": {
                name: json.loads(stack.k3s.kubectl("get", "deployment", name, "-n", PLATFORM_NAMESPACE, "-o", "json").stdout)["status"]
                for name in LOCAL_STORAGE_CONTROLLERS
            },
            "node_daemonsets": {
                doc["metadata"]["name"]: doc["status"] for doc in daemonsets["items"]
            },
            "boundary": "no storage-labelled agent or local pool; Hetzner CSI and object storage retain their P3 doubles",
        }
        platform.overlays.append(f"rendered with --api-versions {SNAPSHOT_CLASS_API}, as the existing local-storage drill")
    return platform


def overlay_cellctl(documents: list[dict[str, Any]]) -> str:
    """Runs the rendered cellctl Deployment as `rehearsal_cellctl` with the S3
    double's credential; returns the overlay's description for the report."""

    cellctl = _find(documents, "Deployment", "cellctl")
    container = cellctl["spec"]["template"]["spec"]["containers"][0]
    container["command"] = ["python3", "-m", "rehearsal_cellctl"]
    container.setdefault("env", []).extend(
        [
            {"name": "REHEARSAL_S3_ACCESS_KEY", "valueFrom": {"secretKeyRef": {"name": "rehearsal-s3", "key": "accessKey"}}},
            {"name": "REHEARSAL_S3_SECRET_KEY", "valueFrom": {"secretKeyRef": {"name": "rehearsal-s3", "key": "secretKey"}}},
        ]
    )
    return (
        "cellctl Deployment: command `python3 -m rehearsal_cellctl` (real run_loop; B2 key management and "
        "Hetzner volume listing doubled) and the S3 double's credential from Secret rehearsal-s3"
    )


def cellctl_secrets(stack: Stack, *, cell_token_key: bytes, control_plane_key: str) -> dict[str, dict[str, str]]:
    """The Secrets the chart's cellctl and gateway read, by name."""

    return {
        "exomem-cellctl-database-dsn": {"dsn": stack.postgres.dsn("exomem_cellctl", from_host=False)},
        "exomem-cloud-gateway-database": {"url": stack.postgres.dsn("exomem_gateway", from_host=False)},
        # D7: 64 hex characters, read by both cellctl and the gateway.
        "exomem-cloud-cell-token-key": {"current": cell_token_key.hex(), "currentVersion": "1"},
        "exomem-cloud-gateway-control-plane-key": {"key": control_plane_key},
        "exomem-cloud-backup-master-key": {
            "keys": json.dumps({"1": base64.b64encode(secrets.token_bytes(32)).decode()}),
            "currentVersion": "1",
        },
        # Doubled in rehearsal_cellctl; present only because the chart requires them.
        "exomem-cloud-b2-key-management": {"keyId": "rehearsal-double", "applicationKey": "rehearsal-double"},
        "exomem-cloud-hetzner-read-token": {"token": "rehearsal-double"},
    }


def cellctl_secret_documents(stack: Stack, values: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    """`values` from cellctl_secrets, plus the S3 double's credential for rehearsal_cellctl."""

    store = stack.object_store
    return [
        _secret(CLOUD_NAMESPACE, "rehearsal-s3", {"accessKey": store.access_key, "secretKey": store.secret_key}),
        *(_secret(CLOUD_NAMESPACE, name, data) for name, data in values.items()),
    ]


def platform_env(platform: Platform, deployment: str) -> list[dict[str, Any]]:
    rendered = _find(platform.rendered, "Deployment", deployment)
    return rendered["spec"]["template"]["spec"]["containers"][0].get("env", [])


def _secret(namespace: str, name: str, data: dict[str, str]) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace},
        "type": "Opaque",
        "data": {key: base64.b64encode(value.encode()).decode() for key, value in data.items()},
    }


def _kubectl_apply(stack: Stack, documents: list[dict[str, Any]]) -> None:
    if not documents:
        return
    payload = "---\n".join(yaml.safe_dump(doc, sort_keys=False) for doc in documents)
    # --server-side: the same apply semantics cellctl itself uses.
    stack.k3s.kubectl("apply", "--server-side", "--field-manager=rehearsal", "--filename=-", input_text=payload)


def _apply_ingress(stack: Stack, pki: tls.RehearsalPki, ingress_source_value: str, image: str) -> None:
    leaf = pki.leaves[tls.MCP_HOST]
    dynamic = {
        "http": {
            "middlewares": {
                "trusted-ingress": {"headers": {"customRequestHeaders": {TRUSTED_INGRESS_HEADER: ingress_source_value}}}
            },
            "routers": {
                "mcp": {
                    "rule": f"Host(`{tls.MCP_HOST}`)",
                    "entryPoints": ["websecure"],
                    "middlewares": ["trusted-ingress"],
                    "service": "gateway",
                    "tls": {},
                }
            },
            "services": {
                "gateway": {
                    "loadBalancer": {
                        "servers": [{"url": f"http://exomem-cloud-gateway.{CLOUD_NAMESPACE}.svc.cluster.local:8080"}],
                        "passHostHeader": True,
                    }
                }
            },
        },
        "tls": {"certificates": [{"certFile": "/tls/tls.crt", "keyFile": "/tls/tls.key"}]},
    }
    labels = {"app.kubernetes.io/name": "rehearsal-traefik", "exomem.io/ingress": "traefik"}
    documents = [
        {
            "apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": EDGE_NAMESPACE, "labels": {"pod-security.kubernetes.io/enforce": "restricted"}},
        },
        {
            "apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/tls",
            "metadata": {"name": "mcp-tls", "namespace": EDGE_NAMESPACE},
            "data": {
                "tls.crt": base64.b64encode(leaf.cert_pem.encode()).decode(),
                "tls.key": base64.b64encode(leaf.key_pem.encode()).decode(),
            },
        },
        {
            # A Secret, not a ConfigMap: the routing carries the trusted
            # ingress header's value.
            "apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": "rehearsal-traefik", "namespace": EDGE_NAMESPACE},
            "stringData": {"dynamic.json": json.dumps(dynamic)},
        },
        {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "rehearsal-traefik", "namespace": EDGE_NAMESPACE, "labels": labels},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "automountServiceAccountToken": False,
                        "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "runAsGroup": 65532,
                                            "seccompProfile": {"type": "RuntimeDefault"}},
                        "containers": [
                            {
                                "name": "traefik",
                                "image": image,
                                "args": [
                                    "--entryPoints.websecure.address=:8443",
                                    "--providers.file.filename=/config/dynamic.json",
                                    "--log.level=WARN",
                                    # No call home: nothing in the run may reach a real server.
                                    "--global.checkNewVersion=false",
                                    "--global.sendAnonymousUsage=false",
                                ],
                                "ports": [{"name": "websecure", "containerPort": 8443}],
                                "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                                    "capabilities": {"drop": ["ALL"]}},
                                "volumeMounts": [
                                    {"name": "config", "mountPath": "/config"},
                                    {"name": "tls", "mountPath": "/tls"},
                                ],
                            }
                        ],
                        "volumes": [
                            {"name": "config", "secret": {"secretName": "rehearsal-traefik"}},
                            {"name": "tls", "secret": {"secretName": "mcp-tls"}},
                        ],
                    },
                },
            },
        },
        {
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": "rehearsal-traefik", "namespace": EDGE_NAMESPACE},
            "spec": {
                "type": "NodePort",
                "externalTrafficPolicy": "Local",
                "selector": labels,
                "ports": [{"name": "websecure", "port": 443, "targetPort": 8443, "nodePort": INGRESS_NODE_PORT}],
            },
        },
    ]
    _kubectl_apply(stack, documents)


def wait_rollout(stack: Stack, namespace: str, deployment: str, *, timeout: int = 240) -> None:
    try:
        wait_for(
            lambda: stack.k3s.kubectl(
                "rollout", "status", f"deployment/{deployment}", "--namespace", namespace, "--timeout=5s", check=False
            ).returncode == 0,
            timeout=timeout, interval=3, description=f"deployment {namespace}/{deployment} to roll out",
        )
    except TimeoutError as error:
        raise TimeoutError(f"{error}\n{why_not_running(stack, namespace)}") from None


def why_not_running(stack: Stack, namespace: str) -> str:
    """Content-free cluster state for a failure message: pods, events, node conditions."""

    pods = stack.k3s.kubectl("get", "pods", "--namespace", namespace, "-o", "wide", check=False).stdout
    events = stack.k3s.kubectl(
        "get", "events", "--namespace", namespace, "--sort-by=.lastTimestamp",
        "-o", "custom-columns=REASON:.reason,OBJECT:.involvedObject.name,MESSAGE:.message", check=False,
    ).stdout.splitlines()[-15:]
    conditions = stack.k3s.kubectl(
        "get", "nodes", "-o", "jsonpath={range .items[*].status.conditions[*]}{.type}={.status} {end}", check=False
    ).stdout
    return f"pods:\n{pods}\nrecent events:\n" + "\n".join(events) + f"\nnode conditions: {conditions}"


def s3_port() -> int:
    return S3_PORT
