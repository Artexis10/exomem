"""Run pipeline: render-native → ingest → retrieve → answer → score → report.

Run directories are immutable (creation fails on collision, never
overwrites). Per-query failures land in ``failures.jsonl`` and stay in every
denominator; an :class:`AdapterEnvironmentError` marks the whole run INVALID
— an environment fault is never a contender loss.
"""

from __future__ import annotations

import dataclasses
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from membench.adapters.base import (
    GOVERNANCE_STATES,
    AdapterEnvironmentError,
    Capability,
    Hit,
    MemoryAdapter,
    Profile,
)
from membench.environment import capture_environment
from membench.native import FactParityReport, load_corpus_view
from membench.native import exomem_kb as exomem_native
from membench.schema import ClaimRecord, ExpectedRecord, QueryRecord, load_jsonl
from membench.scoring import GateStatus, ScoringContext, evaluate, summarize_dimensions
from membench.scoring.answer_contract import AnswerRecord
from membench.scoring.extractive import build_answer
from membench.scoring.retrieval import score_retrieval

_NATIVE_RENDERERS = {"exomem-local": exomem_native.render}


@dataclass
class RunSpec:
    corpus_dir: Path
    adapter: MemoryAdapter
    profile: Profile
    runs_root: Path
    top_k: int = 10
    label: str | None = None
    run_id: str | None = None


@dataclass
class RunResult:
    run_dir: Path
    invalid: bool
    invalid_reason: str | None
    dimensions: dict[str, dict[str, int]] = field(default_factory=dict)


def _jsonl_writer(path: Path):
    handle = path.open("w", encoding="utf-8", newline="\n")

    def write(record: dict) -> None:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()

    return handle, write


def _hit_public(hit: Hit) -> dict:
    payload = dataclasses.asdict(hit)
    payload.pop("text", None)  # bulk text stays out of run artifacts
    payload.pop("raw", None)
    return payload


def _dropped_rule_impact(
    translation: dict | None,
    claims: list[ClaimRecord],
    expected: dict[str, ExpectedRecord],
) -> dict[str, list[str]]:
    """query_id → dropped corpus rule ids its withhold expectation traces to.

    A wired translation that DROPPED a rule (e.g. a ``declassify_at`` exomem's
    time-free policy schema cannot express) left the vault open where the
    corpus expected a withhold. Queries whose ``forbidden_claims`` /
    ``forbidden_disclosures`` derive from a dropped rule's targets — directly
    or through the targeted sources' asserted claims — cannot be measured
    against wired governance: their governance gates become UNSUPPORTED,
    never pass, never fail.
    """

    if not isinstance(translation, dict):
        return {}
    dropped = translation.get("dropped_rules")
    if not isinstance(dropped, list) or not dropped:
        return {}
    impact: dict[str, list[str]] = {}
    for entry in dropped:
        if not isinstance(entry, dict):
            continue
        rule_id = str(entry.get("rule_id") or "")
        target_claims = set(entry.get("target_claims") or [])
        target_sources = set(entry.get("target_sources") or [])
        covered_claims = set(target_claims)
        covered_values: set[str] = set()
        for claim in claims:
            if claim.claim_id in target_claims or any(
                assertion.source_id in target_sources
                for assertion in claim.assertions
            ):
                covered_claims.add(claim.claim_id)
                covered_values.add(claim.object.value)
        for query_id, exp in expected.items():
            if set(exp.forbidden_claims) & covered_claims or (
                set(exp.forbidden_disclosures) & covered_values
            ):
                impact.setdefault(query_id, []).append(rule_id)
    return impact


def _governance_state(adapter: MemoryAdapter) -> str:
    """The adapter's three-state governance label, contract-checked.

    Absent means ``default_open`` (an explicitly ungoverned vault measured as
    the default-open surface). "wired" and the GOVERNED_VIEWS capability must
    agree in both directions: the capability is declared only when wiring is
    active, and an active wiring must declare it.
    """

    state = str(getattr(adapter, "governance_state", "default_open"))
    if state not in GOVERNANCE_STATES:
        raise ValueError(
            f"adapter {adapter.name!r} declares unknown governance_state {state!r}; "
            f"expected one of {sorted(GOVERNANCE_STATES)}"
        )
    governed = Capability.GOVERNED_VIEWS in adapter.capabilities()
    if governed != (state == "wired"):
        raise ValueError(
            f"adapter {adapter.name!r} is inconsistent: governance_state={state!r} "
            f"but GOVERNED_VIEWS {'declared' if governed else 'not declared'}"
        )
    return state


def execute_run(spec: RunSpec) -> RunResult:
    corpus_dir = Path(spec.corpus_dir)
    governance_state = _governance_state(spec.adapter)
    governed = governance_state == "wired"
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_id = spec.run_id or (
        f"{stamp}-{spec.adapter.name}-{spec.label or spec.profile.name}-{uuid.uuid4().hex[:6]}"
    )
    run_dir = Path(spec.runs_root) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)  # collision = abort, never overwrite
    (run_dir / "traces").mkdir()

    manifest: dict[str, object] = {
        "run_id": run_id,
        "provider": spec.adapter.name,
        "profile": {"name": spec.profile.name, "settings": spec.profile.settings},
        "top_k": spec.top_k,
        "corpus_dir": str(corpus_dir),
        "governance_state": governance_state,
        "started_utc": stamp,
        "invalid": False,
        "invalid_reason": None,
    }
    corpus_manifest = (corpus_dir / "manifest.json").read_text(encoding="utf-8")
    (run_dir / "corpus-manifest.json").write_text(corpus_manifest, encoding="utf-8")

    failures_handle, write_failure = _jsonl_writer(run_dir / "failures.jsonl")
    invalid_reason: str | None = None
    per_query_items: list[list] = []
    run_failures = 0

    try:
        view = load_corpus_view(corpus_dir)
        renderer = _NATIVE_RENDERERS.get(spec.adapter.name)
        native_dir = run_dir / "native" / spec.adapter.name
        parity: FactParityReport | None = None
        if renderer is not None:
            parity = renderer(view, native_dir)
            (run_dir / "parity.json").write_text(
                json.dumps(
                    {
                        "renderer": parity.renderer,
                        "entries": {
                            fid: {"status": e.status.value, "reason": e.reason}
                            for fid, e in sorted(parity.entries.items())
                        },
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

        spec.adapter.setup(run_dir / "provider", spec.profile)
        try:
            ingest_handle, write_ingest = _jsonl_writer(run_dir / "ingest.jsonl")
            try:
                for op_result in spec.adapter.ingest(corpus_dir, native_dir):
                    write_ingest(dataclasses.asdict(op_result))
                    if not op_result.ok:
                        run_failures += 1
                        write_failure(
                            {"phase": "ingest", "seq": op_result.seq, "detail": op_result.detail}
                        )
            finally:
                ingest_handle.close()

            queries = load_jsonl(QueryRecord, corpus_dir / "queries.jsonl")
            expected = {
                e.query_id: e
                for e in load_jsonl(ExpectedRecord, corpus_dir / "expected.jsonl")
            }
            ctx = ScoringContext(
                claims_by_id={c.claim_id: c for c in view.claims},
                sources_by_id={s.source_id: s for s in view.sources},
            )

            # Wired-translation report: written by the adapter into its own
            # workdir during ingest; surfaced verbatim at the run root, and
            # its dropped rules joined onto the affected queries' gates.
            dropped_impact: dict[str, list[str]] = {}
            if governed:
                translation_path = run_dir / "provider" / "governance-translation.json"
                if translation_path.is_file():
                    raw_translation = translation_path.read_text(encoding="utf-8")
                    (run_dir / "governance-translation.json").write_text(
                        raw_translation, encoding="utf-8"
                    )
                    dropped_impact = _dropped_rule_impact(
                        json.loads(raw_translation), view.claims, expected
                    )

            retrieval_handle, write_retrieval = _jsonl_writer(run_dir / "retrieval.jsonl")
            answers_handle, write_answer = _jsonl_writer(run_dir / "answers.jsonl")
            scores_per_query: list[dict] = []
            try:
                for query in queries:
                    exp = expected[query.query_id]
                    if "retrieval" not in query.modes and "qa" not in query.modes:
                        write_answer(
                            {"query_id": query.query_id, "status": "out_of_scope_mode"}
                        )
                        scores_per_query.append(
                            {
                                "query_id": query.query_id,
                                "family": query.family,
                                "status": "out_of_scope_mode",
                            }
                        )
                        continue
                    started = time.perf_counter()
                    try:
                        # Persona threading is part of the governed-views
                        # wiring: only adapters declaring GOVERNED_VIEWS
                        # receive it, so every existing two-argument adapter
                        # keeps working unchanged.
                        if governed:
                            hits = spec.adapter.search(
                                query.prompt_text, spec.top_k, persona=query.persona
                            )
                        else:
                            hits = spec.adapter.search(query.prompt_text, spec.top_k)
                    except AdapterEnvironmentError:
                        raise
                    except Exception as exc:
                        run_failures += 1
                        write_failure(
                            {
                                "phase": "retrieve",
                                "query_id": query.query_id,
                                "detail": f"{type(exc).__name__}: {exc}",
                            }
                        )
                        scores_per_query.append(
                            {
                                "query_id": query.query_id,
                                "family": query.family,
                                "status": "failed",
                            }
                        )
                        continue
                    latency_ms = (time.perf_counter() - started) * 1000.0
                    write_retrieval(
                        {
                            "query_id": query.query_id,
                            "latency_ms": latency_ms,
                            "hits": [_hit_public(hit) for hit in hits],
                        }
                    )
                    answer: AnswerRecord = build_answer(query, hits, latency_ms=latency_ms)
                    write_answer(json.loads(answer.model_dump_json()))
                    items = evaluate(query, exp, answer, ctx)
                    dropped_rules = dropped_impact.get(query.query_id)
                    if dropped_rules:
                        # Unsupported-never-zero: the wired translation could
                        # not represent the rule this expectation depends on,
                        # so its governance gates are unmeasurable — never a
                        # pass, never a contender fail.
                        evidence = (
                            "wired translation dropped corpus rule(s) "
                            f"{', '.join(sorted(dropped_rules))}: exomem policy v1 "
                            "has no time-conditioned rules"
                        )
                        items = [
                            dataclasses.replace(
                                item, status=GateStatus.UNSUPPORTED, evidence=evidence
                            )
                            if item.gate in ("no_leak", "abstention")
                            else item
                            for item in items
                        ]
                    per_query_items.append(items)
                    scores_per_query.append(
                        {
                            "query_id": query.query_id,
                            "family": query.family,
                            "status": "ok",
                            "gates": [
                                {
                                    "gate": item.gate,
                                    "dimension": item.dimension,
                                    "status": item.status.value,
                                    "evidence": item.evidence,
                                }
                                for item in items
                            ],
                            "retrieval": score_retrieval(query, exp, hits),
                        }
                    )
            finally:
                retrieval_handle.close()
                answers_handle.close()

            dimensions = summarize_dimensions(per_query_items, run_failures)
            (run_dir / "deterministic-scores.json").write_text(
                json.dumps(
                    {
                        "dimensions": dimensions,
                        "governance_state": governance_state,
                        "per_query": scores_per_query,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            manifest["provider_version"] = spec.adapter.version_info()
        finally:
            spec.adapter.cleanup()
    except AdapterEnvironmentError as exc:
        invalid_reason = f"environment: {exc}"
        run_failures += 1
        write_failure({"phase": "run", "detail": invalid_reason})
    finally:
        failures_handle.close()

    manifest["invalid"] = invalid_reason is not None
    manifest["invalid_reason"] = invalid_reason
    manifest["run_failures"] = run_failures
    manifest["ended_utc"] = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    (run_dir / "environment.json").write_text(
        json.dumps(capture_environment(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    dimensions_out: dict[str, dict[str, int]] = {}
    scores_path = run_dir / "deterministic-scores.json"
    if scores_path.is_file():
        dimensions_out = json.loads(scores_path.read_text(encoding="utf-8"))["dimensions"]
    _write_report(run_dir, manifest, dimensions_out)
    return RunResult(
        run_dir=run_dir,
        invalid=manifest["invalid"],  # type: ignore[arg-type]
        invalid_reason=invalid_reason,
        dimensions=dimensions_out,
    )


def _write_report(run_dir: Path, manifest: dict, dimensions: dict) -> None:
    lines = [
        f"# Run {manifest['run_id']}",
        "",
        f"- provider: {manifest['provider']} · profile: {manifest['profile']['name']}",
        f"- governance: {manifest.get('governance_state', 'default_open')}"
        " (wired | default_open | unsupported; only wired runs enter"
        " comparative governance tables)",
        f"- invalid: {manifest['invalid']}"
        + (f" ({manifest['invalid_reason']})" if manifest["invalid_reason"] else ""),
        f"- run failures (kept in denominators): {manifest.get('run_failures', 0)}",
        "",
        "## Dimensions (no aggregate; unsupported is never zero)",
        "",
        "| dimension | pass | fail | not_applicable | unsupported |",
        "| --- | --- | --- | --- | --- |",
    ]
    for dim, counts in sorted(dimensions.items()):
        if dim.startswith("_"):
            continue
        lines.append(
            f"| {dim} | {counts.get('pass', 0)} | {counts.get('fail', 0)} "
            f"| {counts.get('not_applicable', 0)} | {counts.get('unsupported', 0)} |"
        )
    run_meta = dimensions.get("_run", {})
    lines.extend(
        [
            "",
            f"Queries scored: {run_meta.get('queries_scored', 0)}; "
            f"failures in denominator: {run_meta.get('failures', 0)}.",
            "",
            "Latency is reported separately in retrieval.jsonl; see "
            "docs/memory-proof-benchmark.md for the publication contract.",
        ]
    )
    (run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
