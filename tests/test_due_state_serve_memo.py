"""The served due-state block is built once and reused until an input changes.

Measured 2026-09-15 on the personal vault: after the serve itself was made
bounded (`test_due_state_serve_cost.py`) every recall still rebuilt the whole
block -- 0.6 s idle, 1.5 s on the live service, 4 s under host load -- when
nothing it depends on had changed. The build is a function of the projection
file, the review-state file, the audience, the purpose, the day, the clock,
the release plane's verdicts and the artifact-role findings, so one build is
kept per key and exactly those inputs invalidate it.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from exomem import due_state
from exomem.governance import egress
from exomem.governance.principal import RequestPrincipal

PAGE = "Knowledge Base/Notes/Insights/one.md"


@pytest.fixture(autouse=True)
def _fresh_cache():
    due_state.reset_serve_cache()
    yield
    due_state.reset_serve_cache()


def _principal(audience: str = "aud-1") -> RequestPrincipal:
    return RequestPrincipal(audience_id=audience, authorization_session_id="s-1", issuer_family="test")


def _persist(vault_root: Path, marker: str = "a") -> None:
    (vault_root / PAGE).parent.mkdir(parents=True, exist_ok=True)
    (vault_root / PAGE).write_text("# one\n", encoding="utf-8")
    due_state.save(
        vault_root,
        {
            "version": due_state.SCHEMA_VERSION,
            "categories": {"predictions": {PAGE: {"open": [{"path": PAGE}]}}},
            "marker": marker,
        },
    )


def _spy(
    monkeypatch: pytest.MonkeyPatch,
    horizon: dt.datetime | None = None,
    role_token: str | None = None,
) -> list[dict]:
    calls: list[dict] = []

    def fake(
        vault_root, *, today, now, principal=None, purpose=None,
        payload=None, review_payload=None, sources=None,
    ):
        calls.append({"today": today, "now": now, "purpose": purpose, "payload": payload})
        rows = [{"ref": f"exomem://memory/{len(calls)}", "kind": "open", "path": PAGE}]
        # As the real build does: record the verdict the plane gave at build time.
        keep = egress.release_walk_filter(vault_root, principal=principal, purpose=purpose)
        asked = {PAGE: True if keep is None else bool(keep(PAGE))}
        return rows, horizon, asked, role_token

    monkeypatch.setattr(due_state, "_served_entries_uncached", fake)
    return calls


def test_a_repeat_serve_reuses_the_build_and_returns_a_fresh_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _persist(tmp_path)
    calls = _spy(monkeypatch)
    now = dt.datetime(2026, 9, 15, 12, tzinfo=dt.UTC)
    first = due_state.served_entries(tmp_path, now=now, principal=_principal(), purpose="recall")
    second = due_state.served_entries(tmp_path, now=now, principal=_principal(), purpose="recall")
    assert len(calls) == 1
    assert first == second
    # The caller may annotate its rows without poisoning the next reader.
    second[0]["seen"] = True
    third = due_state.served_entries(tmp_path, now=now, principal=_principal(), purpose="recall")
    assert "seen" not in third[0]
    assert len(calls) == 1


def test_rewriting_the_projection_rebuilds_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _persist(tmp_path, "a")
    calls = _spy(monkeypatch)
    now = dt.datetime(2026, 9, 15, 12, tzinfo=dt.UTC)
    due_state.served_entries(tmp_path, now=now, principal=_principal())
    payload = due_state.load(tmp_path)
    payload["categories"]["predictions"][PAGE]["open"].append({"path": PAGE, "id": "second"})
    due_state.save(tmp_path, payload)
    due_state.served_entries(tmp_path, now=now, principal=_principal())
    due_state.served_entries(tmp_path, now=now, principal=_principal())
    assert len(calls) == 2


def test_a_fresh_load_stays_separate_from_the_shared_snapshot_until_saved(
    tmp_path: Path,
) -> None:
    _persist(tmp_path, "first")
    snapshot = due_state._load_shared(tmp_path)
    payload = due_state.load(tmp_path)
    payload["categories"]["predictions"][PAGE]["open"].append({"path": PAGE, "id": "second"})
    assert len(snapshot["categories"]["predictions"][PAGE]["open"]) == 1
    # `save` hands its payload to the shared cache, so a caller never edits one
    # after saving it; the write-through avoids a 20 MB re-parse per write.
    due_state.save(tmp_path, payload)

    assert len(due_state._load_shared(tmp_path)["categories"]["predictions"][PAGE]["open"]) == 2
    assert len(due_state.load(tmp_path)["categories"]["predictions"][PAGE]["open"]) == 2


def test_shared_projection_tracks_a_foreign_replacement_during_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import vault

    _persist(tmp_path, "first")
    due_state._load_shared(tmp_path)
    replacement = tmp_path / "foreign.json"
    replacement.write_text('{"version":2,"categories":{},"marker":"foreign"}\n')

    def replace(operation):
        operation()
        replacement.replace(due_state.state_path(tmp_path))

    monkeypatch.setattr(vault, "replace_tolerating_transient_sharing", replace)
    _persist(tmp_path, "ours")

    assert due_state._load_shared(tmp_path)["marker"] == "foreign"


def test_failed_projection_save_preserves_the_last_shared_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import vault

    _persist(tmp_path, "first")
    before = due_state.load(tmp_path)
    before["claims"] = {"collection": {"tags": ["persisted"]}}
    due_state.save(tmp_path, before)

    def refuse(_operation):
        raise OSError("replacement refused")

    monkeypatch.setattr(vault, "replace_tolerating_transient_sharing", refuse)
    _persist(tmp_path, "unsaved")
    updated = due_state.apply_write_delta(tmp_path, PAGE)
    updated["claims"]["collection"]["tags"].append("changed by the caller")

    assert due_state._load_shared(tmp_path) == before
    assert due_state.load(tmp_path)["marker"] == "first"
    assert not list(due_state.state_path(tmp_path).parent.glob("*.tmp"))


def test_a_write_that_only_ticks_the_emission_ledger_reuses_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every governed write rewrites the projection; only its due content may rebuild."""
    _persist(tmp_path, "a")
    calls = _spy(monkeypatch)
    now = dt.datetime(2026, 9, 15, 12, tzinfo=dt.UTC)
    due_state.served_entries(tmp_path, now=now, principal=_principal())
    payload = due_state.load(tmp_path)
    payload["emission"] = {"writes": 41, "emitted": 3}
    due_state.save(tmp_path, payload)
    due_state.served_entries(tmp_path, now=now, principal=_principal())
    assert len(calls) == 1


def test_a_record_write_that_changes_collection_claims_rebuilds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claims decide which candidate and observation rows survive, so they key the build."""
    _persist(tmp_path, "a")
    calls = _spy(monkeypatch)
    now = dt.datetime(2026, 9, 15, 12, tzinfo=dt.UTC)
    due_state.served_entries(tmp_path, now=now, principal=_principal())
    payload = due_state.load(tmp_path)
    payload["claims"] = {"example-collection": {"terms": ["covered-term"]}}
    due_state.save(tmp_path, payload)
    due_state.served_entries(tmp_path, now=now, principal=_principal())
    assert len(calls) == 2


def test_the_build_is_handed_the_projection_it_was_keyed_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _persist(tmp_path, "keyed")
    calls = _spy(monkeypatch)
    now = dt.datetime(2026, 9, 15, 12, tzinfo=dt.UTC)
    due_state.served_entries(tmp_path, now=now, principal=_principal())
    assert calls[0]["payload"]["marker"] == "keyed"


def test_the_build_expires_when_the_clock_reaches_the_next_future_due_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _persist(tmp_path)
    now = dt.datetime(2026, 9, 15, 12, 0, 0, tzinfo=dt.UTC)
    calls = _spy(monkeypatch, horizon=now + dt.timedelta(seconds=2))
    due_state.served_entries(tmp_path, now=now, principal=_principal())
    due_state.served_entries(tmp_path, now=now + dt.timedelta(seconds=1), principal=_principal())
    assert len(calls) == 1
    due_state.served_entries(tmp_path, now=now + dt.timedelta(seconds=2), principal=_principal())
    assert len(calls) == 2


def test_purpose_audience_and_day_each_key_their_own_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _persist(tmp_path)
    calls = _spy(monkeypatch)
    now = dt.datetime(2026, 9, 15, 12, tzinfo=dt.UTC)
    due_state.served_entries(tmp_path, now=now, principal=_principal(), purpose="recall")
    due_state.served_entries(tmp_path, now=now, principal=_principal(), purpose="review")
    due_state.served_entries(tmp_path, now=now, principal=_principal("aud-2"), purpose="recall")
    due_state.served_entries(
        tmp_path, now=now + dt.timedelta(days=1), principal=_principal(), purpose="recall"
    )
    assert [(c["purpose"], c["today"]) for c in calls] == [
        ("recall", dt.date(2026, 9, 15)),
        ("review", dt.date(2026, 9, 15)),
        ("recall", dt.date(2026, 9, 15)),
        ("recall", dt.date(2026, 9, 16)),
    ]


def test_a_changed_release_verdict_rebuilds_before_any_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _persist(tmp_path)
    calls = _spy(monkeypatch)
    now = dt.datetime(2026, 9, 15, 12, tzinfo=dt.UTC)
    due_state.served_entries(tmp_path, now=now, principal=_principal())
    due_state.served_entries(tmp_path, now=now, principal=_principal())
    assert len(calls) == 1
    # A policy arrives that withholds the served page: no file the memo keys
    # on changed, and the very next read must still rebuild under it.
    monkeypatch.setattr(egress, "release_walk_filter", lambda *_a, **_k: lambda path: path != PAGE)
    due_state.served_entries(tmp_path, now=now, principal=_principal())
    assert len(calls) == 2
    due_state.served_entries(tmp_path, now=now, principal=_principal())
    assert len(calls) == 2


def test_a_release_plane_that_cannot_decide_serves_nothing_from_a_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _persist(tmp_path)
    _spy(monkeypatch)
    now = dt.datetime(2026, 9, 15, 12, tzinfo=dt.UTC)
    assert due_state.served_entries(tmp_path, now=now, principal=_principal())

    def broken(*_a, **_k):
        raise RuntimeError("release plane down")

    monkeypatch.setattr(egress, "release_walk_filter", broken)
    assert due_state.served_entries(tmp_path, now=now, principal=_principal()) == []


def test_artifact_role_findings_are_served_again_on_a_hit_and_a_change_rebuilds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _persist(tmp_path)
    calls = _spy(monkeypatch, role_token="findings-v1")
    live = {"token": "findings-v1"}
    monkeypatch.setattr(due_state, "_role_token", lambda *_a, **_k: live["token"])
    now = dt.datetime(2026, 9, 15, 12, tzinfo=dt.UTC)
    due_state.served_entries(tmp_path, now=now, principal=_principal())
    due_state.served_entries(tmp_path, now=now, principal=_principal())
    assert len(calls) == 1
    # A source page changed out of band: the findings the vault would serve
    # now differ from those the build served, and no memo may outlive that.
    live["token"] = "findings-v2"
    due_state.served_entries(tmp_path, now=now, principal=_principal())
    assert len(calls) == 2


def test_a_page_deleted_out_of_band_is_dropped_from_a_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _persist(tmp_path)
    calls = _spy(monkeypatch)
    now = dt.datetime(2026, 9, 15, 12, tzinfo=dt.UTC)
    assert due_state.served_entries(tmp_path, now=now, principal=_principal())
    (tmp_path / PAGE).unlink()
    assert due_state.served_entries(tmp_path, now=now, principal=_principal()) == []
    assert len(calls) == 1


def test_removing_the_hit_side_existence_check_fails_this_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mechanism removal: with the check gone, a hit serves the deleted page."""
    _persist(tmp_path)
    _spy(monkeypatch)
    now = dt.datetime(2026, 9, 15, 12, tzinfo=dt.UTC)
    assert due_state.served_entries(tmp_path, now=now, principal=_principal())
    (tmp_path / PAGE).unlink()
    monkeypatch.setattr(due_state, "_page_exists", lambda *_a, **_k: True)
    stale = due_state.served_entries(tmp_path, now=now, principal=_principal())
    assert [row["path"] for row in stale] == [PAGE]


def test_a_projection_that_owes_nothing_asks_the_release_plane_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    due_state.save(tmp_path, {"version": due_state.SCHEMA_VERSION, "categories": {}})
    calls: list[int] = []
    monkeypatch.setattr(egress, "release_walk_filter", lambda *_a, **_k: calls.append(1) or None)
    now = dt.datetime(2026, 9, 15, 12, tzinfo=dt.UTC)
    assert due_state.served_entries(tmp_path, now=now, principal=_principal()) == []
    assert due_state.served_entries(tmp_path, now=now, principal=_principal()) == []
    assert calls == []


def test_a_vault_without_a_persisted_projection_is_never_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _spy(monkeypatch)
    now = dt.datetime(2026, 9, 15, 12, tzinfo=dt.UTC)
    due_state.served_entries(tmp_path, now=now, principal=_principal())
    due_state.served_entries(tmp_path, now=now, principal=_principal())
    assert len(calls) == 2


def test_reset_drops_every_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _persist(tmp_path)
    calls = _spy(monkeypatch)
    now = dt.datetime(2026, 9, 15, 12, tzinfo=dt.UTC)
    due_state.served_entries(tmp_path, now=now, principal=_principal())
    due_state.reset_serve_cache()
    due_state.served_entries(tmp_path, now=now, principal=_principal())
    assert len(calls) == 2


def test_an_expired_build_is_served_while_one_background_build_replaces_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured 2026-10-19: a canary asking every 5 min 20 s rebuilt the block on
    every ask, 1.5-3.5 s, because each found the 300 s build expired."""
    import threading

    from exomem.governance import principal as principal_module

    monkeypatch.delenv("EXOMEM_SYNC_DUE_STATE_WARM", raising=False)
    _persist(tmp_path)
    calls = _spy(monkeypatch)
    spied = due_state._served_entries_uncached
    release = threading.Event()
    bound: list[object] = []

    def gated(*args, **kwargs):
        if calls:  # every build after the first waits for the test
            bound.append(principal_module.current_principal())
            assert release.wait(5)
        return spied(*args, **kwargs)

    monkeypatch.setattr(due_state, "_served_entries_uncached", gated)
    now = dt.datetime(2026, 9, 15, 12, tzinfo=dt.UTC)
    who = _principal()
    with principal_module.request_scope(who):
        due_state.served_entries(tmp_path, now=now, principal=who)
        monkeypatch.setattr(due_state, "_SERVE_CACHE_TTL_SECONDS", 0.0)
        expired = due_state.served_entries(tmp_path, now=now, principal=who)
        again = due_state.served_entries(tmp_path, now=now, principal=who)
    assert [row["ref"] for row in expired + again] == ["exomem://memory/1"] * 2
    assert len(calls) == 1  # neither read built inline
    release.set()
    for thread in threading.enumerate():
        if thread.name == "exomem-due-state-serve-refresh":
            thread.join(5)
    assert len(calls) == 2  # one background build for both expired reads
    assert bound == [who]  # under the caller's binding, not an owner scope
    monkeypatch.setattr(due_state, "_SERVE_CACHE_TTL_SECONDS", 300.0)
    fresh = due_state.served_entries(tmp_path, now=now, principal=who)
    assert [row["ref"] for row in fresh] == ["exomem://memory/2"]
    assert len(calls) == 2


def test_an_expired_build_whose_verdict_was_revoked_is_never_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _persist(tmp_path)
    calls = _spy(monkeypatch)
    now = dt.datetime(2026, 9, 15, 12, tzinfo=dt.UTC)
    due_state.served_entries(tmp_path, now=now, principal=_principal())
    monkeypatch.setattr(due_state, "_SERVE_CACHE_TTL_SECONDS", 0.0)
    monkeypatch.setattr(egress, "release_walk_filter", lambda *_a, **_k: lambda path: path != PAGE)
    served = due_state.served_entries(tmp_path, now=now, principal=_principal())
    # Rebuilt inline under the new verdict; the expired rows never left the memo.
    assert [row["ref"] for row in served] == ["exomem://memory/2"]
    assert len(calls) == 2


def test_a_build_past_its_stale_limit_is_rebuilt_inline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The limit bounds how far the inputs no key covers can lag behind a serve."""
    _persist(tmp_path)
    calls = _spy(monkeypatch)
    now = dt.datetime(2026, 9, 15, 12, tzinfo=dt.UTC)
    due_state.served_entries(tmp_path, now=now, principal=_principal())
    monkeypatch.setattr(due_state, "_SERVE_CACHE_TTL_SECONDS", 0.0)
    monkeypatch.setattr(due_state, "_SERVE_CACHE_STALE_SECONDS", 0.0)
    served = due_state.served_entries(tmp_path, now=now, principal=_principal())
    assert [row["ref"] for row in served] == ["exomem://memory/2"]
    assert len(calls) == 2
