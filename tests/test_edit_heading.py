"""edit() heading/section-targeted mode (the 4th edit mode)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from kb_mcp import edit as edit_module
from kb_mcp import find as find_module

NOTE_REL = "Knowledge Base/Notes/Insights/heading-test.md"

BODY = """\
---
type: insight
status: active
created: 2026-01-01
updated: 2026-01-01
---

# Heading Test

## Overview

Intro line.

## Details

Detail line.

## Connections
"""


def _seed(vault: Path) -> Path:
    p = vault / NOTE_REL
    p.write_text(BODY, encoding="utf-8")
    find_module.clear_cache()
    return p


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_append_lands_at_end_of_section(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _seed(vault)
    edit_module.edit(
        vault, path=NOTE_REL, why="add a point",
        heading="Overview", section_position="append", new_string="Appended line.",
    )
    text = _read(p)
    assert "Appended line." in text
    # Stays inside Overview — before the next heading.
    assert text.index("Appended line.") < text.index("## Details")
    assert text.index("Intro line.") < text.index("Appended line.")


def test_prepend_lands_right_after_heading(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _seed(vault)
    edit_module.edit(
        vault, path=NOTE_REL, why="lead with this",
        heading="Overview", section_position="prepend", new_string="Prepended line.",
    )
    text = _read(p)
    assert text.index("## Overview") < text.index("Prepended line.") < text.index("Intro line.")


def test_replace_swaps_section_body(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _seed(vault)
    edit_module.edit(
        vault, path=NOTE_REL, why="rewrite",
        heading="Overview", section_position="replace", new_string="Brand new overview.",
    )
    text = _read(p)
    assert "Brand new overview." in text
    assert "Intro line." not in text
    assert "Detail line." in text  # other sections untouched


def test_hash_prefixed_heading_arg_matches(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _seed(vault)
    edit_module.edit(
        vault, path=NOTE_REL, why="markers ok",
        heading="## Overview", section_position="append", new_string="With markers.",
    )
    assert "With markers." in _read(p)


def test_heading_not_found_raises(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(vault)
    with pytest.raises(edit_module.EditError) as ei:
        edit_module.edit(
            vault, path=NOTE_REL, why="x",
            heading="Nonexistent", section_position="append", new_string="y",
        )
    assert ei.value.code == "HEADING_NOT_FOUND"


def test_heading_mutually_exclusive_with_new_body(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(vault)
    with pytest.raises(edit_module.EditError) as ei:
        edit_module.edit(
            vault, path=NOTE_REL, why="x",
            heading="Overview", new_string="y", new_body="whole new body",
        )
    assert ei.value.code == "INVALID_EDIT"


def test_heading_mutually_exclusive_with_old_string(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(vault)
    with pytest.raises(edit_module.EditError) as ei:
        edit_module.edit(
            vault, path=NOTE_REL, why="x",
            heading="Overview", new_string="y", old_string="Intro line.",
        )
    assert ei.value.code == "INVALID_EDIT"


def test_heading_requires_new_string(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(vault)
    with pytest.raises(edit_module.EditError) as ei:
        edit_module.edit(vault, path=NOTE_REL, why="x", heading="Overview")
    assert ei.value.code == "INVALID_EDIT"


def test_bad_section_position_rejected(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(vault)
    with pytest.raises(edit_module.EditError) as ei:
        edit_module.edit(
            vault, path=NOTE_REL, why="x",
            heading="Overview", section_position="sideways", new_string="y",
        )
    assert ei.value.code == "INVALID_EDIT"


def test_bumps_updated_and_writes_log(vault, monkeypatch: pytest.MonkeyPatch) -> None:
    p = _seed(vault)
    today = dt.date.today().isoformat()
    edit_module.edit(
        vault, path=NOTE_REL, why="section edit auditable",
        heading="Overview", section_position="append", new_string="Logged line.",
    )
    text = _read(p)
    assert f"updated: {today}" in text
    log_md = (vault / "Knowledge Base" / "log.md").read_text(encoding="utf-8")
    assert "section edit auditable" in log_md
    assert "heading-test" in log_md
