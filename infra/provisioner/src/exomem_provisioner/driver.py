"""Narrow idempotent effect-driver seam and deterministic test driver."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .models import ResourceKind


@dataclass(frozen=True, slots=True)
class EffectContext:
    operation_id: str
    provider_operation_id: str
    tenant_id: str
    cell_id: str | None
    fence_generation: int
    checkpoint: str = "effect-prepared"
    operation_created_at: str = "1970-01-01T00:00:00Z"

    @property
    def provider_identity(self) -> tuple[str, str, str, str | None, int]:
        return (
            self.operation_id,
            self.provider_operation_id,
            self.tenant_id,
            self.cell_id,
            self.fence_generation,
        )


@dataclass(frozen=True, slots=True)
class DriverResource:
    kind: ResourceKind
    recoverable_reference: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class DriverPending:
    checkpoint: str
    retry_after_seconds: int
    resources: tuple[DriverResource, ...] = ()


@dataclass(frozen=True, slots=True)
class DriverFinal:
    result: dict[str, Any] = field(repr=False)
    resources: tuple[DriverResource, ...] = ()


class LostAcknowledgement(RuntimeError):
    pass


class DriverRetryable(RuntimeError):
    pass


class DriverTerminal(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProvisionerDriver(Protocol):
    async def observed_fence(self, tenant_id: str) -> int: ...

    async def execute(
        self,
        action: str,
        request: dict[str, Any],
        context: EffectContext,
    ) -> DriverPending | DriverFinal: ...


class FakeDriver:
    """Provider-free driver proving idempotent effect and acknowledgement replay."""

    def __init__(self) -> None:
        self._tenant_fences: dict[str, int] = {}
        self._effect_metadata: dict[tuple[str, str], EffectContext] = {}
        self._results: dict[tuple[str, str], DriverFinal] = {}
        self._pending_polls: dict[str, int] = {}
        self._lost_acknowledgements: set[str] = set()

    def __repr__(self) -> str:
        return f"FakeDriver(effects={len(self._effect_metadata)}, results={len(self._results)})"

    def set_observed_fence(self, tenant_id: str, fence_generation: int) -> None:
        self._tenant_fences[tenant_id] = fence_generation

    def remain_pending(self, action: str, *, polls: int) -> None:
        self._pending_polls[action] = polls

    def lose_next_acknowledgement(self, action: str) -> None:
        self._lost_acknowledgements.add(action)

    def effect_count(self, action: str, operation_id: str) -> int:
        return int((action, operation_id) in self._effect_metadata)

    async def observed_fence(self, tenant_id: str) -> int:
        return self._tenant_fences.get(tenant_id, 0)

    async def execute(
        self,
        action: str,
        request: dict[str, Any],
        context: EffectContext,
    ) -> DriverPending | DriverFinal:
        key = (action, context.operation_id)
        recorded = self._effect_metadata.get(key)
        if recorded is not None and recorded.provider_identity != context.provider_identity:
            raise DriverTerminal("PROVISIONER_PROVIDER_METADATA_CONFLICT")
        if context.fence_generation < self._tenant_fences.get(context.tenant_id, 0):
            raise DriverTerminal("PROVISIONER_STALE_FENCE")
        if recorded is None:
            self._effect_metadata[key] = context
        self._tenant_fences[context.tenant_id] = max(
            context.fence_generation,
            self._tenant_fences.get(context.tenant_id, 0),
        )

        prior = self._results.get(key)
        if prior is not None:
            return prior
        pending = self._pending_polls.get(action, 0)
        if pending > 0:
            self._pending_polls[action] = pending - 1
            return DriverPending(checkpoint="provider-wait", retry_after_seconds=2)

        result = DriverFinal(
            result=self._result(action, request, context),
            resources=self._resources(action, context),
        )
        self._results[key] = result
        if action in self._lost_acknowledgements:
            self._lost_acknowledgements.remove(action)
            raise LostAcknowledgement("provider effect committed before acknowledgement")
        return result

    @staticmethod
    def _resources(action: str, context: EffectContext) -> tuple[DriverResource, ...]:
        if action != "provision":
            return ()
        return (
            DriverResource(
                kind=ResourceKind.KUBERNETES_NAMESPACE,
                recoverable_reference=f"namespace-{context.cell_id}",
            ),
        )

    @staticmethod
    def _result(action: str, request: dict[str, Any], context: EffectContext) -> dict[str, Any]:
        cell_id = str(request.get("cellId", "cell"))
        if action == "provision":
            return {
                "providerRef": f"provider-{cell_id}",
                "privateEndpoint": f"https://{cell_id}.cells.internal",
            }
        if action == "health":
            return {
                "live": True,
                "ready": True,
                "cellId": cell_id,
                "protocolVersion": request["protocolVersion"],
                "releaseVersion": request["releaseVersion"],
                "serviceAuthenticated": True,
                "mutationAuthority": True,
                "readAdmission": True,
                "writeAdmission": True,
                "workerPolicy": request["workerPolicy"],
                "code": "CELL_READY",
            }
        if action == "rotate-credential":
            return {"previousCredentialRejected": request.get("phase") == "finalize"}
        if action == "export":
            source = f"{context.operation_id}:{cell_id}".encode()
            return {
                "exportRef": f"export-{context.operation_id}",
                "releaseRef": f"release-{context.operation_id}",
                "archiveSha256": hashlib.sha256(b"archive:" + source).hexdigest(),
                "manifestSha256": hashlib.sha256(b"manifest:" + source).hexdigest(),
                "archiveSize": 1024,
                "encryptionScheme": "envelope-aes-256-gcm",
                "integrityVerified": True,
            }
        if action == "export-delete":
            return {"objectDestroyed": True}
        if action == "export-download":
            digest = hashlib.sha256(context.operation_id.encode()).hexdigest()
            expires = datetime.now(UTC) + timedelta(minutes=5)
            return {
                "url": f"https://downloads.invalid/exomem/{digest}",
                "expiresAt": expires.isoformat().replace("+00:00", "Z"),
            }
        if action == "discard":
            return {
                "computeDestroyed": True,
                "storageDestroyed": True,
                "keysDestroyed": True,
            }
        if action == "destroy":
            return {
                "computeDestroyed": True,
                "storageDestroyed": True,
                "keysDestroyed": True,
                "tenantResourcesDestroyed": True,
            }
        return {}
