from __future__ import annotations

import inspect
import uuid
from pathlib import Path

import pytest

from exomem import commands

REVIEW_FIELDS = {
    "validate_only",
    "draft_id",
    "draft_hash",
    "draft_token",
    "relation_disposition",
    "relation_review_hash",
    "relation_review_reason",
}


def _write_semantic_page(root: Path) -> None:
    path = root / "Knowledge Base" / "Notes" / "semantic-surface.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "type: insight\n"
        "title: Semantic surface\n"
        f"exomem_id: {uuid.uuid4()}\n"
        "status: active\n"
        "updated: 2026-07-16\n"
        "metadata:\n"
        "  priority: 3\n"
        "---\n\n"
        "# Semantic surface\n\n"
        "## Decision\n"
        "- category: config\n"
        "- id: public-recall\n\n"
        "Use SQLite WAL mode for public recall.\n",
        encoding="utf-8",
    )


def test_ask_memory_returns_filtered_semantic_units(tmp_path: Path) -> None:
    _write_semantic_page(tmp_path)

    hits = commands.op_ask_memory(
        tmp_path,
        query="SQLite",
        mode="keyword",
        scope="kb-only",
        categories=["config"],
        kinds=["decision"],
        filters={"page.frontmatter:/metadata/priority": {"$eq": 3}},
        result_level="unit",
    )

    assert isinstance(hits, list)
    assert len(hits) == 1
    assert hits[0]["result_type"] == "semantic_unit"
    assert hits[0]["category"] == "config"
    assert hits[0]["kind"] == "decision"
    assert hits[0]["parent_path"].endswith("semantic-surface.md")


def test_deep_semantic_unit_recall_fails_with_a_stable_error(tmp_path: Path) -> None:
    _write_semantic_page(tmp_path)

    with pytest.raises(ValueError, match="PACK_REQUIRES_PAGE_RESULTS"):
        commands.op_ask_memory(
            tmp_path,
            query="SQLite",
            mode="keyword",
            scope="kb-only",
            categories=["config"],
            result_level="unit",
            deep=True,
        )


@pytest.mark.parametrize(
    "leaf_name",
    (
        "op_note",
        "op_remember",
        "op_replace",
        "op_replace_memory",
        "op_create_file",
        "op_manage_memory_file",
    ),
)
def test_existing_creation_review_protocol_is_public(leaf_name: str) -> None:
    params = set(inspect.signature(getattr(commands, leaf_name)).parameters)
    assert REVIEW_FIELDS <= params


def test_remember_active_disconnected_note_can_validate_then_commit(vault: Path) -> None:
    kwargs = {
        "content": "# Public review round trip\n\nA deliberately disconnected conclusion.\n",
        "title": "Public review round trip",
        "slug": "public-review-round-trip",
        "suggestions": False,
    }
    validation = commands.op_remember(vault, validate_only=True, **kwargs)

    assert validation["mutated"] is False
    assert validation["draft_id"]
    assert validation["draft_hash"]
    assert validation["draft_token"]
    assert not (vault / validation["destination"]).exists()

    result = commands.op_remember(
        vault,
        draft_id=validation["draft_id"],
        draft_hash=validation["draft_hash"],
        draft_token=validation["draft_token"],
        relation_disposition="reviewed_none",
        relation_review_hash=validation["draft_hash"],
        relation_review_reason="No honest relation exists in the fixture corpus.",
        **kwargs,
    )

    assert result["path"] == validation["destination"]
    assert (vault / result["path"]).is_file()


@pytest.mark.parametrize("leaf_name", ("op_create_file", "op_manage_memory_file"))
@pytest.mark.parametrize("existing", (False, True))
def test_non_markdown_creation_review_is_rejected_without_mutation(
    tmp_path: Path, leaf_name: str, existing: bool
) -> None:
    rel = "Knowledge Base/scratch.txt"
    target = tmp_path / rel
    if existing:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("original\n", encoding="utf-8")

    kwargs = {
        "path": rel,
        "content": "replacement\n",
        "overwrite": existing,
        "validate_only": True,
    }
    if leaf_name == "op_manage_memory_file":
        kwargs["operation"] = "create"

    with pytest.raises(ValueError, match="CREATION_REVIEW_REQUIRES_MARKDOWN"):
        getattr(commands, leaf_name)(tmp_path, **kwargs)

    if existing:
        assert target.read_text(encoding="utf-8") == "original\n"
    else:
        assert not target.exists()


def test_non_markdown_creation_rejects_ignored_draft_fields(tmp_path: Path) -> None:
    target = tmp_path / "Knowledge Base" / "scratch.txt"

    with pytest.raises(ValueError, match="CREATION_REVIEW_REQUIRES_MARKDOWN"):
        commands.op_create_file(
            tmp_path,
            path="Knowledge Base/scratch.txt",
            content="would write\n",
            draft_id="ignored-draft-id",
        )

    assert not target.exists()
