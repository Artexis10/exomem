"""A paid request completes only after its actual bound filesystem grows."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from cellctl.capacity import GIB
from cellctl.decide import storage_receipt
from cellctl.state import CellRow, ClusterObservation
from cellctl.storage_config import DEFAULT_STORAGE, LEGACY_CLASS, LocalStorage, StorageConfig

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_ready_pod_and_requested_size_do_not_claim_delayed_expansion() -> None:
    row = CellRow("aaaaaaaaaaaaaaaa", UUID(int=1), 20, 1, "running", None, 2)
    observed = ClusterObservation(
        namespace_exists=True, namespace_cell_label=row.cell_id, pvc_bound=True,
        pvc_uid="claim-a", pv_claim_ref_uid="claim-a", pvc_resource_version="17",
        pv_storage_class=LEGACY_CLASS, pvc_capacity_bytes=10 * GIB, pod_ready=True,
    )
    receipt = storage_receipt(row, observed, DEFAULT_STORAGE, now=NOW)
    assert receipt["observed_storage_capacity_bytes"] == 10 * GIB
    assert receipt["observed_storage_generation"] is None
    expanded = storage_receipt(row, replace(observed, pvc_capacity_bytes=20 * GIB), DEFAULT_STORAGE, now=NOW)
    assert expanded["observed_storage_generation"] == 2
    assert expanded["observed_storage_pvc_uid"] == "claim-a"
    assert expanded["observed_storage_pvc_version"] == "17"


def test_pending_filesystem_resize_does_not_finish_a_paid_request() -> None:
    row = CellRow("aaaaaaaaaaaaaaaa", UUID(int=1), 20, 1, "running", None, 2)
    observed = ClusterObservation(
        namespace_exists=True, namespace_cell_label=row.cell_id, pvc_bound=True,
        pvc_uid="claim-a", pv_claim_ref_uid="claim-a", pvc_resource_version="18",
        pv_storage_class=LEGACY_CLASS, pvc_capacity_bytes=20 * GIB, pvc_resize_pending=True,
    )
    assert storage_receipt(row, observed, DEFAULT_STORAGE, now=NOW)["observed_storage_generation"] is None


def test_a_conflicting_claim_clears_the_previous_capacity_receipt() -> None:
    row = CellRow(
        "aaaaaaaaaaaaaaaa", UUID(int=1), 20, 1, "running", None, 2,
        observed_storage_pvc_uid="claim-a", observed_storage_pvc_version="17",
        observed_storage_capacity_bytes=20 * GIB, observed_storage_generation=2,
    )
    observed = ClusterObservation(
        namespace_exists=True, namespace_cell_label=row.cell_id, pvc_bound=True,
        pvc_uid="claim-b", pv_claim_ref_uid="claim-a", pvc_resource_version="19",
        pv_storage_class=LEGACY_CLASS, pvc_capacity_bytes=20 * GIB,
    )
    receipt = storage_receipt(row, observed, DEFAULT_STORAGE, now=NOW)
    assert receipt["observed_storage_pvc_uid"] is None
    assert receipt["observed_storage_capacity_bytes"] is None
    assert receipt["observed_storage_generation"] is None


def test_paid_observation_keeps_the_existing_local_growth_floor() -> None:
    row = CellRow("aaaaaaaaaaaaaaaa", UUID(int=1), 12, 1, "running", None, 2, grown_storage_gib=16)
    local = LocalStorage()
    storage = StorageConfig(domain=local.class_name, local=local)
    observed = ClusterObservation(
        namespace_exists=True, namespace_cell_label=row.cell_id, pvc_bound=True,
        pvc_uid="claim-a", pv_claim_ref_uid="claim-a", pvc_resource_version="20",
        pv_storage_class=local.class_name, pvc_capacity_bytes=12 * GIB,
    )
    assert storage_receipt(row, observed, storage, now=NOW)["observed_storage_generation"] is None
    assert row.size_gib == 16


def test_unchanged_capacity_does_not_rewrite_the_observation_time() -> None:
    row = CellRow(
        "aaaaaaaaaaaaaaaa", UUID(int=1), 20, 1, "running", None, 2,
        observed_storage_pvc_uid="claim-a", observed_storage_pvc_version="21",
        observed_storage_capacity_bytes=20 * GIB, observed_storage_generation=2,
        observed_storage_at=NOW,
    )
    observed = ClusterObservation(
        namespace_exists=True, namespace_cell_label=row.cell_id, pvc_bound=True,
        pvc_uid="claim-a", pv_claim_ref_uid="claim-a", pvc_resource_version="21",
        pv_storage_class=LEGACY_CLASS, pvc_capacity_bytes=20 * GIB,
    )
    assert storage_receipt(row, observed, DEFAULT_STORAGE, now=NOW) == {}
