from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from exomem_provisioner.driver import DriverFinal, EffectContext
from exomem_provisioner.durability_driver import DurabilityActionDriver
from exomem_provisioner.production import build_live_routine_action_driver
from exomem_provisioner.production_durability import (
    B2PortableArchiveStager,
    CandidateBoundRestoreWorkflow,
    DynamicKubernetesRestoreRuntime,
    RefreshingExportWorkflow,
)


@pytest.mark.asyncio
async def test_action_export_refreshes_live_target_inventory_before_provider_effect() -> None:
    events: list[str] = []

    class TargetSource:
        async def list_backup_targets(self):
            events.append("inventory")
            return [SimpleNamespace(cell_id="cell-alpha")]

    class Workflow:
        async def run(self, run, *, worker_id, expires_at=None):
            events.append(f"export:{run.identity.cell_id}:{worker_id}:{expires_at.isoformat()}")
            return "export-proof"

    expires = datetime(2030, 1, 1, tzinfo=UTC)
    run = SimpleNamespace(identity=SimpleNamespace(cell_id="cell-alpha"))
    workflow = RefreshingExportWorkflow(TargetSource(), Workflow())

    result = await workflow.run(run, worker_id="worker-alpha", expires_at=expires)

    assert result == "export-proof"
    assert events == ["inventory", f"export:cell-alpha:worker-alpha:{expires.isoformat()}"]


@pytest.mark.asyncio
async def test_action_export_fails_closed_when_target_is_not_in_authenticated_inventory() -> None:
    class TargetSource:
        async def list_backup_targets(self):
            return [SimpleNamespace(cell_id="another-cell")]

    class Workflow:
        async def run(self, *args, **kwargs):
            raise AssertionError("provider effect must not start")

    workflow = RefreshingExportWorkflow(TargetSource(), Workflow())
    run = SimpleNamespace(identity=SimpleNamespace(cell_id="cell-alpha"))

    with pytest.raises(RuntimeError, match="authenticated durability inventory"):
        await workflow.run(run, worker_id="worker-alpha")


@pytest.mark.asyncio
async def test_restore_binds_the_candidate_to_source_vault_before_downloading() -> None:
    events: list[str] = []

    class Runtime:
        async def bind_candidate(self, candidate_cell_id, *, source_vault_id):
            events.append(f"bind:{candidate_cell_id}:{source_vault_id}")

    class Workflow:
        async def run(self, run, **arguments):
            events.append(f"restore:{run.identity.cell_id}:{arguments['expected_source_cell_id']}")
            return {"restored": True}

    run = SimpleNamespace(
        identity=SimpleNamespace(cell_id="cell-candidate", tenant_id="tenant-alpha")
    )
    workflow = CandidateBoundRestoreWorkflow(Runtime(), Workflow())

    result = await workflow.run(
        run,
        worker_id="restore-worker",
        source_reference="opaque-source",
        expected_source_cell_id="cell-source",
        expected_archive_sha256="a" * 64,
        expected_manifest_sha256="b" * 64,
        expected_archive_size=1024,
    )

    assert result == {"restored": True}
    assert events == [
        "bind:cell-candidate:tenant-alpha",
        "restore:cell-candidate:cell-source",
    ]


@pytest.mark.asyncio
async def test_dynamic_restore_runtime_resolves_one_source_bound_real_adapter() -> None:
    events: list[str] = []

    class Adapter:
        async def inspect_portable_archive(self, path):
            events.append(f"inspect:{path}")
            return "inspection"

        async def stop_candidate(self, candidate_cell_id):
            events.append(f"stop:{candidate_cell_id}")

        async def offline_restore(self, candidate_cell_id, archive_path, **arguments):
            events.append(
                f"restore:{candidate_cell_id}:{archive_path}:{arguments['source_cell_id']}"
            )

        async def authenticated_readiness(self, candidate_cell_id):
            events.append(f"ready:{candidate_cell_id}")
            return True

        async def product_checks(self, candidate_cell_id):
            events.append(f"checks:{candidate_cell_id}")
            return {"candidateIdentity": True}

        async def finalize_candidate(self, candidate_cell_id):
            events.append(f"finalize:{candidate_cell_id}")
            return {"restart": True}

    class Resolver:
        async def resolve(self, candidate_cell_id, *, source_vault_id):
            events.append(f"resolve:{candidate_cell_id}:{source_vault_id}")
            return Adapter()

    runtime = DynamicKubernetesRestoreRuntime(Resolver())
    await runtime.bind_candidate("cell-candidate", source_vault_id="tenant-alpha")

    assert await runtime.inspect_portable_archive("archive") == "inspection"
    await runtime.stop_candidate("cell-candidate")
    await runtime.offline_restore(
        "cell-candidate",
        "archive",
        helper_version="1",
        release_version="0.22.0",
        operation_id="restore-alpha",
        fence_generation=8,
        source_cell_id="cell-source",
        archive_sha256="a" * 64,
        artifact_reference="opaque-source",
    )
    assert await runtime.authenticated_readiness("cell-candidate") is True
    assert await runtime.product_checks("cell-candidate") == {"candidateIdentity": True}
    assert await runtime.finalize_candidate("cell-candidate") == {"restart": True}
    assert events == [
        "resolve:cell-candidate:tenant-alpha",
        "inspect:archive",
        "stop:cell-candidate",
        "restore:cell-candidate:archive:cell-source",
        "ready:cell-candidate",
        "checks:cell-candidate",
        "finalize:cell-candidate",
    ]


@pytest.mark.asyncio
async def test_dynamic_restore_runtime_rejects_use_before_source_binding() -> None:
    runtime = DynamicKubernetesRestoreRuntime(SimpleNamespace())

    with pytest.raises(RuntimeError, match="not source-bound"):
        await runtime.stop_candidate("cell-candidate")


@pytest.mark.asyncio
async def test_restore_stager_uses_deterministic_expiring_delivery_object(tmp_path) -> None:
    archive = tmp_path / "restore.portable"
    archive.write_bytes(b"portable")
    digest = "a" * 64
    uploaded: list[tuple[str, dict[str, str]]] = []

    class Store:
        async def put_file(self, key, source, *, metadata, retain_until):
            assert source == archive and retain_until is None
            uploaded.append((key, metadata))
            return SimpleNamespace(
                key=key,
                size=archive.stat().st_size,
                metadata=metadata,
                retain_until=None,
            )

        async def presigned_download(self, key, *, ttl_seconds):
            assert ttl_seconds == 900
            return f"https://s3.example.invalid/{key}?signature=secret"

    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = Store()
    stager = B2PortableArchiveStager(store, clock=lambda: now)
    staged = await stager.stage(
        archive,
        operation_id="restore-operation",
        archive_sha256=digest,
    )
    now += timedelta(minutes=5)
    replayed = await stager.stage(
        archive,
        operation_id="restore-operation",
        archive_sha256=digest,
    )

    assert uploaded[0][0].startswith("user-export-delivery/restore-staging/")
    assert uploaded[0][1] == {
        "expires-at": "2030-01-01T00:15:00Z",
        "archive-sha256": digest,
        "archive-size": str(archive.stat().st_size),
        "operation-digest": hashlib.sha256(b"restore-operation").hexdigest(),
        "purpose": "restore-staging",
    }
    assert staged.allowed_host == "s3.example.invalid"
    assert staged.url.endswith("?signature=secret")
    assert uploaded[1][0] != uploaded[0][0]
    assert uploaded[1][1]["expires-at"] == "2030-01-01T00:20:00Z"
    assert replayed.identity_sha256 != staged.identity_sha256
    assert len(staged.identity_sha256) == 64


@pytest.mark.asyncio
async def test_production_routine_driver_dispatches_lifecycle_and_object_actions() -> None:
    events: list[str] = []

    class Lifecycle:
        async def observed_fence(self, tenant_id):
            return 8

        async def execute(self, action, request, context):
            events.append(f"lifecycle:{action}")
            return DriverFinal({"live": True})

    class Objects:
        async def release(self, reference, *, tenant_id):
            events.append(f"release:{tenant_id}:{reference}")
            return {"released": True}

        async def download(self, *args, **kwargs):
            raise AssertionError

        async def delete(self, *args, **kwargs):
            raise AssertionError

    driver = build_live_routine_action_driver(
        lifecycle_driver=Lifecycle(),
        durability_repository=SimpleNamespace(),
        export_workflow=SimpleNamespace(),
        restore_workflow=SimpleNamespace(),
        object_service=Objects(),
    )
    context = EffectContext(
        "operation-alpha",
        "provider-alpha",
        "tenant-alpha",
        "cell-alpha",
        8,
    )

    assert isinstance(driver, DurabilityActionDriver)
    assert (await driver.execute("health", {}, context)).result == {"live": True}
    assert (
        await driver.execute(
            "export-release",
            {"releaseRef": "release-alpha"},
            context,
        )
    ).result == {}
    assert events == [
        "lifecycle:health",
        "release:tenant-alpha:release-alpha",
    ]
