#!/usr/bin/env python3
"""Turn results.jsonl into the per-case runsc/runc table (diagnostic branch only).

Per case and repeat, each arm's value is the median of its calls. The ratio of
a repeat is runsc over runc. The reported ratio is the geometric mean of the
repeat ratios, with a 95% interval from Student's t on the log ratios. An
interval that includes 1.0 is "no measurable change".
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

# Two-sided 95% Student t quantiles for 1..10 degrees of freedom.
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228}


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def summarize(rows: list[dict]) -> dict[str, dict]:
    cases: dict[str, dict] = defaultdict(lambda: {"arms": defaultdict(dict), "failed": defaultdict(list),
                                                  "unit": "ms", "items": 1, "notes": defaultdict(list)})
    for row in rows:
        case = cases[row["case"]]
        case["unit"] = row.get("unit", "ms")
        case["items"] = row.get("items", 1)
        if row["ok"] and row["values"]:
            case["arms"][row["arm"]][row["rep"]] = row["values"]
            if row.get("note"):
                case["notes"][row["arm"]].append(row["note"])
        elif not row["ok"]:
            case["failed"][row["arm"]].append(row.get("note", ""))
    result = {}
    for name, case in sorted(cases.items()):
        per = {arm: {rep: statistics.median(values) for rep, values in reps.items()}
               for arm, reps in case["arms"].items()}
        runc, runsc = per.get("runc", {}), per.get("runsc", {})
        pairs = sorted(set(runc) & set(runsc))
        logs = [math.log(runsc[r] / runc[r]) for r in pairs if runc[r] > 0 and runsc[r] > 0]
        entry = {
            "unit": case["unit"], "items": case["items"], "repeats_paired": len(logs),
            "runc_median": statistics.median(runc.values()) if runc else None,
            "runsc_median": statistics.median(runsc.values()) if runsc else None,
            "runc_per_repeat": runc, "runsc_per_repeat": runsc,
            "failed": {arm: len(items) for arm, items in case["failed"].items()},
            "failure_notes": {arm: items[:2] for arm, items in case["failed"].items()},
            "ratio": None, "ci_low": None, "ci_high": None, "verdict": "n/a",
        }
        if logs:
            mean = statistics.fmean(logs)
            entry["ratio"] = math.exp(mean)
            if len(logs) >= 2:
                half = T95[min(len(logs) - 1, 10)] * statistics.stdev(logs) / math.sqrt(len(logs))
                entry["ci_low"], entry["ci_high"] = math.exp(mean - half), math.exp(mean + half)
                if entry["ci_low"] > 1.0:
                    entry["verdict"] = "slower under runsc"
                elif entry["ci_high"] < 1.0:
                    entry["verdict"] = "faster under runsc"
                else:
                    entry["verdict"] = "no measurable change"
            else:
                entry["verdict"] = "single repeat"
        result[name] = entry
    return result


def fmt(value, digits=1):
    return "-" if value is None else f"{value:,.{digits}f}"


ARMS = ("runc: the cell on Docker's default runtime, data on a Docker volume. "
        "runsc: the cell on gVisor (runsc 20261026.0, systrap, directfs on) with the volume as a read-only lower "
        "layer and every write held in sandbox memory (`--overlay2=all:memory`), because plain runsc cannot start "
        "the cell on a volume. The init container and the import Job run on plain runsc over the volume.")


def render(summary: dict[str, dict]) -> str:
    lines = [ARMS, "", "| case | runc median | runsc median | ratio (runsc/runc) | 95% interval | n | verdict | throughput runc -> runsc (items/s) | failures |",
             "|---|---:|---:|---:|---|---:|---|---|---|"]
    for name, e in summary.items():
        interval = "-" if e["ci_low"] is None else f"{e['ci_low']:.2f} to {e['ci_high']:.2f}"
        throughput = "-"
        if e["items"] > 1 and e["runc_median"] and e["runsc_median"]:
            throughput = f"{e['items'] / (e['runc_median'] / 1000):.1f} -> {e['items'] / (e['runsc_median'] / 1000):.1f}"
        failures = ", ".join(f"{arm}:{count}" for arm, count in e["failed"].items()) or "-"
        ratio = "-" if e["ratio"] is None else f"{e['ratio']:.2f}"
        lines.append(f"| {name} | {fmt(e['runc_median'])} {e['unit']} | {fmt(e['runsc_median'])} {e['unit']} | "
                     f"{ratio} | {interval} | {e['repeats_paired']} | {e['verdict']} | {throughput} | {failures} |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", type=Path)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--md-out", type=Path)
    args = parser.parse_args()
    summary = summarize(load(args.results))
    table = render(summary)
    print(table)
    if args.json_out:
        args.json_out.write_text(json.dumps(summary, indent=1, default=str))
    if args.md_out:
        args.md_out.write_text(table + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
