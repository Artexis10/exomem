"""Match the cell volumes on the agents' disks to the cell rows after an etcd
restore (move-cloud-cells-to-local-storage D4, task 2.9).

An etcd snapshot up to 30 minutes old misses volumes created or relocated
since, and those hold the only copy of recent writes. This reads what the
operator collected and says, for each volume and row, which runbook step
applies (docs/runbooks/cloud-node-loss.md). It changes nothing.

    uv run --project infra/cellctl python -m cellctl.readopt \\
        --lvs-dir <list-cell-volumes.yml output> --rows rows.csv --logical-volumes logicalvolumes.json

TopoLVM names a host logical volume after its volume ID, which the row
records as `volume_id`, so the match is by name.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Report:
    # (cell id, node, volume id): on disk, but the restored cluster has no
    # LogicalVolume for it. Re-adopted by the runbook step.
    readopt: list[tuple[str, str, str]] = field(default_factory=list)
    # On disk and known to the cluster: nothing to do.
    in_place: list[str] = field(default_factory=list)
    # Recorded on a row whose own node was listed and does not hold it: the
    # volume is gone, and the cell is relocated from its backup.
    relocate: list[str] = field(default_factory=list)
    # Recorded on a row whose node wrote no listing (unreachable, or no node
    # recorded): nothing is known yet. List that node again before acting.
    unverified: list[str] = field(default_factory=list)
    # (cell id or None, volume, nodes): one name on more than one disk. No
    # copy is re-adopted or released until an operator has compared them.
    conflict: list[tuple[str | None, str, list[str]]] = field(default_factory=list)
    # (node, volume): on disk, claimed by no row and no LogicalVolume.
    # Released only by an operator on the host, never by a tool.
    unclaimed: list[tuple[str, str]] = field(default_factory=list)


def read_host_volumes(directory: Path, *, pool: str) -> dict[str, set[str]]:
    """{node: thin volume names in `pool`} from one `<node>.json` per agent,
    each an `lvs --reportformat json` listing of the cell volume group."""

    host: dict[str, set[str]] = {}
    for path in sorted(Path(directory).glob("*.json")):
        rows = json.loads(path.read_text(encoding="utf-8"))["report"][0]["lv"]
        # A thin volume or thin snapshot has attribute 'V' and names its pool.
        host[path.stem] = {row["lv_name"] for row in rows if row.get("pool_lv") == pool and row["lv_attr"].startswith("V")}
    return host


def classify(*, host: dict[str, set[str]], rows: dict[str, tuple[str, str | None]], objects: set[str]) -> Report:
    """`rows` maps a cell on local storage to its recorded (volume id, node);
    `objects` holds the volume IDs of the restored cluster's LogicalVolumes."""

    report = Report()
    holders: dict[str, list[str]] = {}
    for node, volumes in sorted(host.items()):
        for volume in volumes:
            holders.setdefault(volume, []).append(node)
    recorded = {volume_id for volume_id, _ in rows.values()}
    for cell_id, (volume_id, node) in sorted(rows.items()):
        nodes = holders.get(volume_id, [])
        if len(nodes) > 1:
            report.conflict.append((cell_id, volume_id, nodes))
        elif nodes and volume_id in objects:
            report.in_place.append(cell_id)
        elif nodes:
            report.readopt.append((cell_id, nodes[0], volume_id))
        elif node in host:
            report.relocate.append(cell_id)
        else:
            report.unverified.append(cell_id)
    for volume, nodes in sorted(holders.items()):
        if volume in recorded or volume in objects:
            continue
        if len(nodes) > 1:
            report.conflict.append((None, volume, nodes))
        else:
            report.unclaimed.append((nodes[0], volume))
    report.unclaimed.sort()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cellctl.readopt", description=__doc__.split("\n\n")[0])
    parser.add_argument("--lvs-dir", type=Path, required=True)
    parser.add_argument("--rows", type=Path, required=True,
                        help="CSV of cell_id,volume_id,node for cells on local storage")
    parser.add_argument("--logical-volumes", type=Path, required=True, help="kubectl get logicalvolumes -o json")
    parser.add_argument("--pool", default="pool0")
    args = parser.parse_args(argv)
    with args.rows.open(encoding="utf-8", newline="") as handle:
        rows = {cell_id: (volume_id, node or None) for cell_id, volume_id, node in csv.reader(handle) if volume_id}
    listed = json.loads(args.logical_volumes.read_text(encoding="utf-8"))
    objects = {(item.get("status") or {}).get("volumeID") for item in listed.get("items") or []} - {None}
    report = classify(host=read_host_volumes(args.lvs_dir, pool=args.pool), rows=rows, objects=objects)
    json.dump(asdict(report), sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
