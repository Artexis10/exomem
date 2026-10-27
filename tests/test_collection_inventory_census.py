"""The inventory's sweeps list directories from the artifact census, and a change must reach them.

The census keeps each directory's listing between calls and reads it again only when the
directory's stamp moves. A listing served past a change hides a collection from the next
inventory, and it would hide it without an error.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from test_collection_inventory_errors import (
    _MISLOCATED_PLANNING_MANIFEST,
    _UNPARSEABLE_MANIFEST,
    _UNPARSEABLE_PATH,
    _seed_records_collection,
    _write_manifest,
)

from exomem.governance import artifact_census
from exomem.record_memory import record_memory

_TRACKER = "Knowledge Base/Records/Legacy/Log.md"
_TRACKER_BYTES = "---\ntype: tracker\n---\n\n| Date | Entry |\n|---|---|\n"


@pytest.fixture
def trusting_census(monkeypatch):
    """A census that trusts a listing as soon as its directory is older than the walk."""
    monkeypatch.setattr(artifact_census, "_CENSUS_RACY_NS", 0)


def _age(directory: Path) -> None:
    """Give the directory a stamp from well before the walk, as a long-idle folder has."""
    past = time.time_ns() - 60 * 10**9
    os.utime(directory, ns=(past, past))


def test_a_manifest_added_to_a_directory_the_last_inventory_walked_is_found(tmp_path, trusting_census):
    """Defect: a listing served from its old stamp hides a collection created after the first inventory."""
    _seed_records_collection(tmp_path)
    folder = tmp_path / Path(_UNPARSEABLE_PATH).parent
    folder.mkdir(parents=True)
    _age(folder)
    assert record_memory(tmp_path, "inspect")["unreadable_manifests"] == []

    _write_manifest(tmp_path, _UNPARSEABLE_PATH, _UNPARSEABLE_MANIFEST)

    assert [row["path"] for row in record_memory(tmp_path, "inspect")["unreadable_manifests"]] == [_UNPARSEABLE_PATH]


def test_a_legacy_tracker_added_to_a_directory_the_last_inventory_walked_is_found(tmp_path, trusting_census):
    """Defect: the Records sweep reads the same stale listings, so a new tracker file stays unlisted."""
    _seed_records_collection(tmp_path)
    folder = tmp_path / Path(_TRACKER).parent
    folder.mkdir(parents=True)
    _age(folder)
    assert record_memory(tmp_path, "inspect")["legacy_trackers"] == []

    _write_manifest(tmp_path, _TRACKER, _TRACKER_BYTES)

    assert [row["path"] for row in record_memory(tmp_path, "inspect")["legacy_trackers"]] == [_TRACKER]


def test_a_manifest_written_in_the_same_timestamp_tick_as_the_walk_is_found(tmp_path, monkeypatch):
    """Defect: a directory changed within one timestamp tick of a walk keeps its stamp and its stale listing."""
    real = artifact_census._directory_stamp

    def whole_seconds(directory: str):
        device, inode, modified, changed = real(directory)
        return device, inode, modified // 10**9 * 10**9, changed // 10**9 * 10**9

    monkeypatch.setattr(artifact_census, "_directory_stamp", whole_seconds)
    _seed_records_collection(tmp_path)
    (tmp_path / Path(_UNPARSEABLE_PATH).parent).mkdir(parents=True)
    assert record_memory(tmp_path, "inspect")["unreadable_manifests"] == []

    _write_manifest(tmp_path, _UNPARSEABLE_PATH, _UNPARSEABLE_MANIFEST)

    assert [row["path"] for row in record_memory(tmp_path, "inspect")["unreadable_manifests"]] == [_UNPARSEABLE_PATH]


def test_a_manifest_outside_the_knowledge_base_is_neither_listed_nor_reported(tmp_path):
    """Defect: the census names files across the whole vault, so a note or attachment folder holding a file
    called `_collection.md` would appear in the inventory as a collection or an unreadable row."""
    released = _seed_records_collection(tmp_path)
    _write_manifest(tmp_path, "Attachments/Scans/_collection.md", _UNPARSEABLE_MANIFEST)
    _write_manifest(tmp_path, "Other/_collection.md", _MISLOCATED_PLANNING_MANIFEST)

    inventory = record_memory(tmp_path, "inspect")

    assert [row["manifest_path"] for row in inventory["collections"]] == [released]
    assert inventory["unreadable_manifests"] == []
