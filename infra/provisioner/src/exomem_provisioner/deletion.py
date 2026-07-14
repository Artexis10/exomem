"""Provider-rediscovered candidate discard and ordered tenant destruction."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from .driver import DriverFinal, DriverPending, EffectContext


class DeletionVerificationError(RuntimeError):
    pass


class DeletionResourceKind(StrEnum):
    COMPUTE = "compute"
    ROUTE = "route"
    CREDENTIAL = "credential"
    VOLUME = "volume"
    EXPORT = "export"
    BACKUP = "backup"


@dataclass(frozen=True, slots=True)
class DeletionResource:
    provider: str
    reference: str
    kind: DeletionResourceKind
    tenant_id: str
    cell_id: str | None
    retained_until: datetime | None = None
    wrapped_key_reference: str | None = None
    delete_marker: bool = False

    def __post_init__(self) -> None:
        if not self.provider or not self.reference or not self.tenant_id:
            raise ValueError("deletion resource identity is incomplete")
        if self.retained_until is not None and self.retained_until.tzinfo is None:
            raise ValueError("deletion retention timestamp must be timezone-aware")
        if self.kind in {DeletionResourceKind.EXPORT, DeletionResourceKind.BACKUP}:
            if not self.delete_marker and not self.wrapped_key_reference:
                raise ValueError("recovery objects require a wrapped-key reference")


class DeletionProvider(Protocol):
    async def scan_tenant(self, tenant_id: str) -> tuple[DeletionResource, ...]: ...

    async def delete_resource(self, resource: DeletionResource) -> None: ...

    async def resource_absent(self, resource: DeletionResource) -> bool: ...

    async def destroy_wrapped_key(self, resource: DeletionResource) -> None: ...

    async def wrapped_key_absent(self, resource: DeletionResource) -> bool: ...

    async def active_cells_ready_excluding(self, tenant_id: str, excluded_cell_id: str) -> bool: ...


class OrderedDeletionWorkflow:
    """One idempotent pass; caller persists pending without consuming attempts."""

    _CANDIDATE_KINDS = frozenset(
        {
            DeletionResourceKind.COMPUTE,
            DeletionResourceKind.ROUTE,
            DeletionResourceKind.CREDENTIAL,
            DeletionResourceKind.VOLUME,
        }
    )

    def __init__(
        self,
        provider: DeletionProvider,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._provider = provider
        self._clock = clock

    async def discard_candidate(self, context: EffectContext) -> DriverPending | DriverFinal:
        if context.cell_id is None:
            raise DeletionVerificationError("candidate discard requires a cell identity")
        inventory = await self._provider.scan_tenant(context.tenant_id)
        candidate = tuple(
            resource
            for resource in inventory
            if resource.cell_id == context.cell_id and resource.kind in self._CANDIDATE_KINDS
        )
        if not candidate:
            raise DeletionVerificationError("failed candidate was not rediscovered")
        for resource in candidate:
            await self._provider.delete_resource(resource)
        proofs = {
            resource: await self._provider.resource_absent(resource) for resource in candidate
        }
        if not all(proofs.values()):
            return DriverPending("candidate-absence-verification", 2)
        if not await self._provider.active_cells_ready_excluding(
            context.tenant_id, context.cell_id
        ):
            raise DeletionVerificationError("candidate discard affected the active cell")
        return DriverFinal(
            {
                "computeDestroyed": all(
                    absent
                    for resource, absent in proofs.items()
                    if resource.kind in {DeletionResourceKind.COMPUTE, DeletionResourceKind.ROUTE}
                ),
                "storageDestroyed": all(
                    absent
                    for resource, absent in proofs.items()
                    if resource.kind is DeletionResourceKind.VOLUME
                ),
                "keysDestroyed": all(
                    absent
                    for resource, absent in proofs.items()
                    if resource.kind is DeletionResourceKind.CREDENTIAL
                ),
            }
        )

    async def destroy_tenant(self, context: EffectContext) -> DriverPending | DriverFinal:
        tenant_id = context.tenant_id
        inventory = await self._provider.scan_tenant(tenant_id)
        if any(resource.tenant_id != tenant_id for resource in inventory):
            raise DeletionVerificationError("provider scan crossed tenant boundary")
        now = self._clock()
        locked = tuple(
            resource
            for resource in inventory
            if resource.kind is DeletionResourceKind.BACKUP
            and resource.retained_until is not None
            and resource.retained_until > now
        )
        deletable = tuple(resource for resource in inventory if resource not in locked)
        for resource in deletable:
            await self._provider.delete_resource(resource)
        if not all(
            [await self._provider.resource_absent(resource) for resource in deletable]
        ):
            return DriverPending("absence-verification", 2)
        remaining_references = {
            resource.wrapped_key_reference
            for resource in locked
            if resource.wrapped_key_reference is not None
        }
        keyed_resources = {
            resource.wrapped_key_reference: resource
            for resource in deletable
            if resource.wrapped_key_reference is not None
            and resource.wrapped_key_reference not in remaining_references
        }
        for resource in keyed_resources.values():
            await self._provider.destroy_wrapped_key(resource)
            if not await self._provider.wrapped_key_absent(resource):
                return DriverPending("key-absence-verification", 2)
        if locked:
            wait_seconds = min(
                300,
                max(
                    1,
                    math.ceil(
                        (
                            min(
                                resource.retained_until
                                for resource in locked
                                if resource.retained_until is not None
                            )
                            - now
                        ).total_seconds()
                    ),
                ),
            )
            return DriverPending("retained-wait", wait_seconds)
        remaining = await self._provider.scan_tenant(tenant_id)
        if remaining:
            return DriverPending("provider-rediscovery", 2)
        return DriverFinal(
            {
                "computeDestroyed": True,
                "storageDestroyed": True,
                "keysDestroyed": True,
                "tenantResourcesDestroyed": True,
            }
        )
