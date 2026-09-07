from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest
from lme.dataset import QUESTION_TYPES, LmeDataset, load_dataset


def cohort():
    question = load_dataset("benchmarks/lme/fixtures/mini.json").questions[0]
    questions = [
        dataclasses.replace(question, question_id=f"q-{index}", question_type=kind)
        for index, kind in enumerate(QUESTION_TYPES)
    ]
    questions.append(dataclasses.replace(question, question_id="q-abs_abs"))
    return LmeDataset(tuple(questions))


def test_mini_selects_each_question_type_and_abstention_before_answers():
    from lme.scored_pilot import select_question_ids

    dataset = cohort()
    assert select_question_ids(dataset, 7) == [q.question_id for q in dataset.questions]
    with pytest.raises(ValueError, match="coverage"):
        select_question_ids(LmeDataset(dataset.questions[:-1]), 7)


def test_diagnostic_replay_cannot_bypass_full_run_gate():
    from lme.scored_pilot import select_question_ids

    with pytest.raises(ValueError, match="7 or 25"):
        select_question_ids(cohort(), 500)


def test_twenty_five_keeps_frozen_source_order():
    from lme.scored_pilot import select_question_ids

    template = cohort().questions[0]
    dataset = LmeDataset(tuple(dataclasses.replace(template, question_id=f"q-{i}") for i in range(25)))
    assert select_question_ids(dataset, 25) == [q.question_id for q in dataset.questions]


def test_guest_readiness_requires_positive_case_evidence():
    from lme.scored_pilot import _require_case_readiness

    for lanes in ([], [{"lane": "semantic", "requested": True, "verified": False, "method": "readiness-unverifiable", "fallback_detected": False, "evidence": "missing"}]):
        with pytest.raises(ValueError, match="case readiness"):
            _require_case_readiness(lanes)
    _require_case_readiness([{"lane": "semantic", "requested": True, "verified": True, "method": "doctor-check", "fallback_detected": False, "evidence": "positive fixture"}])


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    from lme import scored_pilot as pilot

    dataset = cohort()
    source = pilot.ReplaySource(
        dataset=dataset,
        contexts={q.question_id: ["PUBLIC RETRIEVED CONTEXT"] for q in dataset.questions},
        identity={"export_path": "fixture-export", "run_plan_path": "fixture-plan", "provider_variant": "exomem-source-only", "export_sha256": "a" * 64},
    )
    monkeypatch.setattr(pilot, "_load_source", lambda *_: source)
    # Exercise the official-script execution seam using a tiny offline script.
    # The real pinned script is exercised separately by the live verifier.
    script = b'''import json, sys
from openai import OpenAI
client = OpenAI(api_key="unused", base_url=None)
gold = {r["question_id"]: r["answer"] for r in json.load(open(sys.argv[3]))}
with open(sys.argv[2] + ".eval-results-" + sys.argv[1], "w") as out:
 for line in open(sys.argv[2]):
  row = json.loads(line)
  answer = client.chat.completions.create(model="gpt-4o-2024-08-06", messages=[{"role":"user","content":"JUDGE " + row["hypothesis"] + " " + gold[row["question_id"]]}], temperature=0, n=1, max_tokens=10)
  row["autoeval_label"] = {"model":"gpt-4o-2024-08-06", "label":"yes" in answer.choices[0].message.content.lower()}
  out.write(json.dumps(row) + "\\n")
'''
    monkeypatch.setattr(pilot, "_judge_source", lambda *_: (script, {"commit_sha": "b" * 40, "script_sha256": hashlib.sha256(script).hexdigest()}))
    monkeypatch.setattr(pilot, "estimate_cost", lambda *_: {"estimate_not_invoice": True})
    run = tmp_path / "pilot"
    info = pilot.prepare_replay(Path("fixture-export"), Path("fixture-plan"), Path("fixture-judge"), run, size=7, cap_usd=2)
    return pilot, run, info, source


def test_prepare_is_private_offline_and_never_rewrites_source(prepared):
    pilot, run, info, _ = prepared
    assert info["question_count"] == 7
    assert info["publishable"] is False
    assert not (run / "execution").exists()
    assert json.loads((run / "replay-plan.json").read_text())["budget_cap_usd"] == 2
    if __import__("os").name == "posix":
        assert run.stat().st_mode & 0o777 == 0o700
        assert (run / "replay-plan.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        pilot.prepare_replay(Path("fixture-export"), Path("fixture-plan"), Path("fixture-judge"), run, size=7, cap_usd=2)


def test_plan_tamper_refuses_before_backend_or_spend(prepared, monkeypatch):
    pilot, run, info, _ = prepared
    path = run / "replay-plan.json"
    data = json.loads(path.read_text())
    data["budget_cap_usd"] = 25
    path.write_text(json.dumps(data))
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key-not-for-output")
    with pytest.raises(ValueError, match="plan digest"):
        pilot.execute_replay(run, expected_plan_sha256=info["plan_sha256"], approval_token="test approval")
    assert not (run / "execution").exists()


def test_context_tamper_refuses_before_backend_or_spend(prepared, monkeypatch):
    pilot, run, info, _ = prepared
    (run / "contexts.json").write_text("{}")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key-not-for-output")
    with pytest.raises(ValueError, match="artifact digest"):
        pilot.execute_replay(run, expected_plan_sha256=info["plan_sha256"], approval_token="test approval")
    assert not (run / "execution").exists()


def test_changed_source_refuses_even_if_prepared_files_are_intact(prepared, monkeypatch):
    pilot, run, info, source = prepared
    monkeypatch.setattr(pilot, "_load_source", lambda *_: dataclasses.replace(source, identity={**source.identity, "export_sha256": "c" * 64}))
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key-not-for-output")
    with pytest.raises(ValueError, match="source changed"):
        pilot.execute_replay(run, expected_plan_sha256=info["plan_sha256"], approval_token="test approval")
    assert not (run / "execution").exists()


def test_missing_key_keeps_prepared_run_reusable(prepared, monkeypatch):
    pilot, run, info, _ = prepared
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        pilot.execute_replay(run, expected_plan_sha256=info["plan_sha256"], approval_token="test approval")
    assert not (run / "execution").exists()


def test_pinned_judge_refuses_modified_script(tmp_path):
    from lme.scored_pilot import _judge_source

    script = tmp_path / "src/evaluation/evaluate_qa.py"
    script.parent.mkdir(parents=True)
    script.write_text("raise RuntimeError('tampered')")
    with pytest.raises(ValueError, match="judge.*digest"):
        _judge_source(tmp_path)


def test_official_client_rejects_unpriced_or_changed_request_settings():
    from lme.scored_pilot import _judge_client

    class Backend:
        def complete(self, *args, **kwargs):
            pytest.fail("invalid request reached metered backend")

    client = _judge_client(Backend())
    with pytest.raises(ValueError, match="judge request"):
        client.chat.completions.create(model="unknown", messages=[], max_tokens=10, n=1, temperature=0)


@pytest.mark.parametrize("mutation", [None, "dataset", "script", "credential"])
def test_full_offline_execution_uses_common_reader_and_official_script(prepared, monkeypatch, mutation):
    import importlib.machinery
    import sys
    import types

    import httpx

    # Only the miniature script above runs here. The real upstream script and
    # its optional dependencies are exercised in the operational preflight.
    for name in ("openai", "backoff", "tqdm"):
        module = types.ModuleType(name)
        module.__spec__ = importlib.machinery.ModuleSpec(name, loader=None)
        if name == "openai":
            module.OpenAI = object
        monkeypatch.setitem(sys.modules, name, module)

    pilot, run, info, _ = prepared
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key-not-for-output")
    prompts = []

    def respond(url, *, json, **kwargs):
        prompt = json["messages"][0]["content"]
        prompts.append(prompt)
        if len(prompts) == 1 and mutation == "dataset":
            import json as json_module
            path = run / "dataset.json"
            data = json_module.loads(path.read_text())
            for row in data:
                row["answer"] = "UNAPPROVED-GOLD"
            path.write_text(json_module.dumps(data))
        if len(prompts) == 1 and mutation == "script":
            (run / "official-evaluate_qa.py").write_text("raise RuntimeError('UNAPPROVED-SCRIPT')")
        answer = "yes" if json["max_tokens"] == 10 else "An answer from the retrieved context."
        if mutation == "credential":
            answer = "fixture-key-not-for-output"
        return httpx.Response(200, json={"model": "gpt-4o-2024-08-06", "choices": [{"message": {"content": answer}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}})

    monkeypatch.setattr(httpx, "post", respond)
    if mutation == "credential":
        with pytest.raises(RuntimeError):
            pilot.execute_replay(run, expected_plan_sha256=info["plan_sha256"], approval_token="test approval")
        assert len(prompts) == 1
        for path in (run / "execution").rglob("*"):
            if path.is_file():
                assert b"fixture-key-not-for-output" not in path.read_bytes()
        assert json.loads((run / "execution/summary.json").read_bytes())["status"] == "failed"
        return
    result = pilot.execute_replay(run, expected_plan_sha256=info["plan_sha256"], approval_token="test approval")
    assert not any("UNAPPROVED-GOLD" in prompt for prompt in prompts)
    assert result["status"] == "complete"
    assert result["publishable"] is False
    assert len(prompts) == 42
    assert sum("PUBLIC RETRIEVED CONTEXT" in p for p in prompts) == 7
    assert result["scores"]["main"] == {"correct": 7, "total": 7, "accuracy": 1.0}
    assert result["cost_usd"] == pytest.approx(42 * (100 * 2.5 + 10 * 10) / 1_000_000)
    with pytest.raises((FileExistsError, ValueError)):
        pilot.execute_replay(run, expected_plan_sha256=info["plan_sha256"], approval_token="test approval")
