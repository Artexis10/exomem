"""Issue #506: an expired/skewed reviewed transition token fails loud with its
own code, instead of the `... or temporal.stamp(now)` fallback silently
minting a fresh stamp and later tripping the unrelated
``LIFECYCLE_TRANSITION_MISMATCH`` guard on ``after_hash``.

On current `main` (pre-fix), the age/skew scenarios below fail with the
misleading ``LIFECYCLE_TRANSITION_MISMATCH`` code. After the fix they fail
with the distinct ``LIFECYCLE_TRANSITION_TOKEN_EXPIRED`` code and a message
that names the actual cause. The fresh-token and no-token paths are
regression-pinned to keep committing exactly as before.

Covers all four writers that shared the identical bug shape:
`edit`, `multi_edit`, `set_frontmatter_field`, and `create_file`'s
overwrite-by-token path.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from exomem import create_file as create_file_module
from exomem import edit as edit_module
from exomem import multi_edit as multi_edit_module
from exomem import semantic_writes
from exomem import set_frontmatter_field as set_frontmatter_module
from exomem import temporal

_PAGE = "Knowledge Base/Notes/Insights/lifecycle-token-expiry.md"
_ID = "00000000-0000-4000-8000-0000000000f1"

# Derived from the same bound constants the production message is built
# from (`semantic_writes._format_hours`/`_format_minutes` applied to
# `_MAX_REVIEWED_STAMP_AGE`/`_MAX_REVIEWED_STAMP_SKEW`) rather than hard-coded
# "24"/"5" literals, so retuning either bound can't silently desync the
# message assertions below from the constant that actually governs behavior.
_EXPECTED_AGE_BOUND = semantic_writes._format_hours(semantic_writes._MAX_REVIEWED_STAMP_AGE)
_EXPECTED_SKEW_BOUND = semantic_writes._format_minutes(semantic_writes._MAX_REVIEWED_STAMP_SKEW)


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
    assert _EXPECTED_AGE_BOUND in exc.value.reason
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
    assert _EXPECTED_SKEW_BOUND in exc.value.reason


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


# ---------------------------------------------------------------------------
# multi_edit — same bug shape, reuses edit._resolve_date_iso directly.
# ---------------------------------------------------------------------------

_ME_PAGE = "Knowledge Base/Notes/Insights/lifecycle-token-expiry-multi.md"
_ME_ID = "00000000-0000-4000-8000-0000000000f2"


def _me_source(body: str) -> str:
    return (
        "---\n"
        "title: Lifecycle Token Expiry (multi_edit)\n"
        "type: insight\n"
        "status: active\n"
        f"exomem_id: {_ME_ID}\n"
        "---\n\n"
        f"{body}\n\n"
        "## Relations\n"
    )


def _me_preview_token(tmp_path: Path, *, today: dt.datetime) -> str:
    _write(tmp_path, _ME_PAGE, _me_source("A"))
    result = multi_edit_module.multi_edit(
        tmp_path,
        path=_ME_PAGE,
        why="preview for multi_edit token-expiry coverage",
        edits=[{"old_string": "A", "new_string": "B"}],
        validate_only=True,
        today=today,
    )
    assert result.semantic is not None
    token = result.semantic["transition_token"]
    assert isinstance(token, str)
    return token


def test_multi_edit_expired_reviewed_token_fails_with_its_own_code(
    tmp_path: Path,
) -> None:
    reviewed_at = dt.datetime(2026, 7, 14, 12, 0, 0, tzinfo=dt.UTC)
    token = _me_preview_token(tmp_path, today=reviewed_at)

    committed_at = reviewed_at + dt.timedelta(hours=24, minutes=1)
    with pytest.raises(edit_module.EditError) as exc:
        multi_edit_module.multi_edit(
            tmp_path,
            path=_ME_PAGE,
            why="commit after the reviewed stamp expired",
            edits=[{"old_string": "A", "new_string": "B"}],
            semantic_transition_token=token,
            today=committed_at,
        )

    assert exc.value.code == "LIFECYCLE_TRANSITION_TOKEN_EXPIRED"
    assert exc.value.code != "LIFECYCLE_TRANSITION_MISMATCH"
    assert _EXPECTED_AGE_BOUND in exc.value.reason
    assert "validate_only" in exc.value.reason


def test_multi_edit_skewed_reviewed_token_fails_with_its_own_code(
    tmp_path: Path,
) -> None:
    reviewed_at = dt.datetime(2026, 7, 14, 12, 10, 0, tzinfo=dt.UTC)
    token = _me_preview_token(tmp_path, today=reviewed_at)

    committed_at = reviewed_at - dt.timedelta(minutes=6)
    with pytest.raises(edit_module.EditError) as exc:
        multi_edit_module.multi_edit(
            tmp_path,
            path=_ME_PAGE,
            why="commit before the reviewed stamp's skew window",
            edits=[{"old_string": "A", "new_string": "B"}],
            semantic_transition_token=token,
            today=committed_at,
        )

    assert exc.value.code == "LIFECYCLE_TRANSITION_TOKEN_EXPIRED"
    assert exc.value.code != "LIFECYCLE_TRANSITION_MISMATCH"
    assert "skew" in exc.value.reason.lower()
    assert _EXPECTED_SKEW_BOUND in exc.value.reason


def test_multi_edit_fresh_reviewed_token_still_commits(tmp_path: Path) -> None:
    reviewed_at = dt.datetime(2026, 7, 14, 12, 0, 0, tzinfo=dt.UTC)
    token = _me_preview_token(tmp_path, today=reviewed_at)
    page = tmp_path / _ME_PAGE

    committed_at = reviewed_at + dt.timedelta(minutes=5)
    result = multi_edit_module.multi_edit(
        tmp_path,
        path=_ME_PAGE,
        why="commit within the reviewed window",
        edits=[{"old_string": "A", "new_string": "B"}],
        semantic_transition_token=token,
        today=committed_at,
    )

    assert result.semantic is not None
    assert result.semantic["mutated"] is True
    assert "updated: 2026-07-14T12:00:00Z" in page.read_text(encoding="utf-8")


def test_multi_edit_no_token_legacy_path_still_commits_with_a_fresh_stamp(
    tmp_path: Path,
) -> None:
    page = _write(tmp_path, _ME_PAGE, _me_source("A"))
    now = dt.datetime(2026, 7, 14, 12, 0, 0, tzinfo=dt.UTC)

    result = multi_edit_module.multi_edit(
        tmp_path,
        path=_ME_PAGE,
        why="legacy commit without a reviewed token",
        edits=[{"old_string": "A", "new_string": "B"}],
        today=now,
    )

    assert result.semantic is not None
    assert result.semantic["mutated"] is True
    assert "updated: 2026-07-14T12:00:00Z" in page.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# set_frontmatter_field — same bug shape, raises SetFrontmatterError.
# ---------------------------------------------------------------------------

_SF_PAGE = "Knowledge Base/Notes/Insights/lifecycle-token-expiry-setfm.md"
_SF_ID = "00000000-0000-4000-8000-0000000000f3"


def _sf_source(body: str) -> str:
    return (
        "---\n"
        "title: Lifecycle Token Expiry (set_frontmatter_field)\n"
        "type: insight\n"
        "status: active\n"
        f"exomem_id: {_SF_ID}\n"
        "---\n\n"
        f"{body}\n\n"
        "## Relations\n"
    )


def _sf_preview_token(tmp_path: Path, *, today: dt.datetime) -> str:
    _write(tmp_path, _SF_PAGE, _sf_source("Prose."))
    result = set_frontmatter_module.set_frontmatter_field(
        tmp_path,
        path=_SF_PAGE,
        field="title",
        value="Renamed title",
        why="preview for set_frontmatter_field token-expiry coverage",
        validate_only=True,
        today=today,
    )
    assert result.semantic is not None
    token = result.semantic["transition_token"]
    assert isinstance(token, str)
    return token


def test_set_frontmatter_field_expired_reviewed_token_fails_with_its_own_code(
    tmp_path: Path,
) -> None:
    reviewed_at = dt.datetime(2026, 7, 14, 12, 0, 0, tzinfo=dt.UTC)
    token = _sf_preview_token(tmp_path, today=reviewed_at)

    committed_at = reviewed_at + dt.timedelta(hours=24, minutes=1)
    with pytest.raises(set_frontmatter_module.SetFrontmatterError) as exc:
        set_frontmatter_module.set_frontmatter_field(
            tmp_path,
            path=_SF_PAGE,
            field="title",
            value="Renamed title",
            why="commit after the reviewed stamp expired",
            semantic_transition_token=token,
            today=committed_at,
        )

    assert exc.value.code == "LIFECYCLE_TRANSITION_TOKEN_EXPIRED"
    assert exc.value.code != "LIFECYCLE_TRANSITION_MISMATCH"
    assert _EXPECTED_AGE_BOUND in exc.value.reason
    assert "validate_only" in exc.value.reason


def test_set_frontmatter_field_skewed_reviewed_token_fails_with_its_own_code(
    tmp_path: Path,
) -> None:
    reviewed_at = dt.datetime(2026, 7, 14, 12, 10, 0, tzinfo=dt.UTC)
    token = _sf_preview_token(tmp_path, today=reviewed_at)

    committed_at = reviewed_at - dt.timedelta(minutes=6)
    with pytest.raises(set_frontmatter_module.SetFrontmatterError) as exc:
        set_frontmatter_module.set_frontmatter_field(
            tmp_path,
            path=_SF_PAGE,
            field="title",
            value="Renamed title",
            why="commit before the reviewed stamp's skew window",
            semantic_transition_token=token,
            today=committed_at,
        )

    assert exc.value.code == "LIFECYCLE_TRANSITION_TOKEN_EXPIRED"
    assert exc.value.code != "LIFECYCLE_TRANSITION_MISMATCH"
    assert "skew" in exc.value.reason.lower()
    assert _EXPECTED_SKEW_BOUND in exc.value.reason


def test_set_frontmatter_field_fresh_reviewed_token_still_commits(
    tmp_path: Path,
) -> None:
    reviewed_at = dt.datetime(2026, 7, 14, 12, 0, 0, tzinfo=dt.UTC)
    token = _sf_preview_token(tmp_path, today=reviewed_at)
    page = tmp_path / _SF_PAGE

    committed_at = reviewed_at + dt.timedelta(minutes=5)
    result = set_frontmatter_module.set_frontmatter_field(
        tmp_path,
        path=_SF_PAGE,
        field="title",
        value="Renamed title",
        why="commit within the reviewed window",
        semantic_transition_token=token,
        today=committed_at,
    )

    assert result.semantic is not None
    assert result.semantic["mutated"] is True
    assert "updated: 2026-07-14T12:00:00Z" in page.read_text(encoding="utf-8")


def test_set_frontmatter_field_no_token_legacy_path_still_commits_with_a_fresh_stamp(
    tmp_path: Path,
) -> None:
    page = _write(tmp_path, _SF_PAGE, _sf_source("Prose."))
    now = dt.datetime(2026, 7, 14, 12, 0, 0, tzinfo=dt.UTC)

    result = set_frontmatter_module.set_frontmatter_field(
        tmp_path,
        path=_SF_PAGE,
        field="title",
        value="Renamed title",
        why="legacy commit without a reviewed token",
        today=now,
    )

    assert result.semantic is not None
    assert result.semantic["mutated"] is True
    assert "updated: 2026-07-14T12:00:00Z" in page.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# create_file — overwrite-by-token path, raises CreateFileError.
# ---------------------------------------------------------------------------

_CF_PAGE = "Knowledge Base/Notes/Insights/lifecycle-token-expiry-createfile.md"
_CF_ID = "00000000-0000-4000-8000-0000000000f4"


def _cf_initial_source(body: str) -> str:
    return (
        "---\n"
        "title: Lifecycle Token Expiry (create_file)\n"
        "type: insight\n"
        "status: active\n"
        f"exomem_id: {_CF_ID}\n"
        "---\n\n"
        f"{body}\n\n"
        "## Relations\n"
    )


_CF_FRONTMATTER = {
    "title": "Lifecycle Token Expiry (create_file)",
    "type": "insight",
    "status": "active",
    "exomem_id": _CF_ID,
}


def _cf_body(body: str) -> str:
    return f"{body}\n\n## Relations\n"


def _cf_preview_token(tmp_path: Path, *, today: dt.datetime) -> str:
    # Overwrite via a `frontmatter=` dict (not raw `content=`) so `created`/
    # `updated` are stamp-filled into the actual page bytes, exactly like
    # edit/multi_edit/set_frontmatter_field — that's what makes a silently
    # re-minted stamp change `after_hash` and reach the mismatch guard.
    _write(tmp_path, _CF_PAGE, _cf_initial_source("A"))
    preview = create_file_module.create_file(
        tmp_path,
        path=_CF_PAGE,
        content=_cf_body("B"),
        frontmatter=dict(_CF_FRONTMATTER),
        overwrite=True,
        validate_only=True,
        today=today,
    )
    token = preview.transition_token
    assert isinstance(token, str)
    return token


def test_create_file_overwrite_expired_reviewed_token_fails_with_its_own_code(
    tmp_path: Path,
) -> None:
    reviewed_at = dt.datetime(2026, 7, 14, 12, 0, 0, tzinfo=dt.UTC)
    token = _cf_preview_token(tmp_path, today=reviewed_at)

    committed_at = reviewed_at + dt.timedelta(hours=24, minutes=1)
    with pytest.raises(create_file_module.CreateFileError) as exc:
        create_file_module.create_file(
            tmp_path,
            path=_CF_PAGE,
            content=_cf_body("B"),
            frontmatter=dict(_CF_FRONTMATTER),
            overwrite=True,
            draft_token=token,
            today=committed_at,
        )

    assert exc.value.code == "LIFECYCLE_TRANSITION_TOKEN_EXPIRED"
    assert exc.value.code != "LIFECYCLE_TRANSITION_MISMATCH"
    assert _EXPECTED_AGE_BOUND in exc.value.reason
    assert "validate_only" in exc.value.reason


def test_create_file_overwrite_skewed_reviewed_token_fails_with_its_own_code(
    tmp_path: Path,
) -> None:
    reviewed_at = dt.datetime(2026, 7, 14, 12, 10, 0, tzinfo=dt.UTC)
    token = _cf_preview_token(tmp_path, today=reviewed_at)

    committed_at = reviewed_at - dt.timedelta(minutes=6)
    with pytest.raises(create_file_module.CreateFileError) as exc:
        create_file_module.create_file(
            tmp_path,
            path=_CF_PAGE,
            content=_cf_body("B"),
            frontmatter=dict(_CF_FRONTMATTER),
            overwrite=True,
            draft_token=token,
            today=committed_at,
        )

    assert exc.value.code == "LIFECYCLE_TRANSITION_TOKEN_EXPIRED"
    assert exc.value.code != "LIFECYCLE_TRANSITION_MISMATCH"
    assert "skew" in exc.value.reason.lower()
    assert _EXPECTED_SKEW_BOUND in exc.value.reason


def test_create_file_overwrite_fresh_reviewed_token_still_commits(
    tmp_path: Path,
) -> None:
    reviewed_at = dt.datetime(2026, 7, 14, 12, 0, 0, tzinfo=dt.UTC)
    token = _cf_preview_token(tmp_path, today=reviewed_at)
    page = tmp_path / _CF_PAGE

    committed_at = reviewed_at + dt.timedelta(minutes=5)
    result = create_file_module.create_file(
        tmp_path,
        path=_CF_PAGE,
        content=_cf_body("B"),
        frontmatter=dict(_CF_FRONTMATTER),
        overwrite=True,
        draft_token=token,
        today=committed_at,
    )

    assert result.semantic is not None
    assert result.semantic["mutated"] is True
    text = page.read_text(encoding="utf-8")
    # The committed `updated:`/`created:` reuse the reviewed instant, not the
    # (later) commit-time clock — same reviewed-stamp seam as the siblings.
    # `serialize_frontmatter` quotes ISO-instant scalars.
    assert 'updated: "2026-07-14T12:00:00Z"' in text
    assert 'created: "2026-07-14T12:00:00Z"' in text
    assert "B\n\n## Relations" in text


def test_create_file_overwrite_no_token_legacy_path_still_commits_with_a_fresh_stamp(
    tmp_path: Path,
) -> None:
    page = _write(tmp_path, _CF_PAGE, _cf_initial_source("A"))
    now = dt.datetime(2026, 7, 14, 12, 0, 0, tzinfo=dt.UTC)

    result = create_file_module.create_file(
        tmp_path,
        path=_CF_PAGE,
        content=_cf_body("B"),
        frontmatter=dict(_CF_FRONTMATTER),
        overwrite=True,
        today=now,
    )

    assert result.semantic is not None
    assert result.semantic["mutated"] is True
    text = page.read_text(encoding="utf-8")
    assert 'updated: "2026-07-14T12:00:00Z"' in text
    assert "B\n\n## Relations" in text


def test_create_file_overwrite_raw_content_expired_token_raises_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """Reviewer finding 1: raw `content=` overwrite (no `frontmatter=` dict)
    never embeds `stamp_iso` into the page bytes, so on unfixed `main` an
    expired token was silently accepted there — the commit still succeeded,
    just with a wrong log-entry date, since the `after_hash` never diverged.
    That's a real, previously-succeeding path with zero prior coverage.
    After the fix it now correctly raises `LIFECYCLE_TRANSITION_TOKEN_EXPIRED`
    too, because the refusal happens at stamp resolution — before create_file
    ever looks at whether a `frontmatter=` dict was supplied — and nothing is
    written.
    """
    raw_overwrite = _cf_initial_source("B")
    page = _write(tmp_path, _CF_PAGE, _cf_initial_source("A"))

    reviewed_at = dt.datetime(2026, 7, 14, 12, 0, 0, tzinfo=dt.UTC)
    preview = create_file_module.create_file(
        tmp_path,
        path=_CF_PAGE,
        content=raw_overwrite,
        overwrite=True,
        validate_only=True,
        today=reviewed_at,
    )
    token = preview.transition_token
    assert isinstance(token, str)

    before_bytes = page.read_bytes()
    committed_at = reviewed_at + dt.timedelta(hours=24, minutes=1)
    with pytest.raises(create_file_module.CreateFileError) as exc:
        create_file_module.create_file(
            tmp_path,
            path=_CF_PAGE,
            content=raw_overwrite,
            overwrite=True,
            draft_token=token,
            today=committed_at,
        )

    assert exc.value.code == "LIFECYCLE_TRANSITION_TOKEN_EXPIRED"
    assert exc.value.code != "LIFECYCLE_TRANSITION_MISMATCH"
    assert _EXPECTED_AGE_BOUND in exc.value.reason
    assert page.read_bytes() == before_bytes


# ---------------------------------------------------------------------------
# semantic_writes.reviewed_transition_stamp — direct shape pin.
#
# Reviewer finding 4: no writer in this file calls it directly anymore (they
# all go through resolve_reviewed_date_iso), but it's still public API in
# semantic_writes.py whose backward-compatible shape the spec locked this
# task to preserving for other callers. Pin its own behavior directly rather
# than only exercising it indirectly through a writer round-trip.
# ---------------------------------------------------------------------------


def test_reviewed_transition_stamp_direct_shape() -> None:
    reviewed_at = dt.datetime(2026, 7, 14, 12, 0, 0, tzinfo=dt.UTC)
    reviewed_stamp = temporal.stamp(reviewed_at)
    token = semantic_writes._existing_transition_token(
        operation="edit",
        path="Knowledge Base/Notes/Insights/shape-pin.md",
        before_hash="a" * 64,
        after_hash="b" * 64,
        stamp=reviewed_stamp,
    )

    # No token: None.
    assert semantic_writes.reviewed_transition_stamp(None, reviewed_at) is None

    # Malformed token: None (decode failure, not a raise).
    assert (
        semantic_writes.reviewed_transition_stamp("not-a-real-token", reviewed_at)
        is None
    )

    # In-window: returns the exact reviewed stamp string.
    assert (
        semantic_writes.reviewed_transition_stamp(
            token, reviewed_at + dt.timedelta(minutes=5)
        )
        == reviewed_stamp
    )

    # Aged past the bound: None.
    assert (
        semantic_writes.reviewed_transition_stamp(
            token,
            reviewed_at + semantic_writes._MAX_REVIEWED_STAMP_AGE + dt.timedelta(minutes=1),
        )
        is None
    )

    # A bare `dt.date` reference is accepted (treated as end-of-day UTC), not
    # just `dt.datetime`.
    assert (
        semantic_writes.reviewed_transition_stamp(token, reviewed_at.date())
        == reviewed_stamp
    )
