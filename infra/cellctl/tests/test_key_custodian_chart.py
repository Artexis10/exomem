"""contain-cloud-controller-compromise D9: the platform chart's key custodian, and the rules that
keep cellctl from creating or reading the per-cell object-store keys the custodian mints."""

from __future__ import annotations

import re
from typing import Any

import pytest

from cellctl.manifests import NAMESPACE_PREFIX, OBJECT_STORE_KEY_SECRET

from .test_platform_chart_headers import HELM, _find, _helm_template, _helm_template_result

pytestmark = pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")

CUSTODIAN = ("--set", "keyCustodian.enabled=true")
CUSTODIAN_NAMESPACE = "exomem-key-custody"


def _scope_rule(documents: list[dict[str, Any]], message_part: str) -> str:
    policy = _find(documents, "ValidatingAdmissionPolicy", "exomem-cellctl-scope")
    return next(v["expression"] for v in policy["spec"]["validations"] if message_part in v["message"])


def _namespace_pattern(expression: str) -> str:
    return re.search(r"matches\('([^']*exo-cell-[^']*)'\)", expression).group(1)


def test_the_custodian_renders_nothing_until_it_is_switched_on() -> None:
    documents = _helm_template()
    assert not [d for d in documents if d["metadata"].get("namespace") == CUSTODIAN_NAMESPACE]
    assert not [d for d in documents if d["metadata"]["name"].startswith(("key-custodian", "exomem-key-custodian"))]


def test_the_custodian_namespace_is_outside_every_pattern_that_confines_cellctl_to_cells() -> None:
    """Defect: cellctl's write scope reaches the custodian's namespace, where the custodian's own key lives."""
    documents = _helm_template(*CUSTODIAN)
    writable = _namespace_pattern(_scope_rule(documents, "may write only inside a cell namespace"))
    custodian_policy = _find(documents, "ValidatingAdmissionPolicy", "exomem-key-custodian-scope")
    custodian_scope = next(v["expression"] for v in custodian_policy["spec"]["validations"] if "cell namespace" in v["message"])

    assert not re.fullmatch(writable, CUSTODIAN_NAMESPACE)
    assert writable == _namespace_pattern(custodian_scope)
    assert re.fullmatch(writable, f"{NAMESPACE_PREFIX}aaaaaaaaaaaaaaaa")


def test_only_the_custodian_can_write_or_read_the_object_store_key_secret() -> None:
    """Defect: cellctl can mint, overwrite or read a cell's object-store key."""
    documents = _helm_template(*CUSTODIAN)
    write_rule = _scope_rule(documents, "may write only Opaque cell credentials")
    cellctl_role = _find(documents, "ClusterRole", "cellctl")
    secret_verbs = {verb for rule in cellctl_role["rules"] if "secrets" in rule["resources"] for verb in rule["verbs"]}

    assert OBJECT_STORE_KEY_SECRET not in write_rule
    assert secret_verbs == {"create", "patch", "delete"}  # no get, list or watch: a key cannot be read back

    custodian_role = _find(documents, "ClusterRole", "key-custodian")
    assert custodian_role["rules"] == [
        {"apiGroups": [""], "resources": ["namespaces"], "verbs": ["list", "watch"]},
        {"apiGroups": [""], "resources": ["secrets"], "resourceNames": [OBJECT_STORE_KEY_SECRET], "verbs": ["get"]},
        {"apiGroups": [""], "resources": ["secrets"], "verbs": ["create"]},
    ]


def test_admission_confines_the_custodian_identity_to_creating_that_secret() -> None:
    """Defect: a widened RBAC rule lets the custodian write anything else."""
    documents = _helm_template(*CUSTODIAN)
    policy = _find(documents, "ValidatingAdmissionPolicy", "exomem-key-custodian-scope")
    binding = _find(documents, "ValidatingAdmissionPolicyBinding", "exomem-key-custodian-scope")

    assert policy["spec"]["failurePolicy"] == "Fail"
    assert binding["spec"] == {"policyName": "exomem-key-custodian-scope", "validationActions": ["Deny"]}
    assert policy["spec"]["matchConstraints"]["resourceRules"] == [{
        "apiGroups": ["*"], "apiVersions": ["*"], "operations": ["CREATE", "UPDATE", "DELETE", "CONNECT"],
        "resources": ["*/*"], "scope": "*",
    }]
    identity = f"system:serviceaccount:{CUSTODIAN_NAMESPACE}:key-custodian"
    assert identity in policy["spec"]["matchConditions"][0]["expression"]
    expressions = " ".join(" ".join(v["expression"].split()) for v in policy["spec"]["validations"])
    assert f"object.metadata.name == '{OBJECT_STORE_KEY_SECRET}'" in expressions
    assert "keyId" in expressions and "applicationKey" in expressions


def test_admission_reads_the_request_fields_the_api_server_omits_when_empty_as_optional() -> None:
    """Defect (found live): a bare `request.subResource` on a main-resource request throws, and
    with failurePolicy Fail that denies every custodian write, the one it exists to allow included."""
    policy = _find(_helm_template(*CUSTODIAN), "ValidatingAdmissionPolicy", "exomem-key-custodian-scope")
    expressions = " ".join(" ".join(v["expression"].split()) for v in policy["spec"]["validations"])

    assert "request.?subResource.orValue('') == ''" in expressions
    assert "request.?namespace.orValue('')" in expressions
    assert "request.subResource" not in expressions and "request.namespace" not in expressions


def test_the_custodian_pod_is_restricted_and_takes_its_credential_from_a_secret_reference() -> None:
    documents = _helm_template(*CUSTODIAN)
    deployment = _find(documents, "Deployment", "key-custodian")
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    cellctl_image = _find(documents, "Deployment", "cellctl")["spec"]["template"]["spec"]["containers"][0]["image"]
    namespace = _find(documents, "Namespace", CUSTODIAN_NAMESPACE)

    assert deployment["metadata"]["namespace"] == CUSTODIAN_NAMESPACE
    assert container["image"] == cellctl_image
    assert container["command"] == ["python3", "-m", "cellctl.custodian"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["nodeSelector"] == {"node-role.kubernetes.io/control-plane": "true"}
    assert namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"
    credential = {e["name"]: e for e in container["env"] if e["name"].startswith("KEY_CUSTODIAN_B2_")}
    assert set(credential) == {"KEY_CUSTODIAN_B2_KEY_ID", "KEY_CUSTODIAN_B2_APPLICATION_KEY"}
    assert all("value" not in entry and entry["valueFrom"]["secretKeyRef"]["name"] == "exomem-key-custodian-b2"
               for entry in credential.values())


def test_the_custodian_accepts_no_traffic_and_reaches_only_dns_the_api_server_and_public_https() -> None:
    documents = _helm_template(*CUSTODIAN)
    policy = _find(documents, "NetworkPolicy", "key-custodian")
    egress = policy["spec"]["egress"]

    assert policy["spec"]["ingress"] == []
    assert [(rule["to"][0].get("ipBlock", {}).get("cidr"), {p["port"] for p in rule["ports"]}) for rule in egress[:2]] == [
        ("10.0.1.2/32", {6443}), ("0.0.0.0/0", {443})]
    assert {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"} <= set(egress[1]["to"][0]["ipBlock"]["except"])
    assert egress[2]["to"][0]["podSelector"]["matchLabels"] == {"k8s-app": "kube-dns"}
    assert len(egress) == 3
    assert any(d["kind"] == "NetworkPolicy" and d["metadata"] == {"name": "default-deny", "namespace": CUSTODIAN_NAMESPACE}
               and d["spec"] == {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]} for d in documents)


def test_the_custodian_will_not_render_without_the_backup_bucket_id() -> None:
    result = _helm_template_result(*CUSTODIAN, "--set-string", "cellData.backupBucketId=")
    assert result.returncode != 0 and "backupBucketId" in result.stderr


def test_the_chart_refuses_a_staging_bucket_that_is_the_locked_backup_bucket() -> None:
    """Defect: cellctl's staging key lists and deletes, so a shared bucket would aim it at backups."""
    result = _helm_template_result("--set-string", "cellData.stagingBucketName=exomem-cell-backups")

    assert result.returncode != 0 and "stagingBucketName must differ" in result.stderr
