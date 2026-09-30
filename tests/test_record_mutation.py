from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from record_fixtures import (
    copy_dataset_fixture,
    copy_vehicle_maintenance_fixture,
    copy_x3_fixture,
    ledger_item,
    setup_ledger_collection,
)
from record_presentation_fixtures import manifest_text as presentation_manifest_text
from record_presentation_fixtures import setup_collection as setup_presentation_collection
from record_presentation_fixtures import values as presentation_values

from exomem import record_formats
from exomem import structured_collections as collections


def _activity_log(vault: Path) -> None:
    log = vault / "Knowledge Base/log.md"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("# Activity\n", encoding="utf-8")


def _manifest(vault: Path, fixture: Path) -> collections.CollectionManifest:
    return collections.load_manifest(vault, fixture / "_collection.md")


def _human_record_manifest() -> str:
    source = presentation_manifest_text(presentation=False).replace(
        "natural_key: [observed_on]", "natural_key: [observed_on, subject]"
    )
    recipe = """item_filename:
  version: 1
  fields: [observed_on, subject]
item_presentation:
  version: 1
  title: subject
  summary: [observed_on]
  long_text: [note, provenance]
"""
    return source.removesuffix("---\n") + recipe + "---\n"


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
            expected_container_hash=parsed.snapshot,
            expected_item_version="0" * 64,
            why="correct the maintenance status",
        )

    result = records.update_record(
        tmp_path,
        manifest.path,
        item_key=record.identity.key,
        changes={"status": "completed"},
        expected_container_hash=parsed.snapshot,
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


def test_record_human_representation_is_written_atomically_and_path_stays_stable(
    tmp_path: Path,
) -> None:
    from exomem import records

    setup_presentation_collection(tmp_path, presentation=False)
    manifest_path = tmp_path / "Knowledge Base/Records/Observed/_collection.md"
    manifest_path.write_text(_human_record_manifest(), encoding="utf-8")
    manifest = collections.load_manifest(tmp_path, manifest_path)
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()

    added = records.append_record(
        tmp_path,
        manifest.path,
        item=presentation_values(),
        item_key="11111111-1111-4111-8111-111111111111",
        expected_container_hash=snapshot.snapshot,
        why="preserve one readable observation",
        body="Authored observation context.\n",
    )
    path = manifest_path.parent / "Items" / "2026-08-13 — Sample A.md"
    first = path.read_text(encoding="utf-8")
    assert added["affected_paths"] == [path.relative_to(tmp_path).as_posix()]
    assert "# Sample &lt;A&gt;" in first
    assert "**Observed On:** 2026-08-13" in first
    assert "Authored observation context.\n" in first

    current = record_formats.load_adapter(tmp_path, manifest).read().records[0]
    changed = records.update_record(
        tmp_path,
        manifest.path,
        item_key=current.identity.key,
        changes={"observed_on": "2026-08-14"},
        expected_container_hash=added["after_container_hash"],
        expected_item_version=current.source.hash,
        why="correct the observed date without moving the stable item",
    )

    assert changed["affected_paths"] == [path.relative_to(tmp_path).as_posix()]
    assert path.is_file()
    assert not path.with_name("2026-08-14 — Sample A.md").exists()
    second = path.read_text(encoding="utf-8")
    assert "**Observed On:** 2026-08-14" in second
    assert "Authored observation context.\n" in second


def test_shared_presentation_render_failure_rolls_back_the_complete_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import records

    setup_presentation_collection(tmp_path, presentation=False)
    manifest_path = tmp_path / "Knowledge Base/Records/Observed/_collection.md"
    manifest_path.write_text(_human_record_manifest(), encoding="utf-8")
    manifest = collections.load_manifest(tmp_path, manifest_path)
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    manifest_before = manifest_path.read_bytes()
    log_path = tmp_path / "Knowledge Base/log.md"
    log_before = log_path.read_bytes()

    def fail_render(*_args: object, **_kwargs: object) -> str:
        raise collections.CollectionError(
            "UNRENDERABLE_ITEM_PRESENTATION", "selected value cannot render"
        )

    monkeypatch.setattr(record_formats, "splice_item_presentation", fail_render)
    with pytest.raises(collections.CollectionError, match="UNRENDERABLE_ITEM_PRESENTATION"):
        records.append_record(
            tmp_path,
            manifest.path,
            item=presentation_values(),
            item_key="11111111-1111-4111-8111-111111111111",
            expected_container_hash=snapshot.snapshot,
            why="attempt one readable observation",
        )

    assert list((manifest_path.parent / "Items").glob("*.md")) == []
    assert manifest_path.read_bytes() == manifest_before
    assert log_path.read_bytes() == log_before


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
    source = tmp_path / "Knowledge Base/Records/New/Events"
    assert source.is_dir()
    assert records.inspect_audit_gap(tmp_path, manifest_path) == {"status": "ok", "gaps": []}
    source.rmdir()
    assert records.inspect_audit_gap(tmp_path, manifest_path)["status"] == "gap"
    with pytest.raises(collections.CollectionError, match="CREATE_ONLY_CONFLICT"):
        records.create_collection(tmp_path, manifest_path, manifest, why="retry creation")


def test_create_markdown_log_inside_an_existing_empty_collection_directory(
    tmp_path: Path,
) -> None:
    from exomem import records

    _activity_log(tmp_path)
    collection = tmp_path / "Knowledge Base/Records/Project/Delivery Outcomes"
    collection.mkdir(parents=True)
    manifest_path = collection.relative_to(tmp_path).joinpath("_collection.md").as_posix()
    manifest = """---
type: collection
exomem_id: 7f2f8a4d-67b5-4d6d-9b91-b5b97b25dd7a
title: Delivery outcomes
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-log
  source: Delivery log.md
  format_version: 1
  section: {level: 2, title: Outcomes}
  item_heading:
    level: 3
    fields:
      - {name: observed_on, type: date, format: "%Y-%m-%d"}
      - {name: change_key, type: string}
      - {name: outcome, type: string}
    separator: " · "
    note: {field: summary, open: " (", close: ")"}
  child_rows:
    prefix: "- "
    delimiter: "|"
    fields: [field, value]
    container_field: details
  insertion: newest-first
item_schema:
  natural_key: [observed_on, change_key, outcome]
  fields:
    observed_on: {type: date, required: true}
    change_key: {type: string, required: true}
    outcome: {type: string, required: true}
    summary: {type: string, required: true}
    details:
      type: array
      items: {type: object}
---
"""

    result = records.create_collection(
        tmp_path,
        manifest_path,
        manifest,
        why="create an observed delivery log",
    )

    assert result["outcome"] == "committed"
    assert (collection / "_collection.md").is_file()
    assert (collection / "Delivery log.md").read_text(encoding="utf-8") == "## Outcomes\n"


def test_create_collection_without_scaffold_has_an_audited_absent_source_state(
    tmp_path: Path,
) -> None:
    from exomem import records

    _activity_log(tmp_path)
    manifest_path = "Knowledge Base/Records/Manifest Only/_collection.md"
    manifest = """---
type: collection
exomem_id: 55555555-5555-4555-8555-555555555555
title: Manifest only records
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

    result = records.create_collection(
        tmp_path, manifest_path, manifest, why="create only the collection contract", scaffold=False
    )

    created = tmp_path / manifest_path
    assert result["audit_correlation"] is not None
    assert result["after_container_hash"] is not None
    assert "record_audit:" in created.read_text(encoding="utf-8")
    assert not (created.parent / "Events").exists()
    assert records.inspect_audit_gap(tmp_path, manifest_path) == {"status": "ok", "gaps": []}
    (created.parent / "Events").mkdir()
    assert records.inspect_audit_gap(tmp_path, manifest_path)["status"] == "gap"


def test_create_unscaffolded_markdown_log_has_an_audited_absent_source_state(
    tmp_path: Path,
) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = (
        (fixture / "_collection.md")
        .read_text(encoding="utf-8")
        .replace("9ba8d1cf-d1e7-4309-95ae-cb28d7a6eea8", "56565656-5656-4565-8565-565656565656")
    )
    manifest_path = "Knowledge Base/Records/Log Only/_collection.md"

    result = records.create_collection(
        tmp_path, manifest_path, manifest, why="create only the log contract", scaffold=False
    )

    source = tmp_path / "Knowledge Base/Records/Log Only/Training Log.md"
    assert result["audit_correlation"] is not None
    assert result["after_container_hash"] is not None
    assert not source.exists()
    assert records.inspect_audit_gap(tmp_path, manifest_path) == {"status": "ok", "gaps": []}
    source.write_text("manual source\n", encoding="utf-8")
    assert records.inspect_audit_gap(tmp_path, manifest_path)["status"] == "gap"


def test_append_preserves_committed_batch_publication_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import records, vault

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    committed = vault.BatchWriteError(
        "BATCH_CLEANUP_INCOMPLETE",
        vault.BatchTargetSummary(affected_count=3, targets=("a.md",), omitted_target_count=2),
        committed=True,
    )
    monkeypatch.setattr(
        records.vault,
        "batch_atomic_write",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(committed),
    )

    with pytest.raises(vault.BatchWriteError) as raised:
        records.append_record(
            tmp_path,
            manifest.path,
            item={
                "occurred_on": "2026-08-03",
                "title": "Pull",
                "status": "completed",
                "movements": [],
            },
            item_key="12121212-1212-4121-8121-121212121212",
            expected_container_hash=parsed.source_versions[-1].hash,
            why="preserve the committed batch outcome",
        )

    assert raised.value is committed
    assert raised.value.committed is True


def test_append_refuses_manual_equal_item_without_a_correlated_transition(tmp_path: Path) -> None:
    from exomem import records

    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    manual = parsed.records[0]

    with pytest.raises(collections.CollectionError) as raised:
        records.append_record(
            tmp_path,
            manifest.path,
            item=manual.values,
            item_key=manual.identity.key,
            expected_container_hash=parsed.snapshot,
            why="do not label a manual item as replayed",
            body=manual.body,
        )

    assert raised.value.code == "RECORD_ID_CONFLICT"


def test_item_body_audit_shaped_prose_does_not_forge_an_audit_marker(tmp_path: Path) -> None:
    from exomem import records

    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    records.append_record(
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
        },
        item_key="13131313-1313-4131-8131-131313131313",
        expected_container_hash=parsed.snapshot,
        why="record a body marker example",
        body="Example prose: exomem-record-audit: deadbeefdeadbeefdeadbeef",
    )

    assert records.inspect_audit_gap(tmp_path, manifest.path) == {"status": "ok", "gaps": []}


def test_mutable_record_ids_are_normalized_uuids_and_dataset_stays_unsupported(
    tmp_path: Path,
) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    with pytest.raises(collections.CollectionError, match="INVALID_RECORD_ID"):
        records.append_record(
            tmp_path,
            manifest.path,
            item={
                "occurred_on": "2026-08-03",
                "title": "Push",
                "status": "completed",
                "movements": [],
            },
            item_key="not-a-uuid",
            expected_container_hash=parsed.source_versions[-1].hash,
            why="reject invalid identity",
        )

    dataset = copy_dataset_fixture(tmp_path / "dataset")
    dataset_manifest = _manifest(tmp_path / "dataset", dataset)
    with pytest.raises(collections.CollectionError, match="UNSUPPORTED_RECORD_MUTATION"):
        records.append_record(
            tmp_path / "dataset",
            dataset_manifest.path,
            item={"reading_id": "r-new", "occurred_on": "2026-01-01", "category": "x", "value": 1},
            item_key="not-a-uuid",
            expected_container_hash="not-a-hash",
            why="dataset remains read only",
        )


def test_log_body_refuses_and_partial_status_is_declarative(tmp_path: Path) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest_path = fixture / "_collection.md"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            '    - equals: "Stopped, didn\'t feel like it, circadian and recovery"\n      values:\n        status: aborted\n',
            '    - equals: "Stopped, didn\'t feel like it, circadian and recovery"\n      values:\n        status: aborted\n    - equals: "Partial"\n      values:\n        status: partial\n',
        ),
        encoding="utf-8",
    )
    manifest = _manifest(tmp_path, fixture)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    item = {
        "occurred_on": "2026-08-03",
        "title": "Pull",
        "note": "Partial",
        "status": "partial",
        "movements": [],
    }
    records.append_record(
        tmp_path,
        manifest.path,
        item=item,
        item_key="77777777-7777-4777-8777-777777777777",
        expected_container_hash=parsed.source_versions[-1].hash,
        why="record partial session",
    )
    with pytest.raises(collections.CollectionError, match="UNREPRESENTABLE_RECORD_BODY"):
        records.append_record(
            tmp_path,
            manifest.path,
            item=item,
            item_key="88888888-8888-4888-8888-888888888888",
            expected_container_hash=record_formats.load_adapter(tmp_path, manifest)
            .read()
            .source_versions[-1]
            .hash,
            why="reject hidden log body",
            body="cannot be represented",
        )


def test_item_container_hash_retries_after_append_and_manifest_object_drift_refuses(
    tmp_path: Path,
) -> None:
    from exomem import records

    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    before = record_formats.load_adapter(tmp_path, manifest).read()
    item = {
        "occurred_on": "2026-08-03",
        "asset": "[[Assets/Vehicle]]",
        "provider": "Northside Garage",
        "services": ["oil change"],
        "amount": 95.0,
        "currency": "GBP",
        "status": "completed",
    }
    result = records.append_record(
        tmp_path,
        manifest,
        item=item,
        item_key="99999999-9999-4999-8999-999999999999",
        expected_container_hash=before.snapshot,
        why="record maintenance",
    )
    replay = records.append_record(
        tmp_path,
        manifest.path,
        item=item,
        item_key="99999999-9999-4999-8999-999999999999",
        expected_container_hash=result["after_container_hash"],
        why="record maintenance",
    )
    assert replay["outcome"] == "replayed"

    changed = fixture / "_collection.md"
    changed.write_text(changed.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
    with pytest.raises(collections.CollectionError, match="STALE_COLLECTION_MANIFEST"):
        records.append_record(
            tmp_path,
            manifest,
            item=item,
            item_key="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            expected_container_hash=result["after_container_hash"],
            why="refuse stale contract",
        )


# --- identity from the declared natural key (design D3) -------------------------


def _natural_key_of(manifest: collections.CollectionManifest, values: dict) -> str:
    """The serialisation the READ path uses, spelled exactly once here too."""
    return collections.natural_key_serialization(
        manifest.schema.version,
        manifest.schema.natural_key,
        values,
        field_types={name: spec.type for name, spec in manifest.schema.fields.items()},
    )


def _service(**overrides) -> dict:
    item = {
        "occurred_on": "2026-07-01",
        "asset": "[[Assets/Vehicle]]",
        "provider": "City Garage",
        "odometer": 44_000,
        "status": "completed",
    }
    item.update(overrides)
    return item


def test_append_without_a_key_derives_the_declared_natural_key(tmp_path: Path) -> None:
    """`uuid4` on an omitted key made a re-stated event a duplicate, not a replay.

    Every manifest already declares a natural key and the read path already knows
    how to serialise it; the write path minted a random identity instead, so the
    substrate could not see that the same observation had arrived twice.
    """
    from exomem import records

    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()

    result = records.append_record(
        tmp_path,
        manifest.path,
        item=_service(),
        expected_container_hash=parsed.snapshot,
        why="log the completed service",
    )

    stored = next(
        record
        for record in record_formats.load_adapter(tmp_path, manifest).read().records
        if record.identity.key == result["item_key"]
    )
    assert result["item_key"] == collections.inferred_item_key(
        manifest.collection_id, _natural_key_of(manifest, stored.values)
    )
    assert result["outcome"] == "committed"


def test_a_re_stated_append_replays_instead_of_duplicating(tmp_path: Path) -> None:
    from exomem import records

    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    before = len(parsed.records)

    first = records.append_record(
        tmp_path,
        manifest.path,
        item=_service(),
        expected_container_hash=parsed.snapshot,
        why="log the completed service",
    )
    replay = records.append_record(
        tmp_path,
        manifest.path,
        item=_service(),
        expected_container_hash=first["after_container_hash"],
        why="log the completed service",
    )

    assert replay["outcome"] == "replayed"
    assert replay["item_key"] == first["item_key"]
    assert len(record_formats.load_adapter(tmp_path, manifest).read().records) == before + 1


def test_the_same_natural_key_with_different_content_refuses(tmp_path: Path) -> None:
    from exomem import records

    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    first = records.append_record(
        tmp_path,
        manifest.path,
        item=_service(),
        expected_container_hash=parsed.snapshot,
        why="log the completed service",
    )

    with pytest.raises(collections.CollectionError, match="RECORD_ID_CONFLICT"):
        records.append_record(
            tmp_path,
            manifest.path,
            item=_service(odometer=44_500),
            expected_container_hash=first["after_container_hash"],
            why="log a different odometer for the same service",
        )


def test_a_missing_natural_key_field_still_mints_a_random_identity(tmp_path: Path) -> None:
    """`provider` is declared in the natural key and is NOT required.

    Derivation is only sound when every declared field is present; an absent one
    must fall back to the pre-change behaviour rather than serialise a hole.
    """
    from exomem import records

    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    partial = _service()
    partial.pop("provider")

    first = records.append_record(
        tmp_path,
        manifest.path,
        item=partial,
        expected_container_hash=parsed.snapshot,
        why="log a service with no provider",
    )
    second = records.append_record(
        tmp_path,
        manifest.path,
        item={**partial, "occurred_on": "2026-07-02"},
        expected_container_hash=first["after_container_hash"],
        why="log another service with no provider",
    )

    assert first["item_key"] != second["item_key"]
    stored = next(
        record
        for record in record_formats.load_adapter(tmp_path, manifest).read().records
        if record.identity.key == first["item_key"]
    )
    assert "provider" not in stored.values


def test_an_explicit_item_key_still_wins(tmp_path: Path) -> None:
    from exomem import records

    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    explicit = "33333333-3333-4333-8333-333333333333"

    result = records.append_record(
        tmp_path,
        manifest.path,
        item=_service(),
        item_key=explicit,
        expected_container_hash=parsed.snapshot,
        why="log the completed service under an explicit identity",
    )

    assert result["item_key"] == explicit


# --- RECORD_NATURAL_KEY_CONFLICT (design D3) ------------------------------------


def test_a_derived_twin_of_a_uuid4_keyed_item_refuses(tmp_path: Path) -> None:
    """The hole the replay rules cannot see.

    The fixture's oil-change event was keyed with a `uuid4` before derivation
    existed. Re-stating that same observation derives a DIFFERENT key, so nothing
    in the replay path matches and the collection would have silently held two
    records of one event under two identities.
    """
    from exomem import records

    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    existing = next(
        record
        for record in parsed.records
        if record.identity.key == "a8d391a5-c2dc-4e79-b57b-6b2bbcaefd64"
    )

    with pytest.raises(collections.CollectionError) as caught:
        records.append_record(
            tmp_path,
            manifest.path,
            item={
                "occurred_on": "2026-06-01",
                "asset": "[[Assets/Vehicle]]",
                "provider": "Northside Garage",
                "odometer": 42_750,
                "status": "completed",
            },
            expected_container_hash=parsed.snapshot,
            why="re-state the oil change",
        )

    assert caught.value.code == "RECORD_NATURAL_KEY_CONFLICT"
    assert existing.identity.key in str(caught.value.details)
    after = record_formats.load_adapter(tmp_path, manifest).read()
    assert len(after.records) == len(parsed.records), "the refusal must write nothing"


def test_a_natural_key_conflict_names_every_existing_twin(tmp_path: Path) -> None:
    from exomem import records

    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    twins = []
    for index, record_id in enumerate(
        ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222")
    ):
        (fixture / "Events" / "released" / f"twin-{index}.md").write_text(
            "---\n"
            "type: record\n"
            f"collection_id: {manifest.collection_id}\n"
            f"record_id: {record_id}\n"
            "schema_version: 1\n"
            "occurred_on: 2026-05-05\n"
            'asset: "[[Assets/Vehicle]]"\n'
            "provider: Twin Garage\n"
            f"odometer: {40_000 + index}\n"
            "status: completed\n"
            "---\n\nA pre-existing duplicate.\n",
            encoding="utf-8",
        )
        twins.append(record_id)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()

    with pytest.raises(collections.CollectionError) as caught:
        records.append_record(
            tmp_path,
            manifest.path,
            item={
                "occurred_on": "2026-05-05",
                "asset": "[[Assets/Vehicle]]",
                "provider": "Twin Garage",
                "odometer": 41_000,
                "status": "completed",
            },
            expected_container_hash=parsed.snapshot,
            why="re-state the twinned service",
        )

    assert caught.value.code == "RECORD_NATURAL_KEY_CONFLICT"
    named = str(caught.value.details)
    for record_id in twins:
        assert record_id in named


# --- the same rule on the UPDATE path (round 1, M3) -----------------------------


def test_an_update_onto_another_items_natural_key_refuses(tmp_path: Path) -> None:
    """Append refuses a twin forever; update was creating them.

    Nothing about the natural key is a property of how an item ARRIVED. An update
    that moves one item's declared key onto another's produces exactly the state
    the append check exists to prevent -- and then the collection cannot be
    appended to for that key again, so the write that created the problem is the
    only one that was allowed.
    """
    from exomem import records

    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    first, second = parsed.records[0], parsed.records[1]
    assert first.identity.key != second.identity.key

    with pytest.raises(collections.CollectionError) as caught:
        records.update_record(
            tmp_path,
            manifest.path,
            item_key=second.identity.key,
            changes={
                name: first.values[name]
                for name in manifest.schema.natural_key
                if name in first.values
            },
            expected_container_hash=parsed.snapshot,
            expected_item_version=second.source.hash,
            why="restate the second service as the first",
        )

    assert caught.value.code == "RECORD_NATURAL_KEY_CONFLICT"
    assert first.identity.key in str(caught.value.details)
    after = record_formats.load_adapter(tmp_path, manifest).read()
    assert {record.identity.key for record in after.records} == {
        record.identity.key for record in parsed.records
    }
    assert (
        next(r for r in after.records if r.identity.key == second.identity.key).values
        == second.values
    ), "the refusal must write nothing"


def test_a_planning_update_onto_another_items_natural_key_refuses(tmp_path: Path) -> None:
    """Planning updates run through the same writer, so they inherit the rule."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from lifecycle_fixtures import PLANNING_PATH, queue_item, seed_vault

    from exomem import planning, records

    seed_vault(tmp_path)
    first = queue_item(tmp_path, "Batch 1")
    second = queue_item(tmp_path, "Batch 2")
    manifest = collections.load_manifest(tmp_path, tmp_path / PLANNING_PATH)
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    guards = records.lifecycle_guards(manifest, snapshot)
    item = next(r for r in snapshot.records if r.identity.key == second["plan_id"])

    with pytest.raises(collections.CollectionError) as caught:
        planning.update(
            tmp_path,
            PLANNING_PATH,
            plan_id=second["plan_id"],
            changes={"title": "Batch 1"},
            expected_container_hash=guards["expected_container_hash"],
            expected_item_version=item.source.hash,
            why="rename the second deliverable onto the first",
        )

    assert caught.value.code == "RECORD_NATURAL_KEY_CONFLICT"
    assert first["plan_id"] in str(caught.value.details)


def test_planning_triage_cannot_reach_title_the_declared_natural_key(
    tmp_path: Path,
) -> None:
    """Triage cannot reach `title` -- and `title` is what these collections key on.

    Stated exactly, because the general claim is false: triage's transition
    surface excludes `title`, so a collection keyed on `[title]` is out of
    identity's way by construction. A collection keyed on a field triage CAN
    reach is not, and the twin check refuses there the same way it does on
    update. The other half of that sentence is the test below.
    """
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from lifecycle_fixtures import PLANNING_PATH, queue_item, seed_vault

    from exomem import planning, records

    seed_vault(tmp_path)
    added = queue_item(tmp_path, "Batch 1")
    manifest = collections.load_manifest(tmp_path, tmp_path / PLANNING_PATH)
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    guards = records.lifecycle_guards(manifest, snapshot)
    item = next(r for r in snapshot.records if r.identity.key == added["plan_id"])
    assert list(manifest.schema.natural_key) == ["title"]

    with pytest.raises(collections.CollectionError) as caught:
        planning.triage(
            tmp_path,
            PLANNING_PATH,
            plan_id=added["plan_id"],
            transition={"title": "Batch 2"},
            expected_container_hash=guards["expected_container_hash"],
            expected_item_version=item.source.hash,
            why="try to rename through triage",
        )

    assert caught.value.code == "INVALID_PLAN_ARGUMENTS"


def test_a_collection_keyed_on_a_triage_field_refuses_the_same_way(
    tmp_path: Path,
) -> None:
    """The other half: where triage CAN reach the key, the twin check refuses.

    `status` is inside triage's transition surface, so a Planning collection
    keyed on `[status]` is a collection where the high-traffic write really can
    move identity -- and it is refused with the same code as an update, rather
    than silently producing the twin state append then refuses forever.
    """
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from lifecycle_fixtures import planning_manifest, seed_vault

    from exomem import planning, records

    seed_vault(tmp_path)
    keyed = "Knowledge Base/Planning/ByStatus/_collection.md"
    planning.create_collection(
        tmp_path,
        keyed,
        planning_manifest(
            natural_key="[status]", collection_id="0b7f5c92-31ad-4e60-8f14-6c9d2a8e4b71"
        ),
        why="file deliverables keyed on their state",
    )
    shape = {"kind": "outcome", "commitment": "committed", "horizon": "quarter"}
    first = planning.add(
        tmp_path,
        keyed,
        item={"title": "Alpha", "status": "planned", **shape},
        why="one planned outcome",
    )
    second = planning.add(
        tmp_path,
        keyed,
        item={"title": "Beta", "status": "active", **shape},
        why="one outcome already moving",
    )
    manifest = collections.load_manifest(tmp_path, tmp_path / keyed)
    assert list(manifest.schema.natural_key) == ["status"]
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    guards = records.lifecycle_guards(manifest, snapshot)
    item = next(r for r in snapshot.records if r.identity.key == second["plan_id"])

    with pytest.raises(collections.CollectionError) as caught:
        planning.triage(
            tmp_path,
            keyed,
            plan_id=second["plan_id"],
            transition={"status": "planned"},
            expected_container_hash=guards["expected_container_hash"],
            expected_item_version=item.source.hash,
            why="move it back to planned",
        )

    assert caught.value.code == "RECORD_NATURAL_KEY_CONFLICT"
    assert first["plan_id"] in str(caught.value.details)


def test_the_natural_key_refusal_tells_a_legacy_vault_how_to_recover(
    tmp_path: Path,
) -> None:
    """A vault that already holds twins cannot append for that key at all.

    "Update the named item instead" is not a route out of that state: the twins
    predate the check, and the caller needs to be told which two writes DO end
    it.
    """
    from exomem.cli_ops import _REMEDIATION

    remediation = _REMEDIATION["RECORD_NATURAL_KEY_CONFLICT"]

    assert "distinct natural key" in remediation
    assert "delete" in remediation and "archive" in remediation
    assert "retry" in remediation


# --------------------------------------------------------------------------
# Field-addressed refusals and strategy-aware representability
# --------------------------------------------------------------------------


def _ledger(tmp_path: Path, *, source: str = "Entries") -> collections.CollectionManifest:
    """A generic publications-ledger collection with no personal content."""
    return collections.load_manifest(tmp_path, setup_ledger_collection(tmp_path, source=source))


def test_item_refusal_names_every_failing_field_in_one_response(tmp_path: Path) -> None:
    from exomem import records

    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()

    with pytest.raises(collections.CollectionError) as caught:
        records.append_record(
            tmp_path,
            manifest.path,
            item={
                "occurred_on": "2026-08-03",
                "asset": "[[Assets/Van]]",
                "odometer": "many",
                "amount": "lots",
                "workshop": "north",
            },
            expected_container_hash=parsed.snapshot,
            why="record a maintenance event",
        )

    error = caught.value
    assert error.code == "SCHEMA_UNKNOWN_FIELD"
    assert error.reason == "item uses fields outside the schema"
    assert error.details["field"] == "workshop"
    assert [
        (issue["field"], issue["code"], issue["received"]) for issue in error.details["issues"]
    ] == [
        ("workshop", "SCHEMA_UNKNOWN_FIELD", "str"),
        ("odometer", "SCHEMA_FIELD_TYPE", "str"),
        ("amount", "SCHEMA_FIELD_TYPE", "str"),
    ]
    assert all(issue["reason"] for issue in error.details["issues"])


def test_every_undeclared_field_is_named(tmp_path: Path) -> None:
    from exomem import records

    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()

    with pytest.raises(collections.CollectionError) as caught:
        records.append_record(
            tmp_path,
            manifest.path,
            item={
                "occurred_on": "2026-08-03",
                "asset": "[[Assets/Van]]",
                "workshop": "north",
                "invoice": 12,
            },
            expected_container_hash=parsed.snapshot,
            why="record a maintenance event",
        )

    issues = caught.value.details["issues"]
    assert [issue["field"] for issue in issues] == ["invoice", "workshop"]
    assert {issue["code"] for issue in issues} == {"SCHEMA_UNKNOWN_FIELD"}
    assert [issue["received"] for issue in issues] == ["int", "str"]


def test_markdown_log_still_refuses_a_line_break_and_names_the_nested_field(
    tmp_path: Path,
) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    source = fixture / "Training Log.md"
    before = source.read_bytes()

    with pytest.raises(collections.CollectionError) as caught:
        records.append_record(
            tmp_path,
            manifest.path,
            item={
                "occurred_on": "2026-08-03",
                "title": "Pull",
                "status": "completed",
                "movements": [
                    {"movement": "Deadlift", "band": "grey", "repetitions": "22"},
                    {"movement": "Row\nheavy", "band": "black", "repetitions": "18"},
                ],
            },
            expected_container_hash=parsed.source_versions[-1].hash,
            why="record a completed session",
        )

    error = caught.value
    assert error.code == "UNREPRESENTABLE_RECORD_VALUE"
    assert error.reason == "record value cannot render losslessly"
    assert error.details["field"] == "movements[1].movement"
    assert error.details["issues"][0]["received"] == "str"
    assert source.read_bytes() == before


def test_markdown_log_refuses_a_line_break_in_a_heading_field(tmp_path: Path) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()

    with pytest.raises(collections.CollectionError) as caught:
        records.append_record(
            tmp_path,
            manifest.path,
            item={"occurred_on": "2026-08-03", "title": "Pull\nsession", "status": "completed"},
            expected_container_hash=parsed.source_versions[-1].hash,
            why="record a completed session",
        )

    assert caught.value.code == "UNREPRESENTABLE_RECORD_VALUE"
    assert caught.value.details["field"] == "title"


def test_array_element_failure_is_addressed_by_index(tmp_path: Path) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()

    with pytest.raises(collections.CollectionError) as caught:
        records.append_record(
            tmp_path,
            manifest.path,
            item={
                "occurred_on": "2026-08-03",
                "title": "Pull",
                "status": "completed",
                "movements": [
                    {"movement": "Deadlift", "band": "grey", "repetitions": "22"},
                    {"movement": "Row", "band": "black", "repetitions": "18"},
                    "not an object",
                ],
            },
            expected_container_hash=parsed.source_versions[-1].hash,
            why="record a completed session",
        )

    error = caught.value
    assert error.code == "SCHEMA_FIELD_TYPE"
    assert error.details["field"] == "movements[2]"
    assert error.details["issues"][0]["received"] == "str"


def test_nested_object_sub_field_failure_is_addressed_by_path(tmp_path: Path) -> None:
    from exomem import records

    manifest = _ledger(tmp_path)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()

    with pytest.raises(collections.CollectionError) as caught:
        records.append_record(
            tmp_path,
            manifest.path,
            item=ledger_item(
                metrics=[
                    {"name": "views", "value": 12},
                    {"name": "replies", "value": 3},
                    {"name": "shares", "value": float("nan")},
                ]
            ),
            expected_container_hash=parsed.snapshot,
            why="record a published entry",
        )

    error = caught.value
    assert error.code == "SCHEMA_FIELD_TYPE"
    assert error.details["field"] == "metrics[2].value"
    assert error.details["issues"][0]["received"] == "float"


def test_markdown_item_commits_multi_line_text_and_reads_it_back_identically(
    tmp_path: Path,
) -> None:
    from exomem import records

    manifest = _ledger(tmp_path)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    exact_text = "First line.\n\nThird line."

    committed = records.append_record(
        tmp_path,
        manifest.path,
        item=ledger_item(exact_text=exact_text),
        expected_container_hash=parsed.snapshot,
        why="record a published entry",
    )

    assert committed["outcome"] == "committed"
    stored = record_formats.load_adapter(tmp_path, manifest).read().records
    assert len(stored) == 1
    read_back = stored[0].values["exact_text"]
    assert read_back == exact_text
    assert (
        hashlib.sha256(read_back.encode("utf-8")).hexdigest()
        == hashlib.sha256(exact_text.encode("utf-8")).hexdigest()
    )


def test_typed_markdown_item_fields_survive_the_round_trip(tmp_path: Path) -> None:
    from exomem import records

    manifest = _ledger(tmp_path)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    item = ledger_item()

    records.append_record(
        tmp_path,
        manifest.path,
        item=item,
        expected_container_hash=parsed.snapshot,
        why="record a published entry",
    )

    stored = record_formats.load_adapter(tmp_path, manifest).read().records[0]
    for name in ("published_on", "published_at", "word_count", "details", "metrics", "channels"):
        assert stored.values[name] == item[name], name


def test_round_trip_refuses_when_serialisation_would_lose_a_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import records, vault

    manifest = _ledger(tmp_path)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    entries = tmp_path / "Knowledge Base/Records/Publications/Entries"
    real = vault.serialize_frontmatter

    def corrupt(frontmatter: dict[str, object]) -> str:
        if "exact_text" in frontmatter:
            frontmatter = {
                **frontmatter,
                "exact_text": f"{frontmatter['exact_text']} (corrupted)",
            }
        return real(frontmatter)

    monkeypatch.setattr(vault, "serialize_frontmatter", corrupt)

    with pytest.raises(collections.CollectionError) as caught:
        records.append_record(
            tmp_path,
            manifest.path,
            item=ledger_item(),
            expected_container_hash=parsed.snapshot,
            why="record a published entry",
        )

    assert caught.value.code == "UNREPRESENTABLE_RECORD_VALUE"
    assert caught.value.details["field"] == "exact_text"
    assert list(entries.iterdir()) == []


# --------------------------------------------------------------------------
# Item-key remediation
# --------------------------------------------------------------------------


def test_natural_key_value_supplied_as_item_key_refuses_with_remediation(
    tmp_path: Path,
) -> None:
    from exomem import records

    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()

    with pytest.raises(collections.CollectionError) as caught:
        records.append_record(
            tmp_path,
            manifest.path,
            item={
                "occurred_on": "2026-08-03",
                "asset": "[[Assets/Van]]",
                "provider": "Northside",
            },
            item_key="2026-08-03",
            expected_container_hash=parsed.snapshot,
            why="record a maintenance event",
        )

    error = caught.value
    assert error.code == "INVALID_RECORD_ID"
    assert error.reason == "record ID must be a UUID"
    assert error.details["argument"] == "item_key"
    assert error.details["received"] == "2026-08-03"
    assert error.details["natural_key"] == ["occurred_on", "asset", "provider"]
    assert "internal UUID" in error.details["remediation"]
    assert "omit" in error.details["remediation"]


def test_complete_natural_key_with_any_non_uuid_item_key_gets_remediation(
    tmp_path: Path,
) -> None:
    from exomem import records

    manifest = _ledger(tmp_path)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()

    with pytest.raises(collections.CollectionError) as caught:
        records.append_record(
            tmp_path,
            manifest.path,
            item=ledger_item(),
            item_key="quarterly-note-2026",
            expected_container_hash=parsed.snapshot,
            why="record a published entry",
        )

    assert caught.value.details["natural_key"] == ["published_on", "slug"]
    assert caught.value.details["received"] == "quarterly-note-2026"


def test_other_non_uuid_item_keys_keep_the_plain_refusal(tmp_path: Path) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()

    with pytest.raises(collections.CollectionError) as caught:
        records.append_record(
            tmp_path,
            manifest.path,
            item={"occurred_on": "2026-08-03"},
            item_key="oops",
            expected_container_hash=parsed.source_versions[-1].hash,
            why="record a completed session",
        )

    error = caught.value
    assert error.code == "INVALID_RECORD_ID"
    assert error.reason == "record ID must be a UUID"
    assert error.details["received"] == "oops"
    assert "natural_key" not in error.details
    assert "remediation" not in error.details


def test_update_with_a_natural_key_value_as_item_key_gets_remediation(
    tmp_path: Path,
) -> None:
    from exomem import records

    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    record = parsed.records[0]

    with pytest.raises(collections.CollectionError) as caught:
        records.update_record(
            tmp_path,
            manifest.path,
            item_key="2026-08-03",
            changes={
                "occurred_on": "2026-08-03",
                "asset": "[[Assets/Van]]",
                "provider": "Northside",
            },
            expected_container_hash=parsed.snapshot,
            expected_item_version=record.source.hash,
            why="correct the maintenance event",
        )

    error = caught.value
    assert error.code == "INVALID_RECORD_ID"
    assert error.details["natural_key"] == ["occurred_on", "asset", "provider"]
    assert error.details["received"] == "2026-08-03"


# --------------------------------------------------------------------------
# Held records
# --------------------------------------------------------------------------


def _held_directory(tmp_path: Path) -> Path:
    return tmp_path / "Knowledge Base/Records/Publications/Held"


def _held_files(tmp_path: Path) -> list[Path]:
    directory = _held_directory(tmp_path)
    return sorted(directory.glob("*.md")) if directory.is_dir() else []


def _held_payload(path: Path) -> dict[str, object]:
    from exomem import vault

    frontmatter, body, _marker = vault.parse_frontmatter(
        path.read_text(encoding="utf-8"), strict=True
    )
    fenced = body.split("```json", 1)[1].rsplit("```", 1)[0]
    return {"frontmatter": frontmatter, "candidate": json.loads(fenced)}


def _refuse_append(tmp_path: Path, manifest, **kwargs: object):
    from exomem import records

    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    with pytest.raises(collections.CollectionError) as caught:
        records.append_record(
            tmp_path,
            manifest.path,
            item=ledger_item(unlisted_channel="digest"),
            expected_container_hash=parsed.snapshot,
            why="record a published entry",
            **kwargs,
        )
    return caught.value


def test_a_refused_append_holds_the_complete_candidate(tmp_path: Path) -> None:
    manifest = _ledger(tmp_path)

    error = _refuse_append(tmp_path, manifest)

    assert error.code == "SCHEMA_UNKNOWN_FIELD"
    held = error.details["held"]
    files = _held_files(tmp_path)
    assert len(files) == 1
    assert files[0].name == f"{held['held_id']}.md"
    assert held["path"] == f"Knowledge Base/Records/Publications/Held/{held['held_id']}.md"
    assert held["diagnostics"] == error.details["issues"]
    payload = _held_payload(files[0])
    frontmatter = payload["frontmatter"]
    assert frontmatter["type"] == "held-record"
    assert frontmatter["collection_id"] == manifest.collection_id
    assert frontmatter["held_id"] == held["held_id"]
    assert frontmatter["attempted_action"] == "append"
    assert frontmatter["why"] == "record a published entry"
    assert frontmatter["held_at"].startswith("20")
    assert len(frontmatter["candidate_sha256"]) == 64
    assert json.loads(frontmatter["diagnostics"]) == error.details["issues"]
    assert "target_item_key" not in frontmatter
    # Every generated frontmatter value stays on one line, so a held file never
    # depends on the representability rules that refused its candidate.
    for value in frontmatter.values():
        assert "\n" not in str(value)
    candidate = payload["candidate"]
    assert candidate["action"] == "append"
    assert candidate["item"] == ledger_item(unlisted_channel="digest")
    assert candidate["body"] == ""


def test_re_holding_the_same_candidate_rewrites_one_file(tmp_path: Path) -> None:
    manifest = _ledger(tmp_path)

    first = _refuse_append(tmp_path, manifest)
    second = _refuse_append(tmp_path, manifest)

    assert first.details["held"]["held_id"] == second.details["held"]["held_id"]
    assert len(_held_files(tmp_path)) == 1


def test_hold_refuses_when_the_held_directory_would_fall_under_the_item_source(
    tmp_path: Path,
) -> None:
    manifest = _ledger(tmp_path, source=".")

    error = _refuse_append(tmp_path, manifest)

    assert error.code == "SCHEMA_UNKNOWN_FIELD"
    assert "held" not in error.details
    assert error.details["warnings"]
    assert not _held_directory(tmp_path).exists()


def test_declining_the_hold_refuses_plainly(tmp_path: Path) -> None:
    manifest = _ledger(tmp_path)

    error = _refuse_append(tmp_path, manifest, hold=False)

    assert error.code == "SCHEMA_UNKNOWN_FIELD"
    assert error.details["issues"]
    assert "held" not in error.details
    assert not _held_directory(tmp_path).exists()


def test_a_hold_failure_does_not_mask_the_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import records

    manifest = _ledger(tmp_path)

    def fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise OSError("held file could not be written")

    monkeypatch.setattr(records, "hold_candidate", fail)

    error = _refuse_append(tmp_path, manifest)

    assert error.code == "SCHEMA_UNKNOWN_FIELD"
    assert error.details["field"] == "unlisted_channel"
    assert "held" not in error.details
    assert error.details["warnings"]
    assert not _held_directory(tmp_path).exists()


def test_guard_refusals_never_hold(tmp_path: Path) -> None:
    from exomem import records

    manifest = _ledger(tmp_path)

    with pytest.raises(collections.CollectionError) as caught:
        records.append_record(
            tmp_path,
            manifest.path,
            item=ledger_item(),
            expected_container_hash="0" * 64,
            why="record a published entry",
        )

    assert caught.value.code == "STALE_RECORD"
    assert not _held_directory(tmp_path).exists()


def test_natural_key_conflict_never_holds(tmp_path: Path) -> None:
    from exomem import records

    manifest = _ledger(tmp_path)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    committed = records.append_record(
        tmp_path,
        manifest.path,
        item=ledger_item(),
        expected_container_hash=parsed.snapshot,
        why="record a published entry",
    )

    with pytest.raises(collections.CollectionError) as caught:
        records.append_record(
            tmp_path,
            manifest.path,
            item=ledger_item(word_count=99),
            item_key="11111111-1111-4111-8111-111111111111",
            expected_container_hash=committed["after_container_hash"],
            why="record the same entry twice",
        )

    assert caught.value.code == "RECORD_NATURAL_KEY_CONFLICT"
    assert not _held_directory(tmp_path).exists()


def test_resuming_a_held_candidate_commits_once_and_removes_the_file(
    tmp_path: Path,
) -> None:
    from exomem import records

    manifest = _ledger(tmp_path)
    error = _refuse_append(tmp_path, manifest)
    held_id = error.details["held"]["held_id"]
    before_head = collections.load_manifest(tmp_path, tmp_path / manifest.path).audit_head

    resumed = records.append_record(
        tmp_path,
        manifest.path,
        held=held_id,
        item={"unlisted_channel": None},
        why="resume the held publication entry",
    )

    assert resumed["outcome"] == "committed"
    after = collections.load_manifest(tmp_path, tmp_path / manifest.path)
    assert after.audit_head != before_head
    stored = record_formats.load_adapter(tmp_path, manifest).read().records
    assert len(stored) == 1
    assert "unlisted_channel" not in stored[0].values
    assert stored[0].values["exact_text"] == ledger_item()["exact_text"]
    assert _held_files(tmp_path) == []


def test_a_resumed_candidate_that_refuses_again_reuses_one_held_file(
    tmp_path: Path,
) -> None:
    from exomem import records

    manifest = _ledger(tmp_path)
    error = _refuse_append(tmp_path, manifest)
    held_id = error.details["held"]["held_id"]

    with pytest.raises(collections.CollectionError) as caught:
        records.append_record(
            tmp_path,
            manifest.path,
            held=held_id,
            item={"word_count": "not a number"},
            why="resume the held publication entry",
        )

    assert caught.value.details["held"]["held_id"] == held_id
    files = _held_files(tmp_path)
    assert len(files) == 1
    assert json.loads(_held_payload(files[0])["frontmatter"]["diagnostics"]) == (
        caught.value.details["issues"]
    )


def test_resuming_an_unknown_reference_names_the_argument(tmp_path: Path) -> None:
    from exomem import records

    manifest = _ledger(tmp_path)

    with pytest.raises(collections.CollectionError) as caught:
        records.append_record(
            tmp_path,
            manifest.path,
            held="11111111-1111-4111-8111-111111111111",
            why="resume a reference that does not exist",
        )

    assert caught.value.code == "HELD_NOT_FOUND"
    assert caught.value.details["argument"] == "held"


def test_resuming_a_reference_from_another_collection_refuses(tmp_path: Path) -> None:
    from exomem import records

    manifest = _ledger(tmp_path)
    error = _refuse_append(tmp_path, manifest)
    held_id = error.details["held"]["held_id"]
    path = _held_files(tmp_path)[0]
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            manifest.collection_id, "99999999-9999-4999-8999-999999999999"
        ),
        encoding="utf-8",
    )

    with pytest.raises(collections.CollectionError) as caught:
        records.append_record(
            tmp_path,
            manifest.path,
            held=held_id,
            why="resume a reference from another collection",
        )

    assert caught.value.code == "HELD_COLLECTION_MISMATCH"
    assert caught.value.details["argument"] == "held"


def test_a_failed_cleanup_after_commit_warns_and_keeps_the_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import records

    manifest = _ledger(tmp_path)
    error = _refuse_append(tmp_path, manifest)
    held_id = error.details["held"]["held_id"]

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("held file could not be removed")

    monkeypatch.setattr(records, "_remove_held_file", fail)

    resumed = records.append_record(
        tmp_path,
        manifest.path,
        held=held_id,
        item={"unlisted_channel": None},
        why="resume the held publication entry",
    )

    assert resumed["outcome"] == "committed"
    assert any("HELD_CLEANUP_FAILED" in warning for warning in resumed["warnings"])
    assert len(record_formats.load_adapter(tmp_path, manifest).read().records) == 1


def test_discarding_a_held_candidate_removes_it_without_touching_the_audit_chain(
    tmp_path: Path,
) -> None:
    from exomem import records

    manifest = _ledger(tmp_path)
    error = _refuse_append(tmp_path, manifest)
    held_id = error.details["held"]["held_id"]
    before_head = collections.load_manifest(tmp_path, tmp_path / manifest.path).audit_head

    discarded = records.discard_held(
        tmp_path, manifest.path, held=held_id, why="the observation was a duplicate"
    )

    assert discarded["operation"] == "discard"
    assert discarded["held_id"] == held_id
    assert discarded["outcome"] == "discarded"
    assert _held_files(tmp_path) == []
    after = collections.load_manifest(tmp_path, tmp_path / manifest.path)
    assert after.audit_head == before_head


def test_discarding_an_unknown_reference_names_the_argument(tmp_path: Path) -> None:
    from exomem import records

    manifest = _ledger(tmp_path)

    with pytest.raises(collections.CollectionError) as caught:
        records.discard_held(
            tmp_path,
            manifest.path,
            held="11111111-1111-4111-8111-111111111111",
            why="discard a reference that does not exist",
        )

    assert caught.value.code == "HELD_NOT_FOUND"
    assert caught.value.details["argument"] == "held"


def test_a_refused_update_holds_the_changes_and_names_the_target(tmp_path: Path) -> None:
    from exomem import records

    manifest = _ledger(tmp_path)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    committed = records.append_record(
        tmp_path,
        manifest.path,
        item=ledger_item(),
        expected_container_hash=parsed.snapshot,
        why="record a published entry",
    )
    current = collections.load_manifest(tmp_path, tmp_path / manifest.path)
    refreshed = record_formats.load_adapter(tmp_path, current).read()
    record = refreshed.records[0]
    guards = records.lifecycle_guards(current, refreshed)

    with pytest.raises(collections.CollectionError) as caught:
        records.update_record(
            tmp_path,
            manifest.path,
            item_key=committed["item_key"],
            changes={"word_count": "not a number"},
            expected_container_hash=guards["expected_container_hash"],
            expected_item_version=record.source.hash,
            why="correct the published entry",
        )

    held = caught.value.details["held"]
    payload = _held_payload(_held_files(tmp_path)[0])
    assert payload["frontmatter"]["attempted_action"] == "update"
    assert payload["frontmatter"]["target_item_key"] == committed["item_key"]
    assert payload["candidate"]["changes"] == {"word_count": "not a number"}
    assert held["held_id"] == payload["frontmatter"]["held_id"]


def _ledger_query(tmp_path: Path) -> dict[str, object]:
    """The ledger's query payload without its wall-clock stamp."""
    from exomem.record_memory import record_memory

    rendered = record_memory(
        tmp_path,
        action="query",
        collection="Knowledge Base/Records/Publications/_collection.md",
    )["rendered"]
    payload = json.loads(rendered)
    payload.pop("generated_at", None)
    return payload


def test_a_held_file_is_invisible_to_items_query_audit_census_and_recall(
    tmp_path: Path,
) -> None:
    from exomem import recall_policy, records, vault

    manifest = _ledger(tmp_path)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    records.append_record(
        tmp_path,
        manifest.path,
        item=ledger_item(),
        expected_container_hash=parsed.snapshot,
        why="record a published entry",
    )
    before_manifest = collections.load_manifest(tmp_path, tmp_path / manifest.path)
    before_snapshot = record_formats.load_adapter(tmp_path, before_manifest).read()
    before_query = _ledger_query(tmp_path)
    before_recall = set(
        recall_policy.iter_recall_markdown(tmp_path, vault.walk_vault_md(tmp_path))
    )

    _refuse_append(tmp_path, manifest)

    held_path = _held_files(tmp_path)[0]
    after_manifest = collections.load_manifest(tmp_path, tmp_path / manifest.path)
    after_snapshot = record_formats.load_adapter(tmp_path, after_manifest).read()
    assert len(after_snapshot.records) == len(before_snapshot.records) == 1
    assert after_snapshot.snapshot == before_snapshot.snapshot
    assert [version.path for version in after_snapshot.source_versions] == [
        version.path for version in before_snapshot.source_versions
    ]
    assert after_manifest.audit_head == before_manifest.audit_head
    assert _ledger_query(tmp_path) == before_query
    assert not recall_policy.is_recall_candidate(tmp_path, held_path)
    assert (
        set(recall_policy.iter_recall_markdown(tmp_path, vault.walk_vault_md(tmp_path)))
        == before_recall
    )
    assert held_path not in before_recall


def test_the_source_census_pin_bites_when_the_same_file_sits_under_the_item_source(
    tmp_path: Path,
) -> None:
    """The pins above are only worth having if they can fail."""
    from exomem import records

    manifest = _ledger(tmp_path)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    records.append_record(
        tmp_path,
        manifest.path,
        item=ledger_item(),
        expected_container_hash=parsed.snapshot,
        why="record a published entry",
    )
    before_manifest = collections.load_manifest(tmp_path, tmp_path / manifest.path)
    before_snapshot = record_formats.load_adapter(tmp_path, before_manifest).read()
    _refuse_append(tmp_path, manifest)
    held_path = _held_files(tmp_path)[0]

    probe = (
        tmp_path
        / "Knowledge Base/Records/Publications/Entries"
        / "22222222-2222-4222-8222-222222222222.md"
    )
    probe.write_bytes(held_path.read_bytes())

    after_snapshot = record_formats.load_adapter(tmp_path, before_manifest).read()
    assert after_snapshot.snapshot != before_snapshot.snapshot
    assert len(after_snapshot.source_versions) > len(before_snapshot.source_versions)


def test_the_recall_pin_bites_for_the_same_bytes_outside_the_records_layer(
    tmp_path: Path,
) -> None:
    from exomem import recall_policy

    manifest = _ledger(tmp_path)
    _refuse_append(tmp_path, manifest)
    held_path = _held_files(tmp_path)[0]

    probe = tmp_path / "Knowledge Base" / "Notes" / "held-probe.md"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_bytes(held_path.read_bytes())

    assert recall_policy.is_recall_candidate(tmp_path, probe)
    assert not recall_policy.is_recall_candidate(tmp_path, held_path)


def test_the_whole_journey_runs_through_the_record_memory_surface(tmp_path: Path) -> None:
    """Create, append, query, refuse-and-hold, resume, all through the product command."""
    from record_fixtures import LEDGER_COLLECTION_PATH, LEDGER_MANIFEST_TEXT

    from exomem.cli_ops import OpError
    from exomem.record_memory import record_memory

    (tmp_path / "Knowledge Base").mkdir()
    (tmp_path / "Knowledge Base" / "log.md").write_text("# Activity\n", encoding="utf-8")

    record_memory(
        tmp_path,
        action="create",
        manifest_path=LEDGER_COLLECTION_PATH,
        manifest_text=LEDGER_MANIFEST_TEXT,
        why="open a publications ledger",
    )
    heads = [collections.load_manifest(tmp_path, tmp_path / LEDGER_COLLECTION_PATH).audit_head]

    exact_text = "First line.\n\nThird line."
    record_memory(
        tmp_path,
        action="append",
        collection=LEDGER_COLLECTION_PATH,
        item=ledger_item(exact_text=exact_text),
        why="record a published entry",
    )
    heads.append(collections.load_manifest(tmp_path, tmp_path / LEDGER_COLLECTION_PATH).audit_head)

    queried = json.loads(
        record_memory(tmp_path, action="query", collection=LEDGER_COLLECTION_PATH)["rendered"]
    )
    assert [row["exact_text"] for row in queried["rows"]] == [exact_text]
    coverage = record_memory(tmp_path, action="inspect", collection=LEDGER_COLLECTION_PATH)[
        "coverage"
    ]
    assert (coverage["committed"], coverage["held"]) == (1, 0)

    with pytest.raises(OpError) as raised:
        record_memory(
            tmp_path,
            action="append",
            collection=LEDGER_COLLECTION_PATH,
            item=ledger_item(slug="second-entry", unlisted_channel="digest"),
            why="record another published entry",
        )
    assert raised.value.code == "SCHEMA_UNKNOWN_FIELD"
    held_id = raised.value.details["held"]["held_id"]

    coverage = record_memory(tmp_path, action="inspect", collection=LEDGER_COLLECTION_PATH)[
        "coverage"
    ]
    assert (coverage["committed"], coverage["held"]) == (1, 1)
    assert [reference["held_id"] for reference in coverage["held_refs"]] == [held_id]

    record_memory(
        tmp_path,
        action="append",
        collection=LEDGER_COLLECTION_PATH,
        held=held_id,
        item={"unlisted_channel": None},
        why="resume the held publication entry",
    )
    heads.append(collections.load_manifest(tmp_path, tmp_path / LEDGER_COLLECTION_PATH).audit_head)

    coverage = record_memory(tmp_path, action="inspect", collection=LEDGER_COLLECTION_PATH)[
        "coverage"
    ]
    assert (coverage["committed"], coverage["held"]) == (2, 0)
    assert coverage["held_refs"] == []
    assert len(set(heads)) == 3


# --------------------------------------------------------------------------
# Review corrections (independent review round 1)
# --------------------------------------------------------------------------


_LOG_HELD_DIRECTORY = "Knowledge Base/Records/Health/X3/Held"


def _refuse_log_append(tmp_path: Path, **overrides: object):
    """Refuse one X3 (Markdown-log) append and return the error."""
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    item: dict[str, object] = {
        "occurred_on": "2026-08-03",
        "title": "Pull",
        "status": "completed",
        "movements": [{"movement": "Row", "band": "black", "repetitions": "18"}],
    }
    item.update(overrides)
    with pytest.raises(collections.CollectionError) as caught:
        records.append_record(
            tmp_path,
            manifest.path,
            item=item,
            expected_container_hash=parsed.source_versions[-1].hash,
            why="record a training session",
        )
    return caught.value


def _log_held_files(tmp_path: Path) -> list[Path]:
    directory = tmp_path / _LOG_HELD_DIRECTORY
    return sorted(directory.glob("*.md")) if directory.is_dir() else []


def test_a_child_row_carrying_the_row_delimiter_is_addressed_and_held(tmp_path: Path) -> None:
    """A grammar token is a representability failure of that strategy.

    The render layer used to raise it with empty details, outside the mutation
    boundary, so the caller learned neither the field nor kept the candidate.
    """
    error = _refuse_log_append(
        tmp_path,
        movements=[
            {"movement": "Row", "band": "black", "repetitions": "18"},
            {"movement": "Deadlift|heavy", "band": "white", "repetitions": "12"},
        ],
    )

    assert error.code == "UNREPRESENTABLE_RECORD_VALUE"
    assert error.details["field"] == "movements[1].movement"
    assert [issue["field"] for issue in error.details["issues"]] == ["movements[1].movement"]
    assert error.details["issues"][0]["received"] == "str"
    held = error.details["held"]
    assert [path.name for path in _log_held_files(tmp_path)] == [f"{held['held_id']}.md"]


def test_a_heading_value_carrying_the_heading_separator_is_addressed_and_held(
    tmp_path: Path,
) -> None:
    error = _refuse_log_append(tmp_path, title="Pull · extra")

    assert error.code == "UNREPRESENTABLE_RECORD_VALUE"
    assert error.details["field"] == "title"
    assert [issue["field"] for issue in error.details["issues"]] == ["title"]
    held = error.details["held"]
    assert [path.name for path in _log_held_files(tmp_path)] == [f"{held['held_id']}.md"]


def test_a_note_carrying_its_own_bracket_is_addressed_and_held(tmp_path: Path) -> None:
    error = _refuse_log_append(tmp_path, note="a (parenthetical)")

    assert error.code == "UNREPRESENTABLE_RECORD_VALUE"
    assert error.details["field"] == "note"
    assert [issue["field"] for issue in error.details["issues"]] == ["note"]
    held = error.details["held"]
    assert [path.name for path in _log_held_files(tmp_path)] == [f"{held['held_id']}.md"]


def test_an_empty_heading_value_is_addressed_and_held(tmp_path: Path) -> None:
    """Emptiness is a representability failure of the log strategy like a token.

    The render layer refuses an empty heading string; judging it in the
    validation pass names the field and lets the refusal hold the candidate.
    """
    error = _refuse_log_append(tmp_path, title="")

    assert error.code == "UNREPRESENTABLE_RECORD_VALUE"
    assert error.details["field"] == "title"
    assert [issue["field"] for issue in error.details["issues"]] == ["title"]
    held = error.details["held"]
    assert [path.name for path in _log_held_files(tmp_path)] == [f"{held['held_id']}.md"]


def test_a_whitespace_only_note_is_addressed_and_held(tmp_path: Path) -> None:
    error = _refuse_log_append(tmp_path, note="   ")

    assert error.code == "UNREPRESENTABLE_RECORD_VALUE"
    assert error.details["field"] == "note"
    assert [issue["field"] for issue in error.details["issues"]] == ["note"]
    held = error.details["held"]
    assert [path.name for path in _log_held_files(tmp_path)] == [f"{held['held_id']}.md"]


def test_the_render_grammar_checks_remain_as_the_last_line_of_defence(tmp_path: Path) -> None:
    """The validator fires first; the render checks still refuse if reached."""
    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    unreachable = {
        "child row cannot render": {
            "occurred_on": "2026-08-03",
            "title": "Pull",
            "status": "completed",
            "movements": [{"movement": "Deadlift|heavy", "band": "white", "repetitions": "12"}],
        },
        "heading value cannot render": {
            "occurred_on": "2026-08-03",
            "title": "Pull · extra",
            "status": "completed",
            "movements": [],
        },
        "note cannot render": {
            "occurred_on": "2026-08-03",
            "title": "Pull",
            "status": "completed",
            "note": "a (parenthetical)",
            "movements": [],
        },
    }
    for reason, values in unreachable.items():
        with pytest.raises(collections.CollectionError) as caught:
            record_formats.render_markdown_log_item(
                manifest, values, "00000000-0000-4000-8000-000000000000", "\n"
            )
        assert caught.value.code == "UNREPRESENTABLE_RECORD_VALUE"
        assert caught.value.reason == reason


def test_a_whole_candidate_round_trip_failure_reports_the_frontmatter_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No field is at fault when the whole block fails, so none is named."""
    from exomem import records, vault

    manifest = _ledger(tmp_path)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()

    monkeypatch.setattr(vault, "serialize_frontmatter", lambda frontmatter: "broken: [unclosed")

    with pytest.raises(collections.CollectionError) as caught:
        records.append_record(
            tmp_path,
            manifest.path,
            item=ledger_item(),
            expected_container_hash=parsed.snapshot,
            why="record a published entry",
            hold=False,
        )

    error = caught.value
    assert error.code == "UNREPRESENTABLE_RECORD_VALUE"
    assert "field" not in error.details
    assert error.details["scope"] == "frontmatter"
    assert [issue["scope"] for issue in error.details["issues"]] == ["frontmatter"]
    assert all("field" not in issue for issue in error.details["issues"])


def test_a_non_finite_number_is_held_and_resumes(tmp_path: Path) -> None:
    """`json.dumps` cannot carry NaN, so the held body tags it instead."""
    from exomem import records

    manifest = _ledger(tmp_path)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()

    with pytest.raises(collections.CollectionError) as caught:
        records.append_record(
            tmp_path,
            manifest.path,
            item=ledger_item(metrics=[{"name": "shares", "value": float("nan")}]),
            expected_container_hash=parsed.snapshot,
            why="record a published entry",
        )

    error = caught.value
    assert error.code == "SCHEMA_FIELD_TYPE"
    assert error.details["field"] == "metrics[0].value"
    assert "warnings" not in error.details
    held_id = error.details["held"]["held_id"]
    files = _held_files(tmp_path)
    assert len(files) == 1
    candidate = _held_payload(files[0])["candidate"]
    assert candidate["item"]["metrics"][0]["value"] == {"__float__": "NaN"}

    # Resuming without an override restores the very value that refused, so the
    # candidate is preserved rather than silently repaired.
    with pytest.raises(collections.CollectionError) as again:
        records.append_record(
            tmp_path,
            manifest.path,
            held=held_id,
            why="resume the held publication entry",
        )
    assert again.value.code == "SCHEMA_FIELD_TYPE"
    assert again.value.details["field"] == "metrics[0].value"
    assert again.value.details["held"]["held_id"] == held_id

    committed = records.append_record(
        tmp_path,
        manifest.path,
        held=held_id,
        item={"metrics": [{"name": "shares", "value": 12}]},
        why="resume the held publication entry",
    )
    assert committed["outcome"] == "committed"
    assert _held_files(tmp_path) == []


@pytest.mark.parametrize(
    "literal",
    [
        {"__float__": "NaN"},
        {"__escaped__": {"__float__": "Infinity"}},
        {"__escaped__": 1},
    ],
    ids=["float-tag", "escape-tag", "escape-scalar"],
)
def test_a_literal_tag_shaped_object_survives_the_held_round_trip(
    tmp_path: Path, literal: dict[str, object]
) -> None:
    """The held body is lossless by construction, so its own tags must be escaped.

    An object field may legitimately carry the exact shape the encoder uses for
    non-finite floats; without escaping, resume would restore it as a float and
    blame a field the caller wrote correctly.
    """
    from exomem import records

    manifest = _ledger(tmp_path)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()

    with pytest.raises(collections.CollectionError) as caught:
        records.append_record(
            tmp_path,
            manifest.path,
            item=ledger_item(details=literal, word_count="bad"),
            expected_container_hash=parsed.snapshot,
            why="record a published entry",
        )
    error = caught.value
    assert error.code == "SCHEMA_FIELD_TYPE"
    assert error.details["field"] == "word_count"
    held_id = error.details["held"]["held_id"]
    stored = _held_payload(_held_files(tmp_path)[0])["candidate"]["item"]["details"]
    assert stored != literal, "a tag-shaped literal must be escaped in the held body"

    committed = records.append_record(
        tmp_path,
        manifest.path,
        held=held_id,
        item={"word_count": 5},
        why="resume the held publication entry",
    )
    assert committed["outcome"] == "committed"
    assert _held_files(tmp_path) == []
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    (record,) = [row for row in snapshot.records if row.values.get("word_count") == 5]
    assert record.values["details"] == literal


def test_two_candidates_sharing_a_natural_key_re_hold_onto_one_file(tmp_path: Path) -> None:
    """Held identity is the item identity, so one blocked item is one file."""
    from exomem import records

    manifest = _ledger(tmp_path)
    parsed = record_formats.load_adapter(tmp_path, manifest).read()

    def refuse(word_count: int):
        with pytest.raises(collections.CollectionError) as caught:
            records.append_record(
                tmp_path,
                manifest.path,
                item=ledger_item(word_count=word_count, unlisted_channel="digest"),
                expected_container_hash=parsed.snapshot,
                why="record a published entry",
            )
        return caught.value

    first = refuse(42)
    second = refuse(99)

    assert first.details["held"]["held_id"] == second.details["held"]["held_id"]
    files = _held_files(tmp_path)
    assert len(files) == 1
    assert _held_payload(files[0])["candidate"]["item"]["word_count"] == 99


def _discard_receipt(tmp_path: Path) -> dict[str, object]:
    from exomem import records

    manifest = _ledger(tmp_path)
    error = _refuse_append(tmp_path, manifest)
    return records.discard_held(
        tmp_path,
        manifest.path,
        held=error.details["held"]["held_id"],
        why="the observation was a duplicate",
    )


def test_a_discard_receipt_is_a_valid_record_receipt(tmp_path: Path) -> None:
    """A receipt no validator accepts is dropped by every consumer downstream."""
    from exomem import mutation_terminal

    receipt = _discard_receipt(tmp_path)

    assert mutation_terminal.valid_record_receipt(receipt) is True
    assert mutation_terminal.valid_collection_receipt(receipt) is True


def test_the_compact_terminal_projects_a_discard_receipt(tmp_path: Path) -> None:
    from exomem import mutation_terminal

    receipt = _discard_receipt(tmp_path)

    terminal = mutation_terminal.committed_terminal(
        receipt,
        request_id="req-discard",
        receipt_id=None,
        idempotency_key=None,
    )
    compact = mutation_terminal.project_terminal(terminal, "compact")

    assert compact["operation"] == "discard"
    assert compact["outcome"] == "discarded"
    assert compact["affected_paths"] == receipt["affected_paths"]
    assert compact["paths"] == receipt["affected_paths"]


def test_governance_does_not_withhold_a_discard_receipt(tmp_path: Path) -> None:
    from exomem import record_governance

    receipt = _discard_receipt(tmp_path)

    projected = record_governance.project_mutation_receipt(receipt)

    assert "withheld" not in projected
    assert projected["operation"] == "discard"
    assert projected["outcome"] == "discarded"
    assert projected["affected_paths"] == receipt["affected_paths"]


def test_writer_lease_treats_a_discard_receipt_as_a_receipt(tmp_path: Path) -> None:
    """`with_graph_outcome` must not smear graph state over a real receipt."""
    from exomem import writer_lease

    receipt = _discard_receipt(tmp_path)

    assert writer_lease.valid_collection_receipt(receipt) is True
