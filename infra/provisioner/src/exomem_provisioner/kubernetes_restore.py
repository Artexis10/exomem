"""Exact Kubernetes Job adapter for Exomem's hosted restore-candidate operator contract."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

_IMAGE_DIGEST = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class RestoreJobFailed(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CandidateRestoreBinding:
    namespace: str
    service_account: str
    scratch_pvc: str
    target_pvc: str
    source_vault_id: str
    target_vault_id: str
    target_vault_root: str
    target_state_root: str
    target_log_root: str
    runtime_uid: int
    runtime_gid: int
    active_credential_version: str
    expected_protocol: str
    workload_name: str = "exomem"

    def __post_init__(self) -> None:
        if self.source_vault_id != self.target_vault_id:
            raise ValueError("restore must preserve the logical vault identity")
        if any(
            not value.startswith("/")
            for value in (self.target_vault_root, self.target_state_root, self.target_log_root)
        ):
            raise ValueError("candidate roots must be absolute")
        if len({self.target_vault_root, self.target_state_root, self.target_log_root}) != 3:
            raise ValueError("candidate roots must be pairwise distinct")
        if not _OPAQUE.fullmatch(self.workload_name):
            raise ValueError("candidate workload name is invalid")


class CandidateProbe(Protocol):
    async def authenticated_readiness(self, cell_id: str) -> bool: ...

    async def product_checks(self, cell_id: str) -> dict[str, bool]: ...


class KubernetesOfflineRestoreRuntime:
    """Runs the immutable image's normative offline restore helper as a pinned Job."""

    REQUEST_PATH = "/run/exomem/operator-requests/restore-candidate.json"
    SCRATCH_ROOT = "/system-scratch"

    def __init__(
        self,
        *,
        batch_api: Any,
        core_api: Any,
        apps_api: Any | None = None,
        candidate_probe: CandidateProbe | None = None,
        binding: CandidateRestoreBinding,
        image: str,
        release_version: str = "",
        poll_seconds: float = 1.0,
        timeout_polls: int = 900,
    ) -> None:
        if not _IMAGE_DIGEST.fullmatch(image):
            raise ValueError("restore image must be an immutable OCI digest")
        self._batch = batch_api
        self._core = core_api
        self._apps = apps_api
        self._candidate_probe = candidate_probe
        self._binding = binding
        self._image = image
        self._release_version = release_version
        self._poll_seconds = poll_seconds
        self._timeout_polls = timeout_polls

    async def inspect_portable_archive(self, path: Path):
        from .durability import PortableArchiveInspection

        def inspect() -> PortableArchiveInspection:
            if not path.is_file() or path.stat().st_size == 0:
                raise RestoreJobFailed("portable archive is unavailable")
            try:
                with zipfile.ZipFile(path) as archive:
                    names = archive.namelist()
                    path_safe = all(self._safe_archive_path(name) for name in names)
                    if "manifest.json" not in names:
                        raise RestoreJobFailed("portable archive manifest is unavailable")
                    manifest_bytes = archive.read("manifest.json")
            except (OSError, zipfile.BadZipFile, KeyError) as error:
                raise RestoreJobFailed("portable archive inspection failed") from error
            if len(manifest_bytes) > 64 * 1024:
                raise RestoreJobFailed("portable archive manifest exceeds safety bound")
            try:
                manifest = json.loads(manifest_bytes)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise RestoreJobFailed("portable archive manifest is invalid") from error
            if not isinstance(manifest, dict):
                raise RestoreJobFailed("portable archive manifest root is invalid")
            source_cell_id = manifest.get("sourceCellId")
            if not isinstance(source_cell_id, str) or not _OPAQUE.fullmatch(source_cell_id):
                raise RestoreJobFailed("portable archive source identity is invalid")
            return PortableArchiveInspection(
                manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                source_cell_id=source_cell_id,
                hosted_state_included=manifest.get("hostedStateIncluded") is not False,
                path_safe=path_safe,
                schema_compatible=manifest.get("schemaVersion") == 1,
                release_compatible=(
                    bool(self._release_version)
                    and manifest.get("releaseVersion") == self._release_version
                ),
            )

        return await asyncio.to_thread(inspect)

    async def stop_candidate(self, candidate_cell_id: str) -> None:
        if not _OPAQUE.fullmatch(candidate_cell_id) or self._apps is None:
            raise RestoreJobFailed("candidate workload stop capability is unavailable")
        await self._apps.patch_namespaced_stateful_set_scale(
            self._binding.workload_name,
            self._binding.namespace,
            {"spec": {"replicas": 0}},
        )
        scale = await self._apps.read_namespaced_stateful_set_scale(
            self._binding.workload_name,
            self._binding.namespace,
        )
        if int(getattr(scale.status, "replicas", -1)) != 0:
            raise RestoreJobFailed("candidate workload did not stop")

    async def authenticated_readiness(self, candidate_cell_id: str) -> bool:
        if self._candidate_probe is None:
            raise RestoreJobFailed("candidate readiness capability is unavailable")
        return await self._candidate_probe.authenticated_readiness(candidate_cell_id)

    async def product_checks(self, candidate_cell_id: str) -> dict[str, bool]:
        if self._candidate_probe is None:
            raise RestoreJobFailed("candidate product-check capability is unavailable")
        return await self._candidate_probe.product_checks(candidate_cell_id)

    @staticmethod
    def _safe_archive_path(value: str) -> bool:
        if not value or "\\" in value:
            return False
        path = PurePosixPath(value)
        return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)

    async def offline_restore(
        self,
        candidate_cell_id: str,
        archive_path: Path,
        *,
        helper_version: str,
        release_version: str,
        operation_id: str,
        fence_generation: int,
        source_cell_id: str,
        archive_sha256: str,
        artifact_reference: str,
    ) -> None:
        if helper_version != "1":
            raise RestoreJobFailed("unsupported hosted operator contract version")
        if not _OPAQUE.fullmatch(candidate_cell_id) or not _OPAQUE.fullmatch(source_cell_id):
            raise RestoreJobFailed("restore cell identity is invalid")
        if candidate_cell_id == source_cell_id:
            raise RestoreJobFailed("restore target must differ from source")
        if not archive_path.is_file() or self._sha256(archive_path) != archive_sha256:
            raise RestoreJobFailed("restore archive digest differs before Job creation")
        suffix = hashlib.sha256(
            f"{operation_id}:{candidate_cell_id}:{fence_generation}".encode()
        ).hexdigest()[:20]
        job_name = f"restore-{suffix}"
        config_name = f"{job_name}-request"
        request = {
            "request_id": self._deterministic_uuid4(operation_id, candidate_cell_id),
            "operation_id": operation_id,
            "artifact_reference": artifact_reference,
            "archive_path": f"{self.SCRATCH_ROOT}/{archive_path.name}",
            "expected_archive_sha256": archive_sha256,
            "source_cell_id": source_cell_id,
            "source_vault_id": self._binding.source_vault_id,
            "target_cell_id": candidate_cell_id,
            "target_vault_id": self._binding.target_vault_id,
            "target_vault_root": self._binding.target_vault_root,
            "target_state_root": self._binding.target_state_root,
            "target_log_root": self._binding.target_log_root,
            "expected_release": release_version,
            "expected_protocol": self._binding.expected_protocol,
            "runtime_uid": self._binding.runtime_uid,
            "runtime_gid": self._binding.runtime_gid,
            "active_credential_version": self._binding.active_credential_version,
            "routing_stopped": True,
            "workload_stopped": True,
        }
        labels = {
            "app.kubernetes.io/name": "exomem-restore-candidate",
            "exomem.io/cell-id": candidate_cell_id,
            "exomem.io/operation-digest": hashlib.sha256(operation_id.encode()).hexdigest()[:32],
            "exomem.io/fence": str(fence_generation),
        }
        request_json = json.dumps(request, sort_keys=True, separators=(",", ":"))
        request_digest = hashlib.sha256(request_json.encode()).hexdigest()
        config_map = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": config_name,
                "namespace": self._binding.namespace,
                "labels": labels,
                "annotations": {"exomem.io/restore-request-sha256": request_digest},
            },
            "immutable": True,
            "data": {"restore-candidate.json": request_json},
        }
        await self._create_or_adopt(
            api=self._core,
            create_method="create_namespaced_config_map",
            read_method="read_namespaced_config_map",
            name=config_name,
            body=config_map,
            digest_annotation="exomem.io/restore-request-sha256",
            expected_digest=request_digest,
        )
        job = self._job(job_name, config_name, labels, archive_path.name)
        job_digest = hashlib.sha256(
            json.dumps(job, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        job["metadata"]["annotations"] = {  # type: ignore[index]
            "exomem.io/restore-job-sha256": job_digest
        }
        await self._create_or_adopt(
            api=self._batch,
            create_method="create_namespaced_job",
            read_method="read_namespaced_job",
            name=job_name,
            body=job,
            digest_annotation="exomem.io/restore-job-sha256",
            expected_digest=job_digest,
        )
        await self._wait_for_success(job_name)
        response = await self._read_response(job_name)
        data = response.get("data")
        if (
            response.get("ok") is not True
            or response.get("code") != "HOSTED_RESTORE_CANDIDATE_READY"
            or not isinstance(data, dict)
            or data.get("target_cell_id") != candidate_cell_id
            or data.get("archive_sha256") != archive_sha256
            or data.get("journal_phase") != "complete"
            or data.get("status") not in {"ready", "degraded"}
        ):
            raise RestoreJobFailed("restore Job returned an invalid final proof")

    def _job(
        self,
        job_name: str,
        config_name: str,
        labels: dict[str, str],
        archive_name: str,
    ) -> dict[str, Any]:
        container = {
            "name": "restore-candidate",
            "image": self._image,
            "imagePullPolicy": "IfNotPresent",
            "command": [
                "exomem",
                "hosted",
                "restore-candidate",
                "--contract-version",
                "1",
                "--request-file",
                self.REQUEST_PATH,
            ],
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
                "readOnlyRootFilesystem": True,
                "runAsNonRoot": True,
                "runAsUser": self._binding.runtime_uid,
                "runAsGroup": self._binding.runtime_gid,
            },
            "resources": {
                "requests": {"cpu": "100m", "memory": "256Mi"},
                "limits": {"cpu": "1", "memory": "1Gi"},
            },
            "volumeMounts": [
                {
                    "name": "operator-request",
                    "mountPath": self.REQUEST_PATH,
                    "subPath": "restore-candidate.json",
                    "readOnly": True,
                },
                {
                    "name": "system-scratch",
                    "mountPath": f"{self.SCRATCH_ROOT}/{archive_name}",
                    "subPath": archive_name,
                    "readOnly": True,
                },
                {"name": "target-data", "mountPath": "/var/lib/exomem"},
                {"name": "tmp", "mountPath": "/tmp"},
            ],
        }
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": job_name, "namespace": self._binding.namespace, "labels": labels},
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": 3600,
                "ttlSecondsAfterFinished": 86400,
                "template": {
                    "metadata": {"labels": {**labels, "job-name": job_name}},
                    "spec": {
                        "serviceAccountName": self._binding.service_account,
                        "automountServiceAccountToken": False,
                        "restartPolicy": "Never",
                        "securityContext": {
                            "runAsNonRoot": True,
                            "seccompProfile": {"type": "RuntimeDefault"},
                            "fsGroup": self._binding.runtime_gid,
                            "fsGroupChangePolicy": "OnRootMismatch",
                        },
                        "containers": [container],
                        "volumes": [
                            {
                                "name": "operator-request",
                                "configMap": {
                                    "name": config_name,
                                    "defaultMode": 0o444,
                                    "items": [
                                        {
                                            "key": "restore-candidate.json",
                                            "path": "restore-candidate.json",
                                            "mode": 0o444,
                                        }
                                    ],
                                },
                            },
                            {
                                "name": "system-scratch",
                                "persistentVolumeClaim": {"claimName": self._binding.scratch_pvc},
                            },
                            {
                                "name": "target-data",
                                "persistentVolumeClaim": {"claimName": self._binding.target_pvc},
                            },
                            {"name": "tmp", "emptyDir": {"sizeLimit": "512Mi"}},
                        ],
                    },
                },
            },
        }

    async def _wait_for_success(self, job_name: str) -> None:
        for _ in range(self._timeout_polls):
            job = await self._batch.read_namespaced_job_status(job_name, self._binding.namespace)
            if int(getattr(job.status, "succeeded", 0) or 0) == 1:
                return
            if int(getattr(job.status, "failed", 0) or 0) > 0:
                raise RestoreJobFailed("restore Job failed")
            await asyncio.sleep(self._poll_seconds)
        raise RestoreJobFailed("restore Job exceeded its observation timeout")

    async def _read_response(self, job_name: str) -> dict[str, Any]:
        pods = await self._core.list_namespaced_pod(
            self._binding.namespace, label_selector=f"job-name={job_name}"
        )
        if len(pods.items) != 1:
            raise RestoreJobFailed("restore Job has no unique result pod")
        raw = await self._core.read_namespaced_pod_log(
            pods.items[0].metadata.name, self._binding.namespace
        )
        if not isinstance(raw, str) or len(raw.encode()) > 65_536 or "\n" in raw.strip():
            raise RestoreJobFailed("restore Job response is not one bounded JSON line")
        try:
            response = json.loads(raw)
        except json.JSONDecodeError as error:
            raise RestoreJobFailed("restore Job response is invalid JSON") from error
        if not isinstance(response, dict):
            raise RestoreJobFailed("restore Job response root is invalid")
        return response

    async def _create_or_adopt(
        self,
        *,
        api: Any,
        create_method: str,
        read_method: str,
        name: str,
        body: dict[str, Any],
        digest_annotation: str,
        expected_digest: str,
    ) -> None:
        try:
            await getattr(api, create_method)(self._binding.namespace, body)
            return
        except Exception as error:
            if getattr(error, "status", None) != 409:
                raise
        existing = await getattr(api, read_method)(name, self._binding.namespace)
        if self._annotation(existing, digest_annotation) != expected_digest:
            raise RestoreJobFailed("existing restore resource identity differs")

    @staticmethod
    def _annotation(resource: Any, name: str) -> str | None:
        if isinstance(resource, dict):
            metadata = resource.get("metadata", {})
            annotations = metadata.get("annotations", {}) if isinstance(metadata, dict) else {}
        else:
            metadata = getattr(resource, "metadata", None)
            annotations = getattr(metadata, "annotations", None) or {}
        return str(annotations[name]) if name in annotations else None

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _deterministic_uuid4(operation_id: str, candidate_cell_id: str) -> str:
        raw = bytearray(
            hashlib.sha256(f"{operation_id}:{candidate_cell_id}".encode()).digest()[:16]
        )
        raw[6] = (raw[6] & 0x0F) | 0x40
        raw[8] = (raw[8] & 0x3F) | 0x80
        return str(uuid.UUID(bytes=bytes(raw)))
