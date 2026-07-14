from __future__ import annotations

import copy
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


class NotFound(RuntimeError):
    status = 404


class BatchApi:
    def __init__(self, *, failed: bool = False) -> None:
        self.failed = failed
        self.jobs: list[dict[str, object]] = []
        self.active_names: set[str] = set()
        self.deleted_jobs: list[str] = []

    async def create_namespaced_job(self, namespace: str, body: dict[str, object]) -> None:
        assert namespace == "exomem-system"
        self.jobs.append(body)
        self.active_names.add(str(body["metadata"]["name"]))  # type: ignore[index]

    async def delete_namespaced_job(
        self, name: str, namespace: str, *, body: dict[str, object]
    ) -> None:
        assert namespace == "exomem-system"
        assert body == {"gracePeriodSeconds": 0, "propagationPolicy": "Foreground"}
        if name not in self.active_names:
            raise NotFound()
        self.active_names.remove(name)
        self.deleted_jobs.append(name)

    async def read_namespaced_job(self, name: str, namespace: str):
        assert namespace == "exomem-system"
        if name not in self.active_names:
            raise NotFound()
        return next(job for job in self.jobs if job["metadata"]["name"] == name)  # type: ignore[index]

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
        self.secrets: list[dict[str, object]] = []
        self.deleted_config_maps: list[str] = []
        self.deleted_secrets: list[str] = []
        self.active_config_maps: set[str] = set()
        self.active_secrets: set[str] = set()
        self.active_restore_pod = False
        self.deleted_pods: list[str] = []

    async def create_namespaced_config_map(self, namespace: str, body: dict[str, object]) -> None:
        assert namespace == "exomem-system"
        self.config_maps.append(body)
        self.active_config_maps.add(str(body["metadata"]["name"]))  # type: ignore[index]
        self.active_restore_pod = True

    async def create_namespaced_secret(self, namespace: str, body: dict[str, object]) -> None:
        assert namespace == "exomem-system"
        self.secrets.append(body)
        self.active_secrets.add(str(body["metadata"]["name"]))  # type: ignore[index]

    async def delete_namespaced_secret(self, name: str, namespace: str) -> None:
        assert namespace == "exomem-system"
        if name not in self.active_secrets:
            raise NotFound()
        self.active_secrets.remove(name)
        self.deleted_secrets.append(name)

    async def delete_namespaced_config_map(self, name: str, namespace: str) -> None:
        assert namespace == "exomem-system"
        if name not in self.active_config_maps:
            raise NotFound()
        self.active_config_maps.remove(name)
        self.deleted_config_maps.append(name)

    async def list_namespaced_pod(self, namespace: str, label_selector: str):
        assert namespace == "exomem-system" and label_selector.startswith("job-name=restore-")
        job_name = label_selector.removeprefix("job-name=")
        items = []
        if self.active_restore_pod:
            items.append(
                SimpleNamespace(
                    metadata=SimpleNamespace(
                        name=f"{job_name}-abcde",
                        labels={
                            "app.kubernetes.io/name": "exomem-restore-candidate",
                            "exomem.io/cell": "exomem-system",
                            "exomem.io/restore-candidate": "true",
                            "exomem.io/cell-id": "cell-candidate-alpha",
                            "exomem.io/operation-digest": "a" * 32,
                            "exomem.io/fence": "10",
                            "job-name": job_name,
                        },
                    )
                )
            )
        return SimpleNamespace(items=items)

    async def delete_namespaced_pod(
        self, name: str, namespace: str, *, body: dict[str, object]
    ) -> None:
        assert namespace == "exomem-system"
        assert body == {"gracePeriodSeconds": 0, "propagationPolicy": "Foreground"}
        if not self.active_restore_pod:
            raise NotFound()
        self.active_restore_pod = False
        self.deleted_pods.append(name)

    async def read_namespaced_pod_log(self, name: str, namespace: str) -> str:
        assert name.endswith("-abcde") and namespace == "exomem-system"
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
        await super().read_namespaced_job(name, namespace)
        return self.by_name[name]

    async def delete_namespaced_job(self, name, namespace, *, body):
        await super().delete_namespaced_job(name, namespace, body=body)
        self.by_name.pop(name, None)


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

    async def create_namespaced_secret(self, namespace: str, body: dict[str, object]) -> None:
        name = body["metadata"]["name"]  # type: ignore[index]
        if name in self.by_name:
            raise AlreadyExists("secret already exists")
        await super().create_namespaced_secret(namespace, body)
        self.by_name[name] = body

    async def read_namespaced_secret(self, name: str, namespace: str):
        assert namespace == "exomem-system"
        return self.by_name[name]

    async def delete_namespaced_secret(self, name: str, namespace: str) -> None:
        await super().delete_namespaced_secret(name, namespace)
        self.by_name.pop(name, None)

    async def delete_namespaced_config_map(self, name: str, namespace: str) -> None:
        await super().delete_namespaced_config_map(name, namespace)
        self.by_name.pop(name, None)


class NetworkingApi:
    def __init__(self) -> None:
        self.policies: list[dict[str, object]] = []
        self.deleted: list[str] = []
        self.active: set[str] = set()

    async def create_namespaced_network_policy(
        self, namespace: str, body: dict[str, object]
    ) -> None:
        assert namespace == "exomem-system"
        self.policies.append(body)
        self.active.add(str(body["metadata"]["name"]))  # type: ignore[index]

    async def delete_namespaced_network_policy(self, name: str, namespace: str) -> None:
        assert namespace == "exomem-system"
        if name not in self.active:
            raise NotFound()
        self.active.remove(name)
        self.deleted.append(name)


class FailingNetworkingApi(NetworkingApi):
    async def create_namespaced_network_policy(self, namespace, body) -> None:
        raise RuntimeError("network policy write failed")


class ArchiveStager:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, str, str]] = []

    async def stage(self, path: Path, *, operation_id: str, archive_sha256: str):
        self.calls.append((path, operation_id, archive_sha256))
        return SimpleNamespace(
            url="https://s3.example.invalid/presigned?signature=secret",
            allowed_host="s3.example.invalid",
            identity_sha256=hashlib.sha256(f"{operation_id}:{archive_sha256}".encode()).hexdigest(),
        )


def _binding() -> CandidateRestoreBinding:
    return CandidateRestoreBinding(
        namespace="exomem-system",
        service_account="privileged-restore-worker",
        target_pvc="cell-candidate-alpha-data",
        credential_secret="exomem-cell-credentials",
        tenant_id="vault-logical-alpha",
        cell_id="cell-candidate-alpha",
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


class CandidateController:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def ensure_offline(self, binding: CandidateRestoreBinding) -> None:
        assert binding == _binding()
        self.events.append("offline")

    async def promote(self, binding: CandidateRestoreBinding) -> None:
        assert binding == _binding()
        self.events.append("promote")


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
        }

    async def finalize_candidate(self, cell_id: str) -> dict[str, bool]:
        assert cell_id == "cell-candidate-alpha"
        return {
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
    controller = CandidateController()
    runtime = KubernetesOfflineRestoreRuntime(
        batch_api=BatchApi(),
        core_api=CoreApi(),
        candidate_controller=controller,
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
    assert controller.events == ["offline"]
    assert await runtime.authenticated_readiness("cell-candidate-alpha") is True
    assert all((await runtime.product_checks("cell-candidate-alpha")).values())
    assert all((await runtime.finalize_candidate("cell-candidate-alpha")).values())


@pytest.mark.asyncio
async def test_restore_job_uses_normative_argv_fixed_request_file_and_immutable_image(
    tmp_path: Path,
) -> None:
    batch = BatchApi()
    core = CoreApi()
    networking = NetworkingApi()
    stager = ArchiveStager()
    controller = CandidateController()
    archive = tmp_path / "source.zip"
    archive.write_bytes(b"portable archive")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    runtime = KubernetesOfflineRestoreRuntime(
        batch_api=batch,
        core_api=core,
        networking_api=networking,
        candidate_controller=controller,
        archive_stager=stager,
        binding=_binding(),
        image="ghcr.io/artexis10/exomem@sha256:" + "a" * 64,
        staging_image="ghcr.io/artexis10/exomem-provisioner@sha256:" + "b" * 64,
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
    assert stager.calls == [(archive, "restore-operation-alpha", digest)]
    assert len(core.secrets) == 1
    source_secret = core.secrets[0]
    assert source_secret["stringData"] == {
        "url": "https://s3.example.invalid/presigned?signature=secret"
    }
    assert core.deleted_secrets == [source_secret["metadata"]["name"]]  # type: ignore[index]
    assert core.deleted_config_maps == [core.config_maps[0]["metadata"]["name"]]  # type: ignore[index]
    job = batch.jobs[0]
    assert job["spec"]["ttlSecondsAfterFinished"] == 300  # type: ignore[index]
    assert batch.deleted_jobs == [job["metadata"]["name"]]  # type: ignore[index]
    assert batch.active_names == set()
    assert core.deleted_pods == [f"{job['metadata']['name']}-abcde"]  # type: ignore[index]
    assert core.active_restore_pod is False
    pod = job["spec"]["template"]["spec"]  # type: ignore[index]
    container = pod["containers"][0]
    init = pod["initContainers"][0]
    assert init["image"] == "ghcr.io/artexis10/exomem-provisioner@sha256:" + "b" * 64
    assert init["command"] == [
        "exomem-restore-fetch",
        "--url-file",
        "/run/exomem/restore-source/url",
        "--output",
        "/system-scratch/source.zip",
        "--expected-sha256",
        digest,
        "--expected-size",
        str(archive.stat().st_size),
        "--allowed-host",
        "s3.example.invalid",
    ]
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
    scratch_volume = next(volume for volume in pod["volumes"] if volume["name"] == "system-scratch")
    assert scratch_volume == {"name": "system-scratch", "emptyDir": {"sizeLimit": "6Gi"}}
    assert not any(
        "persistentVolumeClaim" in volume
        for volume in pod["volumes"]
        if volume["name"] == "system-scratch"
    )
    credential_mount = next(
        mount for mount in container["volumeMounts"] if mount["name"] == "credentials"
    )
    assert credential_mount == {
        "name": "credentials",
        "mountPath": "/run/exomem/credentials",
        "readOnly": True,
    }
    assert len(networking.policies) == 1
    assert networking.deleted == [networking.policies[0]["metadata"]["name"]]  # type: ignore[index]
    assert pod["restartPolicy"] == "Never"
    assert controller.events == ["promote"]


@pytest.mark.asyncio
async def test_restore_cleanup_deletes_orphaned_plaintext_pod_after_job_is_absent() -> None:
    core = CoreApi()
    core.active_restore_pod = True
    runtime = KubernetesOfflineRestoreRuntime(
        batch_api=BatchApi(),
        core_api=core,
        networking_api=NetworkingApi(),
        binding=_binding(),
        image="ghcr.io/artexis10/exomem@sha256:" + "a" * 64,
        staging_image="ghcr.io/artexis10/exomem-provisioner@sha256:" + "b" * 64,
        poll_seconds=0,
    )
    job_name = "restore-" + "a" * 20

    await runtime._delete_job_and_wait(job_name)

    assert core.deleted_pods == [f"{job_name}-abcde"]
    assert core.active_restore_pod is False


@pytest.mark.asyncio
async def test_restore_job_failure_never_claims_publication(tmp_path: Path) -> None:
    archive = tmp_path / "source.zip"
    archive.write_bytes(b"portable archive")
    batch = BatchApi(failed=True)
    core = CoreApi()
    networking = NetworkingApi()
    runtime = KubernetesOfflineRestoreRuntime(
        batch_api=batch,
        core_api=core,
        networking_api=networking,
        archive_stager=ArchiveStager(),
        binding=_binding(),
        image="ghcr.io/artexis10/exomem@sha256:" + "a" * 64,
        staging_image="ghcr.io/artexis10/exomem-provisioner@sha256:" + "b" * 64,
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
    assert batch.active_names == set()
    assert len(batch.deleted_jobs) == 1
    assert core.active_config_maps == set()
    assert core.active_secrets == set()
    assert networking.active == set()


@pytest.mark.asyncio
async def test_partial_restore_setup_failure_cleans_source_secret_and_request(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "source.zip"
    archive.write_bytes(b"portable archive")
    batch = BatchApi()
    core = CoreApi()
    networking = FailingNetworkingApi()
    runtime = KubernetesOfflineRestoreRuntime(
        batch_api=batch,
        core_api=core,
        networking_api=networking,
        archive_stager=ArchiveStager(),
        binding=_binding(),
        image="ghcr.io/artexis10/exomem@sha256:" + "a" * 64,
        staging_image="ghcr.io/artexis10/exomem-provisioner@sha256:" + "b" * 64,
        poll_seconds=0,
    )

    with pytest.raises(RuntimeError, match="network policy write failed"):
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

    assert batch.jobs == []
    assert core.active_config_maps == set()
    assert core.active_secrets == set()


@pytest.mark.asyncio
async def test_restore_job_replay_recreates_a_clean_attempt_after_lost_ack(
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
        networking_api=NetworkingApi(),
        candidate_controller=CandidateController(),
        archive_stager=ArchiveStager(),
        binding=_binding(),
        image="ghcr.io/artexis10/exomem@sha256:" + "a" * 64,
        staging_image="ghcr.io/artexis10/exomem-provisioner@sha256:" + "b" * 64,
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

    assert len(batch.jobs) == 2
    assert len(core.config_maps) == 2
    assert batch.jobs[0]["metadata"]["name"] == batch.jobs[1]["metadata"]["name"]  # type: ignore[index]
    assert batch.active_names == set()


@pytest.mark.asyncio
async def test_restore_job_replay_deletes_a_stale_substituted_source_before_recreating(
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
        networking_api=NetworkingApi(),
        candidate_controller=CandidateController(),
        archive_stager=ArchiveStager(),
        binding=_binding(),
        image="ghcr.io/artexis10/exomem@sha256:" + "a" * 64,
        staging_image="ghcr.io/artexis10/exomem-provisioner@sha256:" + "b" * 64,
        poll_seconds=0,
    )
    arguments = {
        "helper_version": "1",
        "release_version": "0.22.0",
        "operation_id": "restore-operation-alpha",
        "fence_generation": 10,
        "source_cell_id": "cell-source-alpha",
        "archive_sha256": digest,
        "artifact_reference": "recovery_opaque_source",
    }
    await runtime.offline_restore("cell-candidate-alpha", archive, **arguments)
    secret = copy.deepcopy(core.secrets[0])
    secret_name = secret["metadata"]["name"]  # type: ignore[index]
    secret["stringData"]["url"] = (  # type: ignore[index]
        "https://s3.example.invalid/substituted?signature=secret"
    )
    core.by_name[secret_name] = secret
    core.active_secrets.add(secret_name)

    await runtime.offline_restore("cell-candidate-alpha", archive, **arguments)

    assert secret_name in core.deleted_secrets
    assert core.secrets[-1]["stringData"]["url"].endswith("?signature=secret")  # type: ignore[index]


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
        networking_api=NetworkingApi(),
        candidate_controller=CandidateController(),
        archive_stager=ArchiveStager(),
        binding=_binding(),
        image="ghcr.io/artexis10/exomem@sha256:" + "a" * 64,
        staging_image="ghcr.io/artexis10/exomem-provisioner@sha256:" + "b" * 64,
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
