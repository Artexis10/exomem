"""Tests for sub-folder index auto-refresh in indexes.py.

The fixture vault doesn't ship with Notes/index.md or Entities/index.md
(a populated vault does). These tests synthesize the files into the
fixture, exercise the write tools, and confirm the indexes stay in sync.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from exomem import indexes, link as link_module, note as note_module


TODAY = dt.date(2026, 5, 27)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _seed_notes_index(vault: Path) -> Path:
    """Inject a Notes/index.md shaped like the real vault."""
    p = vault / "Knowledge Base" / "Notes" / "index.md"
    p.write_text(
        "# Notes — Index\n\n"
        "## By type\n\n"
        "### Research — project-or-domain-scoped synthesis (2)\n\n"
        "- [[Knowledge Base/Notes/Research/Project Alpha/|Project Alpha]] (1) — engine stuff\n"
        "- [[Knowledge Base/Notes/Research/Health/|Health]] (1) — health research\n\n"
        "### Insights — distilled cross-cutting lessons (1)\n\n"
        "### Failures — documented failure modes (1)\n\n"
        "### Patterns — reusable patterns (1)\n\n"
        "### Productions — creative artifacts (1)\n",
        encoding="utf-8",
    )
    return p


def _seed_entities_index(vault: Path) -> Path:
    p = vault / "Knowledge Base" / "Entities" / "index.md"
    p.write_text(
        "# Entities — Index\n\n"
        "## By type\n\n"
        "- [[Knowledge Base/Entities/People/|People]] (1)\n"
        "- [[Knowledge Base/Entities/Concepts/|Concepts]] (1)\n"
        "- [[Knowledge Base/Entities/Libraries/|Libraries]] (0)\n"
        "- [[Knowledge Base/Entities/Decisions/|Decisions]] — lightweight ADRs (0)\n",
        encoding="utf-8",
    )
    return p


def test_note_updates_top_index_notes_count(vault: Path) -> None:
    """Writing a new insight bumps `- Notes (insight): N` in the top index."""
    top = vault / "Knowledge Base" / "index.md"
    before = top.read_text(encoding="utf-8")
    assert "- Notes (insight): 4" in before

    note_module.note(
        vault,
        content="# t\n\n## Claim\n\nBody.\n",
        note_type="insight",
        title="New insight for count",
        status="draft",
        today=TODAY,
    )
    after = top.read_text(encoding="utf-8")
    assert "- Notes (insight): 5" in after, after


def test_link_updates_top_index_entities_count(vault: Path) -> None:
    """Writing a new person bumps `- Entities (person): N` in the top index."""
    top = vault / "Knowledge Base" / "index.md"
    before = top.read_text(encoding="utf-8")
    assert "- Entities (person): 2" in before

    link_module.link(
        vault,
        entity_type="person",
        name="Test Person For Count",
        summary="x",
        today=TODAY,
    )
    after = top.read_text(encoding="utf-8")
    assert "- Entities (person): 3" in after, after


def test_note_updates_notes_subindex_h3_count(vault: Path) -> None:
    """Writing an insight bumps `### Insights — ... (N)` in Notes/index.md."""
    notes_idx = _seed_notes_index(vault)

    note_module.note(
        vault,
        content="# t\n\n## Claim\n\nBody.\n",
        note_type="insight",
        title="Insight that bumps subindex",
        status="draft",
        today=TODAY,
    )
    text = notes_idx.read_text(encoding="utf-8")
    assert "### Insights — distilled cross-cutting lessons (5)" in text, text


def test_note_updates_notes_subindex_subfolder_count(vault: Path) -> None:
    """Writing a research-note bumps the per-project bullet count."""
    notes_idx = _seed_notes_index(vault)

    note_module.note(
        vault,
        content="# t\n\n## Question\n\nBody.\n",
        note_type="research-note",
        title="New project-alpha finding",
        project="project-alpha",
        status="draft",
        today=TODAY,
    )
    text = notes_idx.read_text(encoding="utf-8")
    assert "- [[Knowledge Base/Notes/Research/Project Alpha/|Project Alpha]] (2) — engine stuff" in text, text
    # The Research H3 header recounts all research notes from disk (4 fixtures +
    # this new one = 5).
    assert "### Research — project-or-domain-scoped synthesis (5)" in text, text


def test_link_updates_entities_subindex_bullet(vault: Path) -> None:
    """Writing a concept bumps `- [[link|Concepts]] (N)` in Entities/index.md."""
    entities_idx = _seed_entities_index(vault)

    link_module.link(
        vault,
        entity_type="concept",
        name="Test Concept For Subindex",
        summary="x",
        today=TODAY,
    )
    text = entities_idx.read_text(encoding="utf-8")
    assert "- [[Knowledge Base/Entities/Concepts/|Concepts]] (4)" in text, text


def test_subindex_refresh_supports_nested_obsidian_root(vault: Path) -> None:
    (vault / "Knowledge Base" / ".obsidian").mkdir()
    notes_idx = _seed_notes_index(vault)
    entities_idx = _seed_entities_index(vault)

    note_module.note(
        vault,
        content="# t\n\n## Question\n\nBody.\n",
        note_type="research-note",
        title="Nested root subindex note",
        project="project-alpha",
        status="draft",
        today=TODAY,
    )
    link_module.link(
        vault,
        entity_type="concept",
        name="Nested Root Subindex Concept",
        summary="x",
        today=TODAY,
    )

    notes_text = notes_idx.read_text(encoding="utf-8")
    entities_text = entities_idx.read_text(encoding="utf-8")
    assert "[[Notes/Research/Project Alpha/|Project Alpha]] (2)" in notes_text
    assert "[[Entities/Concepts/|Concepts]] (4)" in entities_text
    assert "[[Knowledge Base/" not in notes_text + entities_text


def test_subindex_preserves_hand_curated_descriptions(vault: Path) -> None:
    """The auto-refresh must not touch the `— description` tail on bullets."""
    notes_idx = _seed_notes_index(vault)

    note_module.note(
        vault,
        content="# t\n\n## Question\n\nBody.\n",
        note_type="research-note",
        title="Yet another project-alpha note",
        project="project-alpha",
        status="draft",
        today=TODAY,
    )
    text = notes_idx.read_text(encoding="utf-8")
    # Description tail "— engine stuff" must still be there.
    assert "Project Alpha]] (2) — engine stuff" in text, text


def test_subindex_skip_when_missing(vault: Path) -> None:
    """Writers do not error when Notes/index.md or Entities/index.md are absent.

    The fixture intentionally lacks these — they're optional sub-indexes
    that only some vaults maintain."""
    # No seeding here — both sub-indexes absent. Operation must succeed.
    result = note_module.note(
        vault,
        content="# t\n\n## Claim\n\nBody.\n",
        note_type="insight",
        title="Works without subindex",
        status="draft",
        today=TODAY,
    )
    assert (vault / result.path).exists()


def test_count_entities_helper(vault: Path) -> None:
    """`_count_entities` should mirror `_count_sources`'s shape."""
    counts = indexes._count_entities(vault / "Knowledge Base" / "Entities")
    # Fixture has 2 people + 3 concepts; libraries/decisions folders may not exist.
    assert counts.get("person", 0) == 2
    assert counts.get("concept", 0) == 3


def test_count_notes_by_subfolder_helper(vault: Path) -> None:
    """Nested counts for Research/Experiments/Productions; flat for others."""
    counts = indexes._count_notes_by_subfolder(vault / "Knowledge Base" / "Notes")
    # Fixture has Research/Project Alpha (1), Research/Health (1), Research/Infrastructure (2).
    assert counts.get("Research", {}).get("Project Alpha") == 1
    assert counts.get("Research", {}).get("Health") == 1
    assert counts.get("Research", {}).get("Infrastructure") == 2
    # Flat types: Insights, Failures, Patterns — single "" key.
    assert "Insights" in counts
    assert counts["Insights"].get("") == 4


def test_no_obsolete_counts_warning_on_note(vault: Path) -> None:
    """Phase 2 removed the 'Counts in index.md not auto-updated' warning."""
    result = note_module.note(
        vault,
        content="# t\n\n## Claim\n\nB.\n",
        note_type="insight",
        title="No stale counts warning",
        status="draft",
        today=TODAY,
    )
    for w in result.warnings:
        assert "not auto-updated" not in w, w
