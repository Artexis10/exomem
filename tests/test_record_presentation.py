from __future__ import annotations

from pathlib import Path

import pytest

from exomem import record_formats, records
from exomem import structured_collections as collections


def _manifest_text() -> str:
    return """---
type: collection
exomem_id: 44444444-4444-4444-8444-444444444444
title: Observed measurements
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Items
  format_version: 1
item_schema:
  natural_key: [observed_on]
  fields:
    observed_on:
      type: date
      required: true
    subject:
      type: string
      required: true
    measurements:
      type: array
      required: true
      items:
        type: object
record_presentation:
  version: 1
  summary:
    - field: subject
  tables:
    - field: measurements
      label: Measurements
      columns:
        - field: name
          type: string
        - field: value
          type: string
        - field: source
          type: link
          link_kind: note
  notes: []
  details: []
---
"""


def _setup(vault: Path) -> collections.CollectionManifest:
    (vault / "Knowledge Base/log.md").parent.mkdir(parents=True, exist_ok=True)
    (vault / "Knowledge Base/log.md").write_text("# Activity\n", encoding="utf-8")
    path = vault / "Knowledge Base/Records/Observed/_collection.md"
    path.parent.mkdir(parents=True)
    path.write_text(_manifest_text(), encoding="utf-8")
    (path.parent / "Items").mkdir()
    return collections.load_manifest(vault, path)


def test_records_only_presentation_is_normalized_and_legacy_presentation_is_opaque(
    tmp_path: Path,
) -> None:
    manifest = _setup(tmp_path)

    recipe = manifest.record_presentation

    assert recipe is not None
    assert recipe.version == 1
    assert recipe.tables[0].field == "measurements"
    assert recipe.tables[0].columns[2].link_kind == "note"

    planning = _manifest_text().replace("semantic_profile: records", "semantic_profile: planning")
    planning_path = tmp_path / "Knowledge Base/Planning/Observed/_collection.md"
    planning_path.parent.mkdir(parents=True)
    planning_path.write_text(planning.replace("source: Items", "source: Items"), encoding="utf-8")
    with pytest.raises(collections.CollectionError, match="RECORD_PRESENTATION"):
        collections.load_manifest(tmp_path, planning_path)


def test_markdown_item_presentation_renders_from_canonical_values_and_preserves_body(
    tmp_path: Path,
) -> None:
    manifest = _setup(tmp_path)

    rendered = record_formats.render_markdown_item(
        manifest,
        {
            "observed_on": "2026-08-13",
            "subject": "Panel <A>",
            "measurements": [
                {"name": "Example", "value": "<5", "source": "[[Source]]", "secret": "hidden"}
            ],
        },
        "11111111-1111-4111-8111-111111111111",
        "Authored prose.\r\n",
    )

    assert "<!-- exomem-record-presentation:v1" in rendered
    assert "Panel &lt;A&gt;" in rendered
    assert "[[Source]]" in rendered
    managed = rendered.split("<!-- exomem-record-presentation:v1", 1)[1]
    assert "hidden" not in managed
    assert rendered.endswith("Authored prose.\r\n")


def test_record_queries_project_nested_values_and_expand_the_selected_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "state"))
    manifest = _setup(tmp_path)
    result = records.append_record(
        tmp_path,
        manifest,
        item_key="11111111-1111-4111-8111-111111111111",
        item={
            "observed_on": "2026-08-13",
            "subject": "Panel",
            "measurements": [
                {"name": "One", "value": "<5", "source": "[[Source]]", "secret": "hidden"},
                {"name": "Two", "value": None, "source": "[[Source]]", "secret": "also hidden"},
            ],
        },
        why="add observed measurements",
    )

    unexpanded = record_formats.query_collection(tmp_path, manifest, limit=10)
    expanded = record_formats.query_collection(
        tmp_path, manifest, expand_child="measurements", limit=10
    )

    assert result["outcome"] == "committed"
    assert unexpanded.rows[0]["measurements"] == [
        {"name": "One", "value": "<5", "source": "[[Source]]"},
        {"name": "Two", "value": None, "source": "[[Source]]"},
    ]
    assert [row["name"] for row in expanded.rows] == ["One", "Two"]
    assert all("measurements" not in row and "secret" not in row for row in expanded.rows)
    assert all(row["child_field"] == "measurements" for row in expanded.rows)

    replayed = records.append_record(
        tmp_path,
        collections.load_manifest(tmp_path, "Knowledge Base/Records/Observed/_collection.md"),
        item_key="11111111-1111-4111-8111-111111111111",
        item={
            "observed_on": "2026-08-13",
            "subject": "Panel",
            "measurements": [
                {"name": "One", "value": "<5", "source": "[[Source]]", "secret": "hidden"},
                {"name": "Two", "value": None, "source": "[[Source]]", "secret": "also hidden"},
            ],
        },
        why="retry observed measurements",
    )
    assert replayed["outcome"] == "replayed"


def test_presentation_rejects_synthesized_child_column_names(tmp_path: Path) -> None:
    path = tmp_path / "Knowledge Base/Records/Observed/_collection.md"
    path.parent.mkdir(parents=True)
    path.write_text(_manifest_text().replace("field: name\n          type", "field: child_index\n          type"), encoding="utf-8")

    with pytest.raises(collections.CollectionError, match="RECORD_PRESENTATION"):
        collections.load_manifest(tmp_path, path)


def test_presentation_escapes_table_cells_and_keeps_authored_leading_blanks(tmp_path: Path) -> None:
    manifest = _setup(tmp_path)
    rendered = record_formats.render_markdown_item(
        manifest,
        {
            "observed_on": "2026-08-13",
            "subject": "Panel",
            "measurements": [{"name": r"A|B\\C", "value": "line one\nline two", "source": "[[Source]]"}],
        },
        "11111111-1111-4111-8111-111111111111",
        "\n\nAuthored prose.\n",
    )

    assert r"A\|B&#92;&#92;C" in rendered
    assert "line one<br>line two" in rendered
    body = rendered.split("---\n", 2)[2]
    assert records._semantic_body(body) == "\n\nAuthored prose.\n"


def test_selected_child_cap_precedes_nested_link_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "state"))
    manifest = _setup(tmp_path)
    records.append_record(
        tmp_path,
        manifest,
        item_key="11111111-1111-4111-8111-111111111111",
        item={
            "observed_on": "2026-08-13",
            "subject": "Panel",
            "measurements": [
                {"name": "One", "value": "1", "source": "[[Source]]"},
                {"name": "Two", "value": "2", "source": "[[Source]]"},
            ],
        },
        why="add observed measurements",
    )
    calls: list[object] = []
    monkeypatch.setattr(record_formats, "_MAX_CHILD_ROWS", 1)

    with pytest.raises(collections.CollectionError, match="CHILD_LIMIT"):
        record_formats.query_collection(
            tmp_path,
            collections.load_manifest(tmp_path, "Knowledge Base/Records/Observed/_collection.md"),
            expand_child="measurements",
            project_child_value=lambda value, _column: calls.append(value) or value,
        )
    assert calls == []


def test_withheld_nested_link_is_removed_before_query_operations(tmp_path: Path) -> None:
    manifest = _setup(tmp_path)
    values = {
        "observed_on": "2026-08-13",
        "subject": "Panel",
        "measurements": [{"name": "One", "value": "1", "source": "[[Withheld]]", "secret": "x"}],
    }

    projected = record_formats._safe_presentation_values(
        values, manifest, lambda _value, column: None if column.type == "link" else _value
    )

    assert projected["measurements"] == [{"name": "One", "value": "1"}]


def test_managed_block_refuses_oversize_before_splicing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _setup(tmp_path)
    monkeypatch.setattr(record_formats, "_MAX_PRESENTATION_BLOCK_BYTES", 64)

    with pytest.raises(collections.CollectionError, match="PRESENTATION"):
        record_formats.render_markdown_item(
            manifest,
            {"observed_on": "2026-08-13", "subject": "Panel", "measurements": [{"name": "x", "value": "y", "source": "[[Source]]"}]},
            "11111111-1111-4111-8111-111111111111",
        )
