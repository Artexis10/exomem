#!/usr/bin/env python
"""Wheel-only time-to-value gate (OpenSpec: add-one-command-onboarding).

Proves the onboarding path a NEW user takes works from the built wheel alone,
and measures it:

1. `uv build`                          — build the wheel from this checkout
2. fresh venv + install ONLY the wheel — no repo on sys.path
3. `exomem demo --json` from a scratch cwd
   — proves the sample vault ships in the wheel and demo has no repo dependency
4. `exomem setup --vault <tmp> --yes --skip-claude-register`
   — proves the wizard completes from a wheel install

Runs with a scrubbed environment (no EXOMEM_*/KB_MCP_* leakage from a dev
shell) and a scratch HOME (the wizard's skill step must never touch the real
`~/.claude`). Prints a per-step timing breakdown; exits 1 on any failure or
when the total exceeds `--budget-seconds` (the CI wall-time gate; the human
≤5-minute budget is two commands + one paste, which this flow bounds).

Usage:  uv run python scripts/time-to-value.py [--budget-seconds 120]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WINDOWS = sys.platform == "win32"


def _scrubbed_env(home: Path) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("EXOMEM_", "KB_MCP_"))
    }
    env["HOME"] = str(home)
    if WINDOWS:
        env["USERPROFILE"] = str(home)
    # Child pythons on Windows default stdio to the legacy code page when
    # piped; force UTF-8 mode so their output decodes consistently here.
    env["PYTHONUTF8"] = "1"
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--budget-seconds", type=float, default=120.0,
        help="hard wall-time budget for the whole flow (default: 120)",
    )
    args = parser.parse_args()

    timings: list[tuple[str, float]] = []
    t_total = time.perf_counter()

    def step(name: str, fn) -> object:
        t0 = time.perf_counter()
        result = fn()
        seconds = time.perf_counter() - t0
        timings.append((name, seconds))
        print(f"  {name}: {seconds:.1f}s")
        return result

    with tempfile.TemporaryDirectory(prefix="exomem-ttv-") as tmp_str:
        tmp = Path(tmp_str)
        home = tmp / "home"
        home.mkdir()
        work = tmp / "work"
        work.mkdir()
        env = _scrubbed_env(home)

        print("time-to-value: wheel-only onboarding gate")

        def _run_or_raise(label: str, cmd: list[str], **kwargs) -> None:
            proc = subprocess.run(
                cmd, env=env, capture_output=True, text=True, encoding="utf-8", **kwargs
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"{label} exited {proc.returncode}:\n{proc.stdout}\n{proc.stderr}"
                )

        def _build() -> Path:
            _run_or_raise("uv build", ["uv", "build", "--out-dir", str(tmp / "dist")], cwd=REPO)
            wheels = sorted((tmp / "dist").glob("exomem-*.whl"))
            if not wheels:
                raise RuntimeError("uv build produced no wheel")
            return wheels[-1]

        wheel = step("uv build", _build)

        venv = tmp / "venv"
        venv_bin = venv / ("Scripts" if WINDOWS else "bin")
        venv_python = venv_bin / ("python.exe" if WINDOWS else "python")
        exomem_cli = venv_bin / ("exomem.exe" if WINDOWS else "exomem")

        def _venv() -> None:
            _run_or_raise("uv venv", ["uv", "venv", str(venv)])
            _run_or_raise(
                "uv pip install",
                ["uv", "pip", "install", "--python", str(venv_python), str(wheel)],
            )

        step("fresh venv + wheel install", _venv)

        def _demo() -> None:
            proc = subprocess.run(
                [str(exomem_cli), "demo", "--json"],
                cwd=work, env=env, capture_output=True, text=True, encoding="utf-8",
            )
            if proc.returncode != 0:
                raise RuntimeError(f"demo exited {proc.returncode}: {proc.stderr.strip()}")
            envelope = json.loads(proc.stdout.strip().splitlines()[-1])
            if not envelope.get("success"):
                raise RuntimeError(f"demo reported failure: {envelope}")
            names = [s["name"] for s in envelope["steps"]]
            if names != ["doctor", "find", "get", "audit"]:
                raise RuntimeError(f"unexpected demo steps: {names}")

        step("exomem demo --json (wheel, scratch cwd)", _demo)

        def _setup() -> None:
            vault = tmp / "vault"
            proc = subprocess.run(
                [
                    str(exomem_cli), "setup",
                    "--vault", str(vault), "--yes", "--skip-claude-register",
                ],
                cwd=work, env=env, capture_output=True, text=True, encoding="utf-8",
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"setup exited {proc.returncode}:\n{proc.stdout}\n{proc.stderr}"
                )
            if not (vault / "Knowledge Base" / "_Schema" / "SKILL.md").is_file():
                raise RuntimeError("setup did not initialize the Knowledge Base scaffold")

        step("exomem setup --yes (wheel)", _setup)

    total = time.perf_counter() - t_total
    print(f"total: {total:.1f}s (budget {args.budget_seconds:.0f}s)")
    if total > args.budget_seconds:
        print("time-to-value: FAIL — budget exceeded", file=sys.stderr)
        return 1
    print("time-to-value: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
