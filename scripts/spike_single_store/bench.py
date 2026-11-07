"""Run the whole single-store comparison on one machine and filesystem.

Usage: bench.py <work_root> <out_dir> [--shape F] [--pages N] [--ops M]
                [--kills-files K] [--kills-single K]
With --shape, a synthetic vault is generated under <work_root>; without it,
<work_root>/live/vault must already hold a vault copy. Every step's output
lands in <out_dir>; bench-steps.jsonl records each step's wall time.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import spike_env

HERE = Path(__file__).resolve().parent


def step(log, name: str, module: str, *args: str, run_root: Path, timeout: float = 7200,
         env_root: Path | None = None) -> str:
    started = time.perf_counter()
    proc = spike_env.run_child(env_root or run_root, module, *args, timeout=timeout,
                               capture_output=True, text=True)
    record = {"step": name, "rc": proc.returncode, "wall_s": round(time.perf_counter() - started, 1),
              "stdout_tail": proc.stdout.strip().splitlines()[-1][:2000] if proc.stdout.strip() else "",
              "stderr_tail": proc.stderr[-1500:] if proc.returncode else ""}
    log.write(json.dumps(record) + "\n")
    log.flush()
    print(json.dumps(record)[:600], flush=True)
    if proc.returncode != 0:
        raise SystemExit(f"step {name} failed")
    return proc.stdout


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("work_root", type=Path)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--shape", type=Path)
    ap.add_argument("--pages", type=int, default=0)
    ap.add_argument("--ops", type=int, default=100)
    ap.add_argument("--kills-files", type=int, default=6)
    ap.add_argument("--kills-single", type=int, default=10)
    args = ap.parse_args()
    root, out = args.work_root.resolve(), args.out_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    paths = spike_env.layout(root)
    log = (out / "bench-steps.jsonl").open("a", encoding="utf-8")
    if args.shape:
        if root.exists():
            shutil.rmtree(root)
        for key in ("state", "xdg"):
            paths[key].mkdir(parents=True, exist_ok=True)
        step(log, "synth_vault", "synth_vault", str(args.shape), str(paths["vault"]),
             "--pages", str(args.pages), run_root=root)
    step(log, "build_base", "build_base", str(root), run_root=root)
    spike_env.snapshot(root)
    step(log, "make_stream", "make_stream", str(paths["vault"]), str(out / "stream.jsonl"),
         str(args.ops), run_root=root)
    spike_env.restore(root)
    step(log, "arm_files", "arm_files", "run", str(root), str(out / "stream.jsonl"),
         str(out / "arm-a"), "--rate-per-min", "0", run_root=root)
    spike_env.snapshot(root, "final-a")
    spike_env.restore(root)
    step(log, "migrate", "arm_single", "migrate", str(root), run_root=root)
    spike_env.snapshot(root, "base-single")
    step(log, "arm_single", "arm_single", "run", str(root), str(out / "arm-a" / "replay.jsonl"),
         str(out / "arm-b"), run_root=root)
    spike_env.snapshot(root, "final-b")
    sa = next((root / "final-a" / "state").glob("vault-*"))
    sb = next((root / "final-b" / "state").glob("vault-*"))
    diff = subprocess.run([sys.executable, str(HERE / "compare_tables.py"), str(sa / ".lexical.sqlite"),
                           str(sa / ".graph.sqlite"), str(sb / "single-store.sqlite"),
                           str(out / "arm-a" / "replay.jsonl")], capture_output=True, text=True)
    (out / "compare_tables.json").write_text(diff.stdout)
    for arm, count, source, delay in (
            ("single", args.kills_single, out / "arm-a" / "replay.jsonl", 3.0),
            ("files", args.kills_files, out / "stream.jsonl", 60.0)):
        if count:
            started = time.perf_counter()
            subprocess.run([sys.executable, str(HERE / "kill_trials.py"), str(root), arm, str(source),
                            str(out / f"kill-{arm}"), "--trials", str(count), "--max-delay", str(delay)],
                           env=spike_env.exomem_env(root), cwd=str(root), timeout=4 * 3600)
            log.write(json.dumps({"step": f"kill_{arm}", "wall_s":
                                  round(time.perf_counter() - started, 1)}) + "\n")
            log.flush()
    log.close()


if __name__ == "__main__":
    main()
