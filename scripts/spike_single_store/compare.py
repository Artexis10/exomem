"""Per-case comparison of the two arms, with bootstrap and Wilson intervals.

Usage: compare.py <bench_out_dir> [--label L]
Reads arm-a/arm_files.jsonl, arm-b/arm_single.jsonl, arm-b/projection.jsonl,
kill-*/trials.jsonl and compare_tables.json from one bench output directory.

The storage portion of today's acknowledgement is the canonical Markdown commit
plus the post-commit derived fan-out (spans `derived.canonical_commit` and
`derived.fanout`); the prototype's acknowledgement is derivation plus its one
transaction. Governance before the commit is common to both designs and is
reported separately, not compared.
"""
from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path

SEED = 20261107
RESAMPLES = 10_000


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    if not xs:
        return float("nan")
    k = (len(xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def boot_paired(a: list[float], b: list[float], stat) -> tuple[float, float, float]:
    rng = random.Random(SEED)
    n = len(a)
    point = stat(a) - stat(b)
    draws = []
    for _ in range(RESAMPLES):
        idx = [rng.randrange(n) for _ in range(n)]
        draws.append(stat([a[i] for i in idx]) - stat([b[i] for i in idx]))
    draws.sort()
    return point, draws[int(0.025 * RESAMPLES)], draws[int(0.975 * RESAMPLES)]


def wilson(k: int, n: int) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    z = 1.96
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (round(centre - half, 3), round(centre + half, 3))


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]


def r(x: float, d: int = 1) -> float:
    return round(x, d)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--label", default="")
    args = ap.parse_args()
    out = args.out_dir
    a_rows = {row["n"]: row for row in load(out / "arm-a" / "arm_files.jsonl")}
    b_rows = {row["n"]: row for row in load(out / "arm-b" / "arm_single.jsonl")}
    ok = sorted(n for n in a_rows if a_rows[n].get("error") is None and n in b_rows
                and b_rows[n].get("error") is None)
    report: dict = {"label": args.label, "ops_today": len(a_rows),
                    "ops_today_refused": sum(1 for row in a_rows.values() if row.get("error")),
                    "paired_cases": len(ok)}

    def spans(n, name):
        return a_rows[n].get("spans", {}).get(name, 0.0)

    a_storage = [spans(n, "derived.canonical_commit") + spans(n, "derived.fanout") for n in ok]
    a_ack = [a_rows[n]["ack_ms"] for n in ok]
    b_ack = [b_rows[n]["ack_ms"] for n in ok]
    report["ack_ms"] = {
        "today_total": {"p50": r(pct(a_ack, .5)), "p95": r(pct(a_ack, .95))},
        "today_storage": {"p50": r(pct(a_storage, .5)), "p95": r(pct(a_storage, .95))},
        "today_canonical_commit": {"p50": r(pct([spans(n, "derived.canonical_commit") for n in ok], .5))},
        "today_derived_fanout": {"p50": r(pct([spans(n, "derived.fanout") for n in ok], .5)),
                                 "p95": r(pct([spans(n, "derived.fanout") for n in ok], .95))},
        "single_store": {"p50": r(pct(b_ack, .5), 2), "p95": r(pct(b_ack, .95), 2)},
        "single_derive": {"p50": r(pct([b_rows[n]["derive_ms"] for n in ok], .5), 2)},
        "single_commit": {"p50": r(pct([b_rows[n]["commit_ms"] for n in ok], .5), 2),
                          "p95": r(pct([b_rows[n]["commit_ms"] for n in ok], .95), 2)},
    }
    if ok:
        for name, stat in (("p50", lambda xs: pct(xs, .5)), ("p95", lambda xs: pct(xs, .95)),
                           ("mean", lambda xs: sum(xs) / len(xs))):
            point, lo, hi = boot_paired(a_storage, b_ack, stat)
            report["ack_ms"][f"storage_minus_single_{name}"] = {"diff": r(point), "ci95": [r(lo), r(hi)]}
    span_totals: dict[str, float] = {}
    for n in ok:
        for name, ms in a_rows[n].get("spans", {}).items():
            span_totals[name] = span_totals.get(name, 0.0) + ms
    report["today_top_spans_mean_ms"] = {k: r(v / max(1, len(ok))) for k, v in
                                         sorted(span_totals.items(), key=lambda kv: -kv[1])[:14]}

    def vis(rows, key):
        vals = [rows[n].get(key) is True for n in ok]
        return {"visible": sum(vals), "of": len(vals), "wilson95": wilson(sum(vals), len(vals))}

    report["visible_at_ack"] = {"today_lexical": vis(a_rows, "lex_at_ack"),
                                "today_graph": vis(a_rows, "graph_at_ack"),
                                "single_lexical": vis(b_rows, "lex_at_ack"),
                                "single_graph": vis(b_rows, "graph_at_ack")}
    lags = {}
    for key in ("lex_lag_ms", "graph_lag_ms"):
        xs = [a_rows[n][key] for n in ok if a_rows[n].get(key) not in (None, 0.0)]
        never = sum(1 for n in ok if a_rows[n].get(key) is None)
        lags[key] = {"late": len(xs), "never_within_timeout": never,
                     "p50": r(pct(xs, .5)) if xs else None, "max": r(max(xs)) if xs else None}
    report["today_lag_after_ack"] = lags
    exp = [b_rows[n]["export_lag_ms"] for n in ok if b_rows[n].get("export_lag_ms") is not None]
    proj = [row["ms"] for row in load(out / "arm-b" / "projection.jsonl")]
    report["single_export"] = {
        "export_lag_ms": {"p50": r(pct(exp, .5)), "p95": r(pct(exp, .95)),
                          "missing": len(ok) - len(exp)},
        "per_file_ms": {"p50": r(pct(proj, .5), 2), "p95": r(pct(proj, .95), 2),
                        "files": len(proj)},
        "files_per_op_today": r(sum(a_rows[n].get("files_written", 1) for n in ok) / max(1, len(ok)), 2),
    }
    tables = out / "compare_tables.json"
    if tables.exists() and tables.read_text().strip():
        first = json.loads(tables.read_text().splitlines()[0])
        report["derived_rows"] = {"written_paths": first.get("paths"),
                                  "whole_tables": first.get("whole_tables")}
    kills = {}
    for arm in ("files", "single"):
        trials = [t for t in load(out / f"kill-{arm}" / "trials.jsonl") if t.get("summary")]
        if not trials:
            continue
        if arm == "files":
            dirty = [t for t in trials if sum(v for k, v in t["summary"]["before"].items()) > 0]
            unhealed = [t for t in trials if sum(v for k, v in t["summary"]["after"].items()
                                                 if k != "t_s") > 0]
            torn = sum(t["summary"].get("torn", 0) for t in trials)
        else:
            dirty = [t for t in trials if t["summary"]["before_pending"] > 0
                     or t["summary"]["before_stale_unqueued"] > 0]
            unhealed = [t for t in trials if not t["summary"]["after_all_files_match"]
                        or sum(t["summary"]["derived_mismatch"].values()) > 0
                        or t["summary"]["integrity"] != "ok"]
            torn = 0
        kills[arm] = {"trials": len(trials), "left_work_for_recovery": len(dirty),
                      "needs_repair_after_recovery": len(unhealed),
                      "needs_repair_wilson95": wilson(len(unhealed), len(trials)),
                      "torn_pages": torn,
                      "ops_acked_before_kill_median": pct([t["ops_acked_before_kill"] for t in trials], .5)}
    report["hard_kills"] = kills
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
