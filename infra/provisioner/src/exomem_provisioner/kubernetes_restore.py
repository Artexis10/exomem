"""Exact Kubernetes Job adapter for Exomem's hosted restore-candidate operator contract."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
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
class StagedPortableArchive:
    url: str
    allowed_host: str
    identity_sha256: str

    def __post_init__(self) -> None:
        from urllib.parse import urlsplit

        parsed = urlsplit(self.url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != self.allowed_host
            or parsed.username
            or parsed.password
            or parsed.fragment
            or not parsed.query
            or not re.fullmatch(r"[0-9a-f]{64}", self.identity_sha256)
        ):
            raise ValueError("restore staging URL is not an exact presigned HTTPS URL")


class PortableArchiveStager(Protocol):
    async def stage(
        self,
        path: Path,
        *,
        operation_id: str,
        archive_sha256: str,
    ) -> StagedPortableArchive: ...


@dataclass(frozen=True, slots=True)
class CandidateRestoreBinding:
    namespace: str
    service_account: str
    target_pvc: str
    credential_secret: str
    tenant_id: str
    cell_id: str
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
        if self.tenant_id != self.source_vault_id or self.cell_id == self.source_vault_id:
            raise ValueError("restore binding must separate physical and logical identity")
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

    async def finalize_candidate(self, cell_id: str) -> dict[str, bool]: ...


class CandidateController(Protocol):
    async def ensure_offline(self, binding: CandidateRestoreBinding) -> None: ...

    async def promote(self, binding: CandidateRestoreBinding) -> None: ...


class KubernetesOfflineRestoreRuntime:
    """Runs the immutable image's normative offline restore helper as a pinned Job."""

    REQUEST_PATH = "/run/exomem/operator-requests/restore-candidate.json"
    SCRATCH_ROOT = "/system-scratch"

    def __init__(
        self,
        *,
        batch_api: Any,
        core_api: Any,
        networking_api: Any | None = None,
        candidate_controller: CandidateController | None = None,
        candidate_probe: CandidateProbe | None = None,
        archive_stager: PortableArchiveStager | None = None,
        binding: CandidateRestoreBinding,
        image: str,
        staging_image: str | None = None,
        release_version: str = "",
        poll_seconds: float = 1.0,
        timeout_polls: int = 900,
    ) -> None:
        if not _IMAGE_DIGEST.fullmatch(image):
            raise ValueError("restore image must be an immutable OCI digest")
        if staging_image is not None and not _IMAGE_DIGEST.fullmatch(staging_image):
            raise ValueError("restore staging image must be an immutable OCI digest")
        self._batch = batch_api
        self._core = core_api
        self._networking = networking_api
        self._candidate_controller = candidate_controller
        self._candidate_probe = candidate_probe
        self._archive_stager = archive_stager
        self._binding = binding
        self._image = image
        self._staging_image = staging_image
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
        if (
            not _OPAQUE.fullmatch(candidate_cell_id)
            or candidate_cell_id != self._binding.cell_id
            or self._candidate_controller is None
        ):
            raise RestoreJobFailed("candidate offline capability is unavailable")
        await self._candidate_controller.ensure_offline(self._binding)

    async def authenticated_readiness(self, candidate_cell_id: str) -> bool:
        if self._candidate_probe is None:
            raise RestoreJobFailed("candidate readiness capability is unavailable")
        return await self._candidate_probe.authenticated_readiness(candidate_cell_id)

    async def product_checks(self, candidate_cell_id: str) -> dict[str, bool]:
        if self._candidate_probe is None:
            raise RestoreJobFailed("candidate product-check capability is unavailable")
        return await self._candidate_probe.product_checks(candidate_cell_id)

    async def finalize_candidate(self, candidate_cell_id: str) -> dict[str, bool]:
        if self._candidate_probe is None:
            raise RestoreJobFailed("candidate finalization capability is unavailable")
        return await self._candidate_probe.finalize_candidate(candidate_cell_id)

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
        if self._archive_stager is None or self._staging_image is None or self._networking is None:
            raise RestoreJobFailed("remote restore staging capability is unavailable")
        suffix = hashlib.sha256(
            f"{operation_id}:{candidate_cell_id}:{fence_generation}".encode()
        ).hexdigest()[:20]
        job_name = f"restore-{suffix}"
        config_name = f"{job_name}-request"
        source_name = f"{job_name}-source"
        egress_name = f"{job_name}-egress"
        await self._cleanup_attempt(job_name, config_name, source_name, egress_name)
        staged = await self._archive_stager.stage(
            archive_path,
            operation_id=operation_id,
            archive_sha256=archive_sha256,
        )
        if not isinstance(staged, StagedPortableArchive):
            staged = StagedPortableArchive(
                url=str(staged.url),
                allowed_host=str(staged.allowed_host),
                identity_sha256=str(staged.identity_sha256),
            )
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
            "exomem.io/cell": self._binding.namespace,
            "exomem.io/restore-candidate": "true",
            "exomem.io/cell-id": candidate_cell_id,
            "exomem.io/operation-digest": hashlib.sha256(operation_id.encode()).hexdigest()[:32],
            "exomem.io/fence": str(fence_generation),
        }
        annotations = {
            "exomem.io/tenant-id": self._binding.tenant_id,
            "exomem.io/cell-id": candidate_cell_id,
            "exomem.io/operation-id": operation_id,
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
                "annotations": {
                    **annotations,
                    "exomem.io/restore-request-sha256": request_digest,
                },
            },
            "immutable": True,
            "data": {"restore-candidate.json": request_json},
        }
        await self._attempt_step(
            self._create_or_adopt(
                api=self._core,
                create_method="create_namespaced_config_map",
                read_method="read_namespaced_config_map",
                name=config_name,
                body=config_map,
                digest_annotation="exomem.io/restore-request-sha256",
                expected_digest=request_digest,
            ),
            job_name,
            config_name,
            source_name,
            egress_name,
        )
        source_digest = hashlib.sha256(staged.url.encode()).hexdigest()
        source_secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": source_name,
                "namespace": self._binding.namespace,
                "labels": labels,
                "annotations": {
                    **annotations,
                    "exomem.io/restore-source-sha256": source_digest,
                    "exomem.io/restore-object-identity-sha256": staged.identity_sha256,
                },
            },
            "type": "Opaque",
            "immutable": True,
            "stringData": {"url": staged.url},
        }
        staged = await self._attempt_step(
            self._create_or_adopt_source(name=source_name, body=source_secret, staged=staged),
            job_name,
            config_name,
            source_name,
            egress_name,
        )
        egress = self._egress_policy(egress_name, labels, annotations)
        egress_digest = hashlib.sha256(
            json.dumps(egress, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        egress["metadata"]["annotations"]["exomem.io/restore-egress-sha256"] = (  # type: ignore[index]
            egress_digest
        )
        await self._attempt_step(
            self._create_or_adopt(
                api=self._networking,
                create_method="create_namespaced_network_policy",
                read_method="read_namespaced_network_policy",
                name=egress_name,
                body=egress,
                digest_annotation="exomem.io/restore-egress-sha256",
                expected_digest=egress_digest,
            ),
            job_name,
            config_name,
            source_name,
            egress_name,
        )
        job = self._job(
            job_name,
            config_name,
            source_name,
            labels,
            annotations,
            archive_path.name,
            archive_sha256=archive_sha256,
            archive_size=archive_path.stat().st_size,
            allowed_host=staged.allowed_host,
        )
        job_digest = hashlib.sha256(
            json.dumps(job, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        job["metadata"]["annotations"] = {  # type: ignore[index]
            **annotations,
            "exomem.io/restore-job-sha256": job_digest,
        }
        await self._attempt_step(
            self._create_or_adopt(
                api=self._batch,
                create_method="create_namespaced_job",
                read_method="read_namespaced_job",
                name=job_name,
                body=job,
                digest_annotation="exomem.io/restore-job-sha256",
                expected_digest=job_digest,
            ),
            job_name,
            config_name,
            source_name,
            egress_name,
        )
        try:
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
        finally:
            await self._cleanup_attempt(job_name, config_name, source_name, egress_name)
        if self._candidate_controller is None:
            raise RestoreJobFailed("candidate promotion capability is unavailable")
        await self._candidate_controller.promote(self._binding)

    def _job(
        self,
        job_name: str,
        config_name: str,
        source_name: str,
        labels: dict[str, str],
        annotations: dict[str, str],
        archive_name: str,
        *,
        archive_sha256: str,
        archive_size: int,
        allowed_host: str,
    ) -> dict[str, Any]:
        init_container = {
            "name": "fetch-restore-source",
            "image": self._staging_image,
            "imagePullPolicy": "IfNotPresent",
            "command": [
                "exomem-restore-fetch",
                "--url-file",
                "/run/exomem/restore-source/url",
                "--output",
                f"{self.SCRATCH_ROOT}/{archive_name}",
                "--expected-sha256",
                archive_sha256,
                "--expected-size",
                str(archive_size),
                "--allowed-host",
                allowed_host,
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
                "requests": {"cpu": "25m", "memory": "64Mi"},
                "limits": {"cpu": "500m", "memory": "256Mi"},
            },
            "volumeMounts": [
                {
                    "name": "restore-source",
                    "mountPath": "/run/exomem/restore-source",
                    "readOnly": True,
                },
                {"name": "system-scratch", "mountPath": self.SCRATCH_ROOT},
                {"name": "tmp", "mountPath": "/tmp"},
            ],
        }
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
                {
                    "name": "credentials",
                    "mountPath": "/run/exomem/credentials",
                    "readOnly": True,
                },
                {"name": "tmp", "mountPath": "/tmp"},
            ],
        }
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "namespace": self._binding.namespace,
                "labels": labels,
                "annotations": annotations,
            },
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": 3600,
                "ttlSecondsAfterFinished": 300,
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
                        "initContainers": [init_container],
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
                                "name": "restore-source",
                                "secret": {
                                    "secretName": source_name,
                                    "defaultMode": 0o400,
                                    "items": [{"key": "url", "path": "url", "mode": 0o400}],
                                },
                            },
                            {
                                "name": "system-scratch",
                                "emptyDir": {"sizeLimit": "6Gi"},
                            },
                            {
                                "name": "target-data",
                                "persistentVolumeClaim": {"claimName": self._binding.target_pvc},
                            },
                            {
                                "name": "credentials",
                                "secret": {
                                    "secretName": self._binding.credential_secret,
                                    "defaultMode": 0o444,
                                },
                            },
                            {"name": "tmp", "emptyDir": {"sizeLimit": "512Mi"}},
                        ],
                    },
                },
            },
        }

    async def _wait_for_success(self, job_name: str) -> None:
        for _ in range(self._timeout_polls):
            job = await self._api_call(
                self._batch,
                "read_namespaced_job_status",
                job_name,
                self._binding.namespace,
            )
            if int(getattr(job.status, "succeeded", 0) or 0) == 1:
                return
            if int(getattr(job.status, "failed", 0) or 0) > 0:
                raise RestoreJobFailed("restore Job failed")
            await asyncio.sleep(self._poll_seconds)
        raise RestoreJobFailed("restore Job exceeded its observation timeout")

    async def _read_response(self, job_name: str) -> dict[str, Any]:
        pods = await self._api_call(
            self._core,
            "list_namespaced_pod",
            self._binding.namespace,
            label_selector=f"job-name={job_name}",
        )
        if len(pods.items) != 1:
            raise RestoreJobFailed("restore Job has no unique result pod")
        raw = await self._api_call(
            self._core,
            "read_namespaced_pod_log",
            pods.items[0].metadata.name,
            self._binding.namespace,
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
            await self._api_call(api, create_method, self._binding.namespace, body)
            return
        except Exception as error:
            if getattr(error, "status", None) != 409:
                raise
        existing = await self._api_call(api, read_method, name, self._binding.namespace)
        if self._annotation(existing, digest_annotation) != expected_digest:
            raise RestoreJobFailed("existing restore resource identity differs")

    async def _create_or_adopt_source(
        self,
        *,
        name: str,
        body: dict[str, Any],
        staged: StagedPortableArchive,
    ) -> StagedPortableArchive:
        try:
            await self._api_call(
                self._core,
                "create_namespaced_secret",
                self._binding.namespace,
                body,
            )
            return staged
        except Exception as error:
            if getattr(error, "status", None) != 409:
                raise
        existing = await self._api_call(
            self._core,
            "read_namespaced_secret",
            name,
            self._binding.namespace,
        )
        url = self._secret_text(existing, "url")
        from urllib.parse import urlsplit

        current = urlsplit(url)
        expected = urlsplit(staged.url)
        digest = hashlib.sha256(url.encode()).hexdigest()
        if (
            self._annotation(existing, "exomem.io/restore-source-sha256") != digest
            or self._annotation(existing, "exomem.io/restore-object-identity-sha256")
            != staged.identity_sha256
            or current.scheme != "https"
            or current.hostname != staged.allowed_host
            or current.hostname != expected.hostname
            or current.port != expected.port
            or current.path != expected.path
            or current.username
            or current.password
            or current.fragment
            or not current.query
        ):
            raise RestoreJobFailed("existing restore source identity differs")
        return StagedPortableArchive(
            url=url,
            allowed_host=staged.allowed_host,
            identity_sha256=staged.identity_sha256,
        )

    @staticmethod
    def _secret_text(resource: Any, key: str) -> str:
        if isinstance(resource, dict):
            string_data = resource.get("stringData", {})
            data = resource.get("data", {})
        else:
            string_data = getattr(resource, "string_data", None) or {}
            data = getattr(resource, "data", None) or {}
        value = string_data.get(key) if isinstance(string_data, dict) else None
        if isinstance(value, str):
            return value
        encoded = data.get(key) if isinstance(data, dict) else None
        if not isinstance(encoded, str):
            raise RestoreJobFailed("existing restore source is unavailable")
        try:
            return base64.b64decode(encoded, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as error:
            raise RestoreJobFailed("existing restore source is unavailable") from error

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

    async def _delete_if_present(self, api: Any, method: str, name: str) -> None:
        try:
            await self._api_call(api, method, name, self._binding.namespace)
        except Exception as error:
            if getattr(error, "status", None) != 404:
                raise

    async def _attempt_step(
        self,
        awaitable: Any,
        job_name: str,
        config_name: str,
        source_name: str,
        egress_name: str,
    ) -> Any:
        try:
            return await awaitable
        except BaseException:  # noqa: BLE001 - cleanup must also run on cancellation
            await self._cleanup_attempt(job_name, config_name, source_name, egress_name)
            raise

    async def _cleanup_attempt(
        self,
        job_name: str,
        config_name: str,
        source_name: str,
        egress_name: str,
    ) -> None:
        await self._delete_job_and_wait(job_name)
        await self._delete_if_present(self._core, "delete_namespaced_secret", source_name)
        await self._delete_if_present(
            self._networking,
            "delete_namespaced_network_policy",
            egress_name,
        )
        await self._delete_if_present(self._core, "delete_namespaced_config_map", config_name)

    async def _delete_job_and_wait(self, name: str) -> None:
        job_absent = False
        try:
            await self._api_call(
                self._batch,
                "delete_namespaced_job",
                name,
                self._binding.namespace,
                body={"gracePeriodSeconds": 0, "propagationPolicy": "Foreground"},
            )
        except Exception as error:
            if getattr(error, "status", None) != 404:
                raise
            job_absent = True
        if not job_absent:
            for _ in range(self._timeout_polls):
                try:
                    await self._api_call(
                        self._batch,
                        "read_namespaced_job",
                        name,
                        self._binding.namespace,
                    )
                except Exception as error:
                    if getattr(error, "status", None) == 404:
                        job_absent = True
                        break
                    raise
                await asyncio.sleep(self._poll_seconds)
        if not job_absent:
            raise RestoreJobFailed("restore Job cleanup did not complete")

        for _ in range(self._timeout_polls):
            pods = await self._api_call(
                self._core,
                "list_namespaced_pod",
                self._binding.namespace,
                label_selector=f"job-name={name}",
            )
            if not pods.items:
                return
            for pod in pods.items:
                pod_name, labels = self._pod_identity(pod)
                if (
                    labels.get("job-name") != name
                    or labels.get("app.kubernetes.io/name") != "exomem-restore-candidate"
                    or labels.get("exomem.io/restore-candidate") != "true"
                    or labels.get("exomem.io/cell") != self._binding.namespace
                    or labels.get("exomem.io/cell-id") != self._binding.cell_id
                    or not re.fullmatch(
                        r"[a-f0-9]{32}", labels.get("exomem.io/operation-digest", "")
                    )
                    or not re.fullmatch(r"[1-9][0-9]{0,15}", labels.get("exomem.io/fence", ""))
                    or not re.fullmatch(rf"{re.escape(name)}-[a-z0-9]{{5}}", pod_name)
                ):
                    raise RestoreJobFailed("restore plaintext Pod identity differs")
                try:
                    await self._api_call(
                        self._core,
                        "delete_namespaced_pod",
                        pod_name,
                        self._binding.namespace,
                        body={
                            "gracePeriodSeconds": 0,
                            "propagationPolicy": "Foreground",
                        },
                    )
                except Exception as error:
                    if getattr(error, "status", None) != 404:
                        raise
            await asyncio.sleep(self._poll_seconds)
        raise RestoreJobFailed("restore plaintext Pod cleanup did not complete")

    @staticmethod
    def _pod_identity(pod: Any) -> tuple[str, dict[str, str]]:
        if isinstance(pod, dict):
            metadata = pod.get("metadata", {})
            name = metadata.get("name") if isinstance(metadata, dict) else None
            raw_labels = metadata.get("labels", {}) if isinstance(metadata, dict) else {}
        else:
            metadata = getattr(pod, "metadata", None)
            name = getattr(metadata, "name", None)
            raw_labels = getattr(metadata, "labels", None) or {}
        if not isinstance(name, str) or not isinstance(raw_labels, dict):
            raise RestoreJobFailed("restore plaintext Pod identity is unavailable")
        labels = {
            str(key): str(value)
            for key, value in raw_labels.items()
            if isinstance(key, str) and isinstance(value, str)
        }
        return name, labels

    @staticmethod
    async def _api_call(api: Any, method: str, *args: Any, **kwargs: Any) -> Any:
        call = getattr(api, method)
        if inspect.iscoroutinefunction(call):
            return await call(*args, **kwargs)
        return await asyncio.to_thread(call, *args, **kwargs)

    def _egress_policy(
        self,
        name: str,
        labels: dict[str, str],
        annotations: dict[str, str],
    ) -> dict[str, Any]:
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {
                "name": name,
                "namespace": self._binding.namespace,
                "labels": labels,
                "annotations": dict(annotations),
            },
            "spec": {
                "podSelector": {"matchLabels": labels},
                "policyTypes": ["Egress"],
                "egress": [
                    {
                        "to": [
                            {
                                "namespaceSelector": {
                                    "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                                },
                                "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
                            }
                        ],
                        "ports": [
                            {"protocol": "UDP", "port": 53},
                            {"protocol": "TCP", "port": 53},
                        ],
                    },
                    {
                        "to": [
                            {
                                "ipBlock": {
                                    "cidr": "0.0.0.0/0",
                                    "except": [
                                        "10.0.0.0/8",
                                        "100.64.0.0/10",
                                        "127.0.0.0/8",
                                        "169.254.0.0/16",
                                        "172.16.0.0/12",
                                        "192.168.0.0/16",
                                    ],
                                }
                            }
                        ],
                        "ports": [{"protocol": "TCP", "port": 443}],
                    },
                ],
            },
        }

    @staticmethod
    def _deterministic_uuid4(operation_id: str, candidate_cell_id: str) -> str:
        raw = bytearray(
            hashlib.sha256(f"{operation_id}:{candidate_cell_id}".encode()).digest()[:16]
        )
        raw[6] = (raw[6] & 0x0F) | 0x40
        raw[8] = (raw[8] & 0x3F) | 0x80
        return str(uuid.UUID(bytes=bytes(raw)))
