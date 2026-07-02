"""Tests for wikilink normalization machinery in vault.py.

The writer is the chokepoint: once it canonicalizes wikilinks on every
write, drift can't grow. These tests pin the resolver's behaviour across
the forms it has to handle.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem.vault import (
    AmbiguousWikilinkError,
    UnresolvedWikilinkError,
    WikilinkResolver,
    find_body_wikilinks,
    normalize_body_wikilinks,
    normalize_wikilink,
)


def test_resolves_full_vault_rooted_path(vault: Path) -> None:
    canonical, warning = normalize_wikilink(
        "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation",
        vault,
    )
    assert warning is None
    assert canonical == (
        "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation"
    )


def test_resolves_kb_stripped_path(vault: Path) -> None:
    canonical, warning = normalize_wikilink(
        "Notes/Insights/progressive-disclosure-without-mode-fragmentation",
        vault,
    )
    assert warning is None
    assert canonical.startswith("Knowledge Base/")


def test_resolves_bracketed_input(vault: Path) -> None:
    canonical, warning = normalize_wikilink(
        "[[Notes/Insights/progressive-disclosure-without-mode-fragmentation]]",
        vault,
    )
    assert warning is None
    assert canonical == (
        "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation"
    )


def test_resolves_with_md_extension(vault: Path) -> None:
    canonical, warning = normalize_wikilink(
        "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md",
        vault,
    )
    assert warning is None
    assert not canonical.endswith(".md")


def test_resolves_with_alias_strips_alias(vault: Path) -> None:
    canonical, warning = normalize_wikilink(
        "Notes/Insights/progressive-disclosure-without-mode-fragmentation|disclosure",
        vault,
    )
    assert warning is None
    assert "|" not in canonical


def test_preserves_anchor(vault: Path) -> None:
    canonical, warning = normalize_wikilink(
        "Notes/Insights/progressive-disclosure-without-mode-fragmentation#mechanism",
        vault,
    )
    assert warning is None
    assert canonical.endswith("#mechanism")


def test_bare_name_resolves_to_unique_stem(vault: Path) -> None:
    # The fixture has exactly one file with this stem.
    canonical, warning = normalize_wikilink(
        "progressive-disclosure-without-mode-fragmentation", vault
    )
    assert warning is None
    assert canonical == (
        "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation"
    )


def test_title_fallback_resolves_date_prefixed_file(vault: Path, tmp_path: Path) -> None:
    """A bare wikilink should resolve to a file whose frontmatter title matches,
    even when the filename is date-prefixed. This is the `North-Led Content
    Manual` case: 19 refs failed audit because the source file is named
    `2026-05-15-tu-north-led-content-manual.md` but referenced bare.
    """
    source = vault / "Knowledge Base" / "Sources" / "Articles" / "2026-05-15-tu-test-manual.md"
    source.write_text(
        '---\ntype: source\ntitle: "Test Content Manual"\n---\n\n# Test Content Manual\n',
        encoding="utf-8",
    )
    canonical, warning = normalize_wikilink("Test Content Manual", vault)
    assert warning is None
    assert canonical == "Knowledge Base/Sources/Articles/2026-05-15-tu-test-manual"


def test_unresolved_with_kb_prefix_passes_through(vault: Path) -> None:
    """Unresolvable targets that already have the canonical prefix stay as-is
    so callers building wikilink strings get a clean fallback."""
    canonical, warning = normalize_wikilink(
        "Knowledge Base/Notes/Insights/does-not-exist", vault
    )
    assert warning is not None
    assert canonical == "Knowledge Base/Notes/Insights/does-not-exist"
    assert "[[" not in canonical


def test_unresolved_curated_tree_keeps_vault_relative(vault: Path) -> None:
    """A reference into a read-only sibling tree (marked `readonly` in
    `_access.yaml`) is vault-relative, NOT a KB sub-path. Don't prepend
    Knowledge Base/ even when the target file doesn't exist yet."""
    (vault / "Knowledge Base" / "_access.yaml").write_text(
        "readonly:\n  - Reference\n", encoding="utf-8"
    )
    canonical, warning = normalize_wikilink("Reference/Whatever", vault)
    assert warning is not None
    assert canonical == "Reference/Whatever"


def test_unresolved_kb_relative_promotes_to_full(vault: Path) -> None:
    """A path like `Notes/Patterns/missing` is KB-relative; promote it."""
    canonical, warning = normalize_wikilink("Notes/Patterns/missing", vault)
    assert warning is not None
    assert canonical == "Knowledge Base/Notes/Patterns/missing"


def test_strict_mode_raises_on_unresolved(vault: Path) -> None:
    with pytest.raises(UnresolvedWikilinkError):
        normalize_wikilink("nonexistent-page-no-stems", vault, strict=True)


def test_strict_mode_raises_on_ambiguous_bare(vault: Path) -> None:
    """Two files with the same stem in different folders → ambiguous bare name."""
    (vault / "Knowledge Base" / "Notes" / "Failures" / "duplicate.md").write_text(
        "# duplicate\n", encoding="utf-8"
    )
    (vault / "Knowledge Base" / "Notes" / "Patterns" / "duplicate.md").write_text(
        "# duplicate\n", encoding="utf-8"
    )
    with pytest.raises(AmbiguousWikilinkError):
        normalize_wikilink("duplicate", vault, strict=True)


def test_resolver_caches_across_calls(vault: Path) -> None:
    """One resolver should serve many normalize calls — the writer builds
    it once per op."""
    resolver = WikilinkResolver(vault)
    for _ in range(3):
        canonical, warning = normalize_wikilink(
            "Notes/Insights/progressive-disclosure-without-mode-fragmentation",
            vault,
            resolver=resolver,
        )
        assert warning is None


def test_pending_path_resolves_before_disk_write(vault: Path) -> None:
    """A writer registering its about-to-be-written file should make it
    immediately resolvable, even before the disk write."""
    resolver = WikilinkResolver(vault)
    resolver.add_pending(
        "Knowledge Base/Notes/Insights/fresh-note", title="Fresh Note"
    )
    canonical, warning = normalize_wikilink("fresh-note", vault, resolver=resolver)
    assert warning is None
    assert canonical == "Knowledge Base/Notes/Insights/fresh-note"

    canonical, warning = normalize_wikilink("Fresh Note", vault, resolver=resolver)
    assert warning is None
    assert canonical == "Knowledge Base/Notes/Insights/fresh-note"


def test_body_normalization_rewrites_kb_relative_to_full(vault: Path) -> None:
    body = "See [[Notes/Insights/progressive-disclosure-without-mode-fragmentation]] for context."
    new_body, warnings = normalize_body_wikilinks(body, vault)
    assert (
        "[[Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation]]"
        in new_body
    )
    assert warnings == []


def test_body_normalization_preserves_alias(vault: Path) -> None:
    body = "See [[Notes/Insights/progressive-disclosure-without-mode-fragmentation|disclosure rule]]."
    new_body, _ = normalize_body_wikilinks(body, vault)
    assert "|disclosure rule]]" in new_body
    assert (
        "[[Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation|"
        in new_body
    )


def test_body_normalization_skips_fenced_code(vault: Path) -> None:
    """Wikilinks inside ``` ... ``` blocks must NOT be rewritten — they are
    docs, regex examples, or shell snippets where the brackets are content."""
    body = (
        "Normal: [[Notes/Insights/progressive-disclosure-without-mode-fragmentation]]\n"
        "\n"
        "```\n"
        "[[Should/Not/Be/Rewritten]]\n"
        "```\n"
    )
    new_body, _ = normalize_body_wikilinks(body, vault)
    # Outside the fence: rewrote.
    assert "Knowledge Base/Notes/Insights/progressive-disclosure" in new_body
    # Inside the fence: untouched.
    assert "[[Should/Not/Be/Rewritten]]" in new_body
    assert "Knowledge Base/Should/Not/Be/Rewritten" not in new_body


def test_body_normalization_skips_inline_code(vault: Path) -> None:
    """`[[X]]` inside backticks is regex example or shell quoting, not a link."""
    body = "Pattern `[[:space:]]` matches whitespace; real link [[Notes/Insights/progressive-disclosure-without-mode-fragmentation]]."
    new_body, _ = normalize_body_wikilinks(body, vault)
    assert "`[[:space:]]`" in new_body  # inline code preserved
    assert "Knowledge Base/Notes/Insights/progressive-disclosure" in new_body


def test_find_body_wikilinks_excludes_code_blocks(vault: Path) -> None:
    """The wikilink scanner returns only matches outside code regions."""
    body = (
        "Real [[A]]\n"
        "```\n"
        "[[B]]\n"
        "```\n"
        "Another `[[:space:]]` in inline code.\n"
        "Real again [[C]]\n"
    )
    matches = find_body_wikilinks(body)
    targets = [m.group(1).strip() for m in matches]
    assert targets == ["A", "C"]


def test_unresolved_body_links_pass_through_with_warning(vault: Path) -> None:
    body = "Forward ref [[Knowledge Base/Notes/Insights/does-not-exist-yet]]."
    new_body, warnings = normalize_body_wikilinks(body, vault)
    assert "[[Knowledge Base/Notes/Insights/does-not-exist-yet]]" in new_body
    assert len(warnings) == 1
    assert "does-not-exist-yet" in warnings[0]
