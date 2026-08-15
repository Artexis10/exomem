"""Cross-run comparison reporting + judge score merging.

Publication contract enforced here:

- Deterministic dimension tables NEVER incorporate judge values. Judged
  verdicts arrive in their own file (``judged-scores.json``), are tallied
  under their own dimension, and are rendered in their own section — so the
  judged contribution to any published figure can be subtracted by ignoring
  one section, without rerunning anything.
- No weighted aggregate exists anywhere.
- Latency lives in its own section, never mixed into quality tables.
- An INVALID run renders ``INVALID`` in every metric column — never numbers.
- Judge output is advisory: when the judge says ``semantic_match=true`` but
  any deterministic gate failed, the query is annotated
  ``gate_conflict (deterministic verdict stands)``; the deterministic
  verdict is final.
- Governance dimensions are three-state: only runs measured against WIRED
  governance show comparable counts; a run against an explicitly ungoverned
  vault renders its default-open label (and ``unsupported`` likewise) and is
  excluded from the comparative governance row — a label, never a number.
- Environment is compared, not merely recorded. Runs whose environments differ
  in a BLOCKING field (interpreter, product identity, captured knobs, a
  distribution inside the product's runtime closure) are marked NOT COMPARABLE
  against the reference run, loudly, in the report a reader actually opens.
  A cross-run report does not invalidate anyone's run — comparability is a
  property of a pair — but it must never place incomparable numbers side by
  side in silence, which is what produced a published "regression" that was an
  interpreter upgrade.
- Retrieval floor is surfaced per run: a run that retrieved nothing anywhere
  measured nothing, and reads here as INVALID rather than as a column of zeros.
"""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from membench.environment import compare_environments
from membench.judge.backends import parse_judge_verdict
from membench.judge.handshake import PairedResponse, append_failure, load_requests

#: Raw merged judge samples (one row per judged query, per-sample verbatim).
JUDGE_SCORES_NAME = "judge-scores.json"
#: Judged VERDICTS derived from those samples, scoped to rows a deterministic
#: gate reported UNSUPPORTED. Kept out of ``deterministic-scores.json`` so the
#: deterministic record is byte-identical whether or not a judge ran.
JUDGED_SCORES_NAME = "judged-scores.json"
GATE_CONFLICT_NOTE = "gate_conflict (deterministic verdict stands)"
_STATUS_KEYS = ("pass", "fail", "not_applicable", "unsupported")

#: Dimensions whose comparative cells are meaningful only against wired
#: governance (spec: "Governed Views Are Wired, Not Simulated").
GOVERNANCE_DIMENSIONS = frozenset({"governance"})

#: Dimensions scored from a property of the ANSWER rather than of RETRIEVAL,
#: so they measure whoever authored the answer.
#:
#: This distinction is the root cause behind three separate defects. With the
#: harness authoring every answer, `provenance` scored the extractive
#: answerer's top-3 citation policy (4b.31), `abstention` scored whether
#: retrieval returned zero hits — exomem abstained 0 times in 240 answers while
#: 52 queries required it — and `contradiction_uncertainty` demanded hedged
#: language an answerer that only quotes stored text cannot generate (4b.33).
#: `factual_qa` and `temporal` are absent because they ask whether a required
#: value is present in what was retrieved, which no answerer decides.
#:
#: Comparing these rows across contenders whose answers had different authors
#: is the 4b.29 shape: a configuration difference read as a product difference.
ANSWER_MODE_DIMENSIONS = frozenset(
    {"provenance", "abstention", "contradiction_uncertainty"}
)

#: Only the non-default mode is surfaced in labels; a harness-answered run is
#: the baseline every historical run used and needs no annotation.
ANSWER_MODE_NATIVE_LABEL = "native"
ANSWER_MODE_HARNESS = "harness"
WITHHELD_LATENCY = "withheld: transport asymmetry (4b.40)"
WITHHELD_HARNESS_ABSTENTION = "withheld: shared-answerer authored (audit CF#6)"

#: Dimensions that are STRUCTURALLY unmeasurable at raw-source altitude: what
#: they score does not exist, rather than existing and scoring badly. A citation
#: chain is a compiled conclusion pointing at its sources; contradiction
#: detection runs over compiled conclusions that disagree. Neither is present in
#: a pile of independent raw documents, which is why both floor and ceiling sit
#: at 0 for contradiction and why provenance degenerates to "which documents did
#: you return" — the same shallow thing for every contender.
#:
#: `abstention` is deliberately absent. It is affected by altitude (a dense raw
#: dump always matches something) but declining when you should is measurable at
#: any altitude, so listing it here would overclaim.
ALTITUDE_DEPENDENT_DIMENSIONS = frozenset({"provenance", "contradiction_uncertainty"})


def _altitude_conflict(altitudes: Sequence[str]) -> bool:
    """True when the runs being compared did not all measure the same layer."""

    return len({a for a in altitudes if a}) > 1

#: The query FAMILY whose rows (all their gate items, not just the governance
#: dimension) are excluded from comparative tables for non-wired runs — a
#: default-open run's vacuous abstention/temporal passes on governance-family
#: queries must never sit next to a wired run's numbers.
GOVERNANCE_FAMILY = "governance"
_GOVERNANCE_STATE_LABELS = {
    "default_open": "default-open",
    "unsupported": "unsupported",
}


def _answer_mode_conflict(modes: Sequence[str]) -> bool:
    """True when the runs being compared did not all use the same answer author."""

    return len({mode for mode in modes if mode}) > 1



def merge_judge_scores(run_dir: Path, paired: Sequence[PairedResponse]) -> Path:
    """Merge paired judge responses into ``judge-scores.json``.

    Denominators come from the WRITTEN requests (every minted sample),
    so missing or malformed samples appear as error entries and count in
    ``samples_total`` — they are never guessed and never dropped. Per-sample
    values are preserved verbatim next to mean/stdev/majority.
    """

    run_dir = Path(run_dir)
    expected: dict[str, set[int]] = {}
    for request in load_requests(run_dir, "judge"):
        expected.setdefault(request.request_id, set()).add(request.sample_index)

    samples_by_key: dict[tuple[str, int], dict] = {}
    for pair in paired:
        query_id = pair.request.request_id
        sample_index = pair.request.sample_index
        expected.setdefault(query_id, set()).add(sample_index)
        try:
            verdict = parse_judge_verdict(pair.response.response)
        except ValueError as exc:
            append_failure(
                run_dir,
                {
                    "phase": "judge-verdict",
                    "request_id": query_id,
                    "sample_index": sample_index,
                    "detail": f"{exc} (kept in denominators)",
                },
            )
            samples_by_key[(query_id, sample_index)] = {
                "sample_index": sample_index,
                "model_id": pair.response.model_id,
                "error": str(exc),
            }
            continue
        samples_by_key[(query_id, sample_index)] = {
            "sample_index": sample_index,
            "model_id": pair.response.model_id,
            "semantic_match": verdict.semantic_match,
            "explanation_quality": verdict.explanation_quality,
            "reason": verdict.reason,
        }

    per_query: list[dict] = []
    for query_id in sorted(expected):
        samples = [
            samples_by_key.get(
                (query_id, sample_index),
                {"sample_index": sample_index, "error": "no_response"},
            )
            for sample_index in sorted(expected[query_id])
        ]
        valid = [sample for sample in samples if "error" not in sample]
        qualities = [sample["explanation_quality"] for sample in valid]
        matches = sum(1 for sample in valid if sample["semantic_match"])
        total = len(samples)
        per_query.append(
            {
                "query_id": query_id,
                "samples": samples,
                "samples_total": total,
                "samples_valid": len(valid),
                "semantic_matches": matches,
                # errored samples stay in the majority denominator: an
                # unparseable verdict can never count as a match.
                "majority": matches * 2 > total,
                "mean": (sum(qualities) / len(qualities)) if qualities else None,
                "stdev": statistics.stdev(qualities) if len(qualities) >= 2 else None,
            }
        )

    payload = {
        "per_query": per_query,
        "meta": {
            "kind": "judge",
            "queries": len(per_query),
            "samples_total": sum(row["samples_total"] for row in per_query),
            "samples_valid": sum(row["samples_valid"] for row in per_query),
            "note": (
                "advisory only; deterministic gates are final and are never "
                "overridden by judge output"
            ),
        },
    }
    out = run_dir / JUDGE_SCORES_NAME
    out.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return out


@dataclass
class _RunView:
    run_dir: Path
    run_id: str
    label: str
    invalid: bool
    invalid_reason: str | None
    run_failures: int
    dimensions: dict
    per_query: list[dict]
    latencies: list[float]
    judge: dict | None
    failure_lines: int
    governance_state: str = "default_open"
    #: Judged verdicts (``judged-scores.json``). Never merged into
    #: ``dimensions``; rendered only in the judged section.
    judged: dict | None = None
    #: ``environment.json`` as recorded by the run (None for runs that predate
    #: environment capture entirely).
    environment: dict | None = None
    #: The run's own retrieval-floor verdict from its manifest.
    retrieval_floor: dict | None = None
    #: Provider name on its own. The label interleaves provider and profile,
    #: and reference contenders must be identified by identity rather than by
    #: parsing a display string.
    provider: str = "?"
    #: The layer the run measured at; see INGESTION_ALTITUDES.
    ingestion_altitude: str = "raw_source"
    #: Per-op ``latency_ms`` values drained from ``ingest.jsonl`` (write
    #: latency), mirroring ``latencies`` (retrieval). Empty for runs recorded
    #: before ingest latency was captured, or with no ingest ops — graceful
    #: degrade, see ``_load_run`` and ``_ingest_latency_section``.
    ingest_latencies: list[float] = field(default_factory=list)
    #: The writer of answer-level dimensions such as abstention.
    answer_mode: str = ANSWER_MODE_HARNESS
    #: Non-reference providers remain useful fixtures but are not publishable.
    publication_label: str | None = None


def _load_run(run_dir: Path) -> _RunView:
    from membench.runner import load_membench_result_manifest

    run_dir = Path(run_dir)
    manifest = load_membench_result_manifest(run_dir).model_dump(mode="json")
    dimensions: dict = {}
    per_query: list[dict] = []
    scores_path = run_dir / "deterministic-scores.json"
    if scores_path.is_file():
        scores = json.loads(scores_path.read_text(encoding="utf-8"))
        dimensions = scores.get("dimensions", {})
        per_query = scores.get("per_query", [])
    latencies: list[float] = []
    retrieval_path = run_dir / "retrieval.jsonl"
    if retrieval_path.is_file():
        for raw in retrieval_path.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                row = json.loads(raw)
                if isinstance(row.get("latency_ms"), int | float):
                    latencies.append(float(row["latency_ms"]))
    # Write (ingest) latency, mirrored from the retrieval loop above. A run
    # dir with no ingest.jsonl (older runs, partial runs) simply leaves this
    # empty — no required field, no KeyError; see _ingest_latency_section.
    ingest_latencies: list[float] = []
    ingest_path = run_dir / "ingest.jsonl"
    if ingest_path.is_file():
        for raw in ingest_path.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                row = json.loads(raw)
                if isinstance(row.get("latency_ms"), int | float):
                    ingest_latencies.append(float(row["latency_ms"]))
    judge: dict | None = None
    judge_path = run_dir / JUDGE_SCORES_NAME
    if judge_path.is_file():
        judge = json.loads(judge_path.read_text(encoding="utf-8"))
    judged: dict | None = None
    judged_path = run_dir / JUDGED_SCORES_NAME
    if judged_path.is_file():
        judged = json.loads(judged_path.read_text(encoding="utf-8"))
    failures_path = run_dir / "failures.jsonl"
    failure_lines = 0
    if failures_path.is_file():
        failure_lines = sum(
            1 for raw in failures_path.read_text(encoding="utf-8").splitlines() if raw.strip()
        )
    environment: dict | None = None
    environment_path = run_dir / "environment.json"
    if environment_path.is_file():
        loaded = json.loads(environment_path.read_text(encoding="utf-8"))
        environment = loaded if isinstance(loaded, dict) else None
    floor = manifest.get("retrieval_floor")
    profile = manifest.get("profile", {})
    profile_name = profile.get("name", "?") if isinstance(profile, dict) else str(profile)
    provider = str(manifest.get("provider", "?"))
    publication_label = manifest.get("publication_label")
    if publication_label is not None:
        publication_label = str(publication_label)
    return _RunView(
        run_dir=run_dir,
        run_id=str(manifest.get("run_id", run_dir.name)),
        # Answer mode rides in the label because it is a comparability key,
        # not a detail: two columns of the same provider and profile that
        # differ only in who authored the answer are not comparable on
        # provenance/abstention/calibration, and a reader must be able to see
        # that without opening a manifest.
        label=(
            f"{manifest.get('provider', '?')} · {profile_name}"
            + (
                f" · {manifest['answer_mode']}-answer"
                if manifest.get("answer_mode") == ANSWER_MODE_NATIVE_LABEL
                else ""
            )
        ),
        invalid=bool(manifest.get("invalid", False)),
        invalid_reason=manifest.get("invalid_reason"),
        run_failures=int(manifest.get("run_failures", 0) or 0),
        dimensions=dimensions,
        per_query=per_query,
        latencies=latencies,
        ingest_latencies=ingest_latencies,
        judge=judge,
        failure_lines=failure_lines,
        # Runs recorded before the three-state contract carry no field; they
        # were by construction measured against the default-open surface.
        governance_state=str(manifest.get("governance_state", "default_open")),
        judged=judged,
        environment=environment,
        retrieval_floor=floor if isinstance(floor, dict) else None,
        provider=provider,
        ingestion_altitude=str(manifest.get("ingestion_altitude", "raw_source")),
        answer_mode=str(manifest.get("answer_mode", ANSWER_MODE_HARNESS)),
        publication_label=publication_label,
    )


def _dedupe_labels(runs: list[_RunView]) -> None:
    seen: dict[str, int] = {}
    for run in runs:
        seen[run.label] = seen.get(run.label, 0) + 1
    counted: dict[str, int] = {}
    for run in runs:
        if seen[run.label] > 1:
            counted[run.label] = counted.get(run.label, 0) + 1
            run.label = f"{run.label} [{run.run_id}]"


def _p95(values: Sequence[float]) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(0.95 * len(ordered)))
    return ordered[rank - 1]


def _fmt(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _governance_exclusion_label(run: _RunView) -> str:
    label = _GOVERNANCE_STATE_LABELS.get(run.governance_state, run.governance_state)
    return f"{label} (excluded from comparison)"


def _comparable_rows(run: _RunView) -> list[dict]:
    """Per-query rows admissible in comparative tables for this run.

    Wired runs compare everything; non-wired runs drop governance-FAMILY rows
    entirely (all their gate items — the abstention-escape guard). Rows from
    runs recorded before family tagging carry no ``family`` key and are kept.
    """

    if run.governance_state == "wired":
        return run.per_query
    return [row for row in run.per_query if row.get("family") != GOVERNANCE_FAMILY]


def _has_governance_family_rows(run: _RunView) -> bool:
    return any(row.get("family") == GOVERNANCE_FAMILY for row in run.per_query)


def _format_counts(counts: dict) -> str:
    return " · ".join(
        f"{key.replace('not_applicable', 'n/a')}={counts.get(key, 0)}" for key in _STATUS_KEYS
    )


def _recomputed_dimension_counts(rows: list[dict], dimension: str) -> dict | None:
    """Per-dimension tallies over the given rows' gate items; None if absent."""

    counts = {key: 0 for key in _STATUS_KEYS}
    found = False
    for row in rows:
        for gate in row.get("gates") or []:
            if gate.get("dimension") == dimension and gate.get("status") in counts:
                counts[gate["status"]] += 1
                found = True
    return counts if found else None


def _dimension_cell(
    run: _RunView, dimension: str, *, cross_contender: bool = False
) -> str:
    if run.invalid:
        return "INVALID"
    if (
        cross_contender
        and dimension == "abstention"
        and run.answer_mode == ANSWER_MODE_HARNESS
    ):
        # The other answer-mode dimensions assess supplied answer content, but
        # this correction is deliberately scoped to abstention: only a
        # contender's own decision to abstain is a product-owned seam.
        return WITHHELD_HARNESS_ABSTENTION
    if run.governance_state != "wired":
        if dimension in GOVERNANCE_DIMENSIONS:
            # Three-state contract: an ungoverned (or unsupported) measurement
            # is labelled and EXCLUDED from the comparative governance row —
            # its counts never sit next to a wired run's counts.
            return _governance_exclusion_label(run)
        if _has_governance_family_rows(run):
            counts = _recomputed_dimension_counts(_comparable_rows(run), dimension)
            if counts is not None:
                return _format_counts(counts)
            if isinstance(run.dimensions.get(dimension), dict):
                # The dimension existed only through excluded rows.
                return _governance_exclusion_label(run)
            return "—"
    counts = run.dimensions.get(dimension)
    if not isinstance(counts, dict):
        return "—"
    return _format_counts(counts)


def _retrieval_stats(rows: Sequence[dict]) -> dict[str, float | int] | None:
    blocks = [
        row["retrieval"]
        for row in rows
        if isinstance(row.get("retrieval"), dict)
    ]
    if not blocks:
        return None

    def mean_of(key: str) -> float:
        return sum(float(block.get(key, 0.0)) for block in blocks) / len(blocks)

    return {
        "applicable_queries": len(blocks),
        "mean_recall_at_5": mean_of("recall_at_5"),
        "mean_recall_at_10": mean_of("recall_at_10"),
        "mean_mrr": mean_of("mrr"),
    }


def _ingest_latency_section(runs: Sequence[_RunView]) -> list[str]:
    """Ingest (write) latency rows — a sibling of the retrieval latency rows.

    Every adapter populates ``OpResult.latency_ms`` on ingest and the runner
    already drains it to ``ingest.jsonl``; this renders it the same way
    retrieval latency is rendered (median/p95/op count), so a write-latency
    regression is visible here instead of reaching production unmeasured.

    Graceful degrade: if NO run in the comparison has any ingest latency data
    (older runs, partial runs), no rows are returned at all — the report
    renders exactly as it did before ingest latency was captured. A run that
    lacks data while at least one sibling run has it still gets its own
    column, rendered ``n/a`` (the retrieval-latency idiom), rather than
    silently dropping that run out of the row.

    Altitude caveat: each contender's per-op unit of work differs (one
    exomem tool call is not one basic-memory write), so a raw ms figure is
    comparable within a contender but not directly across contenders. The
    run's own ``ingestion_altitude`` — already threaded into the manifest by
    the runner — is attached alongside the numbers as that caveat, rather
    than left in a JSON field nobody reading the report would open.
    """

    if not any(run.ingest_latencies for run in runs):
        return []
    labels = [run.label for run in runs]
    lines = [
        "",
        "`ingest_*` rows: write latency, a sibling of the retrieval latency",
        "rows above. Each contender's per-op unit of work differs (one",
        "exomem tool call is not one basic-memory write), so these ms figures",
        "are comparable within a contender across runs, never directly across",
        "contenders, without accounting for that difference. `ingestion_altitude`",
        "per run, the harness's own signal for it: "
        + " · ".join(f"{run.label}={run.ingestion_altitude}" for run in runs)
        + ".",
        "",
        "| metric | " + " | ".join(labels) + " |",
        "| --- |" + " --- |" * len(runs),
    ]
    for metric_name in ("ingest_median_ms", "ingest_p95_ms", "ingest_ops"):
        cells = []
        for run in runs:
            if run.invalid:
                cells.append("INVALID")
            elif not run.ingest_latencies:
                cells.append("n/a")
            elif metric_name == "ingest_median_ms":
                cells.append(_fmt(statistics.median(run.ingest_latencies)))
            elif metric_name == "ingest_p95_ms":
                cells.append(_fmt(_p95(run.ingest_latencies)))
            else:
                cells.append(str(len(run.ingest_latencies)))
        lines.append(f"| {metric_name} | " + " | ".join(cells) + " |")
    return lines


def _sample_cell(samples: list[dict]) -> str:
    parts: list[str] = []
    for sample in samples:
        index = sample.get("sample_index", "?")
        if "error" in sample:
            parts.append(f"s{index} error={sample['error']}")
        else:
            parts.append(
                f"s{index} match={sample.get('semantic_match')} "
                f"q={sample.get('explanation_quality')}"
            )
    return "; ".join(parts)


def _gate_conflicts(run: _RunView) -> list[str]:
    """Queries where the judge said match but a deterministic gate failed.

    The deterministic verdict stands — conflicts are annotations only.
    """

    if run.judge is None or run.invalid:
        return []
    failed_gates: dict[str, list[str]] = {}
    for row in run.per_query:
        gates = row.get("gates")
        if isinstance(gates, list):
            failed = [
                gate.get("gate", "?") for gate in gates if gate.get("status") == "fail"
            ]
            if failed:
                failed_gates[row.get("query_id", "?")] = failed
    conflicts: list[str] = []
    for row in run.judge.get("per_query", []):
        query_id = row.get("query_id", "?")
        if row.get("majority") and query_id in failed_gates:
            conflicts.append(
                f"- {run.label} — {query_id}: {GATE_CONFLICT_NOTE}; "
                f"failed deterministic gates: {', '.join(failed_gates[query_id])}"
            )
    return conflicts


def _judged_lane_section(runs: Sequence[_RunView]) -> list[str]:
    """Judged verdicts, per run, reported apart from every deterministic count.

    This section exists so a published figure stays auditable: it states how
    much of each dimension's result came from a judge rather than a gate, and
    under which model and prompt, so a reader can subtract it. Folding these
    numbers into the dimension table would make the published counts
    unauditable, which is the objection this benchmark exists to survive.
    """

    lines = [
        "",
        "## Judged lane (separate — never added to the dimension table)",
        "",
        "A judged verdict resolves ONLY a row where a deterministic gate",
        "reported UNSUPPORTED. A gate that returned pass, fail or",
        "not_applicable is final and is never revisited, so no count in the",
        "dimension table can move because a judge ran.",
        "",
    ]
    judged_runs = [run for run in runs if run.judged]
    if not judged_runs:
        lines.append("Judged lane: not run (no judged-scores.json in any run).")
        return lines
    caveats: dict[str, None] = {}
    for run in judged_runs:
        meta = (run.judged or {}).get("meta", {})
        summary = (run.judged or {}).get("summary", {})
        lines.append(f"### {run.label}")
        lines.append("")
        if run.invalid:
            lines.extend(["INVALID run — judged output is not reported as results.", ""])
            continue
        lines.extend(
            [
                f"- backend: {meta.get('backend', '?')} · "
                f"status: {meta.get('status', '?')} · "
                f"prompt: {meta.get('prompt_id', 'n/a')}",
                f"- scope: {', '.join(meta.get('scope_gates', [])) or 'n/a'} "
                f"(UNSUPPORTED rows only) · candidates: {meta.get('candidates', 0)}",
                "",
                "| base dimension | judged pass | judged fail | judged unsupported |",
                "| --- | --- | --- | --- |",
            ]
        )
        for base, counts in sorted(summary.get("by_base_dimension", {}).items()):
            lines.append(
                f"| {base} | {counts.get('pass', 0)} | {counts.get('fail', 0)} "
                f"| {counts.get('unsupported', 0)} |"
            )
        lines.append("")
        caveat = meta.get("caveat")
        if isinstance(caveat, str) and caveat:
            caveats.setdefault(caveat)
    for caveat in caveats:
        lines.extend([f"**{caveat}**", ""])
    return lines


def _environment_cells(run: _RunView) -> tuple[str, str, str]:
    """(interpreter, product, distributions) as rendered in the environment table."""

    environment = run.environment or {}
    if not environment:
        return ("not recorded", "not recorded", "not recorded")
    python = str(
        environment.get("python_version")
        or str(environment.get("python", "?")).split(" ")[0]
    )
    implementation = str(environment.get("python_implementation") or "")
    repos = environment.get("repos") if isinstance(environment.get("repos"), dict) else {}
    product = repos.get("exomem") if isinstance(repos.get("exomem"), dict) else {}
    head = str(product.get("head") or "n/a")[:12]
    dirty = " **DIRTY**" if product.get("dirty") else ""
    distributions = environment.get("distributions")
    closure = environment.get("runtime_closure")
    dist_cell = (
        f"{len(distributions)} recorded / "
        f"{len(closure) if isinstance(closure, list) else '?'} in closure"
        if isinstance(distributions, dict)
        else "**not recorded**"
    )
    return (
        f"{python} {implementation}".strip(),
        f"{environment.get('exomem_version', '?')} @ {head}{dirty}",
        dist_cell,
    )


def _environment_section(runs: Sequence[_RunView]) -> list[str]:
    """Comparability of the runs' environments, against the first run.

    The information was always in ``environment.json``; what was missing was
    anything that read it. A blocking difference does not invalidate either
    run — each is a valid measurement of its own environment — but it does
    mean the columns beside each other are not a comparison.
    """

    lines = [
        "",
        "## Environment (comparability — blocking differences are NOT comparable)",
        "",
        "Blocking: interpreter, product identity (version, repo head, dirty tree),",
        "captured `EXOMEM_*` knobs, and any distribution inside the product's",
        "runtime closure. Reported: everything else installed, the interpreter",
        "build string, platform/machine, generator version. A field one side did",
        "not record is `unverifiable` — recorded as such, never as agreement.",
        "",
    ]
    if not runs:
        lines.append("No runs.")
        return lines
    reference = runs[0]
    lines.extend(
        [
            f"Reference for this table: **{reference.run_id}** (the first run given).",
            "",
            "| run | interpreter | product | distributions | vs reference |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    blocked: list[tuple[_RunView, tuple]] = []
    for run in runs:
        python_cell, product_cell, dist_cell = _environment_cells(run)
        if run is reference:
            verdict = "reference"
        elif run.environment is None or reference.environment is None:
            verdict = "**unverifiable** (an environment was not recorded)"
        else:
            comparison = compare_environments(reference.environment, run.environment)
            if comparison.blocked:
                fields = ", ".join(d.field for d in comparison.blocking)
                verdict = f"**NOT COMPARABLE** — blocking: {fields}"
                blocked.append((run, comparison.blocking))
            elif comparison.reported:
                verdict = (
                    f"comparable · {len(comparison.reported)} reported "
                    f"({len(comparison.unverifiable)} unverifiable)"
                )
            else:
                verdict = "identical on every recorded field"
        lines.append(
            f"| {run.label} | {python_cell} | {product_cell} | {dist_cell} | {verdict} |"
        )
    lines.append("")
    if blocked:
        lines.extend(
            [
                "**These runs were measured in different environments. The blocking",
                "differences below invalidate any comparison drawn between the",
                "columns above — a difference in these numbers is not evidence about",
                "the contenders until the environments match.**",
                "",
                "| run | field | reference | observed |",
                "| --- | --- | --- | --- |",
            ]
        )
        for run, differences in blocked:
            for difference in differences:
                lines.append(
                    f"| {run.label} | {difference.field} | {difference.reference} "
                    f"| {difference.observed} |"
                )
        lines.append("")
    else:
        lines.extend(["No blocking environment difference between these runs.", ""])
    return lines


def _retrieval_floor_section(runs: Sequence[_RunView]) -> list[str]:
    lines = [
        "",
        "## Retrieval floor (zero hits everywhere is a fault, not a score)",
        "",
        "| run | status | queries with hits | total hits |",
        "| --- | --- | --- | --- |",
    ]
    for run in runs:
        floor = run.retrieval_floor or {}
        status = str(floor.get("status", "not recorded"))
        emphasis = "**" if status in ("floor_violation", "near_zero") else ""
        queries = floor.get("queries", "?")
        lines.append(
            f"| {run.label} | {emphasis}{status}{emphasis} "
            f"| {floor.get('queries_with_hits', '?')}/{queries} "
            f"| {floor.get('total_hits', '?')} |"
        )
    details = [
        f"- {run.label}: {(run.retrieval_floor or {}).get('detail')}"
        for run in runs
        if (run.retrieval_floor or {}).get("status") in ("floor_violation", "near_zero")
    ]
    if details:
        lines.append("")
        lines.extend(details)
    return lines


#: Reference contenders. These are instruments, not products: they exist to
#: bound the scale a real result is read on, and must never appear in a product
#: comparison as though they were competitors.
CEILING_PROVIDER = "oracle-retrieval"
FLOOR_PROVIDER = "null-abstain"
REFERENCE_PROVIDERS = frozenset({CEILING_PROVIDER, FLOOR_PROVIDER})


def _pass_count(run: _RunView, dimension: str) -> int | None:
    counts = run.dimensions.get(dimension)
    if not isinstance(counts, dict):
        return None
    value = counts.get("pass")
    return int(value) if isinstance(value, int) else None


def _was_attempted(run: _RunView, dimension: str) -> bool:
    """Did any row in this dimension reach a verdict other than n/a?

    A dimension that is not-applicable everywhere was never exercised — Track C
    behaviour rows in a Track B run, say — and a zero span there means "not
    measured here", which is unremarkable. A dimension that WAS attempted and
    still has a zero span is unpassable, which is a defect. Reporting both as
    VOID would bury the second in the first.
    """

    counts = run.dimensions.get(dimension)
    if not isinstance(counts, dict):
        return False
    return any(int(counts.get(key, 0) or 0) > 0 for key in ("pass", "fail", "unsupported"))


def _bounds_section(
    runs: Sequence[_RunView], *, cross_contender: bool = False
) -> list[str]:
    """Floor and ceiling per dimension, and where each contender sits between.

    A pass count on its own is not a measurement. "148 factual_qa" is only
    interpretable once a reader knows that a perfect retriever scores 172 and a
    contender that retrieves nothing scores 0 — the same number would mean
    something entirely different against a ceiling of 150 or a floor of 140.

    Two failure modes this table is specifically here to expose:

    - **A void dimension**, where floor equals ceiling. Nothing can score and
      nothing can differentiate, so any equality between contenders there is an
      artifact of the gate rather than a finding about the products. Reported
      as VOID rather than as a shared zero, because a shared zero reads as a
      shared capability gap and this is not one.
    - **A contender at or below the floor**, which means the dimension is not
      testing it at all.
    """

    ceiling = next(
        (r for r in runs if r.provider == CEILING_PROVIDER and not r.invalid), None
    )
    floor = next((r for r in runs if r.provider == FLOOR_PROVIDER and not r.invalid), None)
    if ceiling is None and floor is None:
        return [
            "",
            "## Bounds (reference contenders)",
            "",
            f"Not available: this comparison includes neither `{CEILING_PROVIDER}` "
            f"(ceiling) nor `{FLOOR_PROVIDER}` (floor), so every figure above is a "
            "count without a scale. Add a reference run before publishing any of them.",
        ]

    contenders = [
        r for r in runs if r.provider not in REFERENCE_PROVIDERS and not r.invalid
    ]
    dimension_names = sorted(
        {
            name
            for run in runs
            for name in run.dimensions
            if not name.startswith("_") and name not in GOVERNANCE_DIMENSIONS
        }
    )

    header = ["dimension", "floor", "ceiling", "usable range"]
    header.extend(r.label for r in contenders)
    lines = [
        "",
        "## Bounds (reference contenders)",
        "",
        "`oracle-retrieval` returns exactly the sources the oracle admits for each",
        "query, so its score is the best any retriever could earn **under this",
        "scorer**; `null-abstain` retrieves nothing, so its score is what pure",
        "abstention earns. Both are instruments rather than products and are",
        "excluded from the contender columns.",
        "",
        "A ceiling below the query count is a harness defect, not a product",
        "finding — those queries cannot be passed by anything. Percentages are",
        "position in the usable range, `(score - floor) / (ceiling - floor)`.",
        "",
        "Governance dimensions are omitted here: under default-open governance the",
        "floor posts the best sheet in the suite (retrieving nothing cannot leak),",
        "so a floor-to-ceiling scale would be meaningless in the wrong direction.",
        "",
        "| " + " | ".join(header) + " |",
        "| ---" * len(header) + " |",
    ]

    raw_altitude = {
        r.ingestion_altitude for r in runs if r.ingestion_altitude
    } == {"raw_source"}
    for name in dimension_names:
        if raw_altitude and name in ALTITUDE_DEPENDENT_DIMENSIONS:
            # Structurally unmeasurable here: what this scores was never built.
            # Rendering the counts would publish a zero that reads as a finding
            # about the contender rather than about the benchmark.
            lines.append(
                f"| {name} | — | — | *not measurable at this altitude* |"
                + " — |" * len(contenders)
            )
            continue
        low = _pass_count(floor, name) if floor is not None else None
        high = _pass_count(ceiling, name) if ceiling is not None else None
        low_cell = "—" if low is None else str(low)
        high_cell = "—" if high is None else str(high)
        attempted = any(_was_attempted(r, name) for r in runs)
        if low is not None and high is not None:
            span = high - low
            if span > 0:
                range_cell = str(span)
            elif attempted:
                range_cell = "**VOID** (0)"
            else:
                range_cell = "not exercised"
        else:
            span = None
            range_cell = "—"
        if name == "abstention" and cross_contender:
            floor_withheld = floor is not None and floor.answer_mode == ANSWER_MODE_HARNESS
            ceiling_withheld = ceiling is not None and ceiling.answer_mode == ANSWER_MODE_HARNESS
            row = [
                name,
                WITHHELD_HARNESS_ABSTENTION if floor_withheld else low_cell,
                WITHHELD_HARNESS_ABSTENTION if ceiling_withheld else high_cell,
                WITHHELD_HARNESS_ABSTENTION
                if floor_withheld or ceiling_withheld
                else range_cell,
            ]
        else:
            row = [name, low_cell, high_cell, range_cell]
        for contender in contenders:
            if (
                name == "abstention"
                and cross_contender
                and contender.answer_mode == ANSWER_MODE_HARNESS
            ):
                row.append(WITHHELD_HARNESS_ABSTENTION)
                continue
            score = _pass_count(contender, name)
            if score is None:
                row.append("—")
            elif span is None:
                row.append(str(score))
            elif span <= 0:
                row.append("**not measurable**" if attempted else "—")
            elif low is not None and score <= low:
                row.append(f"{score} (**at/below floor**)")
            else:
                row.append(f"{score} ({(score - (low or 0)) / span:.0%})")
        lines.append("| " + " | ".join(row) + " |")

    if raw_altitude:
        lines.extend(
            [
                "",
                "**Every run here measured at raw-source altitude.** Documents were",
                "loaded verbatim and nothing was compiled from them, so no citation",
                "chain and no compiled contradiction exist to score. "
                + ", ".join(f"`{d}`" for d in sorted(ALTITUDE_DEPENDENT_DIMENSIONS))
                + " are",
                "withheld rather than reported as zeros, because a zero there would",
                "read as a finding about the contender instead of a statement about",
                "what the benchmark built.",
            ]
        )
    voids = [
        name
        for name in dimension_names
        if not (raw_altitude and name in ALTITUDE_DEPENDENT_DIMENSIONS)
        and floor is not None
        and ceiling is not None
        and (_pass_count(ceiling, name) or 0) - (_pass_count(floor, name) or 0) <= 0
        # Attempted-and-unpassable is a defect; never-attempted is just a
        # dimension this run set does not exercise.
        and any(_was_attempted(r, name) for r in runs)
    ]
    if voids:
        lines.extend(
            [
                "",
                f"**VOID dimensions: {', '.join(f'`{v}`' for v in voids)}.** Floor equals",
                "ceiling, so these rows have no discriminative range at all. Every",
                "contender scoring the same there is a property of the gate, and must",
                "not be reported as a shared product capability gap.",
            ]
        )
    return lines


def build_comparison_report(run_dirs: Sequence[Path], out_path: Path) -> Path:
    """Render a cross-run markdown comparison to ``out_path``."""

    runs = [_load_run(Path(run_dir)) for run_dir in run_dirs]
    _dedupe_labels(runs)
    labels = [run.label for run in runs]
    cross_contender = len({run.provider for run in runs} - REFERENCE_PROVIDERS) > 1

    lines: list[str] = [
        "# membench cross-run comparison",
        "",
        "No weighted aggregate is computed anywhere. Deterministic gates are",
        "final; judge output (if present) is advisory and never enters the",
        "dimension tables. Invalid runs render INVALID, never numbers.",
        "",
        "Read the Environment section before reading any difference in the",
        "tables below: runs measured under different interpreters, product",
        "revisions or runtime dependencies are marked NOT COMPARABLE there, and",
        "a difference between two such columns is not evidence about either",
        "contender.",
        "",
        "## Runs",
        "",
        "| run_id | provider · profile | status | run failures (kept in denominators) |",
        "| --- | --- | --- | --- |",
    ]
    for run in runs:
        status = f"INVALID: {run.invalid_reason}" if run.invalid else "ok"
        if run.publication_label:
            status = f"{status} · {run.publication_label}"
        lines.append(
            f"| {run.run_id} | {run.label} | {status} | {run.run_failures} |"
        )

    lines.extend(
        [
            "",
            "## Dimensions (deterministic; judge values never incorporated)",
            "",
            "Every count in this table came from a deterministic gate. Judged",
            "verdicts are reported in their own section below and are never",
            "added here — subtract the judged contribution by ignoring that",
            "section.",
            "",
            "Governance dimensions are comparable only between runs measured",
            "against wired governance; default-open and unsupported runs are",
            "labelled and excluded from that row. For those runs every",
            "governance-FAMILY query row (all its gate items) is excluded from",
            "the comparative counts — cells that would consist only of such",
            "rows render the label instead.",
            "",
            "| dimension | " + " | ".join(labels) + " |",
            "| --- |" + " --- |" * len(runs),
        ]
    )
    dimension_names = sorted(
        {
            name
            for run in runs
            for name in run.dimensions
            if not name.startswith("_")
        }
    )
    for name in dimension_names:
        cells = " | ".join(
            _dimension_cell(run, name, cross_contender=cross_contender) for run in runs
        )
        lines.append(f"| {name} | {cells} |")

    lines.extend(_bounds_section(runs, cross_contender=cross_contender))

    lines.extend(
        [
            "",
            "## Retrieval (mean over applicable queries)",
            "",
            "| metric | " + " | ".join(labels) + " |",
            "| --- |" + " --- |" * len(runs),
        ]
    )
    retrieval = [
        None if run.invalid else _retrieval_stats(_comparable_rows(run)) for run in runs
    ]

    def _all_retrieval_excluded(run: _RunView, stats: dict | None) -> bool:
        """Exclusion (not absence) emptied this run's comparative retrieval."""

        return (
            stats is None
            and run.governance_state != "wired"
            and any(isinstance(row.get("retrieval"), dict) for row in run.per_query)
        )

    for metric in ("mean_recall_at_5", "mean_recall_at_10", "mean_mrr"):
        cells = []
        for run, stats in zip(runs, retrieval, strict=True):
            if run.invalid:
                cells.append("INVALID")
            elif _all_retrieval_excluded(run, stats):
                cells.append(_governance_exclusion_label(run))
            elif stats is None:
                cells.append("n/a")
            else:
                cells.append(_fmt(float(stats[metric])))
        lines.append(f"| {metric} | " + " | ".join(cells) + " |")
    applicable_cells = []
    for run, stats in zip(runs, retrieval, strict=True):
        if run.invalid:
            applicable_cells.append("INVALID")
        elif _all_retrieval_excluded(run, stats):
            applicable_cells.append(_governance_exclusion_label(run))
        else:
            applicable_cells.append(str(stats["applicable_queries"] if stats else 0))
    lines.append("| applicable_queries | " + " | ".join(applicable_cells) + " |")

    lines.extend(["", "## Latency (separate section; never mixed into quality tables)", ""])
    if cross_contender:
        lines.append(WITHHELD_LATENCY)
    else:
        lines.extend(
            [
                "| metric | " + " | ".join(labels) + " |",
                "| --- |" + " --- |" * len(runs),
            ]
        )
        for metric_name in ("median_ms", "p95_ms", "search_calls"):
            cells = []
            for run in runs:
                if run.invalid:
                    cells.append("INVALID")
                elif not run.latencies:
                    cells.append("n/a")
                elif metric_name == "median_ms":
                    cells.append(_fmt(statistics.median(run.latencies)))
                elif metric_name == "p95_ms":
                    cells.append(_fmt(_p95(run.latencies)))
                else:
                    cells.append(str(len(run.latencies)))
            lines.append(f"| {metric_name} | " + " | ".join(cells) + " |")

    # Ingest (write) latency renders regardless of cross_contender: unlike
    # retrieval latency's transport-asymmetry withholding (4b.40), its own
    # altitude caveat above is what keeps a cross-contender read honest.
    lines.extend(_ingest_latency_section(runs))

    lines.extend(_environment_section(runs))
    lines.extend(_retrieval_floor_section(runs))

    lines.extend(["", "## Failures (always visible, always in denominators)", ""])
    for run in runs:
        if run.invalid:
            lines.append(
                f"- {run.label}: INVALID ({run.invalid_reason}); "
                f"{run.failure_lines} failure record(s)"
            )
        else:
            lines.append(f"- {run.label}: {run.failure_lines} failure record(s)")

    lines.extend(_judged_lane_section(runs))

    lines.extend(["", "## Judge (advisory — deterministic gates are FINAL)", ""])
    judged = [run for run in runs if run.judge is not None]
    if not judged:
        lines.append("Judge: not run (no judge-scores.json present in any run).")
    else:
        for run in judged:
            lines.append(f"### {run.label}")
            lines.append("")
            if run.invalid:
                lines.append("INVALID run — judge output is not reported as results.")
                lines.append("")
                continue
            lines.append("| query | samples | mean | stdev | majority |")
            lines.append("| --- | --- | --- | --- | --- |")
            for row in run.judge.get("per_query", []):
                lines.append(
                    f"| {row.get('query_id', '?')} "
                    f"| {_sample_cell(row.get('samples', []))} "
                    f"| {_fmt(row.get('mean'), 2)} "
                    f"| {_fmt(row.get('stdev'), 2)} "
                    f"| {row.get('majority')} |"
                )
            lines.append("")
        conflicts = [line for run in judged for line in _gate_conflicts(run)]
        lines.append("### Gate conflicts (deterministic verdict stands)")
        lines.append("")
        if conflicts:
            lines.extend(conflicts)
        else:
            lines.append("None.")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path
