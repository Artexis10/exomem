from __future__ import annotations

from exomem import markdown_relations


def test_canonical_relations_parse_with_line_and_target() -> None:
    document = markdown_relations.parse_markdown_relations(
        """\
## Relations
- depends_on [[Knowledge Base/Notes/Decisions/cache|Cache decision]]
- relates_to [[Knowledge Base/Notes/Insights/latency]]

## Finding
Body.
"""
    )

    assert document.is_valid
    assert [(r.kind, r.target, r.line, r.canonical) for r in document.relations] == [
        ("depends_on", "Knowledge Base/Notes/Decisions/cache", 2, True),
        ("relates_to", "Knowledge Base/Notes/Insights/latency", 3, True),
    ]


def test_inline_and_free_form_links_are_not_typed_relations() -> None:
    document = markdown_relations.parse_markdown_relations(
        "This builds on [[Earlier Work]].\n\n## Connections\n- Builds on [[Other Work]]\n"
    )

    assert document.relations == []
    assert document.errors == []


def test_canonical_section_rejects_unknown_or_non_snake_case_relations() -> None:
    document = markdown_relations.parse_markdown_relations(
        "## Relations\n- Depends-On [[A]]\n- dependson [[B]]\n"
    )

    assert document.relations == []
    assert [error.code for error in document.errors] == [
        "malformed_relation",
        "unsupported_relation",
    ]
    assert [error.line for error in document.errors] == [2, 3]


def test_canonical_section_rejects_missing_or_multiple_wikilinks() -> None:
    document = markdown_relations.parse_markdown_relations(
        "## Relations\n- depends_on Target\n- supports [[First]] [[Second]]\n"
    )

    assert document.relations == []
    assert [error.code for error in document.errors] == [
        "malformed_relation",
        "malformed_relation",
    ]
    assert [error.line for error in document.errors] == [2, 3]


def test_legacy_typed_bullet_remains_indexable_when_requested() -> None:
    document = markdown_relations.parse_markdown_relations(
        "## Finding\n\n- supports [[Earlier Finding]]\n",
        include_legacy=True,
    )

    assert [(r.kind, r.canonical) for r in document.relations] == [("supports", False)]
    assert document.errors == []


def test_fenced_relation_example_is_ignored() -> None:
    document = markdown_relations.parse_markdown_relations(
        "## Relations\n```markdown\n- depends_on [[Example]]\n```\n"
    )

    assert document.relations == []
    assert document.errors == []


def test_canonical_section_metadata_counts_valid_and_malformed_bullets() -> None:
    document = markdown_relations.parse_markdown_relations(
        "## Relations\n- supports [[Target]]\n- (none yet)\n\n## Finding\n- ordinary\n"
    )

    assert document.canonical_section_present is True
    assert document.canonical_bullet_count == 2
    assert [error.code for error in document.errors] == ["malformed_relation"]


def test_missing_and_empty_canonical_sections_are_distinct() -> None:
    missing = markdown_relations.parse_markdown_relations("# Note\n")
    empty = markdown_relations.parse_markdown_relations("# Note\n\n## Relations\n")

    assert (missing.canonical_section_present, missing.canonical_bullet_count) == (
        False,
        0,
    )
    assert (empty.canonical_section_present, empty.canonical_bullet_count) == (True, 0)
