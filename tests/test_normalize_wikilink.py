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
    obsidian_uses_kb_root,
    render_wikilink_target,
    render_wikilinks_for_vault,
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
    even when the filename is date-prefixed.
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


def test_targeted_paths_keep_parser_and_precedence_without_building_a_resolver(tmp_path):
    resolver = WikilinkResolver.from_entries(tmp_path, [
        ("Notes/root.md", None),
        ("Knowledge Base/Notes/root.md", None),
        ("Knowledge Base/Notes/only.md", None),
        ("Knowledge Base/Knowledge Base/doubled.md", None),
    ])

    def unnecessary_resolver():
        pytest.fail("an explicit path built a stem/title index")

    for raw in (
        "[[Notes/root.md#section|label]]", "Notes/only", "Knowledge Base/Notes/only",
        "Knowledge Base/doubled", "Notes/missing", "Notes/./root", "Notes//root",
    ):
        assert normalize_wikilink(
            raw, tmp_path, targeted_namespace_contains=resolver.full_paths.__contains__,
            resolver_factory=unnecessary_resolver,
        ) == normalize_wikilink(raw, tmp_path, resolver=resolver)


def test_targeted_normalization_keeps_bare_name_ambiguity_and_title_lookup(tmp_path):
    resolver = WikilinkResolver.from_entries(tmp_path, [
        ("Knowledge Base/Notes/one.md", "Canonical title"),
        ("Knowledge Base/Notes/duplicate.md", None),
        ("Reference/duplicate.md", None),
    ])
    for raw in ("one", "Canonical title", "duplicate", "missing"):
        assert normalize_wikilink(
            raw, tmp_path, targeted_namespace_contains=lambda _candidate: False,
            resolver_factory=lambda: resolver,
        ) == normalize_wikilink(raw, tmp_path, resolver=resolver)


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


def test_colliding_extension_keys_keep_each_pages_title_visibility_and_physical_path(
    tmp_path: Path,
) -> None:
    from exomem import semantic_contract
    from exomem.governance import egress

    single = "Knowledge Base/Notes/plan.MD"
    double = single + ".md"
    for path, title in ((single, "First title"), (double, "Second title")):
        page = tmp_path / path
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(
            f"---\ntype: insight\nstatus: active\ntitle: {title}\n---\nBody\n",
            encoding="utf-8",
        )
    source = "Knowledge Base/Notes/source.md"
    (tmp_path / source).write_text(
        "---\ntype: insight\nstatus: active\ntitle: Source\n---\n[[Second title]]\n",
        encoding="utf-8",
    )
    resolver = WikilinkResolver(tmp_path)
    corpus = semantic_contract.build_corpus_context(tmp_path)
    visible = egress.page_filter_over(tmp_path, lambda path: path == double)

    assert normalize_wikilink("Second title", tmp_path, resolver=resolver, visible=visible) == (
        single, None
    )
    with pytest.raises(UnresolvedWikilinkError):
        normalize_wikilink("First title", tmp_path, resolver=resolver, visible=visible, strict=True)
    assert semantic_contract._resolve_reference_wikilink_from_context(
        corpus, "Second title", visible
    ).path == double
    assert semantic_contract._resolve_reference_wikilink_from_context(
        corpus, "First title", visible
    ).status == "unresolved"
    assert semantic_contract._resolve_reference_wikilink_from_context(
        corpus, single
    ).status == "ambiguous"
    assert semantic_contract._resolve_target(
        tmp_path, "Second title", resolver, visible
    )[:2] == ("resolved", double)
    assert semantic_contract._qualifying_body_wikilink_targets(
        corpus.pages[source], corpus, visible
    ) == (double,)
    assert normalize_wikilink(
        single, tmp_path, targeted_namespace_contains=lambda _key: True,
        resolver_factory=lambda: resolver, visible=visible,
    ) == (single, None)
    with pytest.raises(AmbiguousWikilinkError):
        normalize_wikilink(single, tmp_path, resolver=resolver, strict=True)
    only_single = egress.page_filter_over(tmp_path, lambda path: path == single)
    assert semantic_contract._resolve_reference_wikilink_from_context(
        corpus, "First title", only_single
    ).path == single
    assert semantic_contract._resolve_reference_wikilink_from_context(
        corpus, "Second title", only_single
    ).status == "unresolved"


def test_retitle_and_delete_preserve_the_other_owner_of_a_colliding_extension_key(
    tmp_path: Path,
) -> None:
    single = "Knowledge Base/Notes/plan.MD"
    double = single + ".md"
    for path, title in ((single, "First title"), (double, "Second title")):
        page = tmp_path / path
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(f"---\ntitle: {title}\n---\nBody\n", encoding="utf-8")
    resolver = WikilinkResolver(tmp_path)
    detached = resolver.fork()
    resolver.on_entries_changed([(single, "Renamed title")], [])
    assert normalize_wikilink("Second title", tmp_path, resolver=resolver) == (single, None)
    assert normalize_wikilink("First title", tmp_path, resolver=resolver)[1] is not None
    (tmp_path / single).write_text("---\ntitle: Renamed title\n---\nBody\n", encoding="utf-8")
    resolver.on_files_changed(tmp_path, [single], [single])
    fresh = WikilinkResolver(tmp_path)
    for target in ("First title", "Second title", "Renamed title", "plan.MD"):
        assert normalize_wikilink(target, tmp_path, resolver=resolver) == normalize_wikilink(
            target, tmp_path, resolver=fresh
        )
    assert normalize_wikilink("First title", tmp_path, resolver=detached) == (single, None)
    assert normalize_wikilink("Renamed title", tmp_path, resolver=detached)[1] is not None

    (tmp_path / single).unlink()
    resolver.on_entries_changed([], [single])
    fresh = WikilinkResolver(tmp_path)
    assert normalize_wikilink("Second title", tmp_path, resolver=resolver) == (single, None)
    for target in ("Second title", "Renamed title", "plan.MD"):
        assert normalize_wikilink(target, tmp_path, resolver=resolver) == normalize_wikilink(
            target, tmp_path, resolver=fresh
        )


def test_normalizer_keeps_stem_priority_while_semantic_references_union_title_matches(
    tmp_path: Path,
) -> None:
    from exomem import semantic_contract

    for path, title in (("topic.md", "Other title"), ("other.md", "topic")):
        (tmp_path / path).write_text(f"---\ntitle: {title}\n---\nBody\n", encoding="utf-8")
    resolver = WikilinkResolver(tmp_path)
    corpus = semantic_contract.build_corpus_context(tmp_path)

    assert normalize_wikilink("topic", tmp_path, resolver=resolver) == ("topic", None)
    assert semantic_contract._resolve_reference_wikilink_from_context(
        corpus, "topic"
    ).status == "ambiguous"


def test_on_files_changed_same_rel_in_both_lists_retains_live_file(vault: Path) -> None:
    """A rel present in BOTH `changed_rels` and `deleted_rels` in the same batch
    — the two-path-string-forms-collapse-to-one-rel case (Windows 8.3 short
    names are the concrete vector, #126: a watchdog event reporting a long
    slug's `REAL-V~1.MD` short alias vs. the walk's long form) — must not drop
    a file that is still on disk. The old code (`changed = _norm(changed_rels)
    - deleted`) unconditionally subtracted the conflicting rel out of
    `changed`, so both the remove-and-re-add loops skipped it entirely and the
    resolver silently lost a live file's entry. The filesystem breaks the tie:
    re-add wins."""
    target = vault / "Knowledge Base" / "Notes" / "Failures" / "dual-form.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('---\ntitle: "Dual Form"\n---\n\nbody\n', encoding="utf-8")
    rel = "Knowledge Base/Notes/Failures/dual-form"

    resolver = WikilinkResolver(vault)
    assert rel in resolver.full_paths

    resolver.on_files_changed(vault, [rel + ".md"], [rel + ".md"])

    assert rel in resolver.full_paths, (
        "resolver dropped a live on-disk file when its rel appeared in both "
        "changed_rels and deleted_rels in the same batch"
    )
    canonical, warning = normalize_wikilink(rel, vault, resolver=resolver)
    assert warning is None
    assert canonical == rel


def test_body_normalization_rewrites_kb_relative_to_full(vault: Path) -> None:
    body = "See [[Notes/Insights/progressive-disclosure-without-mode-fragmentation]] for context."
    new_body, warnings = normalize_body_wikilinks(body, vault)
    assert (
        "[[Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation]]"
        in new_body
    )
    assert warnings == []


def test_body_normalization_uses_kb_relative_links_for_nested_obsidian_root(
    vault: Path,
) -> None:
    (vault / "Knowledge Base" / ".obsidian").mkdir()
    target = (
        "Knowledge Base/Notes/Insights/"
        "progressive-disclosure-without-mode-fragmentation"
    )

    new_body, warnings = normalize_body_wikilinks(
        f"See [[{target}#mechanism|the mechanism]].", vault
    )

    assert obsidian_uses_kb_root(vault) is True
    assert warnings == []
    assert (
        "[[Notes/Insights/progressive-disclosure-without-mode-fragmentation"
        "#mechanism|the mechanism]]"
    ) in new_body
    assert "[[Knowledge Base/" not in new_body
    canonical, warning = normalize_wikilink(
        "Notes/Insights/progressive-disclosure-without-mode-fragmentation", vault
    )
    assert warning is None
    assert canonical.startswith("Knowledge Base/")
    assert render_wikilink_target(canonical, vault).startswith("Notes/")


def test_parent_obsidian_root_keeps_full_vault_rooted_links(vault: Path) -> None:
    (vault / ".obsidian").mkdir()
    target = "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation"

    new_body, warnings = normalize_body_wikilinks(f"See [[{target}]].", vault)

    assert obsidian_uses_kb_root(vault) is False
    assert warnings == []
    assert f"[[{target}]]" in new_body


def test_generated_markdown_renderer_preserves_code_and_aliases(vault: Path) -> None:
    (vault / "Knowledge Base" / ".obsidian").mkdir()
    text = (
        "[[Knowledge Base/Notes/Insights/foo|Foo]]\n"
        "`[[Knowledge Base/Notes/Insights/in-code]]`\n"
    )

    rendered = render_wikilinks_for_vault(text, vault)

    assert "[[Notes/Insights/foo|Foo]]" in rendered
    assert "`[[Knowledge Base/Notes/Insights/in-code]]`" in rendered


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


def test_an_unclosed_backtick_does_not_hide_links_on_later_lines() -> None:
    """A stray backtick is not inline code: the real link after it stays a link."""
    matches = find_body_wikilinks("`unclosed\n\n[[Actual]] with `unclosed")
    assert [match.group(1) for match in matches] == ["Actual"]


def test_unresolved_body_links_pass_through_with_warning(vault: Path) -> None:
    body = "Forward ref [[Knowledge Base/Notes/Insights/does-not-exist-yet]]."
    new_body, warnings = normalize_body_wikilinks(body, vault)
    assert "[[Knowledge Base/Notes/Insights/does-not-exist-yet]]" in new_body
    assert len(warnings) == 1
    assert "does-not-exist-yet" in warnings[0]
