"""The live-acceptance harness has to measure the product, not itself.

A first live run of `scripts/live_write_acceptance.py` failed on numbers the
product does not actually produce. Three harness defects, each measured on a
disposable 3,789-page copy of a real vault, and each pinned here:

* The warm-up opened and closed the readiness window without ever publishing
  the catalogue proof, so managed recall was never admitted. Every write then
  paid an unwarmed corpus-context build and every following read answered
  `warming`. Same corpus, same host, ten samples each: the harness warm-up gave
  an edit median of 2,623ms with a 27,662ms first sample and 20/20 `warming`
  reads; the product's own warm-up (`warmup.warm_retrieval_catalog` under
  `EXOMEM_EAGER_BOOT`) gave 377ms, a 6,613ms first sample and 20/20 `exact`
  reads. The gate script next door had already been bitten by exactly this and
  had the fix; the harness carried a second, abbreviated copy. So the two now
  share one object, because two copies of a warm-up is how one of them drifts.

* Convergence drained in passes of a hard-coded 32 and only looked at the wall
  clock between them. A single component dispatch on that corpus runs 18-88
  seconds, so one pass ran 99 seconds against a 30-second bound and 315 seconds
  against the design's 300. The pass size is now the scheduler's own
  `progress_limit()` for this cell's mode, and both the mode and the limit are
  reported -- a convergence number measured at 32 and one measured at 16 are
  not comparable, and nothing else in the report would say which it was.

* A run that failed to converge said only "did not converge". That is the one
  sentence that cannot distinguish the outcomes an operator has to act on
  differently: a deep backlog that is draining and needs a longer window, a
  drain that is stuck and needs someone, and a store whose counters could not
  be read at all. Convergence now reports what it saw.

And one defect this file's first version introduced and a reviewer caught:
counting every attempted-and-coded row as `stuck` aborted the whole drain on
the ordinary first retry, because `retry_component` stamps a failure code on
every transient retry and nothing clears it before the component completes --
while `due_component_count` counted the same row as due and the next pass would
have claimed it. The dueness test is now the product's own, verbatim.

None of this is the product's write path, which is why none of it is fixed
here. The product defects the runs exposed -- a claim lease shorter than a
dispatch, the linear component chain, the whole-corpus graph refresh per
fan-out -- are named in the lane's result, not smuggled in as harness changes.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _load(stem: str, alias: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(alias, SCRIPTS / f"{stem}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _acceptance():
    return _load("live_write_acceptance", "live_write_acceptance_under_test")


def _latency():
    return _load("semantic_write_latency", "semantic_write_latency_under_test")


DRAIN_KEYS = {
    "mode",
    "limit",
    "passes",
    "completed",
    "due",
    "claimed",
    "retrying",
    "failed",
    "stuck",
    "max_attempt_count",
    "stalled_passes",
    "rate_components_per_min",
    "projected_seconds_to_converge",
    "bound_overrun_seconds",
    "oldest_due_age_seconds",
}


def _drain(**overrides: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "mode": "normal",
        "limit": 16,
        "passes": 1,
        "completed": 0,
        "due": 0,
        "claimed": 0,
        "retrying": 0,
        "failed": 0,
        "stuck": 0,
        "max_attempt_count": 0,
        "stalled_passes": 0,
        "rate_components_per_min": 0.0,
        "projected_seconds_to_converge": None,
        "bound_overrun_seconds": 0.0,
        "oldest_due_age_seconds": None,
    }
    assert set(overrides) <= DRAIN_KEYS, set(overrides) - DRAIN_KEYS
    block.update(overrides)
    return block


def _report(
    *,
    converged: bool = True,
    drain: dict[str, Any] | None = None,
    bound: float = 120.0,
) -> dict:
    """A report that passes every assertion except the one under test.

    The bound defaults to something that is NOT `CONVERGENCE_BOUND_SECONDS`, so
    a failure message that quotes the constant instead of the bound the run was
    actually given is visible rather than accidentally correct.
    """
    return {
        "transport": "direct",
        "samples_per_operation": 30,
        "fast_durable_ack": "active",
        "operations": {
            name: {
                "samples": 30,
                "p50_ms": 900.0,
                "p90_ms": 1_800.0,
                "max_ms": 1_900.0,
            }
            for name in ("remember", "edit")
        },
        "recall_admission": "ready",
        "read_your_write": {"exact": 60, "warming": 0, "stale": 0},
        "uncovered_full_receipts": 0,
        "post_burst_convergence_seconds": 12.0,
        "post_burst_convergence_bound_seconds": bound,
        "post_burst_converged": converged,
        "reconciliation_demanded": 0,
        "drain": _drain() if drain is None else drain,
    }


# --------------------------------------------------------------------------- #
# A real receipt store, seeded row by row
# --------------------------------------------------------------------------- #


def _seeded_store(vault_root: Path, rows: list[tuple[str, str, int, float, str | None]]):
    """Create the store and put exact component rows in it.

    rows: (component, state, attempt_count, next_attempt_at, failure_code).
    A `claimed` row gets a live lease, which is the state that is neither due
    nor stuck and has to be visible some other way.
    """
    from exomem import deferred_index

    vault_root.mkdir(parents=True, exist_ok=True)
    deferred_index._connect(vault_root, create=True).close()
    path = deferred_index.store_path(vault_root)
    now = time.time()
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "INSERT OR REPLACE INTO derived_batches (schema_version, batch_id, "
            "mutation_attempt_digest, canonical_generation, checkpoint_id, "
            "state, created_at, updated_at) "
            "VALUES (1, 'b1', ?, 'gen1', 'ck1', 'ready', ?, ?)",
            ("a" * 64, now, now),
        )
        for component, state, attempts, next_at, code in rows:
            connection.execute(
                "INSERT OR REPLACE INTO derived_batch_components (batch_id, "
                "component, revision, state, lease_revision, claim_owner, "
                "claim_expires_at, attempt_count, next_attempt_at, created_at, "
                "updated_at, failure_code) "
                "VALUES ('b1', ?, 1, ?, 0, NULL, ?, ?, ?, ?, ?, ?)",
                (
                    component,
                    state,
                    (now + 600.0) if state == "claimed" else None,
                    attempts,
                    next_at,
                    now,
                    now,
                    code,
                ),
            )
        connection.commit()
    finally:
        connection.close()
    return path


# --------------------------------------------------------------------------- #
# The warm-up
# --------------------------------------------------------------------------- #


def test_acceptance_warm_up_publishes_the_catalogue_proof(
    vault: Path, tmp_path: Path
) -> None:
    """Admission is the catalogue proof, not a finished warm window.

    `begin_warm`/`finish_warm` only bracket the window. What admits retrieval
    is `readiness.admit_retrieval_proof`, the sole writer of the
    `retrieval_catalog` event. A harness that brackets without publishing
    leaves `_warm_finished` set and the event unset -- which is exactly the
    `unavailable` state, and it is the state the first live run reported while
    answering `warming` to every read it had just written.
    """
    module = _acceptance()
    try:
        report = module.measure(
            vault,
            transport="direct",
            samples_per_operation=1,
            state_dir=tmp_path / "acceptance-state",
            convergence_bound_seconds=60.0,
        )
    finally:
        module.readiness.unmanage_runtime()

    assert report["recall_admission"] == "ready"
    # The proof itself, not merely a window that closed.
    assert module.readiness.is_ready("retrieval_catalog")
    # An admitted catalogue is what makes the read after a write exact. Warming
    # stays a truthful answer under the contract; it is not a truthful answer
    # for a harness that never had a catalogue to answer from.
    assert report["read_your_write"] == {"exact": 2, "warming": 0, "stale": 0}


def test_a_warm_up_that_cannot_admit_fails_the_measure(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The assertion is the whole point of sharing the warm-up.

    A helper that warms and then reports whatever admission it happens to find
    is the defect this change exists to remove, one level down: the run would
    proceed, measure the offline source-walk fallback, and print a report whose
    `recall_admission` quietly said `unavailable` -- which is what the first
    live run did. So the failure must be loud, and the runtime must still be
    given back on the way out.
    """
    from exomem import readiness, warmup

    module = _acceptance()
    monkeypatch.setattr(warmup, "warm_retrieval_catalog", lambda root: False)
    monkeypatch.setattr(
        readiness,
        "retrieval_admission",
        lambda root: {"state": "unavailable", "admitted": False},
    )

    try:
        with pytest.raises(RuntimeError, match="admission was not granted"):
            module.measure(
                vault,
                transport="direct",
                samples_per_operation=1,
                state_dir=tmp_path / "acceptance-state",
                convergence_bound_seconds=5.0,
            )
        # A warm-up that raises after managing the runtime would otherwise
        # leave this process managed for good.
        assert readiness.runtime_managed() is False
    finally:
        readiness.unmanage_runtime()


def test_shared_warm_up_is_the_same_object_in_both_scripts() -> None:
    """One warm-up, imported twice -- not two warm-ups that agree today.

    The gate script had already been bitten by a warm-up that could not admit
    and had fixed it in place. The acceptance harness carried its own shorter
    version and shipped the identical defect months later. Agreeing copies is
    the condition that produced that, so the invariant is object identity.
    """
    acceptance = _acceptance()
    latency = _latency()
    shared = acceptance.managed_recall.enter_managed_recall
    assert latency._enter_managed_recall is shared
    assert shared.__module__ == "managed_recall"


# --------------------------------------------------------------------------- #
# What the store is asked, and what it is asked with
# --------------------------------------------------------------------------- #


def test_an_ordinary_retry_is_retrying_not_stuck(tmp_path: Path) -> None:
    """The harness and the product must not disagree about the same row.

    `retry_component` stamps a `failure_code` and bumps `attempt_count` on
    every transient retry -- including the ordinary one a claim lease shorter
    than its dispatch produces -- and leaves the row `retryable`. Nothing
    clears that code until the component completes. Once the backoff elapses,
    `due_component_count` counts the row as due and `claim_ready_components`
    will claim it on the very next pass. A predicate reading "attempted, and
    carrying a code" therefore condemned the healthy common case.
    """
    from exomem import derived_receipts

    module = _acceptance()
    now = time.time()

    due_again = tmp_path / "due-again"
    _seeded_store(due_again, [("graph", "retryable", 1, now - 30.0, "dispatch_failed")])
    observed = module._drain_observation(due_again)
    assert derived_receipts.due_component_count(due_again) == 1, (
        "the product says this row is due and claimable"
    )
    assert observed["due"] == 1
    assert observed["retrying"] == 1
    assert observed["stuck"] == 0 and observed["failed"] == 0, observed

    backing_off = tmp_path / "backing-off"
    _seeded_store(
        backing_off, [("graph", "retryable", 1, now + 60.0, "dispatch_failed")]
    )
    shallow = module._drain_observation(backing_off)
    assert shallow["due"] == 0 and shallow["retrying"] == 0
    assert shallow["failed"] == 1, "not due and carrying a code, so reported"
    assert shallow["stuck"] == 0, (
        "one attempt deep is a retry, not a component held back"
    )

    deep = tmp_path / "deep"
    _seeded_store(
        deep,
        [("graph", "retryable", module.STUCK_ATTEMPT_THRESHOLD, now + 900.0, None)],
    )
    held = module._drain_observation(deep)
    assert held["stuck"] == 1 and held["failed"] == 0, held
    assert held["max_attempt_count"] == module.STUCK_ATTEMPT_THRESHOLD


def test_a_live_claim_is_visible_rather_than_silently_converged(
    tmp_path: Path,
) -> None:
    """`drain_once` parks a claim when a batch's proof is not `ready`.

    That row is invisible to `due_component_count` (its lease has not expired)
    and to `stuck` (it has never been attempted), so a run would otherwise
    report a clean convergence over work nobody is doing.
    """
    from exomem import derived_receipts

    module = _acceptance()
    vault_root = tmp_path / "parked"
    _seeded_store(vault_root, [("graph", "claimed", 0, time.time(), None)])

    observed = module._drain_observation(vault_root)
    assert derived_receipts.due_component_count(vault_root) == 0
    assert observed["stuck"] == 0 and observed["failed"] == 0
    assert observed["claimed"] == 1, observed


# --------------------------------------------------------------------------- #
# Convergence
# --------------------------------------------------------------------------- #


def test_converge_checks_the_deadline_after_every_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass is bounded by the product's own allowance and then re-checked.

    The allowance is per-machine -- `mode.resolve_mode()` reads `EXOMEM_MODE`,
    then `~/.exomem/config.json` -- so this asserts the harness uses whatever
    the scheduler would use here and *reports* it, rather than pinning a number
    that is only true on the box the test happens to run on. A hard-coded 32
    was neither.
    """
    module = _acceptance()
    limits: list[int] = []
    # A drain that keeps making progress against a backlog that never empties
    # is the shape a missing deadline check runs forever in, so the fake stops
    # it and says so rather than hanging the suite.
    overrun: list[str] = []

    def fake_drain_once(vault_root, **kwargs):
        limits.append(int(kwargs["limit"]))
        if len(limits) > 3:
            overrun.append("kept draining past the deadline")
            return 0
        # One pass outliving the whole window is the measured case, not a
        # contrived one: 208s of drain against a 120s bound.
        time.sleep(0.35)
        return 1

    monkeypatch.setattr(module.derived_drain, "drain_once", fake_drain_once)
    monkeypatch.setattr(
        module.derived_receipts, "due_component_count", lambda root, **kw: 7
    )

    seconds, converged, drain = module._converge(tmp_path, bound_seconds=0.1)

    assert converged is False
    assert not overrun, overrun
    assert drain["passes"] == 1, "the deadline had already passed after pass 1"
    assert seconds >= 0.35
    # The limit is the product's, and the report says which one it was.
    assert limits == [module.derived_drain.progress_limit()]
    assert drain["limit"] == module.derived_drain.progress_limit()
    assert drain["mode"] == module.mode.resolve_mode()


def test_the_bound_is_a_checkpoint_between_passes_and_the_overrun_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is no intra-pass timeout, so the overrun has to be a number.

    `drain_once` dispatches its whole claim synchronously. A bound of 120s was
    overrun by 88s in one pass on the real corpus, and a report that only says
    "did not converge within 120s" hides that the window was never enforced.
    """
    module = _acceptance()

    def slow_pass(vault_root, **kwargs):
        time.sleep(0.6)
        return 1

    monkeypatch.setattr(module.derived_drain, "drain_once", slow_pass)
    monkeypatch.setattr(
        module.derived_receipts, "due_component_count", lambda root, **kw: 9
    )

    seconds, converged, drain = module._converge(tmp_path, bound_seconds=0.15)

    assert converged is False
    assert drain["passes"] == 1
    assert seconds > 0.15
    assert drain["bound_overrun_seconds"] > 0.3, drain
    assert drain["bound_overrun_seconds"] == pytest.approx(seconds - 0.15, abs=0.05)


def test_converge_does_not_abort_on_an_ordinary_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry in flight is a drain doing its job, not a reason to give up.

    This is the reviewer's finding: the first version returned after one pass
    on any row `retry_component` had touched, which on a real corpus is the
    common case -- and then told the operator a person was needed.
    """
    module = _acceptance()
    passes: list[int] = []

    def fake_drain_once(vault_root, **kwargs):
        passes.append(1)
        return 4

    monkeypatch.setattr(module.derived_drain, "drain_once", fake_drain_once)
    monkeypatch.setattr(
        module.derived_receipts, "due_component_count", lambda root, **kw: 30
    )
    monkeypatch.setattr(
        module,
        "_drain_observation",
        lambda root: {
            "due": 30,
            "claimed": 0,
            "retrying": 1,
            "stuck": 0,
            "failed": 1,
            "max_attempt_count": 1,
        },
    )

    seconds, converged, drain = module._converge(tmp_path, bound_seconds=0.4)

    assert converged is False
    assert len(passes) > 1, "the drain gave up on an ordinary retry"
    assert drain["retrying"] == 1 and drain["stuck"] == 0
    message = module._convergence_failure(_report(converged=False, drain=drain))
    assert "deep backlog draining" in message, message
    assert "1 retrying" in message, message
    assert "drain stuck" not in message, message


def test_converge_reports_a_stuck_claim_distinct_from_a_deep_backlog() -> None:
    """"Did not converge" is the one answer an operator cannot act on.

    A deep backlog that is draining wants a longer window and a look at what a
    single dispatch costs. A stuck drain wants a person. An unreadable store is
    a third thing and must not be rendered as either. The failure has to say
    which -- and it has to quote the bound the run was actually given, not the
    constant, or `--convergence-bound` is decoration.
    """
    module = _acceptance()

    stuck = _report(
        converged=False,
        drain=_drain(
            passes=4,
            completed=3,
            due=12,
            failed=1,
            stuck=2,
            max_attempt_count=4,
            rate_components_per_min=1.5,
            projected_seconds_to_converge=1_760.0,
        ),
    )
    with pytest.raises(SystemExit) as stuck_exit:
        module.check(stuck)
    stuck_message = str(stuck_exit.value)
    assert "drain stuck" in stuck_message
    assert "2 component(s) held back at 4 attempt(s)" in stuck_message
    assert "1 carrying a failure code" in stuck_message
    assert "deep backlog" not in stuck_message
    # The bound the run was given, not CONVERGENCE_BOUND_SECONDS.
    assert "within 120s" in stuck_message
    assert "300s" not in stuck_message

    backlog = _report(
        converged=False,
        drain=_drain(
            passes=4,
            completed=9,
            due=108,
            rate_components_per_min=2.4,
            projected_seconds_to_converge=2_700.0,
        ),
    )
    with pytest.raises(SystemExit) as backlog_exit:
        module.check(backlog)
    backlog_message = str(backlog_exit.value)
    assert "deep backlog draining at 2.4 component(s)/min" in backlog_message
    assert "2700" in backlog_message
    assert "stuck" not in backlog_message
    assert "within 120s" in backlog_message

    # Zero progress across two passes is its own stuck: nothing failed, nothing
    # is holding a lease, and nothing is moving either.
    stalled = _report(
        converged=False,
        drain=_drain(passes=2, completed=0, due=40, stalled_passes=2),
    )
    with pytest.raises(SystemExit, match="drain stuck"):
        module.check(stalled)

    # And a report from before this block existed still fails, and still says
    # the plain thing -- the acceptance never silently passes for want of a key.
    legacy = _report(converged=False)
    legacy.pop("drain")
    with pytest.raises(SystemExit, match="did not converge"):
        module.check(legacy)


def test_an_unreadable_counter_is_never_reported_as_a_draining_backlog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store that could not be read has not earned the word "draining".

    `due` going unreadable used to leave `stuck` and `failed` at zero and a
    positive rate, so the message invented a backlog nobody had measured.
    """
    module = _acceptance()

    def unreadable(vault_root, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(module.derived_receipts, "due_component_count", unreadable)
    monkeypatch.setattr(module.derived_drain, "drain_once", lambda root, **kw: 1)

    seconds, converged, drain = module._converge(tmp_path, bound_seconds=0.3)

    assert converged is False
    assert drain["due"] == -1, drain
    message = module._convergence_failure(_report(converged=False, drain=drain))
    assert "the drain observation was unreadable" in message, message
    assert "draining" not in message, message

    # The same for an unreadable component scan.
    for counter in ("stuck", "failed"):
        message = module._convergence_failure(
            _report(converged=False, drain=_drain(**{counter: -1}))
        )
        assert "unreadable" in message, (counter, message)


def test_the_drain_block_is_content_free(vault: Path, tmp_path: Path) -> None:
    """Counts, codes, a mode name and ages -- no batch, no path, no message."""
    module = _acceptance()
    try:
        report = module.measure(
            vault,
            transport="direct",
            samples_per_operation=1,
            state_dir=tmp_path / "acceptance-state",
            convergence_bound_seconds=60.0,
        )
    finally:
        module.readiness.unmanage_runtime()

    drain = report["drain"]
    assert set(drain) == DRAIN_KEYS
    assert drain["mode"] == module.mode.resolve_mode()
    assert drain["limit"] == module.derived_drain.progress_limit()
    rendered = json.dumps(report, sort_keys=True, default=str)
    for token in ("Knowledge Base", str(vault), "acceptance-", ".md"):
        assert token not in rendered, (token, rendered)
