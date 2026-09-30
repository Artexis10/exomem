"""The pre-binding chart shell cannot execute storage or serve traffic."""

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from kubernetes.client import ApiClient
from test_governance_provision_live import ProvisionHarness

from exomem_provisioner.governance_storage_init import KubernetesGovernanceStorageInitAdapter


async def test_actual_prebinding_helm_render_has_storage_but_no_workload_or_routes():
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("pinned Helm is required for rendered chart acceptance")
    h = ProvisionHarness()
    h.context = replace(h.context, checkpoint="namespace-ready")

    async def credentials(*args, effect_guard, **kwargs):
        await effect_guard()

    h.plane._cell.write_credential_bundle = credentials
    await h.step()
    [values] = h.helm_calls
    assert values["provisionMode"] == "serve"
    rendered = subprocess.run(
        [
            helm,
            "template",
            h.current.resource_name,
            str(Path(__file__).resolve().parents[3] / "infra/helm/cell"),
            "--values",
            "-",
        ],
        input=json.dumps(values),
        text=True,
        capture_output=True,
        check=False,
    )
    assert rendered.returncode == 0, rendered.stderr
    documents = [doc for doc in yaml.safe_load_all(rendered.stdout) if isinstance(doc, dict)]
    kinds = {doc["kind"] for doc in documents}
    assert "PersistentVolumeClaim" in kinds
    assert not kinds & {"Job", "Pod", "StatefulSet", "Deployment", "IngressRoute"}
    assert not any(doc["metadata"]["name"].endswith("-init-request") for doc in documents)


@pytest.mark.parametrize("installed_metadata", [False, True])
async def test_actual_initializer_render_matches_closed_execution_and_request_proofs(
    installed_metadata,
):
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("pinned Helm is required for rendered chart acceptance")
    h = ProvisionHarness()
    h.context = replace(h.context, checkpoint=h.bound_checkpoint("initializing"))
    await h.step()
    [values] = h.helm_calls
    rendered = subprocess.run(
        [
            helm,
            "template",
            h.current.resource_name,
            str(Path(__file__).resolve().parents[3] / "infra/helm/cell"),
            "--values",
            "-",
        ],
        input=json.dumps(values),
        text=True,
        capture_output=True,
        check=False,
    )
    assert rendered.returncode == 0, rendered.stderr
    documents = [doc for doc in yaml.safe_load_all(rendered.stdout) if isinstance(doc, dict)]
    assert not {doc["kind"] for doc in documents} & {"StatefulSet", "Deployment", "IngressRoute"}
    [job] = [doc for doc in documents if doc["kind"] == "Job"]
    [request] = [doc for doc in documents if doc["kind"] == "ConfigMap"]
    for document in (job, request):
        document["metadata"].update(
            namespace=h.current.resource_name, uid="api-assigned-uid", resourceVersion="1"
        )
        if installed_metadata:
            # Helm 3.19.4 action.setMetadataVisitor adds only top-level tracking.
            document["metadata"]["labels"]["app.kubernetes.io/managed-by"] = "Helm"
            document["metadata"]["annotations"].update(
                {
                    "meta.helm.sh/release-name": h.current.resource_name,
                    "meta.helm.sh/release-namespace": h.current.resource_name,
                }
            )
    api = ApiClient()
    job = api.sanitize_for_serialization(
        api.deserialize(SimpleNamespace(data=json.dumps(job)), "V1Job")
    )
    authentications = []
    adapter = KubernetesGovernanceStorageInitAdapter(
        core_v1=None,
        batch_v1=None,
        apps_v1=None,
        identity_verifier=SimpleNamespace(
            authenticate=lambda *args, **kwargs: authentications.append((args, kwargs))
        ),
        runtime_image=h.config.image,
    )
    assert adapter._prove_job(job, h.current, h.envelopes["initJob"], allow_deleting=False) == (
        "api-assigned-uid",
        "1",
        False,
    )
    assert adapter._prove_config_map(
        request,
        h.current,
        recovery_envelope=h.envelopes["initRequestConfigMap"],
        init_request={
            "request_id": values["initRequestId"],
            "operation_id": values["initOperationId"],
            "cell_id": values["cellId"],
            "vault_id": values["vaultId"],
            "vault_root": "/var/lib/exomem/vault",
            "state_root": "/var/lib/exomem/state",
            "log_root": "/var/lib/exomem/logs",
            "expected_release": values["expectedRelease"],
            "expected_protocol": values["expectedProtocol"],
            "runtime_uid": values["runtimeUid"],
            "runtime_gid": values["runtimeGid"],
            "active_credential_version": values["activeCredentialVersion"],
        },
    ) == ("api-assigned-uid", "1")
    assert len(authentications) == 2
