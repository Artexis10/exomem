"""Schema-doc parser tests. The real schema docs are copied verbatim into fixtures
so any drift in the canonical text would surface here first.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import schema


def test_parses_real_schema_docs(source_schema: schema.SourceSchema) -> None:
    assert "source_type" in source_schema.required_fields
    assert "captured" in source_schema.required_fields

    assert "Sources/" in source_schema.location_pattern
    assert "YYYY-MM-DD" in source_schema.naming_pattern


def test_shipped_kinds_are_a_sample_not_a_whitelist(
    source_schema: schema.SourceSchema,
) -> None:
    """`source_types` reports the shipped defaults; it no longer gates anything.

    The vocabulary used to be scraped out of a markdown table with a token
    pattern that could not express a hyphen, which is why a multi-word kind was
    impossible and clearly classifiable material was forced into `other`. It now
    comes from `source_taxonomy`, so the doc is documentation again.
    """
    for legacy in ("article", "session", "book", "paper", "video", "other"):
        assert legacy in source_schema.source_types
    # Kinds the old markdown scrape could not have represented at all.
    assert "research-report" in source_schema.source_types
    assert "invoice-receipt" in source_schema.source_types


def test_validate_source_does_not_gate_the_kind_vocabulary(
    source_schema: schema.SourceSchema,
) -> None:
    """A previously unseen kind is not a schema violation."""
    assert (
        schema.validate_source(
            source_schema,
            content="x",
            source_type="field-notebook",
            title="t",
            url=None,
        )
        is None
    )


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


def test_validate_source_rejects_missing_url_when_the_kind_requires_one(
    source_schema: schema.SourceSchema,
) -> None:
    """URL conditionality is the resolved kind's own declared requirement.

    It used to be scraped from prose and hard-coded to three tokens, which
    reserved the property to three built-ins.
    """
    err = schema.validate_source(
        source_schema,
        content="x",
        source_type="article",
        title="t",
        url=None,
        requires_url=True,
    )
    assert err is not None
    assert "url" in err.missing


def test_validate_source_allows_missing_url_when_the_kind_does_not_require_one(
    source_schema: schema.SourceSchema,
) -> None:
    assert (
        schema.validate_source(
            source_schema,
            content="x",
            source_type="correspondence",
            title="t",
            url=None,
        )
        is None
    )


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
