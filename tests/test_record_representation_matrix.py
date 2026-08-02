from __future__ import annotations

import json
from pathlib import Path

import pytest
from record_fixtures import copy_vehicle_maintenance_fixture, copy_x3_fixture

from exomem import record_formats, records, writer_lease
from exomem import structured_collections as collections


@pytest.fixture(autouse=True)
def _isolated_writer_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state"))
    writer_lease.reset_managers_for_tests()
    yield
    writer_lease.reset_managers_for_tests()


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


def test_markdown_item_update_preserves_complex_frontmatter_bytes() -> None:
    source = (
        "\ufeff---\r\n"
        "type: record\r\n"
        "collection_id: 11111111-1111-4111-8111-111111111111\r\n"
        "record_id: 22222222-2222-4222-8222-222222222222\r\n"
        "schema_version: 1\r\n"
        "services: [oil change, tyre rotation] # keep flow comment\r\n"
        "details:\r\n"
        "  provider: Northside # keep nested comment\r\n"
        "  parts: [filter, gasket]\r\n"
        'note: "---"\r\n'
        "# exomem-record-audit: 0123456789abcdef01234567\r\n"
        "---\r\n"
        "Readable body with no final newline."
    )

    updated = record_formats.render_markdown_item_update(
        source,
        {"services": ["brake inspection"], "next_due_on": None},
        "fedcba9876543210fedcba98",
    )

    assert updated.startswith("\ufeff---\r\n")
    assert (
        "details:\r\n  provider: Northside # keep nested comment\r\n  parts: [filter, gasket]\r\n"
        in updated
    )
    assert 'note: "---"\r\n' in updated
    assert "services: [brake inspection] # keep flow comment\r\n" in updated
    assert "next_due_on:\r\n" in updated
    assert updated.count("exomem-record-audit:") == 1
    assert updated.endswith("Readable body with no final newline.")


def test_items_snapshot_includes_unexpected_regular_files_and_missing_directory_differs(
    tmp_path: Path,
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    adapter = record_formats.load_adapter(tmp_path, manifest)
    before = adapter.read().snapshot

    (fixture / "Events" / "human-notes.txt").write_text("ordinary note\n", encoding="utf-8")
    assert record_formats.load_adapter(tmp_path, manifest).read().snapshot != before

    events = fixture / "Events"
    for path in sorted(events.rglob("*"), reverse=True):
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            path.rmdir()
    assert record_formats.load_adapter(tmp_path, manifest).read().records == ()
    events.rmdir()
    with pytest.raises(collections.CollectionError, match="SOURCE_NOT_FOUND"):
        record_formats.load_adapter(tmp_path, manifest).read()


def test_append_replay_returns_original_correlation_without_new_event(tmp_path: Path) -> None:
    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    before = record_formats.load_adapter(tmp_path, manifest).read()
    item = _item("2026-08-03", "Pull")
    key = "abababab-abab-4aba-8aba-abababababab"

    first = records.append_record(
        tmp_path,
        manifest.path,
        item=item,
        item_key=key,
        expected_container_hash=before.source_versions[-1].hash,
        why="record replay correlation",
    )
    log_before = (tmp_path / "Knowledge Base/log.md").read_bytes()
    replay = records.append_record(
        tmp_path,
        manifest.path,
        item=item,
        item_key=key,
        expected_container_hash=first["after_container_hash"],
        why="record replay correlation",
    )

    assert replay["outcome"] == "replayed"
    assert replay["audit_correlation"] == first["audit_correlation"]
    assert (tmp_path / "Knowledge Base/log.md").read_bytes() == log_before


def test_item_update_keeps_one_current_marker_for_distinct_same_value_transition(
    tmp_path: Path,
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    before = record_formats.load_adapter(tmp_path, manifest).read()
    key = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    first = records.append_record(
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
        why="record a maintenance event",
        body="Readable body without a final newline.",
    )
    refreshed = collections.load_manifest(tmp_path, fixture / "_collection.md")
    record = next(
        item
        for item in record_formats.load_adapter(tmp_path, refreshed).read().records
        if item.identity.key == key
    )
    second = records.update_record(
        tmp_path,
        refreshed.path,
        item_key=key,
        changes={"status": "completed"},
        expected_container_hash=first["after_container_hash"],
        expected_item_version=record.source.hash,
        why="confirm the recorded status",
    )

    item_path = tmp_path / record.source.path
    assert item_path.read_text(encoding="utf-8").count("exomem-record-audit:") == 1
    assert first["audit_correlation"] != second["audit_correlation"]
    assert records.inspect_audit_gap(tmp_path, refreshed.path)["status"] == "ok"


def test_exact_byte_aba_restore_is_baseline_not_tamper_evidence(tmp_path: Path) -> None:
    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    source = fixture / "Training Log.md"
    log = tmp_path / "Knowledge Base/log.md"
    before = (source.read_bytes(), (fixture / "_collection.md").read_bytes(), log.read_bytes())
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    records.append_record(
        tmp_path,
        manifest.path,
        item=_item("2026-08-03", "Pull"),
        item_key="ffffffff-ffff-4fff-8fff-ffffffffffff",
        expected_container_hash=snapshot.source_versions[-1].hash,
        why="exercise the documented ABA limit",
    )
    source.write_bytes(before[0])
    (fixture / "_collection.md").write_bytes(before[1])
    log.write_bytes(before[2])

    assert records.inspect_audit_gap(tmp_path, manifest.path)["status"] == "baseline"


@pytest.mark.parametrize("field", ["source_path", "canonical_path", "item_key"])
def test_audit_marker_requires_matching_event_evidence(tmp_path: Path, field: str) -> None:
    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    result = records.append_record(
        tmp_path,
        manifest.path,
        item=_item("2026-08-03", "Pull"),
        item_key="cdcdcdcd-cdcd-4cdc-8cdc-cdcdcdcdcdcd",
        expected_container_hash=snapshot.source_versions[-1].hash,
        why="record audit evidence",
    )
    log = tmp_path / "Knowledge Base/log.md"
    lines = log.read_text(encoding="utf-8").splitlines()
    event_index = next(index for index, line in enumerate(lines) if "audit-v1" in line)
    event = json.loads(lines[event_index].removeprefix("Records audit-v1 "))
    event[field] = (
        "Knowledge Base/Records/Other.md"
        if field != "item_key"
        else "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    )
    lines[event_index] = "Records audit-v1 " + json.dumps(event, sort_keys=True)
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = records.inspect_audit_gap(tmp_path, manifest.path)
    assert result["audit_correlation"] is not None
    assert report["status"] == "gap"


def test_audit_chain_reconstructs_rotations_and_rejects_fork(tmp_path: Path) -> None:
    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    first_snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    first = records.append_record(
        tmp_path,
        manifest.path,
        item=_item("2026-08-03", "Pull"),
        item_key="11111111-aaaa-4111-8111-111111111111",
        expected_container_hash=first_snapshot.source_versions[-1].hash,
        why="record first rotated event",
    )
    current = collections.load_manifest(tmp_path, fixture / "_collection.md")
    records.append_record(
        tmp_path,
        current.path,
        item=_item("2026-08-04", "Push"),
        item_key="22222222-aaaa-4222-8222-222222222222",
        expected_container_hash=first["after_container_hash"],
        why="record second rotated event",
    )
    log = tmp_path / "Knowledge Base/log.md"
    events = [line for line in log.read_text(encoding="utf-8").splitlines() if "audit-v1" in line]
    archive = tmp_path / "Knowledge Base/_archive/logs"
    archive.mkdir(parents=True)
    (archive / "log-z.md").write_text("\n".join(reversed(events)) + "\n", encoding="utf-8")
    (archive / "log-a.md").write_text(events[0] + "\n", encoding="utf-8")
    log.write_text("# Activity\n", encoding="utf-8")
    assert records.inspect_audit_gap(tmp_path, current.path)["status"] == "ok"

    fork = json.loads(events[0].removeprefix("Records audit-v1 "))
    fork["transition_id"] = "999999999999999999999999"
    fork["after_container_hash"] = "0" * 64
    (archive / "log-fork.md").write_text(
        "Records audit-v1 " + json.dumps(fork, sort_keys=True) + "\n", encoding="utf-8"
    )
    assert records.inspect_audit_gap(tmp_path, current.path)["status"] == "gap"


def test_markdown_item_snapshot_includes_empty_nested_directories(tmp_path: Path) -> None:
    """A directory-only out-of-band change is part of the canonical item inventory."""
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    before = record_formats.load_adapter(tmp_path, manifest).read()
    (fixture / "Events" / "released" / "a" / "b").mkdir(parents=True)
    after = record_formats.load_adapter(tmp_path, manifest).read()
    assert after.snapshot != before.snapshot


def test_next_mutation_replaces_human_formatted_audit_mapping(tmp_path: Path) -> None:
    fixture = copy_x3_fixture(tmp_path)
    _activity_log(tmp_path)
    manifest_path = fixture / "_collection.md"
    manifest = collections.load_manifest(tmp_path, manifest_path)
    initial = record_formats.load_adapter(tmp_path, manifest).read()
    first = records.append_record(
        tmp_path,
        manifest.path,
        item=_item("2026-08-03", "Pull"),
        item_key="98989898-aaaa-4989-8989-989898989898",
        expected_container_hash=initial.source_versions[-1].hash,
        why="establish formatted audit mapping",
    )
    text = manifest_path.read_text(encoding="utf-8")
    text = text.replace(
        f"record_audit: {{version: 1, head: {first['audit_correlation']}}}",
        f"record_audit:\n  version: 1\n  head: {first['audit_correlation']}",
    )
    manifest_path.write_text(text, encoding="utf-8")
    refreshed = collections.load_manifest(tmp_path, manifest_path)
    records.append_record(
        tmp_path,
        refreshed.path,
        item=_item("2026-08-04", "Push"),
        item_key="79797979-aaaa-4979-8979-979797979797",
        expected_container_hash=first["after_container_hash"],
        why="replace formatted mapping",
    )
    assert "record_audit: {version: 1, head:" in manifest_path.read_text(encoding="utf-8")
