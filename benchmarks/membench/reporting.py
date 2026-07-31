"""Cross-run comparison reporting + judge score merging.

Publication contract enforced here:

- Deterministic dimension tables NEVER incorporate judge values.
- No weighted aggregate exists anywhere.
- Latency lives in its own section, never mixed into quality tables.
- An INVALID run renders ``INVALID`` in every metric column — never numbers.
- Judge output is advisory: when the judge says ``semantic_match=true`` but
  any deterministic gate failed, the query is annotated
  ``gate_conflict (deterministic verdict stands)``; the deterministic
  verdict is final.
"""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from membench.judge.backends import parse_judge_verdict
from membench.judge.handshake import PairedResponse, append_failure, load_requests

JUDGE_SCORES_NAME = "judge-scores.json"
GATE_CONFLICT_NOTE = "gate_conflict (deterministic verdict stands)"
_STATUS_KEYS = ("pass", "fail", "not_applicable", "unsupported")


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
    failures_path = run_dir / "failures.jsonl"
    failure_lines = 0
    if failures_path.is_file():
        failure_lines = sum(
            1 for raw in failures_path.read_text(encoding="utf-8").splitlines() if raw.strip()
        )
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


def _dimension_cell(run: _RunView, dimension: str) -> str:
    if run.invalid:
        return "INVALID"
    counts = run.dimensions.get(dimension)
    if not isinstance(counts, dict):
        return "—"
    return " · ".join(
        f"{key.replace('not_applicable', 'n/a')}={counts.get(key, 0)}" for key in _STATUS_KEYS
    )


def _retrieval_stats(run: _RunView) -> dict[str, float | int] | None:
    blocks = [
        row["retrieval"]
        for row in run.per_query
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
    retrieval = [None if run.invalid else _retrieval_stats(run) for run in runs]
    for metric in ("mean_recall_at_5", "mean_recall_at_10", "mean_mrr"):
        cells = []
        for run, stats in zip(runs, retrieval, strict=True):
            if run.invalid:
                cells.append("INVALID")
            elif stats is None:
                cells.append("n/a")
            else:
                cells.append(_fmt(float(stats[metric])))
        lines.append(f"| {metric} | " + " | ".join(cells) + " |")
    applicable_cells = [
        "INVALID" if run.invalid else str(stats["applicable_queries"] if stats else 0)
        for run, stats in zip(runs, retrieval, strict=True)
    ]
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

    lines.extend(["", "## Failures (always visible, always in denominators)", ""])
    for run in runs:
        if run.invalid:
            lines.append(
                f"- {run.label}: INVALID ({run.invalid_reason}); "
                f"{run.failure_lines} failure record(s)"
            )
        else:
            lines.append(f"- {run.label}: {run.failure_lines} failure record(s)")

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
