from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from exomem_provisioner.driver import DriverFinal, DriverPending, EffectContext
from exomem_provisioner.durability_repository import RunKind
from exomem_provisioner.lifecycle import OpaqueProviderMetadata
from exomem_provisioner.provider_deletion import (
    DeletionLeaseBusy,
    FencedOrderedDeletionWorkflow,
    LiveDeletionProvider,
)
from exomem_provisioner.provider_identity import (
    ProviderRecoveryIdentityCodec,
    ProviderReference,
    chunk_hcloud_identity_envelope,
)


class NotFound(Exception):
    status = 404


CODEC = ProviderRecoveryIdentityCodec.from_secret("provider-recovery-root")


def metadata(cell_id: str, *, operation: str, fence: int) -> OpaqueProviderMetadata:
    return OpaqueProviderMetadata("tenant-alpha", cell_id, operation, fence)


def envelope(
    provider: str,
    reference: str,
    value: OpaqueProviderMetadata,
) -> str:
    return CODEC.seal(
        provider=provider,
        provider_reference=reference,
        tenant_id=value.tenant_id,
        cell_id=value.subject_id,
        operation_id=value.operation_id,
        fence_generation=value.fence_generation,
    )


class Core:
    def __init__(self, values: list[OpaqueProviderMetadata]) -> None:
        self.namespaces: dict[str, object] = {}
        self.secrets: dict[tuple[str, str], object] = {}
        self.pvs: dict[str, object] = {}
        self.delay_pv_deletion = False
        for value in values:
            name = value.resource_name
            namespace_reference = ProviderReference.kubernetes(
                provider="kubernetes",
                api_version="v1",
                kind="Namespace",
                namespace="",
                name=name,
            )
            self.namespaces[name] = SimpleNamespace(
                metadata=SimpleNamespace(
                    name=name,
                    annotations={
                        **value.kubernetes_annotations,
                        "exomem.io/recovery-envelope": envelope(
                            "kubernetes", namespace_reference, value
                        ),
                        "exomem.io/credentials-secret-name": "exomem-cell-credentials",
                    },
                )
            )
            secret_reference = ProviderReference.kubernetes(
                provider="kubernetes",
                api_version="v1",
                kind="Secret",
                namespace=name,
                name="exomem-cell-credentials",
            )
            self.secrets[(name, "exomem-cell-credentials")] = SimpleNamespace(
                metadata=SimpleNamespace(
                    name="exomem-cell-credentials",
                    namespace=name,
                    annotations={
                        **value.kubernetes_annotations,
                        "exomem.io/recovery-envelope": envelope(
                            "kubernetes", secret_reference, value
                        ),
                    },
                )
            )

    def list_namespace(self, *, label_selector: str):
        assert label_selector == "exomem.io/tenant-cell=true"
        return SimpleNamespace(items=list(self.namespaces.values()))

    def delete_namespaced_secret(self, name: str, namespace: str):
        try:
            self.secrets.pop((namespace, name))
        except KeyError as error:
            raise NotFound from error

    def patch_namespace(self, name: str, body: dict[str, object]):
        try:
            namespace = self.namespaces[name]
        except KeyError as error:
            raise NotFound from error
        namespace.metadata.annotations.update(body["metadata"]["annotations"])

    def delete_namespace(self, name: str):
        self.namespaces.pop(name, None)
        for key in tuple(self.secrets):
            if key[0] == name:
                self.secrets.pop(key, None)

    def read_namespace(self, name: str):
        try:
            return self.namespaces[name]
        except KeyError as error:
            raise NotFound from error

    def list_persistent_volume(self):
        return SimpleNamespace(items=list(self.pvs.values()))

    def delete_persistent_volume(self, name: str):
        if not self.delay_pv_deletion:
            self.pvs.pop(name, None)

    def read_persistent_volume(self, name: str):
        try:
            return self.pvs[name]
        except KeyError as error:
            raise NotFound from error


class Apps:
    def __init__(self, core: Core) -> None:
        self.core = core

    def read_namespaced_stateful_set(self, name: str, namespace: str):
        if namespace not in self.core.namespaces:
            raise NotFound
        return SimpleNamespace(status=SimpleNamespace(ready_replicas=1, available_replicas=1))


class Custom:
    def __init__(self, values: list[OpaqueProviderMetadata]) -> None:
        self.routes: dict[tuple[str, str], dict[str, object]] = {}
        for value in values:
            for suffix in ("control", "transfer"):
                name = f"{value.resource_name}-{suffix}"
                reference = ProviderReference.kubernetes(
                    provider="traefik",
                    api_version="traefik.io/v1alpha1",
                    kind="IngressRoute",
                    namespace=value.resource_name,
                    name=name,
                )
                self.routes[(value.resource_name, name)] = {
                    "metadata": {
                        "name": name,
                        "namespace": value.resource_name,
                        "annotations": {
                            **value.kubernetes_annotations,
                            "exomem.io/recovery-envelope": envelope("traefik", reference, value),
                        },
                    }
                }

    def list_namespaced_custom_object(self, **arguments):
        namespace = arguments["namespace"]
        return {
            "items": [item for (selected, _), item in self.routes.items() if selected == namespace]
        }

    def delete_namespaced_custom_object(self, **arguments):
        self.routes.pop((arguments["namespace"], arguments["name"]), None)

    def get_namespaced_custom_object(self, **arguments):
        try:
            return self.routes[(arguments["namespace"], arguments["name"])]
        except KeyError as error:
            raise NotFound from error


class HCloudVolumes:
    def __init__(self, values: list[OpaqueProviderMetadata]) -> None:
        self.values: dict[int, object] = {}
        for index, value in enumerate(values, start=41):
            reference = ProviderReference.hcloud(kind="volume", resource_id=index)
            labels = {
                **value.hcloud_labels,
                **chunk_hcloud_identity_envelope(envelope("hcloud", reference, value)),
            }
            self.values[index] = SimpleNamespace(id=index, labels=labels)

    def get_all(self, *, label_selector: str):
        del label_selector
        return list(self.values.values())

    def get_by_id(self, identifier: int):
        return self.values.get(identifier)

    def delete(self, value: object):
        self.values.pop(value.id, None)


class B2:
    def __init__(self, locked_until: datetime) -> None:
        self.objects: dict[tuple[str, str], dict[str, object]] = {}
        self.deleted: list[tuple[str, str, bool]] = []
        for bucket, key, kind in (
            ("exports", "tenant/export.enc", "export"),
            ("recovery", "tenant/backup.enc", "backup"),
        ):
            value = metadata("cell-active", operation=f"{kind}-operation", fence=8)
            reference = ProviderReference.b2(bucket=bucket, key=key)
            self.objects[(bucket, key)] = {
                "Metadata": {
                    "identity-envelope": envelope("b2", reference, value),
                    "wrapped-key-reference": f"wrapped:{kind}",
                },
                "ObjectLockRetainUntilDate": locked_until if kind == "backup" else None,
            }

    def list_object_versions(self, **arguments):
        bucket = arguments["Bucket"]
        return {
            "Versions": [
                {"Key": key, "VersionId": "version-current"}
                for selected, key in self.objects
                if selected == bucket
            ],
            "DeleteMarkers": [],
            "IsTruncated": False,
        }

    def head_object(self, *, Bucket: str, Key: str, VersionId: str):
        assert VersionId == "version-current"
        try:
            return {**self.objects[(Bucket, Key)], "VersionId": VersionId}
        except KeyError as error:
            raise NotFound from error

    def delete_object(self, **arguments):
        bucket = arguments["Bucket"]
        key = arguments["Key"]
        assert set(arguments) == {"Bucket", "Key", "VersionId"}
        self.deleted.append((bucket, key, arguments["VersionId"]))
        self.objects.pop((bucket, key), None)


class VersionedB2:
    """Fake the B2 versioning semantics that hide bytes behind delete markers."""

    def __init__(self, retained_until: datetime) -> None:
        self.versions: dict[tuple[str, str, str], dict[str, object]] = {}
        self.delete_markers: set[tuple[str, str, str]] = set()
        self.deleted: list[dict[str, object]] = []
        for bucket, key, kind in (
            ("exports", "tenant/export.enc", "export"),
            ("recovery", "tenant/backup.enc", "backup"),
        ):
            value = metadata("cell-active", operation=f"{kind}-operation", fence=8)
            reference = ProviderReference.b2(bucket=bucket, key=key)
            for version_id in (f"{kind}-current", f"{kind}-older"):
                self.versions[(bucket, key, version_id)] = {
                    "Metadata": {
                        "identity-envelope": envelope("b2", reference, value),
                        "wrapped-key-reference": f"wrapped:{kind}",
                    },
                    "ObjectLockRetainUntilDate": (
                        retained_until if kind == "backup" else None
                    ),
                    "VersionId": version_id,
                }
            self.delete_markers.add((bucket, key, f"{kind}-marker"))

    def list_objects_v2(self, **_arguments):
        raise AssertionError("versioned deletion must not use list_objects_v2")

    def list_object_versions(self, **arguments):
        bucket = arguments["Bucket"]
        prefix = arguments.get("Prefix")
        versions = [
            {"Key": key, "VersionId": version_id}
            for selected, key, version_id in self.versions
            if selected == bucket and (prefix is None or key.startswith(prefix))
        ]
        markers = [
            {"Key": key, "VersionId": version_id}
            for selected, key, version_id in self.delete_markers
            if selected == bucket and (prefix is None or key.startswith(prefix))
        ]
        return {"Versions": versions, "DeleteMarkers": markers, "IsTruncated": False}

    def head_object(self, *, Bucket: str, Key: str, VersionId: str):
        try:
            return self.versions[(Bucket, Key, VersionId)]
        except KeyError as error:
            raise NotFound from error

    def delete_object(self, **arguments):
        assert "VersionId" in arguments
        assert set(arguments) == {"Bucket", "Key", "VersionId"}
        self.deleted.append(dict(arguments))
        identity = (arguments["Bucket"], arguments["Key"], arguments["VersionId"])
        self.versions.pop(identity, None)
        self.delete_markers.discard(identity)


class PrefixRequiredB2(VersionedB2):
    def __init__(self, retained_until: datetime) -> None:
        super().__init__(retained_until)
        self.prefixes: list[tuple[str, str]] = []
        self.heads: list[tuple[str, str, str]] = []

    def list_object_versions(self, **arguments):
        prefix = arguments.get("Prefix")
        assert isinstance(prefix, str) and prefix, "routine deletion used an unbounded B2 scan"
        self.prefixes.append((str(arguments["Bucket"]), prefix))
        return super().list_object_versions(**arguments)

    def head_object(self, *, Bucket: str, Key: str, VersionId: str):
        self.heads.append((Bucket, Key, VersionId))
        return super().head_object(Bucket=Bucket, Key=Key, VersionId=VersionId)


class TruncatedSiblingFloodB2(VersionedB2):
    """Adversarial provider that offers endless sibling-only pages."""

    def __init__(self, retained_until: datetime) -> None:
        super().__init__(retained_until)
        self.calls: dict[tuple[str, str], int] = {}

    def list_object_versions(self, **arguments):
        bucket = str(arguments["Bucket"])
        exact_key = str(arguments["Prefix"])
        identity = (bucket, exact_key)
        self.calls[identity] = self.calls.get(identity, 0) + 1
        assert arguments.get("MaxKeys") == 100
        if self.calls[identity] > 1:
            raise AssertionError("exact-key lookup page-walked prefix siblings")
        sibling = f"{exact_key}-sibling"
        return {
            "Versions": [
                {"Key": sibling, "VersionId": f"sibling-{index:03d}"}
                for index in range(100)
            ],
            "DeleteMarkers": [],
            "IsTruncated": True,
            "NextKeyMarker": sibling,
            "NextVersionIdMarker": "sibling-099",
        }


class Authority:
    def __init__(self, fence: int) -> None:
        self.fence = fence
        self.acquired: list[tuple[str, str, int]] = []
        self.cell_acquired: list[tuple[str, str, str, int]] = []
        self.blocked_cell: str | None = None

    async def current_fence(self, tenant_id: str) -> int:
        return self.fence

    async def acquire(self, tenant_id: str, operation_id: str, fence: int) -> bool:
        self.acquired.append((tenant_id, operation_id, fence))
        return True

    async def acquire_cell(
        self, tenant_id: str, cell_id: str, operation_id: str, fence: int
    ) -> bool:
        self.cell_acquired.append((tenant_id, cell_id, operation_id, fence))
        return cell_id != self.blocked_cell


class Keys:
    def __init__(self) -> None:
        self.deleted: set[tuple[str, str]] = set()
        self.records: list[object] = []
        self.deliveries: list[object] = []

    def add_recovery(
        self,
        *,
        bucket: str,
        key: str,
        kind: str,
        version_id: str,
        retained_until: datetime,
    ) -> None:
        self.records.append(
            SimpleNamespace(
                tenant_id="tenant-alpha",
                cell_id="cell-active",
                operation_id=f"{kind}-operation",
                fence_generation=8,
                kind=(RunKind.USER_EXPORT if kind == "export" else RunKind.VAULT_BACKUP),
                opaque_reference=f"wrapped:{kind}",
                provider_reference=ProviderReference.b2(
                    bucket=bucket,
                    key=key,
                    version_id=version_id,
                ),
                wrapped_data_key=f"wrapped-material:{kind}",
                object_lock_until=retained_until,
                deleted_at=None,
                key_destroyed_at=None,
            )
        )

    async def tenant_recovery_objects(self, tenant_id: str):
        return [record for record in self.records if record.tenant_id == tenant_id]

    async def tenant_export_deliveries(self, tenant_id: str):
        return [record for record in self.deliveries if record.tenant_id == tenant_id]

    async def mark_recovery_object_deleted(self, reference: str, *, tenant_id: str):
        record = next(
            item
            for item in self.records
            if item.tenant_id == tenant_id and item.opaque_reference == reference
        )
        record.deleted_at = datetime(2030, 1, 1, tzinfo=UTC)

    async def mark_export_delivery_deleted(self, reference: str, *, tenant_id: str):
        record = next(
            item for item in self.deliveries if item.tenant_id == tenant_id and item.id == reference
        )
        record.deleted_at = datetime(2030, 1, 1, tzinfo=UTC)

    async def destroy(self, reference: str, *, tenant_id: str):
        self.deleted.add((tenant_id, reference))
        record = next(
            (
                item
                for item in self.records
                if item.tenant_id == tenant_id and item.opaque_reference == reference
            ),
            None,
        )
        if record is not None:
            record.wrapped_data_key = None
            record.key_destroyed_at = datetime(2030, 1, 1, tzinfo=UTC)

    async def absent(self, reference: str, *, tenant_id: str):
        return (tenant_id, reference) in self.deleted

    async def deletion_complete(self, tenant_id: str) -> bool:
        return all(
            record.deleted_at is not None
            and record.wrapped_data_key is None
            and record.key_destroyed_at is not None
            for record in self.records
            if record.tenant_id == tenant_id
        ) and all(
            delivery.deleted_at is not None
            for delivery in self.deliveries
            if delivery.tenant_id == tenant_id
        )


class CrashOnceLedger(Keys):
    """Durable recovery rows survive a worker crash after provider deletion."""

    def __init__(self, retained_until: datetime) -> None:
        super().__init__()
        self.fail_next_destroy = True
        for bucket, key, kind in (
            ("exports", "tenant/export.enc", "export"),
            ("recovery", "tenant/backup.enc", "backup"),
        ):
            self.add_recovery(
                bucket=bucket,
                key=key,
                kind=kind,
                version_id=f"{kind}-current",
                retained_until=retained_until,
            )

    async def destroy(self, reference: str, *, tenant_id: str):
        if self.fail_next_destroy:
            self.fail_next_destroy = False
            raise RuntimeError("worker crashed after provider deletion")
        await super().destroy(reference, tenant_id=tenant_id)
        record = next(item for item in self.records if item.opaque_reference == reference)
        record.wrapped_data_key = None
        record.deleted_at = datetime(2030, 1, 1, tzinfo=UTC)
        record.key_destroyed_at = datetime(2030, 1, 1, tzinfo=UTC)

    async def deletion_complete(self, tenant_id: str) -> bool:
        return all(
            record.deleted_at is not None
            and record.wrapped_data_key is None
            and record.key_destroyed_at is not None
            for record in self.records
            if record.tenant_id == tenant_id
        ) and all(
            delivery.deleted_at is not None
            for delivery in self.deliveries
            if delivery.tenant_id == tenant_id
        )


def build(now: datetime):
    active = metadata("cell-active", operation="active-operation", fence=7)
    candidate = metadata("cell-candidate", operation="candidate-operation", fence=8)
    core = Core([active, candidate])
    custom = Custom([active, candidate])
    hcloud = HCloudVolumes([active, candidate])
    b2 = B2(now + timedelta(days=7))
    authority = Authority(9)
    keys = Keys()
    keys.add_recovery(
        bucket="exports",
        key="tenant/export.enc",
        kind="export",
        version_id="version-current",
        retained_until=now,
    )
    keys.add_recovery(
        bucket="recovery",
        key="tenant/backup.enc",
        kind="backup",
        version_id="version-current",
        retained_until=now + timedelta(days=7),
    )
    provider = LiveDeletionProvider(
        core_v1=core,
        apps_v1=Apps(core),
        custom_objects=custom,
        hcloud_client=SimpleNamespace(volumes=hcloud),
        b2_client=b2,
        recovery_bucket="recovery",
        export_bucket="exports",
        identity_verifier=CODEC.verifier(),
        authority=authority,
        key_store=keys,
    )
    return provider, core, custom, hcloud, b2, authority, keys


def context(cell_id: str | None, *, checkpoint: str = "effect-prepared") -> EffectContext:
    return EffectContext(
        operation_id="database-delete",
        provider_operation_id="delete-operation",
        tenant_id="tenant-alpha",
        cell_id=cell_id,
        fence_generation=9,
        checkpoint=checkpoint,
    )


@pytest.mark.asyncio
async def test_live_candidate_discard_authenticates_inventory_and_preserves_active_and_exports():
    now = datetime(2030, 1, 1, tzinfo=UTC)
    provider, core, custom, hcloud, b2, authority, _ = build(now)

    result = await FencedOrderedDeletionWorkflow(provider, clock=lambda: now).discard_candidate(
        context("cell-candidate")
    )

    assert isinstance(result, DriverFinal)
    assert result.result == {
        "computeDestroyed": True,
        "storageDestroyed": True,
        "keysDestroyed": True,
    }
    assert metadata("cell-active", operation="active-operation", fence=7).resource_name in (
        core.namespaces
    )
    assert len(custom.routes) == 2
    assert all(
        route["metadata"]["namespace"]
        == metadata("cell-active", operation="active-operation", fence=7).resource_name
        for route in custom.routes.values()
    )
    assert set(hcloud.values) == {41}
    assert set(b2.objects) == {
        ("exports", "tenant/export.enc"),
        ("recovery", "tenant/backup.enc"),
    }
    assert authority.acquired == [("tenant-alpha", "delete-operation", 9)]
    assert authority.cell_acquired == [("tenant-alpha", "cell-candidate", "delete-operation", 9)]


@pytest.mark.asyncio
async def test_live_tenant_destroy_waits_for_object_lock_then_deletes_exact_version():
    now = datetime(2030, 1, 1, tzinfo=UTC)
    provider, core, custom, hcloud, b2, authority, keys = build(now)
    clock = [now]
    workflow = FencedOrderedDeletionWorkflow(provider, clock=lambda: clock[0])

    pending = await workflow.destroy_tenant(context(None))

    assert isinstance(pending, DriverPending)
    assert pending.checkpoint == "retained-wait"
    assert set(b2.objects) == {("recovery", "tenant/backup.enc")}
    assert keys.deleted == {("tenant-alpha", "wrapped:export")}
    assert not core.namespaces and not custom.routes and not hcloud.values

    b2.objects[("recovery", "tenant/backup.enc")]["ObjectLockRetainUntilDate"] = now
    clock[0] = now + timedelta(days=7, seconds=1)
    final = await workflow.destroy_tenant(context(None, checkpoint="retained-wait"))

    assert isinstance(final, DriverFinal)
    assert final.result["tenantResourcesDestroyed"] is True
    assert ("recovery", "tenant/backup.enc", "version-current") in b2.deleted
    assert keys.deleted == {
        ("tenant-alpha", "wrapped:export"),
        ("tenant-alpha", "wrapped:backup"),
    }
    assert authority.acquired == [
        ("tenant-alpha", "delete-operation", 9),
        ("tenant-alpha", "delete-operation", 9),
    ]
    assert authority.cell_acquired == [
        ("tenant-alpha", "cell-active", "delete-operation", 9),
        ("tenant-alpha", "cell-candidate", "delete-operation", 9),
        ("tenant-alpha", "cell-active", "delete-operation", 9),
    ]


@pytest.mark.asyncio
async def test_live_tenant_destroy_deletes_every_exact_version_and_marker_without_bypass():
    now = datetime(2030, 1, 1, tzinfo=UTC)
    active = metadata("cell-active", operation="active-operation", fence=7)
    candidate = metadata("cell-candidate", operation="candidate-operation", fence=8)
    core = Core([active, candidate])
    custom = Custom([active, candidate])
    hcloud = HCloudVolumes([active, candidate])
    b2 = VersionedB2(now)
    keys = CrashOnceLedger(now)
    keys.fail_next_destroy = False
    provider = LiveDeletionProvider(
        core_v1=core,
        apps_v1=Apps(core),
        custom_objects=custom,
        hcloud_client=SimpleNamespace(volumes=hcloud),
        b2_client=b2,
        recovery_bucket="recovery",
        export_bucket="exports",
        identity_verifier=CODEC.verifier(),
        authority=Authority(9),
        key_store=keys,
    )

    await provider.bind(context(None))
    inventory = await provider.scan_tenant("tenant-alpha")
    b2_inventory = [resource for resource in inventory if resource.provider == "b2"]
    assert len(b2_inventory) == 6
    assert {
        ProviderReference.parse(resource.reference)["objectVersionId"]
        for resource in b2_inventory
    } == {
        "export-current",
        "export-older",
        "export-marker",
        "backup-current",
        "backup-older",
        "backup-marker",
    }

    result = await FencedOrderedDeletionWorkflow(provider, clock=lambda: now).destroy_tenant(
        context(None)
    )

    assert isinstance(result, DriverFinal)
    assert not b2.versions
    assert not b2.delete_markers
    assert {str(item["VersionId"]) for item in b2.deleted} == {
        "export-current",
        "export-older",
        "export-marker",
        "backup-current",
        "backup-older",
        "backup-marker",
    }
    assert keys.deleted == {
        ("tenant-alpha", "wrapped:export"),
        ("tenant-alpha", "wrapped:backup"),
    }


@pytest.mark.asyncio
async def test_live_tenant_destroy_restart_erases_ledger_key_after_object_was_deleted():
    now = datetime(2030, 1, 1, tzinfo=UTC)
    b2 = VersionedB2(now)
    ledger = CrashOnceLedger(now)

    def provider() -> LiveDeletionProvider:
        core = Core([])
        return LiveDeletionProvider(
            core_v1=core,
            apps_v1=Apps(core),
            custom_objects=Custom([]),
            hcloud_client=SimpleNamespace(volumes=HCloudVolumes([])),
            b2_client=b2,
            recovery_bucket="recovery",
            export_bucket="exports",
            identity_verifier=CODEC.verifier(),
            authority=Authority(9),
            key_store=ledger,
        )

    with pytest.raises(RuntimeError, match="worker crashed"):
        await FencedOrderedDeletionWorkflow(provider(), clock=lambda: now).destroy_tenant(
            context(None)
        )
    assert not b2.versions and not b2.delete_markers

    result = await FencedOrderedDeletionWorkflow(provider(), clock=lambda: now).destroy_tenant(
        context(None, checkpoint="key-absence-verification")
    )

    assert isinstance(result, DriverFinal)
    assert ledger.deleted == {
        ("tenant-alpha", "wrapped:export"),
        ("tenant-alpha", "wrapped:backup"),
    }
    assert await ledger.deletion_complete("tenant-alpha") is True


@pytest.mark.asyncio
async def test_live_tenant_destroy_uses_only_ledger_bounded_exact_key_scans():
    now = datetime(2030, 1, 1, tzinfo=UTC)
    b2 = PrefixRequiredB2(now)
    sibling = metadata("cell-other", operation="other-operation", fence=8)
    sibling_key = "tenant/backup.enc-sibling"
    sibling_version = "other-version"
    sibling_reference = ProviderReference.b2(bucket="recovery", key=sibling_key)
    b2.versions[("recovery", sibling_key, sibling_version)] = {
        "Metadata": {
            "identity-envelope": envelope("b2", sibling_reference, sibling),
            "wrapped-key-reference": "wrapped:other",
        },
        "ObjectLockRetainUntilDate": now,
        "VersionId": sibling_version,
    }
    ledger = CrashOnceLedger(now)
    core = Core([])
    provider = LiveDeletionProvider(
        core_v1=core,
        apps_v1=Apps(core),
        custom_objects=Custom([]),
        hcloud_client=SimpleNamespace(volumes=HCloudVolumes([])),
        b2_client=b2,
        recovery_bucket="recovery",
        export_bucket="exports",
        identity_verifier=CODEC.verifier(),
        authority=Authority(9),
        key_store=ledger,
    )

    await provider.bind(context(None))
    inventory = await provider.scan_tenant("tenant-alpha")

    assert {prefix for _, prefix in b2.prefixes} == {
        "tenant/export.enc",
        "tenant/backup.enc",
    }
    assert ("recovery", sibling_key, sibling_version) not in b2.heads
    assert all(
        ProviderReference.parse(resource.reference).get("key") != sibling_key
        for resource in inventory
        if resource.provider == "b2"
    )


@pytest.mark.asyncio
async def test_exact_b2_lookup_stops_at_truncated_prefix_siblings_in_one_bounded_call():
    now = datetime(2030, 1, 1, tzinfo=UTC)
    b2 = TruncatedSiblingFloodB2(now)
    ledger = CrashOnceLedger(now)
    core = Core([])
    provider = LiveDeletionProvider(
        core_v1=core,
        apps_v1=Apps(core),
        custom_objects=Custom([]),
        hcloud_client=SimpleNamespace(volumes=HCloudVolumes([])),
        b2_client=b2,
        recovery_bucket="recovery",
        export_bucket="exports",
        identity_verifier=CODEC.verifier(),
        authority=Authority(9),
        key_store=ledger,
    )

    await provider.bind(context(None))
    inventory = await provider.scan_tenant("tenant-alpha")

    assert b2.calls == {
        ("exports", "tenant/export.enc"): 1,
        ("recovery", "tenant/backup.enc"): 1,
    }
    assert {
        ProviderReference.parse(resource.reference)["key"]
        for resource in inventory
        if resource.provider == "b2"
    } == {"tenant/export.enc", "tenant/backup.enc"}


@pytest.mark.asyncio
async def test_live_tenant_destroy_keeps_marker_while_its_recovery_version_is_locked():
    now = datetime(2030, 1, 1, tzinfo=UTC)
    locked_until = now + timedelta(days=7)
    b2 = VersionedB2(locked_until)
    ledger = CrashOnceLedger(locked_until)
    ledger.fail_next_destroy = False
    core = Core([])
    provider = LiveDeletionProvider(
        core_v1=core,
        apps_v1=Apps(core),
        custom_objects=Custom([]),
        hcloud_client=SimpleNamespace(volumes=HCloudVolumes([])),
        b2_client=b2,
        recovery_bucket="recovery",
        export_bucket="exports",
        identity_verifier=CODEC.verifier(),
        authority=Authority(9),
        key_store=ledger,
    )

    result = await FencedOrderedDeletionWorkflow(provider, clock=lambda: now).destroy_tenant(
        context(None)
    )

    assert isinstance(result, DriverPending)
    assert result.checkpoint == "retained-wait"
    assert ("recovery", "tenant/backup.enc", "backup-marker") in b2.delete_markers


@pytest.mark.asyncio
async def test_live_tenant_destroy_attributes_marker_only_key_from_durable_ledger():
    now = datetime(2030, 1, 1, tzinfo=UTC)
    b2 = VersionedB2(now)
    for identity in tuple(b2.versions):
        if identity[0] == "recovery":
            b2.versions.pop(identity)
    ledger = CrashOnceLedger(now)
    ledger.fail_next_destroy = False
    core = Core([])
    provider = LiveDeletionProvider(
        core_v1=core,
        apps_v1=Apps(core),
        custom_objects=Custom([]),
        hcloud_client=SimpleNamespace(volumes=HCloudVolumes([])),
        b2_client=b2,
        recovery_bucket="recovery",
        export_bucket="exports",
        identity_verifier=CODEC.verifier(),
        authority=Authority(9),
        key_store=ledger,
    )

    await provider.bind(context(None))
    inventory = await provider.scan_tenant("tenant-alpha")

    assert any(
        resource.delete_marker
        and ProviderReference.parse(resource.reference).get("key") == "tenant/backup.enc"
        for resource in inventory
    )


@pytest.mark.asyncio
async def test_marker_only_recovery_row_honors_future_durable_object_lock():
    now = datetime(2030, 1, 1, tzinfo=UTC)
    locked_until = datetime(2030, 1, 8, tzinfo=UTC)
    b2 = VersionedB2(now)
    for identity in tuple(b2.versions):
        if identity[0] == "recovery":
            b2.versions.pop(identity)
    ledger = CrashOnceLedger(locked_until)
    ledger.fail_next_destroy = False
    recovery = next(record for record in ledger.records if record.kind is RunKind.VAULT_BACKUP)
    provider = LiveDeletionProvider(
        core_v1=Core([]),
        apps_v1=Apps(Core([])),
        custom_objects=Custom([]),
        hcloud_client=SimpleNamespace(volumes=HCloudVolumes([])),
        b2_client=b2,
        recovery_bucket="recovery",
        export_bucket="exports",
        identity_verifier=CODEC.verifier(),
        authority=Authority(9),
        key_store=ledger,
    )

    result = await FencedOrderedDeletionWorkflow(provider, clock=lambda: now).destroy_tenant(
        context(None)
    )

    assert isinstance(result, DriverPending)
    assert result.checkpoint == "retained-wait"
    assert ("recovery", "tenant/backup.enc", "backup-marker") in b2.delete_markers
    assert recovery.deleted_at is None
    assert recovery.wrapped_data_key == "wrapped-material:backup"
    assert recovery.key_destroyed_at is None


@pytest.mark.asyncio
async def test_durable_object_lock_applies_to_every_live_version_before_deletion():
    now = datetime(2030, 1, 1, tzinfo=UTC)
    locked_until = datetime(2030, 1, 8, tzinfo=UTC)
    b2 = VersionedB2(now)
    ledger = CrashOnceLedger(locked_until)
    ledger.fail_next_destroy = False
    core = Core([])
    provider = LiveDeletionProvider(
        core_v1=core,
        apps_v1=Apps(core),
        custom_objects=Custom([]),
        hcloud_client=SimpleNamespace(volumes=HCloudVolumes([])),
        b2_client=b2,
        recovery_bucket="recovery",
        export_bucket="exports",
        identity_verifier=CODEC.verifier(),
        authority=Authority(9),
        key_store=ledger,
    )

    result = await FencedOrderedDeletionWorkflow(provider, clock=lambda: now).destroy_tenant(
        context(None)
    )

    assert isinstance(result, DriverPending)
    assert result.checkpoint == "retained-wait"
    assert {
        identity
        for identity in b2.versions
        if identity[0] == "recovery"
    } == {
        ("recovery", "tenant/backup.enc", "backup-current"),
        ("recovery", "tenant/backup.enc", "backup-older"),
    }
    assert not any(item["Bucket"] == "recovery" for item in b2.deleted)


@pytest.mark.asyncio
async def test_live_tenant_destroy_deletes_recent_plaintext_delivery_from_durable_ledger():
    now = datetime(2030, 1, 1, tzinfo=UTC)
    b2 = VersionedB2(now)
    delivery_key = "user-export-delivery/aa/recent.portable"
    delivery_version = "recent-delivery-v1"
    b2.versions[("exports", delivery_key, delivery_version)] = {
        "Metadata": {"expires-at": (now + timedelta(minutes=15)).isoformat()},
        "ObjectLockRetainUntilDate": None,
        "VersionId": delivery_version,
    }
    ledger = CrashOnceLedger(now)
    ledger.fail_next_destroy = False
    ledger.deliveries.append(
        SimpleNamespace(
            id="delivery-ledger-row",
            source_object_id="source-export-row",
            tenant_id="tenant-alpha",
            cell_id="cell-active",
            operation_id="export-operation",
            fence_generation=8,
            provider_reference=ProviderReference.b2(
                bucket="exports",
                key=delivery_key,
                version_id=delivery_version,
            ),
            expires_at=now + timedelta(minutes=15),
            verified_at=now,
            deleted_at=None,
        )
    )
    core = Core([])
    provider = LiveDeletionProvider(
        core_v1=core,
        apps_v1=Apps(core),
        custom_objects=Custom([]),
        hcloud_client=SimpleNamespace(volumes=HCloudVolumes([])),
        b2_client=b2,
        recovery_bucket="recovery",
        export_bucket="exports",
        identity_verifier=CODEC.verifier(),
        authority=Authority(9),
        key_store=ledger,
    )

    result = await FencedOrderedDeletionWorkflow(provider, clock=lambda: now).destroy_tenant(
        context(None)
    )

    assert isinstance(result, DriverFinal)
    assert ("exports", delivery_key, delivery_version) not in b2.versions
    assert ledger.deliveries[0].deleted_at is not None


@pytest.mark.asyncio
async def test_live_deletion_rejects_stale_fence_before_provider_mutation():
    now = datetime(2030, 1, 1, tzinfo=UTC)
    provider, core, _, _, _, authority, _ = build(now)
    authority.fence = 10

    with pytest.raises(RuntimeError, match="fence"):
        await FencedOrderedDeletionWorkflow(provider).discard_candidate(context("cell-candidate"))

    assert len(core.namespaces) == 2
    assert authority.acquired == []


@pytest.mark.asyncio
async def test_live_credential_deletion_never_reads_secret_payload_and_proves_ordered_receipt():
    now = datetime(2030, 1, 1, tzinfo=UTC)
    provider, core, _, _, _, _, _ = build(now)
    deletion_context = context("cell-candidate")
    await provider.bind(deletion_context)
    resources = await provider.scan_tenant("tenant-alpha")
    credential = next(
        resource
        for resource in resources
        if resource.cell_id == "cell-candidate" and resource.kind.value == "credential"
    )

    await provider.delete_resource(credential)

    assert (
        metadata("cell-candidate", operation="candidate-operation", fence=8).resource_name,
        "exomem-cell-credentials",
    ) not in core.secrets
    assert await provider.resource_absent(credential) is True


@pytest.mark.asyncio
async def test_live_credential_deletion_recovers_after_delete_acknowledgement_is_lost():
    now = datetime(2030, 1, 1, tzinfo=UTC)
    provider, core, _, _, _, _, _ = build(now)
    deletion_context = context("cell-candidate")
    await provider.bind(deletion_context)
    resources = await provider.scan_tenant("tenant-alpha")
    credential = next(
        resource
        for resource in resources
        if resource.cell_id == "cell-candidate" and resource.kind.value == "credential"
    )
    namespace = metadata("cell-candidate", operation="candidate-operation", fence=8).resource_name
    core.secrets.pop((namespace, "exomem-cell-credentials"))

    await provider.delete_resource(credential)

    assert await provider.resource_absent(credential) is True


@pytest.mark.asyncio
async def test_live_volume_deletion_waits_for_pv_absence_before_hcloud_delete():
    now = datetime(2030, 1, 1, tzinfo=UTC)
    provider, core, _, hcloud, _, _, _ = build(now)
    value = metadata("cell-candidate", operation="candidate-operation", fence=8)
    pv_name = "pv-candidate"
    pv_reference = ProviderReference.kubernetes(
        provider="kubernetes",
        api_version="v1",
        kind="PersistentVolume",
        namespace="",
        name=pv_name,
    )
    core.pvs[pv_name] = SimpleNamespace(
        metadata=SimpleNamespace(
            name=pv_name,
            annotations={
                **value.kubernetes_annotations,
                "exomem.io/recovery-envelope": envelope("kubernetes", pv_reference, value),
            },
        ),
        spec=SimpleNamespace(csi=SimpleNamespace(volume_handle="42")),
    )
    core.delay_pv_deletion = True
    await provider.bind(context("cell-candidate"))
    resources = await provider.scan_tenant("tenant-alpha")
    volume = next(
        resource
        for resource in resources
        if resource.cell_id == "cell-candidate" and resource.kind.value == "volume"
    )

    await provider.delete_resource(volume)

    assert pv_name in core.pvs
    assert 42 in hcloud.values

    core.delay_pv_deletion = False
    await provider.delete_resource(volume)

    assert pv_name not in core.pvs
    assert 42 not in hcloud.values


@pytest.mark.asyncio
async def test_live_tenant_destroy_waits_for_every_discovered_cell_lock_before_mutation():
    now = datetime(2030, 1, 1, tzinfo=UTC)
    provider, core, _, _, _, authority, _ = build(now)
    authority.blocked_cell = "cell-candidate"

    with pytest.raises(DeletionLeaseBusy, match="cell operation lock"):
        await FencedOrderedDeletionWorkflow(provider).destroy_tenant(context(None))

    assert len(core.namespaces) == 2
