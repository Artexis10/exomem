from __future__ import annotations

from pathlib import Path

import pytest
from record_fixtures import (
    copy_dataset_fixture,
    copy_vehicle_maintenance_fixture,
    copy_x3_fixture,
)

from exomem import record_formats
from exomem import structured_collections as collections


def _manifest(vault: Path, fixture: Path) -> collections.CollectionManifest:
    return collections.load_manifest(vault, fixture / "_collection.md")


def test_markdown_log_adapter_uses_declared_fence_aware_grammar_and_exact_spans(
    tmp_path: Path,
) -> None:
    fixture = copy_x3_fixture(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    source = (fixture / "Training Log.md").read_bytes()

    adapter = record_formats.load_adapter(tmp_path, manifest)
    parsed = adapter.read()

    assert adapter.mutable is True
    assert len(parsed.records) == 6
    assert [record.values["occurred_on"] for record in parsed.records] == [
        "2026-08-02",
        "2026-06-25",
        "2026-06-24",
        "2026-06-08",
        "2026-06-04",
        "2026-06-03",
    ]
    assert parsed.records[0].identity.inferred is True
    assert source[parsed.records[0].span.start : parsed.records[0].span.end].startswith(
        b"### 2026-08-02 \xc2\xb7 Push\n"
    )
    assert all(record.ambiguous is False for record in parsed.records)
    assert [child.values["movement"] for child in parsed.records[0].children[:2]] == [
        "Overhead Press",
        "Split Squat L",
    ]
    assert parsed.insertion_offset == source.index(b"### 2026-08-02")
    assert (fixture / "Training Log.md").read_bytes() == source


def test_markdown_log_query_expands_declared_child_rows_without_domain_logic(
    tmp_path: Path,
) -> None:
    fixture = copy_x3_fixture(tmp_path)
    manifest = _manifest(tmp_path, fixture)

    result = record_formats.query_collection(
        tmp_path,
        manifest,
        expand_children=True,
        sort_by="movement",
        limit=20,
    )

    assert result.returned == 20
    assert {row["repetitions"] for row in result.rows}.issuperset({"", "8", "15", "21"})
    assert all(row["parent_record_id"] == row["record_id"] for row in result.rows)


def test_markdown_log_identity_survives_manual_reorder_and_date_correction(tmp_path: Path) -> None:
    fixture = copy_x3_fixture(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    log = fixture / "Training Log.md"
    log.write_text(
        log.read_text(encoding="utf-8").replace(
            "### 2026-08-02 · Push\n",
            "### 2026-08-02 · Push\n<!-- exomem-record-id: 14d2bdca-e145-425b-9e4b-df86f7172efa -->\n",
        ),
        encoding="utf-8",
    )
    original = record_formats.load_adapter(tmp_path, manifest).read().records[0]

    text = log.read_text(encoding="utf-8")
    text = text.replace("2026-08-02 · Push", "2026-08-03 · Push")
    start = text.index("### 2026-08-03 · Push")
    end = text.index("### 2026-06-25 · Push")
    block = text[start:end]
    text = text[:start] + text[end:]
    insert = text.index("### 2026-06-24 · Pull")
    log.write_text(text[:insert] + block + text[insert:], encoding="utf-8")

    changed = record_formats.load_adapter(tmp_path, manifest).read().records
    same = next(record for record in changed if record.identity.key == original.identity.key)
    assert same.values["occurred_on"] == "2026-08-03"
    assert same.source.hash != original.source.hash


def test_markdown_log_adapter_honors_declared_oldest_first_insertion_edge(tmp_path: Path) -> None:
    fixture = copy_x3_fixture(tmp_path)
    manifest_path = fixture / "_collection.md"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            "insertion: newest-first", "insertion: oldest-first"
        ),
        encoding="utf-8",
    )
    manifest = _manifest(tmp_path, fixture)
    source = (fixture / "Training Log.md").read_bytes()

    parsed = record_formats.load_adapter(tmp_path, manifest).read()

    assert parsed.insertion_offset == source.index(b"## Current bands")


def test_markdown_item_adapter_uses_file_identity_exact_version_and_readable_body(
    tmp_path: Path,
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = _manifest(tmp_path, fixture)

    adapter = record_formats.load_adapter(tmp_path, manifest)
    parsed = adapter.read()

    assert adapter.mutable is True
    assert len(parsed.records) == 3
    released = next(
        record for record in parsed.records if record.identity.key.startswith("a8d391a5")
    )
    assert released.identity.collection_id == manifest.collection_id
    assert released.values["services"] == ["oil change", "filter replacement"]
    assert released.body == "Directly corrected odometer remains human-editable.\n"
    assert released.source.hash
    assert released.span.start == 0
    assert released.span.end == len((fixture / "Events/released/2026-06-01-oil.md").read_bytes())

    bom_item = next(
        record for record in parsed.records if record.identity.key.startswith("14d2bdca")
    )
    assert (fixture / "Events/released/2026-06-02-bom.md").read_bytes().startswith(b"\xef\xbb\xbf")
    assert bom_item.body == "BOM-bearing item body remains readable.\n"


def test_markdown_item_inspection_reports_undeclared_manual_frontmatter_field(
    tmp_path: Path,
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    item = fixture / "Events/released/2026-06-01-oil.md"
    item.write_text(
        item.read_text(encoding="utf-8").replace(
            "schema_version: 1\n", "schema_version: 1\nundeclared_secret: surprise\n"
        ),
        encoding="utf-8",
    )
    before = item.read_bytes()

    inspected = record_formats.inspect_collection(tmp_path, manifest)

    assert [diagnostic.code for diagnostic in inspected.diagnostics] == ["SCHEMA_UNKNOWN_FIELD"]
    assert item.read_bytes() == before


def test_markdown_log_inspection_rejects_undeclared_descriptor_default(tmp_path: Path) -> None:
    fixture = copy_x3_fixture(tmp_path)
    manifest_path = fixture / "_collection.md"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            "  defaults:\n    status: completed\n",
            "  defaults:\n    status: completed\n    undeclared_default: true\n",
        ),
        encoding="utf-8",
    )
    manifest = _manifest(tmp_path, fixture)
    source = fixture / "Training Log.md"
    before = source.read_bytes()

    inspected = record_formats.inspect_collection(tmp_path, manifest)

    assert [diagnostic.code for diagnostic in inspected.diagnostics] == ["SCHEMA_UNKNOWN_FIELD"]
    assert source.read_bytes() == before


def test_dataset_adapter_is_query_only_and_exposes_declared_keys_and_snapshot(
    tmp_path: Path,
) -> None:
    fixture = copy_dataset_fixture(tmp_path)
    manifest = _manifest(tmp_path, fixture)
    adapter = record_formats.load_adapter(tmp_path, manifest)

    parsed = adapter.read()

    assert len(parsed.records) == 72
    assert parsed.records[0].identity.key == "r-001"
    assert parsed.records[0].source.hash != parsed.snapshot
    assert adapter.mutable is False
    with pytest.raises(collections.CollectionError) as excinfo:
        adapter.refuse_mutation("append")
    assert excinfo.value.code == "UNSUPPORTED_RECORD_MUTATION"


def test_dataset_adapter_uses_declared_json_record_path(tmp_path: Path) -> None:
    collection = tmp_path / "Knowledge Base/Records/JSON"
    collection.mkdir(parents=True)
    (collection / "readings.json").write_text(
        '{"payload":{"readings":[{"reading_id":"json-1","value":1}]}}', encoding="utf-8"
    )
    (collection / "_collection.md").write_text(
        """---
type: collection
exomem_id: 4b90b34f-319f-4d8d-8c92-e45124501870
title: JSON readings
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: dataset
  source: readings.json
  format_version: 1
  record_path: payload.readings
  key: reading_id
item_schema:
  natural_key: [reading_id]
  fields:
    reading_id:
      type: string
      required: true
    value:
      type: number
      required: true
---
""",
        encoding="utf-8",
    )
    manifest = collections.load_manifest(tmp_path, collection / "_collection.md")

    parsed = record_formats.load_adapter(tmp_path, manifest).read()

    assert [record.identity.key for record in parsed.records] == ["json-1"]


def test_query_collection_has_bounded_snapshot_continuations_and_derived_renderers(
    tmp_path: Path,
) -> None:
    fixture = copy_dataset_fixture(tmp_path)
    manifest = _manifest(tmp_path, fixture)

    first = record_formats.query_collection(
        tmp_path,
        manifest,
        sort_by="reading_id",
        limit=2,
        output_format="markdown",
    )
    assert first.returned == 2
    assert first.truncated is True
    assert first.continuation
    assert first.derived is True
    assert manifest.collection_id in first.rendered
    assert "derived: true" in first.rendered

    second = record_formats.query_collection(
        tmp_path,
        manifest,
        sort_by="reading_id",
        limit=2,
        continuation=first.continuation,
        output_format="csv",
    )
    assert [row["reading_id"] for row in second.rows] == ["r-003", "r-004"]
    assert second.rendered.startswith("# collection_id:")

    source = fixture / "readings.csv"
    source.write_text(
        source.read_text(encoding="utf-8") + "r-999,2026-12-01,water,99\n", encoding="utf-8"
    )
    with pytest.raises(collections.CollectionError) as excinfo:
        record_formats.query_collection(
            tmp_path,
            manifest,
            sort_by="reading_id",
            limit=2,
            continuation=first.continuation,
        )
    assert excinfo.value.code == "STALE_RECORD_SNAPSHOT"
