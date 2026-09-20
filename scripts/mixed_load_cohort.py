"""Collect three serial idle/media pairs on one otherwise idle POSIX runner."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from mixed_load_benchmark import require_external_roots

ROOT = Path(__file__).resolve().parents[1]
OUTER_TIMEOUT_S = 1800
MAX_RUNS = 6
OVERHEAD_BUDGET_S = 1800
TOTAL_MAX_BUDGET_S = MAX_RUNS * OUTER_TIMEOUT_S + OVERHEAD_BUDGET_S


def case_order(pairs: int) -> list[tuple[int, str]]:
    if not 1 <= pairs <= 10:
        raise ValueError("pairs must be between 1 and 10")
    return [(pair, case) for pair in range(1, pairs + 1)
            for case in (("idle", "media") if pair % 2 else ("media", "idle"))]


def accepted_outcome(status: object, exit_code: int) -> bool:
    return (status, exit_code) in {("pass", 0), ("fail", 1)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pages", type=int, default=3800)
    parser.add_argument("--cycles", type=int, default=40)
    parser.add_argument("--media-groups", type=int, default=4)
    parser.add_argument("--pairs", type=int, default=3)
    args = parser.parse_args(argv)
    if os.name != "posix":
        parser.error("the cohort runner requires POSIX process-group cleanup")
    order = case_order(args.pairs)
    if not 4 <= args.pages <= 8000 or args.cycles < 1 or not 1 <= args.media_groups <= 10:
        parser.error("invalid pages, cycles or media-groups")
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip():
        parser.error("commit measured driver before running the cohort")
    args.output = args.output.absolute()
    require_external_roots([args.output], [ROOT])
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "source_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "pages": args.pages, "cycles": args.cycles, "media_groups": args.media_groups, "order": order,
        "call_timeout_s": 300, "startup_timeout_s": 600, "graph_timeout_s": 300,
        "outer_timeout_s": OUTER_TIMEOUT_S,
        "overhead_budget_s": OVERHEAD_BUDGET_S,
        "total_max_budget_s": len(order) * OUTER_TIMEOUT_S + OVERHEAD_BUDGET_S,
        "started_utc": dt.datetime.now(dt.UTC).isoformat(),
        "collection_status": "running", "runs": [],
    }
    manifest_path = args.output / "cohort.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    invalid = False
    for pair, case in order:
        run = args.output / f"pair-{pair}-{case}"
        run.mkdir()
        command = [sys.executable, str(ROOT / "scripts/mixed_load_benchmark.py"),
                   "--case", case, "--pages", str(args.pages), "--cycles", str(args.cycles),
                   "--media-groups", str(args.media_groups), "--state", str(run / "state"),
                   "--vault", str(run / "vault"), "--output", str(run / "result.json")]
        env = {key: value for key, value in os.environ.items() if not key.startswith("EXOMEM_")}
        env.update(PYTHONPATH=str(ROOT / "src"), XDG_STATE_HOME=str(run / "runner-xdg"))
        host = {"started_utc": dt.datetime.now(dt.UTC).isoformat(), "load_start": os.getloadavg(),
                "cpu_count": os.cpu_count(), "pair": pair, "case": case}
        print(f"START pair {pair} {case}", flush=True)
        started = time.perf_counter()
        with (run / "stdout.log").open("w") as out, (run / "stderr.log").open("w") as err:
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=out, stderr=err, start_new_session=True)
            try:
                code = process.wait(timeout=OUTER_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                host["outer_timeout"] = True
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                code = 124
        host.update(exit_code=code, elapsed_s=time.perf_counter() - started, load_end=os.getloadavg(),
                    ended_utc=dt.datetime.now(dt.UTC).isoformat())
        (run / "host.json").write_text(json.dumps(host, indent=2) + "\n", encoding="utf-8")
        result_path = run / "result.json"
        if not result_path.exists():
            result = {
                "status": "invalid",
                "errors": ["no final result after outer timeout; call intervals unavailable"],
            }
        else:
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                result = {
                    "status": "invalid",
                    "errors": [f"result unavailable: {type(error).__name__}: {error}"],
                }
        if not isinstance(result, dict):
            result = {"status": "invalid", "errors": ["result is not a JSON object"]}
        status = result.get("status")
        row = {"pair": pair, "case": case, "status": status, "elapsed_s": host["elapsed_s"],
               "errors": result.get("errors"), "foreground_ms": result.get("foreground", {}).get("finished_ms"),
               "graph_wait_ms": result.get("final_graph", {}).get("observed_wait_ms")}
        if not accepted_outcome(status, code):
            invalid = True
        manifest["runs"].append(row)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(row), flush=True)
    # A correctly observed product refusal is a failed benchmark observation,
    # not a failed collection job. Invalid/missing observations fail this job.
    manifest["collection_status"] = "invalid" if invalid else "complete"
    manifest["ended_utc"] = dt.datetime.now(dt.UTC).isoformat()
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return 1 if invalid else 0


if __name__ == "__main__":
    raise SystemExit(main())
