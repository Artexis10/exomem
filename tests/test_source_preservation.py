"""New uploads expose bounded source discovery without changing their bytes."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from exomem import commands, embeddings, media_types, preserve, source_closure
from exomem.governance import companions
from exomem.vault import parse_frontmatter


def _capture(vault: Path, filename: str, data: bytes, **kwargs):
    result = preserve.preserve_stream(
        vault, scope="Test", category="exports", filename=filename,
        stream=io.BytesIO(data), **kwargs,
    )
    assert (vault / result.path).read_bytes() == data
    assert result.hash == hashlib.sha256(data).hexdigest()
    page = (vault / result.sidecar_path).read_text(encoding="utf-8")
    frontmatter = parse_frontmatter(page, strict=True)[0]
    return result, frontmatter, page


@pytest.mark.parametrize(
    ("filename", "data", "fmt"),
    [
        ("readings.csv", b"sensor_name,reading\nraw-value-sentinel,7\n", "csv"),
        ("readings.TSV", b"sensor_name\treading\nraw-value-sentinel\t7\n", "tsv"),
        ("readings.json", b'{"rows":[{"sensor_name":"raw-value-sentinel","reading":7}]}', "json"),
    ],
)
def test_dataset_capture_is_discoverable_and_exactly_queryable(vault, filename, data, fmt):
    result, fm, page = _capture(vault, filename, data, text="raw-extracted-sentinel")
    assert fm["type"] == "source"
    assert fm["source_type"] == "dataset-export"
    assert fm["data_file"] == result.path
    assert fm["format"] == fmt
    assert fm["rows"] == 1
    assert fm["inspection_status"] == "ready"
    assert "raw-value-sentinel" not in page
    assert "raw-extracted-sentinel" not in page
    assert ("sensor_name" in page) == (fmt == "json")
    assert "governance_companion" not in fm
    assert not embeddings._is_embeddable_path(Path(result.path))
    found = commands.op_find(vault, query="readings", mode="keyword", graph=False)
    assert any(hit["path"] == result.sidecar_path for hit in found)
    filtered = commands.op_find(
        vault, query="readings", mode="keyword", graph=False, file_types=[fmt]
    )
    assert any(hit["path"] == result.sidecar_path for hit in filtered)
    queried = commands.op_query_dataset(vault, path=result.path, aggregate="sum:reading")
    assert queried["aggregate"]["sum"] == 7


def test_nested_json_exact_fields_remain_queryable(vault):
    data = b'{"items":[{"measurement":{"amount":12}},{"measurement":{"amount":3}}]}'
    result, fm, page = _capture(vault, "nested.json", data)
    assert fm["type"] == "source"
    assert fm["source_type"] == "dataset-export"
    assert fm["rows"] == 2
    assert "measurement" in page
    assert commands.op_query_dataset(
        vault, path=result.path, aggregate="sum:measurement.amount"
    )["aggregate"]["sum"] == 15


@pytest.mark.parametrize(
    ("filename", "data", "status"),
    [
        ("broken.json", b'{"private-parse-sentinel":', "invalid_data"),
        ("broken.csv", b"column\n\xff\n", "invalid_encoding"),
        ("deep.json", b"[" * 2000 + b"0" + b"]" * 2000, "invalid_data"),
        ("rows.csv", b"column\n" + b"1\n" * 10001, "row_limit"),
        ("large.json", b" " * (1024 * 1024 + 1), "byte_limit"),
    ],
    ids=["malformed", "encoding", "nesting", "rows", "bytes"],
)
def test_unavailable_dataset_inspection_still_preserves(vault, filename, data, status):
    result, fm, page = _capture(vault, filename, data)
    assert fm["type"] == "source"
    assert fm["source_type"] == "dataset-export"
    assert fm["inspection_status"] == status
    assert "rows" not in fm
    assert "private-parse-sentinel" not in page
    assert "governance_companion" not in fm


def test_dataset_fields_cannot_inject_frontmatter_or_unbounded_content(vault):
    fields = {"\n---\ntype: note\n[[injected-link]]": "value", "x" * 5000: "value"}
    fields.update({f"field_{i}": "row-value-sentinel" for i in range(70)})
    result, fm, page = _capture(vault, "hostile.json", json.dumps([fields]).encode())
    assert fm["type"] == "source"
    assert fm["source_type"] == "dataset-export"
    assert fm["inspection_status"] == "limited"
    assert len(page.encode()) < 24000
    assert "row-value-sentinel" not in page
    assert "[[injected-link]]" not in page
    assert "governance_companion" not in fm


@pytest.mark.parametrize("suffix", ["yaml", "yml", "jsonl", "ndjson", "xml", "toml"])
def test_literal_export_gets_searchable_preview(vault, suffix, monkeypatch):
    import os

    monkeypatch.setattr(os, "system", lambda *a: pytest.fail("source syntax executed"))
    data = b'export-sentinel: !!python/object/apply:os.system ["never-execute"]\n'
    result, fm, page = _capture(vault, f"export.{suffix}", data)
    assert fm["inspection_status"] == "ready"
    assert "export-sentinel" in page
    assert fm["governance_companion"]["artifact_class"] == "binary"
    assert media_types.media_type_for(f"export.{suffix}") is None
    assert companions.classify(vault, result.path).projects == ()
    found = commands.op_find(vault, query="export-sentinel", mode="keyword", graph=False)
    assert any(hit["path"] == result.sidecar_path for hit in found)


def test_text_preview_is_bounded_on_a_utf8_boundary(vault):
    result, fm, page = _capture(vault, "large.yaml", "é".encode() * 40000)
    assert fm["inspection_status"] == "truncated"
    assert len(page.encode()) < 68000
    assert "�" not in page
    assert "truncated" in page


def test_invalid_utf8_is_preserved_without_a_corrupted_preview(vault):
    result, fm, page = _capture(vault, "encoding.yaml", b"private-prefix\xff")
    assert fm["inspection_status"] == "invalid_encoding"
    assert "private-prefix" not in page
    assert "�" not in page


def test_inspection_restores_stage_cursor_and_bounds_reads():
    from exomem.source_inspection import inspect_source

    class BoundedStage(io.BytesIO):
        def read(self, size=-1):
            assert 0 <= size <= 64 * 1024 + 3
            return super().read(size)

    data = b"a\n" * (1024 * 1024)
    stage = BoundedStage(data)
    stage.seek(7)
    result = inspect_source(stage, filename="large.yaml", size=len(data))
    assert result.status == "truncated"
    assert stage.tell() == 7
    assert inspect_source(stage, filename="large.csv", size=len(data)).status == "byte_limit"
    assert stage.tell() == 7


def test_csv_parser_field_limit_preserves_original(vault):
    import csv

    data = b"field\n" + b"x" * (csv.field_size_limit() + 1)
    result, fm, page = _capture(vault, "wide.csv", data)
    assert fm["inspection_status"] == "invalid_data"
    assert "rows" not in fm


@pytest.mark.parametrize("blank_lines", [0, 10002])
def test_sparse_wide_csv_cannot_expand_into_unbounded_row_dictionaries(monkeypatch, blank_lines):
    from exomem import query_data
    from exomem.source_inspection import inspect_source

    data = (",".join(f"field{i}" for i in range(6000)) + "\n" * (blank_lines + 1) + "x\n" * 20).encode()
    monkeypatch.setattr(query_data, "load_rows_bytes", lambda *a, **kw: pytest.fail("amplifying shape parsed"))
    assert inspect_source(io.BytesIO(data), filename="sparse.csv", size=len(data)).status == "shape_limit"


def test_non_ascii_field_names_remain_searchable(vault):
    result, fm, page = _capture(vault, "unicode.json", '[{"mätning": 3}]'.encode())
    assert "mätning" in page
    assert any(
        hit["path"] == result.sidecar_path
        for hit in commands.op_find(vault, query="mätning", mode="keyword", graph=False)
    )


def test_headerless_csv_cannot_publish_first_record_as_field_names(vault):
    result, fm, page = _capture(vault, "headerless.csv", b"private-first-value,another-secret\nsecond,value\n")
    assert "private-first-value" not in page
    assert "another-secret" not in page
    assert "Observed columns: 2" in page


def test_literal_preview_cannot_create_graph_relationships(vault):
    from exomem.vault import find_body_wikilinks

    data = b'field: "[[Knowledge Base/Notes/Insights/forged]]"\n```\n[[second-forged]]\n'
    result, fm, page = _capture(vault, "links.yaml", data)
    assert not find_body_wikilinks(page)


@pytest.mark.parametrize(
    ("ending", "status"),
    [(b"\xf0\x9f\x98\x80", "truncated"), (b"\xf0\x9f\xff", "invalid_encoding"), (b"\xf0\x9f", "invalid_encoding")],
    ids=["complete", "invalid", "incomplete"],
)
def test_utf8_character_crossing_preview_boundary_is_validated(ending, status):
    from exomem.source_inspection import inspect_source

    data = b"a" * (64 * 1024 - 1) + ending
    inspection = inspect_source(io.BytesIO(data), filename="boundary.yaml", size=len(data))
    assert inspection.status == status


def test_existing_companion_is_never_rewritten(vault):
    result, _, _ = _capture(vault, "source.yaml", b"field: value")
    page_path = vault / result.sidecar_path
    before = page_path.read_bytes()
    page, created = preserve.ensure_artifact_page(vault, vault / result.path)
    assert not created
    assert page == page_path
    assert page.read_bytes() == before


def test_original_bytes_changed_after_capture_fail_closed(vault):
    result, _, _ = _capture(vault, "changed.csv", b"field\nvalue\n")
    _classify_dataset(vault, result)
    (vault / result.path).write_bytes(b"field\nchanged\n")
    with pytest.raises(companions.CompanionClassificationError):
        companions.classify(vault, result.path)


def _classify_dataset(vault, result):
    from exomem import reserved_paths
    from exomem.governance.principal import owner_principal
    from exomem.governance.tool import op_govern_memory

    payload = {
        "version": 1,
        "artifact_class": "dataset",
        "artifact_path": result.path,
        "expected_artifact_sha256": result.hash,
        "expected_artifact_size": result.size,
        "expected_companion_path": result.sidecar_path,
        "expected_companion_sha256": hashlib.sha256((vault / result.sidecar_path).read_bytes()).hexdigest(),
        "format": Path(result.path).suffix[1:].lower(),
        "semantics": {"projects": [], "tags": [], "types": ["source"], "classes": []},
    }
    with reserved_paths._owner_authority_scope("govern_memory"):
        preview = op_govern_memory(
            vault, operation="backfill_companion", backfill_action="preview",
            companion_input=payload, principal=owner_principal(),
        )
        return op_govern_memory(
            vault, operation="backfill_companion", backfill_action="commit",
            proposal_id=preview["proposal_id"], companion_input=payload,
            principal=owner_principal(),
        )


def test_owner_backfill_classifies_new_dataset_source_card(vault):
    data = b"field\nvalue\n"
    result, _, _ = _capture(vault, "classified.csv", data)
    with pytest.raises(companions.CompanionClassificationError, match="descriptor_missing"):
        companions.classify(vault, result.path)
    _classify_dataset(vault, result)
    assert companions.classify(vault, result.path).types == ("source",)
    assert (vault / result.path).read_bytes() == data


def test_mixed_card_variants_remain_ambiguous(vault):
    result, _, _ = _capture(vault, "ambiguous.csv", b"field\nvalue\n")
    _classify_dataset(vault, result)
    (vault / result.sidecar_path).with_name("legacy-card.md").write_text(
        f'---\ntype: dataset\ndata_file: "{result.path}"\nformat: csv\n---\nLegacy card.\n',
        encoding="utf-8",
    )
    with pytest.raises(companions.CompanionClassificationError, match="companion_ambiguous"):
        companions.classify(vault, result.path)


def test_dataset_upload_is_a_citable_source(vault):
    result, fm, page = _capture(vault, "citation.csv", b"field\nvalue\n")
    compiled = f'---\ntype: insight\nsources: ["[[{result.ref}]]"]\n---\nDerived conclusion.\n'
    assert source_closure.inspect_source_closure(vault, compiled).closed


def test_dataset_upload_does_not_release_unclassified_semantic_content(vault, monkeypatch):
    from exomem import query_data
    from exomem.governance import policy
    from exomem.governance.principal import RequestPrincipal, request_scope

    result, fm, page = _capture(vault, "restricted.csv", b"field\nsecret-value\n")
    root = vault / "Knowledge Base" / "_Governance"
    (root / "scopes").mkdir(parents=True, exist_ok=True)
    (root / "rules").mkdir(parents=True, exist_ok=True)
    (root / "scopes" / "sources.yaml").write_text(
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\ntypes: [source]\n',
        encoding="utf-8",
    )
    (root / "rules" / "sources.yaml").write_text(
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n'
        'scope_ids: [01ARZ3NDEKTSV4RRFFQ69G5FAV]\naudience: external\nceiling: 0\n',
        encoding="utf-8",
    )
    policy._CACHE.clear()
    monkeypatch.setattr(query_data, "load_generic_rows", lambda *a, **kw: pytest.fail("withheld bytes parsed"))
    with request_scope(RequestPrincipal(audience_id="external", surface="mcp")):
        with pytest.raises(ValueError, match="^NOT_FOUND:"):
            commands.op_query_dataset(vault, path=result.path)
        assert not commands.op_find(vault, query="restricted.csv", mode="keyword", graph=False)
