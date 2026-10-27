"""The guarded chart transports its provider policy and generated CRD together."""

import subprocess
import sys

import yaml

from .test_platform_chart_headers import PLATFORM_CHART, _find, _helm_render, _helm_template


def test_guarded_helm_derives_policy_and_replaces_only_the_provider_crd():
    rendered = _helm_render("--set", "cellStorage.local.enabled=true", "--set", "cellStorage.local.reservations=true",
        "--set", "cellStorage.maxCellGib=24", "--set", "cellStorage.local.backupConcurrencyPerNode=3",
        "--set-json", 'topolvm.lvmd.additionalLVMDYamlContent.device-classes=[{"name":"unused-override"}]',
        "--api-versions", "snapshot.storage.k8s.io/v1/VolumeSnapshotClass")
    assert rendered.returncode == 0, rendered.stderr
    before = [doc for doc in yaml.safe_load_all(rendered.stdout) if isinstance(doc, dict)]
    config = _find(before, "ConfigMap", "exomem-local-lvmd-config")
    policy = yaml.safe_load(config["data"]["lvmd.yaml"])["device-classes"][0]["reservation-policy"]
    assert policy == {"ordinary-volume-cap-bytes": 24 * 1024**3, "backup-concurrency": 3,
                      "lock-directory": "/run/topolvm"}
    result = subprocess.run([sys.executable, str(PLATFORM_CHART / "reservation-post-renderer.py")],
                            input=rendered.stdout, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    after = [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]
    crd = _find(after, "CustomResourceDefinition", "logicalvolumes.topolvm.io")
    schema = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]
    assert "backupFamilyRequest" in schema["spec"]["properties"]
    assert "reservationTargetResult" in schema["status"]["properties"]
    def without_crd(docs):
        return [doc for doc in docs if doc.get("metadata", {}).get("name") != "logicalvolumes.topolvm.io"]
    assert without_crd(after) == without_crd(before)
    snapshot = _find(after, "VolumeSnapshotClass", "exomem-cloud-local-snapshot")
    assert snapshot["parameters"]["csi.storage.k8s.io/snapshotter-secret-name"] == "${volumesnapshot.name}"
    assert snapshot["parameters"]["csi.storage.k8s.io/snapshotter-secret-namespace"] == "exomem-local-reservations"
    role = _find(after, "Role", "snapshot-family-reader")
    assert role["metadata"]["namespace"] == "exomem-local-reservations"
    assert role["rules"] == [{"apiGroups": [""], "resources": ["secrets"], "verbs": ["get"]}]
    binding = _find(after, "RoleBinding", "snapshot-family-reader")
    assert binding["subjects"] == [{"kind": "ServiceAccount", "name": "exomem-platform-topolvm-controller",
                                    "namespace": "exomem-platform"}]
    daemon = _find(after, "DaemonSet", "exomem-platform-topolvm-lvmd-0")
    container = daemon["spec"]["template"]["spec"]["containers"][0]
    assert "--config=/etc/topolvm-policy/lvmd.yaml" in container["args"]
    assert any(mount["mountPath"] == "/run/topolvm" for mount in container["volumeMounts"])


def test_unguarded_helm_passes_through_and_has_no_family_secret_requirement():
    rendered = _helm_render("--set", "cellStorage.local.enabled=true",
                            "--api-versions", "snapshot.storage.k8s.io/v1/VolumeSnapshotClass")
    assert rendered.returncode == 0, rendered.stderr
    result = subprocess.run([sys.executable, str(PLATFORM_CHART / "reservation-post-renderer.py")],
                            input=rendered.stdout, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == rendered.stdout
    documents = [doc for doc in yaml.safe_load_all(rendered.stdout) if isinstance(doc, dict)]
    snapshot = _find(documents, "VolumeSnapshotClass", "exomem-cloud-local-snapshot")
    assert "parameters" not in snapshot


def test_legacy_helm_needs_no_reservation_provider():
    documents = _helm_template()
    assert not any(doc.get("metadata", {}).get("name") == "exomem-cellctl-reservations" for doc in documents)
