from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem_provisioner.kubernetes_restore import (
    CandidateRestoreBinding,
    KubernetesOfflineRestoreRuntime,
    RestoreJobFailed,
)


class BatchApi:
    def __init__(self, *, failed: bool = False) -> None:
        self.failed = failed
        self.jobs: list[dict[str, object]] = []

    async def create_namespaced_job(self, namespace: str, body: dict[str, object]) -> None:
        assert namespace == "exomem-system"
        self.jobs.append(body)

    async def read_namespaced_job_status(self, name: str, namespace: str):
        assert name.startswith("restore-") and namespace == "exomem-system"
        return SimpleNamespace(
            status=SimpleNamespace(
                succeeded=0 if self.failed else 1, failed=1 if self.failed else 0
            )
        )


class CoreApi:
    def __init__(self) -> None:
        self.config_maps: list[dict[str, object]] = []

    async def create_namespaced_config_map(self, namespace: str, body: dict[str, object]) -> None:
        assert namespace == "exomem-system"
        self.config_maps.append(body)

    async def list_namespaced_pod(self, namespace: str, label_selector: str):
        assert namespace == "exomem-system" and label_selector.startswith("job-name=restore-")
        return SimpleNamespace(
            items=[SimpleNamespace(metadata=SimpleNamespace(name="restore-pod"))]
        )

    async def read_namespaced_pod_log(self, name: str, namespace: str) -> str:
        assert name == "restore-pod" and namespace == "exomem-system"
        request = json.loads(self.config_maps[0]["data"]["restore-candidate.json"])  # type: ignore[index]
        return json.dumps(
            {
                "ok": True,
                "code": "HOSTED_RESTORE_CANDIDATE_READY",
                "data": {
                    "status": "ready",
                    "target_cell_id": request["target_cell_id"],
                    "archive_sha256": request["expected_archive_sha256"],
                    "journal_phase": "complete",
                    "derived_state": "ready",
                    "derived_error_code": None,
                },
            },
            sort_keys=True,
        )


class AlreadyExists(RuntimeError):
    status = 409


class ReplaySafeBatchApi(BatchApi):
    def __init__(self) -> None:
        super().__init__()
        self.by_name: dict[str, dict[str, object]] = {}

    async def create_namespaced_job(self, namespace: str, body: dict[str, object]) -> None:
        name = body["metadata"]["name"]  # type: ignore[index]
        if name in self.by_name:
            raise AlreadyExists("job already exists")
        await super().create_namespaced_job(namespace, body)
        self.by_name[name] = body

    async def read_namespaced_job(self, name: str, namespace: str):
        assert namespace == "exomem-system"
        return self.by_name[name]


class ReplaySafeCoreApi(CoreApi):
    def __init__(self) -> None:
        super().__init__()
        self.by_name: dict[str, dict[str, object]] = {}

    async def create_namespaced_config_map(self, namespace: str, body: dict[str, object]) -> None:
        name = body["metadata"]["name"]  # type: ignore[index]
        if name in self.by_name:
            raise AlreadyExists("config map already exists")
        await super().create_namespaced_config_map(namespace, body)
        self.by_name[name] = body

    async def read_namespaced_config_map(self, name: str, namespace: str):
        assert namespace == "exomem-system"
        return self.by_name[name]


def _binding() -> CandidateRestoreBinding:
    return CandidateRestoreBinding(
        namespace="exomem-system",
        service_account="privileged-restore-worker",
        scratch_pvc="exomem-system-scratch",
        target_pvc="cell-candidate-alpha-data",
        source_vault_id="vault-logical-alpha",
        target_vault_id="vault-logical-alpha",
        target_vault_root="/var/lib/exomem/vault",
        target_state_root="/var/lib/exomem/state",
        target_log_root="/var/lib/exomem/log",
        runtime_uid=10001,
        runtime_gid=10001,
        active_credential_version="credential-v2",
        expected_protocol="exomem-hosted.v1",
        workload_name="cell-candidate-alpha",
    )


class AppsApi:
    def __init__(self) -> None:
        self.scales: list[int] = []

    async def patch_namespaced_stateful_set_scale(self, name, namespace, body):
        assert name == "cell-candidate-alpha" and namespace == "exomem-system"
        self.scales.append(body["spec"]["replicas"])

    async def read_namespaced_stateful_set_scale(self, name, namespace):
        return SimpleNamespace(status=SimpleNamespace(replicas=0))


class CandidateProbe:
    async def authenticated_readiness(self, cell_id: str) -> bool:
        return cell_id == "cell-candidate-alpha"

    async def product_checks(self, cell_id: str) -> dict[str, bool]:
        assert cell_id == "cell-candidate-alpha"
        return {
            "capture": True,
            "recall": True,
            "review": True,
            "export": True,
            "restart": True,
            "candidateIdentity": True,
        }


@pytest.mark.asyncio
async def test_runtime_implements_inspection_stop_readiness_and_product_contract(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "portable.zip"
    manifest = json.dumps(
        {
            "sourceCellId": "cell-source-alpha",
            "releaseVersion": "0.22.0",
            "schemaVersion": 1,
            "hostedStateIncluded": False,
        },
        sort_keys=True,
    ).encode()
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("manifest.json", manifest)
        output.writestr("vault/note.md", b"hello")
    apps = AppsApi()
    runtime = KubernetesOfflineRestoreRuntime(
        batch_api=BatchApi(),
        core_api=CoreApi(),
        apps_api=apps,
        candidate_probe=CandidateProbe(),
        binding=_binding(),
        image="ghcr.io/artexis10/exomem@sha256:" + "a" * 64,
        release_version="0.22.0",
        poll_seconds=0,
    )

    inspection = await runtime.inspect_portable_archive(archive)
    await runtime.stop_candidate("cell-candidate-alpha")

    assert inspection.source_cell_id == "cell-source-alpha"
    assert inspection.manifest_sha256 == hashlib.sha256(manifest).hexdigest()
    assert inspection.path_safe is True
    assert inspection.schema_compatible is True
    assert inspection.release_compatible is True
    assert apps.scales == [0]
    assert await runtime.authenticated_readiness("cell-candidate-alpha") is True
    assert all((await runtime.product_checks("cell-candidate-alpha")).values())


@pytest.mark.asyncio
async def test_restore_job_uses_normative_argv_fixed_request_file_and_immutable_image(
    tmp_path: Path,
) -> None:
    batch = BatchApi()
    core = CoreApi()
    archive = tmp_path / "source.zip"
    archive.write_bytes(b"portable archive")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    runtime = KubernetesOfflineRestoreRuntime(
        batch_api=batch,
        core_api=core,
        binding=_binding(),
        image="ghcr.io/artexis10/exomem@sha256:" + "a" * 64,
        poll_seconds=0,
    )

    await runtime.offline_restore(
        "cell-candidate-alpha",
        archive,
        helper_version="1",
        release_version="0.22.0",
        operation_id="restore-operation-alpha",
        fence_generation=10,
        source_cell_id="cell-source-alpha",
        archive_sha256=digest,
        artifact_reference="recovery_opaque_source",
    )

    request = json.loads(core.config_maps[0]["data"]["restore-candidate.json"])  # type: ignore[index]
    assert request == {
        "active_credential_version": "credential-v2",
        "archive_path": "/system-scratch/source.zip",
        "artifact_reference": "recovery_opaque_source",
        "expected_archive_sha256": digest,
        "expected_protocol": "exomem-hosted.v1",
        "expected_release": "0.22.0",
        "operation_id": "restore-operation-alpha",
        "request_id": request["request_id"],
        "routing_stopped": True,
        "runtime_gid": 10001,
        "runtime_uid": 10001,
        "source_cell_id": "cell-source-alpha",
        "source_vault_id": "vault-logical-alpha",
        "target_cell_id": "cell-candidate-alpha",
        "target_log_root": "/var/lib/exomem/log",
        "target_state_root": "/var/lib/exomem/state",
        "target_vault_id": "vault-logical-alpha",
        "target_vault_root": "/var/lib/exomem/vault",
        "workload_stopped": True,
    }
    assert request["request_id"].count("-") == 4
    job = batch.jobs[0]
    pod = job["spec"]["template"]["spec"]  # type: ignore[index]
    container = pod["containers"][0]
    assert container["command"] == [
        "exomem",
        "hosted",
        "restore-candidate",
        "--contract-version",
        "1",
        "--request-file",
        "/run/exomem/operator-requests/restore-candidate.json",
    ]
    assert "@sha256:" in container["image"] and ":latest" not in container["image"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    request_mount = next(
        mount for mount in container["volumeMounts"] if mount["name"] == "operator-request"
    )
    assert request_mount == {
        "name": "operator-request",
        "mountPath": "/run/exomem/operator-requests/restore-candidate.json",
        "subPath": "restore-candidate.json",
        "readOnly": True,
    }
    config_volume = next(
        volume for volume in pod["volumes"] if volume["name"] == "operator-request"
    )
    assert config_volume["configMap"]["defaultMode"] == 0o444
    assert pod["restartPolicy"] == "Never"


@pytest.mark.asyncio
async def test_restore_job_failure_never_claims_publication(tmp_path: Path) -> None:
    archive = tmp_path / "source.zip"
    archive.write_bytes(b"portable archive")
    runtime = KubernetesOfflineRestoreRuntime(
        batch_api=BatchApi(failed=True),
        core_api=CoreApi(),
        binding=_binding(),
        image="ghcr.io/artexis10/exomem@sha256:" + "a" * 64,
        poll_seconds=0,
    )

    with pytest.raises(RestoreJobFailed):
        await runtime.offline_restore(
            "cell-candidate-alpha",
            archive,
            helper_version="1",
            release_version="0.22.0",
            operation_id="restore-operation-alpha",
            fence_generation=10,
            source_cell_id="cell-source-alpha",
            archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            artifact_reference="recovery_opaque_source",
        )


@pytest.mark.asyncio
async def test_restore_job_replay_adopts_the_same_immutable_job_after_lost_ack(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "source.zip"
    archive.write_bytes(b"portable archive")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    batch = ReplaySafeBatchApi()
    core = ReplaySafeCoreApi()
    runtime = KubernetesOfflineRestoreRuntime(
        batch_api=batch,
        core_api=core,
        binding=_binding(),
        image="ghcr.io/artexis10/exomem@sha256:" + "a" * 64,
        poll_seconds=0,
    )

    for _ in range(2):
        await runtime.offline_restore(
            "cell-candidate-alpha",
            archive,
            helper_version="1",
            release_version="0.22.0",
            operation_id="restore-operation-alpha",
            fence_generation=10,
            source_cell_id="cell-source-alpha",
            archive_sha256=digest,
            artifact_reference="recovery_opaque_source",
        )

    assert len(batch.jobs) == 1
    assert len(core.config_maps) == 1


@pytest.mark.asyncio
async def test_restore_archive_digest_is_streamed_in_bounded_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "source.zip"
    archive.write_bytes(b"portable archive")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    original_read_bytes = Path.read_bytes

    def reject_whole_archive_read(path: Path) -> bytes:
        if path == archive:
            raise AssertionError("restore must not load the whole archive")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_whole_archive_read)
    runtime = KubernetesOfflineRestoreRuntime(
        batch_api=BatchApi(),
        core_api=CoreApi(),
        binding=_binding(),
        image="ghcr.io/artexis10/exomem@sha256:" + "a" * 64,
        poll_seconds=0,
    )

    await runtime.offline_restore(
        "cell-candidate-alpha",
        archive,
        helper_version="1",
        release_version="0.22.0",
        operation_id="restore-operation-alpha",
        fence_generation=10,
        source_cell_id="cell-source-alpha",
        archive_sha256=digest,
        artifact_reference="recovery_opaque_source",
    )
