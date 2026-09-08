from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from lme import native_pilot as pilot


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    source = json.loads(Path("benchmarks/lme/fixtures/leaky.json").read_text())[:1]
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps(source))
    product = tmp_path / "product"
    scaffold = product / "src/exomem/_scaffold/_Schema"
    scaffold.mkdir(parents=True)
    (scaffold / "SKILL.md").write_text("Use the memory system's normal guidance.")
    (product / "docs").mkdir()
    (product / "docs/prominence.md").write_text(
        "### Maximal — web\n\n```\nUse durable memory. CAPTURE-POLICY-SENTINEL\n```\n"
    )
    (product / "src/exomem/__init__.py").write_text("__version__ = 'fixture'\n")
    monkeypatch.setattr(pilot, "_judge_source", lambda _: (b"# offline fixture judge\n", {"commit_sha": "b" * 40}))
    return dataset, product


def prepare(inputs, out):
    dataset, product = inputs
    return pilot.prepare_native(dataset, Path("fixture-judge"), out, product_root=product, profile="fixture", size=1, budget_cap_usd=2)


def canonical_inputs(inputs, tmp_path, monkeypatch):
    dataset, product = inputs
    template = json.loads(dataset.read_text())[0]
    rows = []
    for index in range(25):
        row = dict(template)
        row["question_id"] = f"canonical-{index:02d}"
        rows.append(row)
    dataset.write_bytes(pilot._json(rows))
    source = {
        "repository": "fixture/longmemeval",
        "revision": "a" * 40,
        "filename": "dataset.json",
        "sha256": pilot._sha(dataset.read_bytes()),
        "byte_count": len(dataset.read_bytes()),
        "row_count": 25,
        "type_census": {"single-session-user": 25},
        "abstention_count": 0,
    }
    order = [f"canonical-{index:02d}" for index in reversed(range(25))]
    artifact = {
        "source_identity": source,
        "selection_algorithm_version": "fixture-canonical-v1",
        "selection_algorithm": "fixture reverse order",
        "target_question_ids": order,
    }
    artifact_raw = pilot._json(artifact)
    monkeypatch.setattr(pilot, "CANONICAL_LME_S_SOURCE", source)
    monkeypatch.setattr(pilot, "load_frozen_lme_selection", lambda: (artifact, artifact_raw))

    def select(census, *, source):
        assert source == artifact["source_identity"]
        assert [row["question_id"] for row in census] == [f"canonical-{index:02d}" for index in range(25)]
        return artifact

    monkeypatch.setattr(pilot, "select_lme_s_25", select)
    return dataset, product, artifact, artifact_raw


def test_native_preparation_blinds_writer_and_preserves_every_session(inputs, tmp_path):
    out = tmp_path / "run"
    info = prepare(inputs, out)
    writer = json.loads((out / "writers/case-0001.json").read_text())
    raw = (out / "writers/case-0001.json").read_text()
    assert len(writer["sessions"]) == 2
    assert "Cobalt" in raw  # True history remains even when it contains the answer.
    for hidden in ("answer_3b7c9", "answer_session_ids", "question_type", "leaky-city", "Which signal was selected"):
        assert hidden not in raw
    assert info["publishable"] is False
    assert info["session_count"] == 2
    assert not (out / "execution").exists()
    with pytest.raises(FileExistsError):
        prepare(inputs, out)


def test_native_fresh_selection_remains_the_default(inputs, tmp_path):
    out = tmp_path / "run"
    prepare(inputs, out)
    plan = json.loads((out / "native-plan.json").read_text())
    assert plan["selection"]["mode"] == "fresh"
    assert plan["selection"]["cohort"] == "fresh holdout excluding the prior-inspected canonical cohort"


@pytest.mark.parametrize("selection_mode", ["unknown", "canonical"])
def test_native_preparation_refuses_unknown_selection_mode(inputs, tmp_path, selection_mode):
    dataset, product = inputs
    with pytest.raises(ValueError, match="selection mode"):
        pilot.prepare_native(dataset, Path("fixture-judge"), tmp_path / "run",
            product_root=product, profile="fixture", size=1, budget_cap_usd=2,
            selection_mode=selection_mode)
    assert not (tmp_path / "run").exists()


def test_native_canonical_selection_requires_size_25(inputs, tmp_path):
    dataset, product = inputs
    with pytest.raises(ValueError, match="size 25"):
        pilot.prepare_native(dataset, Path("fixture-judge"), tmp_path / "run",
            product_root=product, profile="fixture", size=7, budget_cap_usd=2,
            selection_mode="canonical25")
    assert not (tmp_path / "run").exists()


def test_native_canonical_selection_requires_exact_pinned_source(inputs, tmp_path):
    dataset, product = inputs
    with pytest.raises(ValueError, match="pinned official dataset"):
        pilot.prepare_native(dataset, Path("fixture-judge"), tmp_path / "run",
            product_root=product, profile="fixture", size=25, budget_cap_usd=2,
            selection_mode="canonical25")
    assert not (tmp_path / "run").exists()


def test_native_canonical_selection_preserves_order_and_source_only_histories(
    inputs, tmp_path, monkeypatch,
):
    dataset, product, artifact, artifact_raw = canonical_inputs(inputs, tmp_path, monkeypatch)
    out = tmp_path / "run"
    info = pilot.prepare_native(dataset, Path("fixture-judge"), out,
        product_root=product, profile="fixture", size=25, budget_cap_usd=2,
        selection_mode="canonical25")
    plan = pilot.validate_native_run(out, expected_plan_sha256=info["plan_sha256"])
    assert [case["question_id"] for case in plan["cases"]] == artifact["target_question_ids"]
    stable_metadata = {key: value for key, value in plan["selection"].items()
                       if not key.startswith("source_census_")}
    assert stable_metadata == {
        "mode": "canonical25",
        "cohort": "prior-inspected canonical LongMemEval-S 25-case cohort",
        "artifact_path": "benchmarks/equivalence/subsets/lme-s-25.json",
        "artifact_sha256": pilot._sha(artifact_raw),
        "algorithm_version": "fixture-canonical-v1",
        "algorithm": "fixture reverse order",
        "source_identity": artifact["source_identity"],
    }
    assert plan["selection"]["source_census_path"] == "selection/source-census.json"
    assert plan["selection"]["source_census_sha256"] == pilot._sha(
        (out / "selection/source-census.json").read_bytes()
    )
    assert (out / "selection/lme-s-25.json").read_bytes() == artifact_raw
    for case in plan["cases"]:
        writer = json.loads((out / case["writer_path"]).read_text())
        assert len(writer["sessions"]) == 2
        assert "question_type" not in json.dumps(writer)


def test_native_canonical_tampered_cohort_refuses_before_backend(
    inputs, tmp_path, monkeypatch,
):
    dataset, product, _, _ = canonical_inputs(inputs, tmp_path, monkeypatch)
    out = tmp_path / "run"
    pilot.prepare_native(dataset, Path("fixture-judge"), out,
        product_root=product, profile="fixture", size=25, budget_cap_usd=2,
        selection_mode="canonical25")
    plan = json.loads((out / "native-plan.json").read_text())
    plan["cases"] = list(reversed(plan["cases"]))
    raw = pilot._json(plan)
    (out / "native-plan.json").write_bytes(raw)
    monkeypatch.setattr(pilot, "_make_backend", lambda *a, **k: pytest.fail("tampered cohort reached backend"))
    with pytest.raises(ValueError, match="canonical.*order"):
        pilot.execute_native(out, expected_plan_sha256=pilot._sha(raw), approval_token="offline test")
    assert not (out / "execution").exists()


def test_native_freezes_documented_hookless_instructions(inputs, tmp_path):
    out = tmp_path / "run"
    info = prepare(inputs, out)
    assert (out / "custom-instructions.md").read_text() == "Use durable memory. CAPTURE-POLICY-SENTINEL\n"
    _, product = inputs
    (product / "docs/prominence.md").write_text("Changed after preparation")
    plan = pilot.validate_native_run(out, expected_plan_sha256=info["plan_sha256"])
    assert "custom-instructions.md" in plan["artifacts"]
    assert "product/docs/prominence.md" in plan["artifacts"]


def test_native_missing_hookless_contract_refuses_preparation(inputs, tmp_path):
    _, product = inputs
    (product / "docs/prominence.md").write_text("No published instruction block")
    with pytest.raises(ValueError, match="Maximal"):
        prepare(inputs, tmp_path / "run")
    assert not (tmp_path / "run").exists()


def test_changed_evaluator_fields_cannot_change_writer_inputs(inputs, tmp_path):
    prepare(inputs, tmp_path / "one")
    dataset, _ = inputs
    data = json.loads(dataset.read_text())
    data[0].update(question="A different future question?", answer="GOLD-SENTINEL", answer_session_ids=[])
    dataset.write_text(json.dumps(data))
    prepare(inputs, tmp_path / "two")
    assert (tmp_path / "one/writers/case-0001.json").read_bytes() == (tmp_path / "two/writers/case-0001.json").read_bytes()


def test_changed_prepared_input_refuses_before_runtime_or_spend(inputs, tmp_path, monkeypatch):
    out = tmp_path / "run"
    info = prepare(inputs, out)
    (out / "writers/case-0001.json").write_text("{}")
    monkeypatch.setattr(pilot, "_make_backend", lambda *a, **k: pytest.fail("tampered input reached backend"))
    with pytest.raises(ValueError, match="digest"):
        pilot.validate_native_run(out, expected_plan_sha256=info["plan_sha256"])
    assert not (out / "execution").exists()


def test_paid_native_preparation_requires_pinned_official_dataset(inputs, tmp_path):
    dataset, product = inputs
    with pytest.raises(ValueError, match="pinned official dataset"):
        pilot.prepare_native(dataset, Path("fixture-judge"), tmp_path / "run", product_root=product, profile="semantic", size=1, budget_cap_usd=2)


def test_original_source_changes_do_not_change_frozen_product(inputs, tmp_path):
    out = tmp_path / "run"
    info = prepare(inputs, out)
    _, product = inputs
    original = (out / "product/src/exomem/__init__.py").read_bytes()
    (product / "src/exomem/__init__.py").write_text("raise RuntimeError('changed')\n")
    assert (out / "product/src/exomem/__init__.py").read_bytes() == original
    assert pilot.validate_native_run(out, expected_plan_sha256=info["plan_sha256"])["schema"] == "lme-native-pilot.v1"
    assert hashlib.sha256((out / "native-plan.json").read_bytes()).hexdigest() == info["plan_sha256"]


def test_runtime_drift_refuses_before_spend(inputs, tmp_path, monkeypatch):
    out = tmp_path / "run"
    info = prepare(inputs, out)
    monkeypatch.setattr(pilot, "_runtime_identity", lambda _: {"changed": True})
    with pytest.raises(ValueError, match="runtime changed"):
        pilot.validate_native_run(out, expected_plan_sha256=info["plan_sha256"])


def test_fixture_plan_cannot_be_used_for_paid_execution(inputs, tmp_path, monkeypatch):
    out = tmp_path / "run"
    info = prepare(inputs, out)
    monkeypatch.setattr(pilot, "_make_backend", lambda *a, **k: pytest.fail("fixture reached paid backend"))
    with pytest.raises(ValueError, match="fixture preparations"):
        pilot.execute_native(out, expected_plan_sha256=info["plan_sha256"], approval_token="offline test")


def test_source_overlap_preserved_but_harness_injection_rejected(inputs, tmp_path):
    dataset, _ = inputs
    data = json.loads(dataset.read_text())
    question = "Which exact signal did we choose for our launch today?"
    data[0]["question"] = question
    data[0]["haystack_sessions"][0][0]["content"] += " " + question
    dataset.write_text(json.dumps(data))
    out = tmp_path / "run"
    info = prepare(inputs, out)
    plan = pilot.validate_native_run(out, expected_plan_sha256=info["plan_sha256"])
    assert plan["cases"][0]["source_probe_overlap_ordinals"] == [1]
    writer_path = out / "writers/case-0001.json"
    writer = json.loads(writer_path.read_text())
    assert question in json.dumps(writer)
    writer["evaluator_question"] = question
    writer_path.write_bytes(pilot._json(writer))
    plan["artifacts"]["writers/case-0001.json"] = hashlib.sha256(writer_path.read_bytes()).hexdigest()
    plan_bytes = pilot._json(plan)
    (out / "native-plan.json").write_bytes(plan_bytes)
    with pytest.raises(ValueError, match="source-only"):
        pilot.validate_native_run(out, expected_plan_sha256=hashlib.sha256(plan_bytes).hexdigest())


def test_writer_orders_chronologically_without_deduplicating_occurrences(inputs):
    from dataclasses import replace

    from lme.dataset import load_dataset
    dataset, _ = inputs
    sessions = load_dataset(dataset).questions[0].sessions
    a, b = sessions
    ordered = pilot._writer_input((b, a, replace(a, session_id="answer-marked-duplicate")))
    assert len(ordered["sessions"]) == 3
    assert [s["ordinal"] for s in ordered["sessions"]] == [2, 3, 1]
    assert ordered["sessions"][0]["messages"] == ordered["sessions"][1]["messages"]
    assert "answer-marked-duplicate" not in json.dumps(ordered)


@pytest.mark.parametrize("budget,expected", [(20, "completed"), (1, "incomplete")])
def test_native_no_write_and_exhausted_outcomes_are_honest(inputs, tmp_path, budget, expected):
    import asyncio
    from dataclasses import replace
    from types import SimpleNamespace

    from lme.native_agent import AgentLimits

    class EmptyCell:
        schemas = {}
        runtime_receipt = {"fixture": True}
        bootstrap = {}

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def readiness(self):
            return {"semantic_verified": False, "fallback_detected": True}

        def snapshot(self):
            return {"stored_bytes": 0, "files": {}, "compiled_count": 0, "raw_count": 0}

    class NoWrite:
        async def complete_messages(self, *args, **kwargs):
            return SimpleNamespace(message={"role": "assistant", "content": "I don't know."}, input_tokens=10, output_tokens=5, cost_usd=0)

    out = tmp_path / "run"
    prepare(inputs, out)
    plan = json.loads((out / "native-plan.json").read_text())
    plan["limits"] = __import__("dataclasses").asdict(replace(AgentLimits(), run_model_calls=budget))
    execution = out / "execution"
    execution.mkdir()
    rows, envelope = asyncio.run(pilot.run_native_cases(plan, execution, pilot._snapshots(out, plan), backend=NoWrite(), python=Path(__import__("sys").executable), cell_factory=EmptyCell))
    row = rows[0]
    assert row["status"] == expected
    assert not [c for p in row["phases"] for c in p["evidence"]["committed_receipts"]]
    if expected == "completed":
        assert row["hypothesis"] == "I don't know."
        assert row["semantic_verified"] is False
        assert row["after_writing"]["compiled_count"] == 0
    else:
        assert "budget" in envelope.stopped
        assert "hypothesis" not in row


@pytest.mark.parametrize("agent_model", ["gpt-4o-2024-08-06", "gpt-5.6-sol"])
def test_native_execution_uses_capped_async_judge_and_reports_complete_denominator(inputs, tmp_path, monkeypatch, agent_model):
    from types import SimpleNamespace

    from lme.dataset import load_dataset_bytes
    from lme.native_agent import AgentLimits, RunEnvelope

    out = tmp_path / "run"
    info = prepare(inputs, out)
    plan = pilot.validate_native_run(out, expected_plan_sha256=info["plan_sha256"])
    # The execution seam is isolated here; fixture paid-refusal and full semantic
    # preparation validation are exercised separately.
    plan["profile"] = "semantic"
    plan["model"] = agent_model
    plan["models"] = pilot.model_contract(agent_model, "openrouter")
    monkeypatch.setattr(pilot, "validate_native_run", lambda *a, **k: plan)
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-synthetic-key")
    requests = []

    class Backend:
        async def complete_messages(self, messages, *, tools, max_tokens, model):
            requests.append((messages, tools, max_tokens, model))
            return SimpleNamespace(message={"role": "assistant", "content": "yes"}, input_tokens=30, output_tokens=1)

    monkeypatch.setattr(pilot, "_make_backend", lambda *a, **k: Backend())

    async def answered(plan, execution, snapshots, **kwargs):
        evaluator = load_dataset_bytes(snapshots["evaluator.json"])
        return [{"status": "completed", "question_id": evaluator.questions[0].question_id, "hypothesis": "Cobalt"}], RunEnvelope(AgentLimits())

    def judge(script, hypotheses, dataset, backend, **kwargs):
        result = backend.complete("Unmodified official judge prompt", max_tokens=10)
        assert result.response == "yes"
        row = json.loads(kwargs["hypothesis_bytes"].splitlines()[0])
        row["autoeval_label"] = {"label": True}
        return (json.dumps(row) + "\n").encode()

    monkeypatch.setattr(pilot, "run_native_cases", answered)
    monkeypatch.setattr(pilot, "_run_judge", judge)
    result = pilot.execute_native(out, expected_plan_sha256=info["plan_sha256"], approval_token="offline test")
    assert result["status"] == "complete", result
    assert result["accuracy"] == 1
    assert result["publishable"] is False
    assert result["envelope"]["tokens"] == 31
    assert requests == [([{"role": "user", "content": "Unmodified official judge prompt"}], [], 10, "gpt-4o-2024-08-06")]


def test_model_asset_tamper_refuses_prepared_run_before_spend(inputs, tmp_path, monkeypatch):
    model = tmp_path / "model"
    revision = "a" * 40
    snapshot = model / "snapshots" / revision
    snapshot.mkdir(parents=True)
    (model / "refs").mkdir()
    (model / "refs/main").write_text(revision)
    (snapshot / "config.json").write_text("{}")
    (snapshot / "model.safetensors").write_bytes(b"offline fixture weights")
    dataset, product = inputs
    out = tmp_path / "run"
    info = pilot.prepare_native(dataset, Path("fixture-judge"), out, product_root=product,
                                profile="fixture", budget_cap_usd=1, model_cache=model)
    frozen_weight = out / "model-cache/snapshots" / revision / "model.safetensors"
    frozen_weight.write_bytes(b"changed fixture weights")
    monkeypatch.setattr(pilot, "_make_backend", lambda *a, **k: pytest.fail("changed model reached spending"))
    with pytest.raises(ValueError, match="model assets changed"):
        pilot.execute_native(out, expected_plan_sha256=info["plan_sha256"], approval_token="offline test")
    assert not (out / "execution").exists()


def test_native_freezes_role_models_and_rejects_pricing_drift(inputs, tmp_path):
    dataset, product = inputs
    out = tmp_path / "run"
    info = pilot.prepare_native(dataset, Path("fixture-judge"), out, product_root=product,
        profile="fixture", budget_cap_usd=2, agent_model="gpt-5.6-sol")
    plan = pilot.validate_native_run(out, expected_plan_sha256=info["plan_sha256"])
    assert plan["models"]["agent"]["model"] == "gpt-5.6-sol"
    assert plan["models"]["judge"]["model"] == "gpt-4o-2024-08-06"
    plan["models"]["agent"]["input_rate"] = 0
    raw = pilot._json(plan)
    (out / "native-plan.json").write_bytes(raw)
    with pytest.raises(ValueError, match="model contract differs"):
        pilot.validate_native_run(out, expected_plan_sha256=pilot._sha(raw))
