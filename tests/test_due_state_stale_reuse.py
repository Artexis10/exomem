"""A served due-state block survives a key change: the newest build answers, a refresh follows.

The memo key holds the authorization session, the projection token and the review
token, so every new conversation, due-state write and dismissal missed it and the
request paid the whole build (a release decision per due item). A miss is now
answered from the newest build of the same vault, audience, resolution and purpose,
cut to what the current projection and review state still hold, and one background
refresh builds the new key. These tests run the real build against a real projection.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from pathlib import Path

import pytest

from exomem import due_state, review_state
from exomem import find as find_module
from exomem.governance.principal import RequestPrincipal

TODAY = dt.date(2026, 8, 16)
NOW = dt.datetime(2026, 8, 16, 12, tzinfo=dt.UTC)
INSIGHTS = "Knowledge Base/Notes/Insights"
REFRESH = "exomem-due-state-serve-refresh"


def _settle() -> None:
    deadline = time.monotonic() + 10
    while due_state._SERVE_REFRESHING and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not due_state._SERVE_REFRESHING, "a due-state refresh did not finish"


@pytest.fixture(autouse=True)
def _background_refresh(monkeypatch: pytest.MonkeyPatch):
    # The suite runs a refresh inside the call that scheduled it. The service runs it
    # on its own thread after the request, which is the behaviour under test.
    monkeypatch.delenv("EXOMEM_SYNC_DUE_STATE_WARM", raising=False)
    due_state.reset_serve_cache()
    yield
    _settle()
    due_state.reset_serve_cache()


@pytest.fixture
def builds(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The name of the thread behind every build of the served rows."""
    real = due_state._served_entries_uncached
    seen: list[str] = []

    def counted(*args, **kwargs):
        seen.append(threading.current_thread().name)
        return real(*args, **kwargs)

    monkeypatch.setattr(due_state, "_served_entries_uncached", counted)
    return seen


def _principal(audience: str = "aud-1", session: str = "s-1") -> RequestPrincipal:
    return RequestPrincipal(
        audience_id=audience, authorization_session_id=session, issuer_family="test"
    )


def _prediction(vault: Path, slug: str, *, check_by: str, verdict: str | None = None) -> str:
    rows = ["- id: p1", f"- check_by: {check_by}"]
    if verdict is not None:
        rows.append(f"- verdict: {verdict}")
    page = vault / INSIGHTS / f"{slug}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        f"---\ntitle: {slug}\ntype: insight\nstatus: active\ncreated: 2026-01-01\n"
        "updated: 2026-01-01\n---\n\n## Prediction\n\n"
        + "\n".join(rows)
        + "\n\nThe backlog clears within a week.\n",
        encoding="utf-8",
    )
    find_module.clear_cache()
    return f"{INSIGHTS}/{slug}.md"


def _serve(vault: Path, who: RequestPrincipal) -> list[dict]:
    return due_state.served_entries(vault, today=TODAY, now=NOW, principal=who)


def _inline(builds: list[str]) -> list[str]:
    return [name for name in builds if name != REFRESH]


def test_a_new_session_is_served_the_newest_build_and_one_refresh_follows(
    vault: Path, builds: list[str]
) -> None:
    _prediction(vault, "one", check_by="2026-08-01")
    due_state.reconcile(vault, today=TODAY)
    first = _serve(vault, _principal(session="s-1"))
    assert first and len(_inline(builds)) == 1

    builds.clear()
    second = _serve(vault, _principal(session="s-2"))
    assert second == first
    assert _inline(builds) == [], "the request thread rebuilt the block"
    _settle()
    assert builds == [REFRESH]

    # The refresh filed the new session's own build, so its next read is an exact hit.
    assert _serve(vault, _principal(session="s-2")) == first
    assert builds == [REFRESH]


def test_a_dismissal_is_never_served_from_the_older_build(vault: Path, builds: list[str]) -> None:
    _prediction(vault, "kept", check_by="2026-08-01")
    _prediction(vault, "dismissed", check_by="2026-07-01")
    due_state.reconcile(vault, today=TODAY)
    who = _principal()
    first = _serve(vault, who)
    target = next(row for row in first if row["due_since"] == "2026-07-01")
    review_state.ReviewStateStore(vault).apply(
        review_state.parse_review_ref(target["ref"]),
        target["fingerprint"],
        action="dismiss",
        why="done",
        now=NOW,
    )

    builds.clear()
    second = _serve(vault, who)

    assert [row["ref"] for row in second] == [row["ref"] for row in first if row is not target]
    assert _inline(builds) == []


def test_an_item_the_projection_no_longer_holds_is_not_served(
    vault: Path, builds: list[str]
) -> None:
    kept = _prediction(vault, "kept", check_by="2026-08-01")
    _prediction(vault, "resolved", check_by="2026-07-01")
    due_state.reconcile(vault, today=TODAY)
    who = _principal()
    assert len(_serve(vault, who)) == 2

    _prediction(vault, "resolved", check_by="2026-07-01", verdict="confirmed")
    due_state.reconcile(vault, today=TODAY)
    builds.clear()
    after = _serve(vault, who)

    assert [row["path"] for row in after] == [kept]
    assert _inline(builds) == []


def test_a_build_is_never_reused_across_audiences(vault: Path, builds: list[str]) -> None:
    _prediction(vault, "one", check_by="2026-08-01")
    due_state.reconcile(vault, today=TODAY)
    _serve(vault, _principal(audience="aud-1"))

    builds.clear()
    other = _serve(vault, _principal(audience="aud-2"))

    assert other
    assert len(_inline(builds)) == 1, "another audience was answered from a build it did not make"


def test_after_the_refresh_the_served_rows_equal_a_fresh_build(
    vault: Path, builds: list[str]
) -> None:
    _prediction(vault, "one", check_by="2026-08-01")
    due_state.reconcile(vault, today=TODAY)
    first = _serve(vault, _principal(session="s-1"))
    _prediction(vault, "two", check_by="2026-07-15")
    due_state.reconcile(vault, today=TODAY)

    during = _serve(vault, _principal(session="s-2"))
    assert during == first, "an item the older build never saw lands with the refresh"
    _settle()
    after = _serve(vault, _principal(session="s-2"))

    due_state.reset_serve_cache()
    fresh = _serve(vault, _principal(session="s-2"))
    assert after == fresh
    assert len(after) == 2
