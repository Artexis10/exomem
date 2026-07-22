"""Tier 2 filesystem-parity ops: create/list/move/delete/append/frontmatter/links.

Covers happy paths plus the discipline guards — Sources/Evidence append-only,
read-only-tree refusal (subtrees marked `readonly` in `_access.yaml`, a hard
refusal with no override), inbound-wikilink safety on delete/move.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from exomem import append_to_file as append_module
from exomem import create_directory as mkdir_module
from exomem import create_file as create_file_module
from exomem import delete_directory as rmdir_module
from exomem import delete_file as delete_module
from exomem import get_frontmatter as get_fm_module
from exomem import get_page as get_module
from exomem import list_directory as list_dir_module
from exomem import list_inbound_links as inbound_module
from exomem import list_trash as list_trash_module
from exomem import move_file as move_module
from exomem import recover_from_trash as recover_module
from exomem import set_frontmatter_field as set_fm_module

TODAY = dt.date(2026, 5, 24)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _mark_readonly(vault: Path, folder: str) -> None:
    """Mark a top-level folder read-only via _access.yaml (the de-identified
    replacement for the old hardcoded curated-tree list)."""
    (vault / "Knowledge Base" / "_access.yaml").write_text(
        f"readonly:\n  - {folder}\n", encoding="utf-8"
    )


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
    assert "created: \"2026-05-24\"" in text
    assert "updated: \"2026-05-24\"" in text
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


def test_create_file_refuses_readonly_tree(vault: Path) -> None:
    _mark_readonly(vault, "Reference")
    with pytest.raises(create_file_module.CreateFileError) as exc:
        create_file_module.create_file(
            vault,
            path="Reference/something.md",
            content="x",
            today=TODAY,
        )
    assert exc.value.code == "READONLY_PROTECTED"
    assert not (vault / "Reference" / "something.md").exists()


def test_create_file_allow_curated_does_not_override_readonly(vault: Path) -> None:
    """`readonly` (_access.yaml) is a HARD refusal — `allow_curated=true` must
    NOT bypass it (unlike the legacy curated-tree guard, which it could)."""
    _mark_readonly(vault, "Reference")
    with pytest.raises(create_file_module.CreateFileError) as exc:
        create_file_module.create_file(
            vault,
            path="Reference/scratch.md",
            content="x\n",
            allow_curated=True,
            today=TODAY,
        )
    assert exc.value.code == "READONLY_PROTECTED"
    assert not (vault / "Reference" / "scratch.md").exists()


def test_create_file_path_escape_guarded(vault: Path) -> None:
    with pytest.raises(create_file_module.CreateFileError) as exc:
        create_file_module.create_file(
            vault, path="../escape.md", content="x", today=TODAY
        )
    assert exc.value.code == "INVALID_PATH"


def test_create_file_refuses_escape_out_of_knowledge_base(vault: Path) -> None:
    """Governed writes stay under Knowledge Base/: a `..` that escapes KB to a
    vault-root sibling is refused, and a bare path is rooted under KB rather than
    written to the vault root. Regression for the audit's KB-boundary finding."""
    for escape in (
        "Knowledge Base/../outside.md",
        "Knowledge Base/Notes/Insights/../../../escape-deep.md",
        "Knowledge Base\\..\\out.md",
    ):
        with pytest.raises(create_file_module.CreateFileError) as exc:
            create_file_module.create_file(vault, path=escape, content="x", today=TODAY)
        assert exc.value.code == "INVALID_PATH", escape
    # No stray file landed at the vault root (outside Knowledge Base/).
    assert not (vault / "outside.md").exists()
    assert not (vault / "escape-deep.md").exists()
    assert not (vault / "out.md").exists()
    # A bare KB-relative path is rooted UNDER Knowledge Base/, not the vault root.
    create_file_module.create_file(vault, path="Identity/bare.md", content="x", today=TODAY)
    assert (vault / "Knowledge Base" / "Identity" / "bare.md").exists()
    assert not (vault / "Identity" / "bare.md").exists()


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


def test_create_directory_refuses_readonly_tree(vault: Path) -> None:
    _mark_readonly(vault, "Reference")
    with pytest.raises(mkdir_module.CreateDirectoryError) as exc:
        mkdir_module.create_directory(
            vault, path="Reference/new-section", today=TODAY
        )
    assert exc.value.code == "READONLY_PROTECTED"
    assert not (vault / "Reference" / "new-section").exists()


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
        "---\ntype: insight\nstatus: draft\ncreated: 2026-05-23\n"
        "updated: 2026-05-23\ntags: []\n---\n"
        "# Referrer\n\nSee [[Knowledge Base/Notes/Insights/"
        "progressive-disclosure-without-mode-fragmentation]] for context.\n",
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


def test_move_file_rewrites_wikilinks_with_anchor(vault: Path) -> None:
    """Refs like `[[Knowledge Base/Foo#section]]` should be rewritten on move.

    Regression: the old `_rewrite_wikilinks` compared the raw target (including
    `#anchor`) against the path-only `old_full`, so anchored refs were never
    rewritten and `find_inbound_wikilinks` didn't even queue the files. Caught
    during the 2026-05-28 KB recategorization sweep.
    """
    referrer_path = vault / "Knowledge Base" / "Notes" / "Insights" / "anchor-referrer.md"
    referrer_path.write_text(
        "---\ntype: insight\nstatus: draft\ncreated: 2026-05-28\n"
        "updated: 2026-05-28\ntags: []\n---\n"
        "# Anchor Referrer\n\n"
        "See [[Knowledge Base/Notes/Insights/"
        "progressive-disclosure-without-mode-fragmentation"
        "#Mechanism|the mechanism]] and [[Notes/Insights/"
        "progressive-disclosure-without-mode-fragmentation"
        "#Mechanism]] (stripped form).\n",
        encoding="utf-8",
    )
    src_rel = "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md"
    dst_rel = "Knowledge Base/Notes/Insights/anchor-renamed-disclosure.md"
    result = move_module.move_file(
        vault, old_path=src_rel, new_path=dst_rel, today=TODAY
    )
    # Both refs (full + stripped form) must have been rewritten.
    assert result.wikilinks_updated >= 2, result.as_dict()
    text = _read(referrer_path)
    # New target path + anchor preserved on both forms.
    assert (
        "[[Knowledge Base/Notes/Insights/anchor-renamed-disclosure#Mechanism|the mechanism]]"
        in text
    )
    assert (
        "[[Notes/Insights/anchor-renamed-disclosure#Mechanism]]" in text
    )
    # No stale references to the old slug.
    assert "progressive-disclosure-without-mode-fragmentation" not in text


def test_move_file_refuses_sources(vault: Path) -> None:
    with pytest.raises(move_module.MoveFileError) as exc:
        move_module.move_file(
            vault,
            old_path="Knowledge Base/Sources/Articles/2026-05-04-best-egcg-supplements.md",
            new_path="Knowledge Base/Notes/Insights/moved.md",
            today=TODAY,
        )
    assert exc.value.code == "APPEND_ONLY"


def test_move_file_allows_intra_sources_relocation(vault: Path) -> None:
    """Relocating WITHIN Sources/ (e.g. into a themed sub-folder) is allowed.

    Rule 2 protects content immutability, not file location — the bytes are
    carried verbatim. This is the sanctioned path for organizing Sources/.
    """
    src_rel = "Knowledge Base/Sources/Articles/2026-05-04-best-egcg-supplements.md"
    dst_rel = "Knowledge Base/Sources/Articles/Health/2026-05-04-best-egcg-supplements.md"
    result = move_module.move_file(
        vault,
        old_path=src_rel,
        new_path=dst_rel,
        update_wikilinks=False,
        today=TODAY,
    )
    assert not (vault / src_rel).exists()
    assert (vault / dst_rel).exists()
    assert result.new_path == dst_rel


def test_move_file_refuses_into_sources_from_outside(vault: Path) -> None:
    """Moving a non-source file INTO Sources/ is still forbidden (use `add`)."""
    with pytest.raises(move_module.MoveFileError) as exc:
        move_module.move_file(
            vault,
            old_path=(
                "Knowledge Base/Notes/Insights/"
                "progressive-disclosure-without-mode-fragmentation.md"
            ),
            new_path="Knowledge Base/Sources/Articles/smuggled-in.md",
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
        "# Referrer\n\nLinks to [[Knowledge Base/Notes/Insights/"
        "progressive-disclosure-without-mode-fragmentation]].\n",
        encoding="utf-8",
    )
    with pytest.raises(delete_module.DeleteFileError) as exc:
        delete_module.delete_file(
            vault,
            path=(
                "Knowledge Base/Notes/Insights/"
                "progressive-disclosure-without-mode-fragmentation.md"
            ),
            confirm=True,
            today=TODAY,
        )
    assert exc.value.code == "INBOUND_LINKS"


def test_delete_file_force_orphan_overrides(vault: Path) -> None:
    referrer = vault / "Knowledge Base" / "Notes" / "Insights" / "referrer.md"
    referrer.write_text(
        "---\ntype: insight\ncreated: 2026-05-23\nupdated: 2026-05-23\ntags: []\n---\n"
        "# Referrer\n\n[[Knowledge Base/Notes/Insights/"
        "progressive-disclosure-without-mode-fragmentation]]\n",
        encoding="utf-8",
    )
    target = (
        vault
        / "Knowledge Base"
        / "Notes"
        / "Insights"
        / "progressive-disclosure-without-mode-fragmentation.md"
    )
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
    # Create a file with no inbound links, then trash it.
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
    # Original gone, trash copy present, meta sidecar present
    assert not target.exists()
    assert result.inbound_link_count == 0
    assert result.trash_path.startswith("Knowledge Base/_trash/")
    trash_abs = vault / result.trash_path
    assert trash_abs.exists()
    assert (trash_abs.parent / f"{trash_abs.name}.meta.json").exists()


def test_delete_file_refuses_already_trashed(vault: Path) -> None:
    # First trash a file, then try to trash the trash entry.
    target = vault / "Knowledge Base" / "Notes" / "Insights" / "x.md"
    target.write_text(
        "---\ntype: insight\ncreated: 2026-05-23\nupdated: 2026-05-23\ntags: []\n---\n# X\n",
        encoding="utf-8",
    )
    first = delete_module.delete_file(
        vault, path="Knowledge Base/Notes/Insights/x.md",
        confirm=True, today=TODAY,
    )
    with pytest.raises(delete_module.DeleteFileError) as exc:
        delete_module.delete_file(
            vault, path=first.trash_path, confirm=True, today=TODAY,
        )
    assert exc.value.code == "ALREADY_TRASHED"


def test_delete_file_expected_dead_inbound_ignores_listed_referrers(vault: Path) -> None:
    # Two files in a chain: a → b. Trash b normally requires force_orphan,
    # but expected_dead_inbound=[a] should let it through without it.
    target = vault / "Knowledge Base" / "Notes" / "Insights" / "b.md"
    target.write_text(
        "---\ntype: insight\ncreated: 2026-05-23\nupdated: 2026-05-23\ntags: []\n---\n# B\n",
        encoding="utf-8",
    )
    referrer = vault / "Knowledge Base" / "Notes" / "Insights" / "a.md"
    referrer.write_text(
        "---\ntype: insight\ncreated: 2026-05-23\nupdated: 2026-05-23\ntags: []\n---\n"
        "# A\n\nLinks to [[Knowledge Base/Notes/Insights/b]].\n",
        encoding="utf-8",
    )
    result = delete_module.delete_file(
        vault,
        path="Knowledge Base/Notes/Insights/b.md",
        confirm=True,
        expected_dead_inbound=["Knowledge Base/Notes/Insights/a.md"],
        today=TODAY,
    )
    assert not target.exists()
    assert result.inbound_link_count == 0
    assert result.inbound_ignored_count == 1


# ---------------- append_to_file ----------------


def test_append_to_file_adds_content(vault: Path) -> None:
    target = vault / "Knowledge Base" / "Notes" / "appendable.md"
    target.write_text("existing line\n", encoding="utf-8")
    result = append_module.append_to_file(
        vault,
        path="Knowledge Base/Notes/appendable.md",
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


def test_set_frontmatter_field_accepts_crlf_frontmatter(vault: Path) -> None:
    path = "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md"
    absolute = vault / path
    absolute.write_bytes(absolute.read_bytes().replace(b"\n", b"\r\n"))

    result = set_fm_module.set_frontmatter_field(
        vault,
        path=path,
        field="status",
        value="active",
        why="reaffirm active on a Windows-authored page",
        today=TODAY,
    )

    assert result.field == "status"
    assert "status: active" in absolute.read_text(encoding="utf-8")
    assert "updated: 2026-05-24" in absolute.read_text(encoding="utf-8")


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
        "# Referrer\n\nLinks to [[Knowledge Base/Notes/Insights/"
        "progressive-disclosure-without-mode-fragmentation]].\n",
        encoding="utf-8",
    )
    result = inbound_module.list_inbound_links(
        vault,
        target="Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md",
    )
    assert result.count >= 1
    inbound_paths = {hit["path"] for hit in result.inbound}
    assert any("referrer" in p for p in inbound_paths)


# ---------------- delete_directory ----------------


def test_delete_directory_trashes_empty_dir(vault: Path) -> None:
    empty = vault / "Knowledge Base" / "Scratch" / "today"
    empty.mkdir(parents=True)
    result = rmdir_module.delete_directory(
        vault, path="Knowledge Base/Scratch/today", confirm=True, today=TODAY,
    )
    assert not empty.exists()
    assert result.trash_path.startswith("Knowledge Base/_trash/")
    assert (vault / result.trash_path).is_dir()


def test_delete_directory_refuses_unconfirmed(vault: Path) -> None:
    (vault / "Knowledge Base" / "Scratch" / "x").mkdir(parents=True)
    with pytest.raises(rmdir_module.DeleteDirectoryError) as exc:
        rmdir_module.delete_directory(
            vault, path="Knowledge Base/Scratch/x", confirm=False, today=TODAY,
        )
    assert exc.value.code == "UNCONFIRMED"


def test_delete_directory_refuses_non_empty_without_recursive(vault: Path) -> None:
    d = vault / "Knowledge Base" / "Scratch" / "today"
    d.mkdir(parents=True)
    (d / "file.md").write_text(
        "---\ntype: insight\ncreated: 2026-05-23\nupdated: 2026-05-23\ntags: []\n---\n# x\n",
        encoding="utf-8",
    )
    with pytest.raises(rmdir_module.DeleteDirectoryError) as exc:
        rmdir_module.delete_directory(
            vault, path="Knowledge Base/Scratch/today", confirm=True, today=TODAY,
        )
    assert exc.value.code == "NOT_EMPTY"


def test_delete_directory_recursive_trashes_whole_tree(vault: Path) -> None:
    d = vault / "Knowledge Base" / "Scratch" / "today"
    d.mkdir(parents=True)
    (d / "file.md").write_text(
        "---\ntype: insight\ncreated: 2026-05-23\nupdated: 2026-05-23\ntags: []\n---\n# x\n",
        encoding="utf-8",
    )
    result = rmdir_module.delete_directory(
        vault, path="Knowledge Base/Scratch/today",
        confirm=True, recursive=True, today=TODAY,
    )
    assert not d.exists()
    assert result.file_count == 1
    assert (vault / result.trash_path / "file.md").exists()


def test_delete_directory_refuses_sources(vault: Path) -> None:
    with pytest.raises(rmdir_module.DeleteDirectoryError) as exc:
        rmdir_module.delete_directory(
            vault, path="Knowledge Base/Sources/Articles",
            confirm=True, recursive=True, today=TODAY,
        )
    assert exc.value.code == "APPEND_ONLY"


def test_delete_directory_refuses_when_external_inbound_exists(vault: Path) -> None:
    d = vault / "Knowledge Base" / "Scratch" / "x"
    d.mkdir(parents=True)
    (d / "inside.md").write_text(
        "---\ntype: insight\ncreated: 2026-05-23\nupdated: 2026-05-23\ntags: []\n---\n# inside\n",
        encoding="utf-8",
    )
    # External referrer (lives outside the doomed tree).
    referrer = vault / "Knowledge Base" / "Notes" / "Insights" / "ref.md"
    referrer.write_text(
        "---\ntype: insight\ncreated: 2026-05-23\nupdated: 2026-05-23\ntags: []\n---\n"
        "[[Knowledge Base/Scratch/x/inside]]\n",
        encoding="utf-8",
    )
    with pytest.raises(rmdir_module.DeleteDirectoryError) as exc:
        rmdir_module.delete_directory(
            vault, path="Knowledge Base/Scratch/x",
            confirm=True, recursive=True, today=TODAY,
        )
    assert exc.value.code == "INBOUND_LINKS"


# ---------------- list_trash + recover_from_trash + sidecar cleanup ----------------


def _trash_a_file(vault: Path, rel: str) -> tuple[str, str]:
    """Helper: trash a fixture file, return (trash_path, meta_path)."""
    abs_path = vault / rel
    if not abs_path.exists():
        abs_path.write_text(
            "---\ntype: insight\nstatus: draft\ncreated: 2026-05-23\n"
            "updated: 2026-05-23\ntags: []\n---\n# x\n",
            encoding="utf-8",
        )
    result = delete_module.delete_file(
        vault, path=rel, confirm=True, today=TODAY,
    )
    return result.trash_path, result.trash_meta_path


def test_delete_file_response_surfaces_meta_path(vault: Path) -> None:
    rel = "Knowledge Base/Notes/Insights/m1.md"
    tp, mp = _trash_a_file(vault, rel)
    assert tp.startswith("Knowledge Base/_trash/")
    assert mp.endswith(".meta.json")
    assert (vault / mp).exists()


def test_list_trash_returns_entries_with_original_paths(vault: Path) -> None:
    rel = "Knowledge Base/Notes/Insights/lt-target.md"
    _trash_a_file(vault, rel)
    result = list_trash_module.list_trash(vault)
    assert result.count >= 1
    matching = [e for e in result.entries if e.original_path == rel]
    assert len(matching) == 1
    e = matching[0]
    assert e.kind == "file"
    assert e.trashed_at  # nonempty ISO string
    assert e.trash_path.startswith("Knowledge Base/_trash/")
    assert e.meta_path.endswith(".meta.json")


def test_list_trash_surfaces_orphan_sidecars(vault: Path) -> None:
    rel = "Knowledge Base/Notes/Insights/orphan-target.md"
    tp, mp = _trash_a_file(vault, rel)
    # Delete the trashed file but leave the sidecar — simulates legacy state
    (vault / tp).unlink()
    result = list_trash_module.list_trash(vault)
    assert mp in result.orphan_sidecars


def test_recover_from_trash_restores_to_original(vault: Path) -> None:
    rel = "Knowledge Base/Notes/Insights/rec-target.md"
    tp, mp = _trash_a_file(vault, rel)
    assert not (vault / rel).exists()
    result = recover_module.recover_from_trash(
        vault, trash_path=tp, today=TODAY,
    )
    assert result.restored_path == rel
    assert (vault / rel).exists()
    # Sidecar removed
    assert not (vault / mp).exists()


def test_recover_from_trash_refuses_existing_dest(vault: Path) -> None:
    rel = "Knowledge Base/Notes/Insights/dup-target.md"
    tp, _ = _trash_a_file(vault, rel)
    # Recreate the original path so recovery would overwrite
    (vault / rel).write_text("# new version\n", encoding="utf-8")
    with pytest.raises(recover_module.RecoverError) as exc:
        recover_module.recover_from_trash(vault, trash_path=tp, today=TODAY)
    assert exc.value.code == "DEST_EXISTS"


def test_recover_from_trash_to_custom_path(vault: Path) -> None:
    rel = "Knowledge Base/Notes/Insights/cp-target.md"
    tp, _ = _trash_a_file(vault, rel)
    custom = "Knowledge Base/Notes/Insights/cp-renamed.md"
    result = recover_module.recover_from_trash(
        vault, trash_path=tp, restore_path=custom, today=TODAY,
    )
    assert result.restored_path == custom
    assert (vault / custom).exists()
    assert not (vault / rel).exists()


def test_recover_from_trash_refuses_non_trash_paths(vault: Path) -> None:
    with pytest.raises(recover_module.RecoverError) as exc:
        recover_module.recover_from_trash(
            vault,
            trash_path=(
                "Knowledge Base/Notes/Insights/"
                "progressive-disclosure-without-mode-fragmentation.md"
            ),
            today=TODAY,
        )
    assert exc.value.code == "NOT_IN_TRASH"


def test_move_file_from_trash_also_removes_sidecar(vault: Path) -> None:
    """Regression: move_file out of _trash/ used to orphan the .meta.json."""
    rel = "Knowledge Base/Notes/Insights/mv-target.md"
    tp, mp = _trash_a_file(vault, rel)
    move_module.move_file(
        vault, old_path=tp, new_path=rel, today=TODAY,
    )
    assert (vault / rel).exists()
    assert not (vault / mp).exists(), "sidecar should be removed when moving out of trash"


# ---------------- get extension auto-append bug fix ----------------


def test_get_does_not_append_md_when_extension_present(vault: Path) -> None:
    """Regression: `get` used to append .md unconditionally, so trash
    sidecars (.meta.json) couldn't be read through the MCP."""
    rel = "Knowledge Base/Notes/Insights/ga-target.md"
    tp, mp = _trash_a_file(vault, rel)
    # Reading a .meta.json should succeed without .md being appended
    result = get_module.get_page(vault, path=mp)
    assert result.path == mp
    assert "original_path" in result.content


# ---------------- list_inbound_links extras ----------------


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


# ---------------- Tier 2 registration gating (EXOMEM_DISABLE_TIER2) ----------------

# Post-consolidation Tier 2 surface: create_directory folded into create_file
# Product surface: file/data/media tier-2 behavior is exposed through product
# commands while canonical filesystem leaves remain implementation details.
TIER2_TOOLS = {"manage_memory_file", "query_dataset", "read_media"}


def _registered_tool_names(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """Build the FastMCP server and return the set of registered tool names.

    build_server() calls load_dotenv(override=True); neutralize it so the repo
    `.env` can't clobber the test's EXOMEM_VAULT_PATH / EXOMEM_DISABLE_TIER2.
    """
    import asyncio

    from exomem import server as server_module

    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    mcp = server_module.build_server(require_auth=False)
    tools = asyncio.run(mcp.list_tools(run_middleware=False))
    return {t.name for t in tools}


def test_tier2_registered_by_default(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_TIER2", raising=False)
    names = _registered_tool_names(monkeypatch)
    # Tier 1 read/write ops are always present...
    assert {
        "ask_memory",
        "read_memory",
        "remember",
        "capture_source",
        "edit_memory",
        "review_memory",
    } <= names
    # ...and so are the Tier 2 escape hatches when the flag is unset.
    assert TIER2_TOOLS <= names


def test_tier2_dropped_when_disabled(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_TIER2", "1")
    names = _registered_tool_names(monkeypatch)
    # Tier 1 stays fully registered — only the escape hatches drop.
    assert {
        "ask_memory",
        "read_memory",
        "remember",
        "capture_source",
        "edit_memory",
        "review_memory",
        "connect_memory",
    } <= names
    assert names.isdisjoint(TIER2_TOOLS)


def test_excluded_frontmatter_fields_are_refused(vault: Path) -> None:
    """The schema excludes `confidence`/`decay_at`/`expires_at`; the writers must
    enforce that, not just document it. Regression for the audit's confidence-float
    finding (edit_memory(field='confidence') previously persisted 0.99)."""
    import asyncio

    from exomem import server as server_module

    mcp = server_module.build_server(require_auth=False)
    note = vault / "Knowledge Base" / "Notes" / "Insights" / "conf.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("---\ntype: insight\n---\n# C\n\nBody.\n", encoding="utf-8")
    rel = "Knowledge Base/Notes/Insights/conf.md"

    with pytest.raises(Exception) as ei:  # ToolError wrapping EXCLUDED_FIELD
        asyncio.run(mcp.call_tool(
            "edit_memory",
            {"path": rel, "why": "x", "field": "confidence", "value": 0.99},
            run_middleware=True,
        ))
    assert "EXCLUDED_FIELD" in str(ei.value) or "confidence" in str(ei.value)
    assert "confidence" not in note.read_text(encoding="utf-8")

    # create_file with an excluded key in its frontmatter dict is refused too.
    with pytest.raises(create_file_module.CreateFileError) as ce:
        create_file_module.create_file(
            vault, path="Notes/Insights/decay.md",
            content="x", frontmatter={"type": "insight", "decay_at": "2030-01-01"},
            today=TODAY,
        )
    assert ce.value.code == "EXCLUDED_FIELD"
