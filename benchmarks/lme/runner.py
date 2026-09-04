"""Immutable offline-first LongMemEval-S run pipeline."""

from __future__ import annotations

import datetime as dt
import hashlib
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
from protocol.budget import BudgetLedger
from protocol.canary import canary_for, evaluate_probes
from protocol.custody import HeldDirectory, hold_directory
from protocol.leakage import scan_ingest
from protocol.manifest import bind_started_manifest_provider, finalize_manifest, start_manifest
from protocol.models import BudgetSummary, CaseGold, CaseHandle, DatasetIdentity, EventProvenance, LaneReadiness, LeakageSummary, ProtocolEvent, ProbeResult
from protocol.namespace import derive_namespace, namespace_pattern
from protocol.readiness import validate as validate_readiness
from protocol.trace import CaseTraceWriter

from .providers.base import ProviderDescriptor, ProviderSessionContext, RetrievalPurpose
from .providers.lifecycle import (
    CleanupUnproved,
    LifecycleEvidence,
    LifecycleCustody,
    RETIRE_MAX_DEPTH,
    RETIRE_MAX_ENTRIES,
    ProviderConstructionFailure,
    LifecycleRunError,
    bind_observed_variant,
    reset_observed_variant,
    run_provider_lifecycle,
    terminalize_constructor_failure,
)

from .adapter import LmeExomemAdapter, lme_profile
from .bounds import BoundRun, Hypothesis, run_bounds
from .dataset import (
    DatasetValidationError,
    LmeDataset,
    QUESTION_TYPES,
    dump_dataset,
    load_dataset_bytes,
    stable_dataset_bytes,
)
from .fetch import file_sha256, verify_sha256
from .judge_io import _bound_ids, official_judge_commands
from .reader import ABSTENTION, ApiReader, Reader, StubReader, _require_approval
from .report import manifest_banner, render_report
from .normalize import ingest_field_groups, neutralize, render_neutral_session
from equivalence.selection import CANONICAL_LME_S_SOURCE, load_frozen_lme_selection, select_lme_s_25


def _dataset_case_count(dataset: LmeDataset) -> int:
    """Rows in the pinned source, including any this parse deferred.

    `DatasetIdentity` pairs this count with a sha256 taken over the whole file,
    so it has to describe the file rather than our yield from it. Scoped
    deferral makes those two numbers diverge: `len(questions)` silently became
    "rows we could parse" the moment a bad row could be carried instead of
    raised, which would label a 500-row digest as 493 cases.

    The census covers every source row. It is empty only for datasets built
    directly rather than loaded — pilot slices, fixtures — where the questions
    are all the rows there are.
    """
    return len(dataset.census) or len(dataset.questions)


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
    dataset_revision: str | None = None
    metered_approval: str | None = None
    pilot_evidence: Path | None = None
    full_run_approval: str | None = None
    reader_model: str = "gpt-4o"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key_env: str = "OPENAI_API_KEY"
    claude_binary: str = "claude"
    top_k: int = 10
    pilot: int | None = None
    canonical_selection: bool = False
    provider: str | None = None
    budget_cap_usd: float = 0.0


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


_DIRECT_RUN_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_FOREGROUND_EXECUTION_MODEL = "in-process-no-post-return-background"
# A competitor row runs its own provider class under its own project
# environment (design decision 1), so it cannot be in-process.  Declaring the
# foreground model for it would assert an execution property nothing proves,
# so it declares its own and owes the extra process-absence surface instead.
_OWNED_SUBPROCESS_EXECUTION_MODEL = "owned-subprocess-terminated-at-cleanup"
_SUPPORTED_EXECUTION_MODELS = frozenset(
    {_FOREGROUND_EXECUTION_MODEL, _OWNED_SUBPROCESS_EXECUTION_MODEL}
)


def _direct_run_component(value: str) -> str:
    if not isinstance(value, str) or _DIRECT_RUN_COMPONENT.fullmatch(value) is None:
        raise ValueError("direct-provider run id must be one safe path component")
    return value


def _internal_session_id(ordinal: int, logical_id: str) -> str:
    digest = hashlib.sha256(logical_id.encode("utf-8")).hexdigest()[:16]
    return f"session-{ordinal:06d}-{digest}"


def _closed_failure_code(error: BaseException, fallback: str) -> str:
    if type(error) in {CleanupUnproved, LifecycleRunError, ProviderConstructionFailure}:
        fact = error.fact
        return fact if isinstance(fact, str) and fact else fallback
    return fallback


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
        if not _fixture_path(config.dataset) and not config.dataset_revision:
            raise ValueError("real LongMemEval data requires --dataset-revision")
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
    is_canonical_selection: bool = False,
) -> dict[str, object] | None:
    """Refuse every declared full run until generated pilot evidence is approved."""

    if is_pilot or is_canonical_selection:
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


def _canonical_selection(dataset: LmeDataset, dataset_path: Path, config: RunConfig) -> tuple[LmeDataset, dict[str, str]]:
    if config.pilot is not None:
        raise ValueError("--pilot cannot substitute for canonical comparative selection")
    if config.dataset_sha256 != CANONICAL_LME_S_SOURCE["sha256"]:
        raise ValueError("canonical selection requires the frozen source SHA-256")
    if config.dataset_revision != CANONICAL_LME_S_SOURCE["revision"]:
        raise ValueError("canonical selection requires the frozen source revision")
    try:
        artifact, raw = load_frozen_lme_selection()
    except Exception as exc:
        raise ValueError(f"canonical selection artifact is invalid: {exc}") from exc
    if artifact["source_identity"] != CANONICAL_LME_S_SOURCE:
        raise ValueError("canonical selection artifact source identity differs")
    # Regenerate against the full source census, not the rows this loader
    # accepted: the cohort is a property of the frozen source, and selecting
    # from a subset would silently produce a different, smaller-universe cohort.
    regenerated = select_lme_s_25(
        [{"question_id": identity, "question_type": kind} for identity, kind in dataset.census],
        source=CANONICAL_LME_S_SOURCE,
    )
    if artifact["target_question_ids"] != regenerated["target_question_ids"]:
        raise ValueError("canonical selection artifact does not match regenerated membership and order")
    selected = artifact["target_question_ids"]
    # `require` re-raises a deferred validation error, so a cohort row that
    # could not be loaded fails loudly here rather than being quietly dropped.
    return LmeDataset(tuple(dataset.require(question_id) for question_id in selected)), {
        "selection_artifact_path": "benchmarks/equivalence/subsets/lme-s-25.json",
        "selection_artifact_sha256": hashlib.sha256(raw).hexdigest(),
        "selection_algorithm_version": artifact["selection_algorithm_version"],
    }


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


def _budget_summary(ledger: BudgetLedger) -> BudgetSummary:
    """Summarise the real append-only ledger; never substitute made-up zeros."""

    entries = ledger._entries()
    if not entries:
        raise RuntimeError("budget ledger has no recorded operations")
    return BudgetSummary(
        cap_usd=float(ledger.caps["usd"]),
        committed_usd=round(sum(entry.units for entry in entries if entry.kind == "commit"), 8),
        refusals=sum(entry.decision == "refused-cap" for entry in entries),
    )


def _hit_contains(hits: object, token: str) -> bool:
    """A probe hits only when the canary token is in the RETURNED HIT TEXT.

    A dense control returns its top-k for any query, so "a hit came back" would
    mark every run contaminated; only the token's presence is evidence.
    """

    return any(token in str(getattr(hit, "text", "")) for hit in hits or ())


def _canary_verdict(hits_by_kind: dict[str, bool], *, retains_nothing: bool) -> str:
    """Classify canary probes, honouring a control that stores nothing by design.

    The negative control can never retrieve its own presence canary. Scoring
    that as ``unverifiable`` would invalidate the floor the control exists to
    provide, so presence is inconclusive-by-design there — while a cross-case
    or never-ingested hit still contaminates the run exactly as it does
    everywhere else.
    """

    if not retains_nothing:
        return evaluate_probes(hits_by_kind)
    if hits_by_kind.get("cross_case") or hits_by_kind.get("never_ingested"):
        return "contaminated"
    return "isolated"


#: The differ's twelve keys, written per case so `equivalence gate` can run
#: against two REAL run directories. Every value is run-invariant on purpose:
#: run ids, wall clocks, and the run-scoped namespace suffix would otherwise
#: make two honest runs of the same dataset differ on every key. The literal
#: per-case namespace stays in the manifest; the equivalence key records the
#: derivation pattern, which is what the design's namespace check compares.
def _equivalence_case(
    *, question, case_ids: list[str], namespace_pattern: str, payload_shas: list[str],
    readiness: list, retrieved_ids: list[str], retrieved_text: list[str], top_k: int,
    dataset_identity: DatasetIdentity, reader_name: str, reader_model: str,
) -> dict[str, object]:
    from .reader import CONTEXT_SEPARATOR

    return {
        "case_id": question.question_id,
        "dataset_identity": dataset_identity.model_dump(),
        "case_set": sorted(case_ids),
        "session_normalization": "lme.normalize.render_neutral_session/v1",
        "namespace": namespace_pattern,
        "ingestion_payloads": payload_shas,
        "readiness": [
            {key: lane.model_dump()[key] for key in ("lane", "requested", "verified", "method", "fallback_detected")}
            for lane in readiness
        ],
        "exact_query": question.question,
        "top_k": top_k,
        "retrieved_ids": retrieved_ids,
        "retrieved_text": retrieved_text,
        "packed_context": CONTEXT_SEPARATOR.join(retrieved_text),
        "answer_judge_prompt_model_config": {
            "reader": reader_name,
            "reader_model": reader_model,
            "judge_model": "gpt-4o",
            "prompt_sha256": hashlib.sha256(question.question.encode()).hexdigest(),
        },
    }


def _harness_canary(events: list[ProtocolEvent], handle: CaseHandle, token: str) -> ProtocolEvent:
    """Append a pure harness-authored filler event without touching dataset payloads."""

    content = f"Harness diagnostic filler. Token: {token}."
    return ProtocolEvent(
        dataset=events[0].dataset, case_id=handle.case_id,
        session_ordinal=max(event.session_ordinal for event in events) + 1,
        sequence=0, role="user", turn_ordinal=1, content=content,
        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        original_timestamp=events[-1].original_timestamp,
        timestamp_semantics="ingestion_order_only", ingestion_ordinal=max(event.ingestion_ordinal for event in events) + 1,
        provenance=EventProvenance(dataset_row_index=0, upstream_session_id_sha256=hashlib.sha256(f"harness:{handle.case_id}".encode()).hexdigest(), converter="harness-canary", converter_version="1"),
    )


def _run_probes(
    provider: object,
    identity: DatasetIdentity,
    run_id: str,
    *,
    check_variant: Callable[[], str] | None = None,
) -> tuple[list[ProbeResult], list[object]]:
    """Execute all declared probes and retain their evidence as typed records."""
    from protocol.probes import classify_update_outcome, known_answer_probe_specs

    def checked(call):
        value = call()
        if check_variant is not None:
            check_variant()
        return value

    def probe_event(*, case_id: str, content: str, sequence: int) -> ProtocolEvent:
        return ProtocolEvent(
            dataset=identity,
            case_id=case_id,
            session_ordinal=1,
            sequence=sequence,
            role="user",
            turn_ordinal=sequence + 1,
            content=content,
            content_sha256=hashlib.sha256(content.encode()).hexdigest(),
            original_timestamp="2026-01-01T00:00:00Z",
            timestamp_semantics="ingestion_order_only",
            ingestion_ordinal=sequence,
            provenance=EventProvenance(
                dataset_row_index=0,
                upstream_session_id_sha256=hashlib.sha256(case_id.encode()).hexdigest(),
                converter="harness-probe",
                converter_version="1",
            ),
        )

    results: list[ProbeResult] = []
    for ordinal, spec in enumerate(known_answer_probe_specs(), 1):
        case_id = f"__probe__-{spec.kind}"
        handle = CaseHandle(case_id=case_id, case_ordinal=ordinal, question_date="2026-01-01T00:00:00Z")
        # Gated on a DECLARED capability, not on a variant name: a control that
        # stores nothing cannot answer a known-answer probe, and renaming the
        # row must not silently turn that into a probe failure.
        if getattr(provider, "retains_nothing", False):
            results.append(ProbeResult(case_id=case_id, probe_kind=spec.kind, outcome="inconclusive-by-design", detail="provider declares it retains nothing, so a known-answer probe is inapplicable"))
            continue
        if spec.kind == "update-current-state":
            for sequence, marker in enumerate((spec.old_marker, spec.current_marker)):
                event = probe_event(case_id=case_id, content=marker, sequence=sequence)
                checked(lambda event=event: provider.ingest_case([event], handle))
            hits = checked(
                lambda: provider.retrieve(spec.query, 2, RetrievalPurpose.POSITIVE_PROBE)
            )
            outcome = classify_update_outcome(
                [str(getattr(hit, "text", "")) for hit in hits],
                old_marker=spec.old_marker,
                current_marker=spec.current_marker,
            )
            results.append(ProbeResult(
                case_id=case_id,
                probe_kind=spec.kind,
                outcome=outcome,
                hits=[str(getattr(hit, "hit_id", "")) for hit in hits],
                detail="exact returned marker membership classified by declared update sequence",
            ))
            continue
        token = f"probe-{hashlib.sha256((run_id + spec.kind).encode()).hexdigest()[:16]}"
        content = f"{spec.fact}: {token}."
        event = probe_event(case_id=case_id, content=content, sequence=0)
        checked(lambda: provider.ingest_case([event], handle))
        query = spec.query if spec.kind == "semantic-zero-overlap" else token
        top_k = 10 if spec.kind == "semantic-zero-overlap" else 1
        hits = checked(lambda: provider.retrieve(query, top_k, RetrievalPurpose.POSITIVE_PROBE))
        if spec.kind == "semantic-zero-overlap":
            passed = any(
                spec.fact in str(getattr(hit, "text", ""))
                and token in str(getattr(hit, "text", ""))
                for hit in hits
            )
            detail = "returned hit text contained the exact declared fact and marker" if passed else "declared fact identity absent from returned hit text"
        else:
            passed = _hit_contains(hits, token)
            detail = "returned hit text contained probe token" if passed else "probe token absent from returned hit text"
        results.append(ProbeResult(
            case_id=case_id,
            probe_kind=spec.kind,
            outcome="pass" if passed else "fail",
            hits=[str(getattr(hit, "hit_id", "")) for hit in hits],
            detail=detail,
        ))
    return results, _readiness_with_probe_evidence(provider.readiness(), results)


def _readiness_with_probe_evidence(
    readiness: list[LaneReadiness], probes: list[ProbeResult]
) -> list[LaneReadiness]:
    """Replace an explicit semantic-probe deferral with its measured result."""

    semantic_passed = any(
        probe.probe_kind == "semantic-zero-overlap" and probe.outcome == "pass"
        for probe in probes
    )
    if not semantic_passed:
        return list(readiness)
    return [
        LaneReadiness(
            lane=lane.lane,
            requested=True,
            verified=True,
            method="semantic-probe",
            evidence="semantic-zero-overlap known-answer probe passed",
            fallback_detected=False,
        )
        if (
            lane.lane == "semantic"
            and lane.requested
            and not lane.verified
            and lane.method == "readiness-unverifiable"
            and not lane.fallback_detected
        )
        else lane
        for lane in readiness
    ]


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
    raw_dataset = stable_dataset_bytes(dataset_path)
    dataset_checksum = hashlib.sha256(raw_dataset).hexdigest()
    if config.dataset_sha256:
        if not _fixture_path(dataset_path) and not config.dataset_revision:
            raise ValueError("real LongMemEval data requires --dataset-revision")
        if dataset_checksum != config.dataset_sha256:
            raise ValueError("dataset SHA-256 differs from recorded source")
    elif not _fixture_path(dataset_path):
        raise ValueError("real LongMemEval data requires --dataset-sha256 recorded at first fetch")
    parent_dataset = load_dataset_bytes(raw_dataset)
    del raw_dataset
    selection_pins: dict[str, str] = {}
    if config.canonical_selection:
        dataset, selection_pins = _canonical_selection(parent_dataset, dataset_path, config)
    else:
        # Deferral is scoped to cohort runs. Every other path still covers the
        # whole dataset, so an unloadable row must refuse here rather than
        # quietly vanish from the denominator.
        if parent_dataset.deferred_errors:
            raise DatasetValidationError(
                sorted(parent_dataset.deferred_errors.values())[0]
            )
        dataset = _select_pilot(parent_dataset, config.pilot) if config.pilot is not None else parent_dataset
    dataset_identity = DatasetIdentity(
        id="longmemeval",
        variant="LongMemEval-S cleaned September 2025",
        source="xiaowu0162/longmemeval-cleaned",
        revision=(CANONICAL_LME_S_SOURCE["revision"] if config.canonical_selection else config.dataset_revision or "fixture-local"),
        sha256=dataset_checksum,
        case_count=_dataset_case_count(parent_dataset),
    )
    pilot = (
        {
            "size": len(dataset.questions),
            "question_ids": [question.question_id for question in dataset.questions],
        }
        if config.pilot is not None
        else None
    )
    run_id = _default_run_id() if config.run_id is None else config.run_id
    provider_variant = None
    provider_spec_value = None
    provider_kind = "exomem"
    if config.provider:
        from .providers.registry import provider_spec

        _direct_run_component(run_id)
        provider_spec_value = provider_spec(config.provider)
        descriptor = provider_spec_value.descriptor
        if (
            not isinstance(descriptor, ProviderDescriptor)
            or descriptor.execution_model not in _SUPPORTED_EXECUTION_MODELS
        ):
            raise ValueError("direct provider has an unsupported execution model")
        provider_kind = provider_spec_value.namespace_kind
        reset_observed_variant(run_id)
    run_dir = Path(config.out) / run_id
    # Reader approval and full-run evidence are preflight checks: a refusal
    # must not leave an immutable-looking run directory behind.
    pilot_evidence = (
        None
        if _fixture_path(dataset_path)
        else validate_full_run_gate(
            question_count=len(dataset.questions),
            reader_name=config.reader_name,
            pilot_evidence=config.pilot_evidence,
            full_run_approval=config.full_run_approval,
            is_pilot=config.pilot is not None,
            is_canonical_selection=config.canonical_selection,
        )
    )
    if reader is None and config.reader_name in {"openai", "claude"}:
        _require_approval(config.metered_approval)
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise FileExistsError(f"run directory is immutable and already exists: {run_dir}") from exc
    run_custody: HeldDirectory | None = None
    session_parent: HeldDirectory | None = None
    work_parent: HeldDirectory | None = None
    evidence_parent: HeldDirectory | None = None
    start_manifest(
        run_dir, run_id=run_id, dataset=dataset_identity,
        started_at=dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        provider_variant=provider_variant,
        namespaces={question.question_id: derive_namespace(run_id, question.question_id, provider_kind) for question in dataset.questions},
        pins=selection_pins,
    )
    # The legacy adapter has no direct provider instance to observe.  Its
    # report label remains historical presentation metadata; the immutable
    # manifest stays unbound (`null`).
    if not config.provider:
        provider_variant = "exomem-source-only"

    active_reader = reader or _reader(config, run_dir)

    # Construction is deliberately deferred until after the started manifest.
    control_config_sha256 = None
    ledger = BudgetLedger(run_dir, caps={"usd": config.budget_cap_usd})
    ledger.reserve(ts=dt.datetime.now(dt.UTC).isoformat(), seq=0, actor="lme-runner", op="stub-reader-budget", units=0)
    ledger.commit(ts=dt.datetime.now(dt.UTC).isoformat(), seq=1, actor="lme-runner", op="stub-reader-budget", units=0)
    dump_dataset(dataset, run_dir / "dataset.json")
    environment = capture_environment()
    lifecycle_attempts: list[dict[str, object]] = []
    environment["lme"] = {
        "dataset_sha256": dataset_checksum,
        "dataset_source": "xiaowu0162/longmemeval-cleaned",
        "dataset_variant": "LongMemEval-S cleaned September 2025",
        "reader": config.reader_name,
        "reader_model": _reader_model(active_reader, config),
        "provider_variant": provider_variant,
        "requested_provider": config.provider or "legacy-adapter",
        "diagnostic_harness_revision": "direct-lifecycle-2.3a",
        "lifecycle_attempts": lifecycle_attempts,
        "lifecycle_expected_instances": [],
        "judge_protocol": "official evaluate_qa.py, unmodified",
        "judge_model": "gpt-4o",
        "metered_approval": config.metered_approval,
        "full_run_approval": config.full_run_approval,
        "pilot_evidence": pilot_evidence,
        "pilot": pilot,
        "canonical_selection": config.canonical_selection,
        "selection_mode": "canonical" if config.canonical_selection else ("generic-pilot" if len(dataset.questions) == 25 else None),
        "selection": selection_pins or None,
        "retrieval_clock": "question_date",
        "dataset_warnings": {
            question.question_id: list(question.validation_warnings)
            for question in dataset.questions
            if question.validation_warnings
        },
    }
    if config.provider in (None, "exomem-source-only"):
        profile = lme_profile()
        environment["lme"]["requested_profile"] = {
            "name": profile.name, "settings": profile.settings,
        }
    _write_json(run_dir / "environment.json", environment)

    failures: list[dict[str, object]] = []
    outcomes: list[dict[str, object]] = []
    hypotheses: list[dict[str, object]] = []
    invalid_reason: str | None = None
    readiness_list = []
    contamination_by_case: dict[str, str] = {}
    leakage_findings: list[object] = []
    probe_results: list[ProbeResult] = []
    equivalence_cases: list[dict[str, object]] = []
    lifecycle_instances: list[dict[str, object]] = []
    isolation_rows: list[dict[str, object]] = []
    prior_presence_token: str | None = None
    control_flow: BaseException | None = None
    case_ids = [question.question_id for question in dataset.questions]

    def prepare_direct_context(
        *, internal_session_id: str, logical_question_id: str | None, namespace: str,
    ) -> tuple[ProviderSessionContext, LifecycleCustody, dict[str, object]]:
        assert session_parent is not None and work_parent is not None and evidence_parent is not None
        session: HeldDirectory | None = None
        work: HeldDirectory | None = None
        evidence: HeldDirectory | None = None
        try:
            session = session_parent.mkdir(
                internal_session_id,
                logical_ref=Path("sessions") / internal_session_id,
            )
            work = work_parent.mkdir(
                internal_session_id,
                logical_ref=Path("work") / internal_session_id,
            )
            evidence = evidence_parent.mkdir(
                internal_session_id,
                logical_ref=Path("evidence") / internal_session_id,
            )
            custody = LifecycleCustody(session=session, work=work, evidence=evidence)
            custody.assert_bound()
        except BaseException:
            for held in (evidence, work, session):
                if held is not None:
                    try:
                        held.retire(max_entries=RETIRE_MAX_ENTRIES, max_depth=RETIRE_MAX_DEPTH)
                    except BaseException:
                        pass
                    held.close()
            raise
        context = ProviderSessionContext(
            run_id=run_id,
            session_id=internal_session_id,
            namespace=namespace,
            work_root=work.capability_path,
            evidence_root=evidence.capability_path,
            work_ref=work.logical_ref,
            evidence_ref=evidence.logical_ref,
        )
        attempt: dict[str, object] = {
            "internal_session_id": internal_session_id,
            "logical_question_id": logical_question_id,
            "factory_returned": False,
            "setup_completed": False,
            "provider_variant": None,
            "failure_code": None,
        }
        lifecycle_attempts.append(attempt)
        return context, custody, attempt

    def retire_direct_context(custody: LifecycleCustody) -> tuple[str, ...]:
        facts: list[str] = []
        try:
            for label, held in (("work", custody.work), ("session", custody.session)):
                try:
                    if not held.retire(max_entries=RETIRE_MAX_ENTRIES, max_depth=RETIRE_MAX_DEPTH):
                        facts.append(f"{label}_root_binding_lost")
                except BaseException as exc:
                    if not isinstance(exc, Exception):
                        raise
                    facts.append(f"{label}_root_retirement_failed")
        finally:
            custody.close()
        return tuple(facts)

    def preserve_failure_through_retirement(
        primary: BaseException,
        custody: LifecycleCustody,
        attempt: dict[str, object],
    ) -> None:
        try:
            retire_direct_context(custody)
        except BaseException as secondary:
            if isinstance(primary, Exception) and not isinstance(secondary, Exception):
                raise secondary
            attempt["secondary_failure_code"] = _closed_failure_code(
                secondary,
                "runner_retirement_control_flow" if not isinstance(secondary, Exception) else "runner_retirement_failed",
            )
        raise primary

    def close_run_custody() -> None:
        for held in (evidence_parent, work_parent, session_parent, run_custody):
            if held is not None:
                held.close()

    def record_lifecycle(evidence: LifecycleEvidence, trace_writer: Callable[[], CaseTraceWriter]) -> None:
        # Register the expected instance first. A trace persistence failure is
        # then an explicit incomplete ledger, never an unowned observation.
        lifecycle_instances.append(evidence.expected_instance(run_dir))
        trace_writer().append(evidence.trace_record(run_dir))

    if config.provider:
        assert provider_spec_value is not None
        diagnostic_logical_id = "__diagnostic__"
        diagnostic_id = _internal_session_id(0, diagnostic_logical_id)
        try:
            run_custody = hold_directory(run_dir, logical_ref=Path("."))
            run_custody.prove_supported()
            session_parent = run_custody.mkdir("sessions", logical_ref=Path("sessions"))
            work_parent = run_custody.mkdir("work", logical_ref=Path("work"))
            evidence_parent = run_custody.mkdir("evidence", logical_ref=Path("evidence"))
            diagnostic_context, diagnostic_custody, diagnostic_attempt = prepare_direct_context(
                internal_session_id=diagnostic_id,
                logical_question_id=None,
                namespace=provider_spec_value.derive_namespace(run_id, diagnostic_logical_id),
            )
        except BaseException:
            close_run_custody()
            raise
        diagnostic_trace: CaseTraceWriter | None = None

        def get_diagnostic_trace() -> CaseTraceWriter:
            nonlocal diagnostic_trace
            if diagnostic_trace is None:
                diagnostic_trace = CaseTraceWriter(run_dir, diagnostic_id, schema_version=2)
            return diagnostic_trace

        def run_diagnostics(candidate: object) -> tuple[list[ProbeResult], list[object]]:
            nonlocal control_config_sha256, provider_variant
            diagnostic_result = _run_probes(
                candidate,
                dataset_identity,
                run_id,
                check_variant=lambda: bind_observed_variant(diagnostic_context, candidate),
            )
            probes, diagnostic_readiness = diagnostic_result
            probe_results.extend(probes)
            readiness_list.extend(diagnostic_readiness)
            with (run_dir / "probes.jsonl").open("a", encoding="utf-8") as probe_file:
                for probe in probes:
                    probe_file.write(probe.model_dump_json() + "\n")
            provider_variant = bind_observed_variant(diagnostic_context, candidate)
            environment["lme"]["provider_variant"] = provider_variant
            config_value = getattr(candidate, "config", None)
            config_hash = getattr(config_value, "sha256", None)
            control_config_sha256 = config_hash() if callable(config_hash) else None
            bind_started_manifest_provider(
                run_dir,
                provider_variant=provider_variant,
                control_config_sha256=control_config_sha256,
            )
            return diagnostic_result

        try:
            try:
                diagnostic_provider = provider_spec_value.factory()
                diagnostic_attempt["factory_returned"] = True
            except BaseException as exc:
                diagnostic_attempt["failure_code"] = "provider_constructor_failed"
                terminalize_constructor_failure(
                    diagnostic_context,
                    requested_provider=config.provider,
                    error=exc,
                    custody=diagnostic_custody,
                )
            try:
                (_diagnostic_result, _diagnostic_path, _diagnostic_digest, diagnostic_variant) = run_provider_lifecycle(
                    provider=diagnostic_provider, profile=lme_profile(), context=diagnostic_context,
                    binding=provider_spec_value.runtime_binding, requested_provider=config.provider,
                    operation=run_diagnostics,
                    finalized=lambda evidence: record_lifecycle(evidence, get_diagnostic_trace),
                    custody=diagnostic_custody,
                    setup_completed=lambda: diagnostic_attempt.__setitem__("setup_completed", True),
                    variant_observed=lambda value: diagnostic_attempt.__setitem__("provider_variant", value),
                )
            except BaseException as primary:
                if diagnostic_trace is not None:
                    diagnostic_trace.close()
                preserve_failure_through_retirement(primary, diagnostic_custody, diagnostic_attempt)
            if diagnostic_trace is not None:
                diagnostic_trace.close()
            retirement_facts = retire_direct_context(diagnostic_custody)
            if retirement_facts:
                raise CleanupUnproved(
                    "runner-owned lifecycle retirement failed",
                    fact="runner_retirement_failed",
                )
            assert diagnostic_variant == provider_variant
        except BaseException as exc:
            code = _closed_failure_code(exc, "provider_control_flow" if not isinstance(exc, Exception) else "provider_diagnostic_failed")
            if diagnostic_attempt["failure_code"] is None:
                diagnostic_attempt["failure_code"] = code
            invalid_reason = f"diagnostic provider failure: {code}"
            failures.append({"question_id": diagnostic_logical_id, "phase": "diagnostic", "detail": code})
            if not isinstance(exc, Exception):
                control_flow = exc
    started = time.perf_counter()
    for case_ordinal, question in enumerate(
        () if control_flow is not None or invalid_reason is not None else dataset.questions,
        1,
    ):
        question_started = time.perf_counter()
        adapter = adapter_factory() if not config.provider else None
        reader_attempted = False
        reader_wall_time = 0.0
        reader_started = question_started
        provider_answered = False
        trace: CaseTraceWriter | None = None
        internal_case_session_id = (
            _internal_session_id(case_ordinal, question.question_id)
            if config.provider
            else question.question_id
        )
        case_attempt: dict[str, object] | None = None

        def get_case_trace() -> CaseTraceWriter:
            nonlocal trace
            if trace is None:
                trace = CaseTraceWriter(
                    run_dir,
                    internal_case_session_id,
                    schema_version=2 if config.provider else 1,
                )
            return trace

        try:
            if config.provider:
                assert provider_spec_value is not None
                context, case_custody, case_attempt = prepare_direct_context(
                    internal_session_id=internal_case_session_id,
                    logical_question_id=question.question_id,
                    namespace=provider_spec_value.derive_namespace(run_id, question.question_id),
                )
                try:
                    provider = provider_spec_value.factory()
                    case_attempt["factory_returned"] = True
                except BaseException as exc:
                    case_attempt["failure_code"] = "provider_constructor_failed"
                    terminalize_constructor_failure(
                        context,
                        requested_provider=config.provider,
                        error=exc,
                        custody=case_custody,
                    )

                def _owned(candidate: object):
                    nonlocal prior_presence_token, reader_attempted, reader_started, reader_wall_time
                    events = neutralize(question, dataset_identity)
                    token = canary_for(run_id, question.question_id, "presence")
                    handle = CaseHandle(
                        case_id=question.question_id,
                        case_ordinal=case_ordinal,
                        question_date=question.question_date_text,
                    )
                    canary_event = _harness_canary(events, handle, token)
                    events = [*events, canary_event]
                    content_fields, authored_literals, harness_fields = ingest_field_groups(events, handle)
                    authored_literals = {**authored_literals, "harness_canary": canary_event.content}
                    findings = scan_ingest(
                        content_fields,
                        authored_literals,
                        harness_fields,
                        CaseGold(
                            case_id=question.question_id,
                            answer=question.answer,
                            answer_session_ids=list(question.answer_session_ids),
                            question_type=question.question_type,
                            question=question.question,
                        ),
                        raw_upstream_session_ids=[session.session_id for session in question.sessions],
                    )
                    leakage_findings.extend(findings)
                    blocking_findings = [
                        item for item in findings if item.detector != "question-text"
                    ]
                    if blocking_findings:
                        raise RuntimeError(
                            "leakage scan rejected outbound provider payload: "
                            + "; ".join(item.detector for item in blocking_findings)
                        )
                    inserted = candidate.ingest_case(events, handle)
                    bind_observed_variant(context, candidate)
                    payload_shas: list[str] = []
                    canary_ordinals = {event.session_ordinal for event in events if event.provenance.converter == "harness-canary"}
                    for session_ordinal in sorted({event.session_ordinal for event in events}):
                        payload = render_neutral_session([event for event in events if event.session_ordinal == session_ordinal])
                        payload_sha = hashlib.sha256(payload.encode()).hexdigest()
                        if session_ordinal not in canary_ordinals:
                            payload_shas.append(payload_sha)
                        get_case_trace().append({"record": "ingest", "session_ordinal": session_ordinal, "payload_sha256": payload_sha, "provider_ids": list(inserted or [])})
                    per_case_readiness = _readiness_with_probe_evidence(
                        candidate.readiness(), probe_results
                    )
                    bind_observed_variant(context, candidate)
                    hits = candidate.retrieve(question.question, config.top_k, RetrievalPurpose.SCORED_RETRIEVAL)
                    bind_observed_variant(context, candidate)
                    retrieved = [hit.text for hit in hits]
                    get_case_trace().append({"record": "search", "query": question.question, "raw_response_ref": "inline:provider-hit-list", "normalized_hit_ids": [hit.hit_id for hit in hits], "normalized_hit_shas": [hashlib.sha256(hit.text.encode()).hexdigest() for hit in hits], "top_k": config.top_k})
                    presence_hits = candidate.retrieve(token, 1, RetrievalPurpose.POSITIVE_PROBE)
                    bind_observed_variant(context, candidate)
                    presence = _hit_contains(presence_hits, token)
                    if prior_presence_token is None:
                        isolation_rows.append({"case_ordinal": case_ordinal, "prior_case": "not-applicable-no-prior-case"})
                        prior_hit = False
                    else:
                        prior_hits = candidate.retrieve(prior_presence_token, 1, RetrievalPurpose.ABSENCE_PROBE_EXPECTED_EMPTY)
                        bind_observed_variant(context, candidate)
                        prior_hit = _hit_contains(prior_hits, prior_presence_token)
                        isolation_rows.append({"case_ordinal": case_ordinal, "prior_case_token": prior_presence_token, "hit": prior_hit})
                    never_token = canary_for(run_id, question.question_id, "never_ingested")
                    never_hits = candidate.retrieve(never_token, 1, RetrievalPurpose.ABSENCE_PROBE_EXPECTED_EMPTY)
                    bind_observed_variant(context, candidate)
                    never_hit = _hit_contains(never_hits, never_token)
                    contamination_by_case[question.question_id] = _canary_verdict({"presence": presence, "cross_case": prior_hit, "never_ingested": never_hit}, retains_nothing=bool(getattr(candidate, "retains_nothing", False)))
                    reader_attempted = True
                    reader_started = time.perf_counter()
                    hypothesis = active_reader.answer(question, retrieved)
                    reader_wall_time = time.perf_counter() - reader_started
                    get_case_trace().append({"record": "timing", "phase": "retrieve-and-read", "ms": (time.perf_counter() - question_started) * 1000.0})
                    get_case_trace().append({"record": "answer", "prompt_sha256": hashlib.sha256(question.question.encode()).hexdigest(), "model_id": _reader_model(active_reader, config), "response_ref": "inline:stub-response" if isinstance(active_reader, StubReader) else "reader-artifact", "input_tokens": int(getattr(getattr(active_reader, "last_call_metrics", None), "input_tokens", 0) or 0), "output_tokens": int(getattr(getattr(active_reader, "last_call_metrics", None), "output_tokens", 0) or 0)})
                    prior_presence_token = token
                    return payload_shas, per_case_readiness, hits, retrieved, hypothesis

                try:
                    (payload_shas, per_case_readiness, hits, retrieved, hypothesis), _observation_path, _observation_digest, observed_variant = run_provider_lifecycle(
                        provider=provider,
                        profile=lme_profile(),
                        context=context,
                        binding=provider_spec_value.runtime_binding,
                        requested_provider=config.provider,
                        operation=_owned,
                        finalized=lambda evidence: record_lifecycle(evidence, get_case_trace),
                        custody=case_custody,
                        setup_completed=lambda: case_attempt.__setitem__("setup_completed", True),
                        variant_observed=lambda value: case_attempt.__setitem__("provider_variant", value),
                    )
                except BaseException as primary:
                    if trace is not None:
                        trace.close()
                    assert case_attempt is not None
                    preserve_failure_through_retirement(primary, case_custody, case_attempt)
                if trace is not None:
                    trace.close()
                retirement_facts = retire_direct_context(case_custody)
                if retirement_facts:
                    raise CleanupUnproved(
                        "runner-owned lifecycle retirement failed",
                        fact="runner_retirement_failed",
                    )
                if provider_variant is None:
                    provider_variant = observed_variant
                    config_value = getattr(provider, "config", None)
                    config_hash = getattr(config_value, "sha256", None)
                    control_config_sha256 = config_hash() if callable(config_hash) else None
                    bind_started_manifest_provider(run_dir, provider_variant=provider_variant, control_config_sha256=control_config_sha256)
                readiness_list.extend(per_case_readiness)
                equivalence_cases.append(_equivalence_case(
                    question=question, case_ids=case_ids, namespace_pattern=namespace_pattern(provider_kind),
                    payload_shas=payload_shas, readiness=per_case_readiness,
                    retrieved_ids=[hit.hit_id for hit in hits], retrieved_text=retrieved, top_k=config.top_k,
                    dataset_identity=dataset_identity, reader_name=config.reader_name,
                    reader_model=_reader_model(active_reader, config),
                ))
                status = "ok"
                provider_answered = True
            else:
                assert adapter is not None
                trace = get_case_trace()
                retrieved = adapter.run_question(
                    question,
                    run_dir / "questions" / _safe_id(question.question_id),
                    dataset_identity=dataset_identity,
                    case_ordinal=case_ordinal,
                    limit=config.top_k,
                )
                payload_shas = [file_sha256(run_dir / "dataset.json")]
                trace.append({"record": "ingest", "session_ordinal": 1, "payload_sha256": payload_shas[0], "provider_ids": []})
                trace.append({"record": "search", "query": question.question, "raw_response_ref": "inline:adapter-hit-list", "normalized_hit_ids": [], "normalized_hit_shas": [hashlib.sha256(text.encode()).hexdigest() for text in retrieved], "top_k": config.top_k})
                equivalence_cases.append(_equivalence_case(
                    question=question, case_ids=case_ids, namespace_pattern=namespace_pattern(provider_kind),
                    payload_shas=payload_shas, readiness=[], retrieved_ids=[], retrieved_text=retrieved,
                    top_k=config.top_k, dataset_identity=dataset_identity, reader_name=config.reader_name,
                    reader_model=_reader_model(active_reader, config),
                ))
            if not provider_answered:
                reader_attempted = True
                reader_started = time.perf_counter()
                hypothesis = active_reader.answer(question, retrieved)
                reader_wall_time = time.perf_counter() - reader_started
                status = "ok"
                trace.append({"record": "timing", "phase": "retrieve-and-read", "ms": (time.perf_counter() - question_started) * 1000.0})
                trace.append({"record": "answer", "prompt_sha256": hashlib.sha256(question.question.encode()).hexdigest(), "model_id": _reader_model(active_reader, config), "response_ref": "inline:stub-response" if isinstance(active_reader, StubReader) else "reader-artifact", "input_tokens": int(getattr(getattr(active_reader, "last_call_metrics", None), "input_tokens", 0) or 0), "output_tokens": int(getattr(getattr(active_reader, "last_call_metrics", None), "output_tokens", 0) or 0)})
        except AdapterEnvironmentError as exc:
            if config.provider:
                code = _closed_failure_code(exc, "provider_environment_failed")
                if case_attempt is not None and case_attempt["failure_code"] is None:
                    case_attempt["failure_code"] = code
                invalid_reason = f"{question.question_id}: provider failure: {code}"
                failures.append(
                    {"question_id": question.question_id, "phase": "environment", "detail": code}
                )
            else:
                invalid_reason = f"{question.question_id}: {exc}"
                failures.append(
                    {"question_id": question.question_id, "phase": "environment", "detail": str(exc)}
                )
            break
        except Exception as exc:  # retained as a question failure, never dropped
            # Deliberately asymmetric. On the direct-provider path the provider
            # CONTRACT broke, so no case from that provider is trustworthy and
            # the run is voided. On the legacy adapter path a question that
            # fails is a scored failure inside the denominator — that is the
            # established LME policy, and voiding the run instead would silently
            # change what the benchmark measures.
            if config.provider:
                code = _closed_failure_code(exc, "provider_lifecycle_failed")
                if case_attempt is not None and case_attempt["failure_code"] is None:
                    case_attempt["failure_code"] = code
                invalid_reason = f"{question.question_id}: provider failure: {code}"
            if reader_attempted:
                reader_wall_time = time.perf_counter() - reader_started
            status = "failed"
            hypothesis = ABSTENTION
            failures.append(
                {
                    "question_id": question.question_id,
                    "phase": "retrieve-or-read",
                    "detail": code if config.provider else f"{type(exc).__name__}: {exc}",
                }
            )
            if config.provider:
                break
        except BaseException as exc:
            control_flow = exc
            if case_attempt is not None and case_attempt["failure_code"] is None:
                case_attempt["failure_code"] = "provider_control_flow"
            invalid_reason = f"{question.question_id}: provider control-flow"
            failures.append(
                {
                    "question_id": question.question_id,
                    "phase": "retrieve-or-read",
                    "detail": "provider_control_flow",
                }
            )
            break
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

    close_run_custody()
    if control_flow is not None:
        # Preserve the provider's exact control-flow object.  Local evidence
        # writes are best-effort once it exists; none may replace it.
        try:
            bounds_dir = run_dir / "bounds"
            bounds_dir.mkdir()
            _write_jsonl(bounds_dir / "gold-evidence-ceiling.jsonl", [])
            _write_jsonl(bounds_dir / "null-abstain-floor.jsonl", [])
            _write_jsonl(run_dir / "failures.jsonl", failures)
            _write_jsonl(run_dir / "question-outcomes.jsonl", outcomes)
        except BaseException:
            pass
        if config.provider:
            try:
                _write_jsonl(run_dir / "isolation.jsonl", isolation_rows)
                environment["lme"]["lifecycle_expected_instances"] = lifecycle_instances
                _write_json(run_dir / "environment.json", environment)
            except BaseException:
                pass
        try:
            finalize_manifest(
                run_dir,
                status="INVALID",
                finalized_at=dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
                readiness=readiness_list,
                leakage=LeakageSummary(scanned_cases=len(outcomes), invalidated_cases=len(failures)),
                contamination="unverifiable",
                budget=_budget_summary(ledger),
                invalid_reason=invalid_reason,
            )
        except BaseException:
            pass
        raise control_flow

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

    # The terminal verdict is decided HERE, before any artifact that encodes
    # validity — pilot evidence, run.json, report.md.  Deciding it after those
    # writes shipped a poisoned run with `invalid: false` in run.json and a
    # report.md carrying no banner, which judge_io then faithfully re-rendered.
    readiness_report = validate_readiness(readiness_list, strict=False) if readiness_list else None
    readiness_status = readiness_report.status if readiness_report is not None else "VALID"
    verdicts = list(contamination_by_case.values())
    if config.provider:
        contamination = (
            "contaminated" if "contaminated" in verdicts
            else "unverifiable" if "unverifiable" in verdicts
            else "isolated" if verdicts
            else "unverifiable"
        )
        if len(dataset.questions) < 2 and contamination == "isolated":
            contamination = "unverifiable"
        # This path plants and probes canaries, so a probe that could not
        # confirm isolation is a measured fault, not a missing measurement.
        contamination_invalid = contamination in {"contaminated", "unverifiable"}
    else:
        # The legacy adapter path plants no canaries at all.  Recording
        # "isolated" here would be a constructible zero: an isolation verdict
        # with no evidence behind it.  "unverifiable" is the honest state — the
        # run stands on its own, and `validate --strict` refuses it for any
        # comparative table, which is exactly what unverifiable is for.
        contamination = "unverifiable"
        contamination_invalid = False
    detector_counts: dict[str, int] = {}
    for finding in leakage_findings:
        detector = getattr(finding, "detector", "unknown")
        detector_counts[detector] = detector_counts.get(detector, 0) + 1
    if contamination_invalid and invalid_reason is None:
        invalid_reason = f"canary contamination status: {contamination}"
    if readiness_report is not None and readiness_report.status == "INVALID" and invalid_reason is None:
        invalid_reason = "; ".join(readiness_report.reasons)
    if any(probe.probe_kind == "semantic-zero-overlap" and probe.outcome == "fail" for probe in probe_results) and invalid_reason is None:
        invalid_reason = "semantic known-answer readiness probe failed"
    terminal_status = "INVALID" if invalid_reason is not None else readiness_status
    # A real per-case count: every case whose canary verdict was contaminated,
    # plus every case that failed in the harness, never the truthiness of one
    # run-level flag.
    invalidated_case_ids = {case_id for case_id, verdict in contamination_by_case.items() if verdict == "contaminated"}
    invalidated_case_ids |= {
        str(failure["question_id"]) for failure in failures
        if failure.get("phase") in {"environment", "retrieve-or-read"}
    }

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

    if config.provider:
        _write_jsonl(run_dir / "isolation.jsonl", isolation_rows)
        environment["lme"]["lifecycle_expected_instances"] = lifecycle_instances
        _write_json(run_dir / "environment.json", environment)

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
        "canonical_selection": config.canonical_selection,
        "selection": selection_pins or None,
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
    _write_json(
        run_dir / "equivalence.json",
        {"schema": "equivalence-input.v1", "run_id": run_id, "provider_variant": provider_variant, "cases": equivalence_cases},
    )
    (run_dir / "report.md").write_text(
        render_report(
            dataset,
            labels={},
            ceiling_question_ids=ceiling_ids,
            floor_question_ids=floor_ids,
            invalid_reason=manifest_banner(terminal_status, contamination, invalid_reason),
            provider_variant=provider_variant,
        ),
        encoding="utf-8",
    )
    finalize_manifest(
        run_dir,
        status=terminal_status,
        finalized_at=dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        readiness=readiness_list,
        leakage=LeakageSummary(scanned_cases=len(outcomes), invalidated_cases=len(invalidated_case_ids), detectors_fired=detector_counts),
        contamination=contamination,
        budget=_budget_summary(ledger),
        invalid_reason=invalid_reason,
    )
    if control_flow is not None:
        raise control_flow
    if invalid_reason is not None:
        raise LmeRunInvalid(invalid_reason, run_dir)
    return RunResult(
        run_dir=run_dir,
        question_count=len(dataset.questions),
        failure_count=len(failures),
    )
