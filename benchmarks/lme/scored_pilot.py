"""Prepare offline, then score a bounded 7/25-case replay of verified retrieval.

These are diagnostic results, never a comparative publication or full-run
approval receipt. Original checkpoints and retrieval artifacts stay unchanged.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import hashlib
import importlib.util
import io
import json
import math
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_BENCHMARKS_ROOT = Path(__file__).resolve().parents[1]
if str(_BENCHMARKS_ROOT) not in sys.path:
    sys.path.insert(0, str(_BENCHMARKS_ROOT))

from lme.dataset import (  # noqa: E402
    QUESTION_TYPES,
    LmeDataset,
    dump_dataset,
    load_dataset_bytes,
    render_session,
    stable_dataset_bytes,
)
from lme.reader import ApiReader, _require_approval  # noqa: E402

_LOCKFILE = _BENCHMARKS_ROOT / "suites/lme_v1/LOCKFILE.json"
_SCHEMA = "lme-scored-pilot.v1"
_FILES = ("dataset.json", "contexts.json", "official-evaluate_qa.py")
_TRANSPORTS = ("openai", "openrouter")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write(path: Path, payload: bytes) -> None:
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(payload)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()


@dataclass(frozen=True)
class ReplaySource:
    dataset: LmeDataset
    contexts: dict[str, list[str]]
    identity: dict


def _require_case_readiness(lanes: list[dict]) -> None:
    from protocol.models import LaneReadiness
    from protocol.readiness import validate

    evidence = [LaneReadiness.model_validate(lane) for lane in lanes]
    if not any(lane.requested for lane in evidence) or validate(evidence).status != "VALID":
        raise ValueError("source case readiness lacks positive evidence")


def _load_source(export_path: Path, run_plan_path: Path) -> ReplaySource:
    from memorybench.export import (
        _native_dataset,
        _read_plan,
        _secure_read,
        _verify_reference,
        validate_export,
    )
    from protocol.contracts import validate_preregistration_identity
    from protocol.models import GuestCleanup, RunManifest

    plan, plan_bytes = _read_plan(run_plan_path)
    export_bytes = _secure_read(export_path, private=False)
    public = validate_export(json.loads(export_bytes), run_plan_path=run_plan_path)
    # This is a guest export: its lifecycle evidence is GuestCleanup, not the
    # direct runner's environment.json/instance-attempt lifecycle contract.
    manifest_bytes = _secure_read(Path(plan.output_root) / "manifest.json", private=False)
    manifest = RunManifest.model_validate_json(manifest_bytes)
    validate_preregistration_identity(manifest.preregistration_identity, repo_root=_BENCHMARKS_ROOT.parent)
    cleanup_bytes = _secure_read(Path(plan.output_root) / "guest-cleanup.v1.json", private=False)
    cleanup = GuestCleanup.model_validate_json(cleanup_bytes)
    if (not cleanup.all_absent or cleanup.failure_codes or cleanup.trigger != "success"
            or cleanup.run_id != public["run_id"] or cleanup.provider != public["provider"]
            or cleanup.provider_variant != public["provider_variant"]
            or {target.container_tag_hmac_sha256 for target in cleanup.targets}
            != {case["container_tag_hmac_sha256"] for case in public["cases"]}):
        raise ValueError("source guest cleanup is incomplete or mismatched")
    for target in cleanup.targets:
        for reference in target.artifacts:
            _verify_reference(reference.model_dump(mode="json"), plan)
    for reference in cleanup.final_absence.artifacts:
        _verify_reference(reference.model_dump(mode="json"), plan)
    if (public["status"] != "complete" or public["failure_codes"]
            or manifest.status != "VALID" or manifest.contamination == "contaminated"):
        raise ValueError("source retrieval is not valid and ready")
    if manifest.run_id != public["run_id"] or manifest.provider_variant != public["provider_variant"]:
        raise ValueError("source manifest identity differs")
    dataset_bytes, _ = _native_dataset(plan)
    dataset = load_dataset_bytes(dataset_bytes)
    questions, contexts = [], {}
    for case in public["cases"]:
        if case["failure_codes"] or case["private_gold"] is None:
            raise ValueError("source case is incomplete")
        _require_case_readiness(case["readiness"])
        gold = json.loads(_secure_read(Path(plan.output_root) / case["private_gold"]["path"], private=True))
        question = dataset.require(gold["question_id"])
        if question.question_id in contexts:
            raise ValueError("source repeats a question")
        questions.append(question)
        contexts[question.question_id] = [hit["content"] for hit in case["hits"]]
    return ReplaySource(
        LmeDataset(tuple(questions)), contexts,
        {
            "export_path": str(export_path.resolve()), "export_sha256": _sha(export_bytes),
            "run_plan_path": str(run_plan_path.resolve()), "run_plan_sha256": _sha(plan_bytes),
            "manifest_sha256": _sha(manifest_bytes), "cleanup_sha256": _sha(cleanup_bytes),
            "run_id": public["run_id"], "provider_variant": public["provider_variant"],
            "canary_isolation": manifest.contamination or "not_recorded",
            "equivalence": "not_assessed_by_diagnostic_replay",
            "dataset": public["dataset"], "provider": public["provider"],
            "harness": public["harness"],
        },
    )


def select_question_ids(dataset: LmeDataset, size: int) -> list[str]:
    if size not in (7, 25):
        raise ValueError("diagnostic replay accepts only 7 or 25 questions")
    if size == 25:
        if len(dataset.questions) != 25:
            raise ValueError("25-case replay requires the complete 25-case source")
        return [q.question_id for q in dataset.questions]
    selected, seen = [], set()
    for question in dataset.questions:
        ability = "abstention" if question.is_abstention else question.question_type
        if ability not in seen:
            selected.append(question.question_id)
            seen.add(ability)
    if seen != set(QUESTION_TYPES) | {"abstention"}:
        raise ValueError("seven-question source lacks complete ability coverage")
    return selected


def _judge_source(home: Path) -> tuple[bytes, dict]:
    lock = json.loads(_LOCKFILE.read_bytes())
    raw = stable_dataset_bytes(home / lock["evaluate_qa"]["path"])
    if _sha(raw) != lock["evaluate_qa"]["sha256"]:
        raise ValueError("official judge script digest differs from pin")
    head = subprocess.run(["git", "-C", str(home), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    if head != lock["commit_sha"]:
        raise ValueError("official judge checkout revision differs from pin")
    return raw, {"commit_sha": head, "script_sha256": _sha(raw), "lockfile_sha256": _sha(_LOCKFILE.read_bytes())}


def estimate_cost(dataset: LmeDataset, contexts: dict[str, list[str]], judge_source: bytes) -> dict:
    import tiktoken

    encoder = tiktoken.get_encoding("o200k_base")
    def count(text):
        return len(encoder.encode_ordinary(text)) + 7
    # Only the pinned, pure prompt formatter executes during offline preparation.
    formatter = next(node for node in ast.parse(judge_source).body if isinstance(node, ast.FunctionDef) and node.name == "get_anscheck_prompt")
    namespace: dict = {}
    exec(compile(ast.Module(body=[formatter], type_ignores=[]), "official-judge-prompt", "exec"), namespace)
    reader_tokens, judge_tokens, max_prompt = 0, 0, 0
    for question in dataset.questions:
        for context in (contexts[question.question_id], [render_session(s) for s in question.gold_sessions()], []):
            tokens = count(ApiReader._prompt(question, context))
            reader_tokens += tokens
            max_prompt = max(max_prompt, tokens)
        judge_tokens += 3 * count(namespace["get_anscheck_prompt"](question.question_type, question.question, question.answer, "", abstention=question.is_abstention))
    if max_prompt + 512 > 128_000:
        raise ValueError("prepared prompt exceeds the pinned model context window")
    calls = len(dataset.questions) * 3
    def total(answer_tokens):
        return ((reader_tokens + judge_tokens + calls * answer_tokens) * 2.5 + calls * (answer_tokens + 10) * 10) / 1_000_000
    return {
        "estimate_not_invoice": True, "reader_calls": calls, "judge_calls": calls,
        "reader_input_tokens": reader_tokens, "judge_base_input_tokens": judge_tokens,
        "assumed_chat_overhead_tokens": 7, "input_usd_per_million": 2.5, "output_usd_per_million": 10,
        "usd_at_100_answer_tokens_and_10_judge_tokens": total(100),
        "usd_at_512_answer_tokens_and_10_judge_tokens": total(512),
        "excludes": ["retries", "tax", "electricity", "provider ingestion/search", "native compilation"],
    }


def prepare_replay(export_path: Path, run_plan_path: Path, judge_home: Path, out: Path, *, size: int = 7, cap_usd: float = 2, transport: str = "openai") -> dict:
    if transport not in _TRANSPORTS:
        raise ValueError("unsupported diagnostic transport")
    if not math.isfinite(cap_usd) or not 0 < cap_usd <= 25:
        raise ValueError("diagnostic budget cap must be finite and within (0, 25]")
    source = _load_source(export_path, run_plan_path)
    ids = select_question_ids(source.dataset, size)
    dataset = LmeDataset(tuple(source.dataset.require(qid) for qid in ids))
    contexts = {qid: source.contexts[qid] for qid in ids}
    judge, judge_identity = _judge_source(judge_home)
    cost = estimate_cost(dataset, contexts, judge)
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    mask = os.umask(0o077)
    try:
        dump_dataset(dataset, out / "dataset.json")
        _write(out / "contexts.json", _json_bytes(contexts))
        _write(out / "official-evaluate_qa.py", judge)
        plan = {
            "schema": _SCHEMA, "publishable": False, "purpose": "diagnostic-scored-replay",
            "transport": transport,
            "question_ids": ids, "question_count": len(ids), "source": source.identity,
            "judge": judge_identity, "reader_model": "gpt-4o-2024-08-06",
            "reader_source_sha256": _sha(Path(sys.modules[ApiReader.__module__].__file__).read_bytes()),
            "answer_max_tokens": 512, "budget_cap_usd": cap_usd, "estimate": cost,
            "artifacts": {name: _sha((out / name).read_bytes()) for name in _FILES},
        }
        plan_bytes = _json_bytes(plan)
        _write(out / "replay-plan.json", plan_bytes)
    finally:
        os.umask(mask)
    return {"question_count": len(ids), "publishable": False, "transport": transport, "plan_sha256": _sha(plan_bytes), "budget_cap_usd": cap_usd, "estimate": cost}


def _judge_client(backend):
    def create(**kwargs):
        messages = kwargs.get("messages")
        if (set(kwargs) != {"model", "messages", "temperature", "n", "max_tokens"}
                or kwargs["model"] != "gpt-4o-2024-08-06" or kwargs["temperature"] != 0
                or kwargs["n"] != 1 or kwargs["max_tokens"] != 10
                or not isinstance(messages, list) or len(messages) != 1
                or set(messages[0]) != {"role", "content"} or messages[0]["role"] != "user"
                or not isinstance(messages[0]["content"], str)):
            raise ValueError("official judge request differs from pinned settings")
        result = backend.complete(messages[0]["content"], max_tokens=10)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=result.response))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _run_judge(script: Path, hypotheses: Path, dataset: Path, backend, *, script_bytes: bytes, hypothesis_bytes: bytes, dataset_bytes: bytes) -> bytes:
    import openai

    def client(*, api_key, base_url):
        if base_url is not None:
            raise ValueError("official judge cannot override the model endpoint")
        return _judge_client(backend)
    result_path = Path(str(hypotheses) + ".eval-results-gpt-4o")
    output_buffer = io.StringIO()
    # The upstream program reads its pinned hypotheses and gold through open().
    # Supply validated byte snapshots so path replacement cannot change grading.
    class OutputBuffer:
        def __enter__(self):
            return output_buffer

        def __exit__(self, *_):
            return False

    original_open = builtins.open
    def snapshot_open(file, mode="r", *args, **kwargs):
        path = os.fspath(file) if isinstance(file, (str, bytes, os.PathLike)) else None
        if path == str(hypotheses) and mode == "r":
            return io.StringIO(hypothesis_bytes.decode("utf-8"))
        if path == str(dataset) and mode == "r":
            return io.StringIO(dataset_bytes.decode("utf-8"))
        if path == str(result_path) and mode == "w":
            return OutputBuffer()
        return original_open(file, mode, *args, **kwargs)

    log = hypotheses.with_suffix(".judge.log")
    with log.open("x", encoding="utf-8") as output:
        try:
            with patch.object(openai, "OpenAI", client), patch.object(sys, "argv", [str(script), "gpt-4o", str(hypotheses), str(dataset)]), patch.object(builtins, "open", snapshot_open), redirect_stdout(output), redirect_stderr(output):
                exec(compile(script_bytes, str(script), "exec"), {"__name__": "__main__", "__file__": str(script)})
        finally:
            # Preserve even a partial judge output after a failed paid call.
            result = output_buffer.getvalue().encode("utf-8")
            _write(result_path, result)
    return result


def execute_replay(out: Path, *, expected_plan_sha256: str, approval_token: str, api_key_env: str | None = None) -> dict:
    _require_approval(approval_token)
    raw = stable_dataset_bytes(out / "replay-plan.json")
    if _sha(raw) != expected_plan_sha256:
        raise ValueError("prepared plan digest differs")
    plan = json.loads(raw)
    if plan["schema"] != _SCHEMA or plan["publishable"] is not False:
        raise ValueError("unknown diagnostic plan schema")
    # Plans prepared before transport selection used the fixed direct route.
    transport = plan.get("transport", "openai")
    if transport not in _TRANSPORTS:
        raise ValueError("unsupported diagnostic transport")
    api_key_env = api_key_env or ("OPENROUTER_API_KEY" if transport == "openrouter" else "OPENAI_API_KEY")
    snapshots = {name: stable_dataset_bytes(out / name) for name in _FILES}
    for name, payload in snapshots.items():
        if _sha(payload) != plan["artifacts"][name]:
            raise ValueError("prepared artifact digest differs")
    if _sha(Path(sys.modules[ApiReader.__module__].__file__).read_bytes()) != plan["reader_source_sha256"]:
        raise ValueError("common reader source changed")
    source = _load_source(Path(plan["source"]["export_path"]), Path(plan["source"]["run_plan_path"]))
    if source.identity != plan["source"]:
        raise ValueError("retrieval source changed since preparation")
    dataset = load_dataset_bytes(snapshots["dataset.json"])
    if select_question_ids(source.dataset, plan["question_count"]) != plan["question_ids"]:
        raise ValueError("prepared selection differs from source")
    if [q.question_id for q in dataset.questions] != plan["question_ids"]:
        raise ValueError("prepared dataset differs from selection")
    if not os.environ.get(api_key_env, "").strip():
        raise ValueError(f"{api_key_env} is not configured")
    for module in ("openai", "backoff", "tqdm"):
        if importlib.util.find_spec(module) is None:
            raise ValueError("install the benchmark-judge dependency group before execution")
    from lme.metered import MeteredOpenAIBackend

    execution = out / "execution"
    execution.mkdir(mode=0o700, exist_ok=False)
    mask = os.umask(0o077)
    summary = {"schema": _SCHEMA, "status": "failed", "publishable": False, "question_count": len(dataset.questions), "plan_sha256": expected_plan_sha256, "transport": transport}
    try:
        _write(execution / "replay-plan.json", raw)
        for name, payload in snapshots.items():
            _write(execution / name, payload)
        backend = MeteredOpenAIBackend(execution, cap_usd=plan["budget_cap_usd"], approval_token=approval_token, api_key_env=api_key_env, transport=transport)
        reader = ApiReader(backend=backend, approval_token=approval_token, run_dir=execution)
        contexts = json.loads(snapshots["contexts.json"])
        lanes = {"main": [], "ceiling": [], "floor": []}
        for question in dataset.questions:
            for lane, context in (("main", contexts[question.question_id]), ("ceiling", [render_session(s) for s in question.gold_sessions()]), ("floor", [])):
                answer = reader.answer(question, context)
                lanes[lane].append({"question_id": question.question_id, "hypothesis": answer})
                # Flush every completed answer before another potentially billable call.
                with (execution / f"{lane}.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(lanes[lane][-1], ensure_ascii=False) + "\n")
        scores = {}
        for lane in lanes:
            hypothesis_bytes = b"".join((json.dumps(row, ensure_ascii=False) + "\n").encode() for row in lanes[lane])
            label_bytes = _run_judge(execution / "official-evaluate_qa.py", execution / f"{lane}.jsonl", execution / "dataset.json", backend, script_bytes=snapshots["official-evaluate_qa.py"], hypothesis_bytes=hypothesis_bytes, dataset_bytes=snapshots["dataset.json"])
            rows = [json.loads(line) for line in label_bytes.splitlines() if line]
            labels = {row["question_id"]: row["autoeval_label"]["label"] for row in rows}
            if len(rows) != len(labels) or set(labels) != set(plan["question_ids"]) or any(type(v) is not bool for v in labels.values()):
                raise ValueError("official judge labels are incomplete")
            scores[lane] = {"correct": sum(labels.values()), "total": len(labels), "accuracy": sum(labels.values()) / len(labels)}
        entries = [json.loads(line) for line in (execution / "ledger.jsonl").read_text().splitlines() if line]
        summary.update(status="complete", scores=scores, cost_usd=sum(row["units"] for row in entries if row["kind"] == "commit"), source=source.identity)
    except BaseException as exc:
        summary["failure_type"] = type(exc).__name__
        raise
    finally:
        try:
            _write(execution / "summary.json", _json_bytes(summary))
        finally:
            os.umask(mask)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="validate saved retrieval and price a diagnostic run without API calls")
    prepare.add_argument("--export", required=True, type=Path)
    prepare.add_argument("--run-plan", required=True, type=Path)
    prepare.add_argument("--judge-home", required=True, type=Path)
    prepare.add_argument("--out", required=True, type=Path)
    prepare.add_argument("--size", type=int, choices=(7, 25), default=7)
    prepare.add_argument("--budget-cap-usd", type=float, default=2)
    prepare.add_argument("--transport", choices=_TRANSPORTS, default="openai")
    run = commands.add_parser("run", help="execute a prepared diagnostic under its frozen budget")
    run.add_argument("--out", required=True, type=Path)
    run.add_argument("--expected-plan-sha256", required=True)
    run.add_argument("--metered-approval", required=True)
    run.add_argument("--api-key-env", help="credential variable; defaults to the prepared transport's key")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        result = prepare_replay(args.export, args.run_plan, args.judge_home, args.out, size=args.size, cap_usd=args.budget_cap_usd, transport=args.transport)
    else:
        result = execute_replay(args.out, expected_plan_sha256=args.expected_plan_sha256, approval_token=args.metered_approval, api_key_env=args.api_key_env)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
