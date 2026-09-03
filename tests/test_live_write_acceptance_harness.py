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

* Convergence drained in unbounded passes of 32 and only looked at the wall
  clock between them. A single component dispatch on that corpus runs 18-88
  seconds, so one pass ran 99 seconds against a 30-second bound and 315 seconds
  against the design's 300. The bound was not enforced; it was reported after
  the fact.

* A run that failed to converge said only "did not converge". That is the one
  sentence that cannot distinguish the two outcomes an operator has to act on
  differently: a deep backlog that is draining and needs a longer window, and a
  drain that is stuck and needs someone. Convergence now reports what it saw.

None of this is the product's write path, which is why none of it is fixed
here. The product defects the runs exposed -- a claim lease shorter than a
dispatch, the linear component chain, the whole-corpus graph refresh per
fan-out -- are named in the lane's result, not smuggled in as harness changes.
"""

from __future__ import annotations

import importlib.util
import json
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


def _drain(
    *,
    passes: int = 1,
    completed: int = 0,
    failed: int = 0,
    stuck: int = 0,
    stalled_passes: int = 0,
    rate: float = 0.0,
    projected: float | None = None,
) -> dict[str, Any]:
    return {
        "passes": passes,
        "completed": completed,
        "failed": failed,
        "stuck": stuck,
        "stalled_passes": stalled_passes,
        "rate_components_per_min": rate,
        "projected_seconds_to_converge": projected,
    }


def _report(*, converged: bool = True, drain: dict[str, Any] | None = None) -> dict:
    """A report that passes every assertion except the one under test."""
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
        "post_burst_convergence_bound_seconds": 300.0,
        "post_burst_converged": converged,
        "reconciliation_demanded": 0,
        "drain": _drain() if drain is None else drain,
    }


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
# Convergence
# --------------------------------------------------------------------------- #


def test_converge_checks_the_deadline_after_every_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass is bounded by the product's own allowance and then re-checked.

    Draining 32 blind is not the scheduler's number in any mode; it was the
    harness's. On the measured corpus one component runs 18-88s, so a pass of
    32 is a 10-to-45-minute block during which no deadline exists. The bound is
    only a bound if the loop stops at the first pass that crosses it.
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
        # contrived one: 99s of drain against a 30s bound.
        time.sleep(0.35)
        return 1

    monkeypatch.setattr(module.derived_drain, "drain_once", fake_drain_once)
    monkeypatch.setattr(
        module.derived_receipts, "due_component_count", lambda root, **kw: 7
    )

    seconds, converged, drain = module._converge(tmp_path, bound_seconds=0.1)

    assert converged is False
    assert not overrun, overrun
    assert limits == [module.derived_drain.progress_limit()]
    assert limits != [32], "32 is the performance-mode ceiling, not a default"
    assert drain["passes"] == 1, "the deadline had already passed after pass 1"
    assert seconds >= 0.35


def test_converge_reports_a_stuck_claim_distinct_from_a_deep_backlog() -> None:
    """"Did not converge" is the one answer an operator cannot act on.

    A deep backlog that is draining wants a longer window and a note about
    dispatch cost. A stuck drain wants a person. The failure has to say which.
    """
    module = _acceptance()

    stuck = _report(
        converged=False,
        drain=_drain(
            passes=4,
            completed=3,
            failed=1,
            stuck=2,
            rate=1.5,
            projected=1_760.0,
        ),
    )
    with pytest.raises(SystemExit) as stuck_exit:
        module.check(stuck)
    stuck_message = str(stuck_exit.value)
    assert "drain stuck" in stuck_message
    assert "2 component(s)" in stuck_message and "1 failed" in stuck_message
    assert "deep backlog" not in stuck_message

    backlog = _report(
        converged=False,
        drain=_drain(passes=4, completed=9, rate=2.4, projected=2_700.0),
    )
    with pytest.raises(SystemExit) as backlog_exit:
        module.check(backlog)
    backlog_message = str(backlog_exit.value)
    assert "deep backlog draining at 2.4 component(s)/min" in backlog_message
    assert "2700" in backlog_message
    assert "stuck" not in backlog_message

    # Zero progress across two passes is its own stuck: nothing failed, nothing
    # is holding a lease, and nothing is moving either.
    stalled = _report(
        converged=False,
        drain=_drain(passes=2, completed=0, stalled_passes=2),
    )
    with pytest.raises(SystemExit, match="drain stuck"):
        module.check(stalled)

    # And a report from before this block existed still fails, and still says
    # the plain thing -- the acceptance never silently passes for want of a key.
    legacy = _report(converged=False)
    legacy.pop("drain")
    with pytest.raises(SystemExit, match="did not converge"):
        module.check(legacy)


def test_the_drain_block_is_content_free(
    vault: Path, tmp_path: Path
) -> None:
    """Counts, codes and rates -- the drain block names no batch and no path."""
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
    assert set(drain) == {
        "passes",
        "completed",
        "failed",
        "stuck",
        "stalled_passes",
        "rate_components_per_min",
        "projected_seconds_to_converge",
    }
    rendered = json.dumps(report, sort_keys=True, default=str)
    for token in ("Knowledge Base", str(vault), "acceptance-", ".md"):
        assert token not in rendered, (token, rendered)
