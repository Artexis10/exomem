"""Kill today's path between its Markdown commit and its derived fan-out.

Usage: kill_at_commit.py <run_root> <stream.jsonl> <out_dir> --trials N
Each trial restores the base, runs the first stream op with the fan-out
replaced by SIGKILL, then runs the recovery check (startup recovery, then a
reader's first search). The run root needs base-drift.json from a baseline.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import spike_env


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_root", type=Path)
    ap.add_argument("stream", type=Path)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--trials", type=int, default=3)
    args = ap.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    stream = [json.loads(line) for line in args.stream.open(encoding="utf-8")]
    with (args.out_dir / "trials.jsonl").open("a", encoding="utf-8") as log:
        for trial in range(args.trials):
            spike_env.restore(args.run_root)
            one = args.out_dir / f"stream-{trial:03d}.jsonl"
            one.write_text(json.dumps(stream[trial]) + "\n", encoding="utf-8")
            os.environ["SPIKE_KILL_BEFORE_FANOUT"] = "1"
            run = spike_env.run_child(args.run_root, "arm_files", "run", str(args.run_root), str(one),
                                      str(args.out_dir / f"run-{trial:03d}"), "--rate-per-min", "0",
                                      capture_output=True, text=True, timeout=1800)
            del os.environ["SPIKE_KILL_BEFORE_FANOUT"]
            check_out = args.out_dir / f"check-{trial:03d}.json"
            spike_env.run_child(args.run_root, "arm_files", "check", str(args.run_root), str(check_out),
                                "--recover-seconds", "330", timeout=1800)
            check = json.loads(check_out.read_text())
            row = {"trial": trial, "op": stream[trial]["op"], "run_rc": run.returncode,
                   "before": check["before"], "after": check["after"],
                   "after_first_read": check.get("after_first_read"), "torn": len(check["torn"])}
            log.write(json.dumps(row) + "\n")
            log.flush()
            print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main()
