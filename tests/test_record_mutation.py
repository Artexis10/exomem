from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from record_fixtures import copy_dataset_fixture, copy_vehicle_maintenance_fixture, copy_x3_fixture
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
