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
  credential;
- anything `gateway_env_overlay` adds, which exists only when the chart's
  gateway environment does not satisfy the Substrate gateway it deploys.

The chart's Traefik and cert-manager dependencies are replaced by one
Traefik stand-in in `exomem-platform`, carrying the `exomem.io/ingress:
traefik` label the gateway NetworkPolicy admits, terminating TLS for the MCP
hostname with the run's CA on NodePort 30443.
"""

from __future__ import annotations

import base64
import copy
import json
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
INGRESS_NODE_PORT = 30443
TRUSTED_INGRESS_HEADER = "x-exomem-ingress-source"


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


@dataclass
class Platform:
    rendered: list[dict[str, Any]]
    overlays: list[str] = field(default_factory=list)
    chart_defects: list[dict[str, Any]] = field(default_factory=list)


def _render(stack: Stack, config: PlatformConfig) -> list[dict[str, Any]]:
    chart = stack.workdir / "platform-chart"
    if chart.exists():
        shutil.rmtree(chart)
    shutil.copytree(PLATFORM_CHART, chart, ignore=shutil.ignore_patterns("charts", "*.tgz"))
    chart_yaml = yaml.safe_load((chart / "Chart.yaml").read_text(encoding="utf-8"))
    chart_yaml.pop("dependencies", None)
    (chart / "Chart.yaml").write_text(yaml.safe_dump(chart_yaml, sort_keys=False), encoding="utf-8")
    (chart / "Chart.lock").unlink(missing_ok=True)
    pg_cidr = f"{stack.postgres.ip}/32"
    values = {
        "cellctl": {
            "enabled": True,
            "image": config.cellctl_image,
            "cellImageRepository": config.cell_repository,
            "b2BucketName": BACKUP_BUCKET,
            "b2BucketId": "rehearsal-bucket-id",
            "b2AccountId": "rehearsal-account-id",
            "b2Endpoint": stack.object_store.endpoint(from_host=False),
            "databaseEgressCidrs": [pg_cidr],
        },
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
    }
    (stack.workdir / "platform-values.yaml").write_text(yaml.safe_dump(values), encoding="utf-8")
    rendered = run(
        [
            "docker", "run", "--rm",
            "--mount", f"type=bind,source={chart},target=/chart,readonly",
            "--mount", f"type=bind,source={stack.workdir / 'platform-values.yaml'},target=/values.yaml,readonly",
            images.HELM, "template", "exomem-platform", "/chart",
            "--namespace", PLATFORM_NAMESPACE,
            "--values", "/chart/values.validation.yaml",
            "--values", "/values.yaml",
            "--show-only", "templates/cellctl.yaml",
            "--show-only", "templates/cloud-gateway.yaml",
            "--show-only", "templates/cloud-storage-class.yaml",
        ]
    ).stdout
    return [doc for doc in yaml.safe_load_all(rendered) if isinstance(doc, dict)]


def _find(documents: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    return next(doc for doc in documents if doc["kind"] == kind and doc["metadata"]["name"] == name)


def gateway_env_overlay(container: dict[str, Any], required: dict[str, str]) -> list[str]:
    """Adds each variable the Substrate gateway requires that the chart omits."""

    present = {entry["name"] for entry in container.get("env", [])}
    added = []
    for name, value in required.items():
        if name not in present:
            container.setdefault("env", []).append({"name": name, "value": value})
            added.append(name)
    return added


def apply(stack: Stack, config: PlatformConfig, *, s3_access_key: str, s3_secret_key: str,
          gateway_env: dict[str, str], gateway_secret_env: dict[str, str], cellctl_secrets: dict[str, dict[str, str]],
          pki: tls.RehearsalPki, ingress_source_value: str, ingress_image: str) -> Platform:
    documents = _render(stack, config)
    platform = Platform(rendered=copy.deepcopy(documents))

    storage = next(doc for doc in documents if doc["kind"] == "StorageClass")
    storage["provisioner"] = "rancher.io/local-path"
    storage.pop("parameters", None)
    platform.overlays.append(
        f"StorageClass {storage['metadata']['name']}: provisioner rancher.io/local-path (no Hetzner CSI); "
        f"reclaimPolicy {storage.get('reclaimPolicy')} and volumeBindingMode {storage.get('volumeBindingMode')} kept"
    )

    cellctl = _find(documents, "Deployment", "cellctl")
    container = cellctl["spec"]["template"]["spec"]["containers"][0]
    container["command"] = ["python3", "-m", "rehearsal_cellctl"]
    container.setdefault("env", []).extend(
        [
            {"name": "REHEARSAL_S3_ACCESS_KEY", "valueFrom": {"secretKeyRef": {"name": "rehearsal-s3", "key": "accessKey"}}},
            {"name": "REHEARSAL_S3_SECRET_KEY", "valueFrom": {"secretKeyRef": {"name": "rehearsal-s3", "key": "secretKey"}}},
        ]
    )
    platform.overlays.append(
        "cellctl Deployment: command `python3 -m rehearsal_cellctl` (real run_loop; B2 key management and "
        "Hetzner volume listing doubled) and the S3 double's credential from Secret rehearsal-s3"
    )

    gateway = _find(documents, "Deployment", "exomem-cloud-gateway")
    gateway_container = gateway["spec"]["template"]["spec"]["containers"][0]
    added = gateway_env_overlay(gateway_container, gateway_env)
    for name in gateway_secret_env:
        if name not in {entry["name"] for entry in gateway_container["env"]}:
            gateway_container["env"].append(
                {"name": name, "valueFrom": {"secretKeyRef": {"name": "rehearsal-gateway-extra", "key": name}}}
            )
            added.append(name)
    if added:
        platform.chart_defects.append(
            {
                "component": "infra/helm/platform/templates/cloud-gateway.yaml",
                "summary": "the rendered gateway Deployment omits environment the pinned Substrate gateway "
                "requires at startup (validateGatewayEnvironment / loadExomemCloudConfig)",
                "missing": sorted(added),
                "rendered_env": sorted(entry["name"] for entry in platform_env(platform, "exomem-cloud-gateway")),
            }
        )
        platform.overlays.append(f"gateway Deployment: added {', '.join(sorted(added))} (chart defect, see report)")

    # Namespaces and the policy first, then Secrets, then workloads.
    order = {"Namespace": 0, "StorageClass": 1, "ServiceAccount": 2, "ClusterRole": 3, "ClusterRoleBinding": 4,
             "ValidatingAdmissionPolicy": 5, "ValidatingAdmissionPolicyBinding": 6, "NetworkPolicy": 7,
             "Service": 8, "Deployment": 9}
    documents.sort(key=lambda doc: order.get(doc["kind"], 50))
    workloads = [doc for doc in documents if doc["kind"] == "Deployment"]
    others = [doc for doc in documents if doc["kind"] != "Deployment"]
    _kubectl_apply(stack, others)

    secrets = [
        _secret(CLOUD_NAMESPACE, "rehearsal-s3", {"accessKey": s3_access_key, "secretKey": s3_secret_key}),
        _secret(CLOUD_NAMESPACE, "rehearsal-gateway-extra", gateway_secret_env),
        *(_secret(CLOUD_NAMESPACE, name, data) for name, data in cellctl_secrets.items()),
    ]
    _kubectl_apply(stack, secrets)
    _apply_ingress(stack, pki, ingress_source_value, ingress_image)
    _kubectl_apply(stack, workloads)
    return platform


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
            "metadata": {"name": PLATFORM_NAMESPACE, "labels": {"pod-security.kubernetes.io/enforce": "restricted"}},
        },
        {
            "apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/tls",
            "metadata": {"name": "mcp-tls", "namespace": PLATFORM_NAMESPACE},
            "data": {
                "tls.crt": base64.b64encode(leaf.cert_pem.encode()).decode(),
                "tls.key": base64.b64encode(leaf.key_pem.encode()).decode(),
            },
        },
        {
            # A Secret, not a ConfigMap: the routing carries the trusted
            # ingress header's value.
            "apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": "rehearsal-traefik", "namespace": PLATFORM_NAMESPACE},
            "stringData": {"dynamic.json": json.dumps(dynamic)},
        },
        {
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "rehearsal-traefik", "namespace": PLATFORM_NAMESPACE, "labels": labels},
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
            "metadata": {"name": "rehearsal-traefik", "namespace": PLATFORM_NAMESPACE},
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
