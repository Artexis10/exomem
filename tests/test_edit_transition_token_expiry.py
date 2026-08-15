"""Issue #506: an expired/skewed reviewed transition token fails loud with its
own code, instead of the `edit.py` `or`-fallback silently minting a fresh
stamp and later tripping the unrelated ``LIFECYCLE_TRANSITION_MISMATCH``
guard on ``after_hash``.

On current `main` (pre-fix), the age/skew scenarios below fail with the
misleading ``LIFECYCLE_TRANSITION_MISMATCH`` code. After the fix they fail
with the distinct ``LIFECYCLE_TRANSITION_TOKEN_EXPIRED`` code and a message
that names the actual cause. The fresh-token and no-token paths are
regression-pinned to keep committing exactly as before.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from exomem import edit as edit_module

_PAGE = "Knowledge Base/Notes/Insights/lifecycle-token-expiry.md"
_ID = "00000000-0000-4000-8000-0000000000f1"


def _source(body: str) -> str:
    return (
        "---\n"
        "title: Lifecycle Token Expiry\n"
        "type: insight\n"
        "status: active\n"
        f"exomem_id: {_ID}\n"
        "---\n\n"
        f"{body}\n\n"
        "## Relations\n"
    )


def _write(root: Path, rel: str, source: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8", newline="\n")
    return path


def _preview_token(tmp_path: Path, *, today: dt.datetime) -> str:
    _write(tmp_path, _PAGE, _source("A"))
    result = edit_module.edit(
        tmp_path,
        path=_PAGE,
        why="preview for token-expiry coverage",
        old_string="A",
        new_string="B",
        validate_only=True,
        today=today,
    )
    assert result.semantic is not None
    token = result.semantic["transition_token"]
    assert isinstance(token, str)
    return token


def test_expired_reviewed_token_fails_with_its_own_code(tmp_path: Path) -> None:
    reviewed_at = dt.datetime(2026, 7, 14, 12, 0, 0, tzinfo=dt.UTC)
    token = _preview_token(tmp_path, today=reviewed_at)

    committed_at = reviewed_at + dt.timedelta(hours=24, minutes=1)
    with pytest.raises(edit_module.EditError) as exc:
        edit_module.edit(
            tmp_path,
            path=_PAGE,
            why="commit after the reviewed stamp expired",
            old_string="A",
            new_string="B",
            semantic_transition_token=token,
            today=committed_at,
        )

    assert exc.value.code == "LIFECYCLE_TRANSITION_TOKEN_EXPIRED"
    assert exc.value.code != "LIFECYCLE_TRANSITION_MISMATCH"
    assert "24" in exc.value.reason
    assert "validate_only" in exc.value.reason


def test_skewed_reviewed_token_fails_with_its_own_code(tmp_path: Path) -> None:
    reviewed_at = dt.datetime(2026, 7, 14, 12, 10, 0, tzinfo=dt.UTC)
    token = _preview_token(tmp_path, today=reviewed_at)

    committed_at = reviewed_at - dt.timedelta(minutes=6)
    with pytest.raises(edit_module.EditError) as exc:
        edit_module.edit(
            tmp_path,
            path=_PAGE,
            why="commit before the reviewed stamp's skew window",
            old_string="A",
            new_string="B",
            semantic_transition_token=token,
            today=committed_at,
        )

    assert exc.value.code == "LIFECYCLE_TRANSITION_TOKEN_EXPIRED"
    assert exc.value.code != "LIFECYCLE_TRANSITION_MISMATCH"
    assert "skew" in exc.value.reason.lower()
    assert "5" in exc.value.reason


def test_fresh_reviewed_token_still_commits(tmp_path: Path) -> None:
    reviewed_at = dt.datetime(2026, 7, 14, 12, 0, 0, tzinfo=dt.UTC)
    token = _preview_token(tmp_path, today=reviewed_at)
    page = tmp_path / _PAGE

    committed_at = reviewed_at + dt.timedelta(minutes=5)
    result = edit_module.edit(
        tmp_path,
        path=_PAGE,
        why="commit within the reviewed window",
        old_string="A",
        new_string="B",
        semantic_transition_token=token,
        today=committed_at,
    )

    assert result.semantic is not None
    assert result.semantic["mutated"] is True
    # The committed `updated:` reuses the reviewed instant, not the (later)
    # commit-time clock — that's the whole point of the reviewed-stamp seam.
    assert "updated: 2026-07-14T12:00:00Z" in page.read_text(encoding="utf-8")


def test_no_token_legacy_path_still_commits_with_a_fresh_stamp(
    tmp_path: Path,
) -> None:
    page = _write(tmp_path, _PAGE, _source("A"))
    now = dt.datetime(2026, 7, 14, 12, 0, 0, tzinfo=dt.UTC)

    result = edit_module.edit(
        tmp_path,
        path=_PAGE,
        why="legacy commit without a reviewed token",
        old_string="A",
        new_string="B",
        today=now,
    )

    assert result.semantic is not None
    assert result.semantic["mutated"] is True
    assert "updated: 2026-07-14T12:00:00Z" in page.read_text(encoding="utf-8")
