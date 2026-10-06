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
    # Recorded on a row, on no listed disk: relocated from the cell's backup.
    relocate: list[str] = field(default_factory=list)
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


def classify(*, host: dict[str, set[str]], rows: dict[str, str], objects: set[str]) -> Report:
    """`rows` maps a cell on local storage to its recorded volume id;
    `objects` holds the volume IDs of the restored cluster's LogicalVolumes."""

    report = Report()
    where = {volume: node for node, volumes in host.items() for volume in volumes}
    for cell_id, volume_id in sorted(rows.items()):
        if volume_id not in where:
            report.relocate.append(cell_id)
        elif volume_id in objects:
            report.in_place.append(cell_id)
        else:
            report.readopt.append((cell_id, where[volume_id], volume_id))
    recorded = set(rows.values())
    report.unclaimed = sorted(
        (node, volume) for volume, node in where.items() if volume not in recorded and volume not in objects
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cellctl.readopt", description=__doc__.split("\n\n")[0])
    parser.add_argument("--lvs-dir", type=Path, required=True)
    parser.add_argument("--rows", type=Path, required=True, help="CSV of cell_id,volume_id for cells on local storage")
    parser.add_argument("--logical-volumes", type=Path, required=True, help="kubectl get logicalvolumes -o json")
    parser.add_argument("--pool", default="pool0")
    args = parser.parse_args(argv)
    with args.rows.open(encoding="utf-8", newline="") as handle:
        rows = {cell_id: volume_id for cell_id, volume_id in csv.reader(handle) if volume_id}
    listed = json.loads(args.logical_volumes.read_text(encoding="utf-8"))
    objects = {(item.get("status") or {}).get("volumeID") for item in listed.get("items") or []} - {None}
    report = classify(host=read_host_volumes(args.lvs_dir, pool=args.pool), rows=rows, objects=objects)
    json.dump(asdict(report), sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
