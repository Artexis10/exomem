from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from exomem import query_data, record_formats
from exomem import structured_collections as collections

COLLECTION_ID = "c537f2c4-672a-4ddd-afcd-8d99e25f4019"


def _manifest() -> str:
    return f"""---
type: collection
exomem_id: {COLLECTION_ID}
title: Finite grammar
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
  defaults:
    status: completed
  note_rules:
    - equals: Interrupted
      values:
        status: aborted
    - equals: Incomplete
      values:
        status: partial
  insertion: newest-first
  child_rows:
    prefix: "- "
    delimiter: "|"
    fields: [movement, load, repetitions]
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
    status:
      type: enum
      enum: [completed, partial, aborted]
    movements:
      type: array
      items:
        type: object
---
"""


def _collection(tmp_path: Path, body: str) -> collections.CollectionManifest:
    root = tmp_path / "Knowledge Base/Records/Finite"
    root.mkdir(parents=True)
    (root / "_collection.md").write_text(_manifest(), encoding="utf-8")
    (root / "log.md").write_text(body, encoding="utf-8")
    return collections.load_manifest(tmp_path, root / "_collection.md")


def test_finite_heading_grammar_and_manifest_status_rules_are_generic(tmp_path: Path) -> None:
    manifest = _collection(
        tmp_path,
        """## Sessions

### 2026-08-02 · Alpha
- Press | grey | 10+

### 2026-08-01 · Beta (Interrupted)
- Row | grey | ?

### 2026-07-31 · Gamma (Incomplete)
- Row | grey |

## Other
""",
    )

    parsed = record_formats.load_adapter(tmp_path, manifest).read()

    assert [record.values["status"] for record in parsed.records] == [
        "completed",
        "aborted",
        "partial",
    ]
    assert parsed.records[1].values["note"] == "Interrupted"


def test_marker_must_be_unique_first_nonblank_content_after_heading(tmp_path: Path) -> None:
    manifest = _collection(
        tmp_path,
        """## Sessions

### 2026-08-02 · Alpha
- Press | grey | 10
<!-- exomem-record-id: 14d2bdca-e145-425b-9e4b-df86f7172efa -->

## Other
""",
    )

    with pytest.raises(collections.CollectionError) as excinfo:
        record_formats.load_adapter(tmp_path, manifest).read()

    assert excinfo.value.code == "INVALID_RECORD_MARKER"


def test_heading_grammar_rejects_the_removed_pattern_key(tmp_path: Path) -> None:
    manifest = _collection(
        tmp_path,
        """## Sessions

### 2026-08-02 · Alpha
""",
    )
    path = tmp_path / manifest.path
    path.write_text(
        path.read_text(encoding="utf-8").replace('separator: " · "', "pattern: '.*'"),
        encoding="utf-8",
    )
    manifest = collections.load_manifest(tmp_path, path)

    with pytest.raises(collections.CollectionError) as excinfo:
        record_formats.load_adapter(tmp_path, manifest).read()

    assert excinfo.value.code == "INVALID_STORAGE_DESCRIPTOR"


def test_commonmark_fence_closer_rejects_info_string_false_close() -> None:
    headings = record_formats._headings_outside_fences(
        b"```python\n```` not-a-close\n### hidden\n```\n"
    )

    assert headings == []


def test_versioned_checksum_cursor_survives_module_reload_and_rejects_tampering() -> None:
    token = record_formats._encode_continuation(
        {"collection_id": COLLECTION_ID, "offset": 1, "query": {}}
    )
    reloaded = importlib.reload(record_formats)

    assert reloaded._decode_continuation(token)["offset"] == 1
    with pytest.raises(collections.CollectionError) as excinfo:
        reloaded._decode_continuation(token[:-1] + ("0" if token[-1] != "0" else "1"))
    assert excinfo.value.code == "INVALID_RECORD_CONTINUATION"


def test_continuation_payload_and_envelope_caps_round_trip_at_the_boundary() -> None:
    payload = {
        "collection_id": COLLECTION_ID,
        "offset": 1,
        "query": {"columns": ["x" * (record_formats._MAX_TOKEN_PAYLOAD_BYTES - 128)]},
    }

    token = record_formats._encode_continuation(payload)

    assert len(token.encode("utf-8")) <= record_formats._MAX_TOKEN_ENVELOPE_BYTES
    assert record_formats._decode_continuation(token) == payload


def test_truncated_aggregate_is_returned_even_without_row_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _collection(tmp_path, "## Sessions\n\n### 2026-08-02 · Alpha\n")

    def truncated_aggregate(
        _rows: list[dict[object, object]], **kwargs: object
    ) -> query_data.QueryDataResult:
        return query_data.QueryDataResult(
            path="log.md",
            format="markdown-log",
            total_rows=1,
            total_matched=1,
            returned=0,
            columns=[],
            rows=[],
            aggregate={"truncated": True},
            truncated=True,
        )

    monkeypatch.setattr(record_formats.query_data, "evaluate_rows", truncated_aggregate)

    result = record_formats.query_collection(tmp_path, manifest, aggregate="latest:title")

    assert result.aggregate == {"truncated": True}


def test_adapter_refuses_manifest_bytes_that_drift_after_loading(tmp_path: Path) -> None:
    manifest = _collection(tmp_path, "## Sessions\n\n### 2026-08-02 · Alpha\n")
    adapter = record_formats.load_adapter(tmp_path, manifest)
    manifest_path = tmp_path / manifest.path
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8") + "\nChanged.\n", encoding="utf-8"
    )

    with pytest.raises(collections.CollectionError) as excinfo:
        adapter.read()

    assert excinfo.value.code == "STALE_COLLECTION_MANIFEST"


def test_dataset_key_change_stales_second_page_but_fresh_manifest_works(tmp_path: Path) -> None:
    root = tmp_path / "Knowledge Base/Records/Paged"
    root.mkdir(parents=True)
    (root / "rows.csv").write_text(
        "id,replacement,value\nold-1,new-1,1\nold-2,new-2,2\n", encoding="utf-8"
    )
    manifest_path = root / "_collection.md"
    manifest_path.write_text(
        f"""---
type: collection
exomem_id: {COLLECTION_ID}
title: Paged dataset
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: dataset
  source: rows.csv
  format_version: 1
  key: id
item_schema:
  natural_key: [id]
  fields:
    id:
      type: string
      required: true
    replacement:
      type: string
      required: true
    value:
      type: integer
      required: true
---
""",
        encoding="utf-8",
    )
    first_manifest = collections.load_manifest(tmp_path, manifest_path)
    first_page = record_formats.query_collection(tmp_path, first_manifest, limit=1)
    assert first_page.continuation

    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace("key: id", "key: replacement"),
        encoding="utf-8",
    )
    fresh_manifest = collections.load_manifest(tmp_path, manifest_path)

    with pytest.raises(collections.CollectionError) as excinfo:
        record_formats.query_collection(
            tmp_path, fresh_manifest, limit=1, continuation=first_page.continuation
        )
    assert excinfo.value.code == "STALE_RECORD_SNAPSHOT"
    assert (
        record_formats.query_collection(tmp_path, fresh_manifest, limit=1).rows[0]["record_id"]
        == "new-1"
    )


def test_markdown_log_refuses_twenty_thousand_headings_within_the_byte_cap(tmp_path: Path) -> None:
    manifest = _collection(tmp_path, "## Sessions\n" + "### skipped\n" * 20_000)

    with pytest.raises(collections.CollectionError) as excinfo:
        record_formats.load_adapter(tmp_path, manifest).read()

    assert excinfo.value.code == "RECORD_HEADING_LIMIT"


def test_markdown_log_refuses_twenty_thousand_child_rows_within_the_byte_cap(
    tmp_path: Path,
) -> None:
    manifest = _collection(
        tmp_path, "## Sessions\n\n### 2026-08-02 · Alpha\n" + "- Press | grey | 10\n" * 20_000
    )

    with pytest.raises(collections.CollectionError) as excinfo:
        record_formats.load_adapter(tmp_path, manifest).read()

    assert excinfo.value.code == "RECORD_CHILD_ROW_LIMIT"


@pytest.mark.parametrize(
    ("cell", "field", "expected"),
    [("not-an-integer", "count", "INTEGER"), ("1e100000", "amount", "NUMBER")],
)
def test_dataset_coercion_refuses_invalid_typed_cells(
    tmp_path: Path, cell: str, field: str, expected: str
) -> None:
    root = tmp_path / "Knowledge Base/Records/Typed"
    root.mkdir(parents=True)
    row = f"row,{cell},1" if field == "count" else f"row,1,{cell}"
    (root / "rows.csv").write_text(f"id,count,amount\n{row}\n", encoding="utf-8")
    (root / "_collection.md").write_text(
        f"""---
type: collection
exomem_id: {COLLECTION_ID}
title: Typed dataset
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: dataset
  source: rows.csv
  format_version: 1
  key: id
item_schema:
  natural_key: [id]
  fields:
    id:
      type: string
      required: true
    count:
      type: integer
      required: true
    amount:
      type: number
      required: true
---
""",
        encoding="utf-8",
    )
    manifest = collections.load_manifest(tmp_path, root / "_collection.md")

    with pytest.raises(collections.CollectionError) as excinfo:
        record_formats.load_adapter(tmp_path, manifest).read()

    assert excinfo.value.code == "INVALID_DATASET_FIELD"
    assert expected.lower() in excinfo.value.reason


def test_backtick_info_string_with_backtick_does_not_open_a_fence() -> None:
    headings = record_formats._headings_outside_fences(
        b"``` label ` invalid\n### visible\n```\n~~~ label ` valid\n### hidden\n~~~\n"
    )

    assert [heading.title for heading in headings] == ["visible"]


@pytest.mark.parametrize(
    "child_rows",
    [
        'child_rows:\n    prefix: ""\n    delimiter: "|"\n    fields: [movement, load, repetitions]\n    container_field: movements',
        'child_rows:\n    prefix: "- \\n"\n    delimiter: "|"\n    fields: [movement, load, repetitions]\n    container_field: movements',
        'child_rows:\n    prefix: "- "\n    delimiter: "|"\n    fields: movement\n    container_field: movements',
        'child_rows:\n    prefix: "- "\n    delimiter: "|"\n    fields: [movement, movement, repetitions]\n    container_field: movements',
        'child_rows:\n    prefix: "- "\n    delimiter: "|"\n    fields: [movement, record_id, repetitions]\n    container_field: movements',
        'child_rows:\n    prefix: "- "\n    delimiter: "|"\n    fields: [movement, load, repetitions]\n    container_field: movements\n    extra: true',
        'child_rows:\n    prefix: "- "\n    delimiter: "|"\n    fields: [a, b, c, d, e, f, g, h, i, j, k, l, m, n, o, p, q]\n    container_field: movements',
        'child_rows:\n    prefix: "- "\n    delimiter: "|"\n    fields: [movement, load, repetitions]\n    container_field: status',
        'child_rows:\n    prefix: "- "\n    delimiter: "|"\n    fields: [movement, load, repetitions]\n    container_field: [entries]',
    ],
)
def test_child_row_descriptor_refuses_invalid_grammar(tmp_path: Path, child_rows: str) -> None:
    manifest = _collection(tmp_path, "## Sessions\n\n### 2026-08-02 · Alpha\n")
    path = tmp_path / manifest.path
    original = 'child_rows:\n    prefix: "- "\n    delimiter: "|"\n    fields: [movement, load, repetitions]\n    container_field: movements'
    path.write_text(
        path.read_text(encoding="utf-8").replace(original, child_rows), encoding="utf-8"
    )
    manifest = collections.load_manifest(tmp_path, path)

    with pytest.raises(collections.CollectionError) as excinfo:
        record_formats.load_adapter(tmp_path, manifest).read()

    assert excinfo.value.code == "INVALID_STORAGE_DESCRIPTOR"


def test_markdown_log_uses_declared_non_domain_child_container(tmp_path: Path) -> None:
    manifest = _collection(tmp_path, "## Sessions\n\n### 2026-08-02 · Alpha\n- Item | grey | 10\n")
    path = tmp_path / manifest.path
    path.write_text(
        path.read_text(encoding="utf-8")
        .replace(
            "fields: [movement, load, repetitions]\n    container_field: movements",
            "fields: [item, load, repetitions]\n    container_field: entries",
        )
        .replace("    movements:\n", "    entries:\n"),
        encoding="utf-8",
    )
    manifest = collections.load_manifest(tmp_path, path)

    values = record_formats.load_adapter(tmp_path, manifest).read().records[0].values

    assert values["entries"] == [{"item": "Item", "load": "grey", "repetitions": "10"}]
    assert "movements" not in values


def test_markdown_item_accepts_one_bom_without_changing_raw_identity(tmp_path: Path) -> None:
    root = tmp_path / "Knowledge Base/Records/Items"
    root.mkdir(parents=True)
    (root / "_collection.md").write_text(
        f"""---
type: collection
exomem_id: {COLLECTION_ID}
title: BOM item
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Events
  format_version: 1
item_schema:
  natural_key: [name]
  fields:
    name:
      type: string
      required: true
---
""",
        encoding="utf-8",
    )
    item = root / "Events/bom.md"
    raw = (
        b"\xef\xbb\xbf---\n"
        + f"type: record\ncollection_id: {COLLECTION_ID}\nrecord_id: 14d2bdca-e145-425b-9e4b-df86f7172efa\nschema_version: 1\nname: BOM\n---\n\nBody stays exact.\n".encode()
    )
    item.parent.mkdir()
    item.write_bytes(raw)
    manifest = collections.load_manifest(tmp_path, root / "_collection.md")

    record = record_formats.load_adapter(tmp_path, manifest).read().records[0]

    assert record.body == "Body stays exact.\n"
    assert record.source.hash == __import__("hashlib").sha256(raw).hexdigest()
    assert record.span.end == len(raw)


def test_markdown_item_refuses_double_bom(tmp_path: Path) -> None:
    root = tmp_path / "Knowledge Base/Records/Items"
    root.mkdir(parents=True)
    (root / "_collection.md").write_text(
        _manifest().replace("markdown-log", "markdown-items").replace("log.md", "Events"),
        encoding="utf-8",
    )
    item = root / "Events/double.md"
    item.parent.mkdir()
    item.write_bytes(b"\xef\xbb\xbf\xef\xbb\xbf---\n")
    manifest = collections.load_manifest(tmp_path, root / "_collection.md")

    with pytest.raises(collections.CollectionError) as excinfo:
        record_formats.load_adapter(tmp_path, manifest).read()

    assert excinfo.value.code == "INVALID_RECORD_ITEM"
