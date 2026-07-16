"""multi_edit: several surgical pairs against one page in ONE commit.

One atomic write → one embedding re-sync → one log entry → one `updated:` bump,
instead of N separate edit() calls. Pairs apply sequentially in memory (pair K
matches the result of pair K-1). A failing pair aborts the whole batch before
the write, so nothing partial lands.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from exomem import edit as edit_module
from exomem import get_page as get_page_module
from exomem import multi_edit as multi_edit_module

TODAY = dt.date(2026, 6, 1)


def _make_page(vault: Path, body: str) -> str:
    rel = "Knowledge Base/Notes/Research/Taste/scratch-taste.md"
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\ntype: research-note\nproject: taste\nstatus: active\n"
        "created: 2026-05-29\nupdated: 2026-05-29\ntags: [taste]\n---\n" + body,
        encoding="utf-8",
    )
    return rel


def test_multi_edit_applies_all_pairs_in_one_write(vault: Path) -> None:
    rel = _make_page(vault, "# Scratch\n\nalpha\nbeta\ngamma\n")
    result = multi_edit_module.multi_edit(
        vault,
        path=rel,
        why="batch rename",
        edits=[
            {"old_string": "alpha", "new_string": "ALPHA"},
            {"old_string": "gamma", "new_string": "GAMMA"},
        ],
        today=TODAY,
    )
    text = (vault / rel).read_text(encoding="utf-8")
    assert "ALPHA" in text and "GAMMA" in text
    assert "beta" in text  # untouched
    assert result.edits_applied == 2
    assert "updated: 2026-06-01" in text


def test_multi_edit_sequential_semantics(vault: Path) -> None:
    """Pair 2's old_string only exists after pair 1 runs."""
    rel = _make_page(vault, "# Scratch\n\nfoo\n")
    multi_edit_module.multi_edit(
        vault,
        path=rel,
        why="chain",
        edits=[
            {"old_string": "foo", "new_string": "bar"},
            {"old_string": "bar", "new_string": "baz"},
        ],
        today=TODAY,
    )
    text = (vault / rel).read_text(encoding="utf-8")
    assert "baz" in text
    assert "foo" not in text and "\nbar\n" not in text


def test_multi_edit_aborts_on_failing_pair_no_partial_commit(vault: Path) -> None:
    rel = _make_page(vault, "# Scratch\n\nalpha\nbeta\n")
    before = (vault / rel).read_text(encoding="utf-8")
    with pytest.raises(edit_module.EditError) as exc:
        multi_edit_module.multi_edit(
            vault,
            path=rel,
            why="batch",
            edits=[
                {"old_string": "alpha", "new_string": "ALPHA"},
                {"old_string": "does-not-exist", "new_string": "X"},
            ],
            today=TODAY,
        )
    assert exc.value.code == "STRING_NOT_FOUND"
    assert "edit #1" in exc.value.reason  # identifies the failing pair
    # Nothing committed — pair 0's change must NOT have landed.
    assert (vault / rel).read_text(encoding="utf-8") == before


def test_multi_edit_per_pair_replace_all(vault: Path) -> None:
    rel = _make_page(vault, "# Scratch\n\nq q q\nkeep\n")
    multi_edit_module.multi_edit(
        vault,
        path=rel,
        why="batch",
        edits=[{"old_string": "q", "new_string": "w", "replace_all": True}],
        today=TODAY,
    )
    text = (vault / rel).read_text(encoding="utf-8")
    assert "w w w" in text
    assert "q q q" not in text


def test_multi_edit_writes_exactly_one_log_entry(vault: Path) -> None:
    rel = _make_page(vault, "# Scratch\n\nalpha\nbeta\n")
    log_path = vault / "Knowledge Base" / "log.md"
    before = log_path.read_text(encoding="utf-8").count("## [")
    multi_edit_module.multi_edit(
        vault,
        path=rel,
        why="batch",
        edits=[
            {"old_string": "alpha", "new_string": "ALPHA"},
            {"old_string": "beta", "new_string": "BETA"},
        ],
        today=TODAY,
    )
    after = log_path.read_text(encoding="utf-8").count("## [")
    assert after == before + 1


def test_multi_edit_honors_expected_hash(vault: Path) -> None:
    rel = _make_page(vault, "# Scratch\n\nalpha\n")
    g = get_page_module.get_page(vault, path=rel)
    p = vault / rel
    p.write_text(p.read_text(encoding="utf-8") + "\nappended\n", encoding="utf-8")
    with pytest.raises(edit_module.EditError) as exc:
        multi_edit_module.multi_edit(
            vault,
            path=rel,
            why="batch",
            edits=[{"old_string": "alpha", "new_string": "ALPHA"}],
            expected_hash=g.content_hash,
            today=TODAY,
        )
    assert exc.value.code == "STALE_EDIT"


def test_multi_edit_validate_only_reports_per_pair_counts(vault: Path) -> None:
    rel = _make_page(vault, "# Scratch\n\nfoo\nfoo\nbar\n")
    result = multi_edit_module.multi_edit(
        vault,
        path=rel,
        why="preview",
        edits=[
            {"old_string": "foo", "new_string": "X", "replace_all": True},
            {"old_string": "bar", "new_string": "Y"},
        ],
        validate_only=True,
        today=TODAY,
    )
    assert result.validate_only is True
    assert result.edits[0]["match_count"] == 2
    assert result.edits[1]["match_count"] == 1
    # Nothing written.
    text = (vault / rel).read_text(encoding="utf-8")
    assert "foo" in text
    assert "updated: 2026-05-29" in text


def test_multi_edit_refuses_sources(vault: Path) -> None:
    with pytest.raises(edit_module.EditError) as exc:
        multi_edit_module.multi_edit(
            vault,
            path="Knowledge Base/Sources/Articles/2026-05-04-best-egcg-supplements.md",
            why="should fail",
            edits=[{"old_string": "x", "new_string": "y"}],
            today=TODAY,
        )
    assert exc.value.code == "INVALID_EDIT"
    assert "append-only" in exc.value.reason.lower()


def test_multi_edit_empty_edits_list_errors(vault: Path) -> None:
    rel = _make_page(vault, "# Scratch\n\nbody.\n")
    with pytest.raises(edit_module.EditError) as exc:
        multi_edit_module.multi_edit(vault, path=rel, why="x", edits=[], today=TODAY)
    assert exc.value.code == "INVALID_EDIT"
