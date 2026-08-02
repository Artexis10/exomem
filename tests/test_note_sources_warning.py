"""Empty-provenance warning on compiled notes.

`references/frontmatter.md` marks `sources:` required for research-note, insight,
failure, and pattern, but nothing in the writer ever said so. These tests pin the
warning — and, just as importantly, pin that it never blocks a write, because a
conclusion drawn from live work with nothing captured is an honest empty list.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from exomem import note as note_module

TODAY = dt.date(2026, 5, 18)

SOURCES_REQUIRED_TYPES = ("research-note", "insight", "failure", "pattern")


def _sources_warning(warnings: list[str]) -> str | None:
    return next((w for w in warnings if "`sources:`" in w), None)


def _write(vault: Path, note_type: str, **kwargs):
    extra: dict = {}
    if note_type == "research-note":
        extra["project"] = "personal"
    if note_type == "experiment":
        extra.update(domain="health", started="2026-05-01", duration="30 days")
    if note_type == "production-log":
        extra["medium"] = "video"
    extra.update(kwargs)
    return note_module.note(
        vault,
        content="## Finding\n\nA durable conclusion drawn from live work.\n",
        note_type=note_type,
        title=f"Provenance check {note_type}",
        status=extra.pop("status", "draft"),
        today=TODAY,
        **extra,
    )


@pytest.mark.parametrize("note_type", SOURCES_REQUIRED_TYPES)
def test_empty_sources_warns_for_each_required_type(vault: Path, note_type: str) -> None:
    result = _write(vault, note_type)
    warning = _sources_warning(result.warnings)
    assert warning is not None, f"{note_type} warnings: {result.warnings}"
    assert note_type in warning


@pytest.mark.parametrize("note_type", ("experiment", "production-log"))
def test_empty_sources_is_silent_where_provenance_is_optional(
    vault: Path, note_type: str
) -> None:
    status = "planned" if note_type == "production-log" else "draft"
    result = _write(vault, note_type, status=status)
    assert _sources_warning(result.warnings) is None, result.warnings


def test_empty_sources_warning_never_blocks_the_write(vault: Path) -> None:
    result = _write(vault, "insight")
    assert (vault / result.path).is_file(), "the page must still be written"
    assert _sources_warning(result.warnings) is not None


def test_cited_sources_produce_no_warning(vault: Path) -> None:
    source = "Knowledge Base/Sources/Articles/2026-05-04-example-capture"
    (vault / source).parent.mkdir(parents=True, exist_ok=True)
    (vault / f"{source}.md").write_text(
        "---\ntype: source\nsource_type: article\ncaptured: 2026-05-04\n"
        "ingested_into: []\n---\n\n# Example capture\n",
        encoding="utf-8",
    )
    result = _write(vault, "insight", sources=[source])
    assert _sources_warning(result.warnings) is None, result.warnings


def test_write_feedback_reports_required_and_missing_provenance(vault: Path) -> None:
    missing = _write(vault, "failure")
    assert missing.write_feedback["sources"]["required"] is True
    assert missing.write_feedback["sources"]["missing"] is True

    optional = _write(vault, "experiment")
    assert optional.write_feedback["sources"]["required"] is False
    assert optional.write_feedback["sources"]["missing"] is False
