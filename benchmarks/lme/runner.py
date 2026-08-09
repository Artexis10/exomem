"""Immutable offline-first LongMemEval-S run pipeline."""

from __future__ import annotations

import datetime as dt
import json
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from membench.adapters.base import AdapterEnvironmentError
from membench.environment import capture_environment
from membench.judge.backends import ClaudeCliBackend, OpenAICompatBackend
from protocol.canary import canary_for, evaluate_probes
from protocol.manifest import finalize_manifest, start_manifest
from protocol.models import BudgetSummary, CaseHandle, DatasetIdentity, LeakageSummary
from protocol.trace import CaseTraceWriter

from .adapter import LmeExomemAdapter
from .bounds import BoundRun, Hypothesis, run_bounds
from .dataset import LmeDataset, QUESTION_TYPES, dump_dataset, load_dataset
from .fetch import file_sha256, verify_sha256
from .judge_io import _bound_ids, official_judge_commands
from .reader import ABSTENTION, ApiReader, Reader, StubReader
from .report import render_report
from .normalize import neutralize


class LmeRunInvalid(RuntimeError):
    """The run was invalidated by an environment fault and has no score."""

    def __init__(self, reason: str, run_dir: Path) -> None:
        super().__init__(reason)
        self.run_dir = run_dir


class FullRunApprovalRequired(PermissionError):
    """A full-suite request lacks measured pilot evidence or its approval."""


@dataclass(frozen=True)
class RunConfig:
    dataset: Path
    out: Path
    reader_name: str = "stub"
    run_id: str | None = None
    dataset_sha256: str | None = None
    metered_approval: str | None = None
    pilot_evidence: Path | None = None
    full_run_approval: str | None = None
    reader_model: str = "gpt-4o"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key_env: str = "OPENAI_API_KEY"
    claude_binary: str = "claude"
    top_k: int = 10
    pilot: int | None = None
    provider: str | None = None


@dataclass(frozen=True)
class RunResult:
    run_dir: Path
    question_count: int
    failure_count: int


def _default_run_id() -> str:
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"lme-{stamp}"


def _safe_id(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-.")
    return cleaned[:100] or "question"


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _reader(config: RunConfig, run_dir: Path) -> Reader:
    if config.reader_name == "stub":
        return StubReader()
    if config.reader_name == "openai":
        backend = OpenAICompatBackend(
            base_url=config.openai_base_url,
            model=config.reader_model,
            api_key_env=config.openai_api_key_env,
        )
        return ApiReader(
            backend=backend,
            approval_token=config.metered_approval,
            run_dir=run_dir,
        )
    if config.reader_name == "claude":
        backend = ClaudeCliBackend(binary=config.claude_binary, model=config.reader_model)
        return ApiReader(
            backend=backend,
            approval_token=config.metered_approval,
            run_dir=run_dir,
        )
    raise ValueError(f"unknown reader {config.reader_name!r}")


def _reader_model(reader: Reader, config: RunConfig) -> str:
    return "offline deterministic stub" if isinstance(reader, StubReader) else config.reader_model


def _fixture_path(path: Path) -> bool:
    return path.resolve().parent == (Path(__file__).resolve().parent / "fixtures")


def _validate_checksum(config: RunConfig) -> str:
    if config.dataset_sha256:
        return verify_sha256(config.dataset, config.dataset_sha256)
    if not _fixture_path(config.dataset):
        raise ValueError(
            "real LongMemEval data requires --dataset-sha256 recorded at first fetch"
        )
    return file_sha256(config.dataset)


def validate_full_run_gate(
    *,
    question_count: int,
    reader_name: str,
    pilot_evidence: Path | None,
    full_run_approval: str | None,
    is_pilot: bool,
) -> dict[str, object] | None:
    """Refuse every declared full run until generated pilot evidence is approved."""

    if is_pilot:
        return None
    if pilot_evidence is None or not Path(pilot_evidence).is_file():
        raise FullRunApprovalRequired(
            f"full {question_count}-question {reader_name} run requires recorded pilot evidence"
        )
    try:
        evidence = json.loads(Path(pilot_evidence).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FullRunApprovalRequired(f"pilot evidence is unreadable: {exc}") from exc
    required = {
        "scores",
        "ingest_wall_time_extrapolation_seconds",
        "api_cost_extrapolation",
        "pilot",
        "question_outcomes_sha256",
        "reader",
    }
    if not isinstance(evidence, dict) or not required.issubset(evidence):
        missing = sorted(required - set(evidence if isinstance(evidence, dict) else {}))
        raise FullRunApprovalRequired(
            f"pilot evidence is incomplete; missing {missing}"
        )
    if evidence.get("generated_by") != "benchmarks.lme.runner" or evidence.get("valid") is not True:
        raise FullRunApprovalRequired("pilot evidence was not generated by a valid LME pilot run")
    if evidence.get("reader") != reader_name:
        raise FullRunApprovalRequired(
            f"pilot evidence reader {evidence.get('reader')!r} does not match {reader_name!r}"
        )
    outcomes = Path(pilot_evidence).parent / "question-outcomes.jsonl"
    if not outcomes.is_file() or file_sha256(outcomes) != evidence["question_outcomes_sha256"]:
        raise FullRunApprovalRequired(
            "pilot evidence does not match its measured outcomes artifact"
        )
    if not isinstance(full_run_approval, str) or not full_run_approval.strip():
        raise FullRunApprovalRequired(
            "full run requires explicit founder approval recorded after the pilot evidence"
        )
    return evidence


def _ability(question) -> str:
    return "abstention" if question.is_abstention else question.question_type


def _select_pilot(dataset: LmeDataset, size: int) -> LmeDataset:
    if size < 1:
        raise ValueError("--pilot must be at least 1")
    if size > len(dataset.questions):
        raise ValueError(
            f"--pilot {size} exceeds the dataset's {len(dataset.questions)} questions"
        )
    buckets = defaultdict(deque)
    for question in dataset.questions:
        buckets[_ability(question)].append(question)
    order = (*QUESTION_TYPES, "abstention")
    represented = [ability for ability in order if buckets[ability]]
    if size < len(represented):
        raise ValueError(
            f"--pilot {size} cannot cover all {len(represented)} represented ability groups"
        )
    selected = []
    while len(selected) < size:
        progressed = False
        for ability in order:
            if buckets[ability] and len(selected) < size:
                selected.append(buckets[ability].popleft())
                progressed = True
        if not progressed:  # defensive: size was validated against total rows
            break
    return LmeDataset(tuple(selected))


def _ingest_outcome(adapter: object) -> tuple[list[dict[str, object]], float]:
    rows = []
    total_ms = 0.0
    for result in getattr(adapter, "last_ingest_results", ()) or ():
        latency_ms = float(getattr(result, "latency_ms", 0.0))
        total_ms += latency_ms
        rows.append(
            {
                "session_id": getattr(result, "source_id", None),
                "latency_ms": round(latency_ms, 6),
                "ok": bool(getattr(result, "ok", False)),
            }
        )
    return rows, round(total_ms / 1000.0, 6)


def _reader_outcome(reader: Reader, *, attempted: bool) -> dict[str, object]:
    metrics = getattr(reader, "last_call_metrics", None)
    return {
        "reader_call_count": int(getattr(metrics, "call_count", 1 if attempted else 0)),
        "reader_input_tokens": getattr(metrics, "input_tokens", None),
        "reader_output_tokens": getattr(metrics, "output_tokens", None),
        "reader_cost_usd": getattr(metrics, "cost_usd", None),
    }


def _pilot_evidence(
    pilot: dict[str, object],
    outcomes: list[dict[str, object]],
    *,
    valid: bool,
    outcomes_path: Path,
    reader_name: str,
    reader_model: str,
) -> dict[str, object]:
    ingest_seconds = sum(float(row["ingest_wall_time_seconds"]) for row in outcomes)
    observed = len(outcomes)
    ingest_extrapolation = (ingest_seconds / observed * 500) if observed else None
    costs = [row["reader_cost_usd"] for row in outcomes]
    measured_cost = (
        sum(float(cost) for cost in costs)
        if costs and all(isinstance(cost, (int, float)) for cost in costs)
        else None
    )
    extrapolated_cost = (
        measured_cost / observed * 500
        if measured_cost is not None and observed
        else None
    )
    return {
        "schema_version": 1,
        "generated_by": "benchmarks.lme.runner",
        "valid": valid,
        "pilot": pilot,
        "reader": reader_name,
        "reader_model": reader_model,
        "scores": {"status": "awaiting official judge"},
        "measured_ingest_wall_time_seconds": round(ingest_seconds, 6),
        "ingest_wall_time_extrapolation_seconds": (
            round(ingest_extrapolation, 6) if ingest_extrapolation is not None else None
        ),
        "api_cost_extrapolation": {
            "measured_reader_cost_usd": measured_cost,
            "extrapolated_500_question_cost_usd": extrapolated_cost,
        },
        "question_outcomes_sha256": file_sha256(outcomes_path),
    }


def _bound_rows(bounds: BoundRun, lane: str) -> list[dict[str, object]]:
    selected: tuple[Hypothesis, ...] = bounds.ceiling if lane == "ceiling" else bounds.floor
    return [row.as_dict() for row in selected]


def execute_run(
    config: RunConfig,
    *,
    reader: Reader | None = None,
    adapter_factory: Callable[[], object] = LmeExomemAdapter,
) -> RunResult:
    dataset_path = Path(config.dataset)
    dataset_checksum = _validate_checksum(config)
    parent_dataset = load_dataset(dataset_path)
    dataset = (
        _select_pilot(parent_dataset, config.pilot)
        if config.pilot is not None
        else parent_dataset
    )
    dataset_identity = DatasetIdentity(
        id="longmemeval",
        variant="LongMemEval-S cleaned September 2025",
        source="xiaowu0162/longmemeval-cleaned",
        revision=config.dataset_sha256 or "fixture-local",
        sha256=dataset_checksum,
        case_count=len(parent_dataset.questions),
    )
    pilot = (
        {
            "size": len(dataset.questions),
            "question_ids": [question.question_id for question in dataset.questions],
        }
        if config.pilot is not None
        else None
    )
    run_id = config.run_id or _default_run_id()
    run_dir = Path(config.out) / run_id
    # ApiReader validates the metered gate without touching the filesystem.
    # Construct it before reserving an immutable run id so a refusal is retryable.
    active_reader = reader or _reader(config, run_dir)
    pilot_evidence = (
        None
        if _fixture_path(dataset_path)
        else validate_full_run_gate(
            question_count=len(dataset.questions),
            reader_name=config.reader_name,
            pilot_evidence=config.pilot_evidence,
            full_run_approval=config.full_run_approval,
            is_pilot=config.pilot is not None,
        )
    )
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise FileExistsError(f"run directory is immutable and already exists: {run_dir}") from exc
    provider_variant = config.provider or "exomem-source-only"
    control_config_sha256 = None
    if config.provider:
        from .providers.registry import provider_factory
        candidate = provider_factory(config.provider)()
        config_value = getattr(candidate, "config", None)
        config_hash = getattr(config_value, "sha256", None)
        control_config_sha256 = config_hash() if callable(config_hash) else None
    start_manifest(
        run_dir, run_id=run_id, dataset=dataset_identity,
        started_at=dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        provider_variant=provider_variant, control_config_sha256=control_config_sha256,
        leakage=LeakageSummary(scanned_cases=0, invalidated_cases=0),
        budget=BudgetSummary(cap_usd=0, committed_usd=0, refusals=0),
    )
    dump_dataset(dataset, run_dir / "dataset.json")
    environment = capture_environment()
    environment["lme"] = {
        "dataset_sha256": dataset_checksum,
        "dataset_source": "xiaowu0162/longmemeval-cleaned",
        "dataset_variant": "LongMemEval-S cleaned September 2025",
        "reader": config.reader_name,
        "reader_model": _reader_model(active_reader, config),
        "provider_variant": provider_variant,
        "judge_protocol": "official evaluate_qa.py, unmodified",
        "judge_model": "gpt-4o",
        "metered_approval": config.metered_approval,
        "full_run_approval": config.full_run_approval,
        "pilot_evidence": pilot_evidence,
        "pilot": pilot,
        "retrieval_clock": "question_date",
        "dataset_warnings": {
            question.question_id: list(question.validation_warnings)
            for question in dataset.questions
            if question.validation_warnings
        },
    }
    _write_json(run_dir / "environment.json", environment)

    failures: list[dict[str, object]] = []
    outcomes: list[dict[str, object]] = []
    hypotheses: list[dict[str, object]] = []
    invalid_reason: str | None = None
    readiness_list = []
    contamination_results: list[str] = []
    started = time.perf_counter()
    for case_ordinal, question in enumerate(dataset.questions, 1):
        question_started = time.perf_counter()
        adapter = adapter_factory()
        reader_attempted = False
        reader_wall_time = 0.0
        try:
            trace = CaseTraceWriter(run_dir, question.question_id)
            if config.provider:
                from .providers.registry import provider_factory
                provider = provider_factory(config.provider)()
                provider.setup(None)
                events = neutralize(question, dataset_identity)
                # Harness-authored presence canary in a non-evidence filler turn.
                token = canary_for(run_id, question.question_id, "presence")
                filler_content = events[-1].content + f"\n[harness canary {token}]"
                filler = events[-1].model_copy(update={"content": filler_content, "content_sha256": __import__("hashlib").sha256(filler_content.encode()).hexdigest()})
                events = [*events[:-1], filler]
                inserted = provider.ingest_case(events, CaseHandle(case_id=question.question_id, case_ordinal=case_ordinal, question_date=question.question_date_text))
                trace.append({"record": "ingest", "session_ordinal": 1, "payload_sha256": file_sha256(run_dir / "dataset.json"), "provider_ids": list(inserted or [])})
                if not readiness_list:
                    readiness_list.extend(provider.readiness())
                hits = provider.retrieve(question.question, config.top_k)
                retrieved = [hit.text for hit in hits]
                trace.append({"record": "search", "query": question.question, "raw_response_ref": "inline:provider-hit-list", "normalized_hit_ids": [hit.hit_id for hit in hits], "normalized_hit_shas": [__import__("hashlib").sha256(hit.text.encode()).hexdigest() for hit in hits], "top_k": config.top_k})
                contamination_results.append(evaluate_probes({"presence": bool(provider.retrieve(token, 1)), "cross_case": bool(provider.retrieve(canary_for(run_id, question.question_id + "-other", "cross_case"), 1)), "never_ingested": bool(provider.retrieve(canary_for(run_id, question.question_id, "never_ingested"), 1))}))
                provider.cleanup()
            else:
                retrieved = adapter.run_question(
                    question,
                    run_dir / "questions" / _safe_id(question.question_id),
                    dataset_identity=dataset_identity,
                    case_ordinal=case_ordinal,
                    limit=config.top_k,
                )
                trace.append({"record": "ingest", "session_ordinal": 1, "payload_sha256": file_sha256(run_dir / "dataset.json"), "provider_ids": []})
                trace.append({"record": "search", "query": question.question, "raw_response_ref": "inline:adapter-hit-list", "normalized_hit_ids": [], "normalized_hit_shas": [__import__("hashlib").sha256(text.encode()).hexdigest() for text in retrieved], "top_k": config.top_k})
            reader_attempted = True
            reader_started = time.perf_counter()
            hypothesis = active_reader.answer(question, retrieved)
            reader_wall_time = time.perf_counter() - reader_started
            status = "ok"
            trace.append({"record": "timing", "phase": "retrieve-and-read", "ms": (time.perf_counter() - question_started) * 1000.0})
        except AdapterEnvironmentError as exc:
            invalid_reason = f"{question.question_id}: {exc}"
            failures.append(
                {"question_id": question.question_id, "phase": "environment", "detail": str(exc)}
            )
            break
        except Exception as exc:  # retained as a question failure, never dropped
            if reader_attempted:
                reader_wall_time = time.perf_counter() - reader_started
            status = "failed"
            hypothesis = ABSTENTION
            failures.append(
                {
                    "question_id": question.question_id,
                    "phase": "retrieve-or-read",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
        hypotheses.append(
            {"question_id": question.question_id, "hypothesis": hypothesis}
        )
        ingest_sessions, ingest_wall_time = _ingest_outcome(adapter)
        outcomes.append(
            {
                "question_id": question.question_id,
                "status": status,
                "wall_time_seconds": round(time.perf_counter() - question_started, 6),
                "ingest_sessions": ingest_sessions,
                "ingest_wall_time_seconds": ingest_wall_time,
                "reader_wall_time_seconds": round(reader_wall_time, 6),
                **_reader_outcome(active_reader, attempted=reader_attempted),
            }
        )

    bounds_dir = run_dir / "bounds"
    bounds_dir.mkdir()
    if invalid_reason is None:
        bounds = run_bounds(dataset, active_reader)
        failures.extend(bounds.failures)
        _write_jsonl(
            bounds_dir / "gold-evidence-ceiling.jsonl", _bound_rows(bounds, "ceiling")
        )
        _write_jsonl(
            bounds_dir / "null-abstain-floor.jsonl", _bound_rows(bounds, "floor")
        )
        _write_jsonl(run_dir / "hypotheses.jsonl", hypotheses)
    else:
        # Empty immutable artifacts make the invalid state explicit without
        # preserving a partial score that could be mistaken for a pilot.
        _write_jsonl(bounds_dir / "gold-evidence-ceiling.jsonl", [])
        _write_jsonl(bounds_dir / "null-abstain-floor.jsonl", [])
    _write_jsonl(run_dir / "failures.jsonl", failures)
    outcomes_path = run_dir / "question-outcomes.jsonl"
    _write_jsonl(outcomes_path, outcomes)
    if pilot is not None:
        _write_json(
            run_dir / "pilot-evidence.json",
            _pilot_evidence(
                pilot,
                outcomes,
                valid=invalid_reason is None,
                outcomes_path=outcomes_path,
                reader_name=config.reader_name,
                reader_model=_reader_model(active_reader, config),
            ),
        )

    manifest = {
        "run_id": run_id,
        "dataset_sha256": dataset_checksum,
        "question_count": len(dataset.questions),
        "attempted_questions": len(outcomes),
        "failure_count": len(failures),
        "invalid": invalid_reason is not None,
        "invalid_reason": invalid_reason,
        "reader": config.reader_name,
        "provider_variant": provider_variant,
        "reader_model": _reader_model(active_reader, config),
        "metered_approval": config.metered_approval,
        "full_run_approval": config.full_run_approval,
        "pilot_evidence": pilot_evidence,
        "pilot": pilot,
        "judge_model": "gpt-4o",
        "retrieval_clock": "question_date",
        "dataset_warnings": environment["lme"]["dataset_warnings"],
        "wall_time_seconds": round(time.perf_counter() - started, 6),
    }
    _write_json(run_dir / "run.json", manifest)
    (run_dir / "OFFICIAL_JUDGE_COMMAND.txt").write_text(
        official_judge_commands(run_dir), encoding="utf-8"
    )
    ceiling_ids = _bound_ids(run_dir, "ceiling")
    floor_ids = _bound_ids(run_dir, "floor")
    (run_dir / "report.md").write_text(
        render_report(
            dataset,
            labels={},
            ceiling_question_ids=ceiling_ids,
            floor_question_ids=floor_ids,
            invalid_reason=invalid_reason,
        ),
        encoding="utf-8",
    )
    readiness_status = "READINESS_UNVERIFIABLE" if any(item.method == "readiness-unverifiable" for item in readiness_list) else "VALID"
    contamination = (
        "contaminated" if "contaminated" in contamination_results
        else "unverifiable" if "unverifiable" in contamination_results
        else "isolated" if contamination_results else "unverifiable"
    )
    finalize_manifest(
        run_dir,
        status="INVALID" if invalid_reason is not None else readiness_status,
        finalized_at=dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        readiness=readiness_list,
        leakage=LeakageSummary(scanned_cases=len(dataset.questions), invalidated_cases=1 if invalid_reason else 0),
        contamination=contamination,
        budget=BudgetSummary(cap_usd=0, committed_usd=0, refusals=0),
    )
    if invalid_reason is not None:
        raise LmeRunInvalid(invalid_reason, run_dir)
    return RunResult(
        run_dir=run_dir,
        question_count=len(dataset.questions),
        failure_count=len(failures),
    )
