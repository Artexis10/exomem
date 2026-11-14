"""TEMPORARY Windows probe, deleted before the change ends: a held file's identity and times beside a stat's."""

from __future__ import annotations

import json
import os
import sys

import pytest

from exomem import held_fs

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows probe")


def test_probe(tmp_path):
    (tmp_path / "page.md").write_bytes(b"page")
    stat = os.lstat(tmp_path / "page.md")
    filesystem = held_fs.acquire(tmp_path).require()
    with filesystem.parent(".").require() as parent:
        with filesystem.file(parent, "page.md").require() as held:
            version = held.version()
            identity = held.identity
    out = {
        "held_device": identity.device,
        "stat_dev": stat.st_dev,
        "held_inode": identity.inode,
        "stat_ino": stat.st_ino,
        "held_mtime_ns": version.mtime_ns,
        "stat_mtime_ns": stat.st_mtime_ns,
        "held_change_time_ns": version.change_time_ns,
        "stat_ctime_ns": stat.st_ctime_ns,
        "stat_birthtime_ns": getattr(stat, "st_birthtime_ns", None),
        "python": sys.version,
    }
    pytest.fail("PROBE2 " + json.dumps(out, indent=1, sort_keys=True))
