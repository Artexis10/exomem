"""The deletion proof for cells on TopoLVM local volumes
(move-cloud-cells-to-local-storage D4, task 2.5).

The Hetzner proof asks the provider whether a volume id still exists. A local
volume has no provider: what can still hold a deleted cell's data is the
cluster's own objects, so the proof reads them. cellctl never deletes any of
them itself: deleting a LogicalVolume object on a node whose TopoLVM plugin
runs destroys the volume, and that follows only from the claims and
snapshots the cell's namespace took with it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LogicalVolume:
    """A topolvm.io/v1 LogicalVolume: a cell volume, a snapshot or a clone."""

    name: str
    volume_id: str | None
    node: str
    # The LogicalVolume this one was snapshotted or cloned from, by name.
    source: str | None = None


@dataclass(frozen=True)
class LocalVolumeState:
    # (claim namespace, node its PV is pinned to, or None for a volume that
    # is not node-local) for every PV claimed from a namespace.
    claimed_pvs: tuple[tuple[str, str | None], ...]
    # (snapshot namespace, node of its snapshot volume or None) for every
    # VolumeSnapshotContent.
    snapshot_contents: tuple[tuple[str, str | None], ...]
    volumes: tuple[LogicalVolume, ...]
    # Present nodes whose stop is not confirmed. A volume elsewhere sits on a
    # node confirmed destroyed: tainted out of service while not Ready, or
    # removed, which the removal playbook does only after confirming the stop.
    live_nodes: frozenset[str]


def cell_data_absent(state: LocalVolumeState, *, namespace: str, volume_id: str | None) -> bool:
    """True only when no PV, snapshot or logical volume of the cell remains on
    a live node: its volume, the snapshots of it and the clones of those."""

    def remains(node: str | None) -> bool:
        return node is None or node in state.live_nodes

    if any(claimed == namespace and remains(node) for claimed, node in state.claimed_pvs):
        return False
    if any(source == namespace and remains(node) for source, node in state.snapshot_contents):
        return False
    lineage = {volume.name for volume in state.volumes if volume_id is not None and volume.volume_id == volume_id}
    grown = True
    while grown:
        derived = {volume.name for volume in state.volumes if volume.source in lineage} - lineage
        lineage |= derived
        grown = bool(derived)
    return not any(volume.name in lineage and remains(volume.node) for volume in state.volumes)
