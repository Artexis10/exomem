"""The Source backlog in the maintained due-state projection.

A write to a note refreshes the rows of the Sources that note cites, so the served
count falls when a live note compiles a Source and holds when a draft does. Dismissing
items lowers what is visible but never what is outstanding.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import commands, set_frontmatter_field
from exomem import due_state as due_state_module
from exomem import note as note_module

pytestmark = pytest.mark.usefixtures("obligations_only")


def _import_sources(vault: Path, count: int) -> list[str]:
    paths = []
    for index in range(count):
        rel = f"Knowledge Base/Sources/Articles/document-{index}.md"
        target = vault / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "---\ntype: source\ningested_into: []\ncaptured: 2026-01-01\n---\n\n"
            f"# Document {index}\n\nImported text {index}.\n",
            encoding="utf-8",
        )
        paths.append(rel)
    return paths


def _compile(vault: Path, title: str, *, status: str, source: str) -> str:
    """Write a note that cites `source` through the governed writer."""
    arguments = {
        "content": "## Claim\n\nCompiled from the import.\n",
        "note_type": "insight",
        "title": title,
        "status": status,
        "sources": [source.removesuffix(".md")],
    }
    validation = note_module.note(vault, validate_only=True, **arguments)
    return note_module.note(
        vault,
        **arguments,
        draft_id=validation.draft_id,
        draft_hash=validation.draft_hash,
        draft_token=validation.draft_token,
        relation_disposition="reviewed_none",
        relation_review_hash=validation.draft_hash,
        relation_review_reason="No relation exists in this fixture.",
    ).path


def _block(vault: Path) -> dict:
    return due_state_module.served(vault) or {}


def _outstanding_sources(vault: Path) -> int:
    return _block(vault).get("categories", {}).get("unprocessed_source", 0)


@pytest.fixture
def imported(obligations_only: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[str]]:
    """Three outstanding Sources in a built projection, with every full rebuild disabled."""
    vault = obligations_only
    sources = _import_sources(vault, 3)
    due_state_module.reconcile(vault)

    def no_rebuild(*_args, **_kwargs):
        raise AssertionError("a write must refresh its Sources without a vault-wide rebuild")

    monkeypatch.setattr(due_state_module, "reconcile", no_rebuild)
    monkeypatch.setattr(due_state_module, "recompute", no_rebuild)
    return vault, sources


def test_a_live_note_citing_a_source_lowers_the_served_count(
    imported: tuple[Path, list[str]],
) -> None:
    vault, sources = imported
    assert _outstanding_sources(vault) == 3

    _compile(vault, "Compiled claim", status="active", source=sources[0])

    assert _outstanding_sources(vault) == 2
    assert _block(vault)["outstanding"]["unprocessed_source"] == 2


def test_a_draft_note_citing_a_source_leaves_the_count_unchanged_until_promoted(
    imported: tuple[Path, list[str]],
) -> None:
    vault, sources = imported
    draft = _compile(vault, "Draft claim", status="draft", source=sources[0])
    assert _outstanding_sources(vault) == 3

    arguments = {"path": draft, "field": "status", "value": "active", "why": "reviewed and promoted"}
    preview = set_frontmatter_field.set_frontmatter_field(vault, validate_only=True, **arguments)
    set_frontmatter_field.set_frontmatter_field(
        vault,
        **arguments,
        semantic_transition_token=preview.semantic["transition_token"],
        relation_disposition="reviewed_none",
        relation_review_hash=preview.semantic["relation_review_hash"],
        relation_review_reason="No relation exists in this fixture.",
    )

    assert _outstanding_sources(vault) == 2


def test_trashing_the_live_note_returns_its_source_to_the_count(
    imported: tuple[Path, list[str]],
) -> None:
    vault, sources = imported
    note = _compile(vault, "Compiled claim", status="active", source=sources[0])
    assert _outstanding_sources(vault) == 2

    # The Source still names the note, as any cited derivative does.
    commands.op_delete(vault, note, confirm=True, force_orphan=True)

    assert _outstanding_sources(vault) == 3


def test_dismissing_items_lowers_what_is_visible_and_keeps_outstanding(
    imported: tuple[Path, list[str]],
) -> None:
    vault, _sources = imported
    refs = [row["ref"] for row in due_state_module.served_entries(vault)]
    assert len(refs) == 3

    for ref in refs[:2]:
        commands.op_triage_memory(vault, ref=ref, action="dismiss", why="grouped later")

    block = _block(vault)
    assert block["total"] == 1
    assert block["categories"] == {"unprocessed_source": 1}
    assert block["outstanding_total"] == 3
    assert block["outstanding"] == {"unprocessed_source": 3}
