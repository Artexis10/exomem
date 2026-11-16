"""The managed-root helpers take an optional layout: `nested` keeps today's strings, `root` has no wrapper."""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import held_fs, kbdir, note_types
from exomem import vault as vault_module
from exomem.kbdir import Layout, Managed
from tests.conftest import bind_root_vault

# Today's answer for each branch of the nested rule, spelled out for the default root name:
# below the root, the root with its slash, the bare name, outside it, a dot folder, staging.
NESTED_SPLITS = {
    "Knowledge Base/Notes/Ideas/a.md": "Notes/Ideas/a.md",
    "Knowledge Base/": "",
    "Knowledge Base": None,
    "Other/x.md": None,
    ".obsidian/app.json": None,
    "_Imports/0b9f/page.md": None,
}


def test_nested_strings_are_identical_with_the_layout_given_and_without_it() -> None:
    """A caller that passes `nested` (or nothing) must keep every path that exists today."""
    for given in (None, Layout.NESTED, "nested"):
        assert kbdir.managed_join("Notes/a.md", layout=given) == "Knowledge Base/Notes/a.md"
        assert kbdir.managed_join("", layout=given) == "Knowledge Base/"
        splitter = kbdir.managed_splitter(layout=given)
        for path, expected in NESTED_SPLITS.items():
            assert kbdir.managed_split(path, layout=given) == expected
            assert splitter(path) == expected
        parts = kbdir.managed_split_parts(("Knowledge Base", "Notes", "a.md"), layout=given)
        assert parts == ("Notes", "a.md")
        assert kbdir.managed_split_parts(("Other", "x.md"), layout=given) is None


def test_nested_strings_follow_the_root_name_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """`EXOMEM_KB_DIRNAME` renames the wrapper for tests; the layout argument must not bypass it."""
    monkeypatch.setenv("EXOMEM_KB_DIRNAME", "Wiki")

    assert kbdir.managed_join("a.md", layout=Layout.NESTED) == "Wiki/a.md"
    assert kbdir.managed_split("Wiki/a.md", layout=Layout.NESTED) == "a.md"
    assert kbdir.managed_split("Knowledge Base/a.md", layout=Layout.NESTED) is None


def test_root_join_adds_no_wrapper() -> None:
    """In `root` the vault root is the managed root, so a managed path is already vault-relative."""
    assert kbdir.managed_join("Notes/a.md", layout=Layout.ROOT) == "Notes/a.md"
    assert kbdir.managed_join("", layout="root") == ""


def test_root_split_keeps_the_path_and_refuses_what_sits_outside_the_managed_root() -> None:
    """A dot folder, the import staging folder and a recorded `unmanaged` entry are not Exomem's pages."""
    outside = ("Archive",)
    kept = (
        "Ideas/a.md",
        "Archived/a.md",  # shares a prefix with an unmanaged name, which is not the name
        "_Schema/vault-layout.yaml",
        "_ImportsBackup/a.md",
        "",
    )
    refused = (
        ".obsidian/app.json",
        ".exomem/schema/SKILL.md",
        "_Imports",
        "_Imports/0b9f2c1e/page.md",
        "Archive",
        "Archive/old.md",
    )

    splitter = kbdir.managed_splitter(layout=Layout.ROOT, unmanaged=outside)
    for path in kept:
        assert kbdir.managed_split(path, layout=Layout.ROOT, unmanaged=outside) == path
        assert splitter(path) == path
    for path in refused:
        assert kbdir.managed_split(path, layout=Layout.ROOT, unmanaged=outside) is None
        assert splitter(path) is None


def test_root_split_compares_an_unmanaged_name_through_the_callers_key() -> None:
    """A caller whose own rule ignores letter case must see `archive/` as the unmanaged `Archive`."""
    assert kbdir.managed_split("archive/old.md", layout=Layout.ROOT, unmanaged=("Archive",)) == "archive/old.md"
    assert (
        kbdir.managed_split(
            "archive/old.md", layout=Layout.ROOT, unmanaged=("Archive",), key=str.casefold
        )
        is None
    )


def test_root_split_parts_keeps_the_components_and_refuses_the_same_names() -> None:
    assert kbdir.managed_split_parts(("Ideas", "a.md"), layout=Layout.ROOT) == ("Ideas", "a.md")
    assert kbdir.managed_split_parts((), layout=Layout.ROOT) == ()
    assert kbdir.managed_split_parts((".obsidian", "app.json"), layout=Layout.ROOT) is None
    assert kbdir.managed_split_parts(("_Imports", "0b9f2c1e", "page.md"), layout=Layout.ROOT) is None
    assert (
        kbdir.managed_split_parts(("Archive", "old.md"), layout=Layout.ROOT, unmanaged=("Archive",)) is None
    )


def test_an_unknown_layout_is_refused_instead_of_read_as_nested() -> None:
    """A misspelt layout that read as `nested` would put every path under a wrapper that is not there."""
    with pytest.raises(ValueError):
        kbdir.managed_join("a.md", layout="sideways")


def test_the_unbound_default_is_nested_with_no_unmanaged_entry() -> None:
    """A process that never resolves a `root` vault reads the wrapper folder everywhere, as it always did."""
    assert kbdir.bound() == Managed(Layout.NESTED)
    assert kbdir.managed_join("Notes/a.md") == "Knowledge Base/Notes/a.md"
    assert kbdir.managed_split("Knowledge Base/Notes/a.md") == "Notes/a.md"
    assert kbdir.managed_root_rel() == "Knowledge Base"
    assert kbdir.managed_root_key() == "Knowledge Base"
    assert kbdir.managed_root_parts() == ("Knowledge Base",)
    assert note_types.compiled_parent() == ("Notes",)


def test_binding_a_root_layout_switches_the_helpers_that_get_no_layout() -> None:
    """Every helper that reads the bound value, including the ones with no layout argument at all."""
    kbdir.bind(Managed(Layout.ROOT, ("Archive",)))

    assert kbdir.managed_join("Notes/a.md") == "Notes/a.md"
    assert kbdir.managed_split("Ideas/a.md") == "Ideas/a.md"
    assert kbdir.managed_split("Archive/old.md") is None  # the bound record's unmanaged entry
    assert kbdir.managed_root_rel() == held_fs.ROOT_RELATIVE
    assert kbdir.managed_root_key() == ""
    assert kbdir.managed_root_parts() == ()
    assert note_types.compiled_parent() == ()
    assert kbdir.managed_state() == (held_fs.ROOT_RELATIVE, ("Archive",))
    # A call that names its layout still gets that layout, whatever is bound.
    assert kbdir.managed_join("Notes/a.md", layout=Layout.NESTED) == "Knowledge Base/Notes/a.md"
    assert kbdir.managed_split("Archive/old.md", layout=Layout.NESTED) is None


def test_resolving_a_root_vault_binds_it_and_leaves_it_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First of an ordered pair: this test leaves the process bound to a `root` vault."""
    bind_root_vault(tmp_path / "rootvault", monkeypatch)

    assert kbdir.bound().layout is Layout.ROOT
    assert kbdir.managed_join("a.md") == "a.md"


def test_the_next_test_starts_unbound_again() -> None:
    """Second of the pair: the conftest reset must not hand the previous test's `root` binding on."""
    assert kbdir.bound() == Managed(Layout.NESTED)


def test_the_last_vault_resolved_wins(vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A process serves a single vault: resolving a `nested` vault after a `root` vault binds `nested` again."""
    bind_root_vault(tmp_path / "rootvault", monkeypatch)
    assert kbdir.bound().layout is Layout.ROOT

    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    vault_module.resolve_vault()

    assert kbdir.bound().layout is Layout.NESTED


def _validate_managed_root_anchor(root: Path) -> None:
    """What the custody and replica writers do with the managed root: anchor it, then check the anchor."""
    with (
        held_fs.acquire(root).require() as filesystem,
        filesystem.parent(kbdir.managed_root_rel(), access="flush").require() as anchor,
    ):
        # The vault root of a `root` vault has no name in a parent, so it is checked as unnamed.
        filesystem.validate_directory(anchor, require_name=bool(getattr(anchor, "named", False))).require()


def test_the_nested_managed_root_is_a_held_filesystem_anchor(vault: Path) -> None:
    _validate_managed_root_anchor(vault)


def test_the_vault_root_of_a_root_vault_is_a_held_filesystem_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _validate_managed_root_anchor(bind_root_vault(tmp_path / "rootvault", monkeypatch))
