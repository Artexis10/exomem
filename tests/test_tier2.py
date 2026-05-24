"""Tier 2 filesystem-parity ops: create/list/move/delete/append/frontmatter/links.

Covers happy paths plus the discipline guards — Sources/Evidence append-only,
curated-tree refusal (with allow_curated override), inbound-wikilink safety
on delete/move.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from kb_mcp import append_to_file as append_module
from kb_mcp import create_directory as mkdir_module
from kb_mcp import create_file as create_file_module
from kb_mcp import delete_file as delete_module
from kb_mcp import get_frontmatter as get_fm_module
from kb_mcp import list_directory as list_dir_module
from kb_mcp import list_inbound_links as inbound_module
from kb_mcp import move_file as move_module
from kb_mcp import set_frontmatter_field as set_fm_module


TODAY = dt.date(2026, 5, 24)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------- create_file ----------------


def test_create_file_writes_file_with_frontmatter(vault: Path) -> None:
    result = create_file_module.create_file(
        vault,
        path="Knowledge Base/Identity/Career.md",
        content="# Career\n\nCanonical career facts.\n",
        frontmatter={"type": "identity", "scope": "career"},
        today=TODAY,
    )
    written = vault / "Knowledge Base" / "Identity" / "Career.md"
    assert written.exists()
    assert result.path == "Knowledge Base/Identity/Career.md"
    text = _read(written)
    assert text.startswith("---\n")
    assert "type: identity" in text
    assert "scope: career" in text
    assert "created: 2026-05-24" in text
    assert "updated: 2026-05-24" in text
    assert "# Career" in text


def test_create_file_without_frontmatter_writes_verbatim(vault: Path) -> None:
    create_file_module.create_file(
        vault,
        path="Knowledge Base/Templates/note-template.md",
        content="raw body, no frontmatter\n",
        today=TODAY,
    )
    text = _read(vault / "Knowledge Base" / "Templates" / "note-template.md")
    assert text == "raw body, no frontmatter\n"


def test_create_file_refuses_when_exists(vault: Path) -> None:
    create_file_module.create_file(
        vault, path="Knowledge Base/Identity/x.md", content="a\n", today=TODAY
    )
    with pytest.raises(create_file_module.CreateFileError) as exc:
        create_file_module.create_file(
            vault, path="Knowledge Base/Identity/x.md", content="b\n", today=TODAY
        )
    assert exc.value.code == "FILE_EXISTS"


def test_create_file_overwrite_replaces(vault: Path) -> None:
    create_file_module.create_file(
        vault, path="Knowledge Base/Identity/x.md", content="a\n", today=TODAY
    )
    create_file_module.create_file(
        vault,
        path="Knowledge Base/Identity/x.md",
        content="b\n",
        overwrite=True,
        today=TODAY,
    )
    assert _read(vault / "Knowledge Base" / "Identity" / "x.md") == "b\n"


def test_create_file_refuses_sources(vault: Path) -> None:
    with pytest.raises(create_file_module.CreateFileError) as exc:
        create_file_module.create_file(
            vault,
            path="Knowledge Base/Sources/Articles/x.md",
            content="x",
            today=TODAY,
        )
    assert exc.value.code == "APPEND_ONLY"


def test_create_file_refuses_evidence(vault: Path) -> None:
    with pytest.raises(create_file_module.CreateFileError) as exc:
        create_file_module.create_file(
            vault,
            path="Knowledge Base/Evidence/foo/bar.md",
            content="x",
            today=TODAY,
        )
    assert exc.value.code == "APPEND_ONLY"


def test_create_file_refuses_curated_by_default(vault: Path) -> None:
    with pytest.raises(create_file_module.CreateFileError) as exc:
        create_file_module.create_file(
            vault,
            path="Cognitive Core/something.md",
            content="x",
            today=TODAY,
        )
    assert exc.value.code == "CURATED_PROTECTED"


def test_create_file_allow_curated_lets_it_through(vault: Path) -> None:
    result = create_file_module.create_file(
        vault,
        path="Cognitive Core/scratch.md",
        content="x\n",
        allow_curated=True,
        today=TODAY,
    )
    assert (vault / "Cognitive Core" / "scratch.md").exists()
    assert result.path == "Cognitive Core/scratch.md"


def test_create_file_path_escape_guarded(vault: Path) -> None:
    with pytest.raises(create_file_module.CreateFileError) as exc:
        create_file_module.create_file(
            vault, path="../escape.md", content="x", today=TODAY
        )
    assert exc.value.code == "INVALID_PATH"


def test_create_file_logs_to_log_md(vault: Path) -> None:
    create_file_module.create_file(
        vault,
        path="Knowledge Base/Identity/index.md",
        content="# Identity\n",
        frontmatter={"type": "index"},
        today=TODAY,
    )
    log = _read(vault / "Knowledge Base" / "log.md")
    assert "## [2026-05-24] create_file | Identity/index" in log


# ---------------- list_directory ----------------


def test_list_directory_lists_kb_root(vault: Path) -> None:
    result = list_dir_module.list_directory(vault, path="Knowledge Base")
    names = {e.name for e in result.entries}
    # Fixture has Sources, Notes, Entities, _Schema, index.md, log.md
    assert "Sources" in names
    assert "Notes" in names
    assert "index.md" in names


def test_list_directory_empty_path_lists_vault_root(vault: Path) -> None:
    result = list_dir_module.list_directory(vault, path="")
    names = {e.name for e in result.entries}
    assert "Knowledge Base" in names


def test_list_directory_surfaces_frontmatter_type(vault: Path) -> None:
    result = list_dir_module.list_directory(
        vault, path="Knowledge Base/Notes/Insights"
    )
    md_files = [e for e in result.entries if e.type == "file"]
    insight_entry = next(
        e for e in md_files
        if e.name == "progressive-disclosure-without-mode-fragmentation.md"
    )
    assert insight_entry.frontmatter_type == "insight"


def test_list_directory_recursive(vault: Path) -> None:
    result = list_dir_module.list_directory(
        vault, path="Knowledge Base/Notes", recursive=True
    )
    paths = {e.path for e in result.entries}
    # Recursive walk surfaces nested files
    assert any(p.endswith("/engine-architecture.md") for p in paths)


def test_list_directory_refuses_nonexistent(vault: Path) -> None:
    with pytest.raises(list_dir_module.ListDirectoryError) as exc:
        list_dir_module.list_directory(vault, path="Knowledge Base/Nope")
    assert exc.value.code == "NOT_FOUND"


# ---------------- create_directory ----------------


def test_create_directory_makes_folder(vault: Path) -> None:
    result = mkdir_module.create_directory(
        vault, path="Knowledge Base/Identity", today=TODAY
    )
    assert (vault / "Knowledge Base" / "Identity").is_dir()
    assert result.created is True


def test_create_directory_idempotent(vault: Path) -> None:
    mkdir_module.create_directory(
        vault, path="Knowledge Base/Identity", today=TODAY
    )
    result = mkdir_module.create_directory(
        vault, path="Knowledge Base/Identity", today=TODAY
    )
    assert result.created is False


def test_create_directory_parents_creates_intermediate(vault: Path) -> None:
    mkdir_module.create_directory(
        vault, path="Knowledge Base/Identity/Sub/Deeper", today=TODAY
    )
    assert (vault / "Knowledge Base" / "Identity" / "Sub" / "Deeper").is_dir()


def test_create_directory_refuses_curated_default(vault: Path) -> None:
    with pytest.raises(mkdir_module.CreateDirectoryError) as exc:
        mkdir_module.create_directory(
            vault, path="Cognitive Core/new-section", today=TODAY
        )
    assert exc.value.code == "CURATED_PROTECTED"


# ---------------- move_file ----------------


def test_move_file_relocates(vault: Path) -> None:
    src_rel = "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md"
    dst_rel = "Knowledge Base/Notes/Insights/moved-insight.md"
    result = move_module.move_file(
        vault, old_path=src_rel, new_path=dst_rel, today=TODAY
    )
    assert not (vault / src_rel).exists()
    assert (vault / dst_rel).exists()
    assert result.new_path == dst_rel


def test_move_file_updates_inbound_wikilinks(vault: Path) -> None:
    # Set up: drop a file that wikilinks to a known insight.
    referrer_path = vault / "Knowledge Base" / "Notes" / "Insights" / "referrer.md"
    referrer_path.write_text(
        "---\ntype: insight\ncreated: 2026-05-23\nupdated: 2026-05-23\ntags: []\n---\n"
        "# Referrer\n\nSee [[Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation]] for context.\n",
        encoding="utf-8",
    )
    src_rel = "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md"
    dst_rel = "Knowledge Base/Notes/Insights/renamed-disclosure.md"
    result = move_module.move_file(
        vault, old_path=src_rel, new_path=dst_rel, today=TODAY
    )
    assert result.wikilinks_updated >= 1
    referrer_text = _read(referrer_path)
    assert "renamed-disclosure" in referrer_text
    assert "progressive-disclosure-without-mode-fragmentation" not in referrer_text


def test_move_file_refuses_sources(vault: Path) -> None:
    with pytest.raises(move_module.MoveFileError) as exc:
        move_module.move_file(
            vault,
            old_path="Knowledge Base/Sources/Articles/2026-05-04-best-egcg-supplements.md",
            new_path="Knowledge Base/Notes/Insights/moved.md",
            today=TODAY,
        )
    assert exc.value.code == "APPEND_ONLY"


def test_move_file_refuses_when_dest_exists(vault: Path) -> None:
    src_rel = "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md"
    dst_rel = "Knowledge Base/Notes/Patterns/locked-cryptographic-contract-with-rfc-citations.md"
    with pytest.raises(move_module.MoveFileError) as exc:
        move_module.move_file(
            vault, old_path=src_rel, new_path=dst_rel, today=TODAY
        )
    assert exc.value.code == "DEST_EXISTS"


# ---------------- delete_file ----------------


def test_delete_file_requires_confirm(vault: Path) -> None:
    # Drop a fresh test file (must be orphan, in editable area).
    target = vault / "Knowledge Base" / "Notes" / "Insights" / "scratch.md"
    target.write_text(
        "---\ntype: insight\ncreated: 2026-05-23\nupdated: 2026-05-23\ntags: []\n---\n"
        "# Scratch\n",
        encoding="utf-8",
    )
    with pytest.raises(delete_module.DeleteFileError) as exc:
        delete_module.delete_file(
            vault,
            path="Knowledge Base/Notes/Insights/scratch.md",
            confirm=False,
            today=TODAY,
        )
    assert exc.value.code == "UNCONFIRMED"


def test_delete_file_refuses_when_inbound_links_exist(vault: Path) -> None:
    # Wire up an explicit [[...]] wikilink to the target.
    referrer = vault / "Knowledge Base" / "Notes" / "Insights" / "referrer.md"
    referrer.write_text(
        "---\ntype: insight\ncreated: 2026-05-23\nupdated: 2026-05-23\ntags: []\n---\n"
        "# Referrer\n\nLinks to [[Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation]].\n",
        encoding="utf-8",
    )
    with pytest.raises(delete_module.DeleteFileError) as exc:
        delete_module.delete_file(
            vault,
            path="Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md",
            confirm=True,
            today=TODAY,
        )
    assert exc.value.code == "INBOUND_LINKS"


def test_delete_file_force_orphan_overrides(vault: Path) -> None:
    referrer = vault / "Knowledge Base" / "Notes" / "Insights" / "referrer.md"
    referrer.write_text(
        "---\ntype: insight\ncreated: 2026-05-23\nupdated: 2026-05-23\ntags: []\n---\n"
        "# Referrer\n\n[[Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation]]\n",
        encoding="utf-8",
    )
    target = vault / "Knowledge Base" / "Notes" / "Insights" / "progressive-disclosure-without-mode-fragmentation.md"
    delete_module.delete_file(
        vault,
        path=str(target.relative_to(vault).as_posix()),
        confirm=True,
        force_orphan=True,
        today=TODAY,
    )
    assert not target.exists()


def test_delete_file_refuses_sources(vault: Path) -> None:
    with pytest.raises(delete_module.DeleteFileError) as exc:
        delete_module.delete_file(
            vault,
            path="Knowledge Base/Sources/Articles/2026-05-04-best-egcg-supplements.md",
            confirm=True,
            today=TODAY,
        )
    assert exc.value.code == "APPEND_ONLY"


def test_delete_file_happy_path_for_orphan(vault: Path) -> None:
    # Create a file with no inbound links, then delete it.
    target = vault / "Knowledge Base" / "Notes" / "Insights" / "throwaway.md"
    target.write_text(
        "---\ntype: insight\ncreated: 2026-05-23\nupdated: 2026-05-23\ntags: []\n---\n"
        "# Throwaway\n",
        encoding="utf-8",
    )
    result = delete_module.delete_file(
        vault,
        path="Knowledge Base/Notes/Insights/throwaway.md",
        confirm=True,
        today=TODAY,
    )
    assert not target.exists()
    assert result.inbound_link_count == 0


# ---------------- append_to_file ----------------


def test_append_to_file_adds_content(vault: Path) -> None:
    target = vault / "Knowledge Base" / "Notes" / "Insights" / "appendable.md"
    target.write_text("existing line\n", encoding="utf-8")
    result = append_module.append_to_file(
        vault,
        path="Knowledge Base/Notes/Insights/appendable.md",
        content="new line\n",
        today=TODAY,
    )
    assert _read(target) == "existing line\nnew line\n"
    assert result.bytes_appended > 0


def test_append_to_file_refuses_sources(vault: Path) -> None:
    with pytest.raises(append_module.AppendError) as exc:
        append_module.append_to_file(
            vault,
            path="Knowledge Base/Sources/Articles/2026-05-04-best-egcg-supplements.md",
            content="x\n",
            today=TODAY,
        )
    assert exc.value.code == "APPEND_ONLY"


# ---------------- get_frontmatter ----------------


def test_get_frontmatter_returns_fm_dict(vault: Path) -> None:
    result = get_fm_module.get_frontmatter(
        vault,
        path="Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md",
    )
    assert result.has_frontmatter is True
    assert result.frontmatter.get("type") == "insight"


# ---------------- set_frontmatter_field ----------------


def test_set_frontmatter_field_changes_value_and_bumps_updated(vault: Path) -> None:
    path = "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md"
    result = set_fm_module.set_frontmatter_field(
        vault,
        path=path,
        field="status",
        value="active",
        why="reaffirm active",
        today=TODAY,
    )
    text = _read(vault / path)
    assert "status: active" in text
    assert "updated: 2026-05-24" in text
    # Body still intact (H1 + section headers preserved)
    assert "# Progressive disclosure" in text or "Progressive disclosure" in text
    assert result.field == "status"


def test_set_frontmatter_field_requires_why(vault: Path) -> None:
    path = "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md"
    with pytest.raises(set_fm_module.SetFrontmatterError) as exc:
        set_fm_module.set_frontmatter_field(
            vault, path=path, field="status", value="active", why="", today=TODAY
        )
    assert exc.value.code == "INVALID_SET"


def test_set_frontmatter_field_refuses_sources(vault: Path) -> None:
    with pytest.raises(set_fm_module.SetFrontmatterError) as exc:
        set_fm_module.set_frontmatter_field(
            vault,
            path="Knowledge Base/Sources/Articles/2026-05-04-best-egcg-supplements.md",
            field="tags",
            value=["x"],
            why="why",
            today=TODAY,
        )
    assert exc.value.code == "APPEND_ONLY"


def test_set_frontmatter_field_rejects_setting_updated(vault: Path) -> None:
    path = "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md"
    with pytest.raises(set_fm_module.SetFrontmatterError) as exc:
        set_fm_module.set_frontmatter_field(
            vault, path=path, field="updated", value="2099-01-01", why="why",
            today=TODAY,
        )
    assert exc.value.code == "INVALID_SET"


# ---------------- list_inbound_links ----------------


def test_list_inbound_links_finds_matches(vault: Path) -> None:
    referrer = vault / "Knowledge Base" / "Notes" / "Insights" / "referrer.md"
    referrer.write_text(
        "---\ntype: insight\ncreated: 2026-05-23\nupdated: 2026-05-23\ntags: []\n---\n"
        "# Referrer\n\nLinks to [[Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation]].\n",
        encoding="utf-8",
    )
    result = inbound_module.list_inbound_links(
        vault,
        target="Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md",
    )
    assert result.count >= 1
    inbound_paths = {hit["path"] for hit in result.inbound}
    assert any("referrer" in p for p in inbound_paths)


def test_list_inbound_links_returns_empty_for_unreferenced(vault: Path) -> None:
    # Create an orphan file and confirm inbound is empty.
    target = vault / "Knowledge Base" / "Notes" / "Insights" / "totally-orphan-xyz123.md"
    target.write_text(
        "---\ntype: insight\ncreated: 2026-05-23\nupdated: 2026-05-23\ntags: []\n---\n"
        "# Orphan\n",
        encoding="utf-8",
    )
    result = inbound_module.list_inbound_links(
        vault,
        target="Knowledge Base/Notes/Insights/totally-orphan-xyz123.md",
    )
    assert result.count == 0
