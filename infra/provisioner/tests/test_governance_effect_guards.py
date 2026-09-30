from __future__ import annotations

import inspect
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from exomem_provisioner.adapters import (
    KubernetesCellAdapter,
    KubernetesMaintenanceLeaseAdapter,
    KubernetesVaultFingerprintAdapter,
    TraefikRoutingAdapter,
)
from exomem_provisioner.driver import DriverRetryable, LostAcknowledgement
from exomem_provisioner.lifecycle import MetadataConflict, OpaqueProviderMetadata
from exomem_provisioner.repository import ClaimConflict


def test_governance_effect_adapters_accept_optional_keyword_only_guards() -> None:
    for method in (
        KubernetesMaintenanceLeaseAdapter.acquire,
        KubernetesMaintenanceLeaseAdapter.release,
        KubernetesCellAdapter.scale,
        KubernetesCellAdapter.stage_authorization_session_revision,
        KubernetesVaultFingerprintAdapter.fingerprint,
        TraefikRoutingAdapter.disable,
    ):
        parameter = inspect.signature(method).parameters["effect_guard"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is None


METADATA = OpaqueProviderMetadata("tenant-alpha", "cell-alpha", "operation-alpha", 7)


async def _lost_guard() -> None:
    raise ClaimConflict("AUTHORITY_LOST")


async def _allow_guard() -> None:
    return None


def _stateful_set() -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=METADATA.resource_name,
            namespace=METADATA.resource_name,
            uid="statefulset-alpha",
            resource_version="7",
            annotations=METADATA.kubernetes_annotations,
            deletion_timestamp=None,
        )
    )


@pytest.mark.parametrize("method", ["scale", "stage"])
async def test_cell_writes_propagate_guard_loss_after_authenticated_predecessor_read(
    method: str,
) -> None:
    class Apps:
        writes: list[tuple[str, dict[str, object]]] = []

        def read_namespaced_stateful_set(self, name: str, namespace: str):
            assert (name, namespace) == (METADATA.resource_name, METADATA.resource_name)
            return _stateful_set()

        def patch_namespaced_stateful_set_scale(self, name: str, namespace: str, body):
            self.writes.append(("scale", body))

        def patch_namespaced_stateful_set(self, name: str, namespace: str, body):
            self.writes.append(("stage", body))

    apps = Apps()
    adapter = KubernetesCellAdapter(core_v1=SimpleNamespace(), apps_v1=apps)
    with pytest.raises(ClaimConflict, match="AUTHORITY_LOST"):
        if method == "scale":
            await adapter.scale(METADATA, 0, effect_guard=_lost_guard)
        else:
            await adapter.stage_authorization_session_revision(
                METADATA, "a" * 64, effect_guard=_lost_guard
            )
    assert apps.writes == []


async def test_guarded_stopped_scale_handles_absent_statefulset_without_starting_one() -> None:
    class Missing(Exception):
        status = 404

    class Apps:
        writes: list[dict[str, object]] = []

        def read_namespaced_stateful_set(self, name: str, namespace: str):
            raise Missing

        def patch_namespaced_stateful_set_scale(self, name: str, namespace: str, body):
            self.writes.append(body)

    apps = Apps()
    adapter = KubernetesCellAdapter(core_v1=SimpleNamespace(), apps_v1=apps)
    await adapter.scale(METADATA, 0, effect_guard=_allow_guard)
    assert apps.writes == []


async def test_guarded_stopped_scale_rechecks_authority_after_absent_statefulset_read() -> None:
    class Missing(Exception):
        status = 404

    class Apps:
        def read_namespaced_stateful_set(self, name: str, namespace: str):
            raise Missing

    adapter = KubernetesCellAdapter(core_v1=SimpleNamespace(), apps_v1=Apps())
    with pytest.raises(ClaimConflict, match="AUTHORITY_LOST"):
        await adapter.scale(METADATA, 0, effect_guard=_lost_guard)


async def test_guarded_start_refuses_an_absent_statefulset() -> None:
    class Missing(Exception):
        status = 404

    class Apps:
        def read_namespaced_stateful_set(self, name: str, namespace: str):
            raise Missing

    adapter = KubernetesCellAdapter(core_v1=SimpleNamespace(), apps_v1=Apps())
    with pytest.raises(MetadataConflict):
        await adapter.scale(METADATA, 1, effect_guard=_allow_guard)


async def test_lease_create_and_release_propagate_guard_loss_without_writes() -> None:
    class Missing(Exception):
        status = 404

    class Coordination:
        created: list[dict[str, object]] = []
        deleted: list[dict[str, object]] = []
        lease = None

        def read_namespaced_lease(self, name: str, namespace: str):
            if self.lease is None:
                raise Missing
            return self.lease

        def create_namespaced_lease(self, namespace: str, body: dict[str, object]):
            self.created.append(body)

        def delete_namespaced_lease(self, name: str, namespace: str, *, body: dict[str, object]):
            self.deleted.append(body)

    coordination = Coordination()
    adapter = KubernetesMaintenanceLeaseAdapter(
        coordination_v1=coordination,
        now=lambda: datetime(2030, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(ClaimConflict, match="AUTHORITY_LOST"):
        await adapter.acquire(METADATA, METADATA.operation_id, effect_guard=_lost_guard)
    assert coordination.created == []

    coordination.lease = SimpleNamespace(
        metadata=SimpleNamespace(
            name=METADATA.resource_name + "-maintenance",
            namespace=METADATA.resource_name,
            annotations=METADATA.kubernetes_annotations,
            uid="lease",
            resource_version="1",
            deletion_timestamp=None,
        ),
        spec=SimpleNamespace(
            holder_identity=METADATA.operation_id,
            lease_duration_seconds=120,
            renew_time=datetime(2030, 1, 1, tzinfo=UTC),
        ),
    )
    with pytest.raises(ClaimConflict, match="AUTHORITY_LOST"):
        await adapter.release(METADATA, METADATA.operation_id, effect_guard=_lost_guard)
    assert coordination.deleted == []


async def test_guarded_lease_release_refuses_a_foreign_holder_without_bypassing_the_guard() -> None:
    class Coordination:
        deleted: list[dict[str, object]] = []

        def read_namespaced_lease(self, name: str, namespace: str):
            return SimpleNamespace(
                metadata=SimpleNamespace(
                    name=METADATA.resource_name + "-maintenance",
                    namespace=METADATA.resource_name,
                    uid="lease-alpha",
                    resource_version="1",
                    annotations=METADATA.kubernetes_annotations,
                    deletion_timestamp=None,
                ),
                spec=SimpleNamespace(
                    holder_identity="foreign-operation",
                    lease_duration_seconds=120,
                    renew_time=datetime(2030, 1, 1, tzinfo=UTC),
                ),
            )

        def delete_namespaced_lease(self, name: str, namespace: str, *, body: dict[str, object]):
            self.deleted.append(body)

    coordination = Coordination()
    adapter = KubernetesMaintenanceLeaseAdapter(
        coordination_v1=coordination,
        now=lambda: datetime(2030, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(MetadataConflict):
        await adapter.release(METADATA, METADATA.operation_id, effect_guard=_allow_guard)
    assert coordination.deleted == []


@pytest.mark.parametrize(
    "holder,renew,duration",
    [
        (METADATA.operation_id, datetime(2030, 1, 1, 0, 0, 1, tzinfo=UTC), 120),
        ("", datetime(2030, 1, 1, tzinfo=UTC), 120),
        (METADATA.operation_id, datetime(2030, 1, 1, tzinfo=UTC), True),
    ],
)
async def test_guarded_lease_acquire_refuses_invalid_evidence_before_renewal(
    holder: str,
    renew: datetime,
    duration: object,
) -> None:
    class Coordination:
        replaced: list[dict[str, object]] = []

        def read_namespaced_lease(self, name: str, namespace: str):
            return SimpleNamespace(
                metadata=SimpleNamespace(
                    name=METADATA.resource_name + "-maintenance",
                    namespace=METADATA.resource_name,
                    uid="lease-alpha",
                    resource_version="1",
                    annotations=METADATA.kubernetes_annotations,
                    deletion_timestamp=None,
                ),
                spec=SimpleNamespace(
                    holder_identity=holder,
                    lease_duration_seconds=duration,
                    renew_time=renew,
                ),
            )

        def replace_namespaced_lease(self, name: str, namespace: str, body: dict[str, object]):
            self.replaced.append(body)

    coordination = Coordination()
    adapter = KubernetesMaintenanceLeaseAdapter(
        coordination_v1=coordination,
        now=lambda: datetime(2030, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(MetadataConflict):
        await adapter.acquire(METADATA, METADATA.operation_id, effect_guard=_allow_guard)
    assert coordination.replaced == []


@pytest.mark.parametrize(
    "failure",
    [
        type("Timeout", (Exception,), {"status": 503}),
        LostAcknowledgement,
    ],
)
async def test_guarded_fingerprint_delete_acknowledgement_loss_is_retryable(
    failure: type[Exception],
) -> None:
    image = "registry.example/provisioner@sha256:" + "a" * 64
    adapter = KubernetesVaultFingerprintAdapter(
        core_v1=SimpleNamespace(),
        batch_v1=SimpleNamespace(
            delete_namespaced_job=lambda *args, **kwargs: (_ for _ in ()).throw(failure())
        ),
        image=image,
        poll_attempts=1,
    )
    job = {"metadata": {"uid": "fingerprint-alpha", "resourceVersion": "1"}}

    with pytest.raises(DriverRetryable):
        await adapter._delete(METADATA, job, effect_guard=_allow_guard)


async def test_guarded_fingerprint_poll_timeout_retries_only_an_authenticated_fixed_job() -> None:
    image = "registry.example/provisioner@sha256:" + "a" * 64
    adapter = KubernetesVaultFingerprintAdapter(
        core_v1=SimpleNamespace(),
        batch_v1=SimpleNamespace(),
        image=image,
        sleep=lambda _seconds: None,
        poll_attempts=1,
    )
    from test_provider_adapters import _fingerprint_job

    job = _fingerprint_job(
        adapter,
        METADATA,
        operation_id=METADATA.operation_id,
        phase="before",
        envelope="signed",
    )
    job.status.succeeded = 0
    adapter._batch.read_namespaced_job = lambda *args: job

    with pytest.raises(DriverRetryable):
        await adapter.fingerprint(
            METADATA,
            operation_id=METADATA.operation_id,
            phase="before",
            recovery_envelope="signed",
            effect_guard=_allow_guard,
        )

    job.metadata.annotations = {}
    with pytest.raises(MetadataConflict):
        await adapter.fingerprint(
            METADATA,
            operation_id=METADATA.operation_id,
            phase="before",
            recovery_envelope="signed",
            effect_guard=_allow_guard,
        )


async def test_guarded_fingerprint_waits_for_an_accepted_delete_to_finish() -> None:
    image = "registry.example/provisioner@sha256:" + "a" * 64
    adapter = KubernetesVaultFingerprintAdapter(
        core_v1=SimpleNamespace(),
        batch_v1=SimpleNamespace(),
        image=image,
        sleep=lambda _seconds: None,
        poll_attempts=1,
    )
    from test_provider_adapters import _fingerprint_job

    deleting = _fingerprint_job(
        adapter,
        METADATA,
        operation_id=METADATA.operation_id,
        phase="before",
        envelope="signed",
    )
    deleting.metadata.deletion_timestamp = datetime(2030, 1, 1, tzinfo=UTC)
    deleted: list[dict[str, object]] = []
    adapter._batch.read_namespaced_job = lambda *args: deleting
    adapter._batch.delete_namespaced_job = lambda *args, **kwargs: deleted.append(kwargs)

    with pytest.raises(DriverRetryable):
        await adapter.fingerprint(
            METADATA,
            operation_id=METADATA.operation_id,
            phase="before",
            recovery_envelope="signed",
            effect_guard=_allow_guard,
        )
    assert deleted == []

    with pytest.raises(MetadataConflict):
        await adapter.fingerprint(
            METADATA,
            operation_id=METADATA.operation_id,
            phase="before",
            recovery_envelope="signed",
        )


async def test_guarded_fingerprint_delete_gc_timeout_is_retryable() -> None:
    image = "registry.example/provisioner@sha256:" + "a" * 64
    deleting = {
        "metadata": {
            "uid": "fingerprint-alpha",
            "resourceVersion": "1",
            "deletionTimestamp": "2030-01-01T00:00:00Z",
        }
    }
    adapter = KubernetesVaultFingerprintAdapter(
        core_v1=SimpleNamespace(),
        batch_v1=SimpleNamespace(
            delete_namespaced_job=lambda *args, **kwargs: None,
            read_namespaced_job=lambda *args: deleting,
        ),
        image=image,
        sleep=lambda _seconds: None,
        poll_attempts=1,
    )
    job = {"metadata": {"uid": "fingerprint-alpha", "resourceVersion": "1"}}

    with pytest.raises(DriverRetryable):
        await adapter._delete(METADATA, job, effect_guard=_allow_guard)
    with pytest.raises(MetadataConflict):
        await adapter._delete(METADATA, job)


async def test_fingerprint_and_each_route_delete_propagate_guard_loss_before_write() -> None:
    class Missing(Exception):
        status = 404

    class Batch:
        created: list[dict[str, object]] = []

        def read_namespaced_job(self, name: str, namespace: str):
            raise Missing

        def create_namespaced_job(self, namespace: str, body: dict[str, object]):
            self.created.append(body)

    batch = Batch()
    fingerprint = KubernetesVaultFingerprintAdapter(
        core_v1=SimpleNamespace(),
        batch_v1=batch,
        image="registry.example/provisioner@sha256:" + "a" * 64,
        poll_attempts=1,
    )
    with pytest.raises(ClaimConflict, match="AUTHORITY_LOST"):
        await fingerprint.fingerprint(
            METADATA,
            operation_id=METADATA.operation_id,
            phase="before",
            recovery_envelope="signed",
            effect_guard=_lost_guard,
        )
    assert batch.created == []

    class Custom:
        deleted: list[str] = []

        def get_namespaced_custom_object(self, **kwargs: object):
            return _route()

        def delete_namespaced_custom_object(self, **kwargs: object):
            self.deleted.append(str(kwargs["name"]))

    custom = Custom()
    routes = TraefikRoutingAdapter(
        custom_objects=custom,
        control_hostname="control.example.invalid",
        transfer_hostname="transfer.example.invalid",
        probe=lambda *_args: 404,
    )
    with pytest.raises(ClaimConflict, match="AUTHORITY_LOST"):
        await routes.disable(METADATA, effect_guard=_lost_guard)
    assert custom.deleted == []


def _route(
    *, uid: str = "route-alpha", annotations: dict[str, str] | None = None
) -> dict[str, object]:
    name = METADATA.resource_name + "-control"
    return {
        "metadata": {
            "name": name,
            "namespace": METADATA.resource_name,
            "uid": uid,
            "resourceVersion": "7",
            "annotations": METADATA.kubernetes_annotations if annotations is None else annotations,
        }
    }


async def test_guarded_route_delete_rejects_foreign_route_and_uses_read_preconditions() -> None:
    class Custom:
        deleted: list[dict[str, object]] = []

        def get_namespaced_custom_object(self, **kwargs: object):
            name = str(kwargs["name"])
            if name.endswith("-transfer"):
                raise Missing
            return _route(annotations={})

        def delete_namespaced_custom_object(self, **kwargs: object):
            self.deleted.append(kwargs)

    class Missing(Exception):
        status = 404

    custom = Custom()
    adapter = TraefikRoutingAdapter(
        custom_objects=custom,
        control_hostname="control.example.invalid",
        transfer_hostname="transfer.example.invalid",
        probe=lambda *_args: 404,
    )
    with pytest.raises(MetadataConflict):
        await adapter.disable(METADATA, effect_guard=_allow_guard)
    assert custom.deleted == []


async def test_guarded_route_delete_binds_the_observed_route_and_guard_runs_after_read() -> None:
    class Replacement(Exception):
        status = 409

    class Custom:
        route = _route(uid="route-original")
        deleted: list[dict[str, object]] = []

        def get_namespaced_custom_object(self, **kwargs: object):
            return self.route

        def delete_namespaced_custom_object(self, **kwargs: object):
            self.deleted.append(kwargs)
            raise Replacement

    custom = Custom()
    guard_calls = 0

    async def guard() -> None:
        nonlocal guard_calls
        guard_calls += 1
        custom.route = _route(uid="route-replacement")

    adapter = TraefikRoutingAdapter(
        custom_objects=custom,
        control_hostname="control.example.invalid",
        transfer_hostname="transfer.example.invalid",
        probe=lambda *_args: 404,
    )
    with pytest.raises(Replacement):
        await adapter.disable(METADATA, effect_guard=guard)
    assert guard_calls == 1
    assert custom.deleted[0]["body"] == {
        "propagationPolicy": "Foreground",
        "preconditions": {"uid": "route-original", "resourceVersion": "7"},
    }


async def test_guarded_route_delete_does_nothing_for_absence_or_guard_loss_after_read() -> None:
    class Missing(Exception):
        status = 404

    class Custom:
        reads = 0
        deleted: list[dict[str, object]] = []

        def get_namespaced_custom_object(self, **kwargs: object):
            self.reads += 1
            if self.reads == 1:
                return _route()
            raise Missing

        def delete_namespaced_custom_object(self, **kwargs: object):
            self.deleted.append(kwargs)

    custom = Custom()
    adapter = TraefikRoutingAdapter(
        custom_objects=custom,
        control_hostname="control.example.invalid",
        transfer_hostname="transfer.example.invalid",
        probe=lambda *_args: 404,
    )
    with pytest.raises(ClaimConflict, match="AUTHORITY_LOST"):
        await adapter.disable(METADATA, effect_guard=_lost_guard)
    assert custom.reads == 1
    assert custom.deleted == []


async def test_guarded_route_absence_is_a_no_effect() -> None:
    class Missing(Exception):
        status = 404

    class Custom:
        deleted: list[dict[str, object]] = []

        def get_namespaced_custom_object(self, **kwargs: object):
            raise Missing

        def delete_namespaced_custom_object(self, **kwargs: object):
            self.deleted.append(kwargs)

    custom = Custom()
    adapter = TraefikRoutingAdapter(
        custom_objects=custom,
        control_hostname="control.example.invalid",
        transfer_hostname="transfer.example.invalid",
        probe=lambda *_args: 404,
    )
    await adapter.disable(METADATA, effect_guard=_allow_guard)
    assert custom.deleted == []
