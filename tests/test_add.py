"""add tool tests — full SKILL.md rule-7 enforcement against the fixture KB."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from exomem import add as add_module
from exomem import schema as schema_module


TODAY = dt.date(2026, 5, 18)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_add_article_writes_source_file(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    result = add_module.add(
        vault,
        source_schema,
        content="Long-form article body about retrieval-augmented generation.",
        source_type="article",
        title="Agentic RAG explained",
        url="https://example.com/agentic-rag",
        tags=["rag", "agentic"],
        why_captured="Useful for the Q retrieval roadmap.",
        today=TODAY,
    )
    expected = vault / "Knowledge Base" / "Sources" / "Articles" / "2026-05-18-agentic-rag-explained.md"
    assert expected.exists()
    assert result.path == "Knowledge Base/Sources/Articles/2026-05-18-agentic-rag-explained.md"

    text = _read(expected)
    fm, _, body = text.partition("\n---\n")
    parsed = yaml.safe_load(fm.removeprefix("---\n"))
    assert parsed["type"] == "source"
    assert parsed["source_type"] == "article"
    assert parsed["captured"] == TODAY  # date type round-trips
    assert parsed["url"] == "https://example.com/agentic-rag"
    assert parsed["tags"] == ["rag", "agentic"]
    assert parsed["ingested_into"] == []
    assert "# Source: Agentic RAG explained" in body
    assert "Useful for the Q retrieval roadmap" in body
    assert "## Capture" in body


def test_add_session_no_url_ok(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    result = add_module.add(
        vault,
        source_schema,
        content="Pasted conversation about exomem scope.",
        source_type="session",
        title="exomem scope session",
        today=TODAY,
    )
    assert result.path.endswith(".md")
    assert "Sources/Sessions/" in result.path


def test_add_paper_auto_creates_folder(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    papers_dir = vault / "Knowledge Base" / "Sources" / "Papers"
    assert not papers_dir.exists()
    result = add_module.add(
        vault,
        source_schema,
        content="Abstract: novel retrieval method...",
        source_type="paper",
        title="A new retrieval method",
        url="https://arxiv.org/abs/2026.05001",
        today=TODAY,
    )
    assert papers_dir.is_dir()
    assert (papers_dir / "2026-05-18-a-new-retrieval-method.md").exists()


def test_add_video_auto_creates_folder(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    add_module.add(
        vault,
        source_schema,
        content="Video transcript content.",
        source_type="video",
        title="Talk on retrieval",
        url="https://youtube.com/watch?v=xyz",
        today=TODAY,
    )
    assert (vault / "Knowledge Base" / "Sources" / "Videos").is_dir()


def test_add_other_auto_creates_folder(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    add_module.add(
        vault,
        source_schema,
        content="Random capture.",
        source_type="other",
        title="Random note",
        today=TODAY,
    )
    assert (vault / "Knowledge Base" / "Sources" / "Other").is_dir()


def test_add_filename_collision_appends_suffix(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    common = dict(
        source_type="session",
        title="Conflict session",
        content="body",
        today=TODAY,
    )
    r1 = add_module.add(vault, source_schema, **common)
    r2 = add_module.add(vault, source_schema, **common)
    r3 = add_module.add(vault, source_schema, **common)
    assert r1.path.endswith("2026-05-18-conflict-session.md")
    assert r2.path.endswith("2026-05-18-conflict-session-2.md")
    assert r3.path.endswith("2026-05-18-conflict-session-3.md")


def test_add_rejects_invalid_source_type(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    with pytest.raises(add_module.AddError) as exc:
        add_module.add(
            vault,
            source_schema,
            content="x",
            source_type="bogus",
            title="t",
            today=TODAY,
        )
    assert exc.value.code == "INVALID_SOURCE"


def test_add_rejects_article_without_url(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    with pytest.raises(add_module.AddError) as exc:
        add_module.add(
            vault,
            source_schema,
            content="x",
            source_type="article",
            title="t",
            today=TODAY,
        )
    assert "url" in exc.value.missing


def test_add_updates_sources_index_count(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    sources_index = vault / "Knowledge Base" / "Sources" / "index.md"
    before = _read(sources_index)
    assert "Articles]] — captured web/PDF content (2)" in before

    add_module.add(
        vault,
        source_schema,
        content="body",
        source_type="article",
        title="New article",
        url="https://example.com/x",
        today=TODAY,
    )
    after = _read(sources_index)
    assert "Articles]] — captured web/PDF content (3)" in after
    assert "2026-05-18 — [[Knowledge Base/Sources/Articles/2026-05-18-new-article]]" in after


def test_add_updates_sources_index_for_new_folder(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    add_module.add(
        vault,
        source_schema,
        content="body",
        source_type="paper",
        title="New paper",
        url="https://arxiv.org/abs/x",
        today=TODAY,
    )
    after = _read(vault / "Knowledge Base" / "Sources" / "index.md")
    # By-type list should now include Papers with count 1
    assert "Papers]] — academic papers (1)" in after


def test_add_updates_top_index_recent_activity(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    top_index = vault / "Knowledge Base" / "index.md"
    add_module.add(
        vault,
        source_schema,
        content="body",
        source_type="session",
        title="Big session",
        today=TODAY,
    )
    text = _read(top_index)
    # New bullet at the top of Recent activity
    recent_idx = text.find("## Recent activity")
    assert recent_idx != -1
    next_h2 = text.find("\n## ", recent_idx + 1)
    section = text[recent_idx:next_h2]
    # First bullet under the section should be ours
    first_bullet = next(line for line in section.splitlines() if line.startswith("- "))
    assert "2026-05-18" in first_bullet
    assert "Big session" in first_bullet


def test_add_updates_top_index_counts(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    add_module.add(
        vault,
        source_schema,
        content="body",
        source_type="session",
        title="Counts test",
        today=TODAY,
    )
    text = _read(vault / "Knowledge Base" / "index.md")
    # Fixture baseline: Sources: 4 (articles: 2, books: 1, sessions: 1)
    # After adding one session: total 5, sessions 2
    assert "- Sources: 5" in text
    assert "sessions: 2" in text


def test_add_appends_to_log(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    log_file = vault / "Knowledge Base" / "log.md"
    before = _read(log_file)
    add_module.add(
        vault,
        source_schema,
        content="body",
        source_type="session",
        title="Log test",
        why_captured="Testing log append",
        today=TODAY,
    )
    after = _read(log_file)
    # New entry should appear before the previous most-recent (2026-05-10)
    new_idx = after.find("## [2026-05-18] add | Sources/Sessions/2026-05-18-log-test")
    old_idx = after.find("## [2026-05-10] note")
    assert new_idx != -1
    assert new_idx < old_idx
    # Header lines preserved
    assert after.startswith("# Knowledge Base — Activity Log")
    assert "Testing log append" in after


def test_add_full_rule7_atomic_all_four_files_updated(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    """One add → four files mutated."""
    src_index = vault / "Knowledge Base" / "Sources" / "index.md"
    top_index = vault / "Knowledge Base" / "index.md"
    log_file = vault / "Knowledge Base" / "log.md"

    before = {p: _read(p) for p in (src_index, top_index, log_file)}
    result = add_module.add(
        vault,
        source_schema,
        content="atomicity check",
        source_type="book",
        title="Atomicity",
        today=TODAY,
    )
    source_file = vault / result.path

    assert source_file.exists()
    for p, prev in before.items():
        assert _read(p) != prev, f"{p.name} should have been updated"


def test_add_tag_normalization(
    vault: Path, source_schema: schema_module.SourceSchema
) -> None:
    result = add_module.add(
        vault,
        source_schema,
        content="body",
        source_type="session",
        title="Tag test",
        tags=["UPPER", "with space", "with_under", "dupe", "DUPE"],
        today=TODAY,
    )
    text = _read(vault / result.path)
    fm = text.split("\n---\n")[0].removeprefix("---\n")
    parsed = yaml.safe_load(fm)
    assert parsed["tags"] == ["upper", "with-space", "with-under", "dupe"]
