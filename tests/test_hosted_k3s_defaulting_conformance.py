"""Check the closed-set Job proofs against a real Kubernetes API server.

The unit doubles for these proofs are built from the same schema the proofs
check, so they agree by construction: a field the API server adds on its own can
only be discovered live, which is how a stored ``podReplacementPolicy`` came to
kill a production provision at its final step.

This renders each Job the provisioner creates, submits it as a server-side dry
run, which persists nothing, and asserts the stored shape introduces no Job-spec
field outside ``JOB_SPEC_SERVER_DEFAULTS`` -- the single list every proof
tolerates -- and that each such field carries the value the proofs expect.

It runs on the same disposable K3s the other exact-cluster tests use, so CI
exercises it on every pull request rather than when someone remembers to.
Point ``EXOMEM_CONFORMANCE_KUBECONFIG`` at a kubeconfig to check another
cluster instead; the dry run stores nothing, so a production cluster is safe.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Core CI shards run the runtime distribution without the separately packaged
# provisioner. Skip the whole module rather than failing collection.
pytest.importorskip("exomem_provisioner", reason="requires the provisioner package")
kubernetes = pytest.importorskip("kubernetes", reason="requires the provisioner Kubernetes SDK")

from exomem_provisioner.adapters import KubernetesVaultFingerprintAdapter  # noqa: E402
from exomem_provisioner.governance_migration_job import (  # noqa: E402
    MigrationJobRequest,
    build_governance_migration_job,
)
from exomem_provisioner.governance_storage_init import (  # noqa: E402
    KubernetesGovernanceStorageInitAdapter,
)
from exomem_provisioner.job_execution import JOB_SPEC_SERVER_DEFAULTS  # noqa: E402
from exomem_provisioner.lifecycle import OpaqueProviderMetadata  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from test_hosted_k3s_admission import k3s as k3s  # noqa: E402,F401 - reuse the exact harness

METADATA = OpaqueProviderMetadata("tenant-conformance", "cell-conformance", "operation-a", 3)
IMAGE = "ghcr.io/example/exomem-provisioner@sha256:" + "a" * 64
ENVELOPE = "conformance-recovery-envelope"


def _fingerprint_body():
    adapter = KubernetesVaultFingerprintAdapter(
        core_v1=object(), batch_v1=object(), image=IMAGE, sleep=lambda _seconds: None
    )
    return adapter._body(
        METADATA, operation_id="operation-a", phase="before", recovery_envelope=ENVELOPE
    )


def _storage_init_body():
    adapter = KubernetesGovernanceStorageInitAdapter(
        core_v1=object(),
        batch_v1=object(),
        apps_v1=object(),
        identity_verifier=object(),
        runtime_image=IMAGE,
    )
    return adapter._job_body(METADATA, ENVELOPE)


def _migration_body():
    request = MigrationJobRequest(
        metadata=METADATA,
        vault_id=METADATA.tenant_id,
        pvc_uid="pvc-conformance",
        runtime_image=IMAGE,
        custody_revision="c" * 64,
        phase="inspect",
    )
    return build_governance_migration_job(request, recovery_envelope=ENVELOPE)


BODIES = {
    "vault-fingerprint": _fingerprint_body,
    "governance-storage-init": _storage_init_body,
    "governance-migration": _migration_body,
}


@pytest.fixture(scope="module")
def cluster(request, tmp_path_factory):
    path = os.environ.get("EXOMEM_CONFORMANCE_KUBECONFIG")
    if not path:
        # No explicit cluster: stand up the same disposable K3s the other exact
        # cluster tests use, so this runs wherever they run.
        from test_hosted_k3s_admission import _host_kubeconfig

        container = request.getfixturevalue("k3s")
        path = str(_host_kubeconfig(container, tmp_path_factory.mktemp("conformance") / "kubeconfig"))
    kubernetes.config.load_kube_config(config_file=path)
    core = kubernetes.client.CoreV1Api()
    namespace = METADATA.resource_name
    try:
        core.read_namespace(namespace)
        created = False
    except kubernetes.client.ApiException as error:
        if error.status != 404:
            raise
        core.create_namespace({"metadata": {"name": namespace}})
        created = True
    yield kubernetes.client.BatchV1Api()
    if created:
        core.delete_namespace(namespace)


@pytest.mark.parametrize("job", sorted(BODIES))
def test_server_defaulting_stays_inside_what_the_job_proofs_tolerate(cluster, job):
    # Builders that feed a typed client omit the type header the API server needs.
    body = {"apiVersion": "batch/v1", "kind": "Job", **BODIES[job]()}
    stored = kubernetes.client.ApiClient().sanitize_for_serialization(
        cluster.create_namespaced_job(body["metadata"]["namespace"], body, dry_run="All")
    )

    rendered = set(body["spec"])
    # `selector` is server-generated from the controller UID and proven separately.
    unexpected = set(stored["spec"]) - rendered - set(JOB_SPEC_SERVER_DEFAULTS) - {"selector"}
    assert not unexpected, f"{job}: API server added Job-spec fields no proof tolerates"

    for field, tolerated in JOB_SPEC_SERVER_DEFAULTS.items():
        if field in stored["spec"] and field not in rendered:
            assert stored["spec"][field] == tolerated, (
                f"{job}: API server defaulted {field} to {stored['spec'][field]!r}, "
                f"but the proofs only tolerate {tolerated!r}"
            )
