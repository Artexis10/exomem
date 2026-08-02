from __future__ import annotations

import json
from pathlib import Path

import pytest
from record_fixtures import copy_vehicle_maintenance_fixture, copy_x3_fixture

from exomem import mutation_terminal, record_formats
from exomem import structured_collections as collections


def test_markdown_items_refuse_a_symlinked_source_directory(tmp_path: Path) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    events = fixture / "Events"
    outside = tmp_path / "outside-events"
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    events.rename(outside)
    try:
        events.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(
        collections.CollectionError, match="SOURCE_NOT_FOUND|INVALID_RECORD_ITEM_PATH"
    ):
        record_formats.load_adapter(tmp_path, manifest).read()


def test_markdown_items_count_empty_directories_against_the_global_cap(tmp_path: Path) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    for index in range(2_001):
        (fixture / "Events" / f"empty-{index:04d}").mkdir()

    with pytest.raises(collections.CollectionError, match="RECORD_ITEM_LIMIT"):
        record_formats.load_adapter(tmp_path, manifest).read()


def test_manifest_audit_head_is_semantic_and_renderer_preserves_its_style(tmp_path: Path) -> None:
    source = """---
# retain this comment
record_audit: {head: 0123456789abcdef01234567, version: 1} # retain this too
type: collection
---
"""

    rendered = record_formats.render_manifest_audit_head(source, "fedcba9876543210fedcba98")

    assert "# retain this comment" in rendered
    assert "# retain this too" in rendered
    assert "record_audit: {head: fedcba9876543210fedcba98, version: 1}" in rendered


def test_log_create_receipt_with_item_hash_is_projected_but_malformed_create_is_not() -> None:
    receipt = {
        "_record_receipt": "exomem.records-mutation",
        "receipt_version": 1,
        "operation": "create",
        "collection_id": "11111111-1111-4111-8111-111111111111",
        "item_key": None,
        "before_item_hash": None,
        "after_item_hash": None,
        "before_container_hash": None,
        "after_container_hash": "a" * 64,
        "affected_paths": ["Knowledge Base/Records/example/_collection.md"],
        "payload_hash": None,
        "outcome": "committed",
        "audit_correlation": "b" * 24,
    }
    terminal = mutation_terminal.committed_terminal(
        receipt,
        request_id="11111111-1111-4111-8111-111111111111",
        receipt_id=None,
        idempotency_key=None,
    )

    assert mutation_terminal.project_terminal(terminal)["operation"] == "create"
    log_create = dict(receipt, after_item_hash="c" * 64)
    log_terminal = mutation_terminal.committed_terminal(
        log_create,
        request_id="11111111-1111-4111-8111-111111111111",
        receipt_id=None,
        idempotency_key=None,
    )
    assert mutation_terminal.project_terminal(log_terminal)["after_item_hash"] == "c" * 64
    malformed = dict(receipt, after_item_hash="not-a-hash")
    malformed_terminal = mutation_terminal.committed_terminal(
        malformed,
        request_id="11111111-1111-4111-8111-111111111111",
        receipt_id=None,
        idempotency_key=None,
    )
    assert "operation" not in mutation_terminal.project_terminal(malformed_terminal)


def test_audit_detects_a_descendant_left_after_manifest_and_source_rollback(tmp_path: Path) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    (tmp_path / "Knowledge Base/log.md").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "Knowledge Base/log.md").write_text("# Activity\n", encoding="utf-8")
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    initial = record_formats.load_adapter(tmp_path, manifest).read()
    item = {"occurred_on": "2026-08-03", "title": "Pull", "status": "completed", "movements": []}
    first = records.append_record(
        tmp_path,
        manifest.path,
        item=item,
        item_key="11111111-1111-4111-8111-111111111111",
        expected_container_hash=initial.source_versions[-1].hash,
        why="first transition",
    )
    source, manifest_path = fixture / "Training Log.md", fixture / "_collection.md"
    first_bytes = (source.read_bytes(), manifest_path.read_bytes())
    current = collections.load_manifest(tmp_path, manifest_path)
    records.append_record(
        tmp_path,
        current.path,
        item=dict(item, occurred_on="2026-08-04"),
        item_key="22222222-2222-4222-8222-222222222222",
        expected_container_hash=first["after_container_hash"],
        why="second transition",
    )
    source.write_bytes(first_bytes[0])
    manifest_path.write_bytes(first_bytes[1])

    report = records.inspect_audit_gap(tmp_path, manifest_path)

    assert report["status"] == "gap"
    assert any(gap.startswith("unreachable-transition:") for gap in report["gaps"])


def test_audit_rejects_operation_relabel_and_global_transition_conflict(tmp_path: Path) -> None:
    from exomem import records

    fixture = copy_x3_fixture(tmp_path)
    log = tmp_path / "Knowledge Base/log.md"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("# Activity\n", encoding="utf-8")
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    records.append_record(
        tmp_path,
        manifest.path,
        item={"occurred_on": "2026-08-03", "title": "Pull", "status": "completed", "movements": []},
        item_key="33333333-3333-4333-8333-333333333333",
        expected_container_hash=snapshot.source_versions[-1].hash,
        why="audit fixture",
    )
    lines = log.read_text(encoding="utf-8").splitlines()
    index = next(i for i, line in enumerate(lines) if "audit-v1" in line)
    event = json.loads(lines[index].removeprefix("Records audit-v1 "))
    event["operation"] = "update"
    lines[index] = "Records audit-v1 " + json.dumps(event)
    conflict = dict(event, collection_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", operation="append")
    lines.append("Records audit-v1 " + json.dumps(conflict))
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert records.inspect_audit_gap(tmp_path, manifest.path)["status"] == "gap"
