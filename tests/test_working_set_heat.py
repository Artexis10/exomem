"""The hot profile as a decaying event projection (close-memory-loop 7.3/7.4).

The projection keeps typed events — a path, a time, a channel and where the
event came from — instead of re-deriving heat from each file's current mtime.
These are the pure model's own rules, with no sidecar and no vault: the
session window, the categorical ranking, the caller-scoped tiers (ruling
S5-1), the digest, and how external changes are classified.
"""

from __future__ import annotations

import pytest

from exomem import working_set_heat as heat

KB = "Knowledge Base"
SLED = f"{KB}/Products/Cargo Sled.md"
MARIT = f"{KB}/Entities/People/Marit Solheim.md"
DEPOT = f"{KB}/Projects/Depot Rota.md"
NOTE = f"{KB}/Notes/Research/quillon-vantry-window.md"
LEDGER = f"{KB}/Notes/Research/harbour-ledger.md"

S = 1_000_000_000
H = 3600 * S
T0 = 1_790_000_000 * S


def ev(ts: int, path: str, channel: str, **attribution: str) -> heat.HeatEvent:
    return heat.HeatEvent(ts, path, channel, origin=channel, **attribution)


def profile_of(*events: heat.HeatEvent, sessions=(), state: str = "current") -> heat.HeatProfile:
    return heat.build_profile(events, sessions=sessions, state=state)


def top(profile: heat.HeatProfile, *, limit: int = 5, **kwargs) -> tuple[str, ...]:
    """The referent members: the leading tier's first group, cut at `limit`."""
    return heat.members(heat.leading(profile, **kwargs), limit=limit).paths


# --------------------------------------------------------------------------- #
# The session window and the ranking
# --------------------------------------------------------------------------- #


def test_the_session_starts_after_the_last_six_hour_gap() -> None:
    events = [
        ev(T0, MARIT, "work"),
        ev(T0 + 10 * H, SLED, "read"),
        # Five hours later: the same session.
        ev(T0 + 15 * H, DEPOT, "work"),
        ev(T0 + 15 * H + 30 * S, NOTE, "read"),
    ]

    assert heat.session_start(events) == T0 + 10 * H
    # An edit ten hours before the latest session's start ranks nothing.
    profile = profile_of(*events)
    assert profile.session_start_ns == T0 + 10 * H
    assert MARIT not in profile.window_rows
    # A gap of exactly six hours splits.
    assert heat.session_start([ev(T0, MARIT, "work"), ev(T0 + 6 * H, SLED, "read")]) == T0 + 6 * H
    # Contact-only events never open or extend a session.
    assert heat.session_start([ev(T0, MARIT, "work"), ev(T0 + 20 * H, SLED, "captured")]) == T0
    assert heat.session_start([]) == 0


def test_deliberate_acts_outrank_selection_inside_a_session() -> None:
    # A morning edit still outranks an afternoon lookup.
    profile = profile_of(ev(T0, SLED, "work"), ev(T0 + 4 * H, MARIT, "read"))
    assert top(profile) == (SLED,)
    # An edit ten days old yields to a read made yesterday: it is outside the session.
    profile = profile_of(ev(T0, SLED, "work"), ev(T0 + 10 * 24 * H, MARIT, "read"))
    assert top(profile) == (MARIT,)
    # Picks and episodes are deliberate too; citations are selection.
    profile = profile_of(ev(T0, NOTE, "pick"), ev(T0 + H, MARIT, "cite"))
    assert top(profile) == (NOTE,)
    profile = profile_of(ev(T0, DEPOT, "episode"), ev(T0 + H, MARIT, "read"))
    assert top(profile) == (DEPOT,)
    # Contact-only channels never lead a referent.
    profile = profile_of(ev(T0 + H, f"{KB}/Sources/Sessions/talk.md", "captured"))
    assert top(profile) == ()


def test_an_exact_tie_is_every_member_cut_at_k() -> None:
    paths = [f"{KB}/Notes/n{index}.md" for index in range(7)]
    profile = profile_of(*(ev(T0, path, "work") for path in paths))

    (lead,) = [lead for lead in heat.leading(profile) if lead.groups]
    assert lead.groups[0] == tuple(sorted(paths))
    assert top(profile, limit=5) == tuple(sorted(paths))[:5]
    # A three-page work commit is three events at one time: a tie, not a burst.
    profile = profile_of(ev(T0, SLED, "work"), ev(T0, MARIT, "work"), ev(T0, DEPOT, "work"))
    assert top(profile) == tuple(sorted((SLED, MARIT, DEPOT)))


def test_an_ineligible_top_is_skipped_within_the_read_bound() -> None:
    profile = profile_of(
        ev(T0 + 2 * S, SLED, "work"), ev(T0 + S, MARIT, "work"), ev(T0, DEPOT, "work")
    )
    checked: list[str] = []

    def eligible(path: str) -> bool:
        checked.append(path)
        return path != SLED

    chosen = heat.members(heat.leading(profile), limit=5, eligible=eligible)
    assert chosen.paths == (MARIT,) and chosen.tier == 3
    assert checked == [SLED, MARIT], "the next group is read only after the top failed"
    # At most `limit` eligibility reads, whatever the ring holds.
    assert heat.members(heat.leading(profile), limit=1, eligible=eligible).paths == ()


def test_a_full_ring_is_checked_for_admissibility_only_as_far_as_it_is_read() -> None:
    """A ring of thousands of rows is not run through the admissibility check
    on every request: checking stops once far more rows than any reader walks
    are in hand, and the referent is the one a full pass gives."""
    pages = [f"{KB}/Notes/Journal/note-{index:04d}.md" for index in range(4000)]
    profile = profile_of(*(ev(T0 + index * S, page, "work") for index, page in enumerate(pages)))
    skipped = set(pages[-40:])
    checked: list[str] = []

    def admissible(path: str) -> bool:
        checked.append(path)
        return path not in skipped

    chosen = heat.members(heat.leading(profile, admissible=admissible), limit=5)

    assert chosen.paths == (pages[-41],)
    assert len(checked) <= heat.GROUP_ROWS_MAX + len(skipped) + 1
    full = [page for page in reversed(pages) if page not in skipped]
    assert chosen.paths == (full[0],)


def test_a_read_count_never_outranks_a_newer_read() -> None:
    often = [ev(T0 + index * S, SLED, "read") for index in range(1000)]
    profile = profile_of(*often, ev(T0 + 1001 * S, MARIT, "read"))

    assert top(profile) == (MARIT,)
    # And a thousand reads rank as one: the row keeps its latest time only.
    assert profile.all_rows[SLED].read_ns == T0 + 999 * S


def test_continuity_leads_until_a_deliberate_act_elsewhere() -> None:
    minted = T0 + 10 * S
    kwargs = dict(token_paths=frozenset({MARIT}), token_minted_ns=minted, token_passed=True)

    # A newer read elsewhere does not move the conversation on.
    profile = profile_of(ev(T0, MARIT, "work"), ev(minted + S, SLED, "read"))
    assert top(profile, **kwargs) == (MARIT,)
    # A newer edit, pick or episode elsewhere does.
    for channel in ("work", "pick", "episode"):
        profile = profile_of(ev(T0, MARIT, "work"), ev(minted + S, SLED, channel))
        assert top(profile, **kwargs) == (SLED,), channel
    # An act older than the mint leaves the token leading.
    profile = profile_of(ev(minted - S, SLED, "work"))
    assert top(profile, **kwargs) == (MARIT,)
    # A token that does not say when it was minted leads, as tokens always did.
    profile = profile_of(ev(minted + S, SLED, "work"))
    assert top(profile, token_paths=frozenset({MARIT}), token_passed=True) == (MARIT,)
    # A leading token whose refs all went leads with nothing: it never falls through.
    profile = profile_of(ev(minted - S, SLED, "work"))
    assert top(profile, token_paths=frozenset(), token_minted_ns=minted, token_passed=True) == ()
    # Taken whole: a two-page answer is one tier.
    both = frozenset({MARIT, DEPOT})
    profile = profile_of(ev(minted + S, MARIT, "work"))
    assert top(profile, token_paths=both, token_minted_ns=minted, token_passed=True) == tuple(
        sorted(both)
    )


def test_the_digest_moves_on_a_read_and_not_on_a_batch_or_burst() -> None:
    base = [ev(T0, SLED, "work"), ev(T0 + S, MARIT, "read")]
    digest = profile_of(*base).digest

    # A read that reaches the rows moves it.
    assert profile_of(*base, ev(T0 + 2 * S, DEPOT, "read")).digest != digest
    assert profile_of(*base, ev(T0 + 2 * S, SLED, "pick")).digest != digest
    # A batch and an external burst write no event, so nothing about them can move it.
    burst = heat.fold_external_events(
        {path: (T0 + 3 * S + index, 0, 1) for index, path in enumerate((SLED, MARIT, DEPOT, NOTE))},
        now_ns=T0 + 4 * S,
    )
    assert burst.events == ()
    assert profile_of(*base, *burst.events).digest == digest
    assert heat.classify_commit(SLED, traced=True, in_batch=True, reason="edited") is None
    # The state is part of it.
    assert profile_of(*base, state="behind").digest != digest
    # Deterministic.
    assert profile_of(*base).digest == digest


# --------------------------------------------------------------------------- #
# External changes: bursts, echoes, in-flight commits, skewed clocks
# --------------------------------------------------------------------------- #


def test_an_external_burst_carries_no_heat_and_erases_none() -> None:
    user = ev(T0, SLED, "work")
    # A sync rewrote four pages, the user's own among them, a second apart.
    burst = heat.fold_external_events(
        {path: (T0 + 60 * S + index * S, 0, 1) for index, path in enumerate((SLED, MARIT, DEPOT, NOTE))},
        now_ns=T0 + 120 * S,
    )

    assert burst.events == ()
    # The user's earlier work keeps its own time: the burst erased nothing.
    assert top(profile_of(user, *burst.events)) == (SLED,)
    # One lone external edit is work.
    single = heat.fold_external_events({MARIT: (T0 + 60 * S, 0, 1)}, now_ns=T0 + 120 * S)
    assert [(item.path, item.channel, item.origin) for item in single.events] == [
        (MARIT, "work", "external")
    ]
    # A delta larger than a fold may classify is a sync by definition.
    many = {f"{KB}/Notes/n{index}.md": (T0 + index * 10 * S, 0, 1) for index in range(heat.MAX_FOLD_PATHS + 1)}
    assert heat.fold_external_events(many, now_ns=T0 + H * 10**3).events == ()


def test_the_echo_of_a_governed_commit_is_not_an_external_edit() -> None:
    signature = (T0 + 5 * S, T0 + 5 * S, 120)
    echo = heat.fold_external_events(
        {SLED: signature}, attributed={SLED: signature}, now_ns=T0 + 10 * S
    )
    assert echo.events == ()
    # The same page changed again after the commit is a genuine external edit.
    later = (T0 + 6 * S, T0 + 6 * S, 121)
    edited = heat.fold_external_events(
        {SLED: later}, attributed={SLED: signature}, now_ns=T0 + 10 * S
    )
    assert [(item.path, item.ts_ns) for item in edited.events] == [(SLED, T0 + 6 * S)]
    # An echo neither joins nor forms a burst with genuine edits.
    mixed = heat.fold_external_events(
        {
            SLED: signature,
            MARIT: (T0 + 5 * S, 0, 1),
            DEPOT: (T0 + 5 * S + 1, 0, 1),
        },
        attributed={SLED: signature},
        now_ns=T0 + 10 * S,
    )
    assert sorted(item.path for item in mixed.events) == sorted((MARIT, DEPOT))


def test_an_in_flight_path_is_deferred_not_classified() -> None:
    folded = heat.fold_external_events(
        {SLED: (T0, 0, 1), MARIT: (T0 + S, 0, 1)},
        in_flight=frozenset({SLED}),
        now_ns=T0 + 10 * S,
    )

    assert folded.deferred == frozenset({SLED})
    assert [item.path for item in folded.events] == [MARIT]
    # Deleted paths become tombstones and never events.
    gone = heat.fold_external_events({}, deleted=(DEPOT,), now_ns=T0)
    assert gone.tombstones == frozenset({DEPOT}) and gone.events == ()


def test_a_future_dated_mtime_is_clamped_to_observation() -> None:
    now = T0 + 10 * S
    folded = heat.fold_external_events({SLED: (T0 + 400 * 24 * H, 0, 1)}, now_ns=now)

    assert [item.ts_ns for item in folded.events] == [now]
    # So a later edit on this machine can still outrank it.
    profile = profile_of(*folded.events, ev(now + S, MARIT, "work"))
    assert top(profile) == (MARIT,)


def test_raw_material_and_navigation_are_never_events() -> None:
    folded = heat.fold_external_events(
        {
            f"{KB}/Sources/Articles/raw.md": (T0, 0, 1),
            f"{KB}/Evidence/proof.md": (T0, 0, 1),
            f"{KB}/index.md": (T0, 0, 1),
            f"{KB}/Sources/Sessions/talk.md": (T0 + S, 0, 1),
            f"{KB}/Sources/Episodes/recap.md": (T0 + 2 * S, 0, 1),
        },
        now_ns=T0 + 10 * S,
    )

    assert sorted((item.path, item.channel) for item in folded.events) == [
        (f"{KB}/Sources/Episodes/recap.md", "episode_page"),
        (f"{KB}/Sources/Sessions/talk.md", "captured"),
    ]


# --------------------------------------------------------------------------- #
# The cold seed
# --------------------------------------------------------------------------- #


def _seeded_top(mtimes: dict[str, int]) -> tuple[str, ...]:
    return top(profile_of(*heat.seed_events(mtimes), state="seeded"))


def _pre_projection_edit_tier(mtimes: dict[str, int]) -> frozenset[str]:
    """The edit tier of `hot_profile` at `e1429082`, restated: burst members
    carry no edit, nor does anything at or before the latest burst's newest
    edit (R-N2); the leading tier is every page at the newest remaining time."""
    burst = heat.burst_paths(mtimes)
    after = max((mtimes[path] for path in burst), default=0)
    # Anchors are working context: never a navigation page or raw material.
    reason = heat.default_reason_for(mtimes)
    edited = {
        path: mtime
        for path, mtime in mtimes.items()
        if reason(path) == "edited" and path not in burst and mtime > after
    }
    if not edited:
        return frozenset()
    newest = max(edited.values())
    return frozenset(path for path, mtime in edited.items() if mtime == newest)


SEED_SHAPES = {
    "one fresh edit": {SLED: T0 + H, MARIT: T0, DEPOT: T0 - 60 * S, NOTE: T0 - 120 * S},
    "a whole vault in one burst": {SLED: T0, MARIT: T0, DEPOT: T0, NOTE: T0},
    "an edit after the burst": {
        MARIT: T0,
        DEPOT: T0 + S,
        NOTE: T0 + 2 * S,
        SLED: T0 + H,
    },
    "an edit before the burst": {
        SLED: T0,
        MARIT: T0 + H,
        DEPOT: T0 + H + S,
        NOTE: T0 + H + 2 * S,
    },
    "a stalled burst": {
        MARIT: T0,
        DEPOT: T0 + int(2.9 * S),
        NOTE: T0 + int(3.2 * S),
        SLED: T0 - 2 * H,
    },
    "two pages at one time": {SLED: T0 + H, MARIT: T0 + H, DEPOT: T0},
    "navigation never makes a burst": {
        SLED: T0 + H,
        f"{KB}/index.md": T0 + H + 1,
        f"{KB}/log.md": T0 + H + 2,
        MARIT: T0,
    },
}


@pytest.mark.parametrize("shape", sorted(SEED_SHAPES))
def test_the_seed_reproduces_the_pre_projection_ranking(shape: str) -> None:
    mtimes = SEED_SHAPES[shape]

    assert frozenset(_seeded_top(mtimes)) == _pre_projection_edit_tier(mtimes)
    seeded = heat.seed_events(mtimes)
    assert all(item.origin == "seed" for item in seeded)
    assert len(heat.seed_events({f"{KB}/Notes/n{i}.md": T0 + i * H for i in range(400)})) == (
        heat.SEED_MAX
    )


# --------------------------------------------------------------------------- #
# Ruling S5-1: the caller's own session first, then its workspace, then the vault
# --------------------------------------------------------------------------- #

MINE = heat.Attribution(session="s-one", workspace="w-exomem")
OTHER = heat.Attribution(session="s-two", workspace="w-kitchen")


def _mark(session: str, workspace: str, paths: tuple[str, ...], minted: int) -> heat.SessionMark:
    return heat.SessionMark(
        session=session, workspace=workspace, client="claude-code", paths=paths, minted_ns=minted,
        seen_ns=minted,
    )


def test_the_callers_own_session_leads_over_a_newer_act_elsewhere() -> None:
    profile = profile_of(
        ev(T0, SLED, "pick", session="s-one", workspace="w-exomem"),
        # Another session's pick, and unattributed work, both newer.
        ev(T0 + H, MARIT, "pick", session="s-two", workspace="w-kitchen"),
        ev(T0 + 2 * H, DEPOT, "work"),
    )

    assert top(profile, attribution=MINE) == (SLED,)
    assert top(profile, attribution=OTHER) == (MARIT,)
    # A caller with no keys gets the vault-wide ranking, which is today's.
    assert top(profile) == top(profile, attribution=heat.Attribution()) == (DEPOT,)


def test_a_sessions_last_served_thread_leads_its_own_tier() -> None:
    marks = (
        _mark("s-one", "w-exomem", (SLED,), T0),
        _mark("s-two", "w-kitchen", (MARIT,), T0 + H),
    )
    profile = profile_of(ev(T0 + 2 * H, DEPOT, "work"), sessions=marks)

    assert top(profile, attribution=MINE) == (SLED,)
    assert top(profile, attribution=OTHER) == (MARIT,)
    # Its own deliberate act after the mint moves the session on.
    profile = profile_of(
        ev(T0 + 2 * H, NOTE, "pick", session="s-one"), sessions=marks
    )
    assert top(profile, attribution=MINE) == (NOTE,)


def test_a_fresh_session_takes_its_workspaces_thread() -> None:
    marks = (
        _mark("s-old", "w-exomem", (SLED,), T0),
        _mark("s-two", "w-kitchen", (MARIT,), T0 + H),
    )
    profile = profile_of(ev(T0 + 2 * H, DEPOT, "work"), sessions=marks)
    fresh = heat.Attribution(session="s-new", workspace="w-exomem")

    assert top(profile, attribution=fresh) == (SLED,)
    # An episode recorded under another session of the workspace counts too.
    profile = profile_of(
        ev(T0 + 3 * H, NOTE, "episode", session="s-old"), sessions=marks
    )
    assert top(profile, attribution=fresh) == (NOTE,)
    # A workspace with nothing in it falls through to the vault.
    elsewhere = heat.Attribution(session="s-new", workspace="w-garden")
    assert top(profile, attribution=elsewhere) == top(profile)


def test_selection_never_lifts_a_tier_over_a_deliberate_act_below_it() -> None:
    profile = profile_of(
        ev(T0, SLED, "work"),
        ev(T0 + H, MARIT, "read", session="s-one", workspace="w-exomem"),
    )

    leads = heat.leading(profile, attribution=MINE)
    assert [lead.tier for lead in leads] == [3]
    assert top(profile, attribution=MINE) == (SLED,)


def test_recent_lists_the_leading_tier_first_then_fills_from_below() -> None:
    marks = (_mark("s-one", "w-exomem", (SLED,), T0),)
    profile = profile_of(
        ev(T0 + H, MARIT, "work"),
        ev(T0 + 2 * H, DEPOT, "read"),
        ev(T0 + 3 * H, NOTE, "pick", session="s-two", workspace="w-exomem"),
        sessions=marks,
    )

    mine = heat.recent(profile, attribution=MINE)
    assert [(item.path, item.tier) for item in mine] == [
        (SLED, 1),
        (NOTE, 2),
        (DEPOT, 3),
        (MARIT, 3),
    ]
    assert [item.reason for item in mine] == ["activated", "activated", "activated", "edited"]
    # Keyless: newest contact first, which is the vault-wide order.
    assert [item.path for item in heat.recent(profile)] == [NOTE, DEPOT, MARIT]


def test_the_view_digest_moves_with_the_callers_own_tier_only() -> None:
    marks = (_mark("s-one", "w-exomem", (SLED,), T0), _mark("s-two", "w-kitchen", (MARIT,), T0))
    profile = profile_of(ev(T0, DEPOT, "work"), sessions=marks)
    moved = profile_of(
        ev(T0, DEPOT, "work"),
        sessions=(_mark("s-one", "w-exomem", (NOTE,), T0 + S), marks[1]),
    )

    assert heat.view_digest(profile, heat.Attribution()) == profile.digest
    assert heat.view_digest(profile, MINE) != heat.view_digest(moved, MINE)
    assert heat.view_digest(profile, OTHER) == heat.view_digest(moved, OTHER)


# --------------------------------------------------------------------------- #
# The sidecar
# --------------------------------------------------------------------------- #


@pytest.fixture
def sidecar_vault(tmp_path):
    vault = tmp_path / "vault"
    (vault / "Knowledge Base").mkdir(parents=True)
    heat.reset_for_tests()
    yield vault
    heat.reset_for_tests()


def test_the_ring_drops_the_oldest_event(sidecar_vault, monkeypatch) -> None:
    monkeypatch.setattr(heat, "RING_MAX", 8)

    assert heat.append(sidecar_vault, [ev(T0 + index * S, SLED, "read") for index in range(6)])
    assert heat.append(sidecar_vault, [ev(T0 + (6 + index) * S, MARIT, "read") for index in range(4)])

    loaded = heat.load(sidecar_vault)
    assert len(loaded.events) == 8
    assert [item.ts_ns for item in loaded.events] == [T0 + index * S for index in range(2, 10)]
    assert loaded.all_rows[SLED].read_ns == T0 + 5 * S
    # The sidecar lives beside the activation index: machine-local, never in the vault.
    assert not list(sidecar_vault.rglob("*.sqlite"))
    assert heat.sidecar_path(sidecar_vault).exists()


def test_a_busy_sidecar_drops_the_event_and_fails_nothing(sidecar_vault) -> None:
    import sqlite3
    import time

    from exomem import metrics

    heat.append(sidecar_vault, [ev(T0, SLED, "work")])
    before = metrics.snapshot()
    blocker = sqlite3.connect(heat.sidecar_path(sidecar_vault), timeout=0)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        written = heat.append(sidecar_vault, [ev(T0 + S, MARIT, "read")])
        took = time.monotonic() - started
    finally:
        blocker.rollback()
        blocker.close()

    assert written is False
    assert took < 1.0, "a seam never waits seconds for heat"
    after = metrics.snapshot()
    assert str(after) != str(before), "the drop is counted"
    assert [item.path for item in heat.load(sidecar_vault).events] == [SLED]


def test_deleting_the_sidecar_costs_only_a_reseed(sidecar_vault) -> None:
    heat.append(sidecar_vault, [ev(T0, SLED, "work")])
    assert heat.load(sidecar_vault).events

    heat.sidecar_path(sidecar_vault).unlink()
    for suffix in ("-wal", "-shm"):
        extra = heat.sidecar_path(sidecar_vault).with_name(heat.SIDECAR_NAME + suffix)
        if extra.exists():
            extra.unlink()

    fresh = heat.load(sidecar_vault)
    assert fresh.events == () and fresh.state == "empty"
    assert "seeded_at_ns" not in fresh.meta, "a new sidecar asks to be seeded again"
    assert heat.append(sidecar_vault, [ev(T0 + S, MARIT, "read")])
    assert [item.path for item in heat.load(sidecar_vault).events] == [MARIT]


def test_a_schema_change_wipes_and_asks_for_a_reseed(sidecar_vault, monkeypatch) -> None:
    heat.append(sidecar_vault, [ev(T0, SLED, "work")])
    heat.set_meta(sidecar_vault, {"seeded_at_ns": T0})
    before = heat.load(sidecar_vault)
    assert before.events and before.meta.get("seeded_at_ns")

    monkeypatch.setattr(heat, "SCHEMA_VERSION", heat.SCHEMA_VERSION + 1)
    after = heat.load(sidecar_vault)

    assert after.events == ()
    assert "seeded_at_ns" not in after.meta
    assert after.token != before.token, "the token keeps counting across a wipe"


def test_the_kill_switch_opens_nothing(sidecar_vault, monkeypatch) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_WORKING_SET", "1")

    assert heat.append(sidecar_vault, [ev(T0, SLED, "work")]) is False
    assert heat.note_session(sidecar_vault, heat.SessionMark("s", paths=(SLED,))) is False
    assert heat.load(sidecar_vault).events == ()
    assert not heat.sidecar_path(sidecar_vault).exists()


def test_another_processs_events_are_seen_on_the_next_token(sidecar_vault) -> None:
    import sqlite3

    heat.append(sidecar_vault, [ev(T0, SLED, "work")])
    first = heat.load(sidecar_vault)
    assert heat.load(sidecar_vault) is first, "an unmoved token reuses the aggregate"

    # Another process: its own connection, its own insert, the shared token bump.
    other = sqlite3.connect(heat.sidecar_path(sidecar_vault))
    with other:
        other.execute(
            "INSERT INTO heat_events (ts_ns, path, channel, origin, client, session, workspace) "
            "VALUES (?, ?, 'read', 'read', '', '', '')",
            (T0 + S, MARIT),
        )
        other.execute("UPDATE meta SET value = value + 1 WHERE key = 'generation'")
    other.close()

    second = heat.load(sidecar_vault)
    assert second is not first
    assert [item.path for item in second.events] == [SLED, MARIT]


def test_session_and_workspace_keys_are_derived_never_stored_raw(sidecar_vault) -> None:
    import sqlite3

    raw_session, raw_workspace = "ep-" + "ab" * 16, "c0ffee" * 4
    mine = heat.attribution_for(
        sidecar_vault, client="claude-code", session=raw_session, workspace=raw_workspace
    )
    assert mine.session and mine.workspace and mine.client == "claude-code"
    assert raw_session not in mine and raw_workspace not in mine
    assert len(mine.session) == heat.KEY_HEX
    # Stable per sidecar, so a session is recognised on its next turn.
    again = heat.attribution_for(sidecar_vault, session=raw_session, workspace=raw_workspace)
    assert (again.session, again.workspace) == (mine.session, mine.workspace)
    # Unusable values are ignored, never refused.
    odd = heat.attribution_for(sidecar_vault, client="Not A Label", session="x" * 300, workspace=7)
    assert odd == heat.Attribution()

    heat.note_session(
        sidecar_vault,
        heat.SessionMark(mine.session, mine.workspace, mine.client, (SLED,), T0, T0),
    )
    heat.append(sidecar_vault, [ev(T0, SLED, "pick", session=mine.session)])
    dump = "\n".join(sqlite3.connect(heat.sidecar_path(sidecar_vault)).iterdump())
    assert raw_session not in dump and raw_workspace not in dump
    assert mine.session in dump


def test_a_session_keeps_its_thread_through_an_abstention_and_the_table_is_bounded(
    sidecar_vault, monkeypatch
) -> None:
    heat.note_session(sidecar_vault, heat.SessionMark("s1", "w1", "codex", (SLED,), T0, T0))
    heat.note_session(sidecar_vault, heat.SessionMark("s1", "w2", "codex", (), 0, T0 + S))

    mark = heat.load(sidecar_vault).sessions["s1"]
    assert mark.paths == (SLED,) and mark.minted_ns == T0
    assert mark.workspace == "w2" and mark.seen_ns == T0 + S

    monkeypatch.setattr(heat, "SESSIONS_MAX", 3)
    for index in range(5):
        heat.note_session(
            sidecar_vault, heat.SessionMark(f"s{index + 2}", "w", "", (MARIT,), T0, T0 + 10 * S + index)
        )
    assert sorted(heat.load(sidecar_vault).sessions) == ["s4", "s5", "s6"]


# --------------------------------------------------------------------------- #
# The governed seam: origin, not timing, says whose write it was
# --------------------------------------------------------------------------- #

INSIGHT = "Knowledge Base/Notes/Insights/rrf-fusion-beats-score-normalization.md"
PATTERN = "Knowledge Base/Notes/Patterns/retry-with-fixed-interval.md"


def _command(name: str):
    from exomem import commands

    return next(command for command in commands.PRODUCT_COMMANDS if command.name == name)


def _edit(vault, rel: str, old: str, new: str) -> dict:
    from exomem import writer_lease

    return writer_lease.invoke_command(
        _command("edit_memory"),
        vault,
        path=rel,
        why="heat seam test",
        operation={"kind": "replace_string", "old_string": old, "new_string": new},
    )


def _work(vault) -> list[tuple[str, str]]:
    return [(item.path, item.origin) for item in heat.load(vault).events if item.channel == "work"]


def test_a_governed_work_write_records_one_work_event_per_page(vault) -> None:
    from exomem import freshness

    heat.reset_for_tests()
    _edit(vault, INSIGHT, "is more robust than", "is steadier than")

    # The same commit rewrote the activity log and the index: navigation, not work.
    assert _work(vault) == [(INSIGHT, "edit_memory")]
    # Our own commit's signature is kept, so the watcher's echo of it is known.
    signatures = heat.attributed_signatures(vault, [INSIGHT])
    assert signatures[INSIGHT] == tuple(freshness.stat_signature(vault / INSIGHT))
    # A second commit is a second event at its own time, never a count.
    _edit(vault, INSIGHT, "is steadier than", "is sturdier than")
    events = [item for item in heat.load(vault).events if item.channel == "work"]
    assert [item.path for item in events] == [INSIGHT, INSIGHT]
    assert events[0].ts_ns < events[1].ts_ns
    assert heat.load(vault).all_rows[INSIGHT].work_ns == events[1].ts_ns


def test_maintain_memory_backfill_records_no_heat(vault) -> None:
    from exomem import writer_lease

    heat.reset_for_tests()
    pages = sorted(
        str(page.relative_to(vault)) for page in (vault / "Knowledge Base").rglob("*.md")
    )
    writer_lease.invoke_command(
        _command("maintain_memory"), vault, mode="backfill-ids", dry_run=False
    )

    ours = heat.attributed_signatures(vault, pages)
    assert len(ours) >= 3, "the batch must rewrite pages, or this proves nothing"
    assert _work(vault) == []
    assert heat.load(vault).events == ()


def test_a_user_write_concurrent_with_a_batch_is_work(vault) -> None:
    import threading

    from exomem import due_state

    heat.reset_for_tests()
    entered, release = threading.Event(), threading.Event()
    inside: list[bool] = []

    def batch() -> None:
        with due_state.batch_scope(vault):
            inside.append(due_state.in_batch_scope())
            entered.set()
            release.wait(30)

    worker = threading.Thread(target=batch)
    worker.start()
    try:
        assert entered.wait(30)
        # The process-wide flag says a batch is running; the request is not in it.
        assert due_state.batch_active(vault)
        assert not due_state.in_batch_scope()
        _edit(vault, INSIGHT, "is more robust than", "is steadier than")
    finally:
        release.set()
        worker.join(30)

    assert inside == [True]
    assert _work(vault) == [(INSIGHT, "edit_memory")]


def test_a_write_without_a_mutation_trace_records_no_heat(vault) -> None:
    from exomem import freshness, writer_lease
    from exomem import vault as vault_module

    heat.reset_for_tests()
    target = vault / PATTERN
    assert writer_lease.active_mutation_trace() is None
    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(path=target, content=target.read_text() + "\nMore.\n")],
        vault_root=vault,
    )

    assert _work(vault) == []
    # Still ours: its echo must not come back as an external edit.
    assert heat.attributed_signatures(vault, [PATTERN]) == {
        PATTERN: tuple(freshness.stat_signature(target))
    }


def test_a_failed_commit_records_nothing(vault, monkeypatch) -> None:
    from exomem import vault as vault_module

    heat.reset_for_tests()

    def refuse(*_args, **_kwargs):
        raise OSError("disk went away mid-batch")

    monkeypatch.setattr(vault_module, "_batch_atomic_write_locked", refuse)
    with pytest.raises(Exception):  # noqa: B017 - whatever the command maps it to
        _edit(vault, INSIGHT, "is more robust than", "is steadier than")

    assert heat.load(vault).events == ()
    assert heat.attributed_signatures(vault, [INSIGHT]) == {}
    assert heat.in_flight(vault) == frozenset()


def _write_kwargs(name: str) -> dict:
    """Arguments that put each write command on its writer path. Most need
    none: an omitted required selector stays on the conservative path."""
    return {
        "edit_memory": {
            "path": INSIGHT,
            "operation": {"kind": "replace_string", "old_string": "a", "new_string": "b"},
        },
        "govern_memory": {"operation": "commit"},
    }.get(name, {})


def _write_commands() -> list[str]:
    from exomem import commands

    return sorted(command.name for command in commands.PRODUCT_COMMANDS if command.cli_writes)


@pytest.mark.parametrize("name", _write_commands())
def test_every_product_write_command_commits_under_a_trace(vault, name: str) -> None:
    """A work command that lost its trace would lose its heat silently; this
    fails loudly instead. Table-driven over every write-capable command, so a
    new one is covered the day it is registered."""
    import dataclasses

    from exomem import writer_lease

    seen: list[tuple[str, str, str] | None] = []

    def leaf(*_args, **_kwargs):
        seen.append(writer_lease.active_mutation_trace())
        return {"ok": True}

    spied = dataclasses.replace(_command(name), leaf=leaf)
    writer_lease.invoke_command(spied, vault, **_write_kwargs(name))

    assert len(seen) == 1, "the leaf must run on the writer path"
    assert seen[0] is not None and seen[0][1] == name


# --------------------------------------------------------------------------- #
# The selection seams: reads, citations and admitted picks
# --------------------------------------------------------------------------- #

SESSION_CAPTURE = "Knowledge Base/Sources/Sessions/2026-05-05-metabolism-curriculum-design.md"
ARTICLE = "Knowledge Base/Sources/Articles/2026-06-02-postgres-autovacuum-tuning.md"
NONSENSE_TURN = "zqxwvu plonktastic frobnitz quibblewhomp"
NAMED_TURN = "I'm planning to tow the Cargo Sled north — how much depot stock is left?"
REAL_ANCHOR = "Knowledge Base/Products/Cargo Sled.md"


def _selections(vault) -> list[tuple[str, str]]:
    return [
        (item.path, item.channel)
        for item in heat.load(vault).events
        if item.channel in (*heat.SELECTION, "pick")
    ]


def _read(vault, rel: str):
    from exomem import writer_lease

    return writer_lease.invoke_command(_command("read_memory"), vault, path=rel)


@pytest.fixture
def carry_vault(vault):
    from test_working_set_carry import _seed_carry_pages
    from test_working_set_index import _seed_planning, _seed_structure

    from exomem import lexstore, working_set_index, working_set_runtime

    _seed_structure(vault)
    _seed_planning(vault)
    _seed_carry_pages(vault)
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    heat.reset_for_tests()
    return vault


def test_a_read_records_a_read_event(vault) -> None:
    from test_governance_egress import _external, _reset_caches, write_rule, write_scope

    from exomem import commands
    from exomem.governance.principal import request_scope

    heat.reset_for_tests()
    _read(vault, INSIGHT)
    commands.op_get(vault, PATTERN)
    # Raw material is evidence about work, not the work: its read is no event.
    _read(vault, ARTICLE)

    assert _selections(vault) == [(INSIGHT, "read"), (PATTERN, "read")]

    # A read this caller is not released is no event either: otherwise trying
    # to read a withheld page would heat it, and "continue" would tell the
    # caller it exists.
    write_scope(vault, paths="Knowledge Base/Notes/Patterns/*", name="Patterns")
    write_rule(vault, ceiling=0)
    _reset_caches()
    with request_scope(_external()):
        try:
            commands.op_get(vault, PATTERN)
        except Exception:  # noqa: BLE001 - whatever the refusal is, it records nothing
            pass
    assert _selections(vault) == [(INSIGHT, "read"), (PATTERN, "read")]


def test_a_citation_records_a_cite_event(vault) -> None:
    from exomem import writer_lease

    heat.reset_for_tests()
    content = (
        "Retries against the queue stay bounded.\n\n## Observations\n\n"
        "- [operations] Retries against the ingest queue stay bounded at three attempts.\n\n"
        "## Relations\n\n- relates_to [[Idempotency Key]]\n"
    )
    writer_lease.invoke_command(
        _command("remember"),
        vault,
        content=content,
        title="Bounded ingest retries",
        slug="bounded-ingest-retries",
        sources=[SESSION_CAPTURE, ARTICLE],
    )

    # The captured session it drew on is a citation; the article is raw material.
    assert [pair for pair in _selections(vault) if pair[1] == "cite"] == [
        (SESSION_CAPTURE, "cite")
    ]


def test_an_admitted_pick_records_a_pick_and_a_refused_pick_records_nothing(
    carry_vault,
) -> None:
    from test_governance_egress import _external, _reset_caches, write_rule, write_scope
    from test_working_set_carry import CARRY_PAGE

    from exomem import commands, working_set_runtime
    from exomem.governance.principal import request_scope

    session = "ep-" + "5e" * 16
    commands.op_activate_context(
        carry_vault, turn=NONSENSE_TURN, anchor=CARRY_PAGE, client="codex", session=session
    )
    commands.op_activate_context(carry_vault, turn=NONSENSE_TURN, anchor=REAL_ANCHOR)

    picks = [item for item in heat.load(carry_vault).events if item.channel == "pick"]
    assert [item.path for item in picks] == [CARRY_PAGE, REAL_ANCHOR]
    # The caller's own attribution, derived, never the value it passed.
    assert picks[0].session == heat.attribution_for(carry_vault, session=session).session
    assert picks[0].client == "codex" and session not in picks[0]
    assert picks[1].session == "" and picks[1].client == ""

    with pytest.raises(ValueError):
        commands.op_activate_context(
            carry_vault, turn=NONSENSE_TURN, anchor="Knowledge Base/Nowhere/absent.md"
        )
    write_scope(carry_vault, paths="Knowledge Base/Notes/Research/*", name="Research")
    write_rule(carry_vault, ceiling=0)
    _reset_caches()
    working_set_runtime.reset_caches_for_tests()
    with pytest.raises(ValueError):
        with request_scope(_external()):
            commands.op_activate_context(carry_vault, turn=NONSENSE_TURN, anchor=CARRY_PAGE)

    assert [item.path for item in heat.load(carry_vault).events if item.channel == "pick"] == [
        CARRY_PAGE,
        REAL_ANCHOR,
    ]


def test_serving_a_packet_records_no_heat(carry_vault) -> None:
    from exomem import commands

    first = commands.op_activate_context(carry_vault, turn=NAMED_TURN)
    assert first["abstained"] is False, first.get("abstention")
    settled = heat.load(carry_vault).events

    for _ in range(3):
        commands.op_activate_context(carry_vault, turn=NAMED_TURN)
        commands.op_activate_context(carry_vault, turn="continue")

    # Serving is the server's own output, not a choice: nothing accumulates.
    assert heat.load(carry_vault).events == settled
    assert not [item for item in settled if item.channel in (*heat.SELECTION, "pick")]


# --------------------------------------------------------------------------- #
# The external fold, the cold seed and the watermark
# --------------------------------------------------------------------------- #


def _live(vault) -> None:
    """Seed the freshness registry the way the running service's watcher does."""
    from exomem import file_watcher

    file_watcher.FileWatcher(vault)._reconcile_once(seed=True)


def _age(vault, *, newest: str | None = None) -> None:
    """Every page old and a minute apart (no burst), `newest` edited now."""
    import os
    import time

    now = time.time()
    for index, page in enumerate(sorted((vault / "Knowledge Base").rglob("*.md"))):
        stamp = now - 10_000 - index * 60
        os.utime(page, (stamp, stamp))
    if newest:
        os.utime(vault / newest, (now, now))


def _external_edit(vault, rel: str, *, text: str = "\nAn external line.\n") -> None:
    from exomem import freshness

    page = vault / rel
    page.write_text(page.read_text(encoding="utf-8") + text, encoding="utf-8")
    freshness.on_files_changed(vault, changed=[page])


def test_the_profile_reports_seeded_then_current(vault) -> None:
    heat.reset_for_tests()
    _age(vault, newest=INSIGHT)
    _live(vault)

    first = heat.profile(vault)
    assert first.state == "seeded"
    assert {item.origin for item in first.events} == {"seed"}
    assert heat.members(heat.leading(first)).paths == (INSIGHT,)

    _external_edit(vault, PATTERN)
    second = heat.profile(vault)
    assert second.state == "current"
    assert heat.members(heat.leading(second)).paths == (PATTERN,)


def test_an_external_single_edit_is_work(vault) -> None:
    import time

    heat.reset_for_tests()
    _age(vault)
    _live(vault)
    heat.profile(vault)

    before = time.time_ns()
    _external_edit(vault, PATTERN)
    events = [item for item in heat.profile(vault).events if item.origin == "external"]

    assert [(item.path, item.channel) for item in events] == [(PATTERN, "work")]
    assert events[0].ts_ns <= time.time_ns() and events[0].ts_ns >= before - 10**9
    # A governed commit's own echo through the same registry is ours, not external.
    from exomem import freshness

    _edit(vault, INSIGHT, "is more robust than", "is steadier than")
    freshness.on_files_changed(vault, changed=[vault / INSIGHT])
    profile = heat.profile(vault)
    assert [
        (item.path, item.origin)
        for item in profile.events
        if item.path == INSIGHT and item.origin != "seed"
    ] == [(INSIGHT, "edit_memory")]


def test_a_deleted_page_leaves_the_profile_at_once(vault) -> None:
    from exomem import freshness

    heat.reset_for_tests()
    _age(vault, newest=INSIGHT)
    _live(vault)
    assert INSIGHT in heat.profile(vault).all_rows

    (vault / INSIGHT).unlink()
    freshness.on_files_changed(vault, deleted=[vault / INSIGHT])

    assert INSIGHT not in heat.profile(vault).all_rows
    heat.reset_for_tests()
    assert INSIGHT not in {item.path for item in heat.load(vault).events}, "purged on flush"


def test_the_profile_reports_partial_without_a_watcher(vault) -> None:
    heat.reset_for_tests()
    _edit(vault, INSIGHT, "is more robust than", "is steadier than")

    profile = heat.profile(vault)
    assert profile.state == "partial", "governed activity only"
    assert [item.origin for item in profile.events] == ["edit_memory"]
    assert "seeded_at_ns" not in profile.meta, "no watcher, nothing to seed from"

    heat.reset_for_tests()
    empty = heat.profile(vault / ".." / "elsewhere")
    assert empty.state == "empty"


def test_a_restart_recovers_external_edits_from_the_watermark(vault) -> None:
    import os
    import time

    heat.reset_for_tests()
    _age(vault, newest=INSIGHT)
    _live(vault)
    assert heat.profile(vault).state == "seeded"
    watermark = int(heat.load(vault).meta["observed_through_ns"])

    # The process restarts: its fold checkpoint is gone. Meanwhile one page was
    # edited on disk after the last fold, and another before it.
    heat.reset_for_tests()
    time.sleep(0.05)
    stamp = time.time()
    os.utime(vault / PATTERN, (stamp, stamp))
    before = (watermark - 5 * 10**9) / 1e9
    os.utime(vault / "Knowledge Base/Notes/Failures/cache-stampede-on-cold-start.md", (before, before))
    _live(vault)

    profile = heat.profile(vault)
    external = [(item.path, item.ts_ns) for item in profile.events if item.origin == "external"]
    assert [path for path, _ts in external] == [PATTERN], "only what changed after the watermark"
    assert external[0][1] == int(stamp * 1e9) or abs(external[0][1] - stamp * 1e9) < 2e6
    assert profile.state == "current"
    assert int(heat.load(vault).meta["observed_through_ns"]) > watermark


def test_an_incomplete_delta_reports_behind_and_schedules_one_reconcile(
    vault, monkeypatch
) -> None:
    import os
    import time

    from exomem import readiness

    heat.reset_for_tests()
    _age(vault, newest=INSIGHT)
    _live(vault)
    heat.profile(vault)

    scheduled: list[object] = []
    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
    monkeypatch.setattr(heat, "_start_reconcile", lambda work: scheduled.append(work))
    later = time.time() + 1
    os.utime(vault / PATTERN, (later, later))
    _live(vault)  # a reseed: no retained history bridges the old checkpoint

    assert heat.profile(vault).state == "behind"
    assert heat.profile(vault).state == "behind"
    assert len(scheduled) == 1, "one reconcile, however many requests arrive meanwhile"

    scheduled[0]()
    profile = heat.profile(vault)
    assert profile.state == "current"
    assert [item.path for item in profile.events if item.origin == "external"] == [PATTERN]
