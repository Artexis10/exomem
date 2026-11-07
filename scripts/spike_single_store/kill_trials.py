"""Hard-kill trials: kill an arm at a random moment, then check what needs repair.

Usage: kill_trials.py <run_root> <files|single> <input.jsonl> <out_dir>
                      --trials N [--max-delay S] [--seed K]
For `files` the input is the op stream; for `single` it is arm A's replay.
Each trial restores the arm's base, waits for the arm's ready line, kills the
process (SIGKILL; TerminateProcess on Windows) after a uniform random delay,
then runs the arm's `check`. One JSON line per trial goes to trials.jsonl.
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

import spike_env

ARMS = {
    "files": ("arm_files", "base", lambda a: ["run", str(a.run_root), str(a.input),
                                              str(a.out_dir / "victim"), "--rate-per-min", "0"]),
    "single": ("arm_single", "base-single", lambda a: ["run", str(a.run_root), str(a.input),
                                                       str(a.out_dir / "victim")]),
}


def trial(args, index: int, rng: random.Random) -> dict:
    module, base, argv = ARMS[args.arm]
    spike_env.restore(args.run_root, base)
    script = Path(spike_env.__file__).resolve().parent / f"{module}.py"
    proc = subprocess.Popen([sys.executable, str(script), *argv(args)],
                            env=spike_env.exomem_env(args.run_root), cwd=str(args.run_root),
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    lines: list[str] = []
    ready = threading.Event()

    def reader():
        for line in proc.stdout:
            lines.append(line)
            if '"event": "ready"' in line:
                ready.set()

    threading.Thread(target=reader, daemon=True).start()
    if not ready.wait(600):
        proc.kill()
        return {"trial": index, "error": "arm never became ready"}
    delay = rng.uniform(0.0, args.max_delay)
    time.sleep(delay)
    finished_before_kill = proc.poll() is not None
    proc.kill()
    proc.wait()
    ops_acked = sum('"event": "op"' in line for line in lines)
    out = args.out_dir / f"check-{index:03d}.json"
    check = subprocess.run([sys.executable, str(script), "check", str(args.run_root), str(out)],
                           env=spike_env.exomem_env(args.run_root), cwd=str(args.run_root),
                           capture_output=True, text=True, timeout=1200)
    summary = check.stdout.strip().splitlines()[-1] if check.stdout.strip() else None
    return {"trial": index, "delay_s": round(delay, 3), "ops_acked_before_kill": ops_acked,
            "finished_before_kill": finished_before_kill, "check_rc": check.returncode,
            "summary": json.loads(summary) if summary and summary.startswith("{") else summary,
            "check_stderr": check.stderr[-400:] if check.returncode else ""}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_root", type=Path)
    ap.add_argument("arm", choices=sorted(ARMS))
    ap.add_argument("input", type=Path)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--trials", type=int, default=20)
    ap.add_argument("--max-delay", type=float, default=60.0)
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    if args.arm == "files" and not (args.run_root / "base-drift.json").exists():
        spike_env.restore(args.run_root, "base")
        script = Path(spike_env.__file__).resolve().parent / "arm_files.py"
        subprocess.run([sys.executable, str(script), "baseline", str(args.run_root),
                        str(args.run_root / "base-drift.json")],
                       env=spike_env.exomem_env(args.run_root), cwd=str(args.run_root),
                       check=True, timeout=1200)
    with (args.out_dir / "trials.jsonl").open("a", encoding="utf-8") as fh:
        for i in range(args.trials):
            result = trial(args, i, rng)
            fh.write(json.dumps(result) + "\n")
            fh.flush()
            print(json.dumps(result)[:400], flush=True)


if __name__ == "__main__":
    main()
