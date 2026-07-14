"""Authenticated live Kubernetes, HCloud, Traefik, and B2 deletion provider."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from .deletion import (
    DeletionResource,
    DeletionResourceKind,
    OrderedDeletionWorkflow,
)
from .driver import DriverFinal, DriverPending, EffectContext
from .lifecycle import MetadataConflict, OpaqueProviderMetadata, _digest
from .provider_identity import (
    ProviderIdentityConflict,
    ProviderRecoveryIdentityVerifier,
    ProviderReference,
    decode_hcloud_identity_envelope,
)


class DeletionLeaseBusy(RuntimeError):
    """A valid deletion claim is temporarily blocked by another operation lease."""


class DeletionAuthority(Protocol):
    """Durable tenant fence plus a shared provider-side deletion lease."""

    async def current_fence(self, tenant_id: str) -> int: ...

    async def acquire(self, tenant_id: str, operation_id: str, fence: int) -> bool: ...

    async def acquire_cell(
        self,
        tenant_id: str,
        cell_id: str,
        operation_id: str,
        fence: int,
    ) -> bool: ...


class DeletionLedger(Protocol):
    async def tenant_recovery_objects(self, tenant_id: str) -> list[Any]: ...

    async def tenant_export_deliveries(self, tenant_id: str) -> list[Any]: ...

    async def mark_recovery_object_deleted(
        self,
        reference: str,
        *,
        tenant_id: str,
    ) -> None: ...

    async def mark_export_delivery_deleted(
        self,
        reference: str,
        *,
        tenant_id: str,
    ) -> None: ...

    async def destroy(self, reference: str, *, tenant_id: str) -> None: ...

    async def absent(self, reference: str, *, tenant_id: str) -> bool: ...

    async def deletion_complete(self, tenant_id: str) -> bool: ...


class LiveDeletionProvider:
    """Fail-closed provider inventory and absence-proof boundary.

    The instance is bound to one claimed destroy/discard operation before each
    ordered pass. Every mutation rechecks the durable tenant fence; the shared
    lease serializes candidate discard, tenant destroy, and durability work.
    """

    _CREDENTIAL_NAME = "exomem-cell-credentials"
    _CREDENTIAL_DELETION_OPERATION = "exomem.io/credential-deletion-operation-digest"
    _CREDENTIAL_DELETION_FENCE = "exomem.io/credential-deletion-fence"
    _B2_EXACT_MAX_KEYS = 100
    _B2_EXACT_MAX_PAGES = 10
    _B2_EXACT_MAX_ITEMS = 1000

    def __init__(
        self,
        *,
        core_v1: Any,
        apps_v1: Any,
        custom_objects: Any,
        hcloud_client: Any,
        b2_client: Any,
        recovery_bucket: str,
        export_bucket: str,
        identity_verifier: ProviderRecoveryIdentityVerifier,
        authority: DeletionAuthority,
        key_store: DeletionLedger,
    ) -> None:
        if not recovery_bucket or not export_bucket or recovery_bucket == export_bucket:
            raise ValueError("deletion buckets must be distinct and non-empty")
        self._core = core_v1
        self._apps = apps_v1
        self._custom = custom_objects
        self._hcloud = hcloud_client
        self._b2 = b2_client
        self._recovery_bucket = recovery_bucket
        self._export_bucket = export_bucket
        self._verifier = identity_verifier
        self._authority = authority
        self._key_store = key_store
        self._context: EffectContext | None = None
        self._claims: dict[str, dict[str, object]] = {}

    async def bind(self, context: EffectContext) -> None:
        current = await self._authority.current_fence(context.tenant_id)
        if current != context.fence_generation:
            raise MetadataConflict("deletion fence is stale")
        if not await self._authority.acquire(
            context.tenant_id,
            context.provider_operation_id,
            context.fence_generation,
        ):
            raise DeletionLeaseBusy("tenant deletion lease is held by another operation")
        self._context = context
        if context.cell_id is not None and not await self._authority.acquire_cell(
            context.tenant_id,
            context.cell_id,
            context.provider_operation_id,
            context.fence_generation,
        ):
            self._context = None
            raise DeletionLeaseBusy("cell operation lock is held by another operation")

    async def _assert_authorized(self, tenant_id: str) -> EffectContext:
        context = self._context
        if context is None or context.tenant_id != tenant_id:
            raise MetadataConflict("deletion provider is not bound to this tenant")
        if await self._authority.current_fence(tenant_id) != context.fence_generation:
            raise MetadataConflict("deletion fence changed during provider mutation")
        return context

    def _authenticated_claims(
        self,
        *,
        envelope: str,
        provider: str,
        reference: str,
        metadata: OpaqueProviderMetadata | None = None,
    ) -> dict[str, object]:
        claims = self._verifier.claims(envelope)
        if claims["provider"] != provider or claims["providerReference"] != reference:
            raise MetadataConflict("provider deletion identity reference differs")
        if metadata is not None:
            try:
                self._verifier.authenticate(
                    envelope,
                    provider=provider,
                    provider_reference=reference,
                    tenant_id=metadata.tenant_id,
                    cell_id=metadata.subject_id,
                    operation_id=metadata.operation_id,
                    fence_generation=metadata.fence_generation,
                )
            except ProviderIdentityConflict as error:
                raise MetadataConflict("provider deletion identity did not authenticate") from error
        return claims

    def _record(
        self,
        *,
        provider: str,
        reference: str,
        kind: DeletionResourceKind,
        claims: dict[str, object],
        retained_until: datetime | None = None,
        wrapped_key_reference: str | None = None,
        ledger_reference: str | None = None,
        delete_marker: bool = False,
    ) -> DeletionResource:
        self._claims[reference] = claims
        return DeletionResource(
            provider=provider,
            reference=reference,
            kind=kind,
            tenant_id=str(claims["tenantId"]),
            cell_id=str(claims["cellId"]),
            retained_until=retained_until,
            wrapped_key_reference=wrapped_key_reference,
            ledger_reference=ledger_reference,
            delete_marker=delete_marker,
        )

    async def scan_tenant(self, tenant_id: str) -> tuple[DeletionResource, ...]:
        context = await self._assert_authorized(tenant_id)
        resources = [
            *await self._scan_kubernetes(tenant_id),
            *await self._scan_hcloud(tenant_id),
            *await self._scan_b2(tenant_id),
        ]
        if any(resource.tenant_id != tenant_id for resource in resources):
            raise MetadataConflict("provider deletion scan crossed tenant boundary")
        for resource in resources:
            fence = self._claims[resource.reference]["fenceGeneration"]
            if not isinstance(fence, int) or fence > context.fence_generation:
                raise MetadataConflict("provider deletion observed a newer fence")
        if context.cell_id is None:
            for cell_id in sorted(
                {
                    resource.cell_id
                    for resource in resources
                    if isinstance(resource.cell_id, str) and resource.cell_id
                }
            ):
                if not await self._authority.acquire_cell(
                    tenant_id,
                    cell_id,
                    context.provider_operation_id,
                    context.fence_generation,
                ):
                    raise DeletionLeaseBusy("cell operation lock is held by another operation")
        return tuple(
            sorted(
                resources,
                key=lambda item: (
                    item.provider,
                    0 if item.delete_marker else 1,
                    item.reference,
                    item.kind,
                ),
            )
        )

    async def _scan_kubernetes(self, tenant_id: str) -> list[DeletionResource]:
        result: list[DeletionResource] = []
        for item, metadata, namespace_reference, claims in await self._verified_namespaces(
            tenant_id
        ):
            namespace = str(item.metadata.name)
            result.append(
                self._record(
                    provider="kubernetes",
                    reference=namespace_reference,
                    kind=DeletionResourceKind.COMPUTE,
                    claims=claims,
                )
            )
            annotations = dict(getattr(item.metadata, "annotations", None) or {})
            if annotations.get("exomem.io/credentials-secret-name") != self._CREDENTIAL_NAME:
                raise MetadataConflict("credential deletion contract differs")
            secret_reference = ProviderReference.kubernetes(
                provider="kubernetes",
                api_version="v1",
                kind="Secret",
                namespace=namespace,
                name=self._CREDENTIAL_NAME,
            )
            result.append(
                self._record(
                    provider="kubernetes",
                    reference=secret_reference,
                    kind=DeletionResourceKind.CREDENTIAL,
                    claims={
                        **claims,
                        "providerReference": secret_reference,
                    },
                )
            )
            routes = await asyncio.to_thread(
                self._custom.list_namespaced_custom_object,
                group="traefik.io",
                version="v1alpha1",
                namespace=namespace,
                plural="ingressroutes",
                label_selector="exomem.io/tenant-route=true",
            )
            for route in routes.get("items", ()):  # type: ignore[union-attr]
                route_metadata = dict(route.get("metadata", {}))
                route_annotations = dict(route_metadata.get("annotations", {}))
                recovered = OpaqueProviderMetadata.from_kubernetes_annotations(route_annotations)
                if recovered.tenant_id != tenant_id or recovered.subject_id != metadata.subject_id:
                    raise MetadataConflict("route scan crossed tenant boundary")
                name = str(route_metadata.get("name", ""))
                reference = ProviderReference.kubernetes(
                    provider="traefik",
                    api_version="traefik.io/v1alpha1",
                    kind="IngressRoute",
                    namespace=namespace,
                    name=name,
                )
                route_claims = self._authenticated_claims(
                    envelope=str(route_annotations.get("exomem.io/recovery-envelope", "")),
                    provider="traefik",
                    reference=reference,
                    metadata=recovered,
                )
                result.append(
                    self._record(
                        provider="traefik",
                        reference=reference,
                        kind=DeletionResourceKind.ROUTE,
                        claims=route_claims,
                    )
                )
        return result

    async def _verified_namespaces(
        self, tenant_id: str
    ) -> list[tuple[Any, OpaqueProviderMetadata, str, dict[str, object]]]:
        namespaces = await asyncio.to_thread(
            self._core.list_namespace,
            label_selector="exomem.io/tenant-cell=true",
        )
        result: list[tuple[Any, OpaqueProviderMetadata, str, dict[str, object]]] = []
        for item in getattr(namespaces, "items", ()) or ():
            annotations = dict(getattr(item.metadata, "annotations", None) or {})
            metadata = OpaqueProviderMetadata.from_kubernetes_annotations(annotations)
            if metadata.tenant_id != tenant_id:
                continue
            namespace = str(item.metadata.name)
            namespace_reference = ProviderReference.kubernetes(
                provider="kubernetes",
                api_version="v1",
                kind="Namespace",
                namespace="",
                name=namespace,
            )
            claims = self._authenticated_claims(
                envelope=str(annotations.get("exomem.io/recovery-envelope", "")),
                provider="kubernetes",
                reference=namespace_reference,
                metadata=metadata,
            )
            result.append((item, metadata, namespace_reference, claims))
        return result

    async def _scan_hcloud(self, tenant_id: str) -> list[DeletionResource]:
        selector = "exomem_tenant=" + _digest(tenant_id)
        volumes = await asyncio.to_thread(
            self._hcloud.volumes.get_all,
            label_selector=selector,
        )
        result: list[DeletionResource] = []
        for volume in volumes:
            labels = dict(getattr(volume, "labels", None) or {})
            metadata = OpaqueProviderMetadata.from_hcloud_labels(labels)
            if metadata.tenant_id != tenant_id:
                raise MetadataConflict("HCloud scan crossed tenant boundary")
            reference = ProviderReference.hcloud(kind="volume", resource_id=volume.id)
            claims = self._authenticated_claims(
                envelope=decode_hcloud_identity_envelope(labels),
                provider="hcloud",
                reference=reference,
                metadata=metadata,
            )
            result.append(
                self._record(
                    provider="hcloud",
                    reference=reference,
                    kind=DeletionResourceKind.VOLUME,
                    claims=claims,
                )
            )
        return result

    async def _scan_b2(self, tenant_id: str) -> list[DeletionResource]:
        result: list[DeletionResource] = []
        recovery_records = await self._key_store.tenant_recovery_objects(tenant_id)
        for record in recovery_records:
            if record.tenant_id != tenant_id:
                raise MetadataConflict("recovery ledger crossed tenant boundary")
            if (
                record.deleted_at is not None
                and record.wrapped_data_key is None
                and record.key_destroyed_at is not None
            ):
                continue
            result.extend(await self._scan_recovery_record(record))
        delivery_records = await self._key_store.tenant_export_deliveries(tenant_id)
        for record in delivery_records:
            if record.tenant_id != tenant_id:
                raise MetadataConflict("delivery ledger crossed tenant boundary")
            if record.deleted_at is not None:
                continue
            result.extend(await self._scan_delivery_record(record))
        return result

    async def _scan_recovery_record(self, record: Any) -> list[DeletionResource]:
        parsed = self._exact_b2_ledger_reference(record.provider_reference)
        bucket = str(parsed["bucket"])
        key = str(parsed["key"])
        expected_version = str(parsed["objectVersionId"])
        kind = self._recovery_kind(record, bucket=bucket)
        claims = self._ledger_claims(record, provider_reference=record.provider_reference)
        versions, markers = await self._b2_entries(bucket=bucket, prefix=key)
        versions = [item for item in versions if self._b2_entry_identity(item)[0] == key]
        markers = [item for item in markers if self._b2_entry_identity(item)[0] == key]
        resources: list[DeletionResource] = []
        listed_versions: set[str] = set()
        listed_version_order: list[str] = []
        locked_until = getattr(record, "object_lock_until", None)
        if not isinstance(locked_until, datetime) or locked_until.tzinfo is None:
            raise MetadataConflict("recovery ledger retention timestamp is invalid")
        for item in versions:
            _, version_id = self._b2_entry_identity(item)
            listed_versions.add(version_id)
            head = await asyncio.to_thread(
                self._b2.head_object,
                Bucket=bucket,
                Key=key,
                VersionId=version_id,
            )
            if head.get("VersionId") not in {None, version_id}:
                raise MetadataConflict("B2 exact version proof differs")
            object_metadata = {
                str(name).lower(): str(value)
                for name, value in dict(head.get("Metadata", {})).items()
            }
            authenticated = self._authenticated_claims(
                envelope=object_metadata.get("identity-envelope", ""),
                provider="b2",
                reference=ProviderReference.b2(bucket=bucket, key=key),
            )
            self._require_ledger_claims(authenticated, claims)
            if object_metadata.get("wrapped-key-reference") != record.opaque_reference:
                raise MetadataConflict("B2 wrapped-key ledger reference differs")
            retained = head.get("ObjectLockRetainUntilDate")
            if retained is not None and (
                not isinstance(retained, datetime) or retained.tzinfo is None
            ):
                raise MetadataConflict("B2 retention timestamp is invalid")
            if retained is not None and (locked_until is None or retained > locked_until):
                locked_until = retained
            listed_version_order.append(version_id)
        for version_id in listed_version_order:
            resources.append(
                self._record(
                    provider="b2",
                    reference=ProviderReference.b2(
                        bucket=bucket,
                        key=key,
                        version_id=version_id,
                    ),
                    kind=kind,
                    claims=claims,
                    retained_until=locked_until,
                    wrapped_key_reference=record.opaque_reference,
                    ledger_reference=record.opaque_reference,
                )
            )
        if expected_version not in listed_versions:
            resources.append(
                self._record(
                    provider="b2",
                    reference=record.provider_reference,
                    kind=kind,
                    claims=claims,
                    retained_until=locked_until,
                    wrapped_key_reference=record.opaque_reference,
                    ledger_reference=record.opaque_reference,
                )
            )
        for item in markers:
            _, version_id = self._b2_entry_identity(item)
            resources.append(
                self._record(
                    provider="b2",
                    reference=ProviderReference.b2(
                        bucket=bucket,
                        key=key,
                        version_id=version_id,
                        delete_marker=True,
                    ),
                    kind=kind,
                    claims=claims,
                    retained_until=locked_until,
                    ledger_reference=record.opaque_reference,
                    delete_marker=True,
                )
            )
        return resources

    async def _scan_delivery_record(self, record: Any) -> list[DeletionResource]:
        parsed = self._exact_b2_ledger_reference(record.provider_reference)
        bucket = str(parsed["bucket"])
        key = str(parsed["key"])
        expected_version = str(parsed["objectVersionId"])
        if bucket != self._export_bucket or not key.startswith("user-export-delivery/"):
            raise MetadataConflict("delivery ledger provider reference is out of scope")
        claims = self._ledger_claims(record, provider_reference=record.provider_reference)
        versions, markers = await self._b2_entries(bucket=bucket, prefix=key)
        exact_versions = [item for item in versions if self._b2_entry_identity(item)[0] == key]
        exact_markers = [item for item in markers if self._b2_entry_identity(item)[0] == key]
        listed_versions = {
            version_id for _, version_id in map(self._b2_entry_identity, exact_versions)
        }
        resources = [
            self._record(
                provider="b2",
                reference=ProviderReference.b2(bucket=bucket, key=key, version_id=version_id),
                kind=DeletionResourceKind.DELIVERY,
                claims=claims,
                ledger_reference=record.id,
            )
            for _, version_id in map(self._b2_entry_identity, exact_versions)
        ]
        if expected_version not in listed_versions:
            resources.append(
                self._record(
                    provider="b2",
                    reference=record.provider_reference,
                    kind=DeletionResourceKind.DELIVERY,
                    claims=claims,
                    ledger_reference=record.id,
                )
            )
        resources.extend(
            self._record(
                provider="b2",
                reference=ProviderReference.b2(
                    bucket=bucket,
                    key=key,
                    version_id=version_id,
                    delete_marker=True,
                ),
                kind=DeletionResourceKind.DELIVERY,
                claims=claims,
                ledger_reference=record.id,
                delete_marker=True,
            )
            for _, version_id in map(self._b2_entry_identity, exact_markers)
        )
        return resources

    def _exact_b2_ledger_reference(self, reference: str) -> dict[str, object]:
        parsed = ProviderReference.parse(reference)
        if (
            parsed.get("provider") != "b2"
            or not isinstance(parsed.get("bucket"), str)
            or not isinstance(parsed.get("key"), str)
            or not isinstance(parsed.get("objectVersionId"), str)
            or not parsed.get("objectVersionId")
            or parsed.get("deleteMarker") is not False
        ):
            raise MetadataConflict("deletion ledger requires an exact B2 version reference")
        return parsed

    def _recovery_kind(self, record: Any, *, bucket: str) -> DeletionResourceKind:
        kind = getattr(record.kind, "value", str(record.kind))
        if kind == "user-export" and bucket == self._export_bucket:
            return DeletionResourceKind.EXPORT
        if kind == "vault-backup" and bucket == self._recovery_bucket:
            return DeletionResourceKind.BACKUP
        raise MetadataConflict("recovery ledger provider bucket differs from object kind")

    @staticmethod
    def _ledger_claims(record: Any, *, provider_reference: str) -> dict[str, object]:
        return {
            "provider": "b2",
            "providerReference": provider_reference,
            "tenantId": record.tenant_id,
            "cellId": record.cell_id,
            "operationId": record.operation_id,
            "fenceGeneration": record.fence_generation,
        }

    @staticmethod
    def _require_ledger_claims(
        authenticated: dict[str, object],
        ledger: dict[str, object],
    ) -> None:
        for field in ("tenantId", "cellId", "operationId", "fenceGeneration"):
            if authenticated.get(field) != ledger.get(field):
                raise MetadataConflict("B2 provider identity differs from durable ledger")

    async def _b2_entries(
        self,
        *,
        bucket: str,
        prefix: str,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        if not prefix:
            raise MetadataConflict("B2 exact-key listing requires a key")
        versions: list[dict[str, object]] = []
        markers: list[dict[str, object]] = []
        key_marker: str | None = None
        version_marker: str | None = None
        seen_cursors: set[tuple[str, str | None]] = set()
        total_items = 0
        for _page_number in range(1, self._B2_EXACT_MAX_PAGES + 1):
            request: dict[str, object] = {
                "Bucket": bucket,
                "Prefix": prefix,
                "MaxKeys": self._B2_EXACT_MAX_KEYS,
            }
            if key_marker is not None:
                request["KeyMarker"] = key_marker
            if version_marker is not None:
                request["VersionIdMarker"] = version_marker
            page = await asyncio.to_thread(self._b2.list_object_versions, **request)
            page_items = 0
            moved_beyond_exact_key = False
            for field, target in (("Versions", versions), ("DeleteMarkers", markers)):
                entries = page.get(field, ())
                if not isinstance(entries, (list, tuple)):
                    raise MetadataConflict("B2 version listing is invalid")
                page_items += len(entries)
                previous_entry_key: str | None = None
                for item in entries:
                    entry_key, _ = self._b2_entry_identity(item)
                    if previous_entry_key is not None and entry_key < previous_entry_key:
                        raise MetadataConflict("B2 exact-key results are out of order")
                    previous_entry_key = entry_key
                    if entry_key < prefix:
                        raise MetadataConflict("B2 exact-key results moved backwards")
                    if entry_key == prefix:
                        target.append(item)
                    else:
                        moved_beyond_exact_key = True
            if page_items > self._B2_EXACT_MAX_KEYS:
                raise MetadataConflict("B2 exact-key page exceeded its item bound")
            total_items += page_items
            if total_items > self._B2_EXACT_MAX_ITEMS:
                raise MetadataConflict("B2 exact-key listing exceeded its item bound")
            if moved_beyond_exact_key:
                return versions, markers
            if not page.get("IsTruncated"):
                return versions, markers
            next_key = page.get("NextKeyMarker")
            next_version = page.get("NextVersionIdMarker")
            if not isinstance(next_key, str) or not next_key:
                raise MetadataConflict("B2 version pagination is invalid")
            if next_key < prefix or (key_marker is not None and next_key < key_marker):
                raise MetadataConflict("B2 version pagination moved backwards")
            if next_key > prefix:
                return versions, markers
            if not isinstance(next_version, str) or not next_version:
                raise MetadataConflict("B2 version pagination is invalid")
            cursor = (next_key, next_version)
            if cursor in seen_cursors:
                raise MetadataConflict("B2 version pagination did not advance")
            seen_cursors.add(cursor)
            key_marker = next_key
            version_marker = next_version
        raise MetadataConflict("B2 exact-key listing exceeded its page bound")

    @staticmethod
    def _b2_entry_identity(item: object) -> tuple[str, str]:
        if not isinstance(item, dict):
            raise MetadataConflict("B2 version listing is invalid")
        key = item.get("Key")
        version_id = item.get("VersionId")
        if not isinstance(key, str) or not key or not isinstance(version_id, str) or not version_id:
            raise MetadataConflict("B2 version identity is invalid")
        return key, version_id

    async def delete_resource(self, resource: DeletionResource) -> None:
        await self._assert_authorized(resource.tenant_id)
        if resource.reference not in self._claims:
            raise MetadataConflict("deletion resource was not authenticated by this scan")
        parsed = ProviderReference.parse(resource.reference)
        if resource.provider == "kubernetes":
            await self._delete_kubernetes(parsed, resource)
        elif resource.provider == "traefik":
            await self._delete_traefik(parsed)
        elif resource.provider == "hcloud":
            await self._delete_hcloud(parsed, resource)
        elif resource.provider == "b2":
            version_id = parsed.get("objectVersionId")
            if not isinstance(version_id, str) or not version_id:
                raise MetadataConflict("B2 deletion requires an exact version ID")
            arguments: dict[str, object] = {
                "Bucket": str(parsed["bucket"]),
                "Key": str(parsed["key"]),
                "VersionId": version_id,
            }
            await asyncio.to_thread(self._b2.delete_object, **arguments)
        else:
            raise MetadataConflict("deletion provider kind is unsupported")

    async def _delete_kubernetes(
        self,
        parsed: dict[str, object],
        resource: DeletionResource,
    ) -> None:
        kind = parsed.get("kind")
        name = str(parsed.get("name", ""))
        namespace = str(parsed.get("namespace", ""))
        if kind == "Namespace":
            try:
                await asyncio.to_thread(self._core.delete_namespace, name)
            except Exception as error:
                if not _is_not_found(error):
                    raise
            return
        if kind == "Secret":
            if name != self._CREDENTIAL_NAME or not namespace:
                raise MetadataConflict("credential deletion target differs")
            try:
                await asyncio.to_thread(
                    self._core.delete_namespaced_secret,
                    name,
                    namespace,
                )
            except Exception as error:
                if not _is_not_found(error):
                    raise
            # A 404 may be the replay of a successful delete whose response was
            # lost. Always publish the content-free receipt after absence.
            await self._record_credential_deletion(namespace, resource)
            return
        raise MetadataConflict("Kubernetes deletion kind is unsupported")

    async def _record_credential_deletion(
        self,
        namespace: str,
        resource: DeletionResource,
    ) -> None:
        context = await self._assert_authorized(resource.tenant_id)
        annotations = {
            self._CREDENTIAL_DELETION_OPERATION: _digest(
                context.provider_operation_id,
                length=64,
            ),
            self._CREDENTIAL_DELETION_FENCE: str(context.fence_generation),
        }
        try:
            await asyncio.to_thread(
                self._core.patch_namespace,
                namespace,
                {"metadata": {"annotations": annotations}},
            )
        except Exception as error:
            if not _is_not_found(error):
                raise

    async def _delete_traefik(self, parsed: dict[str, object]) -> None:
        try:
            await asyncio.to_thread(
                self._custom.delete_namespaced_custom_object,
                group="traefik.io",
                version="v1alpha1",
                namespace=str(parsed["namespace"]),
                plural="ingressroutes",
                name=str(parsed["name"]),
            )
        except Exception as error:
            if not _is_not_found(error):
                raise

    async def _delete_hcloud(self, parsed: dict[str, object], resource: DeletionResource) -> None:
        identifier = int(str(parsed["id"]))
        claims = self._claims[resource.reference]
        pvs = await asyncio.to_thread(self._core.list_persistent_volume)
        for pv in getattr(pvs, "items", ()) or ():
            csi = getattr(getattr(pv, "spec", None), "csi", None)
            if str(getattr(csi, "volume_handle", "")) != str(identifier):
                continue
            annotations = dict(getattr(pv.metadata, "annotations", None) or {})
            metadata = OpaqueProviderMetadata.from_kubernetes_annotations(annotations)
            if metadata.tenant_id != resource.tenant_id or metadata.subject_id != resource.cell_id:
                raise MetadataConflict("retained PV scan crossed tenant boundary")
            reference = ProviderReference.kubernetes(
                provider="kubernetes",
                api_version="v1",
                kind="PersistentVolume",
                namespace="",
                name=str(pv.metadata.name),
            )
            self._authenticated_claims(
                envelope=str(annotations.get("exomem.io/recovery-envelope", "")),
                provider="kubernetes",
                reference=reference,
                metadata=metadata,
            )
            if metadata.fence_generation != claims["fenceGeneration"]:
                raise MetadataConflict("retained PV and HCloud fences differ")
            await asyncio.to_thread(
                self._core.delete_persistent_volume,
                str(pv.metadata.name),
            )
            if (
                await self._read_or_none(
                    self._core.read_persistent_volume,
                    str(pv.metadata.name),
                )
                is not None
            ):
                return
        volume = await asyncio.to_thread(self._hcloud.volumes.get_by_id, identifier)
        if volume is not None:
            await asyncio.to_thread(self._hcloud.volumes.delete, volume)

    async def resource_absent(self, resource: DeletionResource) -> bool:
        context = await self._assert_authorized(resource.tenant_id)
        parsed = ProviderReference.parse(resource.reference)
        try:
            if resource.provider == "kubernetes":
                if parsed.get("kind") == "Namespace":
                    await asyncio.to_thread(self._core.read_namespace, str(parsed["name"]))
                    return False
                if parsed.get("kind") != "Secret" or parsed.get("name") != self._CREDENTIAL_NAME:
                    raise MetadataConflict("Kubernetes absence target differs")
                namespace = str(parsed["namespace"])
                item = await asyncio.to_thread(self._core.read_namespace, namespace)
                annotations = dict(getattr(item.metadata, "annotations", None) or {})
                metadata = OpaqueProviderMetadata.from_kubernetes_annotations(annotations)
                namespace_reference = ProviderReference.kubernetes(
                    provider="kubernetes",
                    api_version="v1",
                    kind="Namespace",
                    namespace="",
                    name=namespace,
                )
                self._authenticated_claims(
                    envelope=str(annotations.get("exomem.io/recovery-envelope", "")),
                    provider="kubernetes",
                    reference=namespace_reference,
                    metadata=metadata,
                )
                if (
                    metadata.tenant_id != resource.tenant_id
                    or metadata.subject_id != resource.cell_id
                ):
                    raise MetadataConflict("credential deletion receipt crossed tenant boundary")
                return annotations.get(self._CREDENTIAL_DELETION_OPERATION) == _digest(
                    context.provider_operation_id, length=64
                ) and annotations.get(self._CREDENTIAL_DELETION_FENCE) == str(
                    context.fence_generation
                )
            if resource.provider == "traefik":
                await asyncio.to_thread(
                    self._custom.get_namespaced_custom_object,
                    group="traefik.io",
                    version="v1alpha1",
                    namespace=str(parsed["namespace"]),
                    plural="ingressroutes",
                    name=str(parsed["name"]),
                )
                return False
            if resource.provider == "hcloud":
                identifier = int(str(parsed["id"]))
                volume = await asyncio.to_thread(self._hcloud.volumes.get_by_id, identifier)
                if volume is not None:
                    return False
                pvs = await asyncio.to_thread(self._core.list_persistent_volume)
                return not any(
                    str(
                        getattr(
                            getattr(getattr(pv, "spec", None), "csi", None),
                            "volume_handle",
                            "",
                        )
                    )
                    == str(identifier)
                    for pv in (getattr(pvs, "items", ()) or ())
                )
            if resource.provider == "b2":
                version_id = parsed.get("objectVersionId")
                if not isinstance(version_id, str) or not version_id:
                    raise MetadataConflict("B2 absence proof requires an exact version ID")
                versions, markers = await self._b2_entries(
                    bucket=str(parsed["bucket"]),
                    prefix=str(parsed["key"]),
                )
                return not any(
                    self._b2_entry_identity(item)
                    == (str(parsed["key"]), version_id)
                    for item in (*versions, *markers)
                )
        except Exception as error:
            if _is_not_found(error):
                return True
            raise
        raise MetadataConflict("deletion provider kind is unsupported")

    async def record_resource_absence(self, resource: DeletionResource) -> None:
        await self._assert_authorized(resource.tenant_id)
        if resource.provider != "b2" or resource.delete_marker:
            return
        if resource.ledger_reference is None:
            raise MetadataConflict("B2 deletion resource lacks a durable ledger reference")
        if resource.kind is DeletionResourceKind.DELIVERY:
            await self._key_store.mark_export_delivery_deleted(
                resource.ledger_reference,
                tenant_id=resource.tenant_id,
            )
            return
        if resource.kind in {DeletionResourceKind.EXPORT, DeletionResourceKind.BACKUP}:
            await self._key_store.mark_recovery_object_deleted(
                resource.ledger_reference,
                tenant_id=resource.tenant_id,
            )

    async def tenant_deletion_complete(self, tenant_id: str) -> bool:
        await self._assert_authorized(tenant_id)
        return await self._key_store.deletion_complete(tenant_id)

    async def destroy_wrapped_key(self, resource: DeletionResource) -> None:
        await self._assert_authorized(resource.tenant_id)
        if resource.wrapped_key_reference is None:
            raise MetadataConflict("deletion resource has no wrapped key")
        await self._key_store.destroy(
            resource.wrapped_key_reference,
            tenant_id=resource.tenant_id,
        )

    async def wrapped_key_absent(self, resource: DeletionResource) -> bool:
        await self._assert_authorized(resource.tenant_id)
        if resource.wrapped_key_reference is None:
            return True
        return await self._key_store.absent(
            resource.wrapped_key_reference,
            tenant_id=resource.tenant_id,
        )

    async def active_cells_ready_excluding(self, tenant_id: str, excluded_cell_id: str) -> bool:
        await self._assert_authorized(tenant_id)
        namespaces = await asyncio.to_thread(
            self._core.list_namespace,
            label_selector="exomem.io/tenant-cell=true",
        )
        for item in getattr(namespaces, "items", ()) or ():
            annotations = dict(getattr(item.metadata, "annotations", None) or {})
            metadata = OpaqueProviderMetadata.from_kubernetes_annotations(annotations)
            if metadata.tenant_id != tenant_id or metadata.subject_id == excluded_cell_id:
                continue
            stateful_set = await self._read_or_none(
                self._apps.read_namespaced_stateful_set,
                metadata.resource_name,
                metadata.resource_name,
            )
            status = getattr(stateful_set, "status", None)
            if (
                stateful_set is None
                or getattr(status, "ready_replicas", 0) != 1
                or getattr(status, "available_replicas", 0) != 1
            ):
                return False
        return True

    @staticmethod
    async def _read_or_none(call: Any, *arguments: str) -> Any | None:
        try:
            return await asyncio.to_thread(call, *arguments)
        except Exception as error:
            if _is_not_found(error):
                return None
            raise


class FencedOrderedDeletionWorkflow:
    """Bind every ordered pass to the durable fence and shared provider lease."""

    def __init__(
        self,
        provider: LiveDeletionProvider,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._provider = provider
        self._delegate = OrderedDeletionWorkflow(provider, clock=clock)

    async def discard_candidate(self, context: EffectContext) -> DriverPending | DriverFinal:
        await self._provider.bind(context)
        return await self._delegate.discard_candidate(context)

    async def destroy_tenant(self, context: EffectContext) -> DriverPending | DriverFinal:
        await self._provider.bind(context)
        return await self._delegate.destroy_tenant(context)


def _is_not_found(error: Exception) -> bool:
    status = getattr(error, "status", None)
    if status == 404:
        return True
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return False
    code = str(response.get("Error", {}).get("Code", ""))
    return code in {"404", "NoSuchKey", "NotFound"}
