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


def summarize(rows: list[dict], base: str = "runc", test: str = "runsc") -> dict[str, dict]:
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
        runc, runsc = per.get(base, {}), per.get(test, {})
        pairs = sorted(set(runc) & set(runsc))
        logs = [math.log(runsc[r] / runc[r]) for r in pairs if runc[r] > 0 and runsc[r] > 0]
        entry = {
            "unit": case["unit"], "items": case["items"], "repeats_paired": len(logs),
            "base_median": statistics.median(runc.values()) if runc else None,
            "test_median": statistics.median(runsc.values()) if runsc else None,
            "base_per_repeat": runc, "test_per_repeat": runsc,
            "failed": {arm: len(items) for arm, items in case["failed"].items() if arm in (base, test)},
            "failure_notes": {arm: items[:2] for arm, items in case["failed"].items() if arm in (base, test)},
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


ARMS = ("runc: Docker's default runtime. runsc: gVisor runsc 20261026.0, systrap platform, directfs on. "
        "runsc-kvm: the same on the KVM platform (shown only when the runner can start it). "
        "Plain runsc cannot start the cell on a volume, so in every arm the cell runs with /data on a tmpfs "
        "inside the container, copied from the session volume at start: the cells here never touch a disk. "
        "The init container, the import Job and the f.* file benchmarks run on the volume itself.")


def fmt(value, digits=1):
    return "-" if value is None else f"{value:,.{digits}f}"


def render(summary: dict[str, dict], test: str) -> str:
    lines = [f"#### {test} against runc", "",
             f"| case | runc median | {test} median | ratio ({test}/runc) | 95% interval | n | verdict "
             f"| throughput runc -> {test} (items/s) | failures |",
             "|---|---:|---:|---:|---|---:|---|---|---|"]
    for name, e in summary.items():
        interval = "-" if e["ci_low"] is None else f"{e['ci_low']:.2f} to {e['ci_high']:.2f}"
        throughput = "-"
        if e["items"] > 1 and e["base_median"] and e["test_median"]:
            throughput = f"{e['items'] / (e['base_median'] / 1000):.1f} -> {e['items'] / (e['test_median'] / 1000):.1f}"
        failures = ", ".join(f"{arm}:{count}" for arm, count in e["failed"].items()) or "-"
        ratio = "-" if e["ratio"] is None else f"{e['ratio']:.2f}"
        lines.append(f"| {name} | {fmt(e['base_median'])} {e['unit']} | {fmt(e['test_median'])} {e['unit']} | "
                     f"{ratio} | {interval} | {e['repeats_paired']} | {e['verdict']} | {throughput} | {failures} |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", type=Path)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--md-out", type=Path)
    args = parser.parse_args()
    rows = load(args.results)
    tests = sorted({row["arm"] for row in rows} - {"runc"})
    summaries = {test: summarize(rows, "runc", test) for test in tests}
    document = "\n\n".join([ARMS, *(render(summary, test) for test, summary in summaries.items())])
    print(document)
    if args.json_out:
        args.json_out.write_text(json.dumps(summaries, indent=1, default=str))
    if args.md_out:
        args.md_out.write_text(document + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
