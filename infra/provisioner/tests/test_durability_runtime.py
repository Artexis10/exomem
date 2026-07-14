from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem_provisioner.config import ProvisionerSettings
from exomem_provisioner.crypto import AesGcmEnvelopeCodec
from exomem_provisioner.database import ProvisionerDatabase
from exomem_provisioner.deletion import DeletionVerificationError
from exomem_provisioner.driver import (
    DriverFinal,
    DriverPending,
    DriverRetryable,
    DriverTerminal,
    EffectContext,
)
from exomem_provisioner.durability_runtime import (
    BucketScopedB2Client,
    DeletionClaimAuthority,
    DeletionLeaseBusy,
    DeletionOnlyDriver,
    DeletionRuntimeSettings,
    RepositoryWrappedKeyStore,
    build_deletion_operation_worker,
    build_live_deletion_provider,
)
from exomem_provisioner.models import OperationAction, OperationState
from exomem_provisioner.production import (
    build_routine_operation_worker,
)
from exomem_provisioner.production_durability import build_durability_operation_worker
from exomem_provisioner.provider_identity import (
    ProviderRecoveryIdentityVerifier as CanonicalProviderRecoveryIdentityVerifier,
)
from exomem_provisioner.repository import OperationRepository
from exomem_provisioner.worker_ownership import (
    DELETION_OPERATION_ACTIONS,
    DURABILITY_OPERATION_ACTIONS,
    ROUTINE_OPERATION_ACTIONS,
)


def _settings(path: Path) -> ProvisionerSettings:
    return ProvisionerSettings(
        bearer="b" * 32,
        envelope_key="k" * 32,
        database_url=f"sqlite+aiosqlite:///{path}",
        database_schema="exomem_provisioner",
        database_role="exomem_provisioner_runtime",
        trusted_proxy_ips="127.0.0.1",
    )


def _deletion_settings(**overrides: object) -> DeletionRuntimeSettings:
    values: dict[str, object] = {
        "database_url": "postgresql+asyncpg://exomem_provisioner_runtime:secret@db.invalid/app",
        "database_schema": "exomem_provisioner",
        "database_role": "exomem_provisioner_runtime",
        "envelope_key": "wrapping-key-material-which-is-long-enough",
        "claim_seconds": 30,
        "max_failure_attempts": 6,
        "provider_recovery_public_key": "BOGUS-UNTIL-OVERRIDDEN",
        "hcloud_token": "hcloud-delete-token",
        "b2_endpoint_url": "https://s3.us-west-004.backblazeb2.com",
        "b2_region": "us-west-004",
        "recovery_bucket": "exomem-recovery-deadbeef",
        "user_export_bucket": "exomem-export-deadbeef",
        "recovery_delete_key_id": "recovery-key-id",
        "recovery_delete_key": "recovery-key-secret",
        "user_export_delete_key_id": "export-key-id",
        "user_export_delete_key": "export-key-secret",
    }
    values.update(overrides)
    return DeletionRuntimeSettings(**values)


def _request(*, operation_id: str, tenant_id: str, cell_id: str | None = None) -> dict[str, object]:
    value: dict[str, object] = {
        "operationId": operation_id,
        "checkpoint": "online-revoked-and-sealed",
        "fenceGeneration": 8,
        "tenantId": tenant_id,
    }
    if cell_id is not None:
        value["cellId"] = cell_id
    return value


@pytest.mark.asyncio
async def test_deletion_authority_requires_the_live_claim_and_exact_current_fence(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "authority.sqlite")
    database = ProvisionerDatabase(settings)
    await database.create_for_tests()
    repository = OperationRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
    )
    authority = DeletionClaimAuthority(database.session_factory)
    try:
        await repository.submit(
            "destroy",
            "destroy-authority",
            _request(operation_id="provider-destroy-alpha", tenant_id="tenant-alpha"),
        )
        assert await authority.current_fence("tenant-alpha") == 8
        assert await authority.acquire("tenant-alpha", "provider-destroy-alpha", 8) is False

        claimed = await repository.claim_next("deletion-worker")
        assert claimed is not None
        assert await authority.acquire("tenant-alpha", "provider-destroy-alpha", 7) is False
        assert await authority.acquire("tenant-other", "provider-destroy-alpha", 8) is False
        assert await authority.acquire("tenant-alpha", "provider-destroy-alpha", 8) is True
        assert (
            await authority.acquire_cell(
                "tenant-alpha", "rediscovered-cell", "provider-destroy-alpha", 8
            )
            is True
        )
    finally:
        await database.dispose()


def test_deletion_settings_are_verifier_only_and_bind_exact_bucket_contract() -> None:
    from exomem_provisioner.provider_recovery import ProviderRecoveryIdentityCodec

    public_key = ProviderRecoveryIdentityCodec.from_secret("deletion-test-root").public_key()
    settings = _deletion_settings(provider_recovery_public_key=public_key)

    assert settings.recovery_bucket == "exomem-recovery-deadbeef"
    assert settings.user_export_bucket == "exomem-export-deadbeef"
    assert "sign" not in " ".join(type(settings).model_fields).lower()
    with pytest.raises(ValueError):
        _deletion_settings(
            provider_recovery_public_key=public_key,
            user_export_bucket="exomem-recovery-deadbeef",
        )


def test_production_worker_action_sets_have_one_owner_for_every_action() -> None:
    ownership = {
        action: sum(
            action in actions
            for actions in (
                ROUTINE_OPERATION_ACTIONS,
                DURABILITY_OPERATION_ACTIONS,
                DELETION_OPERATION_ACTIONS,
            )
        )
        for action in OperationAction
    }

    assert len(ownership) == 14
    assert set(ownership.values()) == {1}
    assert DELETION_OPERATION_ACTIONS == frozenset(
        {OperationAction.DISCARD, OperationAction.DESTROY}
    )
    assert DURABILITY_OPERATION_ACTIONS == frozenset(
        {
            OperationAction.EXPORT,
            OperationAction.RESTORE,
            OperationAction.EXPORT_RELEASE,
            OperationAction.EXPORT_DOWNLOAD,
            OperationAction.EXPORT_DELETE,
        }
    )


def test_production_builders_pass_explicit_disjoint_action_allowlists() -> None:
    repository = object()
    routine = build_routine_operation_worker(
        repository=repository,  # type: ignore[arg-type]
        driver=object(),  # type: ignore[arg-type]
        worker_id="routine-worker",
    )
    deletion = build_deletion_operation_worker(
        repository=repository,  # type: ignore[arg-type]
        workflow=object(),  # type: ignore[arg-type]
        authority=object(),  # type: ignore[arg-type]
        worker_id="deletion-worker",
    )
    durability = build_durability_operation_worker(
        repository=repository,  # type: ignore[arg-type]
        driver=object(),  # type: ignore[arg-type]
        worker_id="durability-worker",
    )

    assert routine._allowed_actions == ROUTINE_OPERATION_ACTIONS
    assert durability._allowed_actions == DURABILITY_OPERATION_ACTIONS
    assert deletion._allowed_actions == DELETION_OPERATION_ACTIONS
    assert routine._allowed_actions.isdisjoint(durability._allowed_actions)
    assert routine._allowed_actions.isdisjoint(deletion._allowed_actions)
    assert durability._allowed_actions.isdisjoint(deletion._allowed_actions)


def test_deletion_runtime_uses_the_live_provider_canonical_identity_verifier() -> None:
    from exomem_provisioner import durability_runtime

    assert (
        durability_runtime.ProviderRecoveryIdentityVerifier
        is CanonicalProviderRecoveryIdentityVerifier
    )


@pytest.mark.asyncio
async def test_production_deletion_builder_performs_a_canonical_empty_provider_scan() -> None:
    from exomem_provisioner.provider_identity import ProviderRecoveryIdentityCodec

    class Core:
        def list_namespace(self, *, label_selector):
            assert label_selector == "exomem.io/tenant-cell=true"
            return SimpleNamespace(items=[])

    class Volumes:
        def get_all(self, *, label_selector):
            assert label_selector
            return []

    class B2:
        def list_object_versions(self, **arguments):
            raise AssertionError(f"empty durable ledger must not scan B2: {arguments}")

    class Ledger:
        async def tenant_recovery_objects(self, tenant_id):
            assert tenant_id == "tenant-alpha"
            return []

        async def tenant_export_deliveries(self, tenant_id):
            assert tenant_id == "tenant-alpha"
            return []

    class Authority:
        async def current_fence(self, tenant_id):
            assert tenant_id == "tenant-alpha"
            return 8

        async def acquire(self, tenant_id, operation_id, fence):
            return (tenant_id, operation_id, fence) == (
                "tenant-alpha",
                "destroy-alpha",
                8,
            )

        async def acquire_cell(self, *args):
            raise AssertionError("an empty scan has no cell lock to acquire")

    codec = ProviderRecoveryIdentityCodec.from_secret("canonical-production-root")
    provider = build_live_deletion_provider(
        core_v1=Core(),
        apps_v1=SimpleNamespace(),
        custom_objects=SimpleNamespace(),
        hcloud_client=SimpleNamespace(volumes=Volumes()),
        b2_client=B2(),
        recovery_bucket="recovery-bucket",
        export_bucket="export-bucket",
        provider_recovery_public_key=codec.public_key(),
        authority=Authority(),
        key_store=Ledger(),
    )
    context = EffectContext(
        "operation-alpha",
        "destroy-alpha",
        "tenant-alpha",
        None,
        8,
    )

    await provider.bind(context)

    assert await provider.scan_tenant("tenant-alpha") == ()
    assert isinstance(provider._verifier, CanonicalProviderRecoveryIdentityVerifier)


def test_bucket_scoped_b2_client_dispatches_only_the_exact_bucket() -> None:
    class Client:
        def __init__(self, name: str) -> None:
            self.name = name
            self.calls: list[tuple[str, dict[str, object]]] = []

        def list_object_versions(self, **kwargs):
            self.calls.append(("list", kwargs))
            return {"Versions": [], "DeleteMarkers": []}

        def head_object(self, **kwargs):
            self.calls.append(("head", kwargs))
            return {"Metadata": {}}

        def delete_object(self, **kwargs):
            self.calls.append(("delete", kwargs))
            return {}

    recovery = Client("recovery")
    exports = Client("exports")
    client = BucketScopedB2Client(
        {
            "exomem-recovery-deadbeef": recovery,
            "exomem-export-deadbeef": exports,
        }
    )

    client.list_object_versions(Bucket="exomem-recovery-deadbeef")
    client.head_object(Bucket="exomem-export-deadbeef", Key="opaque")
    client.delete_object(
        Bucket="exomem-recovery-deadbeef",
        Key="opaque",
        VersionId="version-opaque",
    )
    assert [call[0] for call in recovery.calls] == ["list", "delete"]
    assert [call[0] for call in exports.calls] == ["head"]
    with pytest.raises(ValueError, match="outside deletion scope"):
        client.list_object_versions(Bucket="database-backup")


@pytest.mark.asyncio
async def test_discard_authority_cannot_acquire_a_different_cell(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "discard-authority.sqlite")
    database = ProvisionerDatabase(settings)
    await database.create_for_tests()
    repository = OperationRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
    )
    authority = DeletionClaimAuthority(database.session_factory)
    try:
        await repository.submit(
            "discard",
            "discard-authority",
            _request(
                operation_id="provider-discard-alpha",
                tenant_id="tenant-alpha",
                cell_id="candidate-alpha",
            ),
        )
        assert await repository.claim_next("deletion-worker") is not None
        assert (
            await authority.acquire_cell(
                "tenant-alpha", "candidate-alpha", "provider-discard-alpha", 8
            )
            is True
        )
        assert (
            await authority.acquire_cell("tenant-alpha", "active-cell", "provider-discard-alpha", 8)
            is False
        )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_wrapped_key_store_marks_provider_absence_before_key_destruction() -> None:
    events: list[str] = []

    class Repository:
        async def mark_recovery_object_deleted(self, reference, *, tenant_id):
            events.append(f"absent:{tenant_id}:{reference}")

        async def destroy_recovery_wrapped_key(self, reference, *, tenant_id):
            events.append(f"destroyed:{tenant_id}:{reference}")

        async def tenant_recovery_objects(self, tenant_id):
            return [
                SimpleNamespace(
                    tenant_id=tenant_id,
                    opaque_reference="wrapped-alpha",
                    wrapped_data_key=None,
                    key_destroyed_at=datetime(2030, 1, 1, tzinfo=UTC),
                )
            ]

    store = RepositoryWrappedKeyStore(Repository())
    await store.destroy("wrapped-alpha", tenant_id="tenant-alpha")

    assert events == [
        "absent:tenant-alpha:wrapped-alpha",
        "destroyed:tenant-alpha:wrapped-alpha",
    ]
    assert await store.absent("wrapped-alpha", tenant_id="tenant-alpha") is True
    assert await store.absent("wrapped-other", tenant_id="tenant-alpha") is False


@pytest.mark.asyncio
async def test_deletion_driver_dispatches_only_discard_and_destroy() -> None:
    class Authority:
        async def current_fence(self, tenant_id: str) -> int:
            return 8

    class Workflow:
        async def discard_candidate(self, context: EffectContext):
            return DriverFinal({"discarded": context.cell_id})

        async def destroy_tenant(self, context: EffectContext):
            return DriverPending("retained-wait", 300)

    driver = DeletionOnlyDriver(authority=Authority(), workflow=Workflow())
    context = EffectContext("internal", "provider", "tenant-alpha", "cell-alpha", 8)

    assert await driver.observed_fence("tenant-alpha") == 8
    assert (await driver.execute("discard", {}, context)).result == {"discarded": "cell-alpha"}
    assert (await driver.execute("destroy", {}, context)).checkpoint == "retained-wait"
    with pytest.raises(DriverTerminal, match="PROVISIONER_DELETION_ACTION_SCOPE"):
        await driver.execute("provision", {}, context)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (DeletionLeaseBusy("held"), DriverPending("deletion-lock-busy", 2)),
        (
            DeletionVerificationError("sensitive provider detail"),
            "PROVISIONER_DELETION_VERIFICATION_FAILED",
        ),
        (RuntimeError("sensitive SDK detail"), "PROVISIONER_DELETION_PROVIDER_RETRY"),
    ],
)
async def test_deletion_driver_maps_provider_failures_without_leaking_detail(
    failure: Exception,
    expected: DriverPending | str,
) -> None:
    class Authority:
        async def current_fence(self, tenant_id: str) -> int:
            return 8

    class Workflow:
        async def discard_candidate(self, context: EffectContext):
            raise failure

        async def destroy_tenant(self, context: EffectContext):
            raise AssertionError("wrong action")

    driver = DeletionOnlyDriver(authority=Authority(), workflow=Workflow())
    context = EffectContext("internal", "provider", "tenant-alpha", "cell-alpha", 8)

    if isinstance(expected, DriverPending):
        assert await driver.execute("discard", {}, context) == expected
    else:
        error_type = (
            DriverTerminal
            if expected == "PROVISIONER_DELETION_VERIFICATION_FAILED"
            else DriverRetryable
        )
        with pytest.raises(error_type, match=expected) as caught:
            await driver.execute("discard", {}, context)
        assert "sensitive" not in str(caught.value)


@pytest.mark.asyncio
async def test_deletion_worker_claims_only_discard_and_destroy(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "deletion-worker.sqlite")
    database = ProvisionerDatabase(settings)
    await database.create_for_tests()
    repository = OperationRepository(
        database.session_factory,
        codec=AesGcmEnvelopeCodec.from_secret(settings.envelope_key.get_secret_value()),
    )

    class Workflow:
        async def discard_candidate(self, context: EffectContext):
            return DriverFinal(
                {
                    "computeDestroyed": True,
                    "storageDestroyed": True,
                    "keysDestroyed": True,
                }
            )

        async def destroy_tenant(self, context: EffectContext):
            return DriverFinal(
                {
                    "computeDestroyed": True,
                    "storageDestroyed": True,
                    "keysDestroyed": True,
                    "tenantResourcesDestroyed": True,
                }
            )

    try:
        provision = await repository.submit(
            "provision",
            "provision-must-remain",
            {
                **_request(operation_id="provider-provision", tenant_id="tenant-provision"),
                "cellId": "cell-provision",
            },
        )
        destroy = await repository.submit(
            "destroy",
            "destroy-must-run",
            _request(operation_id="provider-destroy", tenant_id="tenant-destroy"),
        )
        worker = build_deletion_operation_worker(
            repository=repository,
            workflow=Workflow(),
            authority=DeletionClaimAuthority(database.session_factory),
            worker_id="deletion-worker",
        )

        assert await worker.run_once() is True
        untouched = await repository.get_by_id(provision.id)
        completed = await repository.get_by_id(destroy.id)
        assert untouched is not None and untouched.state is OperationState.PENDING
        assert completed is not None and completed.state is OperationState.FINAL
    finally:
        await database.dispose()
