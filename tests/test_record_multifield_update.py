from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from record_presentation_fixtures import ITEM_KEY, setup_collection, values

from exomem import record_formats, records, vault
from exomem import structured_collections as collections


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
@pytest.mark.parametrize("mixed_update", [False, True], ids=["add", "add-change-delete"])
@pytest.mark.parametrize("reverse_order", [False, True], ids=["schema-order", "reverse-order"])
@pytest.mark.parametrize("scalar_style", ["quoted", "literal", "folded"])
def test_schema_revision_allows_multiple_new_fields_in_one_item_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    newline: str,
    mixed_update: bool,
    reverse_order: bool,
    scalar_style: str,
) -> None:
    manifest = setup_collection(tmp_path, presentation=False)
    initial_values = values()
    source = record_formats.render_markdown_item(
        manifest, initial_values, ITEM_KEY, body="Unmanaged body.\n"
    )
    # A block-valued deletion immediately precedes an untouched field. Its
    # terminator must not cause the deletion to swallow that following field.
    source = source.replace(
        vault.serialize_frontmatter({"note": initial_values["note"]}),
        "note: |-\n  " + str(initial_values["note"]),
    ).replace("subject:", "# Keep this comment.\nsubject:")
    item_path = tmp_path / manifest.storage.source / "item.md"
    before = source.replace("\n", newline).encode("utf-8")
    item_path.write_bytes(before)
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    manifest_path = tmp_path / manifest.path
    proposed = manifest_path.read_text(encoding="utf-8").replace(
        "    provenance:\n",
        "    delta_note:\n"
        "      type: string\n"
        "    alpha_tags:\n"
        "      type: array\n"
        "      items:\n"
        "        type: string\n"
        "    omega_details:\n"
        "      type: object\n"
        "    provenance:\n",
    )
    revision = records.revise_collection(
        tmp_path,
        manifest.path,
        manifest_text=proposed,
        **records.lifecycle_guards(manifest, snapshot),
        why="add optional backfill fields",
    )
    assert revision["outcome"] == "committed"
    assert item_path.read_bytes() == before
    manifest = collections.load_manifest(tmp_path, manifest_path)
    snapshot = record_formats.load_adapter(tmp_path, manifest).read()
    record = snapshot.records[0]
    new_fields = {
        "delta_note": "First line.\nSecond line.",
        "alpha_tags": ["backfill", "reviewed"],
        "omega_details": {"source": "migration", "revision": 2},
    }
    assert all(not manifest.schema.fields[name].required for name in new_fields)
    assert all(name not in record.values for name in new_fields)
    if reverse_order:
        new_fields = dict(reversed(list(new_fields.items())))
    changes = dict(new_fields)
    if mixed_update:
        changes["subject"] = "Corrected sample"
    delete_fields = ("note",) if mixed_update else ()

    if scalar_style != "quoted":
        # The shipped serializer quotes multiline strings. Feed the same value
        # in literal/folded YAML form to exercise append boundaries for those
        # node styles without replacing the writer or the parser.
        serialize = vault.serialize_frontmatter

        def serialize_block_scalar(frontmatter: dict[str, object]) -> str:
            if list(frontmatter) == ["delta_note"]:
                if scalar_style == "literal":
                    return "delta_note: |-\n  First line.\n  Second line."
                return "delta_note: >-\n  First line.\n\n  Second line."
            return serialize(frontmatter)

        monkeypatch.setattr(vault, "serialize_frontmatter", serialize_block_scalar)

    log = tmp_path / "Knowledge Base/log.md"
    audit_count = log.read_text(encoding="utf-8").count("Records audit-v")
    result = records.update_record(
        tmp_path,
        manifest.path,
        item_key=ITEM_KEY,
        changes=changes,
        delete_fields=delete_fields,
        expected_container_hash=snapshot.snapshot,
        expected_item_version=record.source.hash,
        why="backfill all new fields in one update",
    )

    after = item_path.read_bytes()
    frontmatter, body, _marker = vault.parse_frontmatter(after.decode("utf-8"), strict=True)
    original, original_body, _marker = vault.parse_frontmatter(before.decode("utf-8"), strict=True)
    expected = {name: value for name, value in original.items() if name not in delete_fields}
    expected.update(changes)
    assert frontmatter == expected
    # Existing keys retain source order; new keys follow the request's order,
    # even when that order differs from the schema or alphabetical order.
    assert list(frontmatter) == list(expected)
    assert body == original_body
    assert b"# Keep this comment." + newline.encode() in after
    assert b"alpha_tags: [backfill, reviewed]" + newline.encode() in after
    if newline == "\r\n":
        assert b"\n" not in after.replace(b"\r\n", b"")
    else:
        assert b"\r" not in after
    assert result["outcome"] == "committed"
    assert result["affected_paths"] == [record.source.path]
    assert result["after_item_hash"] == hashlib.sha256(after).hexdigest()
    assert log.read_text(encoding="utf-8").count("Records audit-v") == audit_count + 1
    refreshed = collections.load_manifest(tmp_path, manifest_path)
    updated = record_formats.load_adapter(tmp_path, refreshed).read()
    assert len(updated.records) == 1
    assert updated.records[0].values == {
        name: value for name, value in expected.items() if name in refreshed.schema.fields
    }
    assert result["after_container_hash"] == updated.snapshot
    assert records.inspect_audit_gap(tmp_path, manifest.path)["status"] == "ok"


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
@pytest.mark.parametrize("scalar_style", ["plain", "literal"])
@pytest.mark.parametrize("operation", ["change", "delete"])
def test_multifield_append_alongside_last_field_change_or_delete(
    tmp_path: Path,
    newline: str,
    scalar_style: str,
    operation: str,
) -> None:
    manifest = setup_collection(tmp_path, presentation=False)
    initial_values = values()
    source = record_formats.render_markdown_item(
        manifest, initial_values, ITEM_KEY, body="Unmanaged body.\n"
    )
    provenance = str(initial_values["provenance"])
    rendered_provenance = (
        "provenance: " + provenance
        if scalar_style == "plain"
        else "provenance: |-\n  " + provenance
    )
    source = source.replace(
        vault.serialize_frontmatter({"provenance": provenance}), rendered_provenance
    ).replace("\n", newline)
    original, original_body, _marker = vault.parse_frontmatter(source, strict=True)
    assert list(original)[-1] == "provenance"
    assert original["provenance"] == provenance
    changes = {
        "omega_details": {"revision": 2},
        "delta_note": "First line.\nSecond line.",
        "alpha_tags": ["backfill", "reviewed"],
    }
    if operation == "change":
        changes["provenance"] = "Corrected provenance."
    delete_fields = ("provenance",) if operation == "delete" else ()

    # Returning successfully proves the fidelity guard accepts the splice.
    after = record_formats.render_markdown_item_update(
        source, changes, delete_fields=delete_fields
    )

    frontmatter, body, _marker = vault.parse_frontmatter(after, strict=True)
    expected = {name: value for name, value in original.items() if name not in delete_fields}
    expected.update(changes)
    assert frontmatter == expected
    assert list(frontmatter) == [
        name for name in original if name not in delete_fields
    ] + ["omega_details", "delta_note", "alpha_tags"]
    assert body == original_body
    if newline == "\r\n":
        assert "\n" not in after.replace("\r\n", "")
    else:
        assert "\r" not in after


def test_multifield_splice_still_refuses_an_unrequested_field_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = "---\nobsolete: |-\n  Remove this.\nkeep: unchanged\n---\nBody.\n"
    # Reproduce the old deletion-swallow bug: pretending the block span has
    # no terminator makes deletion consume the following field's whole line.
    monkeypatch.setattr(record_formats, "_span_terminator", lambda _text, _span: "")

    with pytest.raises(collections.CollectionError) as raised:
        record_formats.render_markdown_item_update(
            source,
            {"alpha": "first", "beta": ["second"], "gamma": {"third": True}},
            delete_fields=("obsolete",),
        )

    assert raised.value.code == "INVALID_RECORD_ITEM"
    assert "did not preserve the requested field set" in str(raised.value)
