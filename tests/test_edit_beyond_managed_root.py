"""An edit aimed at a page beyond the managed root says so, and a plain miss stays a plain miss.

`get_page` reads any page in the vault by its vault-relative path, so a caller can
read `projects/foo.md` and then ask to edit it by the same string. Governed edits
write only under the managed root; the caller must be told the page is out of
reach, not that it is missing.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

from exomem import edit
from exomem.kbdir import managed_join

TODAY = dt.date(2026, 6, 1)

PAGE = (
    "---\ntype: decision\nstatus: active\ncreated: 2026-05-29\nupdated: 2026-05-29\n"
    "tags: [pricing]\n---\n\n# Pricing\n\nWe keep the flat rate.\n"
)


def _try_edit(vault: Path, path: str) -> edit.EditError:
    with pytest.raises(edit.EditError) as raised:
        edit.edit(
            vault,
            path=path,
            why="tighten the wording",
            old_string="flat rate",
            new_string="flat monthly rate",
            validate_only=True,
            today=TODAY,
        )
    return raised.value


def _write(vault: Path, rel: str, text: str = PAGE) -> Path:
    page = vault / rel
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(text, encoding="utf-8")
    return page


def test_a_page_beyond_the_managed_root_is_refused_as_out_of_reach(vault: Path) -> None:
    _write(vault, "projects/pricing.md")

    error = _try_edit(vault, "projects/pricing.md")

    assert error.code == "OUTSIDE_GOVERNED_ROOT"
    assert error.missing == ["path"]
    assert "projects/pricing.md" in error.reason
    assert managed_join("") in error.reason
    assert "does not exist" not in error.reason


def test_the_refusal_names_the_page_the_way_the_caller_typed_it_once_normalised(
    vault: Path,
) -> None:
    """`observe_memory` takes the caller's normalised text as the page's own path."""
    _write(vault, "projects/pricing.md")

    error = _try_edit(vault, "  /projects\\pricing  ")

    assert error.code == "OUTSIDE_GOVERNED_ROOT"
    assert "projects/pricing.md" in error.reason


def test_a_page_beyond_the_managed_root_is_never_written(vault: Path) -> None:
    page = _write(vault, "projects/pricing.md")

    _try_edit(vault, "projects/pricing.md")

    assert page.read_text(encoding="utf-8") == PAGE


def test_a_path_that_exists_nowhere_in_the_vault_is_not_found(vault: Path) -> None:
    error = _try_edit(vault, "invented-folder/invented-page.md")

    assert error.code == "NOT_FOUND"


def test_a_missing_page_written_with_the_managed_root_prefix_is_not_found(vault: Path) -> None:
    _write(vault, "projects/pricing.md")

    error = _try_edit(vault, managed_join("projects/pricing.md"))

    assert error.code == "NOT_FOUND"


def test_a_directory_beyond_the_managed_root_is_not_found(vault: Path) -> None:
    (vault / "projects" / "pricing.md").mkdir(parents=True)

    error = _try_edit(vault, "projects/pricing.md")

    assert error.code == "NOT_FOUND"


def test_a_symlink_that_leaves_the_vault_is_not_found(vault: Path, tmp_path: Path) -> None:
    """The refusal must not confirm that a file outside the vault exists."""
    elsewhere = _write(tmp_path / "elsewhere", "pricing.md")
    link = vault / "projects" / "pricing.md"
    link.parent.mkdir(parents=True)
    try:
        os.symlink(elsewhere, link)
    except (OSError, NotImplementedError):
        pytest.skip("this runner cannot create symlinks")

    error = _try_edit(vault, "projects/pricing.md")

    assert error.code == "NOT_FOUND"
