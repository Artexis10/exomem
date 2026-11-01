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
import threading
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
    "pending",
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
        "pending": 0,
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
        "condition": "warm",
        "preparation": {"converged": True, "due_projection_present": True},
        "operations": {
            name: {
                "samples": 30,
                "p50_ms": 900.0,
                "p90_ms": 950.0,
                "p95_ms": 975.0,
                "max_ms": 1_900.0,
            }
            for name in ("remember", "edit_memory", "record_memory", "plan_memory")
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
            package_mode="source",
            convergence_bound_seconds=60.0,
        )
    finally:
        module.readiness.unmanage_runtime()

    assert report["recall_admission"] == "ready"
    # The proof itself, not merely a window that closed.
    assert module.readiness.is_ready("retrieval_catalog")
    assert module.readiness.is_ready("semantic_corpus")
    assert report["preparation"]["startup"] == "product-warm-all"
    assert report["preparation"]["converged"] is True
    assert report["preparation"]["due_projection_present"] is True
    # An admitted catalogue is what makes the read after a write exact. Warming
    # stays a truthful answer under the contract; it is not a truthful answer
    # for a harness that never had a catalogue to answer from.
    assert report["read_your_write"] == {"exact": 8, "warming": 0, "stale": 0, "unknown": 0}
    assert report["package"]["mode"] == "source"
    rendered = json.dumps(report)
    for private_value in (str(vault), "Knowledge Base", "acceptanceremember"):
        assert private_value not in rendered
    assert set(report["operations"]) == set(module.OPERATIONS)
    for operation in module.OPERATIONS:
        cases = [case for case in report["cases"] if case["operation"] == operation]
        assert [case["condition"] for case in cases] == ["cold", "warm"]
        assert all(case["terminal_outcome"] == "ok" for case in cases)
        assert all(case["thread_cpu_ms"] >= 0 for case in cases)
        measured = report["operations"][operation]
        assert measured["samples"] == 1
        assert measured["p95_ms"] == cases[1]["elapsed_ms"]


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
                package_mode="source",
            convergence_bound_seconds=5.0,
            )
        # A warm-up that raises after managing the runtime would otherwise
        # leave this process managed for good.
        assert readiness.runtime_managed() is False
    finally:
        readiness.unmanage_runtime()


def _enrol_store(
    module: Any, vault: Path, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's offline `exomem collections create`, in the coordinator database a run will use."""
    from test_collection_store_s1_gate import coordinator
    from test_collection_store_summary import summary_text
    from test_collection_store_writer import manifest_path

    from exomem import __main__ as cli

    state_dir.mkdir(parents=True)
    manifest = state_dir / "store-collection.md"
    manifest.write_text(summary_text(), encoding="utf-8")
    for name, value in {
        "EXOMEM_STATE_ROOT": str(state_dir / "product"),
        "EXOMEM_WRITER_LEASE_URL": "http://localhost",
        "EXOMEM_WRITER_LEASE_VAULT_ID": module.LEASE_VAULT_ID,
        "EXOMEM_WRITER_LEASE_REPLICA_ID": module.LEASE_REPLICA_ID,
        "EXOMEM_WRITER_LEASE_TOKEN": "lease",
        "EXOMEM_WRITER_LEASE_STATE_DIR": str(state_dir),
        "EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN": "operator",
    }.items():
        monkeypatch.setenv(name, value)
    with coordinator(state_dir / module.LEASE_DATABASE_NAME):
        assert cli._collections_main([
            "create", "--vault", str(vault), "--manifest-path", manifest_path(),
            "--manifest-file", str(manifest), "--why", "disposable acceptance store",
        ]) == 0
    monkeypatch.delenv("EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN")


def test_direct_mode_serves_a_collection_store_before_it_prepares_collections(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vault with a store refuses every collection write until a service has admitted it.

    The first live run on a copy of a real vault died preparing collections with
    COLLECTION_NOT_FOUND: direct mode wrote them through a lease manager that had no
    coordinator and no store thread, so the store read as absent.
    """
    module = _acceptance()
    state_dir = tmp_path / "acceptance-state"
    _enrol_store(module, vault, state_dir, monkeypatch)
    try:
        report = module.measure(
            vault, transport="direct", samples_per_operation=1, state_dir=state_dir,
            package_mode="source", convergence_bound_seconds=60.0,
        )
    finally:
        module.readiness.unmanage_runtime()

    assert [case["terminal_outcome"] for case in report["cases"]] == ["ok"] * 8
    assert "EXOMEM_WRITER_LEASE_URL" in module.os.environ  # the caller's own value, put back
    assert module.os.environ["EXOMEM_WRITER_LEASE_URL"] == "http://localhost"


def test_direct_mode_refuses_a_copied_store_with_the_products_adoption_instruction(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copy is not enrolled in this run's coordinator, so it is refused before any write."""
    module = _acceptance()
    _enrol_store(module, vault, tmp_path / "owner-state", monkeypatch)
    state_dir = tmp_path / "acceptance-state"
    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(state_dir / "product"))
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(state_dir))
    before = {path: path.read_bytes() for path in vault.rglob("*.md")}
    try:
        with pytest.raises(SystemExit, match=r"COLLECTION_STORE_ADOPTION_REQUIRED.*adopt-local.*leases\.sqlite"):
            module.measure(
                vault, transport="direct", samples_per_operation=1, state_dir=state_dir,
                package_mode="source", convergence_bound_seconds=60.0,
            )
    finally:
        module.readiness.unmanage_runtime()

    assert {path: path.read_bytes() for path in vault.rglob("*.md")} == before


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
    # `drain_once` is module-level, and a `derived_drain` worker that another
    # test in this process left running calls the same function -- it is woken
    # by any derived receipt and polls on its own besides. Its claim is real
    # work, but it is not this test's evidence, and counting it made a
    # one-pass converge loop look like two. The loop's own pass count is
    # `drain["passes"]`, asserted below; this keeps `limits` to the calls that
    # can have come from it.
    converge_thread = threading.get_ident()

    def fake_drain_once(vault_root, **kwargs):
        if threading.get_ident() != converge_thread:
            return 0
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
            "pending": 30,
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
            package_mode="source",
            convergence_bound_seconds=60.0,
        )
    finally:
        module.readiness.unmanage_runtime()

    drain = report["drain"]
    assert set(drain) == DRAIN_KEYS
    assert drain["mode"] == module.mode.resolve_mode()
    assert drain["limit"] == module.derived_drain.progress_limit()
    # Key names alone are not the content-free rule: a value could carry a path
    # the token grep below never sees, because it lives outside the vault. Every
    # drain value is therefore a count, a duration, an age or None, and `mode`
    # is the single string, constrained to the mode vocabulary.
    for key, value in drain.items():
        if key == "mode":
            assert value in module.mode.CANON, (key, value)
            continue
        assert value is None or isinstance(value, (int, float)), (key, value)
        assert not isinstance(value, str), (key, value)
    rendered = json.dumps(report, sort_keys=True, default=str)
    for token in ("Knowledge Base", str(vault), "acceptance-", ".md"):
        assert token not in rendered, (token, rendered)


def test_convergence_waits_for_retirement_backoff_and_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero completions can retire custody or wait for a transient retry."""
    module = _acceptance()
    observations = iter([
        _drain(pending=5, due=5),
        _drain(pending=4, due=4),  # Retiring an obsolete receipt is progress.
        _drain(pending=2, due=0, stuck=2, failed=2, max_attempt_count=4),
        _drain(pending=1, due=0, failed=1, max_attempt_count=1),
        _drain(pending=1, due=0, claimed=1),
        _drain(),
    ])
    monkeypatch.setattr(module, "_drain_observation", lambda _root: next(observations))
    monkeypatch.setattr(module.derived_drain, "drain_once", lambda *a, **k: 0)
    monkeypatch.setattr(module.derived_drain, "last_pass_observation", lambda _root: {})
    monkeypatch.setattr(module.derived_drain, "component_dispatcher", lambda *a, **k: None)
    monkeypatch.setattr(
        module.derived_drain, "canonical_generation_observer", lambda *a, **k: None
    )

    _elapsed, converged, drain = module._converge(tmp_path, bound_seconds=30.0)

    assert converged is True
    assert drain["pending"] == 0
    assert drain["completed"] == 0
    assert drain["passes"] == 5


@pytest.mark.parametrize("operation", ("remember", "edit_memory", "record_memory", "plan_memory"))
def test_warm_p95_enforces_the_shared_strict_bar(operation: str) -> None:
    module = _acceptance()
    bar = module.request_latency_contract.load_contract().bar("ordinary-write-warm")
    report = _report()
    report["operations"][operation]["p95_ms"] = bar.threshold_ms
    with pytest.raises(SystemExit, match=f"{operation} warm p95=.*ordinary-write-warm"):
        module.check(report)
    report["operations"][operation]["p95_ms"] = bar.threshold_ms - 0.01
    module.check(report)
    report["condition"] = "contended"
    report["operations"][operation]["p95_ms"] = bar.threshold_ms * 2
    module.check(report)


def test_installed_proof_refuses_the_source_import() -> None:
    import exomem

    module = _acceptance()
    with pytest.raises(SystemExit, match="non-editable installed"):
        module._package_identity("installed")
    identity = module._package_identity("source")
    assert identity["mode"] == "source"
    assert identity["version"] == exomem.__version__
    import hashlib

    origin_hash = hashlib.sha256(str(Path(exomem.__file__).resolve()).encode()).hexdigest()
    assert identity["origin_sha256"] == origin_hash
    assert identity["source_revision"]


def test_disposable_measurement_refuses_conflicting_ambient_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _acceptance()
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(tmp_path / "other-vault"))
    with pytest.raises(SystemExit, match="conflicting ambient vault"):
        module.measure(
            tmp_path / "vault", transport="direct", samples_per_operation=1,
            state_dir=tmp_path / "state", package_mode="source",
        )


def test_disposable_measurement_keeps_implicit_writer_state_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _acceptance()
    for name in ("EXOMEM_VAULT_PATH", "EXOMEM_STATE_ROOT", "EXOMEM_WRITER_LEASE_STATE_DIR"):
        monkeypatch.delenv(name, raising=False)
    state = tmp_path / "state"

    module._require_disposable_environment(tmp_path / "vault", state)
    manager = module.writer_lease.get_manager()

    assert manager.idempotency.path.is_relative_to(state)


@pytest.mark.parametrize("tool_error", (False, True))
def test_connector_measures_the_requested_public_command_and_rejects_tool_errors(
    monkeypatch: pytest.MonkeyPatch, tool_error: bool,
) -> None:
    from email.message import Message

    module = _acceptance()
    sent = []
    correlation_ids = []
    authorization_headers = []

    class Response:
        headers = Message()
        headers["Content-Type"] = "application/json"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            request = sent[-1]
            return json.dumps({
                "jsonrpc": "2.0", "id": request["id"],
                "result": {"isError": tool_error, "structuredContent": {"outcome": "committed"}},
            }).encode()

    def respond(request, **_kwargs):
        sent.append(json.loads(request.data))
        correlation_ids.append(request.get_header("X-exomem-request-id"))
        authorization_headers.append(request.get_header("Authorization"))
        return Response()

    monkeypatch.setattr(module, "urlopen", respond)
    client = module._ConnectorTransport("http://127.0.0.1:8765/mcp", "synthetic-session")
    arguments = {"path": "Knowledge Base/Entities/Concepts/example.md", "operation": {"kind": "replace_string", "old_string": "before", "new_string": "after"}, "why": "correct observation"}
    if tool_error:
        with pytest.raises(SystemExit, match="tool returned an error"):
            client.call("edit_memory", arguments)
    else:
        elapsed, result = client.call("edit_memory", arguments)
        assert elapsed >= 0 and result["outcome"] == "committed"
    assert sent[0]["params"] == {"name": "edit_memory", "arguments": arguments}
    assert authorization_headers == ["Bearer synthetic-session"]
    import uuid

    assert str(uuid.UUID(correlation_ids[0])) == client.request_id == sent[0]["id"]


@pytest.fixture
def isolated_connector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The external process and HTTP boundaries of a disposable service."""
    from email.message import Message

    module = _acceptance()
    root, state = tmp_path / "vault", tmp_path / "state"
    for key in ("EXOMEM_VAULT_PATH", "EXOMEM_STATE_ROOT", "EXOMEM_WRITER_LEASE_STATE_DIR"):
        monkeypatch.delenv(key, raising=False)
    environment = {
        "EXOMEM_VAULT_PATH": str(root), "EXOMEM_STATE_ROOT": str(state / "product"),
        "EXOMEM_WRITER_LEASE_STATE_DIR": str(state), "EXOMEM_INSTANCE_ID": "disposable-test",
    }
    namespaces = {"/proc/self/ns/net": "net:[2]", "/proc/4321/ns/net": "net:[2]"}
    health = {"status": "ready", "instance_id": "disposable-test", "release": "test-release",
              "mcp_tool_surface_sha256": "a" * 64, "retrieval": {"state": "ready", "admitted": True}}
    read_bytes = Path.read_bytes
    readlink = module.os.readlink

    def process_environment(path):
        if str(path) == "/proc/4321/environ":
            return b"\0".join(f"{key}={value}".encode() for key, value in environment.items())
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", process_environment)
    monkeypatch.setattr(module.os, "readlink", lambda path, **kw: namespaces[str(path)] if str(path) in namespaces else readlink(path, **kw))
    monkeypatch.setattr(module.socket, "if_nameindex", lambda: [(1, "lo")])

    class Response:
        headers = Message()
        headers["Content-Type"] = "application/json"
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps(health).encode()

    monkeypatch.setattr(module, "urlopen", lambda *_args, **_kw: Response())
    return module, root, state, environment, namespaces, health


@pytest.mark.parametrize("missing", [
    "pid", "host-namespace", "url", "host-network", "other-network", "host-interface", "EXOMEM_VAULT_PATH",
    "EXOMEM_STATE_ROOT", "EXOMEM_WRITER_LEASE_STATE_DIR", "EXOMEM_INSTANCE_ID", "health-instance",
])
def test_connector_refuses_unproven_service_before_any_fixture_write(isolated_connector, monkeypatch, missing):
    module, root, state, environment, namespaces, health = isolated_connector
    pid, host_namespace, url = 4321, "net:[1]", "http://127.0.0.1:8765/mcp"
    if missing == "pid":
        pid = None
    elif missing == "host-namespace":
        host_namespace = None
    elif missing == "url":
        url = "https://example.invalid/mcp"
    elif missing == "host-network":
        host_namespace = "net:[2]"
    elif missing == "other-network":
        namespaces["/proc/4321/ns/net"] = "net:[3]"
    elif missing == "host-interface":
        monkeypatch.setattr(module.socket, "if_nameindex", lambda: [(1, "lo"), (2, "eth0")])
    elif missing == "health-instance":
        health["instance_id"] = "another-service"
    else:
        environment.pop(missing)

    def fixture_write(_client):
        pytest.fail("unproven connector reached a fixture write")

    monkeypatch.setattr(module, "_prepare_collections", fixture_write)
    with pytest.raises(SystemExit, match="connector"):
        module.measure(root, transport="connector", connector_url=url, connector_pid=pid,
                       host_network_namespace=host_namespace,
                       connector_token="synthetic-session",
                       state_dir=state, samples_per_operation=1, package_mode="source")


@pytest.mark.parametrize("projection_state, expected_entries", [("present", 3), ("missing", None), ("unreadable", None)])
def test_connector_observes_service_without_local_runtime_writes(isolated_connector, monkeypatch, projection_state, expected_entries):
    module, root, state, _environment, _namespaces, health = isolated_connector
    calls, notes, rows = [], {}, {}
    projection_path = root.parent / "due-state.json"
    if projection_state == "present":
        projection_path.write_text(json.dumps({
            "version": module.due_state.SCHEMA_VERSION,
            "categories": {
                "category-one": {"Notes/first.md": {
                    "open": [{"ref": "review:first", "due_since": "2026-01-01"}],
                    "pending": [{"ref": "review:second", "due_on": "2027-01-01"}],
                }},
                "category-two": {"Notes/second.md": {
                    "open": [{"ref": "review:third", "due_since": "2026-01-02"}], "pending": [],
                }},
            },
        }))
    elif projection_state == "unreadable":
        projection_path.write_text("unreadable projection")
    monkeypatch.setattr(module.due_state, "state_path", lambda _root: projection_path)

    def local_runtime(*_args, **_kwargs):
        pytest.fail("HTTP observer entered the local runtime")

    for owner, name in (
        (module.managed_recall, "enter_managed_recall"), (module.derived_drain, "drain_once"),
        (module.due_state, "reconcile"), (module.due_state, "served_entries"),
        (module.writer_lease, "LeaseManager"), (module.semantic_contract, "build_corpus_context"),
        (module.find_module, "find"), (module.pending_recall, "status"),
        (module.readiness, "unmanage_runtime"),
    ):
        monkeypatch.setattr(owner, name, local_runtime)
    monkeypatch.setattr(module, "_drain_observation", lambda _root, **_kw: {"pending": 0, "due": 0, "claimed": 0, "retrying": 0, "failed": 0, "stuck": 0, "max_attempt_count": 0})
    monkeypatch.setattr(module, "_full_receipt_count", lambda _root, **_kw: 0)
    monkeypatch.setattr(module, "_reconciliation_demanded", lambda _root, **_kw: 0)

    def public_call(client, name, arguments):
        import uuid

        assert client.token == "synthetic-session"
        client.request_id = str(uuid.uuid4())
        calls.append((name, arguments, client.request_id))
        if name == "record_memory" and arguments["action"] == "describe":
            result = {"examples": {"minimal": {"manifest_path": "Knowledge Base/Records/Acceptance/_collection.md", "manifest_text": "---\ntitle: Acceptance\n---\n", "append_item": {}}}}
        elif name in ("record_memory", "plan_memory"):
            action = arguments["action"]
            if action == "inspect":
                result = {"snapshot": "snapshot"}
            elif action == "query":
                result = {"rows": rows[name]}
            else:
                rows[name] = [arguments.get("item", {})]
                result = {"outcome": "committed"}
        elif name == "remember":
            path = f"Knowledge Base/Notes/Insights/accepted-{len(notes)}.md"
            notes[path] = arguments["content"]
            result = {"path": path, "ref": f"memory:{len(notes)}"}
        elif name == "edit_memory":
            path = arguments["path"]
            operation = arguments["operation"]
            notes[path] = notes[path].replace(operation["old_string"], operation["new_string"])
            result = {"path": path}
        elif name == "ask_memory":
            result = {"hits": [{"path": path, "ref": f"memory:{index}"} for index, (path, body) in enumerate(notes.items(), 1) if arguments["query"] in body]}
            if not arguments["query"]:
                result = {"hits": [{"path": "Knowledge Base/Notes/Insights/existing.md"}]}
        elif name == "read_memory":
            index = int(arguments["path"].split(":")[1]) - 1
            path = list(notes)[index]
            result = {"path": path, "body": notes[path]}
        else:
            pytest.fail(f"unexpected public command {name}")
        return 1.0, result

    monkeypatch.setattr(module._ConnectorTransport, "call", public_call)
    report = module.measure(root, transport="connector", connector_url="http://127.0.0.1:8765/mcp", connector_pid=4321,
                            host_network_namespace="net:[1]",
                            connector_token="synthetic-session",
                            state_dir=state, samples_per_operation=1, package_mode="source")

    assert report["read_your_write"] == {"exact": 8, "warming": 0, "unavailable": 0, "stale": 0, "unknown": 0}
    assert report["package"]["role"] == "observer-only"
    assert report["connector_runtime_provenance"]["release"] == health["release"]
    assert report["preparation"]["startup"] == "service-owned"
    assert report["preparation"]["runtime_readiness"] == "externally-unverified"
    assert report["preparation"]["due_projection_entries"] == expected_entries
    assert report["preparation"]["due_items"] is None
    assert all(case["thread_cpu_ms"] is None for case in report["cases"])
    public_writes = {request_id for name, arguments, request_id in calls if name in module.OPERATIONS and arguments.get("action") not in ("describe", "create", "inspect", "query")}
    assert {case["request_id"] for case in report["cases"]} == public_writes
    assert report["drain"]["owner"] == "service"


@pytest.mark.parametrize("response, body, expected", [
    ({"hits": []}, "independent-marker", "stale"),
    ({"success": False, "error": {"code": "NOT_FOUND"}}, "independent-marker", "unknown"),
    ({"success": False, "error": {"code": "RETRIEVAL_INDEX_WARMING", "status": "warming", "complete": False}}, "independent-marker", "warming"),
    ({"success": False, "error": {"code": "RETRIEVAL_INDEX_WARMING", "status": "temporarily_unavailable", "complete": False}}, "independent-marker", "unavailable"),
    ({"hits": [{"path": "different.md"}]}, "independent-marker", "stale"),
    ({"hits": [{"path": "accepted.md", "ref": "memory:1"}]}, "old-marker", "stale"),
    ({"hits": [{"path": "accepted.md", "ref": "memory:1"}]}, "independent-marker", "exact"),
])
def test_connector_readback_uses_public_marker_and_returned_reference(response, body, expected):
    module = _acceptance()
    calls = []

    class Client:
        def call(self, name, arguments):
            calls.append((name, arguments))
            return 1.0, response if name == "ask_memory" else {"path": "accepted.md", "body": body}

    assert module._connector_read_your_write(Client(), "independent-marker", "accepted.md") == expected
    assert calls[0][1]["query"] == "independent-marker"
    if expected == "exact":
        assert calls[1] == ("read_memory", {"path": "memory:1"})


@pytest.mark.parametrize("state", ["retryable", "claimed"])
def test_connector_convergence_observes_backoff_and_claims_without_draining(vault, monkeypatch, state):
    module = _acceptance()
    _seeded_store(vault, [("graph", state, 1, time.time() + 60.0, None)])

    def drain(*_args, **_kwargs):
        pytest.fail("observer drained the service queue")

    monkeypatch.setattr(module.derived_drain, "drain_once", drain)
    acknowledged = time.monotonic() - 0.1
    elapsed, converged, observation = module._observe_convergence(vault, bound_seconds=0.05, started=acknowledged)
    assert elapsed >= 0.1
    assert converged is False
    assert observation["pending"] == 1
    assert observation["due"] == 0


def test_connector_waits_for_service_owned_queue_settlement(vault, monkeypatch):
    module = _acceptance()
    store = _seeded_store(vault, [("graph", "retryable", 1, time.time() + 60.0, None)])
    observed = threading.Event()
    observe = module._drain_observation

    def service_worker():
        if observed.wait(timeout=2.0):
            with sqlite3.connect(store) as connection:
                connection.execute("UPDATE derived_batch_components SET state = 'completed'")

    def observation(root, **kwargs):
        result = observe(root, **kwargs)
        observed.set()
        return result

    monkeypatch.setattr(module, "_drain_observation", observation)
    worker = threading.Thread(target=service_worker)
    worker.start()
    try:
        elapsed, converged, report = module._observe_convergence(vault, bound_seconds=2.0)
    finally:
        worker.join(timeout=2.0)

    assert converged is True
    assert report["observations"] >= 2
    assert report["pending"] == report["due"] == report["claimed"] == 0
    assert elapsed < 2.0


@pytest.mark.parametrize("store_state", ["current", "old-schema", "missing"])
def test_connector_receipt_counts_never_repair_or_create_the_observed_store(vault, tmp_path, monkeypatch, store_state):
    module = _acceptance()
    store = _seeded_store(vault, [("graph", "retryable", 1, time.time() + 60.0, None)])
    with sqlite3.connect(store) as connection:
        connection.execute("INSERT INTO full_upserts VALUES ('../outside.md', 1, 1, 1)")
        connection.execute("UPDATE derived_batches SET state = 'reconcile_required'")
        if store_state == "old-schema":
            connection.execute("DROP TABLE write_advisory_result_candidates")

    def snapshot():
        with sqlite3.connect(store) as connection:
            return {
                "schema": connection.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall(),
                "full": connection.execute("SELECT * FROM full_upserts").fetchall(),
                "batches": connection.execute("SELECT * FROM derived_batches").fetchall(),
                "components": connection.execute("SELECT * FROM derived_batch_components").fetchall(),
            }

    before = snapshot()

    def product_reader(*_args, **_kwargs):
        pytest.fail("connector accounting used a product reader that can repair state")

    for owner, name in (
        (module.deferred_index, "_connect"), (module.deferred_index, "_connect_readonly"),
        (module.deferred_index, "list_full_paths"), (module.derived_receipts, "_connect_receipt_read"),
        (module.derived_receipts, "stranded_batch_count"), (module.derived_receipts, "due_component_count"),
    ):
        monkeypatch.setattr(owner, name, product_reader)
    missing = tmp_path / "missing.sqlite"
    if store_state == "missing":
        monkeypatch.setattr(module.deferred_index, "store_path", lambda _root: missing)

    full = module._full_receipt_count(vault, read_only=True)
    stranded = module._reconciliation_demanded(vault, read_only=True)
    queue = module._drain_observation(vault, read_only=True)

    if store_state == "current":
        assert full == 1
        assert stranded == 1
        assert queue["due"] == 0
        assert queue["pending"] == 1
    else:
        assert full == stranded == queue["pending"] == queue["due"] == -1
    assert snapshot() == before
    assert not missing.exists()



def test_external_contract_uses_the_shared_validator_and_reports_its_bytes(tmp_path: Path) -> None:
    import hashlib

    module = _acceptance()
    original, _ = module._load_contract()
    document = original.model_dump(mode="json")
    document["revision"] += "-matched-trial"
    contract_file = tmp_path / "contract.json"
    source = json.dumps(document).encode()
    contract_file.write_bytes(source)
    contract, digest = module._load_contract(contract_file)
    assert contract.revision == document["revision"]
    assert digest == hashlib.sha256(source).hexdigest()
    assert contract.bar("ordinary-write-warm") == original.bar("ordinary-write-warm")
    module.check(_report(), contract=contract)


def test_failed_remember_skips_only_its_edit_and_preserves_independent_timings(
    vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _acceptance()
    real_call = module._DirectTransport.call

    def fail_remember(self, name, arguments):
        if name == "remember":
            raise ValueError("sensitive diagnostic must stay private")
        return real_call(self, name, arguments)

    monkeypatch.setattr(module._DirectTransport, "call", fail_remember)
    report = module.measure(
        vault, transport="direct", samples_per_operation=1,
        state_dir=tmp_path / "acceptance-state", package_mode="source",
        convergence_bound_seconds=60,
    )
    assert report["operations"]["remember"]["samples"] == 1
    assert report["operations"]["edit_memory"]["samples"] == 0
    assert report["operations"]["edit_memory"]["p95_ms"] is None
    for operation in ("record_memory", "plan_memory"):
        assert report["operations"][operation]["samples"] == 1
    for case in report["cases"]:
        if case["operation"] == "remember":
            assert case["terminal_outcome"] == "error"
            assert case["elapsed_ms"] >= 0
        if case["operation"] == "edit_memory":
            assert case["terminal_outcome"] == "skipped"
            assert case["elapsed_ms"] is None
    assert "sensitive diagnostic" not in json.dumps(report)
    with pytest.raises(SystemExit, match="failed or skipped"):
        module.check(report)


def test_a_healthy_write_report_passes_acceptance() -> None:
    _acceptance().check(_report())


@pytest.mark.parametrize("visibility", ["stale", "unknown"])
def test_failed_visibility_prevents_write_acceptance(visibility: str) -> None:
    report = _report()
    report["read_your_write"][visibility] = 1
    with pytest.raises(SystemExit, match="read"):
        _acceptance().check(report)


@pytest.mark.parametrize("receipts", [1, -1])
def test_uncovered_or_unknown_custody_prevents_write_acceptance(receipts: int) -> None:
    report = _report()
    report["uncovered_full_receipts"] = receipts
    with pytest.raises(SystemExit, match="uncovered full receipt"):
        _acceptance().check(report)


def test_reconciliation_demand_prevents_write_acceptance() -> None:
    report = _report()
    report["reconciliation_demanded"] = 1
    with pytest.raises(SystemExit, match="reconciliation demand"):
        _acceptance().check(report)


def test_missing_samples_prevent_write_acceptance() -> None:
    report = _report()
    report["operations"]["remember"]["samples"] = 29
    with pytest.raises(SystemExit, match="samples"):
        _acceptance().check(report)


def test_a_fast_refusal_prevents_write_acceptance() -> None:
    report = _report()
    report["cases"] = [{"terminal_outcome": "refused", "elapsed_ms": 1.0}]
    with pytest.raises(SystemExit, match="failed or skipped"):
        _acceptance().check(report)


def test_write_acceptance_requires_an_explicit_vault() -> None:
    with pytest.raises(SystemExit, match="requires an explicit --vault"):
        _acceptance().main(["--transport", "direct"])


def test_connector_acceptance_requires_its_endpoint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("EXOMEM_CONNECTOR_URL", raising=False)
    with pytest.raises(SystemExit, match="connector transport requires"):
        _acceptance().main(["--transport", "connector", "--vault", str(tmp_path)])
