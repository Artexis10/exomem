"""note tool tests — covers auto-H1 removal, slug truncation, dry_run."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from exomem import find as find_module
from exomem import note as note_module


TODAY = dt.date(2026, 5, 18)


def _body_after_frontmatter(text: str) -> str:
    """Return the body markdown (everything after the closing `---\\n`)."""
    fm_end = text.find("\n---\n", 4)  # skip the opening "---"
    return text[fm_end + len("\n---\n"):]


def test_note_writes_caller_h1_verbatim_no_duplicate(vault: Path) -> None:
    """The caller supplies the H1 in content; the tool must not prepend one."""
    result = note_module.note(
        vault,
        content=(
            "# A note about retrieval pipelines\n"
            "\n"
            "## Question\n"
            "\n"
            "Does HyDE beat dense retrieval on your corpora?\n"
        ),
        note_type="insight",
        title="A note about retrieval pipelines",
        today=TODAY,
    )
    text = (vault / result.path).read_text(encoding="utf-8")
    body = _body_after_frontmatter(text)
    # Exactly one H1 line, and it's the caller's.
    h1_count = sum(1 for ln in body.splitlines() if ln.startswith("# "))
    assert h1_count == 1, body
    assert body.lstrip().startswith("# A note about retrieval pipelines")


def test_note_body_with_no_h1_is_written_verbatim(vault: Path) -> None:
    """If the caller declines to supply an H1, the tool doesn't invent one."""
    result = note_module.note(
        vault,
        content="## Claim\n\nNo H1 today.\n",
        note_type="insight",
        title="No H1 today",
        today=TODAY,
    )
    text = (vault / result.path).read_text(encoding="utf-8")
    body = _body_after_frontmatter(text)
    assert "# No H1 today" not in body
    assert body.lstrip().startswith("## Claim")


def test_note_slug_truncation_emits_warning(vault: Path) -> None:
    """A title that exceeds SLUG_MAX_LENGTH should produce a slug_warning."""
    very_long_title = (
        "Procedural pushback and evidentiary recording in "
        "opponent-controlled meetings spanning multiple jurisdictions "
        "and overlapping privilege regimes for the discovery period"
    )
    result = note_module.note(
        vault,
        content="# title H1\n\n## Claim\n\nBody.\n",
        note_type="insight",
        title=very_long_title,
        today=TODAY,
    )
    # Path is on disk (truncated form).
    assert (vault / result.path).exists()
    # At least one warning mentions slug truncation.
    assert any(
        "slug truncated" in w.lower() for w in result.warnings
    ), result.warnings


def test_note_short_title_no_truncation_warning(vault: Path) -> None:
    result = note_module.note(
        vault,
        content="# t\n\n## Claim\n\nB.\n",
        note_type="insight",
        title="Short title",
        today=TODAY,
    )
    assert not any(
        "slug truncated" in w.lower() for w in result.warnings
    )


def test_note_no_cap50_warning(vault: Path) -> None:
    """The 'Recent activity trimmed at cap-50' warning was per-write noise; gone now."""
    # The fixture log/index has fewer than 50 entries; no trim should happen.
    # But even if one did, we don't want the warning surfaced.
    result = note_module.note(
        vault,
        content="# t\n",
        note_type="insight",
        title="cap warning test",
        today=TODAY,
    )
    assert not any("trimmed at cap-50" in w for w in result.warnings)


def test_note_without_links_or_sources_does_not_build_resolver(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plain note creation should not pay a full-vault resolver build."""

    def fail_shared_resolver(_vault: Path):
        raise AssertionError("shared resolver should not be built")

    monkeypatch.setattr(find_module, "shared_resolver", fail_shared_resolver)
    result = note_module.note(
        vault,
        content="# Resolver-free note\n\n## Claim\n\nNo wikilinks here.\n",
        note_type="insight",
        title="Resolver-free note",
        today=TODAY,
    )

    assert (vault / result.path).exists()
