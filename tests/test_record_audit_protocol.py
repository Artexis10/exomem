from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from record_fixtures import copy_x3_fixture

from exomem import record_formats, vault
from exomem import structured_collections as collections


def _activity_log(vault: Path) -> None:
    path = vault / "Knowledge Base/log.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Activity\n", encoding="utf-8")


def _item(day: str, title: str) -> dict[str, object]:
    return {
        "occurred_on": day,
        "title": title,
        "status": "completed",
        "movements": [{"movement": "Deadlift", "band": "grey", "repetitions": "22"}],
    }


def test_audit_manifest_head_and_transition_chain_keep_manual_gap_visible(tmp_path: Path) -> None:
    """A later transition must not bless an earlier direct canonical edit."""
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    initial = record_formats.load_adapter(tmp_path, manifest).read()
    first = records.append_record(
        tmp_path,
        manifest.path,
        item=_item("2026-08-03", "Pull"),
        item_key="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        expected_container_hash=initial.source_versions[-1].hash,
        why="record a session",
    )

    manifest_bytes = (fixture / "_collection.md").read_bytes()
    assert b"record_audit:" in manifest_bytes
    assert first["audit_correlation"] is not None
    assert records.inspect_audit_gap(tmp_path, manifest.path)["status"] == "ok"

    source = fixture / "Training Log.md"
    source.write_bytes(source.read_bytes() + b"\nmanual edit\n")
    changed = collections.load_manifest(tmp_path, fixture / "_collection.md")
    current = record_formats.load_adapter(tmp_path, changed).read()
    records.append_record(
        tmp_path,
        changed.path,
        item=_item("2026-08-04", "Push"),
        item_key="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        expected_container_hash=current.source_versions[-1].hash,
        why="record a later session",
    )

    report = records.inspect_audit_gap(tmp_path, changed.path)
    assert report["status"] == "gap"
    assert report["gaps"]


def test_append_without_an_optional_container_guard_serializes_same_vault_writers(
    tmp_path: Path,
) -> None:
    """Append may use the fresh guarded source when the caller has no prior snapshot."""
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    start = threading.Barrier(2)

    def append(number: int) -> dict[str, object]:
        start.wait(timeout=2)
        return records.append_record(
            tmp_path,
            manifest.path,
            item=_item(f"2026-08-0{number + 3}", "Pull"),
            item_key=f"{number + 1:08d}-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            expected_container_hash=None,
            why="record concurrent session",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(append, range(2)))

    assert {result["outcome"] for result in results} == {"committed"}
    assert records.inspect_audit_gap(tmp_path, manifest.path)["status"] == "ok"


def test_audit_history_rejects_conflicting_duplicate_and_unsafe_archive(tmp_path: Path) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    initial = record_formats.load_adapter(tmp_path, manifest).read()
    records.append_record(
        tmp_path,
        manifest.path,
        item=_item("2026-08-03", "Pull"),
        item_key="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        expected_container_hash=initial.source_versions[-1].hash,
        why="record a session",
    )
    log = tmp_path / "Knowledge Base/log.md"
    event_line = next(
        line for line in log.read_text(encoding="utf-8").splitlines() if "audit-v1" in line
    )
    event = json.loads(event_line.removeprefix("Records audit-v1 "))
    event["after_container_hash"] = "0" * 64
    log.write_text(
        log.read_text(encoding="utf-8") + "\nRecords audit-v1 " + json.dumps(event) + "\n",
        encoding="utf-8",
    )
    assert records.inspect_audit_gap(tmp_path, manifest.path)["status"] == "gap"

    archive = tmp_path / "Knowledge Base/_archive/logs"
    archive.mkdir(parents=True)
    try:
        (archive / "log-unsafe.md").symlink_to(log)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    assert records.inspect_audit_gap(tmp_path, manifest.path)["status"] == "history_incomplete"


def test_audit_chain_is_archive_order_independent_and_exact_duplicates_dedupe(
    tmp_path: Path,
) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    initial = record_formats.load_adapter(tmp_path, manifest).read()
    first = records.append_record(
        tmp_path,
        manifest.path,
        item=_item("2026-08-03", "Pull"),
        item_key="12121212-1212-4212-8212-121212121212",
        expected_container_hash=initial.source_versions[-1].hash,
        why="record first session",
    )
    refreshed = collections.load_manifest(tmp_path, fixture / "_collection.md")
    second = records.append_record(
        tmp_path,
        refreshed.path,
        item=_item("2026-08-04", "Push"),
        item_key="34343434-3434-4434-8434-343434343434",
        expected_container_hash=first["after_container_hash"],
        why="record second session",
    )
    assert second["outcome"] == "committed"
    log = tmp_path / "Knowledge Base/log.md"
    events = [line for line in log.read_text(encoding="utf-8").splitlines() if "audit-v1" in line]
    archive = tmp_path / "Knowledge Base/_archive/logs"
    archive.mkdir(parents=True)
    (archive / "log-z.md").write_text("\n".join(reversed(events)) + "\n", encoding="utf-8")
    (archive / "log-a.md").write_text(events[0] + "\n", encoding="utf-8")
    log.write_text("# Activity\n", encoding="utf-8")
    assert records.inspect_audit_gap(tmp_path, refreshed.path)["status"] == "ok"


@pytest.mark.parametrize("unsafe", ["fifo", "oversize", "over-cap"])
def test_history_caps_and_unsafe_archive_are_bounded_incomplete(
    tmp_path: Path, unsafe: str
) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    records.append_record(
        tmp_path,
        manifest.path,
        item=_item("2026-08-03", "Pull"),
        item_key="56565656-5656-4565-8565-565656565656",
        expected_container_hash=snapshot.source_versions[-1].hash,
        why="record bounded history fixture",
    )
    archive = tmp_path / "Knowledge Base/_archive/logs"
    archive.mkdir(parents=True)
    if unsafe == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFOs are unsupported")
        os.mkfifo(archive / "log-fifo.md")
    elif unsafe == "oversize":
        (archive / "log-large.md").write_bytes(b"x" * 2_000_001)
    else:
        for index in range(129):
            (archive / f"log-{index:03d}.md").write_text("# inert\n", encoding="utf-8")
    report = records.inspect_audit_gap(tmp_path, manifest.path)
    assert report["status"] == "history_incomplete"
    assert len(report["gaps"]) <= 32


def test_history_descriptor_drift_is_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    records.append_record(
        tmp_path,
        manifest.path,
        item=_item("2026-08-03", "Pull"),
        item_key="78787878-7878-4787-8787-787878787878",
        expected_container_hash=snapshot.source_versions[-1].hash,
        why="record drift fixture",
    )
    archive = tmp_path / "Knowledge Base/_archive/logs"
    archive.mkdir(parents=True)
    drifting = archive / "log-drift.md"
    drifting.write_text("# inert\n", encoding="utf-8")
    real_read = records.os.read
    reads = 0

    def drift(descriptor: int, size: int) -> bytes:
        nonlocal reads
        reads += 1
        value = real_read(descriptor, size)
        if reads == 2:
            drifting.write_text("# inert\n", encoding="utf-8")
            info = drifting.stat()
            os.utime(drifting, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
        return value

    monkeypatch.setattr(records.os, "read", drift)
    assert records.inspect_audit_gap(tmp_path, manifest.path)["status"] == "history_incomplete"


def test_caught_and_abrupt_publication_prefixes_leave_rollback_or_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    source = fixture / "Training Log.md"
    before = (
        source.read_bytes(),
        (fixture / "_collection.md").read_bytes(),
        (tmp_path / "Knowledge Base/log.md").read_bytes(),
    )
    initial = record_formats.load_adapter(tmp_path, manifest).read()
    real_replace = vault._BatchWorkspace.replace_artifact
    calls = 0

    def fail_first(workspace, artifact, target):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError(13, "Access is denied", str(target))
        return real_replace(workspace, artifact, target)

    monkeypatch.setattr(vault._BatchWorkspace, "replace_artifact", fail_first)
    with pytest.raises(collections.CollectionError, match="RECORD_PUBLICATION_FAILED"):
        records.append_record(
            tmp_path,
            manifest.path,
            item=_item("2026-08-03", "Pull"),
            item_key="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            expected_container_hash=initial.source_versions[-1].hash,
            why="exercise portable replacement failure",
        )
    assert (
        source.read_bytes(),
        (fixture / "_collection.md").read_bytes(),
        (tmp_path / "Knowledge Base/log.md").read_bytes(),
    ) == before

    monkeypatch.setattr(vault._BatchWorkspace, "replace_artifact", real_replace)
    calls = 0

    def interrupt_after_canonical(workspace, artifact, target):
        nonlocal calls
        result = real_replace(workspace, artifact, target)
        calls += 1
        if calls == 1:
            raise KeyboardInterrupt("simulated abrupt publication")
        return result

    monkeypatch.setattr(vault._BatchWorkspace, "replace_artifact", interrupt_after_canonical)
    with pytest.raises(KeyboardInterrupt, match="simulated abrupt publication"):
        records.append_record(
            tmp_path,
            manifest.path,
            item=_item("2026-08-03", "Pull"),
            item_key="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            expected_container_hash=initial.source_versions[-1].hash,
            why="exercise abrupt publication",
        )
    assert records.inspect_audit_gap(tmp_path, manifest.path)["status"] == "gap"


def test_create_rolls_back_caught_failure_and_exposes_abrupt_manifest_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import records

    _activity_log(tmp_path)
    manifest_path = "Knowledge Base/Records/New/_collection.md"
    text = """---
type: collection
exomem_id: ffffffff-ffff-4fff-8fff-ffffffffffff
title: New records
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Events
  format_version: 1
item_schema:
  natural_key: [occurred_on]
  fields:
    occurred_on:
      type: date
      required: true
---
"""
    real_replace = vault._BatchWorkspace.replace_artifact
    calls = 0

    def fail_scaffold(workspace, artifact, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError(13, "Access is denied", str(target))
        return real_replace(workspace, artifact, target)

    monkeypatch.setattr(vault._BatchWorkspace, "replace_artifact", fail_scaffold)
    with pytest.raises(collections.CollectionError, match="RECORD_PUBLICATION_FAILED"):
        records.create_collection(tmp_path, manifest_path, text, why="exercise create rollback")
    assert not (tmp_path / manifest_path).exists()
    assert not (tmp_path / "Knowledge Base/Records/New/Events").exists()

    monkeypatch.setattr(vault._BatchWorkspace, "replace_artifact", real_replace)
    calls = 0

    def interrupt_after_manifest(workspace, artifact, target):
        nonlocal calls
        result = real_replace(workspace, artifact, target)
        calls += 1
        if calls == 1:
            raise KeyboardInterrupt("simulated abrupt create")
        return result

    monkeypatch.setattr(vault._BatchWorkspace, "replace_artifact", interrupt_after_manifest)
    with pytest.raises(KeyboardInterrupt, match="simulated abrupt create"):
        records.create_collection(tmp_path, manifest_path, text, why="exercise abrupt create")
    assert (tmp_path / manifest_path).is_file()
    assert records.inspect_audit_gap(tmp_path, manifest_path)["status"] == "gap"


def test_create_refuses_nfc_casefold_component_alias(tmp_path: Path) -> None:
    from exomem import records

    _activity_log(tmp_path)
    (tmp_path / "Knowledge Base/Records/new").mkdir(parents=True)
    text = """---
type: collection
exomem_id: 99999999-9999-4999-8999-999999999999
title: New records
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Events
  format_version: 1
item_schema:
  natural_key: [occurred_on]
  fields:
    occurred_on:
      type: date
      required: true
---
"""
    with pytest.raises(collections.CollectionError, match="CREATE_ONLY_CONFLICT"):
        records.create_collection(
            tmp_path, "Knowledge Base/Records/New/_collection.md", text, why="reject alias"
        )
