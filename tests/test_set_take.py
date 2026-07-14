"""set_take: fill a `[take: ]` opinion row by its natural leading text.

The server reads the note and locates the row itself, so the caller never
re-sends the (often huge) body just to compute an exact byte-match string.
A thin wrapper over `edit`'s surgical core — it inherits atomicity, the
`updated:` bump, the single log entry, and embedding re-sync.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from exomem import set_take as set_take_module

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


def test_set_take_fills_empty(vault: Path) -> None:
    rel = _make_page(
        vault,
        "# Scratch\n\n## Opinions\n\n"
        "- Whiplash (2014) — 10/10 — [take: ]  <!-- platform:imdb -->\n"
        "- Seven (1995) — 10/10 — [take: ]  <!-- platform:imdb -->\n",
    )
    result = set_take_module.set_take(
        vault, path=rel, row_key="Whiplash (2014)", take="relentless", why="fill", today=TODAY
    )
    text = (vault / rel).read_text(encoding="utf-8")
    assert (
        "- Whiplash (2014) — 10/10 — [take: relentless]  <!-- platform:imdb -->"
        in text
    )
    # The other row, including its provenance comment, is untouched.
    assert "- Seven (1995) — 10/10 — [take: ]  <!-- platform:imdb -->" in text
    assert "updated: 2026-06-01" in text
    assert "relentless" in result.row


def test_set_take_ambiguous_row_key(vault: Path) -> None:
    rel = _make_page(
        vault,
        "# Scratch\n\n"
        "- Whiplash (2014) — 10/10 — [take: ]\n"
        "- Whiplash (Reissue) — 9/10 — [take: ]\n",
    )
    with pytest.raises(set_take_module.SetTakeError) as exc:
        set_take_module.set_take(
            vault, path=rel, row_key="Whiplash", take="x", why="y", today=TODAY
        )
    assert exc.value.code == "AMBIGUOUS_ROW"
    assert len(exc.value.candidates) == 2


def test_set_take_zero_match(vault: Path) -> None:
    rel = _make_page(vault, "# Scratch\n\n- Seven (1995) — 10/10 — [take: ]\n")
    with pytest.raises(set_take_module.SetTakeError) as exc:
        set_take_module.set_take(
            vault, path=rel, row_key="Whiplash", take="x", why="y", today=TODAY
        )
    assert exc.value.code == "ROW_NOT_FOUND"


def test_set_take_already_filled_without_overwrite(vault: Path) -> None:
    rel = _make_page(vault, "# Scratch\n\n- Whiplash (2014) — 10/10 — [take: existing]\n")
    with pytest.raises(set_take_module.SetTakeError) as exc:
        set_take_module.set_take(
            vault, path=rel, row_key="Whiplash", take="new", why="y", today=TODAY
        )
    assert exc.value.code == "ROW_NOT_FOUND"
    assert "overwrite" in exc.value.reason.lower()


def test_set_take_overwrite_replaces_filled(vault: Path) -> None:
    rel = _make_page(vault, "# Scratch\n\n- Whiplash (2014) — 10/10 — [take: old]\n")
    set_take_module.set_take(
        vault, path=rel, row_key="Whiplash", take="new", why="y", overwrite=True, today=TODAY
    )
    text = (vault / rel).read_text(encoding="utf-8")
    assert "[take: new]" in text
    assert "[take: old]" not in text
    assert text.count("[take:") == 1  # replaced, not duplicated


def test_set_take_em_dash_hyphen_fold(vault: Path) -> None:
    # Row separator is an em-dash; row_key is typed with an ASCII hyphen.
    rel = _make_page(vault, "# Scratch\n\n- Whiplash (2014) — 10/10 — [take: ]\n")
    set_take_module.set_take(
        vault, path=rel, row_key="Whiplash (2014) - 10/10", take="z", why="y", today=TODAY
    )
    assert "[take: z]" in (vault / rel).read_text(encoding="utf-8")


def test_set_take_ignores_code_fenced_placeholder(vault: Path) -> None:
    rel = _make_page(
        vault,
        "# Scratch\n\n"
        "```\n- Example (2020) — 5/5 — [take: ]\n```\n\n"
        "- Whiplash (2014) — 10/10 — [take: ]\n",
    )
    # The only "Example" row is inside a fence → not a real match.
    with pytest.raises(set_take_module.SetTakeError) as exc:
        set_take_module.set_take(
            vault, path=rel, row_key="Example", take="z", why="y", today=TODAY
        )
    assert exc.value.code == "ROW_NOT_FOUND"
    # The real row outside the fence still fills.
    set_take_module.set_take(
        vault, path=rel, row_key="Whiplash", take="z", why="y", today=TODAY
    )
    assert "[take: z]" in (vault / rel).read_text(encoding="utf-8")


def test_set_take_refuses_sources(vault: Path) -> None:
    with pytest.raises(set_take_module.SetTakeError) as exc:
        set_take_module.set_take(
            vault,
            path="Knowledge Base/Sources/Articles/2026-05-04-best-egcg-supplements.md",
            row_key="x",
            take="y",
            why="z",
            today=TODAY,
        )
    assert exc.value.code == "INVALID_EDIT"
    assert "append-only" in exc.value.reason.lower()


def test_set_take_logs_one_entry(vault: Path) -> None:
    rel = _make_page(vault, "# Scratch\n\n- Whiplash (2014) — 10/10 — [take: ]\n")
    log_path = vault / "Knowledge Base" / "log.md"
    before = log_path.read_text(encoding="utf-8").count("## [")
    set_take_module.set_take(
        vault, path=rel, row_key="Whiplash", take="z", why="fill take", today=TODAY
    )
    after = log_path.read_text(encoding="utf-8").count("## [")
    assert after == before + 1
