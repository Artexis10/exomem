"""add-cloud-media-store D4-D6: the platform chart's media edge, the selection and the rules that confine it."""

from __future__ import annotations

import contextlib
import json
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

from cellctl.manifests import MEDIA_EDGE_ACCESS_LABEL, render_cell_manifests

from .test_media_edge import _spec
from .test_platform_chart_headers import (
    ARTIFACT_BROKER_ARGS,
    HELM,
    PLATFORM_CHART,
    REPO_ROOT,
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


def _selects(selector: dict[str, Any], labels: dict[str, str]) -> bool:
    """Kubernetes label-selector matching for the two operators the chart uses; another one raises."""
    operators = {"Exists": lambda key: key in labels, "DoesNotExist": lambda key: key not in labels}
    return (all(labels.get(key) == value for key, value in selector.get("matchLabels", {}).items())
            and all(operators[e["operator"]](e["key"]) for e in selector.get("matchExpressions", [])))


def _edge_admits(documents: list[dict[str, Any]], rendered: list[dict[str, Any]]) -> bool:
    """Whether the edge's ingress admits the runtime pod of a cell that cellctl rendered."""
    namespace = next(m for m in rendered if m["kind"] == "Namespace")["metadata"]["labels"]
    pod = next(m for m in rendered if m["kind"] == "StatefulSet")["spec"]["template"]["metadata"]["labels"]
    # A peer with no namespace selector admits pods of the edge's own namespace only.
    return any(_selects(peer["namespaceSelector"], namespace) and _selects(peer["podSelector"], pod)
               for peer in _ingress_peers(documents) if "namespaceSelector" in peer)


def _edge_peer_guarded_by_the_label(expression: str) -> bool:
    """Whether the admission gives a runtime the edge peer only in a namespace that carries the label."""
    clause = (f"((namespaceObject != null && has(namespaceObject.metadata.labels) && "
              f"'{MEDIA_EDGE_ACCESS_LABEL}' in namespaceObject.metadata.labels && "
              f"namespaceObject.metadata.labels['{MEDIA_EDGE_ACCESS_LABEL}'] == 'true') && "
              "e.to[0].podSelector.matchLabels == {'app.kubernetes.io/name': \"exomem-media-edge\"}")
    return clause in " ".join(expression.split())


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


def test_the_edge_admits_cellctl_and_the_runtimes_cellctl_labels_and_reaches_public_443_and_dns() -> None:
    documents = _helm_template(*EDGE)
    policy = _find(documents, "NetworkPolicy", "exomem-media-edge")["spec"]
    peers = _ingress_peers(documents)

    assert peers == [
        {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "cellctl"}}},
        {"namespaceSelector": {"matchLabels": {MEDIA_EDGE_ACCESS_LABEL: "true"},
                               "matchExpressions": [{"key": "exomem.io/cloud-cell", "operator": "Exists"}]},
         "podSelector": {"matchLabels": {"app.kubernetes.io/name": "exomem-cell"},
                         "matchExpressions": [{"key": "exomem.io/cell-job", "operator": "DoesNotExist"}]}},
    ]
    assert policy["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 8769}]
    assert [rule["to"][0].get("ipBlock", {}).get("cidr") for rule in policy["egress"]] == ["0.0.0.0/0", None]
    assert {p["port"] for rule in policy["egress"] for p in rule["ports"]} == {443, 53}

    cellctl_egress = _find(documents, "NetworkPolicy", "cellctl")["spec"]["egress"]
    assert {"to": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "exomem-media-edge"}}}],
            "ports": [{"port": 8769, "protocol": "TCP"}]} in cellctl_egress


def test_only_a_labelled_namespace_may_be_given_the_edge_peer_and_nothing_else_widens() -> None:
    documents = _helm_template(*EDGE)
    expression = _scope_expression(documents, "runtime-ingress only")

    # The edge's port follows the namespace label; the broker's stays with its own cell list.
    assert _edge_peer_guarded_by_the_label(expression)
    assert _clause("exomem-artifact-broker", OTHER) in expression
    assert _clause("exomem-media-edge", CANARY) not in expression
    assert "e.ports[0].port == 8769" in expression and "e.ports[0].port == 8767" in expression
    assert "size(variables.np.egress) <= 2" in expression
    assert "!has(e.to[0].ipBlock)" in expression


def test_a_turned_off_canary_reaches_the_edge_while_its_namespace_is_labelled_and_never_without_the_label() -> None:
    # A draining cell must still read its evicted originals after the edge stops selecting it (D12).
    documents = _helm_template(*EDGE, "--set-json", "mediaEdge.cellIds=[]")
    expression = _scope_expression(documents, "runtime-ingress only")
    draining = render_cell_manifests(_spec(media_store_enabled=False))
    retained = render_cell_manifests(_spec(
        media_edge_url="", media_edge_credential="", media_age_identity="", media_recipient="",
        media_name_key=b"", media_key_version=0, media_max_object_bytes=0, media_store_retained=True))

    assert _edge_admits(documents, draining) is True
    assert _edge_admits(documents, retained) is False
    assert _edge_peer_guarded_by_the_label(expression)


def test_the_fleet_switch_selects_every_cell_beside_the_canary_list_and_leaves_the_network_wall() -> None:
    canary = _helm_template(*EDGE)
    documents = _helm_template(*EDGE, "--set", "mediaEdge.allCells=true")
    cellctl = _find(documents, "Deployment", "cellctl")["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e.get("value") for e in cellctl["env"]}

    # The switch chooses which cells cellctl serves; the label, not the switch, opens the network.
    assert _ingress_peers(documents) == _ingress_peers(canary)
    assert (_scope_expression(documents, "runtime-ingress only")
            == _scope_expression(canary, "runtime-ingress only"))
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


@contextlib.contextmanager
def _api_server(active_set_json: str | None) -> Iterator[str]:
    """A local stand-in for the Kubernetes API that answers the chart's `lookup` calls: the edge's
    active set when given, and 404 for every other object. Offline, `helm template` skips `lookup`."""
    active_set = f"/api/v1/namespaces/{NAMESPACE}/configmaps/exomem-media-edge-active-set"

    class Api(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the http.server hook name
            path = self.path.split("?")[0]
            if path == "/api/v1":
                code, body = 200, {"kind": "APIResourceList", "groupVersion": "v1", "resources": [
                    {"name": "configmaps", "namespaced": True, "kind": "ConfigMap", "verbs": ["get"]}]}
            elif path == active_set and active_set_json is not None:
                code, body = 200, {"apiVersion": "v1", "kind": "ConfigMap",
                                   "metadata": {"name": "exomem-media-edge-active-set", "namespace": NAMESPACE},
                                   "data": {"active-set.json": active_set_json}}
            else:
                code, body = 404, {"apiVersion": "v1", "kind": "Status", "status": "Failure", "reason": "NotFound",
                                   "code": 404}
            payload = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Api)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _upgrade_with_the_edge_off(active_set_json: str | None, home: Path) -> subprocess.CompletedProcess[str]:
    """The server-side render an upgrade does, against a cluster whose live active set is the one given.
    An empty kubeconfig keeps every real cluster and credential out of it."""
    kubeconfig = home / "kubeconfig"
    kubeconfig.write_text("")
    with _api_server(active_set_json) as url:
        return subprocess.run(
            [HELM, "template", "platform-header-test", str(PLATFORM_CHART), "--namespace", "exomem-platform",
             "--values", str(PLATFORM_CHART / "values.validation.yaml"),
             "--dry-run=server", "--kube-apiserver", url],
            cwd=REPO_ROOT, capture_output=True, text=True, check=False,
            env={"HOME": str(home), "KUBECONFIG": str(kubeconfig), "PATH": "/usr/bin:/bin"},
        )


def test_turning_the_edge_off_is_refused_while_the_live_active_set_lists_a_cell(tmp_path: Path) -> None:
    # With the edge off cellctl signs no credential, so a listed cell would lose every evicted original.
    # A draining cell stays listed until its record shows no evicted original.
    result = _upgrade_with_the_edge_off(f'{{"generation":7,"cells":{{"{OTHER}":6,"{CANARY}":7}}}}', tmp_path)

    assert result.returncode != 0
    assert CANARY in result.stderr and OTHER in result.stderr


def test_turning_the_edge_off_is_refused_while_the_live_active_set_cannot_be_read(tmp_path: Path) -> None:
    # An unreadable set proves nothing drained.
    result = _upgrade_with_the_edge_off("{not json", tmp_path)

    assert result.returncode != 0
    assert "cannot be read" in result.stderr


@pytest.mark.parametrize(
    "active_set_json",
    [
        # Every cell drained and left the set.
        '{"generation":9,"cells":{}}',
        # The edge never ran, so cellctl never published a set.
        None,
    ],
)
def test_turning_the_edge_off_renders_once_the_live_active_set_lists_no_cell(
    active_set_json: str | None, tmp_path: Path
) -> None:
    result = _upgrade_with_the_edge_off(active_set_json, tmp_path)

    assert result.returncode == 0, result.stderr
