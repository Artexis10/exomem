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
    # (claim namespace, node its PV is pinned to or None for a volume that is
    # not node-local, its volume handle) for every PV claimed from a namespace.
    claimed_pvs: tuple[tuple[str, str | None, str | None], ...]
    # (snapshot namespace, node of its snapshot volume or None) for every
    # VolumeSnapshotContent.
    snapshot_contents: tuple[tuple[str, str | None], ...]
    volumes: tuple[LogicalVolume, ...]
    # Every node in the API, a stopped one included: a node stopped for
    # repair can rejoin with its disk. A volume on a node gone from the API is
    # destroyed: the removal playbook deletes a Node only after confirming its
    # stop, and the runbook erases or destroys its disk.
    present_nodes: frozenset[str]


def cell_data_absent(state: LocalVolumeState, *, namespace: str, volume_id: str | None) -> bool:
    """True only when no PV, snapshot or logical volume of the cell remains on
    a present node: its volumes, the snapshots of them and the clones of those.

    The volumes are the recorded one and every one a PV claimed from the
    namespace still names: after a relocation, the row records only the new
    volume, while the retained PV names the old one."""

    def remains(node: str | None) -> bool:
        return node is None or node in state.present_nodes

    if any(claimed == namespace and remains(node) for claimed, node, _ in state.claimed_pvs):
        return False
    if any(source == namespace and remains(node) for source, node in state.snapshot_contents):
        return False
    roots = {volume_id} | {handle for claimed, _, handle in state.claimed_pvs if claimed == namespace}
    lineage = {volume.name for volume in state.volumes if volume.volume_id is not None and volume.volume_id in roots}
    grown = True
    while grown:
        derived = {volume.name for volume in state.volumes if volume.source in lineage} - lineage
        lineage |= derived
        grown = bool(derived)
    return not any(volume.name in lineage and remains(volume.node) for volume in state.volumes)
