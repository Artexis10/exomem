"""Independent financial accounting probes using the real ledger, no network."""
import pytest
from protocol.budget import BudgetLedger


def test_settled_child_charge_is_not_also_held(tmp_path):
    from membench.utility.runner import _child_totals
    ledger = BudgetLedger(tmp_path, caps={'usd': 2})
    common = {'ts': '2026-09-13T00:00:00Z', 'actor': 'instrument', 'op': 'one-call'}
    ledger.reserve(**common, seq=1, units=.02)
    ledger.commit(**common, seq=2, units=.01)
    ledger.release(**common, seq=3, units=.01)
    committed, held, stopped = _child_totals(ledger)
    assert committed == pytest.approx(.01)
    assert held == 0
    assert not stopped


def test_uncertain_call_retains_only_its_unsettled_reservation(tmp_path):
    from membench.utility.runner import _child_totals
    ledger = BudgetLedger(tmp_path, caps={'usd': 2})
    common = {'ts': '2026-09-13T00:00:00Z', 'actor': 'instrument', 'op': 'one-call'}
    ledger.reserve(**common, seq=1, units=.02)
    ledger.commit(**common, seq=2, units=.01)
    ledger.release(**common, seq=3, units=.01)
    ledger.reserve(**{**common, 'op': 'uncertain-call'}, seq=4, units=.02)
    ledger.stop_path.write_text('unknown transport charge\n')
    committed, held, stopped = _child_totals(ledger)
    assert committed == pytest.approx(.01)
    assert held == pytest.approx(.02)
    assert stopped


# --------------------------------------------------------------------------
# Parent/child settlement under faults, and one-sided arm retention.
# --------------------------------------------------------------------------

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from membench.utility.schema import VARIANTS


def _ts() -> str:
    return datetime.now(UTC).isoformat()


class _FakeCell:
    schemas = {
        "bootstrap": {"description": "Bootstrap.", "inputSchema": {"type": "object"}},
        "ask_memory": {"description": "Recall memories.", "inputSchema": {"type": "object"}},
        "remember": {"description": "Write compiled memory.", "inputSchema": {"type": "object"}},
    }

    async def call(self, name, arguments):
        return {"structuredContent": {"result": "ok"}}

    def snapshot(self):
        return {"stored_bytes": 0, "files": {}}

    async def readiness(self):
        return {"status": "ready_lexical_only", "profile": "fixture"}

    runtime_receipt = {"profile": "fixture"}


class _CellCM:
    def __init__(self, root, *, fail=False, **kwargs):
        self.root, self.fail = root, fail

    async def __aenter__(self):
        if self.fail:
            raise RuntimeError("cell entry failed")
        return _FakeCell()

    async def __aexit__(self, *exc):
        return None


class _Backend:
    """Reserve/commit/release on a real ledger, like the metered backend."""

    def __init__(self, root, cap_usd, token, *, reserve=0.02, actual=0.01,
                 stop_on_call=None, overspend=False):
        self.ledger = BudgetLedger(root, caps={"usd": cap_usd})
        self.cap_usd = cap_usd
        self._seq = 0
        self._reserve, self._actual = reserve, actual
        self._stop_on_call, self._overspend = stop_on_call, overspend
        self.calls = 0
        self.ledger.approve(ts=_ts(), seq=self._next(), actor="fake", op="approval")

    def _next(self):
        self._seq += 1
        return self._seq

    async def complete_messages(self, messages, *, tools, max_tokens):
        self.calls += 1
        self.ledger.reserve(ts=_ts(), seq=self._next(), actor="fake", op="call", units=self._reserve)
        if self._stop_on_call == self.calls:
            self.ledger.stop_path.write_text("unknown transport charge\n", encoding="utf-8")
            raise RuntimeError("uncertain failure")
        actual = self.cap_usd * 2 if self._overspend else self._actual
        self.ledger.commit(ts=_ts(), seq=self._next(), actor="fake", op="call", units=actual)
        self.ledger.release(ts=_ts(), seq=self._next(), actor="fake", op="call",
                            units=self._reserve - self._actual)
        return SimpleNamespace(message={"role": "assistant", "content": "Done."},
                               input_tokens=10, output_tokens=5, cost_usd=actual)


def _product_root(tmp_path: Path) -> Path:
    schema = tmp_path / "product" / "src" / "exomem" / "_scaffold" / "_Schema"
    (schema / "references").mkdir(parents=True)
    (schema / "SKILL.md").write_text("Skill. See [writing](references/writing.md).", encoding="utf-8")
    (schema / "references" / "writing.md").write_text("WRITING", encoding="utf-8")
    return tmp_path / "product"


def _run(tmp_path, *, backend_factory, cell_factory, variants=VARIANTS):
    from membench.utility.runner import run_utility

    product_root = _product_root(tmp_path)
    return asyncio.run(run_utility(
        tmp_path / "run", seed=1, product_root=product_root,
        python=Path(__import__("sys").executable), tokenizer_path=product_root / "src" / "exomem" / "_scaffold" / "_Schema" / "SKILL.md",
        profile="fixture", cap_usd=2.0, paid=True, approval_token="operator-approved",
        phase_seconds=5, variants=variants,
        identity_provider=lambda root: "fake-identity",
        family_gate=lambda identity, families: None,
        cell_factory=cell_factory, backend_factory=backend_factory,
    ))


def _parent(tmp_path) -> BudgetLedger:
    return BudgetLedger(tmp_path / "run" / "ledger", caps={"usd": 2.0})


def test_parent_settles_child_charges_when_the_cell_fails_to_start(tmp_path):
    """A failed arm still charged model calls; the parent may not hold the
    whole reservation on top of them, nor lose the run report."""
    from membench.utility.runner import _child_totals

    backends = {}

    def backend_factory(root, cap, token):
        backends[root] = _Backend(root, cap, token)
        return backends[root]

    report = _run(tmp_path, backend_factory=backend_factory,
                  cell_factory=lambda root, **kw: _CellCM(root, fail=True),
                  variants=("helpful_history",))
    child = next(iter(backends.values()))
    child_committed, child_held, _ = _child_totals(child.ledger)
    parent_committed, parent_held, _ = _child_totals(_parent(tmp_path))
    assert parent_committed == pytest.approx(child_committed)
    assert parent_held == pytest.approx(child_held)
    assert (tmp_path / "run" / "report.json").is_file()
    assert report["scores"]["helpful_history"]["arms"]["memory"]["attempted"] >= 0


def test_one_sided_attempt_keeps_the_arm_that_ran(tmp_path):
    """The control ran and was graded; only the memory arm is missing."""
    def cell_factory(root, **kwargs):
        return _CellCM(root, fail=True)

    report = _run(tmp_path, backend_factory=lambda root, cap, token: _Backend(root, cap, token),
                  cell_factory=cell_factory, variants=("helpful_history",))
    arms = report["scores"]["helpful_history"]["arms"]
    attempted = {arm: arms[arm]["attempted"] for arm in ("control", "memory")}
    assert attempted["control"] == 1, "an attempted, graded arm must never be discarded"
    assert arms["memory"]["missing"] + arms["memory"]["invalid"] == 1
    assert report["scores"]["helpful_history"]["pair"]["attempted"] == 1


def test_settled_spend_is_never_counted_twice_across_pairs(tmp_path):
    from membench.utility.runner import _child_totals

    children = []

    def backend_factory(root, cap, token):
        child = _Backend(root, cap, token)
        children.append(child)
        return child

    report = _run(tmp_path, backend_factory=backend_factory,
                  cell_factory=lambda root, **kw: _CellCM(root))
    total_child = sum(_child_totals(c.ledger)[0] for c in children)
    parent_committed, parent_held, _ = _child_totals(_parent(tmp_path))
    assert parent_committed == pytest.approx(total_child)
    assert parent_held == 0, "fully settled pairs must release their whole reservation"
    assert report["spend"]["committed_usd"] == pytest.approx(total_child)
    assert report["spend"]["held_usd"] == 0


def test_correct_state_cannot_turn_a_declared_budget_failure_into_a_pass(tmp_path, monkeypatch):
    from membench.utility import runner

    async def phase(broker, *, view, out):
        target = broker.world._episode.oracle.current_state
        broker.world.call("apply_config", {"project": target.project,
                         "steps": list(target.steps), "constraint": target.constraint})
        failed = broker.arm == "memory" and view.phase == "action"
        return {"status": "incomplete" if failed else "completed",
                "outcome_class": "declared_exhaustion" if failed else "completed",
                "reason": "phase wall-clock budget exhausted" if failed else None}

    monkeypatch.setattr(runner, "run_phase", phase)
    report = _run(tmp_path, backend_factory=lambda root, cap, token: _Backend(root, cap, token),
                  cell_factory=lambda root, **kw: _CellCM(root), variants=("helpful_history",))
    assert report["scores"]["helpful_history"]["pair"]["losses"] == 1
    from tests.test_membench_utility_report import _load
    assert _load(tmp_path / "run", tmp_path / "product")["recomputed"]["scores"] == report["scores"]


def test_backend_construction_failure_releases_the_uncalled_pair(tmp_path):
    def broken(*args):
        raise RuntimeError("construction failed before any call")

    report = _run(tmp_path, backend_factory=broken, cell_factory=lambda root, **kw: _CellCM(root))
    from membench.utility.runner import _child_totals
    assert _child_totals(_parent(tmp_path))[:2] == (0, 0)
    assert report["halted"] is True
    assert report["scores"]["helpful_history"]["pair"]["missing"] == 1


def test_halted_pair_holds_exactly_its_unsettled_reservation(tmp_path):
    from membench.utility.runner import _child_totals

    children = {}

    def backend_factory(root, cap, token):
        stop = 1 if root.parent.name == VARIANTS[1] else None
        children[root.parent.name] = _Backend(root, cap, token, stop_on_call=stop)
        return children[root.parent.name]

    report = _run(tmp_path, backend_factory=backend_factory,
                  cell_factory=lambda root, **kw: _CellCM(root))
    assert report["halted"] is True
    stopped = children[VARIANTS[1]]
    child_committed, child_held, _ = _child_totals(stopped.ledger)
    assert child_held == pytest.approx(0.02), "only the uncertain call stays held"
    parent_committed, parent_held, _ = _child_totals(_parent(tmp_path))
    expected_committed = sum(_child_totals(c.ledger)[0] for c in children.values())
    assert parent_committed == pytest.approx(expected_committed)
    assert parent_held == pytest.approx(child_held)


def test_liability_above_the_pair_envelope_stops_the_run(tmp_path):
    """A child that charges more than its pair reservation must stop the run
    rather than quietly overspend the cap."""
    def backend_factory(root, cap, token):
        return _Backend(root, cap * 4, token, reserve=cap * 3, actual=cap * 3, overspend=True)

    report = _run(tmp_path, backend_factory=backend_factory,
                  cell_factory=lambda root, **kw: _CellCM(root))
    assert report["halted"] is True
    assert report["attempted_variants"] == [VARIANTS[0]]
    assert (tmp_path / "run" / "ledger" / "STOP").exists()
