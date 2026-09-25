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
