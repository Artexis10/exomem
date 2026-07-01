"""InboundLinkIndex: find_inbound_wikilinks output must be identical
(content AND order) to the historical per-call full-vault scan, and the
digest freshness key must catch edits, deletes, and pure renames."""

from __future__ import annotations

import os
from pathlib import Path

from kb_mcp import vault as vault_module
from kb_mcp.vault import InboundLink, find_inbound_wikilinks, walk_vault_md


def _reference_scan(vault_root: Path, target_rel_path: str) -> list[InboundLink]:
    """The pre-index implementation, kept verbatim as the test oracle."""
    target = target_rel_path.replace("\\", "/").removesuffix(".md")
    target_full = (
        target if target.startswith("Knowledge Base/")
        else "Knowledge Base/" + target
    )
    target_stripped = target_full.removeprefix("Knowledge Base/")
    target_basename = target.rsplit("/", 1)[-1]

    basename_count = 0
    for md in walk_vault_md(vault_root):
        if md.stem == target_basename:
            basename_count += 1
            if basename_count > 1:
                break
    basename_unique = basename_count == 1

    matches: list[InboundLink] = []
    for md in walk_vault_md(vault_root):
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            md_rel = md.resolve().relative_to(vault_root.resolve()).as_posix()
        except ValueError:
            continue
        if md_rel.removesuffix(".md") in (target_full, target_stripped):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for m in vault_module._WIKILINK_PATTERN.finditer(line):
                raw = m.group(1).strip()
                normalized = raw.split("#", 1)[0].rstrip().removesuffix(".md")
                if normalized == target_full or normalized == target_stripped:
                    matches.append(InboundLink(
                        path=md_rel, line_number=lineno,
                        context=line.strip()[:240], raw_target=raw,
                    ))
                elif (
                    basename_unique
                    and "/" not in normalized
                    and normalized == target_basename
                ):
                    matches.append(InboundLink(
                        path=md_rel, line_number=lineno,
                        context=line.strip()[:240], raw_target=raw,
                    ))
    return matches


def _all_md_rels(vault_root: Path) -> list[str]:
    out = []
    for md in walk_vault_md(vault_root):
        try:
            out.append(md.resolve().relative_to(vault_root.resolve()).as_posix())
        except ValueError:
            continue
    return out


def test_index_matches_reference_for_every_fixture_page(vault: Path) -> None:
    for rel in _all_md_rels(vault):
        assert find_inbound_wikilinks(vault, rel) == _reference_scan(vault, rel), rel


def test_matches_reference_with_link_forms_and_ambiguity(vault: Path) -> None:
    notes = vault / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True, exist_ok=True)
    target = notes / "inbound-target.md"
    target.write_text("# Inbound target\n", encoding="utf-8")
    linker = notes / "inbound-linker.md"
    linker.write_text(
        "# Linker\n\n"
        "Full: [[Knowledge Base/Notes/inbound-target]] here.\n"
        "Stripped: [[Notes/inbound-target]] and bare [[inbound-target]].\n"
        "Anchored: [[Knowledge Base/Notes/inbound-target#section|alias]].\n"
        "Self is skipped in the target file itself.\n",
        encoding="utf-8",
    )
    got = find_inbound_wikilinks(vault, "Knowledge Base/Notes/inbound-target.md")
    ref = _reference_scan(vault, "Knowledge Base/Notes/inbound-target.md")
    assert got == ref
    assert len(got) == 4  # full + stripped + bare (unique) + anchored

    # Make the basename ambiguous — the bare match must drop, matching ref.
    dup = vault / "Knowledge Base" / "Sources" / "inbound-target.md"
    dup.parent.mkdir(parents=True, exist_ok=True)
    dup.write_text("# Duplicate basename\n", encoding="utf-8")
    got = find_inbound_wikilinks(vault, "Knowledge Base/Notes/inbound-target.md")
    assert got == _reference_scan(vault, "Knowledge Base/Notes/inbound-target.md")
    assert len(got) == 3


def test_self_reference_excluded(vault: Path) -> None:
    notes = vault / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True, exist_ok=True)
    selfref = notes / "self-ref.md"
    selfref.write_text("# Self\n\nSee [[Notes/self-ref]] (me).\n", encoding="utf-8")
    assert find_inbound_wikilinks(vault, "Knowledge Base/Notes/self-ref.md") == []


def test_index_invalidates_on_edit_delete_and_pure_rename(vault: Path) -> None:
    notes = vault / "Knowledge Base" / "Notes"
    notes.mkdir(parents=True, exist_ok=True)
    target = notes / "invalidate-target.md"
    target.write_text("# Invalidate target\n", encoding="utf-8")
    linker = notes / "invalidate-linker.md"
    linker.write_text("# L\n\n[[Notes/invalidate-target]]\n", encoding="utf-8")
    assert len(find_inbound_wikilinks(vault, "Knowledge Base/Notes/invalidate-target.md")) == 1

    # Edit: second link appears.
    ns = linker.stat().st_mtime_ns
    linker.write_text(
        "# L\n\n[[Notes/invalidate-target]] and again [[Notes/invalidate-target]]\n",
        encoding="utf-8",
    )
    os.utime(linker, ns=(ns + 2_000_000_000, ns + 2_000_000_000))
    assert len(find_inbound_wikilinks(vault, "Knowledge Base/Notes/invalidate-target.md")) == 2

    # Pure rename of the linker (mtime preserved, count unchanged): the digest
    # key must register it — link entries must carry the NEW path, because
    # move/delete safety checks act on these paths.
    renamed = linker.with_name("invalidate-linker-renamed.md")
    os.replace(linker, renamed)
    links = find_inbound_wikilinks(vault, "Knowledge Base/Notes/invalidate-target.md")
    assert {ln.path for ln in links} == {"Knowledge Base/Notes/invalidate-linker-renamed.md"}

    # Delete: inbound links disappear.
    renamed.unlink()
    assert find_inbound_wikilinks(vault, "Knowledge Base/Notes/invalidate-target.md") == []


def test_kb_root_target_where_stripped_equals_basename(vault: Path) -> None:
    """A KB-root file makes target_stripped == target_basename — the bucket
    union must not double-count (the reference's elif never fired)."""
    rootfile = vault / "Knowledge Base" / "kb-root-target.md"
    rootfile.write_text("# KB root target\n", encoding="utf-8")
    linker = vault / "Knowledge Base" / "Notes" / "kb-root-linker.md"
    linker.parent.mkdir(parents=True, exist_ok=True)
    linker.write_text("# L\n\n[[kb-root-target]] once.\n", encoding="utf-8")
    got = find_inbound_wikilinks(vault, "Knowledge Base/kb-root-target.md")
    assert got == _reference_scan(vault, "Knowledge Base/kb-root-target.md")
    assert len(got) == 1
