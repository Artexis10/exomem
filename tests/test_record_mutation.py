from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from record_fixtures import copy_dataset_fixture, copy_vehicle_maintenance_fixture, copy_x3_fixture

from exomem import record_formats
from exomem import structured_collections as collections


def _activity_log(vault: Path) -> None:
    log = vault / "Knowledge Base/log.md"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("# Activity\n", encoding="utf-8")


def _manifest(vault: Path, fixture: Path) -> collections.CollectionManifest:
    return collections.load_manifest(vault, fixture / "_collection.md")


def test_log_append_is_exact_splice_and_replay_is_idempotent(tmp_path: Path) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    source = fixture / "Training Log.md"
    before = source.read_bytes()
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    item = {
        "occurred_on": "2026-08-03",
        "title": "Pull",
        "status": "completed",
        "movements": [{"movement": "Deadlift", "band": "grey", "repetitions": "22"}],
    }
    key = "11111111-1111-4111-8111-111111111111"

    first = records.append_record(
        tmp_path,
        manifest.path,
        item=item,
        item_key=key,
        expected_container_hash=parsed.source_versions[-1].hash,
        why="record a completed session",
    )

    after = source.read_bytes()
    inserted = after.index(b"### 2026-08-03 \xc2\xb7 Pull")
    appended = next(
        record
        for record in record_formats.load_adapter(tmp_path, manifest).read().records
        if record.identity.key == key
    )
    assert after[:inserted] == before[: parsed.insertion_offset]
    assert after[appended.span.end :] == before[parsed.insertion_offset :]
    assert first["outcome"] == "committed"
    assert first["after_item_hash"] == appended.source.hash

    replay = records.append_record(
        tmp_path,
        manifest.path,
        item=item,
        item_key=key,
        expected_container_hash=first["after_container_hash"],
        why="record a completed session",
    )
    assert replay["outcome"] == "replayed"
    assert source.read_bytes() == after
    with pytest.raises(collections.CollectionError, match="RECORD_ID_CONFLICT"):
        records.append_record(
            tmp_path,
            manifest.path,
            item={**item, "title": "Push"},
            item_key=key,
            expected_container_hash=first["after_container_hash"],
            why="record a changed session",
        )


def test_item_update_requires_both_guards_and_preserves_bom_and_body(tmp_path: Path) -> None:
    from exomem import records

    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    record = next(item for item in parsed.records if item.identity.key.startswith("14d2bdca"))
    path = tmp_path / record.source.path
    before = path.read_bytes()

    with pytest.raises(collections.CollectionError, match="STALE_RECORD"):
        records.update_record(
            tmp_path,
            manifest.path,
            item_key=record.identity.key,
            changes={"status": "completed"},
            expected_container_hash=record.source.hash,
            expected_item_version="0" * 64,
            why="correct the maintenance status",
        )

    result = records.update_record(
        tmp_path,
        manifest.path,
        item_key=record.identity.key,
        changes={"status": "completed"},
        expected_container_hash=record.source.hash,
        expected_item_version=record.source.hash,
        why="correct the maintenance status",
    )
    after = path.read_bytes()
    assert after.startswith(b"\xef\xbb\xbf")
    assert after.endswith(b"BOM-bearing item body remains readable.\n")
    assert b"status: completed" in after
    assert result["affected_paths"] == [record.source.path]
    assert before != after


def test_dataset_mutation_refuses_without_writing(tmp_path: Path) -> None:
    from exomem import records

    fixture = copy_dataset_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    source = fixture / "readings.csv"
    before = source.read_bytes()

    with pytest.raises(collections.CollectionError, match="UNSUPPORTED_RECORD_MUTATION"):
        records.append_record(
            tmp_path,
            manifest.path,
            item={
                "reading_id": "r-999",
                "occurred_on": "2026-01-01",
                "category": "water",
                "value": 1,
            },
            item_key="r-999",
            expected_container_hash=hashlib.sha256(before).hexdigest(),
            why="record a reading",
        )
    assert source.read_bytes() == before


def test_item_append_creates_only_one_deterministic_file(tmp_path: Path) -> None:
    from exomem import records

    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    before = record_formats.load_adapter(tmp_path, manifest).read()
    key = "22222222-2222-4222-8222-222222222222"

    result = records.append_record(
        tmp_path,
        manifest.path,
        item={
            "occurred_on": "2026-08-03",
            "asset": "[[Assets/Vehicle]]",
            "provider": "Northside Garage",
            "services": ["oil change"],
            "amount": 95.0,
            "currency": "GBP",
            "status": "completed",
            "next_due_on": None,
        },
        item_key=key,
        expected_container_hash=before.snapshot,
        why="record completed maintenance",
        body="Ordinary readable body.\n",
    )

    path = fixture / "Events" / f"{key}.md"
    assert path.is_file()
    assert result["affected_paths"] == [path.relative_to(tmp_path).as_posix()]
    assert "Ordinary readable body." in path.read_text(encoding="utf-8")
    assert len(record_formats.load_adapter(tmp_path, manifest).read().records) == 4


def test_log_update_replaces_only_target_block_and_direct_edit_is_stale(tmp_path: Path) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    source = fixture / "Training Log.md"
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    target = parsed.records[0]
    before = source.read_bytes()
    source.write_bytes(before.replace(b"2026-08-02", b"2026-08-04", 1))

    with pytest.raises(collections.CollectionError, match="STALE_RECORD"):
        records.update_record(
            tmp_path,
            manifest.path,
            item_key=target.identity.key,
            changes={"title": "Pull"},
            expected_container_hash=parsed.source_versions[-1].hash,
            expected_item_version=target.source.hash,
            why="correct a session title",
        )


def test_log_update_replaces_the_exact_resolved_block(tmp_path: Path) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    source = fixture / "Training Log.md"
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    target = parsed.records[0]
    before = source.read_bytes()

    result = records.update_record(
        tmp_path,
        manifest.path,
        item_key=target.identity.key,
        changes={"title": "Pull"},
        expected_container_hash=parsed.source_versions[-1].hash,
        expected_item_version=target.source.hash,
        why="correct a session title",
    )

    after = source.read_bytes()
    assert result["outcome"] == "committed"
    assert after.startswith(before[: target.span.start])
    assert after.endswith(before[target.span.end :])
    assert b"### 2026-08-02 \xc2\xb7 Pull" in after


def test_aborted_log_append_round_trips_note_and_refuses_heading_delimiters(tmp_path: Path) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    item = {
        "occurred_on": "2026-08-03",
        "title": "Push",
        "note": "Stopped, didn't feel like it, circadian and recovery",
        "status": "aborted",
        "movements": [{"movement": "Press", "band": "grey", "repetitions": ""}],
    }
    records.append_record(
        tmp_path,
        manifest.path,
        item=item,
        item_key="55555555-5555-4555-8555-555555555555",
        expected_container_hash=parsed.source_versions[-1].hash,
        why="record an aborted session",
    )
    appended = next(
        record
        for record in record_formats.load_adapter(tmp_path, manifest).read().records
        if record.identity.key == "55555555-5555-4555-8555-555555555555"
    )
    assert appended.values["status"] == "aborted"
    assert appended.values["note"] == item["note"]

    current = record_formats.load_adapter(tmp_path, manifest).read()
    with pytest.raises(collections.CollectionError, match="UNREPRESENTABLE_RECORD_VALUE"):
        records.append_record(
            tmp_path,
            manifest.path,
            item={**item, "title": "Push · Pull"},
            item_key="66666666-6666-4666-8666-666666666666",
            expected_container_hash=current.source_versions[-1].hash,
            why="attempt an ambiguous heading",
        )


def test_audit_inspection_reports_direct_canonical_edit_gap(tmp_path: Path) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    result = records.append_record(
        tmp_path,
        manifest.path,
        item={
            "occurred_on": "2026-08-03",
            "title": "Pull",
            "status": "completed",
            "movements": [{"movement": "Deadlift", "band": "grey", "repetitions": "22"}],
        },
        item_key="33333333-3333-4333-8333-333333333333",
        expected_container_hash=parsed.source_versions[-1].hash,
        why="record a session",
    )
    assert result["outcome"] == "committed"
    assert records.inspect_audit_gap(tmp_path, manifest.path)["status"] == "ok"
    source = fixture / "Training Log.md"
    source.write_bytes(source.read_bytes() + b"\nmanual edit\n")
    assert records.inspect_audit_gap(tmp_path, manifest.path)["status"] == "gap"


def test_create_collection_is_create_only_and_scaffolds_item_directory(tmp_path: Path) -> None:
    from exomem import records

    _activity_log(tmp_path)
    manifest_path = "Knowledge Base/Records/New/_collection.md"
    manifest = """---
type: collection
exomem_id: 44444444-4444-4444-8444-444444444444
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

    result = records.create_collection(tmp_path, manifest_path, manifest, why="create a collection")

    assert result["outcome"] == "committed"
    assert (tmp_path / manifest_path).is_file()
    assert (tmp_path / "Knowledge Base/Records/New/Events").is_dir()
    with pytest.raises(collections.CollectionError, match="CREATE_ONLY_CONFLICT"):
        records.create_collection(tmp_path, manifest_path, manifest, why="retry creation")
