"""D9: capacity is observed, not reserved.

`cell_slots = attachments_limit - headroom - non_cell_attachments`, where
`attachments_limit` comes from Kubernetes (a node's `CSINode` allocatable
count for the configured CSI driver, or the configured fallback), never a
Hetzner server id or a hardcoded chart constant. No reservation ledger
exists; a deleted cell's volume simply stops counting the next time this is
computed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CapacityConfig:
    csi_driver: str = "csi.hetzner.cloud"
    headroom: int = 0
    # Used only when a node's CSINode has no allocatable count for the
    # driver -- how the local rehearsal runs without Hetzner CSI at all.
    attachments_limit_fallback: int | None = None


@dataclass(frozen=True)
class NodeCapacity:
    cell_slots: int
    attachments_used: int
    limit_known: bool


def compute_node_capacity(
    *,
    allocatable: int | None,
    attachments_used: int,
    non_cell_attachments: int,
    config: CapacityConfig,
) -> NodeCapacity:
    limit = allocatable if allocatable is not None else config.attachments_limit_fallback
    if limit is None:
        # "Nothing is admitted to a node whose limit is unknown."
        return NodeCapacity(cell_slots=0, attachments_used=attachments_used, limit_known=False)
    cell_slots = limit - config.headroom - non_cell_attachments
    return NodeCapacity(cell_slots=cell_slots, attachments_used=attachments_used, limit_known=True)


def admits_new_cell(*, non_deleted_cell_count: int, cell_slots_by_node: dict[str, int]) -> bool:
    """Substrate's own admission check: non-deleted cell rows below the sum
    of published cell_slots across every node."""

    return non_deleted_cell_count < sum(cell_slots_by_node.values())
