"""The dedicated_host role's stored-data guard (move-cloud-cells-to-local-storage D2).

The CI loop-device run proves the blank and refused-filesystem paths on real
block devices; these cases cover the branches it does not reach.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_PATH = (
    Path(__file__).resolve().parents[1]
    / "infra/ansible/roles/dedicated_host/filter_plugins/dedicated_host.py"
)
_spec = importlib.util.spec_from_file_location("dedicated_host_filters", _PATH)
assert _spec is not None and _spec.loader is not None
filters = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(filters)

DISK = "/dev/disk/by-id/nvme-a-part4"


def _lsblk(children: list[dict] | None = None, mountpoints: list | None = None) -> str:
    node = {"name": "/dev/nvme0n1p4", "type": "part", "mountpoints": mountpoints or [None]}
    if children:
        node["children"] = children
    return json.dumps({"blockdevices": [node]})


_PARTITION_ENTRY = "DEVNAME=/dev/nvme0n1p4\nPART_ENTRY_SCHEME=gpt\nPART_ENTRY_NUMBER=4\n"
_MD_CHILD = [{"name": "/dev/md127", "type": "raid1", "mountpoints": ["/"]}]


@pytest.mark.parametrize(
    "blkid,lsblk,wipe,verdict",
    [
        # An unformatted partition carries only its table entry: buildable.
        (_PARTITION_ENTRY, _lsblk(), [], "blank"),
        # A rerun finds its own array member and leaves it alone.
        ("TYPE=linux_raid_member\nLABEL=dx1:exomem-cells\n", _lsblk(), [], "ours"),
        # A filesystem the inventory did not name for wiping is never touched.
        ("TYPE=ext4\nUUID=1\n", _lsblk(), [], "refuse"),
        # Named for wiping and idle: wiped, then used.
        ("TYPE=ext4\nUUID=1\n", _lsblk(), [DISK], "wipe"),
        # Another array that is assembled and mounted stays refused even when named.
        ("TYPE=linux_raid_member\nLABEL=rescue:2\n", _lsblk(_MD_CHILD), [DISK], "refuse"),
    ],
)
def test_disk_guard_touches_only_blank_owned_or_named_idle_disks(
    blkid: str, lsblk: str, wipe: list[str], verdict: str
) -> None:
    probe = {"device": DISK, "blkid": blkid, "lsblk": lsblk}
    [result] = filters.dedicated_disk_verdicts([probe], wipe, "exomem-cells")
    assert result["verdict"] == verdict
