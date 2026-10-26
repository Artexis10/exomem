"""edit() heading/section-targeted mode (the 4th edit mode)."""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import edit as edit_module
from exomem import find as find_module
from exomem import temporal

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
    # `temporal.stamp` folds the instant to UTC for storage, so the stamp's date
    # is the UTC day, not the local one. Asserting against `date.today()` failed
    # for every run between UTC midnight and local midnight.
    today = temporal.stamp(temporal.now())[: len("0000-00-00")]
    edit_module.edit(
        vault, path=NOTE_REL, why="section edit auditable",
        heading="Overview", section_position="append", new_string="Logged line.",
    )
    text = _read(p)
    assert f"updated: {today}" in text
    log_md = (vault / "Knowledge Base" / "log.md").read_text(encoding="utf-8")
    assert "section edit auditable" in log_md
    assert "heading-test" in log_md


def test_an_existing_relations_section_is_reused_whatever_its_casing(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The relation parser reads `## relations` as the Relations section, so a
    # create-missing write must append there instead of adding a second heading.
    p = vault / NOTE_REL
    text = BODY.replace("## Connections", "## relations\n\n- part_of [[Alpha]]")
    p.write_text(text, encoding="utf-8")
    find_module.clear_cache()
    edit_module.edit(
        vault, path=NOTE_REL, why="accept a relation", heading="Relations",
        section_position="append", new_string="- supports [[Beta]]",
        create_missing_section=True,
    )
    text = _read(p)
    assert text.lower().count("## relations") == 1
    assert text.index("- part_of [[Alpha]]") < text.index("- supports [[Beta]]")


def test_a_heading_inside_a_code_fence_is_not_a_section(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = vault / NOTE_REL
    p.write_text(
        BODY.replace("## Connections", "```\n## Relations\n```"), encoding="utf-8",
    )
    find_module.clear_cache()
    edit_module.edit(
        vault, path=NOTE_REL, why="accept a relation", heading="Relations",
        section_position="append", new_string="- supports [[Beta]]",
        create_missing_section=True,
    )
    text = _read(p)
    # The fenced example stays untouched; a real section is added after it.
    assert text.count("## Relations") == 2
    assert text.rindex("## Relations") < text.index("- supports [[Beta]]")
    assert text.index("```\n## Relations\n```") < text.rindex("## Relations")


def test_a_fenced_hash_line_does_not_end_the_section_it_sits_in(
    vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = vault / NOTE_REL
    p.write_text(
        BODY.replace("Intro line.", "Intro line.\n\n```\n# shell comment\n```"),
        encoding="utf-8",
    )
    find_module.clear_cache()
    edit_module.edit(
        vault, path=NOTE_REL, why="extend overview", heading="Overview",
        section_position="append", new_string="Tail line.",
    )
    text = _read(p)
    assert text.index("# shell comment") < text.index("Tail line.") < text.index("## Details")
