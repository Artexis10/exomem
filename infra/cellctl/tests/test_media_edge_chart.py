"""add-cloud-media-store D4-D6: the platform chart's media edge, the selection and the rules that confine it."""

from __future__ import annotations

import json
from typing import Any

import pytest
import yaml

from .test_platform_chart_headers import (
    ARTIFACT_BROKER_ARGS,
    HELM,
    PLATFORM_CHART,
    _find,
    _helm_template,
    _helm_template_result,
)

pytestmark = pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")

CANARY = "aaaaaaaaaaaaaaaa"
OTHER = "bbbbbbbbbbbbbbbb"
EDGE = (
    *ARTIFACT_BROKER_ARGS,
    "--set-json", f'artifactBroker.cellIds=["{OTHER}"]',
    "--set", "mediaEdge.enabled=true",
    "--set-string", "mediaEdge.endpoint=http://10.43.0.30:8769",
    "--set-string", "mediaEdge.bucket=media-bucket",
    "--set-json", f'mediaEdge.cellIds=["{CANARY}"]',
)
NAMESPACE = "exomem-cloud"


def _scope_expression(documents: list[dict[str, Any]], message_part: str) -> str:
    policy = _find(documents, "ValidatingAdmissionPolicy", "exomem-cellctl-scope")
    return next(v["expression"] for v in policy["spec"]["validations"] if message_part in v["message"])


def _clause(peer: str, cells: str) -> str:
    return f"in [\"{cells}\"] &&\n        e.to[0].podSelector.matchLabels == {{'app.kubernetes.io/name': \"{peer}\"}}"


def _ingress_peers(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _find(documents, "NetworkPolicy", "exomem-media-edge")["spec"]["ingress"][0]["from"]


def test_the_edge_is_off_by_default_and_the_default_render_names_none_of_it() -> None:
    documents = _helm_template()
    rendered = json.dumps(documents)

    # A list of what leaked, so a failure prints names, not the whole render.
    leaked = [n for n in ("exomem-media-edge", "8769", "MEDIA_EDGE", "exomem-media-edge-active-set") if n in rendered]
    assert leaked == []
    cellctl = _find(documents, "Deployment", "cellctl")
    assert [v["name"] for v in cellctl["spec"]["template"]["spec"]["volumes"]] == ["tmp"]


def test_the_edge_is_a_restricted_recreate_deployment_on_a_literal_service_address() -> None:
    documents = _helm_template(*EDGE)
    deployment = _find(documents, "Deployment", "exomem-media-edge")
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    broker = _find(documents, "Deployment", "exomem-artifact-broker")["spec"]["template"]["spec"]["containers"][0]
    service = _find(documents, "Service", "exomem-media-edge")["spec"]
    claim = _find(documents, "PersistentVolumeClaim", "exomem-media-edge-state")

    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    assert container["command"] == ["python", "-m", "exomem.media_edge"]
    assert container["image"] == broker["image"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert pod["automountServiceAccountToken"] is False
    assert (service["clusterIP"], service["ports"][0]["port"]) == ("10.43.0.30", 8769)
    assert claim["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    env = {e["name"]: e.get("value") for e in container["env"]}
    state = next(m for m in container["volumeMounts"] if m["name"] == "state")
    assert env["EXOMEM_MEDIA_EDGE_STATE_DIR"] == state["mountPath"]


def test_the_edge_render_holds_neither_the_media_keys_nor_the_signing_key() -> None:
    documents = _helm_template(*EDGE)
    pod = _find(documents, "Deployment", "exomem-media-edge")["spec"]["template"]["spec"]
    volumes = {v["name"]: v for v in pod["volumes"]}

    # The verifying key and the one active-set file: no signing key, no cell Secret, no epoch record.
    assert volumes["public-key"]["secret"]["secretName"] == "exomem-media-edge-public-key"
    assert volumes["active-set"]["configMap"]["items"] == [{"key": "active-set.json", "path": "active-set.json"}]
    rendered = json.dumps(pod)
    for held in ("signing-key", "media-age-identity", "media-name-key", "media-edge-credential",
                 "cell-credentials", "credential-epochs"):
        assert held not in rendered, held
    cellctl = _find(documents, "Deployment", "cellctl")["spec"]["template"]["spec"]
    assert any(v.get("secret", {}).get("secretName") == "exomem-media-edge-signing-key" for v in cellctl["volumes"])


def test_the_edge_admits_cellctl_and_only_the_selected_runtimes_and_reaches_public_443_and_dns() -> None:
    documents = _helm_template(*EDGE)
    policy = _find(documents, "NetworkPolicy", "exomem-media-edge")["spec"]
    peers = _ingress_peers(documents)

    assert peers[0] == {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "cellctl"}}}
    runtimes = peers[1:]
    assert [p["namespaceSelector"]["matchLabels"]["exomem.io/cloud-cell"] for p in runtimes] == [CANARY]
    assert policy["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 8769}]
    assert [rule["to"][0].get("ipBlock", {}).get("cidr") for rule in policy["egress"]] == ["0.0.0.0/0", None]
    assert {p["port"] for rule in policy["egress"] for p in rule["ports"]} == {443, 53}

    cellctl_egress = _find(documents, "NetworkPolicy", "cellctl")["spec"]["egress"]
    assert {"to": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "exomem-media-edge"}}}],
            "ports": [{"port": 8769, "protocol": "TCP"}]} in cellctl_egress


def test_only_a_selected_cell_may_be_given_the_edge_peer_and_nothing_else_widens() -> None:
    documents = _helm_template(*EDGE)
    expression = _scope_expression(documents, "runtime-ingress only")

    # The edge's port is allowed for the canary and the broker's for its own cell, each for that cell alone.
    assert _clause("exomem-media-edge", CANARY) in expression
    assert _clause("exomem-artifact-broker", OTHER) in expression
    assert _clause("exomem-media-edge", OTHER) not in expression
    assert "e.ports[0].port == 8769" in expression and "e.ports[0].port == 8767" in expression
    assert "size(variables.np.egress) <= 2" in expression
    assert "!has(e.to[0].ipBlock)" in expression


def test_the_fleet_switch_selects_every_cell_beside_the_canary_list() -> None:
    documents = _helm_template(*EDGE, "--set", "mediaEdge.allCells=true")
    peers = _ingress_peers(documents)
    expression = _scope_expression(documents, "runtime-ingress only")
    cellctl = _find(documents, "Deployment", "cellctl")["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e.get("value") for e in cellctl["env"]}

    assert len(peers) == 2 and peers[1]["namespaceSelector"] == {
        "matchExpressions": [{"key": "exomem.io/cloud-cell", "operator": "Exists"}]}
    assert _clause("exomem-media-edge", CANARY) not in expression
    assert _clause("exomem-artifact-broker", OTHER) in expression
    assert env["CELLCTL_MEDIA_EDGE_ALL_CELLS"] == "true"
    assert json.loads(env["CELLCTL_MEDIA_EDGE_CELL_IDS"]) == [CANARY]


def test_cellctl_receives_its_edge_settings_and_may_write_one_config_map() -> None:
    documents = _helm_template(
        *EDGE, "--set-json", f'mediaEdge.credentialEpochs={{"{CANARY}":2}}',
        "--set-json", 'mediaEdge.storePolicy={"evict_after_days":30,"max_temporary_bytes":5637144576,"max_hydrations":1}')
    cellctl = _find(documents, "Deployment", "cellctl")["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e.get("value") for e in cellctl["env"]}
    role = _find(documents, "Role", "cellctl-media-edge-active-set")

    assert env["CELLCTL_MEDIA_EDGE_URL"] == "http://10.43.0.30:8769"
    assert json.loads(env["CELLCTL_MEDIA_EDGE_CREDENTIAL_EPOCHS"]) == {CANARY: 2}
    default_policy = yaml.safe_load((PLATFORM_CHART / "values.yaml").read_text())["mediaEdge"]["storePolicy"]
    assert json.loads(env["CELLCTL_MEDIA_EDGE_STORE_POLICY"]) == {
        **default_policy, "evict_after_days": 30, "max_temporary_bytes": 5637144576, "max_hydrations": 1}
    assert role["metadata"]["namespace"] == NAMESPACE
    assert role["rules"] == [
        {"apiGroups": [""], "resources": ["configmaps"], "resourceNames": ["exomem-media-edge-active-set"],
         "verbs": ["get", "patch"]},
        {"apiGroups": [""], "resources": ["configmaps"], "verbs": ["create"]},
    ]
    assert "request.namespace ==" in _scope_expression(documents, "inside a cell namespace")
    assert "exomem-media-edge-active-set" in _scope_expression(documents, "may write only the ConfigMap")


def test_the_default_object_bound_admits_a_5_gib_file_at_the_edge_and_in_cellctl() -> None:
    # A 5 GiB original's ciphertext is about 1.25 MiB larger. Below that bound such a file never offloads,
    # and an edge and a cellctl that disagree would admit what the other refuses.
    documents = _helm_template(*EDGE)
    edge = _find(documents, "Deployment", "exomem-media-edge")["spec"]["template"]["spec"]["containers"][0]
    cellctl = _find(documents, "Deployment", "cellctl")["spec"]["template"]["spec"]["containers"][0]
    edge_env = {e["name"]: e.get("value") for e in edge["env"]}
    cellctl_env = {e["name"]: e.get("value") for e in cellctl["env"]}

    assert edge_env["EXOMEM_MEDIA_EDGE_MAX_OBJECT_BYTES"] == str(5 * 2**30 + 2 * 2**20)
    assert cellctl_env["CELLCTL_MEDIA_EDGE_MAX_OBJECT_BYTES"] == str(5 * 2**30 + 2 * 2**20)


@pytest.mark.parametrize(
    "override",
    [
        ("--set-string", "mediaEdge.endpoint="),
        ("--set-string", "mediaEdge.endpoint=http://10.43.0.30:8768"),
        ("--set-string", "mediaEdge.endpoint=http://203.0.113.9:8769"),
        ("--set-string", "mediaEdge.bucket="),
        ("--set-string", "artifactBroker.image=ghcr.io/artexis10/exomem:latest"),
        ("--set-json", 'cells.jobEgressExcept=["10.0.0.0/8"]'),
        ("--set-json", 'mediaEdge.cellIds=["short"]'),
        ("--set-json", 'mediaEdge.storePolicy.allowance_bytes=true'),
        ("--set-json", 'mediaEdge.storePolicy.allowance_bytes=-1'),
        ("--set-json", 'mediaEdge.storePolicy.allowance_bytes=1.5'),
        # The hydration capacity is a pair of positive whole numbers: both or neither.
        ("--set-json", 'mediaEdge.storePolicy={"max_hydrations":1}'),
        ("--set-json", 'mediaEdge.storePolicy={"max_temporary_bytes":5637144576}'),
        ("--set-json", 'mediaEdge.storePolicy={"max_temporary_bytes":5637144576,"max_hydrations":0}'),
        ("--set-json", 'mediaEdge.storePolicy={"max_temporary_bytes":1.5,"max_hydrations":1}'),
        ("--set-json", 'mediaEdge.storePolicy={"max_temporary_bytes":5637144576,"max_hydrations":true}'),
        ("--set-json", f'mediaEdge.credentialEpochs={{"{CANARY}":-1}}'),
    ],
)
def test_unusable_edge_settings_refuse_the_render(override: tuple[str, str]) -> None:
    assert _helm_template_result(*EDGE, *override).returncode != 0


def test_a_cell_list_without_the_edge_refuses_the_render() -> None:
    assert _helm_template_result("--set-json", f'mediaEdge.cellIds=["{CANARY}"]').returncode != 0
