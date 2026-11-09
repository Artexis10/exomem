"""automate-cell-storage-deletion: the platform chart's parts of the deletion purge. The live K3s job proves
what admission admits (test_k3s_integration); these renders prove which parts exist in which configuration."""

from __future__ import annotations

import json
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .test_key_custodian_chart import CUSTODIAN, CUSTODIAN_NAMESPACE
from .test_media_edge_chart import CANARY, EDGE, NAMESPACE, _scope_expression
from .test_platform_chart_headers import HELM, _find, _helm_render, _helm_template

pytestmark = pytest.mark.skipif(HELM is None, reason="helm binary not on PATH")

# The current grant public key and the next one, as a rotation pins them both.
GRANT_KEYS = [
    Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    for _ in range(2)
]
POLICY = (
    *CUSTODIAN, *EDGE,
    "--set-json", f"cellDeletion.grantPublicKeys={json.dumps(GRANT_KEYS)}",
    "--set-json", f'cellDeletion.holdCells=["{CANARY}"]',
)


def _pod(documents: list[dict[str, Any]], workload: str) -> dict[str, Any]:
    return _find(documents, "Deployment", workload)["spec"]["template"]["spec"]


def _env(pod: dict[str, Any]) -> dict[str, Any]:
    return {e["name"]: e.get("value", e.get("valueFrom")) for e in pod["containers"][0]["env"]}


def _mounted(pod: dict[str, Any], path: str) -> tuple[dict[str, Any], str]:
    """The volume that the container shows at `path`, and the key of that volume which the file holds."""
    for mount in pod["containers"][0]["volumeMounts"]:
        directory = mount["mountPath"].rstrip("/") + "/"
        if path.startswith(directory):
            volume = next(v for v in pod["volumes"] if v["name"] == mount["name"])
            name = path.removeprefix(directory)
            items = volume.get("configMap", {}).get("items")
            return volume, next(i["key"] for i in items if i["path"] == name) if items else name
    raise AssertionError(f"nothing is mounted at {path}")


@pytest.mark.parametrize("settings", [(), EDGE], ids=["edge off", "edge on"])
def test_cellctl_may_relay_the_deletion_grants_with_the_media_edge_on_or_off(settings: tuple[str, ...]) -> None:
    """Defect: the relay's grant and admission exception hang on the media edge switch, so a platform without
    the edge never relays a grant and the key custodian never purges a deleted cell's backups."""
    documents = _helm_template(*settings)

    role = _find(documents, "Role", "cellctl-cell-deletions")
    binding = _find(documents, "RoleBinding", "cellctl-cell-deletions")
    assert role["metadata"]["namespace"] == binding["metadata"]["namespace"] == NAMESPACE
    assert role["rules"] == [
        {"apiGroups": [""], "resources": ["configmaps"], "resourceNames": ["exomem-cell-deletions"],
         "verbs": ["get", "patch"]},
        {"apiGroups": [""], "resources": ["configmaps"], "verbs": ["create"]},
    ]
    assert binding["subjects"] == [{"kind": "ServiceAccount", "name": "cellctl", "namespace": NAMESPACE}]
    assert "'exomem-cell-deletions'" in _scope_expression(documents, "may write only the ConfigMap")


@pytest.mark.parametrize(("namespace", "workload", "prefix"), [
    (CUSTODIAN_NAMESPACE, "key-custodian", "KEY_CUSTODIAN"),
    (NAMESPACE, "exomem-media-edge", "EXOMEM_MEDIA_EDGE"),
])
def test_each_executor_reads_the_grant_keys_and_the_hold_list_that_the_chart_values_set(
    namespace: str, workload: str, prefix: str,
) -> None:
    """Defect: an executor reads a hold list that the chart never fills, and purges a held cell; or it reads
    no grant key, or only the current one during a rotation, and never purges a deleted cell."""
    documents = _helm_template(*POLICY)
    pod = _pod(documents, workload)
    env = _env(pod)

    for variable, expected in (("GRANT_PUBLIC_KEYS_FILE", GRANT_KEYS), ("HOLD_LIST_FILE", [CANARY])):
        volume, key = _mounted(pod, env[f"{prefix}_{variable}"])
        config_map = next(d for d in documents if d["kind"] == "ConfigMap" and d["metadata"]["namespace"] == namespace
                          and d["metadata"]["name"] == volume["configMap"]["name"])
        assert json.loads(config_map["data"][key]) == expected


def test_the_edge_reads_the_grants_that_cellctl_relays_and_starts_before_cellctl_relays_one() -> None:
    """Defect: the edge reads its grants from a file that nothing mounts, and never purges a deleted cell's
    media; or its pod waits for a ConfigMap that cellctl creates only with the first grant."""
    pod = _pod(_helm_template(*EDGE), "exomem-media-edge")

    volume, key = _mounted(pod, _env(pod)["EXOMEM_MEDIA_EDGE_GRANTS_FILE"])

    assert (volume["configMap"]["name"], key, volume["configMap"]["optional"]) == (
        "exomem-cell-deletions", "grants.json", True)


def test_only_the_media_edge_holds_its_list_key_and_it_starts_without_it() -> None:
    """Defect: the edge's list key reaches another workload; or a release that reaches the cluster before the
    operator creates that Secret stops the edge, and every served cell loses its media."""
    documents = _helm_template(*CUSTODIAN, *EDGE)

    holders = {(d["kind"], d["metadata"]["name"]) for d in documents
               if "exomem-media-edge-list" in json.dumps(d.get("spec", {}))}
    env = _env(_pod(documents, "exomem-media-edge"))

    assert holders == {("Deployment", "exomem-media-edge")}
    assert env["EXOMEM_MEDIA_EDGE_LIST_KEY_ID"]["secretKeyRef"] == {
        "name": "exomem-media-edge-list", "key": "keyId", "optional": True}
    assert env["EXOMEM_MEDIA_EDGE_LIST_APPLICATION_KEY"]["secretKeyRef"] == {
        "name": "exomem-media-edge-list", "key": "applicationKey", "optional": True}


def test_the_custodian_may_read_the_relayed_grants_and_write_only_its_own_done_marks() -> None:
    """Defect: the custodian cannot read the grants or record a finished purge, so every purge stalls; or a
    wider rule lets it read another ConfigMap in cellctl's namespace."""
    documents = _helm_template(*CUSTODIAN)
    env = _env(_pod(documents, "key-custodian"))
    grants = _find(documents, "Role", "key-custodian-cell-deletions")
    purges = _find(documents, "Role", "key-custodian-purges")

    assert grants["metadata"]["namespace"] == env["KEY_CUSTODIAN_DELETIONS_NAMESPACE"] == NAMESPACE
    assert grants["rules"] == [{"apiGroups": [""], "resources": ["configmaps"],
                                "resourceNames": ["exomem-cell-deletions"], "verbs": ["get"]}]
    assert purges["metadata"]["namespace"] == env["KEY_CUSTODIAN_NAMESPACE"] == CUSTODIAN_NAMESPACE
    assert purges["rules"] == [
        {"apiGroups": [""], "resources": ["configmaps"], "resourceNames": ["exomem-key-custodian-purges"],
         "verbs": ["get", "update"]},
        {"apiGroups": [""], "resources": ["configmaps"], "verbs": ["create"]},
    ]
    for name, namespace in (("key-custodian-cell-deletions", NAMESPACE), ("key-custodian-purges", CUSTODIAN_NAMESPACE)):
        binding = _find(documents, "RoleBinding", name)
        assert binding["metadata"]["namespace"] == namespace
        assert binding["subjects"] == [
            {"kind": "ServiceAccount", "name": "key-custodian", "namespace": CUSTODIAN_NAMESPACE}]


@pytest.mark.parametrize("value", [
    "cellDeletion.grantPublicKeys=[\"not a key\"]",
    "cellDeletion.holdCells=[\"not-a-cell-id\"]",
])
def test_a_malformed_policy_value_refuses_the_render(value: str) -> None:
    """Defect: a pasted key that no executor can load, or a typed cell ID that matches no cell, renders; the
    executors then verify no grant, or hold the wrong cell, and only the watcher shows it days later."""
    result = _helm_render(*CUSTODIAN, "--set-json", value)

    assert result.returncode != 0
    assert "cellDeletion" in result.stderr
