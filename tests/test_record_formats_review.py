from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from record_fixtures import copy_dataset_fixture, copy_vehicle_maintenance_fixture

from exomem import query_data, record_formats
from exomem import structured_collections as collections

COLLECTION_ID = "9ba8d1cf-d1e7-4309-95ae-cb28d7a6eea8"


def test_markdown_items_snapshot_binds_authorized_non_record_markdown(tmp_path: Path) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    readme = fixture / "Events" / "README.md"
    readme.write_text("human context", encoding="utf-8")

    first = record_formats.load_adapter(tmp_path, manifest).read()
    readme.write_text("direct edit", encoding="utf-8")
    second = record_formats.load_adapter(tmp_path, manifest).read()

    relative = readme.relative_to(tmp_path).as_posix()
    assert relative in {path for path, _kind, _digest in first.source_inventory}
    assert first.snapshot != second.snapshot


def test_markdown_items_hides_candidates_from_public_caps_but_keeps_authorized_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    events = fixture / "Events"
    hidden = events / "hidden"
    hidden.mkdir()
    for number in range(4):
        (hidden / f"{number}.bin").write_bytes(b"hidden")
    readme = events / "README.md"
    readme.write_text("context", encoding="utf-8")
    monkeypatch.setattr(record_formats, "_MAX_ITEM_FILES", 4)
    monkeypatch.setattr(record_formats, "_MAX_RAW_ITEM_ENTRIES", 16)

    def authorized(path: str) -> bool:
        return not {"hidden", "withheld"}.intersection(path.split("/"))

    first = record_formats.load_adapter(tmp_path, manifest, authorize_path=authorized).read()
    assert len(first.records) == 2
    assert readme.relative_to(tmp_path).as_posix() in {
        path for path, _kind, _digest in first.source_inventory
    }
    assert not any("/hidden/" in f"/{path}" for path, _kind, _digest in first.source_inventory)

    readme.write_text("direct edit", encoding="utf-8")
    second = record_formats.load_adapter(tmp_path, manifest, authorize_path=authorized).read()
    assert second.snapshot != first.snapshot

    (events / "authorized.bin").write_bytes(b"public")
    with pytest.raises(collections.CollectionError, match="too many item files"):
        record_formats.load_adapter(tmp_path, manifest, authorize_path=authorized).read()


def test_markdown_items_raw_ceiling_is_independent_of_public_file_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = copy_vehicle_maintenance_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    monkeypatch.setattr(record_formats, "_MAX_RAW_ITEM_ENTRIES", 2)

    with pytest.raises(collections.CollectionError, match="too many item entries"):
        record_formats.load_adapter(
            tmp_path, manifest, authorize_path=lambda path: path == manifest.storage.source
        ).read()


def test_hidden_link_field_does_not_remove_dataset_row_from_identity_or_query(
    tmp_path: Path,
) -> None:
    fixture = copy_dataset_fixture(tmp_path)
    manifest = collections.load_manifest(tmp_path, fixture / "_collection.md")
    target = fixture / "readings.csv"
    target.write_text(
        target.read_text(encoding="utf-8").replace("electricity", "[[Evidence/Secret]]", 1),
        encoding="utf-8",
    )
    manifest_path = fixture / "_collection.md"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8")
        .replace("  key: reading_id\n", "")
        .replace("natural_key: [reading_id]", "natural_key: [category]")
        .replace("    category:\n      type: string", "    category:\n      type: link"),
        encoding="utf-8",
    )
    manifest = collections.load_manifest(tmp_path, manifest_path)

    result = record_formats.query_collection(
        tmp_path,
        manifest,
        project_values=lambda values: {
            key: value
            for key, value in values.items()
            if not (key == "category" and value == "[[Evidence/Secret]]")
        },
    )

    assert result.total_matched == 72
    assert len(result.rows) == 72
    assert sum("category" not in row for row in result.rows) == 1


def _log_manifest() -> str:
    return f"""---
type: collection
exomem_id: {COLLECTION_ID}
title: Generic sessions
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-log
  source: log.md
  format_version: 1
  section:
    level: 2
    title: Sessions
  item_heading:
    level: 3
    fields:
      - name: occurred_on
        type: date
        format: "%Y-%m-%d"
      - name: title
        type: string
    separator: " · "
    note:
      field: note
      open: " ("
      close: ")"
  insertion: newest-first
  child_rows:
    prefix: "- "
    delimiter: "|"
    fields: [movement, band, repetitions]
    container_field: movements
item_schema:
  natural_key: [occurred_on, title]
  fields:
    occurred_on:
      type: date
      required: true
    title:
      type: string
      required: true
    movements:
      type: array
      items:
        type: object
---
"""


def _log_collection(tmp_path: Path) -> collections.CollectionManifest:
    collection = tmp_path / "Knowledge Base/Records/Generic"
    collection.mkdir(parents=True)
    (collection / "_collection.md").write_text(_log_manifest(), encoding="utf-8")
    (collection / "log.md").write_text(
        """## Sessions

### 2026-08-02 · Push
<!-- exomem-record-id: 14d2bdca-e145-425b-9e4b-df86f7172efa -->
- Press | grey | 10+

```markdown
<!-- exomem-record-id: a8d391a5-c2dc-4e79-b57b-6b2bbcaefd64 -->
- Decoy | black | 999
### 2099-01-01 · Decoy
````
```

## Legend
- Legend | black | 999
""",
        encoding="utf-8",
    )
    return collections.load_manifest(tmp_path, collection / "_collection.md")


def test_generic_manifest_declares_real_heading_and_bullet_row_grammar(tmp_path: Path) -> None:
    manifest = _log_collection(tmp_path)

    parsed = record_formats.load_adapter(tmp_path, manifest).read()

    assert len(parsed.records) == 1
    assert parsed.records[0].values == {
        "occurred_on": "2026-08-02",
        "title": "Push",
        "note": None,
        "movements": [{"movement": "Press", "band": "grey", "repetitions": "10+"}],
    }
    assert parsed.records[0].children[0].values["repetitions"] == "10+"


def test_continuation_binds_manifest_bytes_and_refuses_tampering(tmp_path: Path) -> None:
    manifest = _log_collection(tmp_path)
    log = tmp_path / manifest.storage.source
    log.write_text(
        log.read_text(encoding="utf-8").replace(
            "```markdown", "### 2026-08-01 · Pull\n- Row | grey | !\n\n```markdown"
        ),
        encoding="utf-8",
    )

    first = record_formats.query_collection(tmp_path, manifest, limit=1)
    assert first.continuation
    assert "source_hashes" in first.rendered
    _version, token_payload, checksum = first.continuation.split(".")
    compact_payload = token_payload.replace("~", "")
    forged = json.loads(
        base64.urlsafe_b64decode(compact_payload + "=" * (-len(compact_payload) % 4))
    )
    forged["offset"] = 999
    altered = (
        "v1."
        + base64.urlsafe_b64encode(json.dumps(forged).encode()).decode().rstrip("=")
        + "."
        + checksum
    )
    with pytest.raises(collections.CollectionError) as excinfo:
        record_formats.query_collection(tmp_path, manifest, limit=1, continuation=altered)
    assert excinfo.value.code == "INVALID_RECORD_CONTINUATION"

    manifest_path = tmp_path / manifest.path
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8") + "\nChanged descriptor context.\n",
        encoding="utf-8",
    )
    changed_manifest = collections.load_manifest(tmp_path, manifest_path)
    with pytest.raises(collections.CollectionError) as excinfo:
        record_formats.query_collection(
            tmp_path, changed_manifest, limit=1, continuation=first.continuation
        )
    assert excinfo.value.code == "STALE_RECORD_SNAPSHOT"


def test_saved_view_query_binds_definition_and_manifest_only_edits_stale_continuation(
    tmp_path: Path,
) -> None:
    manifest = _log_collection(tmp_path)
    path = tmp_path / manifest.path
    source = tmp_path / manifest.storage.source
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            "```markdown",
            "### 2026-08-01 · Pull\n- Row | grey | 8\n\n```markdown",
        ),
        encoding="utf-8",
    )
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "item_schema:",
            "views:\n  recent:\n    query:\n      limit: 1\n    sort: [occurred_on, desc]\nitem_schema:",
        ),
        encoding="utf-8",
    )
    manifest = collections.load_manifest(tmp_path, path)

    first = record_formats.query_collection(tmp_path, manifest, view="recent")
    assert first.view is not None
    assert first.view["name"] == "recent"
    assert first.continuation

    path.write_text(
        path.read_text(encoding="utf-8").replace("limit: 1", "limit: 2"), encoding="utf-8"
    )
    changed = collections.load_manifest(tmp_path, path)
    with pytest.raises(collections.CollectionError) as raised:
        record_formats.query_collection(
            tmp_path, changed, view="recent", continuation=first.continuation
        )
    assert raised.value.code == "STALE_RECORD_SNAPSHOT"


def test_saved_view_source_snapshot_binds_canonical_data_not_its_manifest(tmp_path: Path) -> None:
    manifest = _log_collection(tmp_path)
    path = tmp_path / manifest.path
    source = tmp_path / manifest.storage.source
    source_snapshot = record_formats.load_adapter(tmp_path, manifest).read().data_snapshot
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "item_schema:",
            "views:\n  current:\n    query:\n      limit: 1\n"
            f"    source_snapshot: {source_snapshot}\nitem_schema:",
        ),
        encoding="utf-8",
    )
    manifest = collections.load_manifest(tmp_path, path)

    current = record_formats.inspect_collection(tmp_path, manifest)
    assert current.snapshot is not None
    assert "STALE_SAVED_VIEW" not in {diagnostic.code for diagnostic in current.diagnostics}

    source.write_text(source.read_text(encoding="utf-8").replace("Push", "Pull"), encoding="utf-8")
    stale = record_formats.inspect_collection(tmp_path, manifest)

    assert "STALE_SAVED_VIEW" in {diagnostic.code for diagnostic in stale.diagnostics}


def test_inspection_keeps_adapter_evidence_when_one_saved_view_is_malformed(tmp_path: Path) -> None:
    manifest = _log_collection(tmp_path)
    path = tmp_path / manifest.path
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "item_schema:",
            "views:\n  good:\n    query:\n      limit: 1\n  broken:\n"
            "    query:\n      columns: [[title]]\nitem_schema:",
        ),
        encoding="utf-8",
    )
    manifest = collections.load_manifest(tmp_path, path)

    inspected = record_formats.inspect_collection(tmp_path, manifest)

    assert inspected.snapshot is not None
    assert inspected.source_versions
    assert "INVALID_SAVED_VIEW" in {diagnostic.code for diagnostic in inspected.diagnostics}


def test_inspection_reports_one_located_diagnostic_per_invalid_saved_view(tmp_path: Path) -> None:
    manifest = _log_collection(tmp_path)
    path = tmp_path / manifest.path
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "item_schema:",
            "views:\n  broken:\n    query:\n      columns: [[title]]\nitem_schema:",
        ),
        encoding="utf-8",
    )
    manifest = collections.load_manifest(tmp_path, path)

    diagnostics = record_formats.inspect_collection(tmp_path, manifest).diagnostics

    assert diagnostics == (
        collections.CollectionDiagnostic(
            "INVALID_SAVED_VIEW", "saved view columns are invalid", "views.broken"
        ),
    )


def test_report_only_inspection_returns_typed_diagnostics_without_repair(tmp_path: Path) -> None:
    manifest = _log_collection(tmp_path)
    source = tmp_path / manifest.storage.source
    source.write_text("## Wrong section\n", encoding="utf-8")
    before = source.read_bytes()

    inspected = record_formats.inspect_collection(tmp_path, manifest)

    assert inspected.snapshot is None
    assert inspected.source_hashes[manifest.manifest_version.path] == manifest.manifest_version.hash
    assert inspected.diagnostics[0].code == "COLLECTION_SECTION_NOT_FOUND"
    assert source.read_bytes() == before


def test_dataset_duplicate_keys_are_ambiguous_and_rows_hide_item_versions(tmp_path: Path) -> None:
    collection = tmp_path / "Knowledge Base/Records/Dataset"
    collection.mkdir(parents=True)
    (collection / "rows.csv").write_text("id,value\nduplicate,1\nduplicate,2\n", encoding="utf-8")
    (collection / "_collection.md").write_text(
        _log_manifest()
        .replace("markdown-log", "dataset")
        .replace("log.md", "rows.csv")
        .replace(
            '  section:\n    level: 2\n    title: Sessions\n  item_heading:\n    level: 3\n    fields:\n      - name: occurred_on\n        type: date\n        format: "%Y-%m-%d"\n      - name: title\n        type: string\n    separator: " · "\n    note:\n      field: note\n      open: " ("\n      close: ")"\n  insertion: newest-first\n  child_rows:\n    prefix: "- "\n    delimiter: "|"\n    fields: [movement, band, repetitions]\n    container_field: movements\n',
            "  key: id\n",
        )
        .replace("natural_key: [occurred_on, title]", "natural_key: [id]")
        .replace(
            "    occurred_on:\n      type: date\n      required: true\n    title:\n      type: string\n      required: true\n    movements:\n      type: array\n      items:\n        type: object\n",
            "    id:\n      type: string\n      required: true\n    value:\n      type: number\n      required: true\n",
        ),
        encoding="utf-8",
    )
    manifest = collections.load_manifest(tmp_path, collection / "_collection.md")

    parsed = record_formats.load_adapter(tmp_path, manifest).read()
    result = record_formats.query_collection(tmp_path, manifest, limit=10)

    assert all(record.ambiguous for record in parsed.records)
    assert any(item.code == "DUPLICATE_RECORD_ID" for item in parsed.diagnostics)
    assert all("item_version" not in row for row in result.rows)


def test_query_profiles_keep_exact_cardinality_and_group_and_aggregate_rendering_bounded(
    tmp_path: Path,
) -> None:
    rows = [{"category": f"category-{number}", "value": number} for number in range(100)]

    profile = query_data.evaluate_rows(rows, path="rows", format="dataset", aggregate="profile")
    grouped = query_data.evaluate_rows(
        rows, path="rows", format="dataset", aggregate="group:category"
    )

    category = next(
        column for column in profile.aggregate["profile"]["columns"] if column["name"] == "category"
    )
    assert category["distinct"] == 100
    assert len(category["top_values"]) == query_data.PROFILE_MAX_DISTINCT
    assert grouped.aggregate["truncated"] is True
    assert len(grouped.aggregate["groups"]) == query_data.PROFILE_MAX_DISTINCT


def test_oversized_aggregate_is_capped_before_rendering() -> None:
    result = query_data.evaluate_rows(
        [{"value": "x" * (query_data.MAX_RESPONSE_BYTES + 1)}],
        path="rows",
        format="dataset",
        aggregate="latest:value",
    )

    assert result.aggregate == {"truncated": True, "reason": "aggregate exceeds response size cap"}
    assert result.truncated is True
