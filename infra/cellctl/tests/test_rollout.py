"""D6 image selection from the desired row and platform release."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from cellctl.rollout import initial_image, select_upgrade_candidate
from cellctl.state import CellRow, ClusterObservation, RolloutRow

CELL_ID = "aaaaaaaaaaaaaaaa"
PLATFORM_IMAGE = "registry.example/cell@sha256:" + "a" * 64
DESIRED_IMAGE = "registry.example/cell@sha256:" + "b" * 64


def test_row_desired_image_overrides_platform_image_for_new_and_existing_cells() -> None:
    row = CellRow(
        cell_id=CELL_ID,
        tenant_id=UUID(int=1),
        storage_gib=10,
        rollout_priority=1,
        desired_state="running",
        desired_image=DESIRED_IMAGE,
        generation=1,
        observed_image=PLATFORM_IMAGE,
        ready=True,
    )
    rollout = RolloutRow()

    assert initial_image(row, rollout, PLATFORM_IMAGE) == (DESIRED_IMAGE, None)
    assert select_upgrade_candidate(
        [row],
        {CELL_ID: ClusterObservation(statefulset_image=PLATFORM_IMAGE, pod_ready=True)},
        rollout,
        PLATFORM_IMAGE,
        now=datetime(2026, 1, 1, tzinfo=UTC),
        any_cell_already_upgrading=False,
    ) == CELL_ID
