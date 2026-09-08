"""Prepare and run a private native write-maintain-recall LongMemEval diagnostic."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT / "benchmarks") not in sys.path:
    sys.path.insert(0, str(_ROOT / "benchmarks"))

from equivalence.selection import (  # noqa: E402
    CANONICAL_LME_S_SOURCE,
    load_frozen_lme_selection,
    select_lme_s_25,
)

from lme.dataset import _question_dict, load_dataset_bytes, stable_dataset_bytes  # noqa: E402
from lme.metered_profiles import (  # noqa: E402
    GLM_MODEL,
    JUDGE_MODEL,
    SOL_MODEL,
    model_contract,
    model_profile,
)
from lme.native_agent import AgentLimits, NativeBroker, RunEnvelope, run_agent_phase  # noqa: E402
from lme.scored_pilot import _judge_source, _run_judge  # noqa: E402

SCHEMA = "lme-native-pilot.v1"
VARIANT = "exomem-native-session-maintenance"
_PIN = _ROOT / "benchmarks/equivalence/subsets/lme-s-25.json"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()


def _write(path: Path, raw: bytes):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o600), "wb") as stream:
        stream.write(raw)


def _implementation_identity() -> dict[str, str]:
    # Bind the executable harness, including budget/judge dependencies, not a
    # narrative version label. Product source is separately frozen as input.
    paths = sorted((_ROOT / "benchmarks").rglob("*.py"))
    return {str(path.relative_to(_ROOT)): _sha(stable_dataset_bytes(path)) for path in paths}


def _writer_input(sessions) -> dict:
    """Only source sessions cross this seam; evaluator records are not accepted."""
    ordered = sorted(enumerate(sessions, 1), key=lambda item: (item[1].timestamp, item[0]))
    return {"sessions": [
        {"ordinal": ordinal, "timestamp": session.timestamp_text,
         "messages": [{"role": message.role, "content": message.content} for message in session.messages]}
        for ordinal, session in ordered
    ]}


def _overlap(question) -> list[int]:
    def shingles(text):
        words = re.findall(r"\w+", text.casefold())
        return {tuple(words[i:i + 8]) for i in range(max(0, len(words) - 7))}
    probe = shingles(question.question)
    return [index for index, session in enumerate(question.sessions, 1)
            if any(probe & shingles(message.content) for message in session.messages)]


def _revision(product_root: Path) -> str:
    result = subprocess.run(["git", "-C", str(product_root), "rev-parse", "HEAD"], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else "fixture-unversioned"


def _runtime_identity(python: Path) -> dict:
    """Bind the interpreter, prefix and installed distribution metadata."""
    executable = python.absolute()
    probe = """import hashlib, importlib.metadata as m, json, platform, sys
print(json.dumps({'version':sys.version, 'prefix':sys.prefix, 'platform':platform.platform(),
 'distributions': sorted((d.metadata['Name'], d.version,
 hashlib.sha256((d.read_text('RECORD') or '').encode()).hexdigest()) for d in m.distributions())}))"""
    result = subprocess.run([str(executable), "-I", "-c", probe], capture_output=True, text=True,
                            env={"PATH": os.defpath}, timeout=30, check=True)
    return {"executable": str(executable), "binary_sha256": _sha(executable.read_bytes()),
            "installed": json.loads(result.stdout)}


def _model_identity(root: Path) -> dict:
    """Hash frozen model assets with bounded memory, separate from prompt inputs."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError("native model cache must be a frozen directory")
    files, size = {}, 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("frozen model cache must not contain symlinks")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("frozen model cache has a special file")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1_048_576):
                digest.update(block)
                size += len(block)
        files[path.relative_to(root).as_posix()] = digest.hexdigest()
    if not files:
        raise ValueError("native model cache is empty")
    return {"files": files, "stored_bytes": size}


def _hookless_instructions(document: bytes) -> str:
    """Read the published Maximal block, without maintaining a benchmark copy."""
    lines = document.decode("utf-8").splitlines(keepends=True)
    in_section = False
    start = None
    for index, line in enumerate(lines):
        if line.startswith("### "):
            if in_section:
                break
            in_section = line.startswith("### Maximal")
        elif in_section and line.startswith("```"):
            if start is None:
                start = index + 1
            else:
                block = "".join(lines[start:index])
                if block.strip():
                    return block
                break
    raise ValueError("product docs/prominence.md has no Maximal instruction block")


def prepare_native(dataset_path: Path, judge_home: Path, out: Path, *, product_root: Path = _ROOT,
                   profile: str = "semantic", size: int = 1, budget_cap_usd: float,
                   seed: str = "native-lme-v1", limits: AgentLimits | None = None,
                   transport: str = "openrouter", python: Path = Path(sys.executable),
                   model_cache: Path | None = None, clip_model_cache: Path | None = None,
                   agent_model: str = JUDGE_MODEL, agent_tokenizer: Path | None = None,
                   selection_mode: str = "fresh") -> dict:
    """Freeze source, guidance, cohort and limits without model or service calls."""
    if profile not in {"fixture", "semantic"} or transport not in {"openai", "openrouter"}:
        raise ValueError("unknown native profile or transport")
    if selection_mode not in {"fresh", "canonical25"}:
        raise ValueError("unknown native selection mode")
    if size not in {1, 7, 25}:
        raise ValueError("native diagnostic accepts only 1, 7 or 25 cases")
    if selection_mode == "canonical25" and size != 25:
        raise ValueError("canonical25 selection requires size 25")
    if isinstance(budget_cap_usd, bool) or not math.isfinite(budget_cap_usd) or budget_cap_usd <= 0:
        raise ValueError("budget cap must be finite and positive")
    models = model_contract(agent_model, transport)
    from lme.tokenization import tokenizer_bytes
    agent_tokenizer_bytes = tokenizer_bytes(model_profile(agent_model, transport), agent_tokenizer)
    limits = limits or AgentLimits()
    dataset_bytes = stable_dataset_bytes(dataset_path)
    pin = json.loads(_PIN.read_bytes())
    expected_dataset_sha256 = (CANONICAL_LME_S_SOURCE["sha256"]
                               if selection_mode == "canonical25" else pin["source_identity"]["sha256"])
    if (profile == "semantic" or selection_mode == "canonical25") and _sha(dataset_bytes) != expected_dataset_sha256:
        raise ValueError("native product runs require the pinned official dataset")
    if profile == "semantic" and (model_cache is None or clip_model_cache is None):
        raise ValueError("native semantic preparation requires explicit local BGE and CLIP model caches")
    dataset = load_dataset_bytes(dataset_bytes)
    selection_artifact_bytes = None
    selection_census_bytes = None
    if selection_mode == "canonical25":
        artifact, selection_artifact_bytes = load_frozen_lme_selection()
        census = [{"question_id": identity, "question_type": kind} for identity, kind in dataset.census]
        regenerated = select_lme_s_25(census, source=CANONICAL_LME_S_SOURCE)
        if artifact != regenerated:
            raise ValueError("native canonical selection artifact differs from source census")
        selected = [dataset.require(question_id) for question_id in artifact["target_question_ids"]]
        selection_census_bytes = _json(census)
        selection = {
            "mode": "canonical25",
            "cohort": "prior-inspected canonical LongMemEval-S 25-case cohort",
            "artifact_path": "benchmarks/equivalence/subsets/lme-s-25.json",
            "artifact_sha256": _sha(selection_artifact_bytes),
            "algorithm_version": artifact["selection_algorithm_version"],
            "algorithm": artifact["selection_algorithm"],
            "source_identity": artifact["source_identity"],
            "source_census_path": "selection/source-census.json",
            "source_census_sha256": _sha(selection_census_bytes),
        }
    else:
        # Previously inspected pilot cases are held out of the new diagnostic.
        prior_ids = set(pin["target_question_ids"])
        candidates = [question for question in dataset.questions if question.question_id not in prior_ids]
        candidates.sort(key=lambda q: (_sha((seed + "\0" + q.question_id).encode()), q.question_id))
        if len(candidates) < size:
            raise ValueError("not enough fresh questions for the selected native diagnostic")
        selected = candidates[:size]
        selection = {
            "mode": "fresh",
            "cohort": "fresh holdout excluding the prior-inspected canonical cohort",
            "seed": seed,
            "algorithm": "sha256(seed + NUL + question_id)",
            "excluded_prior_question_ids": sorted(prior_ids),
        }
    judge_bytes, judge_identity = _judge_source(judge_home)
    product_root = product_root.resolve()
    source_root = product_root / "src"
    if not (source_root / "exomem/_scaffold/_Schema/SKILL.md").is_file():
        raise ValueError("product source must include its shipped skill")
    prominence = stable_dataset_bytes(product_root / "docs/prominence.md")
    custom_instructions = _hookless_instructions(prominence)
    runtimes = {"product": _runtime_identity(python), "harness": _runtime_identity(Path(sys.executable))}
    if out.is_symlink():
        raise ValueError("native output may not be a symlink")
    out.mkdir(mode=0o700, parents=False, exist_ok=False)
    artifacts: dict[str, str] = {}
    model_identity = None
    clip_identity = None
    if model_cache is not None:
        from lme.native_cell import copy_model_cache
        copy_model_cache(model_cache, out / "model-cache")
        model_identity = _model_identity(out / "model-cache")
    if clip_model_cache is not None:
        from lme.native_cell import copy_model_cache
        copy_model_cache(clip_model_cache, out / "clip-model-cache")
        clip_identity = _model_identity(out / "clip-model-cache")

    def save(relative: str, data: bytes):
        _write(out / relative, data)
        artifacts[relative] = _sha(data)

    if agent_tokenizer_bytes is not None:
        save("agent-tokenizer.json", agent_tokenizer_bytes)
    if selection_artifact_bytes is not None and selection_census_bytes is not None:
        save("selection/lme-s-25.json", selection_artifact_bytes)
        save("selection/source-census.json", selection_census_bytes)

    save("product/docs/prominence.md", prominence)
    save("custom-instructions.md", custom_instructions.encode("utf-8"))
    for path in sorted(source_root.rglob("*")):
        if path.is_symlink():
            raise ValueError("product snapshot refuses symlinks")
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        save("product/src/" + path.relative_to(source_root).as_posix(), stable_dataset_bytes(path))
    save("evaluator.json", _json([_question_dict(question) for question in selected]))
    save("official-evaluate_qa.py", judge_bytes)
    cases = []
    for index, question in enumerate(selected, 1):
        case_id = f"case-{index:04d}"
        writer = _writer_input(question.sessions)
        writer_path = f"writers/{case_id}.json"
        save(writer_path, _json(writer))
        cases.append({"case_id": case_id, "question_id": question.question_id,
                      "writer_path": writer_path, "session_count": len(writer["sessions"]),
                      "source_probe_overlap_ordinals": _overlap(question)})
    plan = {
        "schema": SCHEMA, "variant": VARIANT, "publishable": False,
        "profile": profile, "transport": transport, "budget_cap_usd": budget_cap_usd,
        "limits": asdict(limits), "model": agent_model, "models": models, "judge": judge_identity,
        "runtimes": runtimes,
        "model_cache": model_identity,
        "clip_model_cache": clip_identity,
        "dataset_sha256": _sha(dataset_bytes), "product_revision": _revision(product_root),
        "implementation_sha256": _implementation_identity(), "artifacts": artifacts, "cases": cases,
        "selection": selection,
        "scheduling": "chronological session-end maintenance; fresh context per session and answer",
        "cost_basis": "capped actual usage; agent-chosen step count is unknown before execution",
        "text_only": True, "answer_access": "recall-only",
        "custom_instructions_source": "product/docs/prominence.md#maximal",
        "client_hooks": False,
    }
    plan_bytes = _json(plan)
    _write(out / "native-plan.json", plan_bytes)
    return {"plan_sha256": _sha(plan_bytes), "question_count": len(cases),
            "session_count": sum(case["session_count"] for case in cases),
            "budget_cap_usd": budget_cap_usd, "publishable": False, "profile": profile,
            "minimum_model_calls": sum(case["session_count"] for case in cases) + len(cases) * 2,
            "model_asset_bytes": sum(item["stored_bytes"] for item in (model_identity, clip_identity) if item)}


def _snapshots(out: Path, plan: dict) -> dict[str, bytes]:
    snapshots = {}
    for relative, expected in plan["artifacts"].items():
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or "\\" in relative:
            raise ValueError("invalid prepared artifact path")
        for parent in (out / path).parents:
            if parent == out:
                break
            if parent.is_symlink():
                raise ValueError("prepared artifact parent is a symlink")
        raw = stable_dataset_bytes(out / path)
        if _sha(raw) != expected:
            raise ValueError("prepared artifact digest differs")
        snapshots[relative] = raw
    return snapshots


def validate_native_run(out: Path, *, expected_plan_sha256: str) -> dict:
    raw = stable_dataset_bytes(out / "native-plan.json")
    if _sha(raw) != expected_plan_sha256:
        raise ValueError("native plan digest differs")
    plan = json.loads(raw)
    if plan["schema"] != SCHEMA or plan["publishable"] is not False or plan["variant"] != VARIANT:
        raise ValueError("unknown native plan contract")
    AgentLimits(**plan["limits"])
    if plan.get("models") != model_contract(plan["model"], plan["transport"]):
        raise ValueError("native model contract differs; prepare a fresh run")
    if plan["implementation_sha256"] != _implementation_identity():
        raise ValueError("native implementation digest changed; prepare a fresh run")
    for name, identity in plan["runtimes"].items():
        executable = Path(sys.executable) if name == "harness" else Path(identity["executable"])
        if _runtime_identity(executable) != identity:
            raise ValueError(f"native {name} runtime changed; prepare a fresh run")
    if plan.get("model_cache") is not None and _model_identity(out / "model-cache") != plan["model_cache"]:
        raise ValueError("native model assets changed; prepare a fresh run")
    if plan.get("clip_model_cache") is not None and _model_identity(out / "clip-model-cache") != plan["clip_model_cache"]:
        raise ValueError("native CLIP model assets changed; prepare a fresh run")
    if plan["profile"] == "semantic" and (plan.get("model_cache") is None or plan.get("clip_model_cache") is None):
        raise ValueError("native semantic plan needs frozen BGE and CLIP model caches")
    snapshots = _snapshots(out, plan)
    tokenizer_sha = plan["models"]["agent"].get("tokenizer_sha256")
    if tokenizer_sha is not None and _sha(snapshots.get("agent-tokenizer.json", b"")) != tokenizer_sha:
        raise ValueError("native agent tokenizer differs from the model profile")
    evaluator = load_dataset_bytes(snapshots["evaluator.json"])
    selection = plan.get("selection", {})
    if selection.get("mode") not in {"fresh", "canonical25"}:
        raise ValueError("unknown native selection mode")
    if selection["mode"] == "canonical25":
        if plan["dataset_sha256"] != CANONICAL_LME_S_SOURCE["sha256"]:
            raise ValueError("native canonical dataset identity differs")
        artifact, raw = load_frozen_lme_selection()
        if snapshots.get("selection/lme-s-25.json") != raw:
            raise ValueError("native canonical selection artifact differs")
        census_raw = snapshots.get("selection/source-census.json")
        if census_raw is None or _sha(census_raw) != selection.get("source_census_sha256"):
            raise ValueError("native canonical source census differs")
        try:
            census = json.loads(census_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("native canonical source census is invalid") from exc
        regenerated = select_lme_s_25(census, source=CANONICAL_LME_S_SOURCE)
        if artifact != regenerated:
            raise ValueError("native canonical selection artifact differs from source census")
        expected_ids = artifact["target_question_ids"]
        if [case["question_id"] for case in plan["cases"]] != expected_ids:
            raise ValueError("native canonical case order differs")
        if [question.question_id for question in evaluator.questions] != expected_ids:
            raise ValueError("native canonical evaluator order differs")
        expected_metadata = {
            "mode": "canonical25",
            "cohort": "prior-inspected canonical LongMemEval-S 25-case cohort",
            "artifact_path": "benchmarks/equivalence/subsets/lme-s-25.json",
            "artifact_sha256": _sha(raw),
            "algorithm_version": artifact["selection_algorithm_version"],
            "algorithm": artifact["selection_algorithm"],
            "source_identity": artifact["source_identity"],
            "source_census_path": "selection/source-census.json",
            "source_census_sha256": _sha(census_raw),
        }
        if selection != expected_metadata:
            raise ValueError("native canonical selection metadata differs")
    for case in plan["cases"]:
        question = evaluator.require(case["question_id"])
        if json.loads(snapshots[case["writer_path"]]) != _writer_input(question.sessions):
            raise ValueError("writer input differs from source-only session projection")
    return plan


def _make_backend(execution: Path, plan: dict, approval_token: str, api_key_env: str | None):
    from lme.metered import MeteredOpenAIBackend
    return MeteredOpenAIBackend(execution, cap_usd=plan["budget_cap_usd"], approval_token=approval_token,
                                transport=plan["transport"], api_key_env=api_key_env, model=plan["model"],
                                tokenizer_path=(execution.parent / "agent-tokenizer.json"
                                    if plan["models"]["agent"].get("tokenizer_sha256") else None))


def _structured(payload: dict):
    data = payload.get("structuredContent")
    if isinstance(data, dict):
        return data.get("result", data)
    for item in payload.get("content", []):
        if item.get("type") == "text":
            try:
                parsed = json.loads(item["text"])
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, dict):
                return parsed.get("result", parsed)
    return {}


def _phase_evidence(path: Path) -> dict:
    events = [json.loads(line) for line in (path / "events.jsonl").read_text().splitlines()] if (path / "events.jsonl").exists() else []
    pending = None
    operations, commits, recalls = [], [], []
    for event in events:
        if event["direction"] == "request" and event.get("kind") == "tool":
            pending = event
        elif event["direction"] == "response" and "result" in event and pending:
            data = _structured(event["result"])
            operations.append({"name": pending["name"], "tool_call_id": pending.get("tool_call_id"), "is_error": event["result"].get("isError", False)})
            if isinstance(data, dict) and data.get("mutated") is True and data.get("status") == "committed":
                commits.append({"tool": pending["name"], "receipt": data})
            if pending["name"] in {"ask_memory", "read_memory"}:
                recalls.append({"tool": pending["name"], "result": data})
            pending = None
    return {"operations": operations, "committed_receipts": commits, "recall": recalls}


async def run_native_cases(plan: dict, execution: Path, snapshots: dict[str, bytes], *, backend,
                           python: Path, cell_factory=None, model_cache: Path | None = None,
                           clip_model_cache: Path | None = None) -> tuple[list[dict], RunEnvelope]:
    """Run source-only writer packets, then fresh question-only answering workers."""
    if cell_factory is None:
        from lme.native_cell import NativeCell
        cell_factory = NativeCell
    frozen = execution / "frozen"
    for relative, raw in snapshots.items():
        _write(frozen / relative, raw)
    evaluator = load_dataset_bytes(snapshots["evaluator.json"])
    guidance_prefix = "product/src/exomem/_scaffold/_Schema/"
    guidance = {name.removeprefix(guidance_prefix): raw.decode("utf-8") for name, raw in snapshots.items()
                if name.startswith(guidance_prefix) and name.endswith(".md")}
    envelope = RunEnvelope(AgentLimits(**plan["limits"]))
    rows = []
    for case in plan["cases"]:
        case_root = execution / case["case_id"]
        case_root.mkdir(mode=0o700)
        row = {"case_id": case["case_id"], "question_id": case["question_id"], "status": "incomplete", "phases": []}
        rows.append(row)
        try:
            envelope.check()
            remaining = envelope.limits.run_seconds - (time.monotonic() - envelope.started)
            async with asyncio.timeout(remaining), cell_factory(case_root / "cell", python=python, product_root=frozen / "product", profile=plan["profile"], model_cache=model_cache, clip_model_cache=clip_model_cache) as cell:
                row["runtime"] = cell.runtime_receipt
                row["initial_bootstrap"] = cell.bootstrap
                row["initial_readiness"] = await cell.readiness()
                if plan["profile"] == "semantic" and row["initial_readiness"].get("semantic_verified") is not True:
                    raise RuntimeError("native semantic readiness unavailable before writing")
                row["before"] = cell.snapshot()
                broker = NativeBroker(
                    cell=cell, backend=backend, envelope=envelope, guidance=guidance,
                    custom_instructions=snapshots["custom-instructions.md"].decode("utf-8"),
                )
                writer = json.loads(snapshots[case["writer_path"]])
                for index, session in enumerate(writer["sessions"], 1):
                    phase_path = case_root / f"write-{index:04d}"
                    turn = "Completed conversation:\n" + json.dumps(session, ensure_ascii=False)
                    phase_result = await run_agent_phase(broker, phase="writer", turn=turn, out=phase_path)
                    phase_result["evidence"] = _phase_evidence(phase_path)
                    row["phases"].append(phase_result)
                    if phase_result["status"] != "completed":
                        raise RuntimeError("writer phase incomplete")
                row["after_writing"] = cell.snapshot()
                row["answer_readiness"] = await cell.readiness()
                if plan["profile"] == "semantic" and row["answer_readiness"].get("semantic_verified") is not True:
                    raise RuntimeError("native semantic readiness unavailable after writing")
                question = evaluator.require(case["question_id"])
                answer = await run_agent_phase(broker, phase="answer", turn=f"Question date: {question.question_date_text}\nQuestion: {question.question}", out=case_root / "answer")
                answer["evidence"] = _phase_evidence(case_root / "answer")
                row["phases"].append(answer)
                row["after_answer"] = cell.snapshot()
                row["recall_readiness"] = await cell.readiness()
                row["semantic_ready"] = (
                    plan["profile"] == "semantic"
                    and row["recall_readiness"].get("semantic_verified") is True
                    and row["recall_readiness"].get("fallback_detected") is not True
                )
                row["semantic_verified"] = row["semantic_ready"] and row["recall_readiness"].get("semantic_retrieval_verified") is True
                if row["after_writing"]["files"] != row["after_answer"]["files"]:
                    raise RuntimeError("memory content changed during recall-only answer phase")
                if answer["status"] != "completed":
                    raise RuntimeError("answer phase incomplete")
                row.update(status="completed", hypothesis=answer["answer"])
        except Exception as exc:  # noqa: BLE001 - preserve a redacted, non-publishable incomplete row
            row["reason"] = envelope.stopped or type(exc).__name__
            envelope.stopped = row["reason"]
        finally:
            _write(case_root / "row.json", _json(row))
    return rows, envelope


def execute_native(out: Path, *, expected_plan_sha256: str, approval_token: str,
                   python: Path | None = None, api_key_env: str | None = None) -> dict:
    from lme.reader import _require_approval
    _require_approval(approval_token)
    plan = validate_native_run(out, expected_plan_sha256=expected_plan_sha256)
    if plan["profile"] != "semantic":
        raise ValueError("fixture preparations cannot execute paid product runs")
    pinned_python = Path(plan["runtimes"]["product"]["executable"])
    if python is not None and python.absolute() != pinned_python:
        raise ValueError("product interpreter differs from prepared runtime")
    snapshots = _snapshots(out, plan)
    key_name = api_key_env or ("OPENROUTER_API_KEY" if plan["transport"] == "openrouter" else "OPENAI_API_KEY")
    if not os.environ.get(key_name, "").strip():
        raise ValueError(f"{key_name} is not configured")
    execution = out / "execution"
    execution.mkdir(mode=0o700, exist_ok=False)
    summary = {"schema": SCHEMA, "variant": VARIANT, "publishable": False, "status": "incomplete",
               "plan_sha256": expected_plan_sha256, "question_count": len(plan["cases"]), "accuracy": None,
               "models": plan["models"]}
    try:
        _write(execution / "native-plan.json", stable_dataset_bytes(out / "native-plan.json"))
        backend = _make_backend(execution, plan, approval_token, api_key_env)
        rows, envelope = asyncio.run(run_native_cases(plan, execution, snapshots, backend=backend, python=pinned_python, model_cache=out / "model-cache", clip_model_cache=out / "clip-model-cache"))
        summary["cases"] = rows
        complete = [row for row in rows if row["status"] == "completed"]
        summary["answered_count"] = len(complete)
        if complete and not envelope.stopped:
            hypotheses = b"".join((json.dumps({"question_id": row["question_id"], "hypothesis": row["hypothesis"]}, ensure_ascii=False) + "\n").encode() for row in complete)
            _write(execution / "native.jsonl", hypotheses)

            class JudgeBackend:
                def complete(self, prompt, *, max_tokens):
                    envelope.take("model")
                    reserved = envelope.limits.max_context_tokens + max_tokens
                    envelope.reserve_tokens(reserved)

                    async def bounded_judge():
                        remaining = envelope.limits.run_seconds - (time.monotonic() - envelope.started)
                        async with asyncio.timeout(remaining):
                            return await backend.complete_messages([{"role": "user", "content": prompt}], tools=[], max_tokens=max_tokens, model=JUDGE_MODEL)

                    completion = asyncio.run(bounded_judge())
                    envelope.settle_tokens(reserved, completion.input_tokens + completion.output_tokens)
                    return SimpleNamespace(response=completion.message["content"])

            labels = _run_judge(execution / "frozen/official-evaluate_qa.py", execution / "native.jsonl", execution / "frozen/evaluator.json", JudgeBackend(), script_bytes=snapshots["official-evaluate_qa.py"], hypothesis_bytes=hypotheses, dataset_bytes=snapshots["evaluator.json"])
            results = [json.loads(line) for line in labels.splitlines() if line]
            expected_ids = {row["question_id"] for row in complete}
            if len(results) != len(expected_ids) or {r["question_id"] for r in results} != expected_ids or any(type(r["autoeval_label"]["label"]) is not bool for r in results):
                raise ValueError("official judge labels are incomplete")
            summary["correct"] = sum(row["autoeval_label"]["label"] for row in results)
            if len(complete) == len(rows):
                summary.update(status="complete", accuracy=summary["correct"] / len(rows))
    except Exception as exc:  # noqa: BLE001 - retain spending and partial outcomes without arbitrary exception text
        summary["reason"] = type(exc).__name__
    finally:
        if "envelope" in locals():
            summary["envelope"] = {"model_calls": envelope.model_calls, "tool_calls": envelope.tool_calls,
                                   "tokens": envelope.tokens, "held_tokens": envelope.held_tokens,
                                   "stopped": envelope.stopped}
        ledger = execution / "ledger.jsonl"
        if ledger.exists():
            entries = [json.loads(line) for line in ledger.read_text().splitlines()]
            summary["cost_usd"] = sum(entry["units"] for entry in entries if entry["kind"] == "commit")
            liability = sum(entry["units"] for entry in entries if entry["kind"] == "reserve" and entry["decision"] != "refused-cap") - sum(entry["units"] for entry in entries if entry["kind"] == "release")
            summary["cost_plus_held_usd"] = max(0.0, liability)
            summary["held_usd"] = max(0.0, liability - summary["cost_usd"])
            summary["ledger_stop"] = (execution / "STOP").exists()
        _write(execution / "summary.json", _json(summary))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--dataset", type=Path, required=True)
    prepare.add_argument("--judge-home", type=Path, required=True)
    prepare.add_argument("--out", type=Path, required=True)
    prepare.add_argument("--product-root", type=Path, default=_ROOT)
    prepare.add_argument("--size", type=int, choices=[1, 7, 25], default=1)
    prepare.add_argument("--selection-mode", choices=["fresh", "canonical25"], default="fresh")
    prepare.add_argument("--budget-cap-usd", type=float, required=True)
    prepare.add_argument("--seed", default="native-lme-v1")
    prepare.add_argument("--transport", choices=["openai", "openrouter"], default="openrouter")
    prepare.add_argument("--agent-model", choices=[JUDGE_MODEL, SOL_MODEL, GLM_MODEL], default=JUDGE_MODEL)
    prepare.add_argument("--agent-tokenizer", type=Path, help="Pinned local tokenizer JSON required by GLM")
    prepare.add_argument("--python", type=Path, default=Path(sys.executable))
    prepare.add_argument("--model-cache", type=Path, required=True, help="Local HF models--BAAI--bge-base-en-v1.5 directory")
    prepare.add_argument("--clip-model-cache", type=Path, required=True, help="Local HF models--sentence-transformers--clip-ViT-B-32 directory")
    run = commands.add_parser("run")
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--expected-plan-sha256", required=True)
    run.add_argument("--metered-approval", required=True)
    run.add_argument("--python", type=Path)
    run.add_argument("--api-key-env")
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare_native(args.dataset, args.judge_home, args.out, product_root=args.product_root,
                                size=args.size, budget_cap_usd=args.budget_cap_usd, seed=args.seed, transport=args.transport, python=args.python, model_cache=args.model_cache, clip_model_cache=args.clip_model_cache, agent_model=args.agent_model, agent_tokenizer=args.agent_tokenizer, selection_mode=args.selection_mode)
    else:
        result = execute_native(args.out, expected_plan_sha256=args.expected_plan_sha256,
                                approval_token=args.metered_approval, python=args.python, api_key_env=args.api_key_env)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
