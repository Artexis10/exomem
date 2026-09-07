from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from test_hosted_helm_contract import PLATFORM, _find, _render, _render_process

ROOT = Path(__file__).resolve().parents[1]
GATEWAY_ARGS = (
    "--set", "gateway.enabled=true",
    "--set", "gateway.image=ghcr.io/substrate-systems/substrate-gateway@sha256:" + "a" * 64,
    "--set", "gateway.originHostname=mcp-origin.example.test",
    "--set", "gateway.databaseEgressCidrs[0]=203.0.113.10/32",
)


@pytest.fixture(scope="module")
def documents() -> list[dict]:
    return _render(
        PLATFORM, PLATFORM / "values.validation.yaml",
        namespace="exomem-platform", extra_args=GATEWAY_ARGS,
    )


def test_gateway_is_bounded_and_has_only_two_secret_references(documents: list[dict]) -> None:
    deployment = _find(documents, "Deployment", "exomem-gateway")
    pod = deployment["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["terminationGracePeriodSeconds"] == 30
    assert deployment["spec"]["replicas"] == 1
    container, = pod["containers"]
    assert "@sha256:" in container["image"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["resources"]["limits"]["memory"] == "512Mi"
    env = {item["name"]: item for item in container["env"]}
    secrets = {name for name, item in env.items() if "valueFrom" in item}
    assert secrets == {"DATABASE_URL", "EXOMEM_CONTROL_PLANE_KEY"}
    assert "envFrom" not in container
    assert env["EXOMEM_GATEWAY_MAX_INFLIGHT"]["value"] == "16"
    values = yaml.safe_load((PLATFORM / "values.validation.yaml").read_text())
    lock = json.loads(values["provisioner"]["deploymentLockJson"])
    assert env["EXOMEM_CELL_PROTOCOL_VERSION"]["value"] == lock["runtimeTarget"]["protocolVersion"]
    assert env["EXOMEM_GATEWAY_INTERNAL_ORIGIN"]["value"] == (
        "http://exomem-platform-traefik.exomem-platform.svc.cluster.local:80"
    )
    assert env["EXOMEM_PUBLIC_BASE_URL"]["value"] == "https://substratesystems.io"
    assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"
    assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"
    account = _find(documents, "ServiceAccount", "exomem-gateway")
    assert account["automountServiceAccountToken"] is False
    assert not any(
        doc.get("kind") in {"RoleBinding", "ClusterRoleBinding"}
        and any(subject.get("name") == "exomem-gateway" for subject in doc.get("subjects", []))
        for doc in documents
    )


def test_gateway_ingress_is_exact_path_with_overwritten_aggregate_source(documents: list[dict]) -> None:
    route = _find(documents, "IngressRoute", "exomem-gateway")
    assert route["spec"]["entryPoints"] == ["web"]
    rule, = route["spec"]["routes"]
    assert rule["match"] == (
        "Host(`mcp-origin.example.test`) && Path(`/api/exomem/mcp/v1`)"
    )
    assert rule["services"] == [{"name": "exomem-gateway", "port": 8080}]
    headers = _find(documents, "Middleware", "exomem-gateway-boundary")["spec"]["headers"]
    assert headers["customRequestHeaders"] == {
        "X-Exomem-Gateway-Ingress": "shared-edge",
        "X-Forwarded-For": "",
        "X-Real-Ip": "",
    }
    assert headers["customResponseHeaders"]["X-Vercel-Enable-Rewrite-Caching"] == "0"
    assert "no-store" in headers["customResponseHeaders"]["Cache-Control"]


def test_gateway_network_policy_limits_ingress_and_egress(documents: list[dict]) -> None:
    policy = _find(documents, "NetworkPolicy", "exomem-gateway")["spec"]
    assert policy["policyTypes"] == ["Ingress", "Egress"]
    ingress, = policy["ingress"]
    peer, = ingress["from"]
    assert peer["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": "exomem-platform"
    }
    assert peer["podSelector"]["matchLabels"] == {"exomem.io/ingress": "traefik"}
    assert ingress["ports"] == [{"port": 8080, "protocol": "TCP"}]
    traefik, dns, database = policy["egress"]
    assert traefik["to"] == [peer]
    assert traefik["ports"] == [{"port": 8000, "protocol": "TCP"}]
    assert dns["to"][0]["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": "kube-system"
    }
    assert dns["to"][0]["podSelector"]["matchLabels"] == {"k8s-app": "kube-dns"}
    assert database["to"] == [{"ipBlock": {"cidr": "203.0.113.10/32"}}]
    assert {port["port"] for port in database["ports"]} == {443, 5432}


@pytest.mark.parametrize("override", [
    "gateway.image=ghcr.io/substrate-systems/substrate-gateway:latest",
    "gateway.image=ghcr.io/artexis10/substrate-gateway@sha256:" + "a" * 64,
    "gateway.originHostname=control.example.test",
    "gateway.databaseEgressCidrs[0]=0.0.0.0/0",
])
def test_gateway_refuses_unsafe_enabled_configuration(override: str) -> None:
    result = _render_process(
        PLATFORM, PLATFORM / "values.validation.yaml", namespace="exomem-platform",
        extra_args=(*GATEWAY_ARGS, "--set", override),
    )
    assert result.returncode != 0


def test_gateway_is_an_explicit_disabled_cutover() -> None:
    schema = json.loads((PLATFORM / "values.schema.json").read_text())
    assert schema["properties"]["gateway"]["properties"]["enabled"]["type"] == "boolean"
    documents = _render(PLATFORM, PLATFORM / "values.validation.yaml", namespace="exomem-platform")
    assert not any(doc.get("metadata", {}).get("name") == "exomem-gateway" for doc in documents)
