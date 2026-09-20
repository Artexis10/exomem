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
from pathlib import Path

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
    scheduled: list[Path] = []
    monkeypatch.setattr(working_set_runtime, "_schedule_build", scheduled.append)

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
    scheduled: list[Path] = []
    monkeypatch.setattr(working_set_runtime, "_schedule_build", scheduled.append)

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
_CALLS_THROUGH_FIRST_LANE = 9  # 8 pre-lane checks + the first lane's own check


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
    activation_vault: Path, countdown_budget, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem.governance import egress as egress_module

    guard_calls: list[str] = []
    original_guard = egress_module.guard_working_set

    def _spy_guard(*args, **kwargs):
        guard_calls.append("guard_working_set")
        return original_guard(*args, **kwargs)

    monkeypatch.setattr(egress_module, "guard_working_set", _spy_guard)

    working_set_runtime.reset_caches_for_tests()
    budget = countdown_budget(_CALLS_THROUGH_FIRST_LANE)

    packet = commands.op_activate_context(activation_vault, turn=TURN, include_timings=True)

    assert packet["abstained"] is True
    assert packet["abstention"] == {"reason": "unavailable"}
    stages = packet["timings"]["stages"]
    # Resolution, role selection and current state all ran, and the FIRST
    # selected lane (`identity`, confirmed by the probe above) ran too.
    assert {
        "working_set.resolve",
        "working_set.roles",
        "working_set.current_state",
        "working_set.lanes.identity",
    } <= set(stages)
    # There is no partial packet: the second lane never ran, and the whole
    # request abstains rather than disclosing what the first lane found —
    # the discarded first lane's items never reach `packet["units"]`.
    assert "working_set.lanes.constraints" not in stages
    assert "working_set.budget" not in stages
    # The guard still runs once, on the empty abstained packet, without
    # costing a second budget check (see the "after resolve" test above for
    # why).
    assert guard_calls == ["guard_working_set"]
    assert packet["units"] == []
    assert budget.skipped == ["working_set.lanes.constraints"]
    assert packet["request_budget"]["skipped"] == ["working_set.lanes.constraints"]
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


def test_no_bound_budget_still_gates_a_boundary_via_the_door_budget(
    activation_vault: Path, budget_free, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The REST/CLI shape: no MCP budget is bound. `op_activate_context`
    binds its own `ACTIVATION_DOOR_BUDGET_SECONDS` budget for the call. A
    controllable clock on `RequestBudget.remaining` (not the global
    `time.monotonic`, which every other subsystem in the process also reads)
    proves the door budget actually gates a mid-call boundary rather than
    merely existing unused."""
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
    activation_vault: Path, budget_free
) -> None:
    """The anchor-override refusal path raises `ValueError` from INSIDE
    `_op_activate_context_body`; the public wrapper's `try/finally` must
    still clean up the door-bound budget it bound."""
    assert request_budget.current() is None

    with pytest.raises(ValueError):
        commands.op_activate_context(
            activation_vault, turn=TURN, anchor="no-such-anchor-ref-at-all"
        )

    assert request_budget.current() is None
