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


def test_shared_capacity_is_absolute_and_charges_pending_and_excess_reservations() -> None:
    from dataclasses import replace
    from decimal import Decimal

    from cellctl.capacity import (
        AttachmentReservation,
        CapacityObservation,
        NodeObservation,
        PodReservation,
        SharedWorkerPolicy,
        compute_shared_capacity,
    )
    from cellctl.manifests import ResourceSettings

    policy = SharedWorkerPolicy(mode="all-shared", profile="qualified-test", topology_key="topology.kubernetes.io/zone",
                                topology_value="test-zone", occupancy=6,
                                resources=ResourceSettings(cpu_request="1", memory_request="2Gi"),
                                reserve_cpu="500m", reserve_memory="1Gi")
    node = NodeObservation(name="worker", attachment_limit=16, cpu=Decimal(4), memory=Decimal(8 * 1024**3),
                           labels={"exomem.io/shared-profile": policy.profile, policy.topology_key: policy.topology_value},
                           ready=True, schedulable=True, pressure=False, taints=(("exomem.io/shared-profile", policy.profile, "NoSchedule"),))
    committed = frozenset({"aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"})
    # Ordinary committed running and unscheduled Pending cells are already in
    # admission's count. Only their demand beyond a footprint is additional.
    pods = (PodReservation(node="worker", cell_id="aaaaaaaaaaaaaaaa", cpu=Decimal(1), memory=Decimal(2 * 1024**3)),
            PodReservation(node=None, cell_id="bbbbbbbbbbbbbbbb", cpu=Decimal(1), memory=Decimal(2 * 1024**3)),
            PodReservation(node="worker", cell_id=None, cpu=Decimal("0.5"), memory=Decimal(1024**3)))
    observation = CapacityObservation(nodes={"worker": node}, pods=pods)
    assert compute_shared_capacity(observation, policy=policy, config=CapacityConfig(), committed=committed)["worker"].cell_slots == 3
    extra = PodReservation(node="worker", cell_id="aaaaaaaaaaaaaaaa", cpu=Decimal(1), memory=Decimal(2 * 1024**3))
    assert compute_shared_capacity(replace(observation, pods=pods + (extra,)), policy=policy, config=CapacityConfig(), committed=committed)["worker"].cell_slots == 2
    orphan = replace(extra, cell_id="cccccccccccccccc")
    assert compute_shared_capacity(replace(observation, pods=pods + (orphan,)), policy=policy, config=CapacityConfig(), committed=committed)["worker"].cell_slots == 2
    for bad in (replace(node, ready=False), replace(node, pressure=True), replace(node, schedulable=False),
                replace(node, labels={}), replace(node, taints=()), replace(node, taints=(("other", "", "NoSchedule"),))):
        assert compute_shared_capacity(replace(observation, nodes={"worker": bad}), policy=policy, config=CapacityConfig(), committed=committed)["worker"].cell_slots == 0
    attached = AttachmentReservation(node="worker", volume="cell-pv", cell_id="aaaaaaaaaaaaaaaa")
    attachment_bound = replace(observation, nodes={"worker": replace(node, attachment_limit=2)},
                               attachments=(attached, attached))
    assert compute_shared_capacity(attachment_bound, policy=policy, config=CapacityConfig(), committed=committed)["worker"].cell_slots == 2
    orphan_volume = AttachmentReservation(node="worker", volume="orphan-pv", cell_id=None)
    assert compute_shared_capacity(replace(attachment_bound, attachments=(attached, orphan_volume)),
                                   policy=policy, config=CapacityConfig(), committed=committed)["worker"].cell_slots == 1
    # Adding a second profile worker must not silently advertise unqualified packing.
    assert all(value.cell_slots == 0 for value in compute_shared_capacity(
        replace(observation, nodes={"worker": node, "second": replace(node, name="second")}),
        policy=policy, config=CapacityConfig(), committed=committed).values())
    assert compute_shared_capacity(observation, policy=policy, config=CapacityConfig(), committed=committed)["worker"].cell_slots == 3
