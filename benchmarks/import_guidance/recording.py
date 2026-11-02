"""Record each arm's replies from its own code, so two arms differ only where the products differ.

An arm is a checkout. `record_worker.py` runs inside that checkout's environment, makes the scenario's real tool
calls (the product's terminal builder, its refusal envelope and its bootstrap) on a throwaway fixture vault,
and the scripted server then serves what came back. A reply copied by hand into the scenario would be the same
for both arms by construction. The recording is made once per arm and scenario and then reused, so the ids it
holds stay fixed for every run of that arm and the answer checks can look for them.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .scripted_replies import Scenario

WORKER = Path(__file__).resolve().parent / "record_worker.py"
RECORD_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class Arm:
    label: str
    root: Path


def parse_arm(text: str) -> Arm:
    """`label=path`, or a path alone (its folder name is the label)."""
    label, sep, path = text.partition("=")
    root = Path(path if sep else text).expanduser().resolve()
    return Arm(label if sep else root.name, root)


def checkout_sha(root: Path) -> str:
    done = subprocess.run(  # noqa: S603,S607
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True
    )
    return done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else "unknown"


def worker_env(arm: Arm, scratch: Path) -> dict[str, str]:
    """The worker imports the arm's `exomem` first, and reaches no real state: every state, home and temp folder
    is under `scratch`, and no `EXOMEM_*` variable passes through."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("EXOMEM_", "PYTHON"))}
    home = scratch / "home"
    home.mkdir(parents=True, exist_ok=True)
    return env | {
        "PYTHONPATH": str(arm.root / "src"),
        "HOME": str(home),
        "TMPDIR": str(scratch),
        "XDG_STATE_HOME": str(home / "state"),
        "XDG_DATA_HOME": str(home / "share"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_CACHE_HOME": str(home / "cache"),
    }


def record(scenario: Scenario, arm: Arm, target: Path, scratch: Path) -> list[str]:
    """Write the arm's recording of one scenario to `target` unless it exists. Returns the problems found."""
    if target.exists():
        return []
    if not scenario.record.get("steps"):
        return [f"{scenario.id} has no record steps"]
    scratch.mkdir(parents=True, exist_ok=True)
    plan = scratch / f"{scenario.id}.plan.json"
    plan.write_text(json.dumps(scenario.record), encoding="utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".partial")
    try:
        done = subprocess.run(  # noqa: S603
            [sys.executable, str(WORKER), str(plan), str(arm.root), str(partial)],
            cwd=scratch,
            env=worker_env(arm, scratch),
            capture_output=True,
            text=True,
            timeout=RECORD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return [f"{arm.label}: recording {scenario.id} timed out"]
    finally:
        # The worker's throwaway vault and state, left for us because a rebuild still running at its exit would
        # race a removal made from inside.
        for leftover in scratch.glob("ig-record-*"):
            shutil.rmtree(leftover, ignore_errors=True)
    if done.returncode != 0 or not partial.exists():
        tail = (done.stderr or done.stdout).strip().splitlines()[-3:]
        return [f"{arm.label}: recording {scenario.id} failed: {' | '.join(tail)}"]
    recorded = json.loads(partial.read_text(encoding="utf-8"))
    recorded["arm"] = arm.label
    recorded["product_sha"] = checkout_sha(arm.root)
    target.write_text(json.dumps(recorded, indent=1), encoding="utf-8")
    partial.unlink()
    return []


def load_recording(target: Path) -> dict:
    return json.loads(target.read_text(encoding="utf-8"))
