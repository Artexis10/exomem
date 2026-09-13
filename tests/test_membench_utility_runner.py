"""run_utility: preflight, hierarchical budget, arm rotation, STOP handling."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from protocol.budget import BudgetLedger

from membench.utility.runner import UtilityRunnerError, run_utility
from membench.utility.schema import VARIANTS

PRODUCT_ROOT = None  # patched in fixture


@pytest.fixture(autouse=True)
def _product_root(tmp_path_factory):
    global PRODUCT_ROOT
    root = tmp_path_factory.mktemp("product")
    scaffold = root / "src" / "exomem" / "_scaffold" / "_Schema"
    scaffold.mkdir(parents=True)
    (scaffold / "SKILL.md").write_text("SKILL-SENTINEL", encoding="utf-8")
    (scaffold / "references").mkdir()
    (scaffold / "references" / "writing.md").write_text("WRITING-SENTINEL", encoding="utf-8")
    (scaffold / "SKILL.md").write_text(
        "SKILL-SENTINEL See [writing](references/writing.md).", encoding="utf-8")
    # Frozen path placeholders: the runner refuses paths that do not exist.
    (root / "fake-python").write_text("#!/bin/false\n", encoding="utf-8")
    (root / "fake-tok").write_text("{}", encoding="utf-8")
    PRODUCT_ROOT = root
    yield root


def _now() -> str:
    return datetime.now(UTC).isoformat()


class FakeCell:
    schemas = {
        "bootstrap": {"description": "Bootstrap.", "inputSchema": {"type": "object"}},
        "ask_memory": {"description": "Recall memories.", "inputSchema": {"type": "object"}},
        "remember": {"description": "Write compiled memory.", "inputSchema": {"type": "object"}},
    }

    def __init__(self):
        self.calls = []

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        return {"structuredContent": {"result": "ok"}}

    def snapshot(self):
        return {"stored_bytes": 0, "files": {}}


class FakeCellCM:
    instances: list["FakeCellCM"] = []

    def __init__(self, root, **kwargs):
        self.root = root
        self.kwargs = kwargs
        FakeCellCM.instances.append(self)

    async def __aenter__(self):
        self.cell = FakeCell()
        return self.cell

    async def __aexit__(self, *exc):
        return None


def cell_factory(root, **kwargs):
    return FakeCellCM(root, **kwargs)


class FakeBackend:
    """Mimics MeteredOpenAIBackend's reserve/commit/release discipline on a
    real BudgetLedger, so parent/child settlement math is exercised for
    real."""

    def __init__(self, root, cap_usd, approval_token, *, replies=None, stop_on_call=None):
        self.ledger = BudgetLedger(root, caps={"usd": cap_usd})
        self._seq = 0
        self.ledger.approve(ts=_now(), seq=self._next(), actor="fake-backend", op="approval")
        self._per_call_reserve = 0.02
        self._per_call_actual = 0.01
        self._replies = iter(replies or [])
        self._stop_on_call = stop_on_call
        self._call_count = 0

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    async def complete_messages(self, messages, *, tools, max_tokens):
        self._call_count += 1
        self.ledger.reserve(ts=_now(), seq=self._next(), actor="fake-backend", op="call", units=self._per_call_reserve)
        if self._stop_on_call == self._call_count:
            self.ledger.stop_path.write_text("simulated uncertain failure\n", encoding="utf-8")
            raise RuntimeError("simulated uncertain failure")
        self.ledger.commit(ts=_now(), seq=self._next(), actor="fake-backend", op="call", units=self._per_call_actual)
        self.ledger.release(
            ts=_now(), seq=self._next(), actor="fake-backend", op="call",
            units=self._per_call_reserve - self._per_call_actual,
        )
        try:
            reply = next(self._replies)
        except StopIteration:
            reply = {"role": "assistant", "content": "Done."}
        return SimpleNamespace(message=reply, input_tokens=10, output_tokens=5, cost_usd=self._per_call_actual)


def _identity_provider(product_root):
    return "fake-identity"


def _family_gate_ok(identity, families):
    return None


def _run(tmp_path, *, backend_factory, variants=VARIANTS, cap_usd=2.0, family_gate=None, cell_factory_=None):
    return asyncio.run(run_utility(
        tmp_path / "run", seed=1,
        product_root=PRODUCT_ROOT, python=PRODUCT_ROOT / "fake-python", tokenizer_path=PRODUCT_ROOT / "fake-tok",
        cap_usd=cap_usd, paid=True, approval_token="test-token", phase_seconds=5,
        profile="fixture",
        variants=variants,
        identity_provider=_identity_provider,
        family_gate=family_gate or _family_gate_ok,
        cell_factory=cell_factory_ or cell_factory,
        backend_factory=backend_factory,
    ))


def test_refuses_without_paid(tmp_path):
    with pytest.raises(UtilityRunnerError):
        _coro = run_utility(
            tmp_path / "run", seed=1, product_root=PRODUCT_ROOT, python=PRODUCT_ROOT / "p",
            tokenizer_path=PRODUCT_ROOT / "t", paid=False, approval_token="x",
        )
        asyncio.run(_coro)
    assert not (tmp_path / "run").exists()


def test_refuses_existing_output(tmp_path):
    (tmp_path / "run").mkdir()
    with pytest.raises(UtilityRunnerError):
        asyncio.run(run_utility(
            tmp_path / "run", seed=1, product_root=PRODUCT_ROOT, python=PRODUCT_ROOT / "p",
            tokenizer_path=PRODUCT_ROOT / "t", paid=True, approval_token="x",
        ))


def test_refuses_cap_too_small_for_all_scheduled_pairs(tmp_path):
    with pytest.raises(UtilityRunnerError):
        asyncio.run(run_utility(
            tmp_path / "run", seed=1, product_root=PRODUCT_ROOT, python=PRODUCT_ROOT / "p",
            tokenizer_path=PRODUCT_ROOT / "t", paid=True, approval_token="x", cap_usd=0.01,
            identity_provider=_identity_provider, family_gate=_family_gate_ok,
        ))
    assert not (tmp_path / "run").exists()


def test_pending_family_gate_blocks_before_any_spend(tmp_path):
    def _blocking_gate(identity, families):
        raise RuntimeError("amendment pending")

    with pytest.raises(RuntimeError, match="amendment pending"):
        asyncio.run(run_utility(
            tmp_path / "run", seed=1, product_root=PRODUCT_ROOT,
            python=PRODUCT_ROOT / "fake-python", tokenizer_path=PRODUCT_ROOT / "fake-tok",
            paid=True, approval_token="x",
            identity_provider=_identity_provider, family_gate=_blocking_gate,
        ))
    assert not (tmp_path / "run").exists()


def test_full_paired_run_produces_scores_for_every_variant(tmp_path):
    def backend_factory(root, cap, token):
        return FakeBackend(root, cap, token)

    report = _run(tmp_path, backend_factory=backend_factory)
    assert set(report["scores"]) == set(VARIANTS)
    assert report["attempted_variants"] == list(VARIANTS)
    assert not report["halted"]
    for variant in VARIANTS:
        pair = report["scores"][variant]["pair"]
        assert pair["valid"] == 1
    assert (tmp_path / "run" / "report.json").is_file()


def test_control_arm_never_touches_a_product_cell(tmp_path):
    FakeCellCM.instances.clear()

    def backend_factory(root, cap, token):
        return FakeBackend(root, cap, token)

    _run(tmp_path, backend_factory=backend_factory, variants=("helpful_history",))
    # One memory-arm cell per attempted pair; none for control.
    assert len(FakeCellCM.instances) == 1


def test_arm_order_rotates_by_seed_and_variant_index(tmp_path):
    from membench.utility.runner import _arm_order

    assert _arm_order(1, 0) != _arm_order(1, 1) or _arm_order(1, 0) == ("control", "memory")
    orders = {_arm_order(1, i) for i in range(len(VARIANTS))}
    assert len(orders) == 2  # both rotations occur across three variants


def test_stop_halts_remaining_pairs_and_preserves_partial_results(tmp_path):
    def backend_factory(root, cap, token):
        # The second pair's very first model call reports an uncertain
        # failure and halts; the third pair must never be attempted.
        if root.parent.name == VARIANTS[1]:
            return FakeBackend(root, cap, token, stop_on_call=1)
        return FakeBackend(root, cap, token)

    report = _run(tmp_path, backend_factory=backend_factory)
    assert report["halted"] is True
    assert report["attempted_variants"] == list(VARIANTS[:2])
    assert report["scores"][VARIANTS[0]]["pair"]["valid"] == 1
    assert report["scores"][VARIANTS[2]]["pair"]["scheduled"] == 1
    assert report["scores"][VARIANTS[2]]["pair"]["missing"] == 1
    # Parent ledger retained the held reservation from the stopped pair
    # instead of releasing it as if nothing were spent.
    ledger = BudgetLedger(tmp_path / "run" / "ledger", caps={"usd": 2.0})
    assert ledger._running_total() > 0


def test_reservation_math_matches_documented_ceiling():
    from membench.utility.runner import pair_reservation_usd

    assert pair_reservation_usd(input_rate=0.15, output_rate=0.5) == pytest.approx(0.3936)


def test_reservation_refuses_above_pinned_price_ceiling():
    from membench.utility.runner import pair_reservation_usd, UtilityRunnerError as Err

    with pytest.raises(Err):
        pair_reservation_usd(input_rate=0.20, output_rate=0.5)


# --------------------------------------------------------------------------
# Preflight validation: nothing may be created before every check passes.
# --------------------------------------------------------------------------

def _preflight(tmp_path, **overrides):
    kwargs = dict(
        seed=1, product_root=PRODUCT_ROOT, python=PRODUCT_ROOT / "fake-python",
        tokenizer_path=PRODUCT_ROOT / "fake-tok", paid=True, approval_token="operator-approved",
        profile="fixture", identity_provider=_identity_provider, family_gate=_family_gate_ok,
        cell_factory=cell_factory, backend_factory=lambda root, cap, token: FakeBackend(root, cap, token),
    )
    kwargs.update(overrides)
    return asyncio.run(run_utility(tmp_path / "run", **kwargs))


@pytest.mark.parametrize("bad", [0, -1, 2.5, float("inf"), float("nan"), True, "2", None])
def test_invalid_cap_refuses_before_any_artifact(tmp_path, bad):
    with pytest.raises(UtilityRunnerError):
        _preflight(tmp_path, cap_usd=bad)
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("bad", [0, -1, 181, float("inf"), float("nan"), True, "60"])
def test_invalid_phase_seconds_refuses_before_any_artifact(tmp_path, bad):
    with pytest.raises(UtilityRunnerError):
        _preflight(tmp_path, phase_seconds=bad)
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("bad", [True, 1.5, "1", None])
def test_seed_must_be_a_plain_integer(tmp_path, bad):
    with pytest.raises(UtilityRunnerError):
        _preflight(tmp_path, seed=bad)
    assert not (tmp_path / "run").exists()


def test_unknown_profile_refuses(tmp_path):
    with pytest.raises(UtilityRunnerError):
        _preflight(tmp_path, profile="embeddings")
    assert not (tmp_path / "run").exists()


def test_repeated_variants_refuse(tmp_path):
    with pytest.raises(UtilityRunnerError):
        _preflight(tmp_path, variants=("helpful_history", "helpful_history"))
    assert not (tmp_path / "run").exists()


def test_missing_frozen_paths_refuse(tmp_path):
    with pytest.raises(UtilityRunnerError):
        _preflight(tmp_path, tokenizer_path=tmp_path / "absent-tokenizer.json",
                   python=PRODUCT_ROOT / "fake-python")
    assert not (tmp_path / "run").exists()


def test_cap_must_cover_all_three_pairs_even_for_a_subset_request(tmp_path):
    """A one-variant subset is a synthetic test injection; the protocol's cap
    still has to cover the whole three-pair schedule."""
    with pytest.raises(UtilityRunnerError):
        _preflight(tmp_path, variants=("helpful_history",), cap_usd=0.5)
    assert not (tmp_path / "run").exists()


def test_approval_token_that_looks_like_a_credential_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value-not-a-marker")
    with pytest.raises(UtilityRunnerError):
        _preflight(tmp_path, approval_token="sk-secret-value-not-a-marker")
    assert not (tmp_path / "run").exists()


# --------------------------------------------------------------------------
# Evaluator-only manifest, written before any actor exists.
# --------------------------------------------------------------------------

def _manifest(tmp_path):
    return json.loads((tmp_path / "run" / "manifest.json").read_text(encoding="utf-8"))


def test_manifest_is_written_before_the_first_actor_call(tmp_path):
    seen = {}

    class RecordingBackend(FakeBackend):
        async def complete_messages(self, messages, *, tools, max_tokens):
            seen.setdefault("manifest_existed", (tmp_path / "run" / "manifest.json").is_file())
            return await super().complete_messages(messages, tools=tools, max_tokens=max_tokens)

    _preflight(tmp_path, variants=("helpful_history",),
               backend_factory=lambda root, cap, token: RecordingBackend(root, cap, token))
    assert seen["manifest_existed"] is True


def test_manifest_records_the_frozen_experiment_identity(tmp_path):
    _preflight(tmp_path, variants=("helpful_history",))
    manifest = _manifest(tmp_path)
    assert manifest["manifest_version"] >= 1
    scenario = manifest["scenario"]
    assert scenario["seed"] == 1 and scenario["generator_version"]
    assert set(scenario["variants"]) == {"helpful_history"}
    assert scenario["phase_hashes"]["helpful_history"] and scenario["oracle_hashes"]["helpful_history"]
    product = manifest["product"]
    assert "commit" in product and "tree_clean" in product
    assert product["scaffold_hashes"] and product["guidance_hashes"]
    actor = manifest["actor"]
    for field in ("model", "wire_model", "provider", "effort", "tokenizer", "pricing", "parameters"):
        assert field in actor, field
    assert actor["fallback"] is False
    limits = manifest["limits"]
    assert limits["cap_usd"] == 2.0 and limits["phase_seconds"] and limits["pair_reservation_usd"]
    policy = manifest["policy"]
    assert policy["memory_attentiveness"] and policy["context_policy"]
    assert policy["ordinary_persistence"] and policy["retry_policy"] == "none"
    assert policy["arm_order"]["helpful_history"] in (["control", "memory"], ["memory", "control"])
    assert manifest["tool_schemas"]
    assert manifest["protocol"]["family_id"] == "f32"
    assert manifest["clock"]["started_at"]


def test_injected_fakes_label_the_run_synthetic(tmp_path):
    _preflight(tmp_path, variants=("helpful_history",))
    manifest = _manifest(tmp_path)
    assert manifest["synthetic"] is True
    assert manifest["synthetic_reasons"]
    assert json.loads((tmp_path / "run" / "report.json").read_text())["synthetic"] is True


def test_manifest_never_reaches_an_actor(tmp_path):
    _preflight(tmp_path, variants=("stale_distractor",))
    manifest_text = (tmp_path / "run" / "manifest.json").read_text(encoding="utf-8")
    salt = _manifest(tmp_path)["hash_salt"]
    for path in (tmp_path / "run" / "sessions").rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            assert salt not in text
            assert "oracle" not in text.lower()
            assert "stale_distractor" not in text
            assert "hash_salt" not in text
    assert "stale_distractor" in manifest_text  # evaluator side keeps it


def test_worker_visible_paths_do_not_name_the_variant_or_arm(tmp_path):
    _preflight(tmp_path, variants=("stale_distractor",))
    sessions = tmp_path / "run" / "sessions"
    assert sessions.is_dir()
    for path in sessions.rglob("*"):
        relative = str(path.relative_to(sessions))
        assert "stale_distractor" not in relative
        assert "memory" not in relative and "control" not in relative


# --------------------------------------------------------------------------
# Observed evidence: snapshots, readiness, per-phase usage.
# --------------------------------------------------------------------------

def test_run_saves_observed_world_and_memory_snapshots_per_phase(tmp_path):
    _preflight(tmp_path, variants=("helpful_history",))
    pair = tmp_path / "run" / "pairs" / "helpful_history"
    for arm in ("control", "memory"):
        for phase in ("experience", "change", "action"):
            world = json.loads((pair / arm / f"{phase}-world.json").read_text())
            assert "applied" in world and "write_events" in world
            assert (pair / arm / f"{phase}-result.json").is_file()
    cell = json.loads((pair / "memory" / "cell.json").read_text())
    assert cell["profile"] == "fixture" and cell["semantic_shipped_default"] is False
    assert "memory_snapshot" in cell


def test_report_breaks_usage_down_by_phase_and_keeps_unknowns_unknown(tmp_path):
    report = _preflight(tmp_path, variants=("helpful_history",))
    phases = report["usage_by_phase"]
    assert set(phases) == {"experience", "change", "action"}
    assert phases["action"]["input_tokens"]["total"] is not None
    # Never invented as a zero when the backend does not report it.
    assert phases["action"]["reasoning_tokens"]["total"] is None
    assert report["overhead"]["phase_orchestration_seconds"] is not None
    assert report["overhead"]["outside_phase_seconds"] is not None
    assert report["spend"]["cap_usd"] == 2.0


def test_semantic_profile_requires_verified_cell_readiness(tmp_path):
    class NotReadyCellCM(FakeCellCM):
        async def __aenter__(self):
            cell = await super().__aenter__()
            cell.readiness = lambda: _not_ready()
            return cell

    async def _not_ready():
        return {"status": "not_ready", "profile": "semantic"}

    report = _preflight(tmp_path, variants=("helpful_history",), profile="semantic",
                        cell_factory=lambda root, **kw: NotReadyCellCM(root, **kw))
    arms = report["scores"]["helpful_history"]["arms"]
    assert arms["memory"]["invalid"] == 1
    assert report["scores"]["helpful_history"]["pair"]["valid"] == 0


def test_declared_task_failure_does_not_halt_the_paired_partner(tmp_path):
    """A memory arm that exhausts its declared output budget is a scored task
    failure; the control must still be attempted."""
    from lme.metered import MeteredActorOutputError

    class ExhaustedBackend(FakeBackend):
        async def complete_messages(self, messages, *, tools, max_tokens):
            self._call_count += 1
            self.ledger.reserve(ts=_now(), seq=self._next(), actor="fake-backend", op="call",
                                units=self._per_call_reserve)
            self.ledger.commit(ts=_now(), seq=self._next(), actor="fake-backend", op="call",
                               units=self._per_call_actual)
            self.ledger.release(ts=_now(), seq=self._next(), actor="fake-backend", op="call",
                                units=self._per_call_reserve - self._per_call_actual)
            raise MeteredActorOutputError(
                "completion was truncated at max_tokens", input_tokens=10,
                output_tokens=2000, cost_usd=self._per_call_actual,
            )

    report = _preflight(tmp_path, variants=("helpful_history",),
                        backend_factory=lambda root, cap, token: ExhaustedBackend(root, cap, token))
    arms = report["scores"]["helpful_history"]["arms"]
    assert arms["control"]["attempted"] == 1 and arms["memory"]["attempted"] == 1
    assert arms["control"]["valid"] == 1 and arms["memory"]["valid"] == 1
    assert report["scores"]["helpful_history"]["pair"]["valid"] == 1
    assert report["halted"] is False
    assert report["spend"]["committed_usd"] > 0


def test_infrastructure_fault_invalidates_the_attempt_without_erasing_cost(tmp_path):
    class BrokenBackend(FakeBackend):
        async def complete_messages(self, messages, *, tools, max_tokens):
            self.ledger.reserve(ts=_now(), seq=self._next(), actor="fake-backend", op="call",
                                units=self._per_call_reserve)
            raise ConnectionError("transport failure with ambiguous spend")

    report = _preflight(tmp_path, variants=("helpful_history",),
                        backend_factory=lambda root, cap, token: BrokenBackend(root, cap, token))
    arms = report["scores"]["helpful_history"]["arms"]
    assert arms["control"]["invalid"] == 1
    assert report["scores"]["helpful_history"]["pair"]["valid"] == 0
    assert report["spend"]["held_usd"] > 0
