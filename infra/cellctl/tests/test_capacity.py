"""Tests for D9 capacity publication (task 3.8).

D9 amendment (M8): capacity comes from Kubernetes -- a node's CSINode
allocatable count for the configured CSI driver, and its attached
VolumeAttachments -- never a Hetzner server id string-matched against the
chart's `nodeName`, and never a hardcoded chart constant for the limit.
"""

from __future__ import annotations

from cellctl.capacity import CapacityConfig, admits_new_cell, compute_node_capacity


def test_cell_slots_subtracts_headroom_and_non_cell_attachments() -> None:
    capacity = compute_node_capacity(
        allocatable=10,
        attachments_used=3,
        non_cell_attachments=1,
        config=CapacityConfig(headroom=2),
    )
    assert capacity.attachments_used == 3
    assert capacity.cell_slots == 10 - 2 - 1
    assert capacity.limit_known is True


def test_no_allocatable_and_no_fallback_publishes_zero_slots_but_known_false() -> None:
    # M8: "nothing is admitted to a node whose limit is unknown" -- this is
    # the local-rehearsal-without-Hetzner-CSI case with no fallback set.
    capacity = compute_node_capacity(
        allocatable=None,
        attachments_used=0,
        non_cell_attachments=0,
        config=CapacityConfig(attachments_limit_fallback=None),
    )
    assert capacity.cell_slots == 0
    assert capacity.limit_known is False


def test_fallback_limit_is_used_when_the_node_has_no_allocatable_count() -> None:
    # The local rehearsal runs without Hetzner CSI at all.
    capacity = compute_node_capacity(
        allocatable=None,
        attachments_used=1,
        non_cell_attachments=0,
        config=CapacityConfig(attachments_limit_fallback=8, headroom=1),
    )
    assert capacity.cell_slots == 8 - 1 - 0
    assert capacity.limit_known is True


def test_node_is_full_denies_admission() -> None:
    assert admits_new_cell(non_deleted_cell_count=5, cell_slots_by_node={"node-1": 5}) is False
    assert admits_new_cell(non_deleted_cell_count=4, cell_slots_by_node={"node-1": 5}) is True


def test_admission_sums_slots_across_every_node() -> None:
    assert (
        admits_new_cell(non_deleted_cell_count=9, cell_slots_by_node={"node-1": 5, "node-2": 5})
        is True
    )
