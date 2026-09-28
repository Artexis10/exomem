"""Template-only (no live cluster) coverage for the platform chart's edge
routing: `helm template` the chart and confirm the Cloud gateway's route
never strips X-Real-Ip.

D11's direct-TLS Cloud gateway keys its IP rate limit on X-Real-Ip, which
Traefik itself sets/overwrites per hop. The OLD shared-edge gateway
(templates/gateway.yaml)'s `exomem-gateway-boundary` Middleware deliberately
blanks both X-Forwarded-For and X-Real-Ip before proxying to
`exomem-gateway`, because that path sits behind a shared edge it does not
trust. The Cloud gateway's own IngressRoute (templates/cloud-ingress.yaml)
must never reference that Middleware, or any other one that does the same
thing, or its rate limiting would silently key on nothing useful.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

CELLCTL_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = CELLCTL_ROOT.parents[1]
PLATFORM_CHART = REPO_ROOT / "infra/helm/platform"

HELM = shutil.which("helm")

# harden-exomem-cloud-operator-access D1: Traefik and the public Cloud route
# live here, away from every namespace that holds a durable key.
EDGE_NAMESPACE = "exomem-edge"
TRAEFIK = "platform-header-test-traefik"


def _helm_template() -> list[dict[str, Any]]:
    result = subprocess.run(
        [
            HELM, "template", "platform-header-test", str(PLATFORM_CHART),
            "--namespace", "exomem-platform",
            "--values", str(PLATFORM_CHART / "values.validation.yaml"),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def _find(documents: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    for doc in documents:
        if doc.get("kind") == kind and doc.get("metadata", {}).get("name") == name:
            return doc
    raise AssertionError(f"no {kind} named {name!r} in rendered chart output")


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_cloud_gateway_route_does_not_strip_x_real_ip() -> None:
    documents = _helm_template()

    cloud_route = _find(documents, "IngressRoute", "exomem-cloud-gateway")
    middleware_names = {
        middleware["name"]
        for route in cloud_route["spec"]["routes"]
        for middleware in route.get("middlewares", [])
    }
    assert "exomem-gateway-boundary" not in middleware_names, (
        "the Cloud gateway's IngressRoute must not reuse the shared-edge "
        "boundary Middleware, which blanks X-Real-Ip"
    )

    # Belt and suspenders: no Middleware anywhere in the chart that the Cloud
    # route could plausibly reference may blank X-Real-Ip, whatever it is
    # named -- not just the one known-bad name above.
    for doc in documents:
        if doc.get("kind") != "Middleware":
            continue
        custom_headers = doc.get("spec", {}).get("headers", {}).get("customRequestHeaders", {})
        if doc["metadata"]["name"] in middleware_names:
            assert custom_headers.get("X-Real-Ip") != "", (
                f"Middleware {doc['metadata']['name']!r}, used by the Cloud gateway's route, "
                "blanks X-Real-Ip"
            )


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_cellctl_admission_policy_covers_subresources_with_a_lone_wildcard() -> None:
    """D4: the policy confining cellctl must match every resource and every
    subresource. Kubernetes rejects `"*/*"` listed alongside any other
    resource (`if '*/*' is present, must not specify other resources`),
    and `"*"` alone would miss subresources such as `statefulsets/scale`."""
    documents = _helm_template()

    policy = _find(documents, "ValidatingAdmissionPolicy", "exomem-cellctl-scope")
    rules = policy["spec"]["matchConstraints"]["resourceRules"]

    assert rules, "the cellctl admission policy matches nothing"
    for rule in rules:
        assert rule["resources"] == ["*/*"], rule


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_cellctl_admission_policy_pins_the_pod_security_version_cellctl_renders() -> None:
    """The policy's required enforce-version must be the one cellctl's own
    namespace renderer writes, or cellctl's first namespace create is refused
    (found live on K3s when the policy demanded `latest`)."""
    from cellctl.manifests import POD_SECURITY_VERSION

    documents = _helm_template()
    policy = _find(documents, "ValidatingAdmissionPolicy", "exomem-cellctl-scope")
    expressions = " ".join(v["expression"] for v in policy["spec"]["validations"])

    assert f"enforce-version'] == '{POD_SECURITY_VERSION}'" in expressions


def _helm_template_result(*extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            HELM, "template", "platform-header-test", str(PLATFORM_CHART),
            "--namespace", "exomem-platform",
            "--values", str(PLATFORM_CHART / "values.validation.yaml"),
            *extra,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


PRIVATE_DB_ARGS = (
    "--set-string", "cloudDatabase.hostname=db.example.test",
    "--set-string", "cloudDatabase.privateIp=10.0.1.5",
)


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_cloud_database_alias_is_absent_by_default() -> None:
    documents = _helm_template()
    for name in ("cellctl", "exomem-cloud-gateway"):
        deployment = _find(documents, "Deployment", name)
        assert "hostAliases" not in deployment["spec"]["template"]["spec"]


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_cloud_database_alias_is_only_on_the_two_cloud_control_deployments() -> None:
    result = _helm_template_result(
        *PRIVATE_DB_ARGS,
        "--set", "gateway.enabled=true",
        "--set-string", "gateway.image=ghcr.io/substrate-systems/substrate-gateway@sha256:" + "a" * 64,
        "--set-string", "gateway.originHostname=legacy.example.test",
        "--set-string", "gateway.databaseEgressCidrs[0]=10.0.1.5/32",
    )
    assert result.returncode == 0, result.stderr
    documents = [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]
    aliases = {
        doc["metadata"]["name"]: doc["spec"]["template"]["spec"]["hostAliases"]
        for doc in documents
        if doc.get("kind") == "Deployment" and "hostAliases" in doc["spec"]["template"]["spec"]
    }
    assert aliases == {
        "cellctl": [{"ip": "10.0.1.5", "hostnames": ["db.example.test"]}],
        "exomem-cloud-gateway": [{"ip": "10.0.1.5", "hostnames": ["db.example.test"]}],
    }
    assert "hostAliases" not in _find(documents, "Deployment", "exomem-gateway")["spec"]["template"]["spec"]


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_cloud_database_requires_both_hostname_and_private_ip() -> None:
    for setting in PRIVATE_DB_ARGS[1::2]:
        result = _helm_template_result("--set-string", setting)
        assert result.returncode != 0, setting
        assert "cloudDatabase" in result.stderr, result.stderr


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_cloud_database_rejects_malformed_hostname_and_private_ip() -> None:
    for setting in (
        "cloudDatabase.hostname=bad host",
        "cloudDatabase.hostname=db..example.test",
        "cloudDatabase.privateIp=999.0.0.1",
        "cloudDatabase.privateIp=10.0.1.5/32",
    ):
        result = _helm_template_result(*PRIVATE_DB_ARGS, "--set-string", setting)
        assert result.returncode != 0, setting
        assert "cloudDatabase" in result.stderr, result.stderr


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
@pytest.mark.parametrize("address", ["8.8.8.8", "127.0.0.1", "169.254.1.1", "224.0.0.1", "100.64.0.1", "172.32.0.1"])
def test_cloud_database_rejects_non_private_routes_even_with_matching_egress(address: str) -> None:
    result = _helm_template_result(
        *PRIVATE_DB_ARGS,
        "--set-string", f"cloudDatabase.privateIp={address}",
        "--set-string", f"cellctl.databaseEgressCidrs[0]={address}/32",
        "--set-string", f"cloudGateway.databaseEgressCidrs[0]={address}/32",
    )
    assert result.returncode != 0, address
    assert "cloudDatabase" in result.stderr, result.stderr


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_cloud_database_certificate_hostname_cannot_be_an_ip_literal() -> None:
    result = _helm_template_result(*PRIVATE_DB_ARGS, "--set-string", "cloudDatabase.hostname=10.0.1.5")
    assert result.returncode != 0
    assert "cloudDatabase" in result.stderr, result.stderr


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
@pytest.mark.parametrize("address", ["10.50.1.20", "172.16.0.1", "172.31.255.254", "192.168.1.5"])
def test_cloud_database_accepts_rfc1918_routes(address: str) -> None:
    result = _helm_template_result(
        *PRIVATE_DB_ARGS,
        "--set-string", f"cloudDatabase.privateIp={address}",
        "--set-string", f"cellctl.databaseEgressCidrs[0]={address}/32",
        "--set-string", f"cloudGateway.databaseEgressCidrs[0]={address}/32",
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_private_database_ip_must_match_each_enabled_workloads_egress_policy() -> None:
    for missing, allowed in (("cellctl", "cloudGateway"), ("cloudGateway", "cellctl")):
        result = _helm_template_result(
            "--set-string", "cloudDatabase.hostname=db.example.test",
            "--set-string", "cloudDatabase.privateIp=10.0.1.6",
            "--set-string", f"{allowed}.databaseEgressCidrs[0]=10.0.1.6/32",
        )
        assert result.returncode != 0, missing
        assert f"{missing}.databaseEgressCidrs" in result.stderr, result.stderr


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_private_database_egress_check_skips_a_disabled_workload() -> None:
    result = _helm_template_result(
        *PRIVATE_DB_ARGS,
        "--set", "cellctl.enabled=false",
        "--set-string", "cellctl.databaseEgressCidrs[0]=10.0.1.6/32",
    )
    assert result.returncode == 0, result.stderr
    documents = [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]
    assert not any(doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "cellctl" for doc in documents)
    gateway = _find(documents, "Deployment", "exomem-cloud-gateway")
    assert gateway["spec"]["template"]["spec"]["hostAliases"] == [
        {"ip": "10.0.1.5", "hostnames": ["db.example.test"]}
    ]


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_backup_window_schema_accepts_a_midnight_crossing_and_rejects_an_empty_window() -> None:
    # D8: a window may cross midnight, and the schema rejects an empty one.
    for window in ("2-5", "22-3", "02-05"):
        result = _helm_template_result("--set-string", f"cells.backupWindow={window}")
        assert result.returncode == 0, (window, result.stderr)
    for window in ("4-4", "02-2", "23-23", "0-00"):
        result = _helm_template_result("--set-string", f"cells.backupWindow={window}")
        assert result.returncode != 0, window
        assert "backupWindow" in result.stderr, (window, result.stderr)


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_cellctl_admission_policy_allowlists_projected_sources_and_denies_csi_volumes() -> None:
    # D4/NEW-1: a pod template that sets automountServiceAccountToken: false
    # can still mount a token through a projected source, and a denylist of
    # sources misses podCertificate and clusterTrustBundle. Projected
    # sources are therefore allowlisted, and CSI inline volumes denied.
    import re

    documents = _helm_template()
    policy = _find(documents, "ValidatingAdmissionPolicy", "exomem-cellctl-scope")
    rules = [v for v in policy["spec"]["validations"] if "projected" in v["expression"]]
    assert len(rules) == 1
    expression = rules[0]["expression"]
    assert "['statefulsets', 'jobs']" in expression
    assert "!has(v.csi)" in expression
    sources = expression.split("v.projected.sources.all(s,", 1)[1]
    assert set(re.findall(r"has\(s\.(\w+)\)", sources)) == {"configMap", "secret", "downwardAPI"}
    assert "!has(s." not in sources  # an allowlist, not a denylist of sources
    assert "projected volume sources" in rules[0]["message"]


def _ports(rules: list[dict[str, Any]]) -> set[int]:
    return {port["port"] for rule in rules for port in rule.get("ports", [])}


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_exomem_cloud_is_default_deny_and_each_workload_policy_allows_its_own_traffic() -> None:
    # D5: one podSelector {} policy denies all ingress and egress in
    # exomem-cloud; cellctl and the gateway each declare both directions.
    documents = _helm_template()
    policies = {
        doc["metadata"]["name"]: doc
        for doc in documents
        if doc.get("kind") == "NetworkPolicy" and doc["metadata"].get("namespace") == "exomem-cloud"
    }
    deny = policies["default-deny"]["spec"]
    assert deny == {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}

    gateway = policies["exomem-cloud-gateway"]["spec"]
    assert gateway["policyTypes"] == ["Ingress", "Egress"]
    assert gateway["ingress"][0]["from"][0]["podSelector"]["matchLabels"] == {"exomem.io/ingress": "traefik"}
    assert _ports(gateway["egress"]) == {8765, 5432, 53}

    cellctl = policies["cellctl"]["spec"]
    assert cellctl["policyTypes"] == ["Ingress", "Egress"]
    assert cellctl["ingress"] == []
    assert _ports(cellctl["egress"]) == {443, 6443, 5432, 53}


def _selects(selector: dict[str, Any], labels: dict[str, str]) -> bool:
    if any(labels.get(key) != value for key, value in (selector.get("matchLabels") or {}).items()):
        return False
    for expression in selector.get("matchExpressions") or []:
        key, operator = expression["key"], expression["operator"]
        if operator == "Exists" and key not in labels:
            return False
        if operator == "DoesNotExist" and key in labels:
            return False
        if operator == "In" and labels.get(key) not in expression["values"]:
            return False
        if operator == "NotIn" and labels.get(key) in expression["values"]:
            return False
    return True


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_the_gateway_egresses_on_8765_only_to_cell_pods_in_cell_namespaces() -> None:
    # D5/NEW-3: "The gateway may egress only to cell pods on 8765." The
    # selectors are checked against the labels cellctl actually renders.
    from cellctl.manifests import (
        CellManifestSpec,
        render_backup_job,
        render_namespace,
        render_statefulset,
    )

    documents = _helm_template()
    gateway = _find(documents, "NetworkPolicy", "exomem-cloud-gateway")["spec"]
    cell_rules = [rule for rule in gateway["egress"] if 8765 in _ports([rule])]
    assert len(cell_rules) == 1 and len(cell_rules[0]["to"]) == 1
    peer = cell_rules[0]["to"][0]

    spec = CellManifestSpec(
        cell_id="aaaaaaaaaaaaaaaa", image="registry.example/cell@sha256:" + "a" * 64, replicas=0, read_only=False,
        hold_kind="backup", hold_started_at="2026-01-01T02:00:00+00:00",
    )
    cell_namespace = render_namespace(spec)["metadata"]["labels"]
    cell_pod = render_statefulset(spec)["spec"]["template"]["metadata"]["labels"]
    job_pod = render_backup_job(spec, bucket_name="b", endpoint="https://s3.example")["spec"]["template"]["metadata"]["labels"]

    assert _selects(peer["namespaceSelector"], cell_namespace)
    assert not _selects(peer["namespaceSelector"], {"kubernetes.io/metadata.name": "exomem-platform"})
    assert _selects(peer["podSelector"], cell_pod)
    assert not _selects(peer["podSelector"], job_pod)
    assert not _selects(peer["podSelector"], {"app.kubernetes.io/name": "exomem-cloud-gateway"})


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_cellctl_admission_policy_pins_every_object_cellctl_writes_to_what_it_renders() -> None:
    # Security MED-1: the policy pins NetworkPolicies, the Secret, the PVC,
    # the quota and pod-template placement to cellctl's own renderer, and
    # cellctl deletes only namespaces and Jobs. Its literals must match the
    # renderer's constants, or every cell's apply would be refused.
    from cellctl.manifests import (
        CELL_PORT,
        GATEWAY_NAMESPACE,
        GATEWAY_POD_LABEL,
        GATEWAY_POD_LABEL_VALUE,
        JOB_KIND_LABEL,
        STORAGE_CLASS,
        CellManifestSpec,
    )

    documents = _helm_template()
    policy = _find(documents, "ValidatingAdmissionPolicy", "exomem-cellctl-scope")
    expressions = "\n".join(v["expression"] for v in policy["spec"]["validations"])
    spec = CellManifestSpec(cell_id="a" * 16, image="i", replicas=1, read_only=False)
    for literal in (
        f"'{spec.secret_name}'",
        f"'{spec.pvc_name}'",
        f"'{STORAGE_CLASS}'",
        f"'{JOB_KIND_LABEL}'",
        f"{{'kubernetes.io/metadata.name': '{GATEWAY_NAMESPACE}'}}",
        f"{{'{GATEWAY_POD_LABEL}': '{GATEWAY_POD_LABEL_VALUE}'}}",
        f"port == {CELL_PORT}",
        "'cell-quota'",
        "['default-deny', 'runtime-ingress', 'job-egress']",
        "request.resource.resource in ['namespaces', 'jobs']",
        "variables.target.type == 'Opaque'",
        "!has(variables.target.spec.dataSource)",
        "has(v.persistentVolumeClaim) || has(v.emptyDir)",
        "!has(variables.podSpec.nodeName)",
        "!has(variables.podSpec.runtimeClassName)",
    ):
        assert literal in expressions, literal
    values = yaml.safe_load((PLATFORM_CHART / "values.yaml").read_text(encoding="utf-8"))
    import json

    assert json.dumps(values["cells"]["jobEgressExcept"], separators=(",", ":")) in expressions



@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_cellctl_runs_a_pod_only_where_its_deny_all_default_deny_exists() -> None:
    # Security MEDIUM (recheck): the scope policy pins NetworkPolicy writes,
    # but a fresh cell namespace has none. The isolation policy takes the
    # namespace's default-deny as its param, found in the request's own
    # namespace, and denies when it is missing.
    from cellctl.reconcile import (
        DEFAULT_ISOLATION_BINDING_NAME,
        DEFAULT_ISOLATION_POLICY_NAME,
        ISOLATION_PARAM_NAME,
    )

    documents = _helm_template()
    policy = _find(documents, "ValidatingAdmissionPolicy", DEFAULT_ISOLATION_POLICY_NAME)
    assert policy["spec"]["failurePolicy"] == "Fail"
    assert policy["spec"]["paramKind"] == {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy"}
    (rule,) = policy["spec"]["matchConstraints"]["resourceRules"]
    assert set(rule["resources"]) == {"statefulsets", "jobs"}
    assert set(rule["operations"]) == {"CREATE", "UPDATE"}
    binding = _find(documents, "ValidatingAdmissionPolicyBinding", DEFAULT_ISOLATION_BINDING_NAME)
    assert binding["spec"]["policyName"] == DEFAULT_ISOLATION_POLICY_NAME
    assert binding["spec"]["validationActions"] == ["Deny"]
    assert binding["spec"]["paramRef"] == {"name": ISOLATION_PARAM_NAME, "parameterNotFoundAction": "Deny"}
    assert "matchResources" not in binding["spec"]
    # The param is resolved before matchConditions, so the policy itself is
    # scoped to cell namespaces; other controllers' Jobs never reach it.
    assert policy["spec"]["matchConstraints"]["namespaceSelector"] == {
        "matchExpressions": [{"key": "exomem.io/cloud-cell", "operator": "Exists"}]
    }
    # ...and the scope policy makes every namespace cellctl writes carry it.
    scope = _find(documents, "ValidatingAdmissionPolicy", "exomem-cellctl-scope")
    expressions = " ".join(v["expression"] for v in scope["spec"]["validations"])
    assert "variables.labels['exomem.io/cloud-cell'] == variables.namespaceName.substring(9)" in expressions
    # ...and runs a pod only in a namespace that carries it.
    assert (
        "namespaceObject.metadata.labels['exomem.io/cloud-cell'] == variables.namespaceName.substring(9)" in expressions
    )


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_the_cloudflare_tunnel_keeps_a_single_web_port_while_websecure_rides_hostport() -> None:
    documents = _helm_template()
    traefik = _find(documents, "Service", "platform-header-test-traefik")
    assert [port["name"] for port in traefik["spec"]["ports"]] == ["web"]
    deployment = _find(documents, "Deployment", "platform-header-test-traefik")
    ports = {port["name"]: port for port in deployment["spec"]["template"]["spec"]["containers"][0]["ports"]}
    assert ports["websecure"]["hostPort"] == 443
    assert {name for name, port in ports.items() if "hostPort" in port} == {"websecure"}
    args = deployment["spec"]["template"]["spec"]["containers"][0]["args"]
    assert not any(arg.lower().startswith("--entrypoints.websecure.forwardedheaders.trustedips") for arg in args)


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_cellctl_is_a_single_nonroot_read_only_recreate_deployment() -> None:
    deployment = _find(_helm_template(), "Deployment", "cellctl")
    spec = deployment["spec"]
    assert spec["replicas"] == 1
    assert spec["strategy"] == {"type": "Recreate"}
    pod = spec["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    (container,) = pod["containers"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_cloud_workload_images_and_cell_admission_are_digest_pinned() -> None:
    import json
    import re

    documents = _helm_template()
    values = yaml.safe_load((PLATFORM_CHART / "values.validation.yaml").read_text(encoding="utf-8"))
    for name, configured_image in (
        ("cellctl", values["cellctl"]["image"]),
        ("exomem-cloud-gateway", values["cloudGateway"]["image"]),
    ):
        deployment = _find(documents, "Deployment", name)
        (container,) = deployment["spec"]["template"]["spec"]["containers"]
        assert container["image"] == configured_image
        assert re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", container["image"])

    policy = _find(documents, "ValidatingAdmissionPolicy", "exomem-cellctl-scope")
    (image_rule,) = [v for v in policy["spec"]["validations"] if "digest-pinned" in v["message"]]
    repository = values["cellctl"]["cellImageRepository"]
    expression = image_rule["expression"]
    assert "containers.all(c, c.image.matches(" in expression
    assert "initContainers.all(c, c.image.matches(" in expression
    image_patterns = re.findall(r'c\.image\.matches\(("(?:\\.|[^"])*")\)', expression)
    assert len(image_patterns) == 2
    assert image_patterns[0] == image_patterns[1]
    pinned_cell_image = re.compile(json.loads(image_patterns[0]))
    assert pinned_cell_image.search(f"{repository}@sha256:{'a' * 64}")
    assert not pinned_cell_image.search(f"{repository}:latest")
    assert not pinned_cell_image.search(f"{repository}@sha256:{'a' * 63}")
    assert not pinned_cell_image.search(f"other/{repository}@sha256:{'a' * 64}")


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_cloud_storage_class_uses_the_encryption_secret_and_deletes_volumes() -> None:
    from cellctl.manifests import STORAGE_CLASS

    storage = _find(_helm_template(), "StorageClass", STORAGE_CLASS)
    assert storage["provisioner"] == "csi.hetzner.cloud"
    assert storage["reclaimPolicy"] == "Delete"
    assert storage["parameters"] == {
        "csi.storage.k8s.io/fstype": "ext4",
        "csi.storage.k8s.io/node-publish-secret-name": "exomem-cloud-volume-encryption",
        "csi.storage.k8s.io/node-publish-secret-namespace": "exomem-platform",
    }


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_cloud_gateway_certificate_uses_a_cluster_issuer_whose_token_stays_in_exomem_cloud() -> None:
    # harden-exomem-cloud-operator-access D3: the Certificate and its TLS
    # Secret live in the edge namespace, and name a ClusterIssuer whose
    # DNS-01 token cert-manager resolves in exomem-cloud, never beside Traefik.
    documents = _helm_template()
    values = yaml.safe_load((PLATFORM_CHART / "values.validation.yaml").read_text(encoding="utf-8"))
    hostname = values["cloudGateway"]["hostname"]
    assert hostname == values["cloudIngress"]["hostname"]
    assert hostname not in {
        values["provisioner"]["controlHostname"],
        values["provisioner"]["transferHostname"],
    }

    assert not [doc for doc in documents if doc.get("kind") == "Issuer"], "no namespaced Issuer may remain"
    issuer = _find(documents, "ClusterIssuer", "exomem-cloud-dns01")
    certificate = _find(documents, "Certificate", "exomem-cloud-gateway")
    assert "namespace" not in issuer["metadata"]
    assert certificate["metadata"]["namespace"] == EDGE_NAMESPACE
    assert issuer["spec"]["acme"]["solvers"] == [{
        "selector": {"dnsNames": [hostname]},
        "dns01": {"cloudflare": {"apiTokenSecretRef": {
            "name": "exomem-cloudflare-dns-token", "key": "token",
        }}},
    }]
    assert issuer["spec"]["acme"]["privateKeySecretRef"] == {"name": "exomem-cloud-acme-account-key"}
    # A ClusterIssuer's Secret references resolve in cert-manager's cluster
    # resource namespace, which must be the one the signed registry already
    # delivers exomem-cloudflare-dns-token to.
    controller = _find(documents, "Deployment", "platform-header-test-cert-manager")
    (container,) = controller["spec"]["template"]["spec"]["containers"]
    assert "--cluster-resource-namespace=exomem-cloud" in container["args"]
    assert certificate["spec"]["issuerRef"] == {"name": issuer["metadata"]["name"], "kind": "ClusterIssuer"}
    assert certificate["spec"]["dnsNames"] == [hostname]
    assert certificate["spec"]["secretName"] == "exomem-cloud-gateway-tls"
    route = _find(documents, "IngressRoute", "exomem-cloud-gateway")
    assert route["metadata"]["namespace"] == certificate["metadata"]["namespace"]
    # Only the Cloud MCP path and its protected-resource metadata are public.
    # The gateway also serves the hosted MCP path, which must not be reachable
    # through the Cloud hostname.
    assert route["spec"]["routes"][0]["match"] == (
        f"Host(`{hostname}`) && "
        "(Path(`/mcp`) || Path(`/.well-known/oauth-protected-resource/mcp`))"
    )
    assert route["spec"]["tls"]["secretName"] == certificate["spec"]["secretName"]


def test_traefik_rollout_can_replace_the_hostport_pod_on_one_node() -> None:
    # websecure binds hostPort 443 on the single server node, so a surge pod
    # could never schedule next to the old one and every rollout would stall.
    traefik = _find(_helm_template(), "Deployment", "platform-header-test-traefik")
    assert traefik["spec"]["strategy"] == {
        "type": "RollingUpdate",
        "rollingUpdate": {"maxUnavailable": 1, "maxSurge": 0},
    }


# The Substrate gateway's environment contract (substrate main,
# src/exomem-gateway/server.ts validateGatewayEnvironment and cloud-config.ts),
# plus DATABASE_URL for its own Postgres role and the port the chart probes.
GATEWAY_ENV = {
    "EXOMEM_CONTROL_PLANE_KEY",
    "EXOMEM_PUBLIC_BASE_URL",
    "EXOMEM_CELL_PROTOCOL_VERSION",
    "EXOMEM_GATEWAY_CONTROL_HOSTNAME",
    "EXOMEM_GATEWAY_INTERNAL_ORIGIN",
    "EXOMEM_GATEWAY_TRUSTED_INGRESS_SOURCE_HEADER",
    "EXOMEM_GATEWAY_TRUSTED_INGRESS_SOURCE_VALUE",
    "EXOMEM_CLOUD_MCP_URL",
    "EXOMEM_CLOUD_MCP_PATH",
    "EXOMEM_CLOUD_CELL_TOKEN_KEY",
    "DATABASE_URL",
    "EXOMEM_GATEWAY_PORT",
}


def _gateway_env(documents: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    deployment = _find(documents, "Deployment", "exomem-cloud-gateway")
    (container,) = deployment["spec"]["template"]["spec"]["containers"]
    env = container["env"]
    names = [entry["name"] for entry in env]
    assert len(names) == len(set(names)), names
    return {entry["name"]: entry for entry in env}


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_the_cloud_gateway_renders_exactly_the_substrate_gateway_env_contract() -> None:
    documents = _helm_template()
    env = _gateway_env(documents)
    assert set(env) == GATEWAY_ENV

    values = yaml.safe_load((PLATFORM_CHART / "values.validation.yaml").read_text(encoding="utf-8"))
    gateway = values["cloudGateway"]
    hostname = gateway["hostname"]
    assert env["EXOMEM_GATEWAY_CONTROL_HOSTNAME"]["value"] == hostname
    assert env["EXOMEM_CLOUD_MCP_PATH"]["value"] == "/mcp"
    assert env["EXOMEM_CLOUD_MCP_URL"]["value"] == f"https://{hostname}/mcp"
    assert env["EXOMEM_CELL_PROTOCOL_VERSION"]["value"] == "1"
    assert env["EXOMEM_PUBLIC_BASE_URL"]["value"] == gateway["publicBaseUrl"]
    assert env["EXOMEM_GATEWAY_INTERNAL_ORIGIN"]["value"] == (
        "http://exomem-cloud-gateway.exomem-cloud.svc.cluster.local:8080"
    )
    assert env["EXOMEM_GATEWAY_PORT"]["value"] == "8080"
    # Secrets come from Secrets. The cell token key is the same Secret entry
    # cellctl reads as its current key (64 hex characters).
    assert env["EXOMEM_CLOUD_CELL_TOKEN_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "exomem-cloud-cell-token-key",
        "key": "current",
    }
    cellctl = _find(documents, "Deployment", "cellctl")
    cellctl_env = {e["name"]: e for e in cellctl["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert (
        cellctl_env["CELLCTL_CELL_TOKEN_KEY_CURRENT"]["valueFrom"]["secretKeyRef"]
        == env["EXOMEM_CLOUD_CELL_TOKEN_KEY"]["valueFrom"]["secretKeyRef"]
    )
    assert "secretKeyRef" in env["EXOMEM_CONTROL_PLANE_KEY"]["valueFrom"]
    assert "secretKeyRef" in env["DATABASE_URL"]["valueFrom"]


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_the_cloud_ingress_sets_the_trusted_ingress_source_header_the_gateway_expects() -> None:
    # Without it the gateway skips its per-IP bucket. Traefik overwrites any
    # client-sent copy, and only Traefik pods reach the gateway.
    documents = _helm_template()
    env = _gateway_env(documents)
    header = env["EXOMEM_GATEWAY_TRUSTED_INGRESS_SOURCE_HEADER"]["value"]
    value = env["EXOMEM_GATEWAY_TRUSTED_INGRESS_SOURCE_VALUE"]["value"]
    assert header == "x-exomem-ingress-source"

    middleware = _find(documents, "Middleware", "exomem-cloud-trusted-ingress")
    assert middleware["metadata"]["namespace"] == EDGE_NAMESPACE
    assert middleware["spec"]["headers"]["customRequestHeaders"] == {header: value}
    route = _find(documents, "IngressRoute", "exomem-cloud-gateway")
    (rule,) = route["spec"]["routes"]
    assert rule["middlewares"] == [{"name": "exomem-cloud-trusted-ingress"}]


# --- harden-exomem-cloud-operator-access: edge isolation (D1-D3) ---


def _grants_secrets(rules: list[dict[str, Any]]) -> bool:
    for rule in rules:
        groups = rule.get("apiGroups") or []
        resources = rule.get("resources") or []
        if ("" in groups or "*" in groups) and ("secrets" in resources or "*" in resources):
            return True
    return False


def _traefik_secret_scopes(documents: list[dict[str, Any]]) -> set[str]:
    """Where Traefik's ServiceAccount may read Secrets: a namespace for each
    RoleBinding whose role grants them, "*" for each such ClusterRoleBinding."""

    account = _find(documents, "ServiceAccount", TRAEFIK)
    subject = ("ServiceAccount", account["metadata"]["name"], account["metadata"]["namespace"])
    scopes: set[str] = set()
    for binding in documents:
        if binding.get("kind") not in {"RoleBinding", "ClusterRoleBinding"}:
            continue
        subjects = {(s["kind"], s["name"], s.get("namespace")) for s in binding.get("subjects") or []}
        if subject not in subjects:
            continue
        ref = binding["roleRef"]
        role = next(
            doc for doc in documents
            if doc.get("kind") == ref["kind"] and doc["metadata"]["name"] == ref["name"]
            and (ref["kind"] == "ClusterRole" or doc["metadata"].get("namespace") == binding["metadata"]["namespace"])
        )
        if _grants_secrets(role.get("rules") or []):
            scopes.add("*" if binding["kind"] == "ClusterRoleBinding" else binding["metadata"]["namespace"])
    return scopes


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_traefik_deployment_service_and_role_render_in_the_edge_namespace() -> None:
    documents = _helm_template()
    for kind in ("Deployment", "Service", "ServiceAccount"):
        assert _find(documents, kind, TRAEFIK)["metadata"]["namespace"] == EDGE_NAMESPACE, kind
    # Namespaced RBAC renders a Role in every namespace Traefik watches; the
    # watch list must be the edge namespace alone.
    roles = [doc for doc in documents if doc.get("kind") == "Role" and doc["metadata"]["name"] == TRAEFIK]
    assert [role["metadata"]["namespace"] for role in roles] == [EDGE_NAMESPACE]
    bindings = [doc for doc in documents if doc.get("kind") == "RoleBinding" and doc["metadata"]["name"] == TRAEFIK]
    assert [binding["metadata"]["namespace"] for binding in bindings] == [EDGE_NAMESPACE]


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_no_traefik_cluster_role_grants_secrets() -> None:
    documents = _helm_template()
    traefik_cluster_roles = [
        doc for doc in documents
        if doc.get("kind") == "ClusterRole"
        and doc["metadata"].get("labels", {}).get("app.kubernetes.io/name") == "traefik"
    ]
    assert not [role["metadata"]["name"] for role in traefik_cluster_roles if _grants_secrets(role.get("rules") or [])]
    assert "*" not in _traefik_secret_scopes(documents)


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_the_only_role_granting_traefik_secrets_is_in_the_edge_namespace() -> None:
    assert _traefik_secret_scopes(_helm_template()) == {EDGE_NAMESPACE}


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_traefik_watches_only_the_edge_namespace_and_may_route_to_an_external_name() -> None:
    deployment = _find(_helm_template(), "Deployment", TRAEFIK)
    args = deployment["spec"]["template"]["spec"]["containers"][0]["args"]
    assert f"--providers.kubernetescrd.namespaces={EDGE_NAMESPACE}" in args
    assert "--providers.kubernetescrd.allowExternalNameServices=true" in args
    assert "--providers.kubernetescrd.disableClusterScopeResources=true" in args
    assert "--providers.kubernetescrd.allowCrossNamespace=true" not in args
    assert not any(arg.startswith("--providers.kubernetesingress") for arg in args)


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_the_edge_namespace_enforces_privileged_and_audits_restricted() -> None:
    # Restricted includes Baseline, which refuses any non-zero hostPort, so
    # enforcing it would refuse Traefik's hostPort 443 pod.
    namespace = _find(_helm_template(), "Namespace", EDGE_NAMESPACE)
    labels = namespace["metadata"]["labels"]
    assert {key: value for key, value in labels.items() if key.startswith("pod-security.kubernetes.io/")} == {
        "pod-security.kubernetes.io/enforce": "privileged",
        "pod-security.kubernetes.io/enforce-version": "v1.35",
        "pod-security.kubernetes.io/audit": "restricted",
        "pod-security.kubernetes.io/audit-version": "v1.35",
        "pod-security.kubernetes.io/warn": "restricted",
        "pod-security.kubernetes.io/warn-version": "v1.35",
    }


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_the_cloud_route_objects_render_in_the_edge_namespace() -> None:
    documents = _helm_template()
    for kind, name in (
        ("IngressRoute", "exomem-cloud-gateway"),
        ("Middleware", "exomem-cloud-trusted-ingress"),
        ("Certificate", "exomem-cloud-gateway"),
    ):
        assert _find(documents, kind, name)["metadata"]["namespace"] == EDGE_NAMESPACE, kind

    (external,) = [
        doc for doc in documents
        if doc.get("kind") == "Service" and doc["metadata"].get("namespace") == EDGE_NAMESPACE
        and doc["spec"].get("type") == "ExternalName"
    ]
    assert external["metadata"]["name"] == "exomem-cloud-gateway"
    assert external["spec"]["externalName"] == "exomem-cloud-gateway.exomem-cloud.svc.cluster.local"
    route = _find(documents, "IngressRoute", "exomem-cloud-gateway")
    (rule,) = route["spec"]["routes"]
    assert rule["services"] == [{"name": external["metadata"]["name"], "port": 8080}]

    # The edge namespace holds no Secret but the certificate cert-manager
    # writes there at runtime: the chart itself renders none, and no issuer.
    in_edge = [doc for doc in documents if doc.get("metadata", {}).get("namespace") == EDGE_NAMESPACE]
    assert not [doc for doc in in_edge if doc["kind"] in {"Secret", "Issuer"}]


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_the_gateway_admits_ingress_only_from_edge_traefik_pods() -> None:
    documents = _helm_template()
    gateway = _find(documents, "NetworkPolicy", "exomem-cloud-gateway")["spec"]
    assert gateway["ingress"] == [{
        "from": [{
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": EDGE_NAMESPACE}},
            "podSelector": {"matchLabels": {"exomem.io/ingress": "traefik"}},
        }],
        "ports": [{"port": 8080, "protocol": "TCP"}],
    }]
    traefik = _find(documents, "Deployment", TRAEFIK)
    assert traefik["metadata"]["namespace"] == EDGE_NAMESPACE
    assert traefik["spec"]["template"]["metadata"]["labels"]["exomem.io/ingress"] == "traefik"


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_only_the_edge_namespace_may_request_a_certificate_from_the_cloud_cluster_issuer() -> None:
    # D3: any namespace can name a ClusterIssuer, and cert-manager's edit
    # role aggregates into the built-in admin and edit roles, so admission
    # keeps the MCP hostname's certificate to the Cloud route alone.
    documents = _helm_template()
    policy = _find(documents, "ValidatingAdmissionPolicy", "exomem-cloud-issuer-scope")["spec"]
    assert policy["failurePolicy"] == "Fail"
    (rule,) = policy["matchConstraints"]["resourceRules"]
    assert rule["apiGroups"] == ["cert-manager.io"]
    assert set(rule["resources"]) == {"certificates", "certificaterequests"}
    assert set(rule["operations"]) == {"CREATE", "UPDATE"}
    text = " ".join(
        [v["expression"] for v in policy.get("variables", [])] + [v["expression"] for v in policy["validations"]]
    )
    issuer = _find(documents, "ClusterIssuer", "exomem-cloud-dns01")["metadata"]["name"]
    for literal in (f"'{issuer}'", "'ClusterIssuer'", f"request.namespace == '{EDGE_NAMESPACE}'"):
        assert literal in text, literal
    binding = _find(documents, "ValidatingAdmissionPolicyBinding", "exomem-cloud-issuer-scope")["spec"]
    assert binding == {"policyName": "exomem-cloud-issuer-scope", "validationActions": ["Deny"]}


# --- harden-exomem-cloud-operator-access: operator identities (D4) ---

OPERATOR_GROUP = "exomem:operators"
BREAK_GLASS_GROUP = "exomem:break-glass"
CONNECT_SUBRESOURCES = {"pods/exec", "pods/attach", "pods/portforward", "pods/proxy", "pods/ephemeralcontainers"}


def _group_bindings(documents: list[dict[str, Any]], group: str) -> list[dict[str, Any]]:
    return [
        doc for doc in documents
        if doc.get("kind") in {"RoleBinding", "ClusterRoleBinding"}
        and {"kind": "Group", "name": group, "apiGroup": "rbac.authorization.k8s.io"} in (doc.get("subjects") or [])
    ]


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_the_everyday_operator_reads_status_events_and_logs_but_no_secret_or_connect_subresource() -> None:
    documents = _helm_template()
    role = _find(documents, "ClusterRole", "exomem-operator-read")
    granted: dict[tuple[str, str], set[str]] = {}
    for rule in role["rules"]:
        assert "resourceNames" not in rule and "nonResourceURLs" not in rule, rule
        for group in rule["apiGroups"]:
            for resource in rule["resources"]:
                granted.setdefault((group, resource), set()).update(rule["verbs"])
    assert not [key for key in granted if "*" in key], granted
    assert {verb for verbs in granted.values() for verb in verbs} <= {"get", "list", "watch"}
    resources = {resource for _, resource in granted}
    assert "secrets" not in resources
    assert not resources & CONNECT_SUBRESOURCES
    assert set(granted) == {
        ("", "pods"), ("", "pods/log"), ("", "events"), ("", "namespaces"),
        ("", "persistentvolumeclaims"), ("", "services"), ("", "nodes"),
        ("apps", "deployments"), ("apps", "statefulsets"), ("batch", "jobs"),
    }

    (binding,) = _group_bindings(documents, OPERATOR_GROUP)
    assert binding["kind"] == "ClusterRoleBinding"
    assert binding["metadata"]["name"] == "exomem-operator-read"
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "exomem-operator-read",
    }
    assert binding["subjects"] == [{"kind": "Group", "name": OPERATOR_GROUP, "apiGroup": "rbac.authorization.k8s.io"}]


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_the_break_glass_group_is_bound_to_cluster_admin_and_nothing_else() -> None:
    # D4: no standing credential carries this group; a one-hour certificate
    # minted through an approved CSR does.
    (binding,) = _group_bindings(_helm_template(), BREAK_GLASS_GROUP)
    assert binding["kind"] == "ClusterRoleBinding"
    assert binding["metadata"]["name"] == "exomem-break-glass"
    assert binding["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "cluster-admin"}
    assert binding["subjects"] == [{"kind": "Group", "name": BREAK_GLASS_GROUP, "apiGroup": "rbac.authorization.k8s.io"}]


@pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")
def test_connect_subresources_in_cell_namespaces_are_admitted_only_for_break_glass() -> None:
    # D5, shipped after the live probe (test_k3s_integration.py) showed the
    # API server enforcing this policy type on CONNECT. It reads only user
    # info and the namespace, and applies to system:masters too.
    documents = _helm_template()
    policy = _find(documents, "ValidatingAdmissionPolicy", "exomem-cell-connect-guard")["spec"]
    assert policy["failurePolicy"] == "Fail"
    rules = {
        (tuple(rule["operations"]), tuple(sorted(rule["resources"])))
        for rule in policy["matchConstraints"]["resourceRules"]
        if rule["apiGroups"] == [""]
    }
    assert rules == {
        (("CONNECT",), ("pods/attach", "pods/exec", "pods/portforward")),
        (("UPDATE",), ("pods/ephemeralcontainers",)),
    }
    assert len(policy["matchConstraints"]["resourceRules"]) == 2
    assert "namespaceSelector" not in policy["matchConstraints"], "name-matched, so an unlabelled exo-cell-* is covered too"
    (validation,) = policy["validations"]
    expression = validation["expression"]
    assert f"'{BREAK_GLASS_GROUP}' in request.userInfo.groups" in expression
    assert "object" not in expression.replace("request.userInfo", "")
    # The same namespace shape cellctl is confined to.
    scope = _find(documents, "ValidatingAdmissionPolicy", "exomem-cellctl-scope")
    assert "matches('^exo-cell-[a-z2-7]{16}$')" in " ".join(v["expression"] for v in scope["spec"]["validations"])
    assert "request.namespace.matches('^exo-cell-[a-z2-7]{16}$')" in expression
    binding = _find(documents, "ValidatingAdmissionPolicyBinding", "exomem-cell-connect-guard")["spec"]
    assert binding == {"policyName": "exomem-cell-connect-guard", "validationActions": ["Deny"]}
