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
from dataclasses import dataclass
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
#: The query FAMILY whose rows (all their gate items, not just the governance
#: dimension) are excluded from comparative tables for non-wired runs — a
#: default-open run's vacuous abstention/temporal passes on governance-family
#: queries must never sit next to a wired run's numbers.
GOVERNANCE_FAMILY = "governance"
_GOVERNANCE_STATE_LABELS = {
    "default_open": "default-open",
    "unsupported": "unsupported",
}


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


def _load_run(run_dir: Path) -> _RunView:
    run_dir = Path(run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
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
    return _RunView(
        run_dir=run_dir,
        run_id=str(manifest.get("run_id", run_dir.name)),
        label=f"{manifest.get('provider', '?')} · {profile_name}",
        invalid=bool(manifest.get("invalid", False)),
        invalid_reason=manifest.get("invalid_reason"),
        run_failures=int(manifest.get("run_failures", 0) or 0),
        dimensions=dimensions,
        per_query=per_query,
        latencies=latencies,
        judge=judge,
        failure_lines=failure_lines,
        # Runs recorded before the three-state contract carry no field; they
        # were by construction measured against the default-open surface.
        governance_state=str(manifest.get("governance_state", "default_open")),
        judged=judged,
        environment=environment,
        retrieval_floor=floor if isinstance(floor, dict) else None,
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


def _dimension_cell(run: _RunView, dimension: str) -> str:
    if run.invalid:
        return "INVALID"
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


def build_comparison_report(run_dirs: Sequence[Path], out_path: Path) -> Path:
    """Render a cross-run markdown comparison to ``out_path``."""

    runs = [_load_run(Path(run_dir)) for run_dir in run_dirs]
    _dedupe_labels(runs)
    labels = [run.label for run in runs]

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
        cells = " | ".join(_dimension_cell(run, name) for run in runs)
        lines.append(f"| {name} | {cells} |")

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

    lines.extend(
        [
            "",
            "## Latency (separate section; never mixed into quality tables)",
            "",
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
