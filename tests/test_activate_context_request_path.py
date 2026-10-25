"""Lane A1 — the context-activation request path is timed, fails fast on a
managed freshness mismatch instead of reprojecting or walking, and honours
the existing request budget.

Contract: openspec/changes/close-memory-loop/tasks.md task 6.9 and
design.md's "Activation latency repair" section. Background: a live trial of
automatic context activation took 42 s and 296 s per request while prepared
snapshots ran in 141-524 ms; most of the live time was in code with no timing
span.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_working_set_index import _seed_planning, _seed_structure

from exomem import (
    commands,
    freshness,
    lexstore,
    readiness,
    recall_policy,
    request_budget,
    working_set,
    working_set_index,
    working_set_runtime,
    working_set_state,
)
from exomem import find as find_module
from exomem import find_types as find_types_module
from exomem import vault as vault_module

TURN = "I'm planning to tow the Cargo Sled north — what are its constraints?"


@pytest.fixture
def activation_vault(vault: Path) -> Path:
    """A vault whose activation index is built and ready to resolve TURN."""
    _seed_structure(vault)
    _seed_planning(vault)
    lexstore.ensure_fresh(vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    return vault


@pytest.fixture
def budget_free():
    """No budget bound, whatever a previous test left behind."""
    token = request_budget.set_current(None)
    try:
        yield
    finally:
        request_budget.reset_current(token)


@pytest.fixture
def bind_budget():
    tokens = []

    def _bind(seconds: float) -> request_budget.RequestBudget:
        budget = request_budget.RequestBudget(seconds=seconds)
        tokens.append(request_budget.set_current(budget))
        return budget

    try:
        yield _bind
    finally:
        for token in reversed(tokens):
            request_budget.reset_current(token)


def _seed_live_recall(root: Path) -> None:
    """Mark both recall scopes live at the CURRENT recall-policy identity."""
    kb = root / "Knowledge Base"
    freshness.seed(
        root,
        "vault",
        ((str(path), freshness.stat_signature(path)) for path in vault_module.walk_vault_md(root)),
    )
    freshness.seed(
        root,
        "kb",
        ((str(path), freshness.stat_signature(path)) for path in find_module._walk_md(kb)),
    )


# --------------------------------------------------------------------------- #
# T1 — spans, retained on abstention too
# --------------------------------------------------------------------------- #


def test_freshness_current_state_and_guard_spans_are_registered(
    activation_vault: Path,
) -> None:
    packet = commands.op_activate_context(activation_vault, turn=TURN, include_timings=True)

    assert packet["abstained"] is False
    stages = packet["timings"]["stages"]
    assert {
        "working_set.freshness",
        "working_set.current_state",
        "working_set.guard",
    } <= set(stages)


def test_no_reported_stage_is_nested_inside_another_reported_stage(
    activation_vault: Path,
) -> None:
    # The timing table is flat by contract: its stages sum to no more than the
    # total. A span reported alongside the parent that contains it is counted
    # twice, and the sum then exceeds the total whenever the child is large.
    packet = commands.op_activate_context(activation_vault, turn=TURN, include_timings=True)

    names = sorted(packet["timings"]["stages"])
    nested = [
        (parent, child)
        for parent in names
        for child in names
        if child.startswith(parent + ".")
    ]
    assert nested == []


def test_a_disabled_abstention_still_returns_its_timings(
    activation_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXOMEM_DISABLE_WORKING_SET", "1")

    packet = commands.op_activate_context(activation_vault, turn=TURN, include_timings=True)

    assert packet["abstention"] == {"reason": "disabled"}
    assert "timings" in packet


def test_an_unresolved_abstention_still_returns_its_timings(
    activation_vault: Path,
) -> None:
    packet = commands.op_activate_context(activation_vault, turn="   ", include_timings=True)

    assert packet["abstention"] == {"reason": "unresolved"}
    assert "timings" in packet


def test_activation_timings_have_no_parent_conflicts(
    activation_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new `working_set.freshness` span must not alias `recall_projection`
    (or any other stage name) under a second, different parent — that would
    break the timing table's root partition (`FindTimings.parent_conflicts`).

    `op_activate_context` builds its own `FindTimings` internally and only
    exposes `.as_dict()` on the packet, so the real object is captured by
    wrapping the constructor it calls.
    """
    captured: list[find_types_module.FindTimings] = []
    real_ctor = find_types_module.FindTimings

    def _capturing_ctor():
        instance = real_ctor()
        captured.append(instance)
        return instance

    monkeypatch.setattr(find_types_module, "FindTimings", _capturing_ctor)

    packet = commands.op_activate_context(activation_vault, turn=TURN, include_timings=True)

    assert packet["abstained"] is False
    assert len(captured) == 1
    assert captured[0].parent_conflicts() == {}
    assert "working_set.freshness" in packet["timings"]["stages"]


# --------------------------------------------------------------------------- #
# T2 — managed activation fails fast on a freshness identity mismatch
# --------------------------------------------------------------------------- #


def test_managed_activation_fails_fast_on_a_mismatched_recall_identity(
    activation_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_live_recall(activation_vault)
    # Sanity: the registry really is live and matching before the mismatch is
    # introduced, so the fail-fast path below is exercised for the right reason.
    assert freshness.live_recall_checkpoint(activation_vault, "kb") is not None

    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
    monkeypatch.setattr(
        recall_policy,
        "recall_policy_identity",
        lambda _root: ("mismatched-policy-version", "mismatched-access-fingerprint"),
    )

    def _forbidden_reprojection(*_args, **_kwargs):
        raise AssertionError(
            "activation must not synchronously reproject broad entries on a "
            "managed identity mismatch"
        )

    monkeypatch.setattr(freshness, "recall_checkpoint", _forbidden_reprojection)

    def _forbidden_walk(_root: Path):
        raise AssertionError("activation must not cold-walk the vault on the request thread")
        yield  # pragma: no cover - generator-shaped test double

    monkeypatch.setattr(vault_module, "walk_vault_md", _forbidden_walk)

    working_set_runtime.reset_caches_for_tests()
    packet = commands.op_activate_context(activation_vault, turn=TURN, include_timings=True)

    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "unavailable"}
    assert "timings" in packet
    assert working_set_runtime._PACKET_CACHE == {}


def test_managed_cold_start_never_constructs_the_recall_projection(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both the anchor index and the recall registry are cold — a genuine
    first-boot managed install. A prior round's fix (gating
    `require_live_recall` on `recall_is_live`) correctly stopped the
    MISMATCH case from reprojecting, but left this NEVER-LIVE case falling
    through `FreshnessSnapshot` to `freshness._cold_recall_projection_scope_snapshot`
    — a full directory walk of the KB scope, on the request thread, before
    `ensure_index` ever gets to say `index_warming`. Activation must not
    even ATTEMPT the projection here: no reprojection, no cold walk."""
    _seed_structure(vault)
    _seed_planning(vault)
    working_set_runtime.reset_caches_for_tests()

    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
    # A genuinely in-flight boot seed, not merely "never live": `managed_cold`
    # requires both (see `test_managed_never_live_and_not_seed_pending_falls_
    # through_to_cold_walk` for the other half of that distinction).
    monkeypatch.setattr(freshness, "recall_seed_pending", lambda *_a, **_k: True)
    scheduled: list[Path] = []
    monkeypatch.setattr(
        working_set_runtime, "_schedule_build", lambda root, **_kwargs: scheduled.append(root)
    )

    def _forbidden_cold_snapshot(*_a, **_k):
        raise AssertionError(
            "managed cold start must not construct the recall projection at all"
        )

    monkeypatch.setattr(freshness, "_cold_recall_projection_scope_snapshot", _forbidden_cold_snapshot)

    def _forbidden_walk(_root: Path):
        raise AssertionError("managed cold start must not cold-walk the vault")
        yield  # pragma: no cover - generator-shaped test double

    monkeypatch.setattr(vault_module, "walk_vault_md", _forbidden_walk)

    packet = commands.op_activate_context(vault, turn=TURN, include_timings=True)

    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "index_warming"}
    assert scheduled == [vault]
    # No snapshot was even constructed: the span never opened.
    assert "working_set.freshness" not in packet["timings"]["stages"]
    assert "working_set.readiness" in packet["timings"]["stages"]
    assert "timings" in packet
    assert working_set_runtime._PACKET_CACHE == {}


def test_managed_cold_start_abstains_warming_even_with_a_ready_anchor_index(
    activation_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recall registry has never gone live, but the anchor index happens
    to be pre-built (warm, via the `activation_vault` fixture's own
    `.rebuild()`): activation must still abstain `index_warming` rather than
    serve lexical/role evidence against a registry it cannot prove current —
    the READY-but-cold branch `ensure_index`'s own state alone cannot cover."""
    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
    monkeypatch.setattr(freshness, "recall_seed_pending", lambda *_a, **_k: True)
    scheduled: list[Path] = []
    monkeypatch.setattr(
        working_set_runtime, "_schedule_build", lambda root, **_kwargs: scheduled.append(root)
    )

    def _forbidden_cold_snapshot(*_a, **_k):
        raise AssertionError(
            "managed cold start must not construct the recall projection at all"
        )

    monkeypatch.setattr(freshness, "_cold_recall_projection_scope_snapshot", _forbidden_cold_snapshot)

    def _forbidden_walk(_root: Path):
        raise AssertionError("managed cold start must not cold-walk the vault")
        yield  # pragma: no cover - generator-shaped test double

    monkeypatch.setattr(vault_module, "walk_vault_md", _forbidden_walk)

    lexical_calls: list[str] = []
    monkeypatch.setattr(
        working_set_runtime,
        "lexical_evidence",
        lambda *a, **k: (lexical_calls.append("lexical_evidence"), ([], "available"))[1],
    )

    packet = commands.op_activate_context(activation_vault, turn=TURN, include_timings=True)

    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "index_warming"}
    # The anchor index was already warm: no background build was scheduled.
    assert scheduled == []
    assert lexical_calls == []
    assert "working_set.readiness" in packet["timings"]["stages"]
    assert "working_set.freshness" not in packet["timings"]["stages"]
    assert "timings" in packet
    assert working_set_runtime._PACKET_CACHE == {}


def test_managed_never_live_and_not_seed_pending_falls_through_to_cold_walk(
    activation_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`managed_cold` requires an ACTIVELY PENDING seed, not merely "never
    live" — a managed runtime whose recall registry has never gone live but
    for which nothing is currently seeding it (no watcher running here, so
    `recall_seed_pending` is `False` by construction) must not abstain
    `index_warming` forever. It instead falls through to the same
    offline-style cold-walk fallback an unmanaged runtime always used, and
    serves normally rather than hanging on a seed that will never arrive."""
    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
    assert freshness.recall_seed_pending(activation_vault, "kb") is False

    packet = commands.op_activate_context(activation_vault, turn=TURN, include_timings=True)

    assert packet["abstained"] is False
    assert packet["units"]
    assert "working_set.freshness" in packet["timings"]["stages"]


def test_offline_activation_is_unchanged_by_the_require_live_recall_rule(
    activation_vault: Path,
) -> None:
    """Unmanaged (offline) activation never sets `require_live_recall`, so an
    unmanaged caller still resolves normally even with no live registry."""
    packet = commands.op_activate_context(activation_vault, turn=TURN)

    assert packet["abstained"] is False
    assert packet["units"]


def _assert_generic_freshness_failure_abstains_immediately(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shared body for the unmanaged and managed-live variants below.

    A generic (non-`RetrievalIndexWarming`) exception constructing or
    reading the freshness snapshot must abstain `unavailable` right there
    instead of continuing in a half-state: freshness that could not be
    established grants nothing, and continuing would either re-attempt the
    same unbounded work downstream (lexical's `recall_checkpoint`,
    `_units_lane`'s own fallback snapshot) or silently run without it.
    """
    init_calls: list[int] = []
    real_init = find_module.FreshnessSnapshot.__init__

    def _counting_init(self, *args, **kwargs):
        init_calls.append(1)
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(find_module.FreshnessSnapshot, "__init__", _counting_init)

    def _raise_generic(*_a, **_k):
        raise RuntimeError("simulated freshness failure")

    monkeypatch.setattr(find_module.FreshnessSnapshot, "projection_key", _raise_generic)

    lexical_calls: list[str] = []
    monkeypatch.setattr(
        working_set_runtime,
        "lexical_evidence",
        lambda *a, **k: (lexical_calls.append("lexical_evidence"), ([], "available"))[1],
    )
    lane_calls: list[str] = []
    monkeypatch.setattr(
        working_set, "run_lanes", lambda *a, **k: (lane_calls.append("run_lanes"), ((), ()))[1]
    )

    working_set_runtime.reset_caches_for_tests()
    packet = commands.op_activate_context(vault, turn=TURN, include_timings=True)

    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "unavailable"}
    assert len(init_calls) == 1, "exactly one snapshot must be constructed"
    assert lexical_calls == []
    assert lane_calls == []
    # Not a budget cause: no request_budget block.
    assert "request_budget" not in packet
    assert "timings" in packet
    assert working_set_runtime._PACKET_CACHE == {}


def test_a_generic_freshness_failure_abstains_immediately_unmanaged(
    activation_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unmanaged/offline variant."""
    _assert_generic_freshness_failure_abstains_immediately(activation_vault, monkeypatch)


def test_a_generic_freshness_failure_abstains_immediately_managed_live(
    activation_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Managed, with an ESTABLISHED live recall registry
    (`require_live_recall=True`, distinct from the separate `managed_cold`
    path which skips snapshot construction entirely): the same guarantee
    holds regardless of which `require_live_recall` branch a request takes."""
    _seed_live_recall(activation_vault)
    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
    _assert_generic_freshness_failure_abstains_immediately(activation_vault, monkeypatch)


# --------------------------------------------------------------------------- #
# T3 — the existing request budget is honoured
# --------------------------------------------------------------------------- #


def test_activation_returns_promptly_on_an_already_expired_budget(
    activation_vault: Path, bind_budget, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `activation_vault` is seeded so TURN resolves normally (proven by
    # `test_a_normal_budget_leaves_activation_unchanged` below); the only
    # variable here is the expired budget.
    lane_calls: list[str] = []
    monkeypatch.setattr(
        working_set,
        "run_lanes",
        lambda *a, **k: lane_calls.append("run_lanes") or ((), ()),
    )
    state_calls: list[str] = []
    monkeypatch.setattr(
        working_set_state,
        "current_state_for",
        lambda *a, **k: state_calls.append("current_state_for") or (),
    )

    working_set_runtime.reset_caches_for_tests()
    budget = bind_budget(0.0)

    packet = commands.op_activate_context(activation_vault, turn=TURN, include_timings=True)

    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "unavailable"}
    assert lane_calls == []
    assert state_calls == []
    assert "timings" in packet
    # An already-expired budget fails at the FIRST boundary checked
    # (`working_set.freshness`) rather than a generic catch-all name — see
    # the T3b tests below for the other boundaries.
    assert budget.skipped == ["working_set.freshness"]
    assert working_set_runtime._PACKET_CACHE == {}
    # m2: a budget-caused abstention carries the existing advisory `budget`
    # block under `request_budget` (not the packet's own pre-existing
    # `budget` key, which already means the character budget).
    assert packet["request_budget"]["skipped"] == ["working_set.freshness"]
    assert packet["request_budget"]["applied"] is True
    assert packet["budget"] == {"limit_chars": 4000, "used_chars": 0}


def test_a_normal_budget_leaves_activation_unchanged(
    activation_vault: Path, bind_budget
) -> None:
    budget = bind_budget(50.0)

    packet = commands.op_activate_context(activation_vault, turn=TURN)

    assert packet["abstained"] is False
    assert packet["units"]
    assert budget.skipped == []
    assert "request_budget" not in packet


def test_an_unrelated_abstention_does_not_claim_a_borrowed_budget_omission(
    activation_vault: Path, bind_budget, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prior omission must not turn a later freshness error into a budget cause."""
    budget = bind_budget(50.0)
    budget.note_skipped("working_set.conversation")

    _assert_generic_freshness_failure_abstains_immediately(activation_vault, monkeypatch)

    assert request_budget.current() is budget


@pytest.mark.parametrize("headroom", [0.05, 1.2])
def test_relevant_due_advice_respects_the_fixed_request_deadline(
    activation_vault: Path, monkeypatch: pytest.MonkeyPatch, headroom: float,
) -> None:
    """Optional advice needs headroom and must withdraw after a late source read."""
    import datetime as dt

    from exomem import due_state
    from exomem.governance import egress

    page = "Knowledge Base/Products/Cargo Sled.md"
    target = activation_vault / page
    target.write_text(
        target.read_text(encoding="utf-8")
        + "\n## Prediction\n\n- id: capacity-check\n- check_by: "
        + (dt.date.today() - dt.timedelta(days=1)).isoformat()
        + "\n\nThe runners withstand the planned load.\n",
        encoding="utf-8",
    )
    due_state.reconcile(activation_vault)
    lexstore.ensure_fresh(activation_vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(activation_vault).rebuild()
    baseline = commands.op_activate_context(activation_vault, turn=TURN, anchor=page)
    assert baseline["abstained"] is False and baseline["relevant_due_refs"]
    # The late read below must happen: drop the parse the baseline left behind.
    due_state.reset_serve_cache()

    clock = [time.monotonic()]
    budget = request_budget.RequestBudget(seconds=50, entry=clock[0] - 47)
    deadline = budget.deadline
    guard = egress.guard_working_set
    read = vault_module.read_bytes_without_pinning
    projection = due_state.state_path(activation_vault)
    projection_reads: list[float] = []

    def slow_guard(*args, **kwargs):
        packet = guard(*args, **kwargs)
        pause = budget.remaining() - headroom
        assert pause > 0, "the completed guard must leave the intended headroom"
        clock[0] += pause
        return packet

    def slow_projection(path, *args, **kwargs):
        if path == projection:
            projection_reads.append(budget.remaining())
            clock[0] += budget.remaining() + 0.03
        return read(path, *args, **kwargs)

    monkeypatch.setattr(egress, "guard_working_set", slow_guard)
    monkeypatch.setattr(vault_module, "read_bytes_without_pinning", slow_projection)
    # Charge only injected stage delays; runner load must not erase the chosen headroom.
    monkeypatch.setattr(request_budget, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    token = request_budget.set_current(budget)
    try:
        packet = commands.op_activate_context(activation_vault, turn=TURN, anchor=page)
        returned = request_budget.time.monotonic()
        assert request_budget.current() is budget
    finally:
        request_budget.reset_current(token)

    assert budget.deadline == deadline
    assert packet["abstained"] is False
    assert packet["anchors"] == baseline["anchors"]
    assert packet["units"] == baseline["units"]
    assert packet.get("current_state") == baseline.get("current_state")
    assert "relevant_due_refs" not in packet
    assert packet["request_budget"]["applied"] is True
    assert packet["request_budget"]["skipped"] == budget.skipped
    assert packet["request_budget"]["truncated"] == budget.truncated
    if headroom < request_budget.ACTIVATION_STAGE_RESERVE_SECONDS:
        assert projection_reads == []
        assert budget.skipped == ["working_set.relevant_due"]
        assert budget.truncated == []
    else:
        assert projection_reads
        assert returned > deadline
        assert budget.skipped == []
        assert budget.truncated == ["working_set.relevant_due"]


# --------------------------------------------------------------------------- #
# T3b — every stage boundary honours the budget, not only entry
#
# An entry-only check cannot fire for a request whose client gave up while
# the server kept working: the live trial that motivated this lane was a
# request that took ~296 s against a fresh 50 s
# `request_budget.MCP_REQUEST_BUDGET_SECONDS` origin budget. A `_CountdownBudget`
# test double drives `remaining()` deterministically instead of sleeping: it
# reports "healthy" for the first `healthy_calls` reads (one read per stage
# boundary `working_set.budget_exhausted` checks, in call order) and
# "exhausted" on every read after, so a request's budget can be made to run
# out at an exact, reproducible stage.
# --------------------------------------------------------------------------- #


class _CountdownBudget(request_budget.RequestBudget):
    """A REAL `request_budget.RequestBudget` whose `remaining()` is driven by
    a call counter instead of the wall clock, so a request's budget can be
    made to run out at an exact, reproducible stage without sleeping.
    Healthy (a reserve comfortably above `ACTIVATION_STAGE_RESERVE_SECONDS`)
    for the first `healthy_calls` reads of `remaining()`, exhausted (0.0) on
    every read after. Every other method — `can_afford`, `note_skipped`,
    `.skipped` — is the unmodified `RequestBudget` implementation (inherited,
    not duck-typed), so `working_set.budget_exhausted`'s
    `can_afford(ACTIVATION_STAGE_RESERVE_SECONDS)` gate exercises real
    behaviour."""

    _HEALTHY_SECONDS = 50.0

    def __init__(self, healthy_calls: int) -> None:
        super().__init__(seconds=self._HEALTHY_SECONDS)
        self._calls = 0
        self._healthy_calls = healthy_calls

    def remaining(self) -> float:
        self._calls += 1
        return self._HEALTHY_SECONDS if self._calls <= self._healthy_calls else 0.0


@pytest.fixture
def countdown_budget():
    tokens = []

    def _bind(healthy_calls: int) -> _CountdownBudget:
        budget = _CountdownBudget(healthy_calls)
        tokens.append(request_budget.set_current(budget))
        return budget

    try:
        yield _bind
    finally:
        for token in reversed(tokens):
            request_budget.reset_current(token)


# Stage-boundary call order for a normal, resolving TURN against
# `activation_vault`: freshness(1) readiness(2) lexical(3) release(4)
# semantic(5) resolve(6) roles(7) current_state(8), then one call per
# selected role lane (8+N), then packet-build, then guard. Confirmed against
# `activation_vault` + TURN by inspecting `packet["timings"]["stages"]`
# insertion order and `packet["roles"]`: six lanes run in the order
# identity, constraints, resources, current_state, active_plans, location.
_CALLS_THROUGH_RESOLVE = 6


def test_budget_exhausted_after_freshness_skips_readiness_onward(
    activation_vault: Path, countdown_budget, monkeypatch: pytest.MonkeyPatch
) -> None:
    readiness_calls: list[str] = []
    monkeypatch.setattr(
        working_set_runtime,
        "ensure_index",
        lambda *a, **k: (readiness_calls.append("ensure_index"), (working_set_runtime.UNAVAILABLE, None, False))[1],
    )
    lexical_calls: list[str] = []
    monkeypatch.setattr(
        working_set_runtime,
        "lexical_evidence",
        lambda *a, **k: (lexical_calls.append("lexical_evidence"), ([], "available"))[1],
    )
    lane_calls: list[str] = []
    monkeypatch.setattr(
        working_set, "run_lanes", lambda *a, **k: (lane_calls.append("run_lanes"), ((), ()))[1]
    )

    working_set_runtime.reset_caches_for_tests()
    budget = countdown_budget(1)  # healthy through the freshness check only

    packet = commands.op_activate_context(activation_vault, turn=TURN, include_timings=True)

    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "unavailable"}
    # Freshness itself ran (its own boundary check was still healthy).
    assert "working_set.freshness" in packet["timings"]["stages"]
    # Nothing after the freshness boundary started.
    assert readiness_calls == []
    assert lexical_calls == []
    assert lane_calls == []
    assert "working_set.readiness" not in packet["timings"]["stages"]
    assert budget.skipped == ["working_set.readiness"]
    assert "timings" in packet
    assert working_set_runtime._PACKET_CACHE == {}
    assert packet["request_budget"]["skipped"] == ["working_set.readiness"]


def test_budget_exhausted_after_resolve_skips_roles_onward(
    activation_vault: Path, countdown_budget, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import context_roles
    from exomem.governance import egress as egress_module

    role_calls: list[str] = []
    monkeypatch.setattr(
        context_roles,
        "select_roles",
        lambda *a, **k: (role_calls.append("select_roles"), ())[1],
    )
    state_calls: list[str] = []
    monkeypatch.setattr(
        working_set_state,
        "current_state_for",
        lambda *a, **k: (state_calls.append("current_state_for"), ())[1],
    )
    lane_calls: list[str] = []
    monkeypatch.setattr(
        working_set, "run_lanes", lambda *a, **k: (lane_calls.append("run_lanes"), ((), ()))[1]
    )
    guard_calls: list[str] = []
    original_guard = egress_module.guard_working_set

    def _spy_guard(*args, **kwargs):
        guard_calls.append("guard_working_set")
        return original_guard(*args, **kwargs)

    monkeypatch.setattr(egress_module, "guard_working_set", _spy_guard)

    working_set_runtime.reset_caches_for_tests()
    budget = countdown_budget(_CALLS_THROUGH_RESOLVE)

    packet = commands.op_activate_context(activation_vault, turn=TURN, include_timings=True)

    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "unavailable"}
    stages = packet["timings"]["stages"]
    # Everything through resolve ran.
    assert {
        "working_set.freshness",
        "working_set.readiness",
        "working_set.lexical",
        "working_set.release",
        "working_set.semantic",
        "working_set.resolve",
    } <= set(stages)
    # Nothing from roles onward started.
    assert role_calls == []
    assert state_calls == []
    assert lane_calls == []
    assert "working_set.roles" not in stages
    assert "working_set.current_state" not in stages
    # The guard itself still runs, exactly once, on the already-abstained
    # (empty) packet compile_packet handed back — "a packet is either
    # guarded and returned, or not returned at all" holds even for an empty
    # one. What it does NOT do is cost a second budget check: the guard
    # boundary is only checked when there is real content to weigh it
    # against, so it is never independently re-charged here.
    assert guard_calls == ["guard_working_set"]
    assert "working_set.guard" in stages
    assert budget.skipped == ["working_set.roles"]
    assert "timings" in packet
    assert working_set_runtime._PACKET_CACHE == {}
    # m1+m2: the request_budget block is attached even when the skip is
    # caught deep inside `compile_packet` (via `working_set_runtime.serve`'s
    # `except working_set.BudgetExhausted`), not only for the commands.py-
    # level boundaries.
    assert packet["request_budget"]["skipped"] == ["working_set.roles"]


def test_budget_exhausted_between_two_role_lanes_discards_the_first_lanes_work(
    activation_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import egress as egress_module

    guard_calls: list[str] = []
    original_guard = egress_module.guard_working_set

    def _spy_guard(*args, **kwargs):
        guard_calls.append("guard_working_set")
        return original_guard(*args, **kwargs)

    monkeypatch.setattr(egress_module, "guard_working_set", _spy_guard)

    working_set_runtime.reset_caches_for_tests()
    budget = request_budget.RequestBudget(seconds=50)
    original_lane = working_set._lane

    def expire_after_identity(root, role, **kwargs):
        result = original_lane(root, role, **kwargs)
        if role.id == "identity":
            budget.deadline = time.monotonic() - 1
        return result

    monkeypatch.setattr(working_set, "_lane", expire_after_identity)
    token = request_budget.set_current(budget)
    try:
        packet = commands.op_activate_context(activation_vault, turn=TURN, include_timings=True)
    finally:
        request_budget.reset_current(token)

    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "unavailable"}
    stages = packet["timings"]["stages"]
    # Resolution, role selection and current state all ran, and the FIRST
    # selected lane (`identity`, confirmed by the probe above) ran too.
    assert {
        "working_set.resolve",
        "working_set.roles",
        "working_set.current_state",
        "working_set.lanes.0",
    } <= set(stages)
    # There is no partial packet: the second lane never ran, and the whole
    # request abstains rather than disclosing what the first lane found —
    # the discarded first lane's items never reach `packet["units"]`.
    assert "working_set.lanes.1" not in stages
    assert "working_set.budget" not in stages
    # The guard still runs once, on the empty abstained packet, without
    # costing a second budget check (see the "after resolve" test above for
    # why).
    assert guard_calls == ["guard_working_set"]
    assert packet["units"] == []
    assert budget.skipped == ["working_set.lanes.1"]
    assert packet["request_budget"]["skipped"] == ["working_set.lanes.1"]
    assert "timings" in packet
    assert working_set_runtime._PACKET_CACHE == {}


def test_the_stage_reserve_blocks_a_stage_with_positive_but_insufficient_remaining(
    activation_vault: Path, bind_budget
) -> None:
    """M3: `working_set.budget_exhausted` gates on
    `can_afford(ACTIVATION_STAGE_RESERVE_SECONDS)` (1.0s), not `remaining() > 0`.
    0.5s of remaining budget is positive — the OLD check would have started
    the freshness stage — but is less than the reserve, so the new,
    reserve-gated check must still treat it as exhausted."""
    assert request_budget.ACTIVATION_STAGE_RESERVE_SECONDS == 1.0
    budget = bind_budget(request_budget.ACTIVATION_STAGE_RESERVE_SECONDS - 0.5)  # 0.5s, real clock

    packet = commands.op_activate_context(activation_vault, turn=TURN, include_timings=True)

    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "unavailable"}
    assert budget.skipped == ["working_set.freshness"]
    assert "working_set.freshness" not in (packet["timings"]["stages"])
    assert packet["request_budget"]["skipped"] == ["working_set.freshness"]


def test_a_budget_skip_inside_compile_packet_logs_info_without_a_traceback(
    activation_vault: Path, countdown_budget, caplog: pytest.LogCaptureFixture
) -> None:
    """m1: a deliberate budget skip caught by `working_set_runtime.serve`'s
    `except working_set.BudgetExhausted` logs at INFO with no traceback —
    distinct from the broad `except Exception` handler's WARNING + full
    stack trace for a genuine compilation bug."""
    caplog.set_level(logging.INFO, logger="exomem.working_set_runtime")
    working_set_runtime.reset_caches_for_tests()
    countdown_budget(_CALLS_THROUGH_RESOLVE)  # exhausts at "working_set.roles"

    packet = commands.op_activate_context(activation_vault, turn=TURN, include_timings=True)

    assert packet["abstained"] is True
    skip_records = [
        record
        for record in caplog.records
        if record.name == "exomem.working_set_runtime" and "working_set.roles" in record.getMessage()
    ]
    assert len(skip_records) == 1
    assert skip_records[0].levelname == "INFO"
    assert skip_records[0].exc_info is None
    # And the broad "compilation failed" WARNING was NOT also emitted for
    # this same deliberate, expected skip.
    assert not any(
        record.name == "exomem.working_set_runtime" and record.levelname == "WARNING"
        for record in caplog.records
    )


# --------------------------------------------------------------------------- #
# M2 — activation binds its own budget on doors with no MCP-dispatch budget
# --------------------------------------------------------------------------- #


def test_a_pre_bound_budget_is_used_unchanged_not_replaced(
    activation_vault: Path, bind_budget
) -> None:
    budget = bind_budget(50.0)

    packet = commands.op_activate_context(activation_vault, turn=TURN)

    assert packet["abstained"] is False
    assert packet["units"]
    # Identity, not just equality: the wrapper must not have swapped in its
    # own door-budget and swapped back an equivalent-looking one.
    assert request_budget.current() is budget
    assert budget.skipped == []


def test_unmanaged_runtime_never_binds_a_door_budget(
    activation_vault: Path, budget_free, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MINOR fix: `ACTIVATION_DOOR_BUDGET_SECONDS` binds only for a
    MANAGED runtime. An unmanaged process (the default here — no watcher, no
    background convergence a later request could benefit from) must be left
    running unbounded rather than having a budget tuned for the managed
    doors' shipped hook timeouts turn a legitimately slower one-off cold walk
    into a hard failure with nothing left to retry against."""
    assert readiness.runtime_managed() is False
    assert request_budget.current() is None

    bound_calls: list[float] = []
    real_set_current = request_budget.set_current

    def _spy_set_current(budget):
        if budget is not None:
            bound_calls.append(budget.seconds)
        return real_set_current(budget)

    monkeypatch.setattr(request_budget, "set_current", _spy_set_current)

    packet = commands.op_activate_context(activation_vault, turn=TURN)

    assert packet["abstained"] is False
    assert packet["units"]
    assert bound_calls == []
    assert request_budget.current() is None


def test_no_bound_budget_still_gates_a_boundary_via_the_door_budget(
    activation_vault: Path, budget_free, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The REST/CLI shape: no MCP budget is bound. `op_activate_context`
    binds its own `ACTIVATION_DOOR_BUDGET_SECONDS` budget for the call, but
    only for a MANAGED runtime (an unmanaged process has no background
    convergence a later request could benefit from, so it is left
    unbounded). A controllable clock on `RequestBudget.remaining` (not the
    global `time.monotonic`, which every other subsystem in the process also
    reads) proves the door budget actually gates a mid-call boundary rather
    than merely existing unused."""
    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
    assert request_budget.current() is None

    real_remaining = request_budget.RequestBudget.remaining
    calls = {"n": 0}

    def _controlled_remaining(self):
        calls["n"] += 1
        # Healthy through the first two boundary reads (freshness,
        # readiness) — proving those stages' own real work still ran — then
        # exhausted from the third read (lexical) onward, past the door
        # budget's own real deadline.
        if calls["n"] <= 2:
            return real_remaining(self)
        return 0.0

    monkeypatch.setattr(request_budget.RequestBudget, "remaining", _controlled_remaining)

    lexical_calls: list[str] = []
    monkeypatch.setattr(
        working_set_runtime,
        "lexical_evidence",
        lambda *a, **k: (lexical_calls.append("lexical_evidence"), ([], "available"))[1],
    )

    packet = commands.op_activate_context(activation_vault, turn=TURN, include_timings=True)

    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "unavailable"}
    assert lexical_calls == []
    assert "working_set.freshness" in packet["timings"]["stages"]
    assert "working_set.readiness" in packet["timings"]["stages"]
    assert packet["request_budget"]["skipped"] == ["working_set.lexical"]
    assert "timings" in packet
    # Restored after a normal return: no leak onto a later call.
    assert request_budget.current() is None


def test_the_door_bound_budget_is_restored_after_an_exception(
    activation_vault: Path, budget_free, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The anchor-override refusal path raises `ValueError` from INSIDE
    `_op_activate_context_body`; the public wrapper's `try/finally` must
    still clean up the door-bound budget it bound. Managed, so the wrapper
    actually binds a door budget for this call to clean up in the first
    place."""
    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
    assert request_budget.current() is None

    with pytest.raises(ValueError):
        commands.op_activate_context(
            activation_vault, turn=TURN, anchor="no-such-anchor-ref-at-all"
        )

    assert request_budget.current() is None


# --------------------------------------------------------------------------- #
# D3 — a turn that names no anchor is carried by one dominant recall hit
# --------------------------------------------------------------------------- #


@pytest.fixture
def carried_vault(activation_vault: Path) -> Path:
    """The activation vault plus a research note that is NOT an anchor.

    Same pattern as `activation_vault` itself — `lexstore.ensure_fresh`
    before the index rebuild — because a carried packet is served entirely
    off the maintained lexical catalogue, and an unproven one carries
    nothing.
    """
    from test_working_set_carry import _seed_carry_pages

    _seed_carry_pages(activation_vault)
    lexstore.ensure_fresh(activation_vault)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(activation_vault).reset()
    working_set_index.WorkingSetIndex(activation_vault).rebuild()
    return activation_vault


def test_a_turn_naming_no_anchor_is_served_that_notes_units(
    carried_vault: Path, budget_free
) -> None:
    """The whole door, not just the compiler: freshness, readiness, the
    lexical lane, the release plane and the guard all run, and the turn that
    used to come back empty comes back with the research note's own units.
    """
    from test_working_set_carry import CARRY_PAGE, CARRY_TURN

    packet = commands.op_activate_context(
        carried_vault, turn=CARRY_TURN, include_timings=True
    )

    assert packet["abstained"] is False, packet.get("abstention")
    assert packet["generation"]["carried_by"] == "retrieval"
    assert len(packet["anchors"]) == 1
    assert packet["anchors"][0]["status"] == "resolved"  # Named by its own title (the 2026-10-05 ruling).
    assert packet["anchors"][0]["kind"] == "page"
    assert packet["anchors"][0]["path"] == CARRY_PAGE
    assert packet["units"], packet
    assert {
        str((unit.get("provenance") or {}).get("path") or "") for unit in packet["units"]
    } == {CARRY_PAGE}
    assert "working_set.carry" in packet["timings"]["stages"]
    # Was "no token" (U3); R-P2: "continue" resumes a carried page only if the
    # token names it.
    assert working_set_runtime.decode_continuity(packet["continuity"])["refs"] == [CARRY_PAGE]


@pytest.mark.parametrize("fail_role", [False, True])
def test_managed_activation_selects_around_encoding_and_restores_the_caller_scope(
    carried_vault: Path, warm_managed_cell, monkeypatch: pytest.MonkeyPatch, budget_free,
    fail_role: bool,
) -> None:
    """Carry and role queries share one view, and neither encoding nor scope exit retains it."""
    from dataclasses import replace

    from test_working_set_carry import CARRY_PAGE, CARRY_TURN
    from test_working_set_index_vectors import _Encoder

    from exomem import embeddings, ranking_config
    from exomem.governance import egress

    warm_managed_cell(carried_vault)
    encoder = _Encoder()
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "activation_fingerprint", lambda: encoder.fingerprint)
    monkeypatch.setattr(embeddings, "embed_activation_passages_if_loaded", encoder.passages)
    monkeypatch.setattr(
        ranking_config, "DEFAULT_RANKING",
        replace(ranking_config.DEFAULT_RANKING, working_set_semantic_min_population=1),
    )
    index = working_set_index.WorkingSetIndex(carried_vault)
    try:
        index.rebuild()
    finally:
        index.close()

    lexical_views = []
    compiled_views = []
    stages = []
    lexical_query = working_set_runtime._lexical_query
    carry = working_set_runtime.carry_candidates
    units_lane = working_set._units_lane
    label_origins = working_set._label_origins

    def lexical(*args, **kwargs):
        view = lexstore._admitted_catalog_view(carried_vault)
        assert view is not None
        lexical_views.append(view)
        return lexical_query(*args, **kwargs)

    def encode(text):
        assert lexical_views and lexical_views[0].connection is None
        assert lexstore._admitted_catalog_view(carried_vault) is None
        stages.append("encode")
        return encoder.vector(text)

    def observe_carry(*args, **kwargs):
        view = lexstore._admitted_catalog_view(carried_vault)
        assert view is not None and view.connection is not None
        compiled_views.append(view)
        stages.append("carry")
        return carry(*args, **kwargs)

    def observe_units(*args, **kwargs):
        view = lexstore._admitted_catalog_view(carried_vault)
        assert compiled_views and view is compiled_views[0]
        assert view.connection is compiled_views[0].connection
        stages.append("role")
        if fail_role:
            raise egress.ReaderViewUnavailable("role interrupted")
        return units_lane(*args, **kwargs)

    def label(*args, **kwargs):
        assert lexstore._admitted_catalog_view(carried_vault) is None
        assert compiled_views[0].connection is None
        stages.append("origins")
        return label_origins(*args, **kwargs)

    monkeypatch.setattr(working_set_runtime, "_lexical_query", lexical)
    monkeypatch.setattr(embeddings, "embed_activation_query_if_loaded", encode)
    monkeypatch.setattr(working_set_runtime, "carry_candidates", observe_carry)
    monkeypatch.setattr(working_set, "_units_lane", observe_units)
    monkeypatch.setattr(working_set, "_label_origins", label)
    with lexstore.admitted_catalog_scope(carried_vault):
        caller = lexstore._ADMITTED_CATALOG.get()
        packet = commands.op_activate_context(carried_vault, turn=CARRY_TURN)
        assert lexstore._ADMITTED_CATALOG.get() is caller

    assert stages[0] == "encode" and "carry" in stages and "role" in stages
    assert all(view.connection is None for view in lexical_views + compiled_views)
    assert lexstore._admitted_catalog_view(carried_vault) is None
    if fail_role:
        assert packet["abstention"] == {"reason": "unavailable"}
    else:
        assert packet["abstained"] is False
        assert stages[-1] == "origins"
        assert {unit["provenance"]["path"] for unit in packet["units"]} == {CARRY_PAGE}


def test_a_named_anchor_still_wins_through_the_whole_door(
    carried_vault: Path, budget_free
) -> None:
    """The named anchor wins; a page the turn also names is carried BESIDE it.

    Orchestrator ruling (recall-breadth, item 2): the compiler surfaces
    everything the turn names. The carried page is labelled, never a peer
    anchor, and never displaces or resolves in place of the named one."""
    from test_working_set_carry import CARRY_PAGE

    packet = commands.op_activate_context(
        carried_vault,
        turn=f"{TURN} and what did we decide about the quillon vantry window?",
    )

    assert packet["abstained"] is False, packet.get("abstention")
    assert "carried_by" not in packet["generation"]
    anchors = packet["anchors"]
    first = anchors[0]
    assert first["status"] == "resolved"
    assert str(first.get("path") or first.get("ref")) != CARRY_PAGE
    assert first.get("evidence")
    # The page is named by its own title, so it resolves beside the anchor
    # (the 2026-10-05 ruling); it is still a carried page, never in its place.
    assert [a["status"] for a in anchors].count("resolved") == 2
    carried = [a for a in anchors if str(a.get("path") or "") == CARRY_PAGE]
    assert [(a["status"], a["kind"]) for a in carried] == [("resolved", "page")]
    assert packet["generation"]["also_carried"] == "retrieval"
    assert not packet.get("ambiguity")


def test_first_activation_after_an_edit_serves_current_units_before_its_own_refresh(
    activation_vault: Path, monkeypatch: pytest.MonkeyPatch, budget_free
) -> None:
    """A request's refresh must not withdraw the current units it just compiled."""
    from test_governance_egress import _external, write_rule, write_scope

    from exomem.governance import egress
    from exomem.governance.principal import request_scope

    page = "Knowledge Base/Products/Cargo Sled.md"
    hidden = activation_vault / "Knowledge Base/Notes/Patterns/withheld-refresh-unit.md"
    hidden.parent.mkdir(parents=True, exist_ok=True)
    hidden.write_text(
        "---\ntype: note\ntags: [hub]\nstatus: active\n---\n\n"
        "# WITHHELDREFRESH\n\nConfidential refresh payload.\n", encoding="utf-8",
    )
    target = activation_vault / page
    target.write_text(target.read_text(encoding="utf-8") + "\n## Related\n\nRelated: [[withheld-refresh-unit]]\n", encoding="utf-8")
    write_scope(activation_vault)
    write_rule(activation_vault, ceiling=0)
    _seed_live_recall(activation_vault)
    stamp = working_set_runtime._key_text(
        find_module.FreshnessSnapshot(activation_vault).projection_key("kb")
    )
    index = working_set_index.WorkingSetIndex(activation_vault)
    index.update(freshness_stamp=stamp)
    old_token = index.token()
    commands.op_edit(
        activation_vault, path=page, why="Correct the operating constraint",
        old_string="Never exceed 400 kg.", new_string="Never exceed 275 kg.",
    )
    lexstore.ensure_fresh(activation_vault)
    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
    started = threading.Event()
    publish = threading.Event()
    finished = threading.Event()
    guarded = threading.Event()
    publication_after_guard: list[bool] = []
    workers: list[threading.Thread] = []

    class ControlledWorker(threading.Thread):
        def start(self):
            workers.append(self)
            super().start()
            assert started.wait(5), "the real refresh worker did not start"

        def run(self):
            started.set()
            try:
                if publish.wait(10):
                    super().run()
                    publication_after_guard.append(guarded.is_set())
            finally:
                finished.set()

    # Patch this module's namespace, never threading.Thread shared by other workers.
    monkeypatch.setattr(working_set_runtime, "threading", SimpleNamespace(Thread=ControlledWorker))
    real_compile = working_set.compile_packet
    compiled = []

    def compile_with_publication_window(*args, **kwargs):
        packet = real_compile(*args, **kwargs)
        compiled.append(packet)
        if started.is_set():
            # Before the repair, readiness dispatched this worker. Publish only
            # after readiness captured its evidence token and compilation ran.
            publish.set()
            assert finished.wait(10), "the real refresh did not publish"
        return packet

    real_guard = egress.guard_working_set

    def observe_guard(*args, **kwargs):
        packet = real_guard(*args, **kwargs)
        guarded.set()
        return packet

    monkeypatch.setattr(working_set, "compile_packet", compile_with_publication_window)
    monkeypatch.setattr(egress, "guard_working_set", observe_guard)
    try:
        with request_scope(_external()):
            first = commands.op_activate_context(activation_vault, turn=TURN)
        assert first["abstained"] is False, first.get("abstention")
        assert any(unit["text"] == "Never exceed 275 kg." for unit in first["units"]), first["units"]
        assert "WITHHELDREFRESH" not in str(first)
        assert "withheld-refresh-unit" not in str(first)
        assert "Confidential refresh payload." not in str(first)
        assert len(compiled) == 1, "the first response must not retry compilation"
        assert first["generation"].get("index_stale") is True
        assert guarded.is_set() and started.is_set() and not finished.is_set()
        assert index.token() == old_token
    finally:
        publish.set()
        for worker in workers:
            worker.join(10)
            assert not worker.is_alive(), "the owned refresh worker did not finish"

    assert publication_after_guard == [True]
    assert index.token() != old_token
    with request_scope(_external()):
        current = commands.op_activate_context(activation_vault, turn=TURN)
    assert current["abstained"] is False
    assert "index_stale" not in current["generation"]
    assert any(unit["text"] == "Never exceed 275 kg." for unit in current["units"])


def test_an_independent_publication_still_withdraws_compiled_units_without_a_privacy_oracle(
    activation_vault: Path, monkeypatch: pytest.MonkeyPatch, budget_free
) -> None:
    """Deferral owns only this request's worker; another publisher still invalidates it."""
    from test_governance_egress import _external, write_rule, write_scope

    from exomem.governance.principal import request_scope

    page = "Knowledge Base/Products/Cargo Sled.md"
    hidden = activation_vault / "Knowledge Base/Notes/Patterns/withheld-refresh-unit.md"
    target = activation_vault / page
    target.write_text(target.read_text(encoding="utf-8") + "\n## Related\n\nRelated: [[withheld-refresh-unit]]\n", encoding="utf-8")
    write_scope(activation_vault)
    write_rule(activation_vault, ceiling=0)
    real_compile = working_set.compile_packet
    responses = []
    for private_present, old_limit, new_limit in ((False, 400, 275), (True, 275, 200)):
        if private_present:
            hidden.parent.mkdir(parents=True, exist_ok=True)
            hidden.write_text(
                "---\ntype: note\ntags: [hub]\nstatus: active\n---\n\n"
                "# WITHHELDREFRESH\n\nConfidential refresh payload.\n", encoding="utf-8",
            )
        _seed_live_recall(activation_vault)
        lexstore.ensure_fresh(activation_vault)
        stamp = working_set_runtime._key_text(
            find_module.FreshnessSnapshot(activation_vault).projection_key("kb")
        )
        index = working_set_index.WorkingSetIndex(activation_vault)
        index.update(freshness_stamp=stamp)
        before = index.token()
        publish = threading.Event()
        finished = threading.Event()
        errors: list[Exception] = []
        compiled = []

        def independent_publisher(
            publish=publish, finished=finished, errors=errors,
            old_limit=old_limit, new_limit=new_limit,
        ):
            try:
                if publish.wait(10):
                    commands.op_edit(
                        activation_vault, path=page, why="Correct the operating constraint",
                        old_string=f"Never exceed {old_limit} kg.",
                        new_string=f"Never exceed {new_limit} kg.",
                    )
                    working_set_index.WorkingSetIndex(activation_vault).update()
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()

        publisher = threading.Thread(target=independent_publisher, name="test-independent-publisher")
        publisher.start()

        def compile_before_independent_publication(
            *args, compiled=compiled, publish=publish, finished=finished, **kwargs
        ):
            packet = real_compile(*args, **kwargs)
            compiled.append(packet)
            publish.set()
            assert finished.wait(10), "the independent publisher did not finish"
            return packet

        with monkeypatch.context() as request:
            request.setattr(readiness, "runtime_managed", lambda: True)
            request.setattr(working_set, "compile_packet", compile_before_independent_publication)
            try:
                with request_scope(_external()):
                    packet = commands.op_activate_context(activation_vault, turn=TURN)
            finally:
                publish.set()
                publisher.join(10)
                assert not publisher.is_alive(), "the owned publisher did not finish"
        assert errors == []
        assert index.token() != before
        assert any(unit["text"] == f"Never exceed {old_limit} kg." for unit in compiled[0]["units"]), [unit["text"] for unit in compiled[0]["units"]]
        assert packet.get("abstention") == {"reason": "unavailable"}
        assert packet["generation"]["lexical_evidence"] == "stale"
        assert all(packet[section] == [] for section in (
            "anchors", "units", "pointers", "roles", "current_state", "missing", "ambiguity",
        ))
        # Continuity contains a fresh random thread, not evidence or diagnostics.
        packet.pop("continuity", None)
        responses.append(packet)

    assert responses[0] == responses[1]
    assert "WITHHELDREFRESH" not in str(responses)
    assert "withheld-refresh-unit" not in str(responses)


def test_nested_refresh_deferral_dispatches_once_after_an_exception_and_preserves_its_stamp(
    activation_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Queueing reserves no build, and scope exit preserves the single-flight owner's key."""
    index = working_set_index.WorkingSetIndex(activation_vault)
    workers: list[threading.Thread] = []

    class ObservedWorker(threading.Thread):
        def start(self):
            workers.append(self)
            super().start()

    monkeypatch.setattr(working_set_runtime, "threading", SimpleNamespace(Thread=ObservedWorker))
    original = ValueError("original activation failure")
    try:
        with pytest.raises(ValueError) as raised:
            with working_set_runtime.defer_index_refreshes():
                working_set_runtime._schedule_build(activation_vault, freshness_stamp="first")
                with working_set_runtime.defer_index_refreshes():
                    working_set_runtime._schedule_build(activation_vault / ".", freshness_stamp="later")
                assert activation_vault.absolute() not in working_set_runtime._BUILDS
                assert workers == []
                raise original
        assert raised.value is original
    finally:
        for worker in workers:
            worker.join(10)
            assert not worker.is_alive(), "the owned refresh worker did not finish"
    assert len(workers) == 1
    assert index.freshness_stamp() == "first"

    # A real readiness abstention must flush its queued warm, too.
    index.reset()
    monkeypatch.setattr(readiness, "runtime_managed", lambda: True)
    try:
        cold = commands.op_activate_context(activation_vault, turn=TURN)
        assert cold["abstention"] == {"reason": "index_warming"}
    finally:
        for worker in workers[1:]:
            worker.join(10)
            assert not worker.is_alive(), "the abstained request's refresh did not finish"
    assert len(workers) == 2 and index.anchors()

    def failed_dispatch(*args, **kwargs):
        raise RuntimeError("optional refresh could not start")

    with pytest.raises(ValueError) as raised:
        with working_set_runtime.defer_index_refreshes():
            working_set_runtime._schedule_build(activation_vault, freshness_stamp="unused")
            monkeypatch.setattr(working_set_runtime, "_schedule_build", failed_dispatch)
            raise original
    assert raised.value is original
