"""Run pipeline: render-native → ingest → retrieve → answer → score → judge → report.

Run directories are immutable (creation fails on collision, never
overwrites). Per-query failures land in ``failures.jsonl`` and stay in every
denominator; an :class:`AdapterEnvironmentError` marks the whole run INVALID
— an environment fault is never a contender loss.

Two further faults invalidate rather than score, both for that same reason:

- **Blocking environment mismatch.** A run may declare a
  ``reference_environment`` — the run (or ``environment.json``) it claims to
  reproduce. A blocking difference (see :mod:`membench.environment`) is
  detected BEFORE the first query, so nothing is measured, ``run_failures``
  stays at zero, and no dimension is scored in either direction. A run that
  declares no reference is never invalidated this way; it records its
  environment and the report says it is unverified.
- **Retrieval floor.** A contender that returns zero hits on *every* query
  produced no measurement, only the shape of one. Scoring it publishes a
  sheet of zeros that reads as a catastrophic product result and is far more
  likely a broken harness — as it was on 2026-08-05, where a zero-hit run
  additionally collected 16 vacuous governance PASSES for retrieving nothing.
  Exactly-zero is the only line drawn: a contender that retrieves one wrong
  document anywhere is terrible and gets scored as terrible.

The judge phase is the LAST thing that happens, and that ordering is load
bearing rather than incidental:

- ``deterministic-scores.json`` is already written, and the adapter already
  cleaned up, before a judge is asked anything. The deterministic record is
  therefore byte-identical whether or not a judge was configured — not by
  discipline, but because there is no longer anything to write to.
- Judged verdicts land in a SEPARATE file (``judged-scores.json``) and a
  separate dimension. Nothing merges them; a reader subtracts the judged
  contribution by ignoring one file.
- The default backend is ``none`` (:data:`~membench.judge.backends.
  DEFAULT_BACKEND_NAME`). A run with no judge configured completes normally
  and leaves those rows UNSUPPORTED — never guessing, never erroring.
- Any judge failure — backend absent or skipped, malformed JSON, refusal,
  timeout, a blinding-leak refusal — is recorded in ``failures.jsonl``, leaves
  the affected rows UNSUPPORTED with the cause named, and does NOT enter
  ``run_failures`` or mark the run INVALID. A judge fault is not a contender
  loss any more than an environment fault is.
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
    INGESTION_ALTITUDES,
    AdapterEnvironmentError,
    Capability,
    Hit,
    MemoryAdapter,
    Profile,
)
from membench.environment import (
    EnvironmentComparison,
    capture_environment,
    compare_environments,
    load_environment,
)
from membench.judge.backends import JudgeBackend, default_backend
from membench.judge.blinding import BlindingMap
from membench.judge.handshake import append_failure, collect_responses
from membench.native import FactParityReport, load_corpus_view
from membench.native import basic_memory as basic_memory_native
from membench.native import exomem_kb as exomem_native
from membench.native import graybox as graybox_native
from membench.native import oracle_ceiling as oracle_ceiling_native
from membench.reporting import (
    JUDGE_SCORES_NAME,
    JUDGED_SCORES_NAME,
    merge_judge_scores,
)
from membench.schema import ClaimRecord, ExpectedRecord, QueryRecord, load_jsonl
from membench.scoring import GateStatus, ScoringContext, evaluate, summarize_dimensions
from membench.scoring.answer_contract import AnswerRecord
from membench.scoring.extractive import build_answer
from membench.scoring.judged import (
    JUDGE_RESOLVABLE_GATES,
    JUDGE_UPPER_BOUND_CAVEAT,
    JUDGED_DIMENSION,
    JudgeCandidate,
    JudgedItem,
    candidate_for,
    prompt_fingerprint,
    request_items,
    resolve,
    summarize_judged,
    unresolved,
)
from membench.scoring.retrieval import score_retrieval

#: Who authored the answer that got scored. Answer-property dimensions
#: (provenance, abstention, calibration) measure whoever wrote the answer, so
#: this is a comparability key, not a cosmetic label: placing a natively
#: answered contender next to a harness-answered one on those rows is the
#: 4b.29 shape — a configuration difference read as a product difference.
ANSWER_MODE_NATIVE = "native"
ANSWER_MODE_HARNESS = "harness"

# Every contender needs its corpus rendered into ITS OWN native grammar before
# ingest. A provider absent from this map receives an EMPTY directory and is
# structurally guaranteed to retrieve nothing — which reads as a catastrophic
# contender result while measuring only our own omission. Registering exomem
# alone (the state until 2026-08-05) meant the harness could only ever produce
# zeros for competitors, i.e. it was rigged in our favour by accident. The
# retrieval floor caught it before publication; this map is the actual fix.
#
# A renderer here is not optional politeness: `basic_memory.render` and
# `graybox.render` already existed, tested for grammar and per-fact parity, and
# were simply never wired up.
_NATIVE_RENDERERS = {
    "exomem-local": exomem_native.render,
    "basic-memory-local": basic_memory_native.render,
    "graybox-local": graybox_native.render,
    # Identity: the ceiling reads the canonical corpus, so nothing is lost in
    # translation and its parity report says so explicitly.
    "oracle-retrieval": oracle_ceiling_native.render,
    # The floor is given the same corpus everyone else gets; withholding
    # retrieval is its declared behaviour, withholding *ingest* would make it
    # an empty-corpus artefact instead of a measurement.
    "null-abstain": oracle_ceiling_native.render,
}


@dataclass
class RunSpec:
    corpus_dir: Path
    adapter: MemoryAdapter
    profile: Profile
    runs_root: Path
    top_k: int = 10
    label: str | None = None
    run_id: str | None = None
    #: Opt-in by CONFIGURATION, not by flag: a judge needs a model id,
    #: credentials-by-env-var-name and a timeout, none of which an enum switch
    #: can carry, and half-configured is the state that produces fake numbers.
    #: ``None`` means :func:`~membench.judge.backends.default_backend` — the
    #: ``none`` backend, which does not run.
    judge_backend: JudgeBackend | None = None
    judge_samples: int = 1
    #: The environment this run claims to reproduce: a captured environment
    #: mapping, an ``environment.json`` path, or a run directory. Opt-in, and
    #: deliberately so — invalidation on a blocking difference is only honest
    #: where a reproduction was claimed. ``None`` records the environment and
    #: reports it as unverified.
    reference_environment: dict | Path | str | None = None


@dataclass
class RunResult:
    run_dir: Path
    invalid: bool
    invalid_reason: str | None
    dimensions: dict[str, dict[str, int]] = field(default_factory=dict)
    #: Judged tallies, kept in their own attribute for the same reason they
    #: are kept in their own file: a caller cannot fold them into
    #: ``dimensions`` without meaning to.
    judged: dict[str, object] = field(default_factory=dict)


#: Below this many attempted retrieval queries the floor guard does not fire.
#: A narrow probe legitimately returning nothing is not evidence of a broken
#: harness; a whole suite returning nothing is.
RETRIEVAL_FLOOR_MIN_QUERIES = 10
#: Positive-but-tiny hit coverage is WARNED about, never invalidated. Drawing
#: an invalidating line here would require asserting a lower bound on a real
#: system's competence, which this benchmark has no standing to assert: a
#: contender is allowed to be dreadful, and dreadful must remain measurable.
RETRIEVAL_FLOOR_WARN_FRACTION = 0.05

FLOOR_OK = "ok"
FLOOR_NEAR_ZERO = "near_zero"
FLOOR_VIOLATION = "floor_violation"
FLOOR_NOT_APPLICABLE = "not_applicable"
#: A reference floor contender that declared up front it retrieves nothing.
#: Recorded distinctly so no reader can confuse a declared zero with an
#: observed one — the whole point of the guard is that those look identical in
#: the artifacts, and only intent separates them.
FLOOR_DECLARED_NULL = "declared_null"
#: The declaration was false: an adapter promising zero retrieval returned
#: hits. That invalidates too. A declaration that is not held to is a way to
#: switch the guard off, which is precisely what must not be purchasable.
FLOOR_DECLARATION_BROKEN = "declaration_broken"


@dataclass(frozen=True)
class RetrievalFloor:
    """Whether a run retrieved enough to have measured anything at all."""

    queries: int
    queries_with_hits: int
    total_hits: int
    status: str
    detail: str

    @property
    def invalid(self) -> bool:
        return self.status in (FLOOR_VIOLATION, FLOOR_DECLARATION_BROKEN)

    def as_dict(self) -> dict[str, object]:
        return {
            "queries": self.queries,
            "queries_with_hits": self.queries_with_hits,
            "total_hits": self.total_hits,
            "status": self.status,
            "detail": self.detail,
            "min_queries": RETRIEVAL_FLOOR_MIN_QUERIES,
            "warn_fraction": RETRIEVAL_FLOOR_WARN_FRACTION,
        }


def evaluate_retrieval_floor(
    queries: int,
    queries_with_hits: int,
    total_hits: int,
    *,
    declares_null_retrieval: bool = False,
) -> RetrievalFloor:
    """Classify a run's retrieval volume: measured, barely measured, or not.

    ``declares_null_retrieval`` is the reference-floor seam. A contender whose
    *purpose* is to retrieve nothing (``null-abstain``) produces artifacts
    byte-indistinguishable from a broken harness, so intent is the only thing
    that can separate them — and intent has to be declared before the run, by
    the adapter class itself, never by a flag a real run could pass. The
    declaration is held both ways: declaring it and then returning hits is
    ``FLOOR_DECLARATION_BROKEN`` and invalidates, because a declaration nobody
    checks is just a switch for turning the guard off.


    Exactly zero hits across a whole suite is not a degree of badness, it is
    the absence of a signal: nothing in the artifacts distinguishes "this
    system has no answer for any of N diverse queries, including bare
    entity-name lookups" from "the harness or the environment is broken".
    Both observed instances of it were the latter. A positive but tiny hit
    rate IS a signal — it is scored, and flagged for a human to check the
    environment before publishing.
    """

    if declares_null_retrieval:
        if queries_with_hits or total_hits:
            return RetrievalFloor(
                queries=queries,
                queries_with_hits=queries_with_hits,
                total_hits=total_hits,
                status=FLOOR_DECLARATION_BROKEN,
                detail=(
                    f"declared null retrieval, returned {total_hits} hit(s) on "
                    f"{queries_with_hits}/{queries} queries. The declaration exempts a "
                    "reference floor from the zero-hit guard, so an adapter that "
                    "breaks it has switched the guard off while retrieving — INVALID"
                ),
            )
        return RetrievalFloor(
            queries=queries,
            queries_with_hits=0,
            total_hits=0,
            status=FLOOR_DECLARED_NULL,
            detail=(
                f"declared null retrieval: 0 hits on all {queries} queries, by design. "
                "This is the reference FLOOR — the score every real contender must be "
                "read against — and not an environment fault. It is never a "
                "contender result and never enters a product comparison"
            ),
        )
    if queries < RETRIEVAL_FLOOR_MIN_QUERIES:
        return RetrievalFloor(
            queries=queries,
            queries_with_hits=queries_with_hits,
            total_hits=total_hits,
            status=FLOOR_NOT_APPLICABLE,
            detail=(
                f"retrieval floor not applied: {queries} retrieval quer(y/ies) "
                f"attempted, below the {RETRIEVAL_FLOOR_MIN_QUERIES}-query minimum"
            ),
        )
    if queries_with_hits == 0:
        return RetrievalFloor(
            queries=queries,
            queries_with_hits=0,
            total_hits=total_hits,
            status=FLOOR_VIOLATION,
            detail=(
                f"retrieval floor: 0 hits on all {queries} retrieval queries. A "
                "contender that retrieves nothing anywhere has produced no "
                "measurement to score — this is a harness or environment fault, "
                "and a harness fault is INVALID, never a contender loss"
            ),
        )
    fraction = queries_with_hits / queries
    if fraction < RETRIEVAL_FLOOR_WARN_FRACTION:
        return RetrievalFloor(
            queries=queries,
            queries_with_hits=queries_with_hits,
            total_hits=total_hits,
            status=FLOOR_NEAR_ZERO,
            detail=(
                f"near-zero retrieval: {queries_with_hits}/{queries} queries "
                f"({fraction:.1%}) returned any hit. SCORED — a real contender is "
                "allowed to be this bad — but verify the environment before "
                "publishing these numbers"
            ),
        )
    return RetrievalFloor(
        queries=queries,
        queries_with_hits=queries_with_hits,
        total_hits=total_hits,
        status=FLOOR_OK,
        detail=(
            f"{queries_with_hits}/{queries} queries returned at least one hit "
            f"({total_hits} hits total)"
        ),
    )


def retrieval_floor_from_run_dir(run_dir: Path | str) -> RetrievalFloor:
    """Apply the floor to an already-completed run's ``retrieval.jsonl``.

    Lets an archived run be judged by the same rule as a live one, which is
    the point: the runs that motivated the guard are on disk already.
    """

    path = Path(run_dir)
    if path.is_dir():
        path = path / "retrieval.jsonl"
    queries = 0
    queries_with_hits = 0
    total_hits = 0
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            hits = json.loads(raw).get("hits")
            hit_count = len(hits) if isinstance(hits, list) else 0
            queries += 1
            total_hits += hit_count
            if hit_count:
                queries_with_hits += 1
    return evaluate_retrieval_floor(queries, queries_with_hits, total_hits)


class _EnvironmentMismatch(Exception):
    """Blocking environment difference: same verdict as an environment fault.

    Private, and deliberately NOT an :class:`AdapterEnvironmentError`: that
    path counts one entry in ``run_failures``, and this one measured nothing
    at all, so counting a failure against the contender would be exactly the
    misattribution the invariant forbids.
    """


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


def _ingestion_altitude(adapter: MemoryAdapter) -> str:
    """The layer this adapter loaded the corpus at, contract-checked.

    Absence means ``raw_source``: a bulk load that compiles nothing is a
    raw-source load, and defaulting the other way would let a run claim depth it
    never had. Unlike governance there is no capability to cross-check against,
    because altitude is a property of how the harness drove the product rather
    than of what the product can do.
    """

    altitude = str(getattr(adapter, "ingestion_altitude", "raw_source"))
    if altitude not in INGESTION_ALTITUDES:
        raise ValueError(
            f"adapter {adapter.name!r} declares unknown ingestion_altitude "
            f"{altitude!r}; expected one of {sorted(INGESTION_ALTITUDES)}"
        )
    return altitude


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


def _judge_phase(
    run_dir: Path,
    spec: RunSpec,
    run_id: str,
    candidates: list[JudgeCandidate],
    *,
    skip_reason: str | None,
) -> tuple[dict[str, object], list[JudgedItem]]:
    """Ask a judge about the rows a deterministic gate could not decide.

    Returns ``(meta, judged_items)``. Every failure path returns rather than
    raises: the deterministic run is already complete and written by the time
    this is called, and a judge fault must never invalidate it. Rows that
    receive no usable verdict come back UNSUPPORTED with the cause named.
    """

    backend = spec.judge_backend or default_backend()
    backend_name = str(getattr(backend, "name", type(backend).__name__))
    meta: dict[str, object] = {
        "backend": backend_name,
        "prompt_id": prompt_fingerprint(),
        "dimension": JUDGED_DIMENSION,
        "scope_gates": sorted(JUDGE_RESOLVABLE_GATES),
        "candidates": len(candidates),
        "samples": spec.judge_samples,
        "caveat": JUDGE_UPPER_BOUND_CAVEAT,
        "note": (
            "judged verdicts resolve ONLY gates that reported UNSUPPORTED; a "
            "pass/fail/not_applicable gate is final and is never revisited"
        ),
    }
    if skip_reason is not None:
        meta["status"] = "skipped"
        meta["detail"] = skip_reason
        return meta, []
    if not candidates:
        meta["status"] = "no_candidates"
        meta["detail"] = (
            "no judge-resolvable gate reported UNSUPPORTED; the judge has "
            "nothing non-redundant to add to this run"
        )
        return meta, []

    try:
        token = BlindingMap.mint([spec.adapter.name], f"{run_id}:judge").token_for(
            spec.adapter.name
        )
        outcome = backend.run_phase(
            run_dir,
            "judge",
            request_items(candidates, provider_token=token),
            samples=spec.judge_samples,
            seed=f"{run_id}:judge",
        )
    except Exception as exc:  # noqa: BLE001 - a judge fault is never a contender loss
        # Deliberately total. A backend can fail in ways this harness does not
        # enumerate — a refusal, a timeout, an HTTP client raising something
        # new, a LeakageError refusing to serialize a request. Every one of
        # them must leave the rows UNSUPPORTED with the cause named rather
        # than propagate into a valid, already-written deterministic run.
        detail = f"{type(exc).__name__}: {exc}"
        append_failure(run_dir, {"phase": "judge", "detail": detail})
        meta["status"] = "error"
        meta["detail"] = detail
        return meta, unresolved(candidates, cause=detail)

    meta["phase_status"] = outcome.status
    meta["phase_note"] = outcome.note
    if outcome.status == "not_run":
        # DEFAULT_BACKEND_NAME is "none". The run completes normally and the
        # rows stay UNSUPPORTED in the deterministic record — no judged file,
        # no guess, no error.
        meta["status"] = "not_run"
        meta["detail"] = outcome.note
        return meta, []
    if outcome.status == "prepared":
        # Requests were written for an external executor; no responses exist
        # yet. Collecting now would record one "missing response" failure per
        # request for something that has not gone wrong.
        meta["status"] = "prepared"
        meta["detail"] = outcome.note
        return meta, unresolved(
            candidates, cause=f"judge requests prepared, awaiting executor ({outcome.note})"
        )

    try:
        paired, stats = collect_responses(run_dir, "judge")
        merge_judge_scores(run_dir, paired)
        merged = json.loads((run_dir / JUDGE_SCORES_NAME).read_text(encoding="utf-8"))
        rows = {
            str(row.get("query_id")): row
            for row in merged.get("per_query", [])
            if isinstance(row, dict)
        }
    except Exception as exc:  # noqa: BLE001 - malformed judge output is data, not a crash
        detail = f"{type(exc).__name__}: {exc}"
        append_failure(run_dir, {"phase": "judge-collect", "detail": detail})
        meta["status"] = "error"
        meta["detail"] = detail
        return meta, unresolved(candidates, cause=detail)

    meta["status"] = outcome.status
    meta["handshake"] = stats
    return meta, resolve(candidates, rows, backend=backend_name)


def execute_run(spec: RunSpec) -> RunResult:
    corpus_dir = Path(spec.corpus_dir)
    governance_state = _governance_state(spec.adapter)
    ingestion_altitude = _ingestion_altitude(spec.adapter)
    governed = governance_state == "wired"
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_id = spec.run_id or (
        f"{stamp}-{spec.adapter.name}-{spec.label or spec.profile.name}-{uuid.uuid4().hex[:6]}"
    )
    run_dir = Path(spec.runs_root) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)  # collision = abort, never overwrite
    (run_dir / "traces").mkdir()

    # Captured ONCE, before anything is measured, and reused as both the
    # written artifact and the thing that gets verified: a run must not
    # publish an environment record it did not itself check.
    environment = capture_environment()
    comparison: EnvironmentComparison | None = None
    environment_mismatch: str | None = None
    if spec.reference_environment is not None:
        comparison = compare_environments(
            load_environment(spec.reference_environment), environment
        )
        if comparison.blocked:
            environment_mismatch = f"environment: {comparison.summary()}"

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
        "environment_verification": (
            comparison.as_dict()
            if comparison is not None
            else {
                "status": "unverified",
                "summary": (
                    "no reference environment supplied: this run's environment is "
                    "recorded but was not compared to anything, so it cannot be "
                    "claimed to reproduce another run"
                ),
                "blocking": [],
                "reported": [],
            }
        ),
        "retrieval_floor": evaluate_retrieval_floor(0, 0, 0).as_dict(),
        # Present from the start so an early-invalidated run still records who
        # would have authored its answers; overwritten below once capabilities
        # are read. A missing key would read as "unknown mode" and silently
        # dodge the mixed-mode comparability check.
        "ingestion_altitude": ingestion_altitude,
        "answer_mode": (
            ANSWER_MODE_NATIVE
            if Capability.NATIVE_ANSWER in spec.adapter.capabilities()
            else ANSWER_MODE_HARNESS
        ),
    }
    corpus_manifest = (corpus_dir / "manifest.json").read_text(encoding="utf-8")
    (run_dir / "corpus-manifest.json").write_text(corpus_manifest, encoding="utf-8")

    failures_handle, write_failure = _jsonl_writer(run_dir / "failures.jsonl")
    invalid_reason: str | None = None
    per_query_items: list[list] = []
    judge_candidates: list[JudgeCandidate] = []
    run_failures = 0
    floor_invalid_reason: str | None = None

    try:
        if environment_mismatch is not None:
            # Before the corpus is even loaded: a run that cannot be compared
            # to what it claims to reproduce must not spend an hour producing
            # numbers nobody may use.
            raise _EnvironmentMismatch(environment_mismatch)
        view = load_corpus_view(corpus_dir)
        native_answer_mode = Capability.NATIVE_ANSWER in spec.adapter.capabilities()
        manifest["answer_mode"] = (
            ANSWER_MODE_NATIVE if native_answer_mode else ANSWER_MODE_HARNESS
        )
        renderer = _NATIVE_RENDERERS.get(spec.adapter.name)
        native_dir = run_dir / "native" / spec.adapter.name
        parity: FactParityReport | None = None
        if renderer is not None:
            parity = renderer(view, native_dir, altitude=ingestion_altitude)
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
                # Citation precision resolves same-entity references by name, so
                # it needs the entity records: without them the gate cannot tell
                # a reference-resolving claim from an attribute claim and
                # reports UNSUPPORTED rather than guessing in either direction.
                entities_by_id={e.entity_id: e for e in view.entities},
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
            retrieval_queries = 0
            queries_with_hits = 0
            total_hits = 0
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
                    retrieval_queries += 1
                    total_hits += len(hits)
                    if hits:
                        queries_with_hits += 1
                    write_retrieval(
                        {
                            "query_id": query.query_id,
                            "latency_ms": latency_ms,
                            "hits": [_hit_public(hit) for hit in hits],
                        }
                    )
                    if native_answer_mode:
                        # The contender answers for itself. Its citations are a
                        # closed claim and its abstention is its own judgment;
                        # the harness contributes neither.
                        # Persona threading is as load-bearing here as on the
                        # search path: an answer resolved against the default
                        # principal reads the ungoverned vault while the run
                        # claims a persona, which is a governance leak rather
                        # than a scoring quirk.
                        if governed:
                            native = spec.adapter.answer(
                                query.prompt_text, spec.top_k, persona=query.persona
                            )
                        else:
                            native = spec.adapter.answer(query.prompt_text, spec.top_k)
                        answer = AnswerRecord(
                            query_id=query.query_id,
                            answer_text=native.text,
                            citations=list(native.citations),
                            abstained=native.abstained,
                            hedged=native.hedged,
                            clarification_question=native.clarification_question,
                            latency_ms=latency_ms,
                            citations_are_native=True,
                            raw=dict(native.raw) or None,
                        )
                    else:
                        answer = build_answer(query, hits, latency_ms=latency_ms)
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
                    # Judge candidates are minted from the FINAL deterministic
                    # items, so a row the gates decided can never become one.
                    candidate = candidate_for(query, exp, answer, items)
                    if candidate is not None:
                        judge_candidates.append(candidate)
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

            floor = evaluate_retrieval_floor(
                retrieval_queries,
                queries_with_hits,
                total_hits,
                # Class attribute, never a flag: only a purpose-built reference
                # adapter can claim the exemption, and it is recorded in the
                # manifest so a declared zero can never read as an observed one.
                declares_null_retrieval=bool(
                    getattr(spec.adapter, "retrieves_nothing_by_design", False)
                ),
            )
            manifest["retrieval_floor"] = floor.as_dict()
            if floor.invalid:
                floor_invalid_reason = floor.detail

            dimensions = summarize_dimensions(per_query_items, run_failures)
            # Written even for a floor-invalidated run — the tallies are the
            # evidence FOR the invalidation — but labelled in the file itself,
            # and withheld from the report and from RunResult.dimensions so
            # nothing can lift them out as a contender's result.
            (run_dir / "deterministic-scores.json").write_text(
                json.dumps(
                    {
                        "dimensions": dimensions,
                        "governance_state": governance_state,
                        "invalid": floor_invalid_reason is not None,
                        "invalid_reason": floor_invalid_reason,
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
    except _EnvironmentMismatch as exc:
        # Recorded in failures.jsonl for visibility, but NOT in run_failures:
        # nothing was measured, so there is nothing to count against the
        # contender. INVALID is a statement about the run, not about it.
        invalid_reason = str(exc)
        write_failure({"phase": "environment-verification", "detail": invalid_reason})
    except AdapterEnvironmentError as exc:
        invalid_reason = f"environment: {exc}"
        run_failures += 1
        write_failure({"phase": "run", "detail": invalid_reason})
    finally:
        failures_handle.close()

    if invalid_reason is None and floor_invalid_reason is not None:
        # The floor verdict lands before the judge phase below, so a judge is
        # never asked to grade a run that measured nothing.
        invalid_reason = floor_invalid_reason

    # The judge runs only now: deterministic-scores.json is on disk, the
    # adapter is cleaned up, and the failures handle is closed (so the judge
    # phase appends through append_failure rather than racing that writer).
    # Judge failures are visible in failures.jsonl but deliberately stay OUT
    # of run_failures — they are not contender failures and must not enter a
    # deterministic denominator.
    judge_meta, judged_items = _judge_phase(
        run_dir,
        spec,
        run_id,
        judge_candidates,
        skip_reason=(
            None if invalid_reason is None else f"run is INVALID ({invalid_reason})"
        ),
    )
    judged_payload: dict[str, object] = {}
    if judged_items:
        judged_payload = {
            "meta": judge_meta,
            "summary": summarize_judged(judged_items),
            "per_query": [item.as_dict() for item in judged_items],
        }
        (run_dir / JUDGED_SCORES_NAME).write_text(
            json.dumps(judged_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    manifest["judge"] = judge_meta

    manifest["invalid"] = invalid_reason is not None
    manifest["invalid_reason"] = invalid_reason
    manifest["run_failures"] = run_failures
    manifest["ended_utc"] = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    (run_dir / "environment.json").write_text(
        json.dumps(environment, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    dimensions_out: dict[str, dict[str, int]] = {}
    scores_path = run_dir / "deterministic-scores.json"
    if scores_path.is_file() and invalid_reason is None:
        # An INVALID run returns NO dimensions. The file keeps them as
        # evidence; the caller gets nothing it could publish as a result.
        dimensions_out = json.loads(scores_path.read_text(encoding="utf-8"))["dimensions"]
    _write_report(run_dir, manifest, dimensions_out, judged_payload, environment=environment)
    return RunResult(
        run_dir=run_dir,
        invalid=manifest["invalid"],  # type: ignore[arg-type]
        invalid_reason=invalid_reason,
        dimensions=dimensions_out,
        judged=judged_payload,
    )


def _judged_cell(judged: dict, dimension: str) -> str:
    """How much of ``dimension``'s row came from a judge rather than a gate.

    Rendered as its OWN column, never folded into the four counts beside it:
    a reader must be able to subtract the judged contribution from any
    published figure without rerunning the benchmark.
    """

    summary = judged.get("summary") if isinstance(judged, dict) else None
    by_base = summary.get("by_base_dimension", {}) if isinstance(summary, dict) else {}
    counts = by_base.get(dimension)
    if not isinstance(counts, dict):
        return "—"
    parts = [
        f"{key}={counts.get(key, 0)}"
        for key in ("pass", "fail", "unsupported")
        if counts.get(key, 0)
    ]
    return " · ".join(parts) if parts else "—"


def _environment_report_section(manifest: dict, environment: dict | None) -> list[str]:
    """Environment status, readable without opening a single JSON file.

    This section exists because ``environment.json`` recorded the interpreter
    version from the first run onward and no reader ever looked: a fact in an
    artifact that nothing surfaces is a fact nobody has.
    """

    environment = environment or {}
    verification = manifest.get("environment_verification") or {}
    repos = environment.get("repos") if isinstance(environment.get("repos"), dict) else {}
    product = repos.get("exomem") if isinstance(repos.get("exomem"), dict) else {}
    head = str(product.get("head") or "n/a")
    distributions = environment.get("distributions")
    closure = environment.get("runtime_closure")
    knobs = environment.get("env_knobs") if isinstance(environment.get("env_knobs"), dict) else {}
    lines = [
        "",
        "## Environment (blocking vs reported)",
        "",
        f"- interpreter: {environment.get('python_version', '?')} "
        f"{environment.get('python_implementation', '')}".rstrip()
        + f" · platform: {environment.get('platform', '?')}"
        f" · machine: {environment.get('machine', '?')}",
        f"- product: exomem {environment.get('exomem_version', '?')} @ {head[:12]}"
        + (" **(DIRTY TREE — head does not identify the source)**"
           if product.get("dirty") else ""),
        f"- knobs: {', '.join(f'{k}={v}' for k, v in sorted(knobs.items())) or 'none'}",
        f"- distributions recorded: "
        f"{len(distributions) if isinstance(distributions, dict) else 'NOT RECORDED'}"
        f" · product runtime closure: "
        f"{len(closure) if isinstance(closure, list) else 'NOT RECORDED'}"
        " (a difference inside the closure is blocking; outside it, reported)",
        f"- verification: **{verification.get('status', 'unverified')}** — "
        f"{verification.get('summary', 'n/a')}",
        "",
    ]
    blocking = verification.get("blocking") or []
    reported = verification.get("reported") or []
    if blocking:
        lines.extend(
            [
                "### Blocking differences (these invalidate the comparison)",
                "",
                "| field | reference | observed | why |",
                "| --- | --- | --- | --- |",
            ]
        )
        for entry in blocking:
            lines.append(
                f"| {entry.get('field')} | {entry.get('reference')} "
                f"| {entry.get('observed')} | {entry.get('detail')} |"
            )
        lines.append("")
    if reported:
        unverifiable = [e for e in reported if e.get("unverifiable")]
        lines.append(
            f"Reported differences: {len(reported)} "
            f"({len(unverifiable)} unverifiable — one side did not record the "
            "field, which is NOT the same as agreement)."
        )
        lines.extend(["", "| field | reference | observed | why |", "| --- | --- | --- | --- |"])
        for entry in reported:
            lines.append(
                f"| {entry.get('field')} | {entry.get('reference')} "
                f"| {entry.get('observed')} | {entry.get('detail')} |"
            )
        lines.append("")
    return lines


def _retrieval_floor_section(manifest: dict) -> list[str]:
    floor = manifest.get("retrieval_floor") or {}
    status = str(floor.get("status", "?"))
    emphasis = "**" if status in (FLOOR_VIOLATION, FLOOR_NEAR_ZERO) else ""
    return [
        "",
        "## Retrieval floor",
        "",
        f"- status: {emphasis}{status}{emphasis} · "
        f"{floor.get('queries_with_hits', 0)}/{floor.get('queries', 0)} queries "
        f"returned a hit · {floor.get('total_hits', 0)} hits total",
        f"- {floor.get('detail', 'n/a')}",
    ]


def _write_report(
    run_dir: Path,
    manifest: dict,
    dimensions: dict,
    judged: dict | None = None,
    *,
    environment: dict | None = None,
) -> None:
    judged = judged or {}
    judge_meta = manifest.get("judge") or {}
    verification = manifest.get("environment_verification") or {}
    floor = manifest.get("retrieval_floor") or {}
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
        f"- environment: {verification.get('status', 'unverified')} · "
        f"retrieval floor: {floor.get('status', '?')}",
        f"- judge: {judge_meta.get('backend', 'none')} "
        f"({judge_meta.get('status', 'not_run')})",
    ]
    if manifest["invalid"]:
        # No table. A sheet of plausible-looking counts is precisely what an
        # invalidated run must not publish — the 2026-08-05 zero-hit run
        # rendered 744 fails AND 16 governance passes for retrieving nothing.
        lines.extend(
            [
                "",
                "## Dimensions — WITHHELD (this run is INVALID)",
                "",
                f"Reason: {manifest['invalid_reason']}",
                "",
                "No dimension counts are published for an invalidated run. It is"
                " not a contender result in either direction — not a win, and"
                " above all not a loss. Any tallies in"
                " `deterministic-scores.json` are evidence for the invalidation,"
                " carry `\"invalid\": true`, and are not results.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "## Dimensions (no aggregate; unsupported is never zero)",
                "",
                "The first four count columns are DETERMINISTIC ONLY. The `judged`"
                " column is a separate lane reported beside them and is never added"
                " into them: subtract it by ignoring the column, or delete"
                f" `{JUDGED_SCORES_NAME}` and nothing else changes.",
                "",
                "| dimension | pass | fail | not_applicable | unsupported | judged (separate) |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for dim, counts in sorted(dimensions.items()):
            if dim.startswith("_"):
                continue
            lines.append(
                f"| {dim} | {counts.get('pass', 0)} | {counts.get('fail', 0)} "
                f"| {counts.get('not_applicable', 0)} | {counts.get('unsupported', 0)} "
                f"| {_judged_cell(judged, dim)} |"
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
    lines.extend(_retrieval_floor_section(manifest))
    lines.extend(_environment_report_section(manifest, environment))
    lines.extend(_judged_section(judge_meta, judged))
    (run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _judged_section(judge_meta: dict, judged: dict) -> list[str]:
    """The judged lane, reported apart from every deterministic number."""

    lines = [
        "",
        f"## Judged lane — `{JUDGED_DIMENSION}` (NOT part of the counts above)",
        "",
        f"- backend: {judge_meta.get('backend', 'none')} · "
        f"status: {judge_meta.get('status', 'not_run')}",
        f"- prompt: {judge_meta.get('prompt_id', 'n/a')} · "
        f"samples per row: {judge_meta.get('samples', 0)}",
        f"- scope: only these gates, and only where they reported UNSUPPORTED: "
        f"{', '.join(judge_meta.get('scope_gates', [])) or 'n/a'}",
        f"- candidate rows: {judge_meta.get('candidates', 0)}",
        "",
        "A judged verdict resolves ONLY a gate that reported UNSUPPORTED. A"
        " pass, fail or not_applicable gate is final and is never revisited,"
        " so no number above can move because a judge ran.",
        "",
    ]
    detail = judge_meta.get("detail")
    if detail:
        lines.extend([f"Judge phase detail: {detail}", ""])
    caveat = judge_meta.get("caveat")
    if caveat:
        lines.extend([f"**{caveat}**", ""])

    rows = judged.get("per_query") if isinstance(judged, dict) else None
    if not rows:
        lines.append("No judged verdicts in this run.")
        return lines
    summary = judged.get("summary", {})
    lines.extend(
        [
            "| base dimension | judged pass | judged fail | judged unsupported |",
            "| --- | --- | --- | --- |",
        ]
    )
    for base, counts in sorted(summary.get("by_base_dimension", {}).items()):
        lines.append(
            f"| {base} | {counts.get('pass', 0)} | {counts.get('fail', 0)} "
            f"| {counts.get('unsupported', 0)} |"
        )
    lines.extend(
        [
            "",
            "| query | gate | judged | model(s) | evidence |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for row in rows:
        provenance = row.get("provenance") or {}
        models = ", ".join(provenance.get("model_ids", [])) or "n/a"
        evidence = str(row.get("evidence", "")).replace("|", "\\|")
        lines.append(
            f"| {row.get('query_id')} | {row.get('gate')} | {row.get('status')} "
            f"| {models} | {evidence} |"
        )
    return lines
