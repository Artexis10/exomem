"""replace tool tests — supersession chain integrity per SKILL rule 6."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from kb_mcp import note as note_module
from kb_mcp import replace as replace_module


TODAY = dt.date(2026, 5, 25)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _fm(p: Path) -> dict:
    fm = _read(p).split("\n---\n")[0].removeprefix("---\n")
    return yaml.safe_load(fm)


def _make_insight(vault: Path, title: str) -> str:
    """Create a fresh insight via note() so the supersession chain has a real target."""
    result = note_module.note(
        vault,
        content=f"# {title}\n\nbody.\n",
        note_type="insight",
        title=title,
        today=TODAY,
    )
    return result.path


def test_replace_writes_new_and_flips_old(vault: Path) -> None:
    old_rel = _make_insight(vault, "Old Insight v1")
    result = replace_module.replace(
        vault,
        old_path=old_rel,
        content="# Old Insight v2\n\nrevised body.\n",
        note_type="insight",
        title="Old Insight v2",
        today=TODAY,
    )
    new_abs = vault / result.new_path
    old_abs = vault / result.old_path
    assert new_abs.exists()
    assert old_abs.exists()  # old stays; never deleted

    new_fm = _fm(new_abs)
    old_fm = _fm(old_abs)
    assert old_fm["status"] == "superseded"
    # superseded_by: list-of-string-wikilinks (parsed by YAML)
    superseded_by = old_fm["superseded_by"]
    assert isinstance(superseded_by, list)
    assert any(result.new_path.removesuffix(".md") in s for s in superseded_by)
    # New page declares supersedes:
    supersedes = new_fm["supersedes"]
    assert old_rel.removesuffix(".md") in str(supersedes)


def test_replace_bumps_old_updated_date(vault: Path) -> None:
    old_rel = _make_insight(vault, "Bumped Insight")
    later = dt.date(2026, 6, 1)
    replace_module.replace(
        vault,
        old_path=old_rel,
        content="# Bumped v2\n\nbody.\n",
        note_type="insight",
        title="Bumped v2",
        today=later,
    )
    old_fm = _fm(vault / old_rel)
    assert old_fm["updated"] == later


def test_replace_refuses_when_old_in_sources(vault: Path) -> None:
    """SKILL rule 2: Sources/ is append-only — can't be superseded."""
    src_rel = "Knowledge Base/Sources/Articles/2026-05-04-best-egcg-supplements.md"
    with pytest.raises(replace_module.ReplaceError) as exc:
        replace_module.replace(
            vault,
            old_path=src_rel,
            content="# x\n",
            note_type="insight",
            title="x",
            today=TODAY,
        )
    assert exc.value.code == "INVALID_REPLACE"


def test_replace_refuses_when_old_already_superseded(vault: Path) -> None:
    old_rel = _make_insight(vault, "Double Superseded")
    replace_module.replace(
        vault,
        old_path=old_rel,
        content="# v2\n",
        note_type="insight",
        title="Double Superseded v2",
        today=TODAY,
    )
    with pytest.raises(replace_module.ReplaceError) as exc:
        replace_module.replace(
            vault,
            old_path=old_rel,  # already superseded
            content="# v3\n",
            note_type="insight",
            title="Double Superseded v3",
            today=TODAY,
        )
    assert exc.value.code == "ALREADY_SUPERSEDED"


def test_replace_refuses_when_old_not_found(vault: Path) -> None:
    with pytest.raises(replace_module.ReplaceError) as exc:
        replace_module.replace(
            vault,
            old_path="Knowledge Base/Notes/Insights/nope.md",
            content="# x\n",
            note_type="insight",
            title="x",
            today=TODAY,
        )
    assert exc.value.code == "OLD_NOT_FOUND"


def test_replace_logs_with_reason(vault: Path) -> None:
    old_rel = _make_insight(vault, "Reasoned Replace")
    replace_module.replace(
        vault,
        old_path=old_rel,
        content="# v2\n",
        note_type="insight",
        title="Reasoned Replace v2",
        reason="Old framing was too narrow; broader scope here.",
        today=TODAY,
    )
    log = _read(vault / "Knowledge Base" / "log.md")
    assert "## [2026-05-25] replace |" in log
    assert "Old framing was too narrow" in log


def test_replace_does_not_retarget_inbound_wikilinks(vault: Path) -> None:
    """Rule 6: readers follow the supersession chain; inbound links stay on old."""
    old_rel = _make_insight(vault, "Linked Insight")
    referrer = vault / "Knowledge Base" / "Notes" / "Insights" / "links-to-old.md"
    old_no_ext = old_rel.removesuffix(".md")
    referrer.write_text(
        f"---\ntype: insight\ncreated: 2026-05-23\nupdated: 2026-05-23\ntags: []\n---\n"
        f"# Linker\n\nSee [[{old_no_ext}]].\n",
        encoding="utf-8",
    )
    replace_module.replace(
        vault,
        old_path=old_rel,
        content="# v2\n",
        note_type="insight",
        title="Linked Insight v2",
        today=TODAY,
    )
    referrer_text = _read(referrer)
    # Wikilink to old still points at old (unchanged).
    assert f"[[{old_no_ext}]]" in referrer_text


def test_replace_propagates_new_note_validation_errors(vault: Path) -> None:
    """If the new-page args are invalid, the supersession is aborted."""
    old_rel = _make_insight(vault, "Validation Source")
    with pytest.raises((note_module.NoteError, ValueError)):
        replace_module.replace(
            vault,
            old_path=old_rel,
            content="# x\n",
            note_type="research-note",
            title="needs-project",
            # missing required `project` for research-note → NoteError
            today=TODAY,
        )
    # Old should be untouched (no half-state)
    old_fm = _fm(vault / old_rel)
    assert old_fm.get("status") != "superseded"
