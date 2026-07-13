from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from exomem.semantic_blocks import parse_semantic_blocks
from exomem.semantic_units import canonicalize_category, parse_semantic_units


def test_unicode_categories_preserve_raw_and_share_a_canonical_key() -> None:
    document = parse_semantic_units(
        "- [Äri Reegel] First\n- [äri-reegel] Second\n",
        path="Knowledge Base/Notes/Test.md",
    )

    assert document.is_valid
    assert [unit.category_raw for unit in document.units] == ["Äri Reegel", "äri-reegel"]
    assert [unit.category_key for unit in document.units] == ["äri_reegel", "äri_reegel"]
    assert [unit.category for unit in document.units] == ["äri_reegel", "äri_reegel"]
    assert canonicalize_category("  ＣＯＮＦＩＧ---Value  ") == "config_value"


def test_category_length_boundary_is_64_unicode_codepoints() -> None:
    category_64 = "a" * 64
    category_65 = "a" * 65

    document = parse_semantic_units(
        f"- [{category_64}] accepted\n- [{category_65}] rejected\n",
        path="length.md",
    )

    assert [unit.category_raw for unit in document.units] == [category_64]
    assert [(error.code, error.line) for error in document.errors] == [
        ("invalid_compact_category", 2)
    ]
    assert document.errors[0].raw == f"- [{category_65}] rejected"


def test_trailing_unicode_and_path_tags_are_structured_in_order() -> None:
    document = parse_semantic_units("- [config] Cache policy #ümlaut #路径/子-1\n")

    unit = document.units[0]
    assert unit.content == "Cache policy"
    assert unit.tags == ("ümlaut", "路径/子-1")


def test_invalid_or_nontrailing_tag_like_text_remains_content() -> None:
    document = parse_semantic_units(
        "- [config] Use #valid before prose\n"
        "- [config] Value has embedded#hash\n"
        "- [config] Keep #valid #bad//path\n"
    )

    assert [unit.tags for unit in document.units] == [(), (), ()]
    assert [unit.content for unit in document.units] == [
        "Use #valid before prose",
        "Value has embedded#hash",
        "Keep #valid #bad//path",
    ]


def test_suffixes_parse_from_anchor_to_context_to_tags() -> None:
    document = parse_semantic_units(
        r"- [rule] Keep \(literal\) #one #路径/二 (outer (inner)) ^anchor-1" "\n"
    )

    unit = document.units[0]
    assert unit.content == r"Keep \(literal\)"
    assert unit.tags == ("one", "路径/二")
    assert unit.context == "outer (inner)"
    assert unit.anchor == "anchor-1"


def test_escaped_and_nonfinal_parentheses_remain_content() -> None:
    document = parse_semantic_units(
        r"- [rule] Keep \(literal\) here" "\n"
        "- [rule] Use (draft) before release\n"
    )

    assert [unit.context for unit in document.units] == [None, None]
    assert [unit.content for unit in document.units] == [
        r"Keep \(literal\) here",
        "Use (draft) before release",
    ]


def test_only_valid_terminal_anchors_are_structured() -> None:
    document = parse_semantic_units(
        "- [term] Valid ^a-1\n"
        "- [term] Invalid ^bad_\n"
        "- [term] Also invalid ^-bad\n"
    )

    assert [unit.anchor for unit in document.units] == ["a-1", None, None]
    assert [unit.content for unit in document.units] == [
        "Valid",
        "Invalid ^bad_",
        "Also invalid ^-bad",
    ]


def test_bullets_parse_anywhere_with_supported_markers_and_indentation() -> None:
    markdown = """\
# Title

- [config] Dash
  * [rule] Star
   + [term] Plus
"""

    document = parse_semantic_units(markdown)

    assert [unit.content for unit in document.units] == ["Dash", "Star", "Plus"]
    assert all(unit.kind == "observation" for unit in document.units)
    assert all(unit.form == "compact" for unit in document.units)


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_observations_inside_fences_are_ignored(fence: str) -> None:
    markdown = (
        f"{fence}markdown\n- [config] Example only\n{fence}\n"
        "- [config] Authored\n"
    )

    document = parse_semantic_units(markdown)

    assert [unit.content for unit in document.units] == ["Authored"]


def test_task_boxes_and_workflow_rows_are_ordinary_markdown() -> None:
    markdown = """\
- [ ] Todo
- [x] Done
- [X] Done
- [-] Cancelled
- [take: ] Review queue
- [!] Admonition
- [?] Question
"""

    document = parse_semantic_units(markdown)

    assert document.units == ()
    assert document.errors == ()
    assert document.warnings == ()


def test_malformed_category_candidates_and_empty_content_are_diagnostics() -> None:
    invalid_unicode_category = "e\N{COMBINING ACUTE ACCENT}"
    markdown = (
        f"- [{invalid_unicode_category}] invalid category\n"
        "- [config] #tag (context) ^anchor\n"
    )

    document = parse_semantic_units(markdown, path="bad.md")

    assert document.units == ()
    assert [(error.code, error.line) for error in document.errors] == [
        ("invalid_compact_category", 1),
        ("empty_compact_observation", 2),
    ]
    assert all(error.path == "bad.md" for error in document.errors)
    assert all(error.severity == "error" for error in document.errors)
    assert all(error.span is not None for error in document.errors)
    assert all(error.remediation for error in document.errors)


def test_compact_source_span_uses_full_file_codepoint_coordinates() -> None:
    markdown = "α\n- [Äri] café\n"

    unit = parse_semantic_units(markdown).units[0]

    assert unit.span.start_line == 2
    assert unit.span.start_column == 1
    assert unit.span.end_line == 2
    assert unit.span.end_column == len("- [Äri] café") + 1
    assert unit.span.start_offset == len("α\n")
    assert unit.span.end_offset == len("α\n- [Äri] café")
    assert unit.span.text == "- [Äri] café"
    assert len(unit.source_hash) == 64


def test_compact_and_rich_units_share_shape_but_not_governed_kind() -> None:
    markdown = """\
- [decision] Use SQLite

## Decision
- category: config
- id: d1
- status: active
- relations: supports: [[Architecture]]

Use PostgreSQL.
"""

    document = parse_semantic_units(markdown)

    assert [unit.form for unit in document.units] == ["compact", "rich"]
    compact, rich = document.units
    assert (compact.category, compact.kind) == ("decision", "observation")
    assert (rich.category_raw, rich.category_key, rich.category, rich.kind) == (
        "config",
        "config",
        "config",
        "decision",
    )
    assert rich.anchor == "d1"
    assert rich.content == "Use PostgreSQL."
    assert rich.body == "Use PostgreSQL."
    assert rich.metadata["status"] == "active"
    assert rich.relations[0].kind == "supports"
    assert rich.relations[0].target == "[[Architecture]]"


def test_rich_category_defaults_to_kind_and_canonicalizes_override() -> None:
    markdown = """\
## Claim

Default category.

## Decision
- category: Äri-Reegel

Override category.
"""

    document = parse_semantic_units(markdown)

    assert [(unit.kind, unit.category_raw, unit.category) for unit in document.units] == [
        ("claim", "claim", "claim"),
        ("decision", "Äri-Reegel", "äri_reegel"),
    ]


def test_invalid_rich_category_reports_without_losing_legacy_block() -> None:
    markdown = "## Decision\n- category: invalid/category\n\nKeep the decision.\n"

    document = parse_semantic_units(markdown, path="decision.md")

    assert len(document.units) == 1
    assert document.units[0].category == "decision"
    assert [(error.code, error.line) for error in document.errors] == [
        ("invalid_rich_category", 2)
    ]
    assert document.semantic_blocks == [parse_semantic_blocks(markdown).blocks[0].to_dict()]


def test_legacy_projection_is_exact_rich_only_and_nonduplicating() -> None:
    markdown = """\
- [claim] Compact is not legacy

## Claim
- id: c1
- status: active
- relations: supports: [[A]]

Rich body.
"""
    legacy = parse_semantic_blocks(markdown)

    document = parse_semantic_units(markdown)

    assert document.semantic_blocks == [block.to_dict() for block in legacy.blocks]
    assert document.legacy_semantic_blocks == document.semantic_blocks
    assert len(document.semantic_blocks) == 1
    assert len([unit for unit in document.units if unit.form == "rich"]) == 1


def test_legacy_diagnostics_are_normalized_with_source_context() -> None:
    markdown = "## Claim\n- relations: agrees_with: [[A]]\n\nBody.\n"

    document = parse_semantic_units(markdown, path="claim.md")

    assert len(document.errors) == 1
    error = document.errors[0]
    assert error.code == "unsupported_relation"
    assert error.path == "claim.md"
    assert error.line == 2
    assert error.span is not None
    assert error.raw == "- relations: agrees_with: [[A]]"
    assert error.remediation


def test_units_are_source_ordered_across_compact_and_rich_forms() -> None:
    markdown = """\
- [term] First

## Claim

Second.

# Notes

- [rule] Third
"""

    document = parse_semantic_units(markdown)

    assert [(unit.form, unit.content) for unit in document.units] == [
        ("compact", "First"),
        ("rich", "Second."),
        ("compact", "Third"),
    ]


def test_parse_output_is_frozen_and_byte_stable() -> None:
    markdown = "- [config] Value #tag (ctx) ^id\n"

    first = parse_semantic_units(markdown, path="stable.md")
    second = parse_semantic_units(markdown, path="stable.md")

    assert json.dumps(first.to_dict(), ensure_ascii=False, sort_keys=True) == json.dumps(
        second.to_dict(), ensure_ascii=False, sort_keys=True
    )
    with pytest.raises(FrozenInstanceError):
        first.units[0].content = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        first.units[0].metadata["new"] = "value"  # type: ignore[index]


def test_validate_false_still_excludes_malformed_units_without_diagnostics() -> None:
    document = parse_semantic_units("- [config]\n", validate=False)

    assert document.units == ()
    assert document.errors == ()
    assert document.warnings == ()
