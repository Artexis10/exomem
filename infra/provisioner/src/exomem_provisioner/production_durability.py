"""Production-only durability adapters for routine operation actions."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from .kubernetes_restore import StagedPortableArchive
from .repository import OperationRepository
from .worker import ProvisionerWorker
from .worker_ownership import DURABILITY_OPERATION_ACTIONS


def build_durability_operation_worker(
    *,
    repository: OperationRepository,
    driver: Any,
    worker_id: str,
) -> ProvisionerWorker:
    """Build the short-lived exclusive owner of user durability actions."""

    return ProvisionerWorker(
        repository,
        driver,
        worker_id=worker_id,
        allowed_actions=DURABILITY_OPERATION_ACTIONS,
    )


class LiveTargetSource(Protocol):
    async def list_backup_targets(self) -> list[Any]: ...


class ExportWorkflow(Protocol):
    async def run(
        self,
        run: Any,
        *,
        worker_id: str,
        expires_at: datetime | None = None,
    ) -> Any: ...


class RefreshingExportWorkflow:
    """Authenticate the current cell inventory immediately before an export."""

    def __init__(self, target_source: LiveTargetSource, workflow: ExportWorkflow) -> None:
        self._target_source = target_source
        self._workflow = workflow

    async def run(
        self,
        run: Any,
        *,
        worker_id: str,
        expires_at: datetime | None = None,
    ) -> Any:
        targets = await self._target_source.list_backup_targets()
        if sum(target.cell_id == run.identity.cell_id for target in targets) != 1:
            raise RuntimeError("cell is absent from authenticated durability inventory")
        return await self._workflow.run(
            run,
            worker_id=worker_id,
            expires_at=expires_at,
        )


class PortableDeliveryStore(Protocol):
    async def put_file(
        self,
        key: str,
        source: Path,
        *,
        metadata: dict[str, str],
        retain_until: datetime | None,
    ) -> Any: ...

    async def presigned_download(self, key: str, *, ttl_seconds: int) -> str: ...


class B2PortableArchiveStager:
    """Publish one short-lived plaintext restore handoff without B2 credentials in-cell."""

    def __init__(
        self,
        store: PortableDeliveryStore,
        *,
        clock: Any = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._clock = clock

    async def stage(
        self,
        path: Path,
        *,
        operation_id: str,
        archive_sha256: str,
    ) -> StagedPortableArchive:
        size = path.stat().st_size
        object_digest = hashlib.sha256(
            f"{operation_id}:{archive_sha256}:{size}".encode()
        ).hexdigest()
        nonce = secrets.token_hex(16)
        key = (
            f"user-export-delivery/restore-staging/{object_digest[:2]}/"
            f"{object_digest}-{nonce}.portable"
        )
        expires = self._clock() + timedelta(minutes=15)
        metadata = {
            "expires-at": expires.isoformat().replace("+00:00", "Z"),
            "archive-sha256": archive_sha256,
            "archive-size": str(size),
            "operation-digest": hashlib.sha256(operation_id.encode()).hexdigest(),
            "purpose": "restore-staging",
        }
        receipt = await self._store.put_file(
            key,
            path,
            metadata=metadata,
            retain_until=None,
        )
        if (
            receipt.key != key
            or receipt.size != size
            or receipt.metadata != metadata
            or receipt.retain_until is not None
        ):
            raise RuntimeError("restore staging upload proof differs")
        url = await self._store.presigned_download(key, ttl_seconds=900)
        host = urlsplit(url).hostname
        if not host:
            raise RuntimeError("restore staging URL has no HTTPS host")
        identity = hashlib.sha256(
            json.dumps(
                {"key": key, "metadata": metadata},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return StagedPortableArchive(
            url=url,
            allowed_host=host,
            identity_sha256=identity,
        )


class CandidateBindingRuntime(Protocol):
    async def bind_candidate(
        self,
        candidate_cell_id: str,
        *,
        source_vault_id: str,
    ) -> None: ...


class KubernetesRestoreAdapter(Protocol):
    async def inspect_portable_archive(self, path: Any) -> Any: ...

    async def stop_candidate(self, candidate_cell_id: str) -> None: ...

    async def offline_restore(
        self,
        candidate_cell_id: str,
        archive_path: Any,
        **arguments: Any,
    ) -> None: ...

    async def authenticated_readiness(self, candidate_cell_id: str) -> bool: ...

    async def product_checks(self, candidate_cell_id: str) -> dict[str, bool]: ...

    async def finalize_candidate(self, candidate_cell_id: str) -> dict[str, bool]: ...


class CandidateBindingResolver(Protocol):
    async def resolve(
        self,
        candidate_cell_id: str,
        *,
        source_vault_id: str,
    ) -> KubernetesRestoreAdapter: ...


class DynamicKubernetesRestoreRuntime:
    """Resolve a concrete source-bound Kubernetes restore adapter per operation."""

    def __init__(self, resolver: CandidateBindingResolver) -> None:
        self._resolver = resolver
        self._candidate_cell_id: str | None = None
        self._source_vault_id: str | None = None
        self._adapter: KubernetesRestoreAdapter | None = None

    async def bind_candidate(
        self,
        candidate_cell_id: str,
        *,
        source_vault_id: str,
    ) -> None:
        if not candidate_cell_id or not source_vault_id or candidate_cell_id == source_vault_id:
            raise ValueError("restore candidate requires a distinct source vault binding")
        adapter = await self._resolver.resolve(
            candidate_cell_id,
            source_vault_id=source_vault_id,
        )
        self._candidate_cell_id = candidate_cell_id
        self._source_vault_id = source_vault_id
        self._adapter = adapter

    async def inspect_portable_archive(self, path: Any) -> Any:
        return await self._bound().inspect_portable_archive(path)

    async def stop_candidate(self, candidate_cell_id: str) -> None:
        await self._for_candidate(candidate_cell_id).stop_candidate(candidate_cell_id)

    async def offline_restore(
        self,
        candidate_cell_id: str,
        archive_path: Any,
        **arguments: Any,
    ) -> None:
        await self._for_candidate(candidate_cell_id).offline_restore(
            candidate_cell_id,
            archive_path,
            **arguments,
        )

    async def authenticated_readiness(self, candidate_cell_id: str) -> bool:
        return await self._for_candidate(candidate_cell_id).authenticated_readiness(
            candidate_cell_id
        )

    async def product_checks(self, candidate_cell_id: str) -> dict[str, bool]:
        return await self._for_candidate(candidate_cell_id).product_checks(candidate_cell_id)

    async def finalize_candidate(self, candidate_cell_id: str) -> dict[str, bool]:
        return await self._for_candidate(candidate_cell_id).finalize_candidate(candidate_cell_id)

    def _bound(self) -> KubernetesRestoreAdapter:
        if self._adapter is None:
            raise RuntimeError("restore candidate is not source-bound")
        return self._adapter

    def _for_candidate(self, candidate_cell_id: str) -> KubernetesRestoreAdapter:
        adapter = self._bound()
        if candidate_cell_id != self._candidate_cell_id:
            raise RuntimeError("restore candidate differs from source-bound adapter")
        return adapter


class RestoreWorkflow(Protocol):
    async def run(self, run: Any, **arguments: Any) -> dict[str, object]: ...


class CandidateBoundRestoreWorkflow:
    """Bind a restore candidate's logical vault before any recovery-object read."""

    def __init__(
        self,
        runtime: CandidateBindingRuntime,
        workflow: RestoreWorkflow,
    ) -> None:
        self._runtime = runtime
        self._workflow = workflow

    async def run(self, run: Any, **arguments: Any) -> dict[str, object]:
        source_cell_id = arguments.get("expected_source_cell_id")
        if not isinstance(source_cell_id, str) or not source_cell_id:
            raise ValueError("restore source cell identity is required")
        await self._runtime.bind_candidate(
            run.identity.cell_id,
            source_vault_id=run.identity.tenant_id,
        )
        return await self._workflow.run(run, **arguments)
