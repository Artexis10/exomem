from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from record_presentation_fixtures import ITEM_KEY, manifest_text, setup_collection, values

from exomem import record_formats, records
from exomem import structured_collections as collections


def _replace(source: str, old: str, new: str) -> str:
    assert old in source
    return source.replace(old, new, 1)


def test_authoring_contract_exposes_closed_recipe_schema_and_generic_example() -> None:
    contract = collections.manifest_authoring_contract()
    schema = contract["json_schema"]["properties"]["record_presentation"]

    assert schema["additionalProperties"] is False
    assert schema["required"] == ["version", "tables"]
    assert schema["properties"]["version"] == {"const": 1}
    assert schema["properties"]["tables"]["maxItems"] == 8
    assert schema["properties"]["tables"]["items"]["properties"]["columns"]["maxItems"] == 16
    example = contract["examples"]["readable_nested_records"]
    assert example["record_presentation"]["tables"][0]["field"] == "observations"
    assert "diagnosis" not in json.dumps(example).lower()


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("record_presentation:\n  version: 1", "record_presentation:\n  version: 2"),
        ("  details:\n", "  unknown: true\n  details:\n"),
        ("    - field: measurements", "    - field: absent"),
        ("    - field: measurements", "    - field: subject"),
        ("        - field: name", "        - field: child_index"),
        ("          type: string", "          type: object"),
        ("          type: link\n          link_kind: note", "          type: link"),
        ("          type: string\n        - field: value", "          type: string\n          link_kind: note\n        - field: value"),
    ],
)
def test_invalid_recipe_shapes_refuse_before_any_item_mutation(
    tmp_path: Path, old: str, new: str
) -> None:
    text = _replace(manifest_text(), old, new)
    path = tmp_path / "Knowledge Base/Records/Observed/_collection.md"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(collections.CollectionError, match="INVALID_RECORD_PRESENTATION"):
        collections.load_manifest(tmp_path, path)

    assert path.read_bytes() == before
    assert not (path.parent / "Items").exists()


@pytest.mark.parametrize(
    ("section", "declared_field", "schema_field", "selected_value"),
    [
        (section, "measurements", "", [{"sentinel_private_key": "must-not-render"}])
        for section in ("summary", "notes", "details")
    ]
    + [
        (
            section,
            "metadata",
            "    metadata:\n      type: object\n",
            {"sentinel_private_key": "must-not-render"},
        )
        for section in ("summary", "notes", "details")
    ],
)
def test_parent_presentation_sections_refuse_non_scalar_declared_fields_without_nested_leak(
    tmp_path: Path,
    section: str,
    declared_field: str,
    schema_field: str,
    selected_value: object,
) -> None:
    text = manifest_text().replace("    note:\n", schema_field + "    note:\n", 1)
    original = {
        "summary": "    - field: subject\n      label: Subject",
        "notes": "    - field: note\n      label: Note",
        "details": "    - field: provenance\n      label: Provenance",
    }[section]
    replacement = f"    - field: {declared_field}\n      label: Selected"
    text = _replace(text, original, replacement)
    item_values = values()
    item_values[declared_field] = selected_value
    path = tmp_path / "Knowledge Base/Records/Observed/_collection.md"

    with pytest.raises(collections.CollectionError, match="INVALID_RECORD_PRESENTATION"):
        manifest = collections.parse_manifest_bytes(tmp_path, path, text.encode())
        rendered = record_formats.render_markdown_item(manifest, item_values, ITEM_KEY)
        assert "sentinel_private_key" not in rendered


def test_parent_scalar_runtime_type_drift_refuses_before_nested_sentinel_render(
    tmp_path: Path,
) -> None:
    manifest = setup_collection(tmp_path)
    item_values = values()
    item_values["subject"] = {"sentinel_private_key": "must-not-render"}

    with pytest.raises(collections.CollectionError, match="UNRENDERABLE_RECORD_PRESENTATION"):
        rendered = record_formats.render_markdown_item(manifest, item_values, ITEM_KEY)
        assert "sentinel_private_key" not in rendered


def test_recipe_is_records_markdown_items_only_and_legacy_presentation_stays_opaque(
    tmp_path: Path,
) -> None:
    legacy = manifest_text(presentation=False).replace(
        "item_schema:\n", "presentation:\n  renderer: custom\n  nested: {kept: true}\nitem_schema:\n"
    )
    legacy_path = tmp_path / "Knowledge Base/Records/Legacy/_collection.md"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(legacy, encoding="utf-8")
    parsed = collections.load_manifest(tmp_path, legacy_path)
    assert parsed.record_presentation is None

    planning_path = tmp_path / "Knowledge Base/Planning/Observed/_collection.md"
    planning_path.parent.mkdir(parents=True)
    for candidate_path, text in (
        (
            planning_path,
            manifest_text()
            .replace("semantic_profile: records", "semantic_profile: planning")
            .replace("Knowledge Base/Records", "Knowledge Base/Planning"),
        ),
        (legacy_path, manifest_text().replace("strategy: markdown-items", "strategy: dataset")),
    ):
        with pytest.raises(collections.CollectionError, match="INVALID_RECORD_PRESENTATION"):
            collections.parse_manifest_bytes(tmp_path, candidate_path, text.encode())


def test_normalized_recipe_projects_through_describe_and_revision_validation(tmp_path: Path) -> None:
    manifest = setup_collection(tmp_path)
    recipe = manifest.record_presentation
    assert recipe is not None
    assert recipe.summary == (("subject", "Subject"), ("observed_on", None))
    assert recipe.tables[0].columns[-1].link_kind == "note"

    described = collections.manifest_authoring_contract()["record_presentation"]
    assert described["version"] == 1
    without = manifest_text(presentation=False)
    path = tmp_path / manifest.path
    path.write_text(without, encoding="utf-8")
    current = collections.load_manifest(tmp_path, path)
    proposed = records.validate_collection_revision(tmp_path, current, manifest_text())
    assert proposed["valid"] is True
    assert proposed["lifecycle_guards"]["expected_manifest_hash"] == current.manifest_version.hash


def test_renderer_digest_sections_and_observed_value_fidelity_are_deterministic(tmp_path: Path) -> None:
    manifest = setup_collection(tmp_path)
    item_values = values()
    first = record_formats.render_markdown_item(manifest, item_values, ITEM_KEY, "Authored.\n")
    second = record_formats.render_markdown_item(manifest, item_values, ITEM_KEY, "Authored.\n")

    assert first == second
    span = record_formats._presentation_span(first)
    assert span is not None
    block = first[slice(*span)]
    digest = hashlib.sha256(
        json.dumps(
            record_formats._presentation_payload(manifest, item_values),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    assert f"digest=sha256:{digest}" in block
    assert "Sample &lt;A&gt;" in block
    assert "&lt;5" in block and "null" in block and "True" in block
    assert "unit/mL" in block and "fasting | repeated" in block
    assert "### Notes" in block and "<details>" in block
    assert "diagnosis" not in block.lower() and "advice" not in block.lower()
    assert "not projected" not in block


def test_managed_values_are_inert_markdown_in_every_section_and_keep_literal_fidelity(
    tmp_path: Path,
) -> None:
    manifest = setup_collection(tmp_path)
    attack = (
        "![remote](https://example.invalid/image.png) "
        "[link](https://example.invalid/page) <https://example.invalid/auto> "
        "[[Private/Target]] <img src=https://example.invalid/raw.png> "
        "**bold** _emphasis_ ~~strike~~ `code` # heading $math$ "
        "pipe|slash\\line\nnext"
    )
    item_values = values(child_count=1)
    item_values["subject"] = attack
    item_values["note"] = attack
    item_values["provenance"] = attack
    row = item_values["measurements"][0]  # type: ignore[index]
    row["name"] = attack
    row["value"] = attack
    row["source"] = "[[Private/Target]]"

    rendered = record_formats.render_markdown_item(manifest, item_values, ITEM_KEY)
    span = record_formats._presentation_span(rendered)
    assert span is not None
    block = rendered[slice(*span)]

    for active in (
        "![remote](",
        "[link](",
        "<https://example.invalid",
        "<img src=",
        "[[Private/Target]]",
        "**bold**",
        "_emphasis_",
        "~~strike~~",
        "`code`",
        "$math$",
    ):
        assert active not in block
    assert "remote" in block and "Private/Target" in block and "next" in block
    assert "<br>" in block
    assert record_formats._presentation_payload(manifest, item_values)["values"]["measurements"][0][
        "source"
    ] == "[[Private/Target]]"


@pytest.mark.parametrize(
    ("newline", "bom", "body", "final_newline"),
    [
        ("\n", False, "", False),
        ("\n", True, "\n", True),
        ("\n", False, "\n\nAuthored\n", True),
        ("\r\n", False, "\r\n\r\nAuthored\r\n", True),
        ("\r\n", True, "\r\nmarker-like <!-- exomem-record-presentation:v2 -->", False),
    ],
)
def test_source_splice_preserves_legacy_byte_shape_outside_managed_span(
    tmp_path: Path, newline: str, bom: bool, body: str, final_newline: bool
) -> None:
    manifest = setup_collection(tmp_path)
    prefix = "\ufeff" if bom else ""
    source = prefix + f"---{newline}type: record{newline}---" + body
    if final_newline and not source.endswith(newline):
        source += newline
    if not final_newline:
        source = source.rstrip("\r\n")

    rendered = record_formats.splice_record_presentation(source, manifest, values())
    span = record_formats._presentation_span(rendered)
    assert span is not None
    separator_start = span[0] - len(newline)
    assert rendered[separator_start : span[0]] == newline
    outside = rendered[:separator_start] + rendered[span[1] :]
    assert outside == source
    assert rendered.startswith(prefix)
    assert rendered.endswith(newline) is final_newline


@pytest.mark.parametrize(
    "body",
    [
        "<!-- /exomem-record-presentation -->",
        "<!-- exomem-record-presentation:v1 digest=sha256:" + "0" * 64 + " -->",
        (
            "<!-- exomem-record-presentation:v1 digest=sha256:"
            + "0" * 64
            + " -->\n<!-- exomem-record-presentation:v1 digest=sha256:"
            + "1" * 64
            + " -->\n<!-- /exomem-record-presentation -->"
        ),
        (
            "<!-- exomem-record-presentation:v1 digest=sha256:"
            + "0" * 64
            + " -->\n<!-- /exomem-record-presentation -->\n<!-- /exomem-record-presentation -->"
        ),
    ],
)
def test_malformed_exact_markers_refuse_rendering(tmp_path: Path, body: str) -> None:
    manifest = setup_collection(tmp_path)
    source = "---\ntype: record\n---\n" + body
    with pytest.raises(collections.CollectionError, match="MALFORMED_RECORD_PRESENTATION"):
        record_formats.splice_record_presentation(source, manifest, values())


def test_semantic_replay_does_not_strip_tampered_managed_bytes(tmp_path: Path) -> None:
    manifest = setup_collection(tmp_path)
    item_values = values()
    rendered = record_formats.render_markdown_item(manifest, item_values, ITEM_KEY, "Authored.\n")
    _frontmatter, body, _marker = __import__("exomem.vault", fromlist=["vault"]).parse_frontmatter(
        rendered, strict=True
    )
    assert records._semantic_body(body, manifest, item_values) == "Authored.\n"

    tampered = body.replace("digest=sha256:", "digest=sha256:0", 1)
    assert records._semantic_body(tampered, manifest, item_values) == tampered

    older_renderer = body.replace("Below threshold", "Older renderer wording", 1)
    assert records._semantic_body(older_renderer, manifest, item_values) == "Authored.\n"
