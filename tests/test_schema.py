"""Schema-doc parser tests. The real schema docs are copied verbatim into fixtures
so any drift in the canonical text would surface here first.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kb_mcp import schema


def test_parses_real_schema_docs(source_schema: schema.SourceSchema) -> None:
    assert "article" in source_schema.source_types
    assert "session" in source_schema.source_types
    assert "book" in source_schema.source_types
    assert "paper" in source_schema.source_types
    assert "video" in source_schema.source_types
    assert "other" in source_schema.source_types

    assert "source_type" in source_schema.required_fields
    assert "captured" in source_schema.required_fields

    # url is conditional on article/paper/video per the spec
    assert "article" in source_schema.conditional_url_types
    assert "video" in source_schema.conditional_url_types
    assert "paper" in source_schema.conditional_url_types

    assert "Sources/" in source_schema.location_pattern
    assert "YYYY-MM-DD" in source_schema.naming_pattern


def test_raises_when_schema_doc_missing(tmp_path: Path) -> None:
    # No Knowledge Base/_Schema directory at all
    with pytest.raises(schema.SchemaParseError):
        schema.load_source_schema(tmp_path)


def test_validate_source_accepts_valid_article(
    source_schema: schema.SourceSchema,
) -> None:
    err = schema.validate_source(
        source_schema,
        content="some body",
        source_type="article",
        title="Hello",
        url="https://example.com",
    )
    assert err is None


def test_validate_source_rejects_unknown_type(
    source_schema: schema.SourceSchema,
) -> None:
    err = schema.validate_source(
        source_schema,
        content="x",
        source_type="bogus",
        title="t",
        url=None,
    )
    assert err is not None
    assert err.code == "INVALID_SOURCE"
    assert "source_type" in err.missing


def test_validate_source_rejects_article_without_url(
    source_schema: schema.SourceSchema,
) -> None:
    err = schema.validate_source(
        source_schema,
        content="x",
        source_type="article",
        title="t",
        url=None,
    )
    assert err is not None
    assert "url" in err.missing


def test_validate_source_rejects_empty_content(
    source_schema: schema.SourceSchema,
) -> None:
    err = schema.validate_source(
        source_schema,
        content="",
        source_type="session",
        title="t",
        url=None,
    )
    assert err is not None
    assert "content" in err.missing


def test_validate_source_rejects_empty_title(
    source_schema: schema.SourceSchema,
) -> None:
    err = schema.validate_source(
        source_schema,
        content="x",
        source_type="session",
        title="   ",
        url=None,
    )
    assert err is not None
    assert "title" in err.missing


def test_validate_source_book_no_url_required(
    source_schema: schema.SourceSchema,
) -> None:
    err = schema.validate_source(
        source_schema,
        content="x",
        source_type="book",
        title="t",
        url=None,
    )
    assert err is None
