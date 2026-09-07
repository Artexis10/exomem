"""Reconstruct a workflow's server occupancy from content-free call-ledger fields.

Select a client and an explicit UTC time range. These select a time slice, not
an authenticated workflow identity: the caller must establish its provenance.
Ledger timestamps record completion. Client gaps cannot identify model time,
user pauses or connector overhead without a matching client trace.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


def _timestamp(value: str) -> float:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("UTC offset required")
    return parsed.timestamp() * 1000


def summarize_rows(rows: list[dict]) -> dict:
    calls = []
    invalid = []
    measured_spans = {}
    for index, row in enumerate(rows, 1):
        try:
            end = _timestamp(row["ts_utc"])
            if type(row.get("sequence")) is not int or row["sequence"] < 1:
                raise ValueError("invalid ledger sequence")
            total = row["total_ms"]
            if isinstance(total, bool) or not isinstance(total, (int, float)):
                raise ValueError("invalid duration")
            if not math.isfinite(total) or total < 0 or not isinstance(row["tool"], str):
                raise ValueError("invalid duration or tool")
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", row["tool"]):
                raise ValueError("invalid tool name")
            code = row.get("error_code")
            if code is not None and (
                not isinstance(code, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,95}", code)
            ):
                raise ValueError("invalid error code")
            if row.get("outcome", "unknown") not in {"ok", "refused", "error", "unknown"}:
                raise ValueError("invalid outcome")
            calls.append((end - total, end, total, row))
        except (KeyError, ValueError, TypeError, OverflowError):
            invalid.append({"row": index, "reason": "invalid_call_timing"})
            continue
        spans = row.get("spans", [])
        if not isinstance(spans, list):
            invalid.append({"row": index, "reason": "invalid_spans"})
            continue
        observed_names = set()
        for span_index, span in enumerate(spans, 1):
            try:
                name, elapsed, count = span["name"], span["ms"], span["count"]
                if (
                    not isinstance(name, str)
                    or not re.fullmatch(r"[a-z][a-z0-9_.]{0,63}", name)
                    or type(count) is not int or count < 1
                    or isinstance(elapsed, bool) or not isinstance(elapsed, (int, float))
                    or not math.isfinite(elapsed) or elapsed < 0
                ):
                    raise ValueError("invalid span")
            except (KeyError, TypeError, ValueError):
                invalid.append({"row": index, "span": span_index, "reason": "invalid_span"})
                continue
            slot = measured_spans.setdefault(name, {"calls_with_measurement": 0, "invocations": 0, "sum_ms": 0})
            slot["calls_with_measurement"] += name not in observed_names
            slot["invocations"] += count
            slot["sum_ms"] = round(slot["sum_ms"] + elapsed, 3)
            observed_names.add(name)
    calls.sort(key=lambda item: item[0])
    intervals = []
    by_tool = defaultdict(list)
    codes = Counter()
    warming = []
    open_span = None
    for start, end, total, row in calls:
        by_tool[row["tool"]].append(total)
        if intervals and start <= intervals[-1][1]:
            intervals[-1][1] = max(end, intervals[-1][1])
        else:
            intervals.append([start, end])
        if row.get("error_code"):
            codes[row["error_code"]] += 1
    # Observations follow completion order, independently of overlap accounting.
    for _start, end, _total, row in sorted(
        calls, key=lambda item: (item[1], item[3]["sequence"])
    ):
        if row.get("error_code") == "RETRIEVAL_INDEX_WARMING":
            if open_span is None:
                open_span = {"first_refusal_ms": end, "refusals": 0}
            open_span["refusals"] += 1
        elif row["tool"] == "ask_memory" and row.get("outcome") == "ok" and open_span:
            warming.append({
                "elapsed_ms": round(end - open_span["first_refusal_ms"], 3),
                "refusals": open_span["refusals"],
                "closed_by_success": True,
                "continuous_outage_proven": False,
            })
            open_span = None
    if open_span:
        warming.append({
            "elapsed_ms": None,
            "refusals": open_span["refusals"],
            "closed_by_success": False,
            "continuous_outage_proven": False,
        })
    occupied = sum(end - start for start, end in intervals)
    wall = max(item[1] for item in calls) - calls[0][0] if calls else None
    return {
        "schema_version": 1,
        "complete": not invalid,
        "public_tool_calls": len(calls),
        "clock_basis": "UTC completion timestamps plus monotonic server durations",
        "clock_continuity_verified": False,
        "ledger_observation_span_ms": round(wall, 3) if wall is not None else None,
        "workflow_wall_ms": None,
        "server_execution_sum_ms": round(sum(item[2] for item in calls), 3),
        "server_occupied_ms": round(occupied, 3),
        "server_idle_within_observed_span_ms": round(wall - occupied, 3) if wall is not None else None,
        "client_gap_ms": None,
        "connector_overhead_ms": None,
        "model_planning_ms": None,
        "verification_only_calls": None,
        "measured_spans": measured_spans,
        "canonical_commit_ms": measured_spans.get("derived.canonical_commit", {}).get("sum_ms"),
        "canonical_commit_coverage_calls": measured_spans.get("derived.canonical_commit", {}).get("calls_with_measurement", 0),
        "canonical_commit_p95_ms": None,
        "unmeasured_reason": (
            "The ledger measures server wrapper time. Recorded spans can nest and "
            "aggregate multiple invocations; do not add them or infer invocation percentiles. "
            "Client traces are needed for transport, model planning, pauses and "
            "verification intent. Refusal spans are sampled observations. "
            "UTC interval reconstruction assumes no wall-clock adjustment; "
            "the ledger alone cannot verify that assumption."
        ),
        "warming_refusals": codes["RETRIEVAL_INDEX_WARMING"],
        "observed_refusal_spans": warming,
        "outcomes": dict(Counter(item[3].get("outcome", "unknown") for item in calls)),
        "error_codes": dict(codes),
        "by_tool": {
            tool: {"calls": len(values), "server_ms": round(sum(values), 3)}
            for tool, values in sorted(by_tool.items())
        },
        "invalid_rows": invalid,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledger", type=Path, nargs="+")
    parser.add_argument("--client", required=True)
    parser.add_argument("--start", required=True, help="Inclusive completion timestamp with UTC offset")
    parser.add_argument("--end", required=True, help="Inclusive completion timestamp with UTC offset")
    args = parser.parse_args(argv)
    try:
        start, end = _timestamp(args.start), _timestamp(args.end)
    except ValueError:
        parser.error("--start and --end require ISO timestamps with UTC offsets")
    if start > end:
        parser.error("--start must precede --end")
    rows, failures = [], []
    for file_index, path in enumerate(args.ledger, 1):
        try:
            with path.open(encoding="utf-8") as stream:
                for line_index, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            raise ValueError("row must be an object")
                        when = _timestamp(row["ts_utc"])
                        if row.get("client_name") == args.client and start <= when <= end:
                            rows.append(row)
                    except (KeyError, ValueError, TypeError, OverflowError):
                        failures.append({"file": file_index, "line": line_index, "reason": "invalid_row"})
        except OSError:
            failures.append({"file": file_index, "reason": "unreadable_file"})
    report = summarize_rows(rows)
    report["input_failures"] = failures
    report["complete"] = report["complete"] and not failures and bool(rows)
    print(json.dumps(report, indent=2))
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
