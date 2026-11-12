"""Record each arm's replies from its own code, so two arms differ only where the products differ.

An arm is a checkout. `record_worker.py` runs inside that checkout's environment, makes the scenario's real tool
calls (the product's terminal builder, its refusal envelope and its bootstrap) on a throwaway fixture vault, or on a
seeded vault when the scenario names a seed folder, and the scripted server then serves what came back. A reply copied by hand into the scenario would be the same
for both arms by construction. The recording is made once per arm and scenario and then reused, so the ids it
holds stay fixed for every run of that arm and the answer checks can look for them. It is reused only while the arm's
checkout is in the state it was recorded from (`checkout_sha`); otherwise the runner stops and says to delete it.
"""

from __future__ import annotations

import hashlib
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


#: The checkout folders a worker imports or installs from: the package and its scaffold under `src`, the client hooks
#: under `plugins`.
PRODUCT_FOLDERS = ("src", "plugins")


def parse_arm(text: str) -> Arm:
    """`label=path`, or a path alone (its folder name is the label)."""
    label, sep, path = text.partition("=")
    root = Path(path if sep else text).expanduser().resolve()
    return Arm(label if sep else root.name, root)


def checkout_sha(root: Path) -> str:
    """The checkout's HEAD, with `-dirty-` and the first 12 hex digits of a sha256 when the product differs from HEAD,
    or "unknown". The hash covers the diff of tracked files against HEAD, and the names and bytes of untracked,
    unignored files under the folders the worker runs from (`PRODUCT_FOLDERS`): a new scaffold or registry file there
    changes what a seeded vault holds. Untracked files elsewhere do not count: a run folder kept inside the checkout
    would otherwise change the state between two recordings."""
    head = subprocess.run(  # noqa: S603,S607
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True
    )
    if head.returncode != 0 or not head.stdout.strip():
        return "unknown"
    diff = subprocess.run(  # noqa: S603,S607
        ["git", "-C", str(root), "diff", "HEAD", "--binary", "--no-color", "--no-ext-diff", "--no-textconv"], capture_output=True
    )
    untracked = subprocess.run(  # noqa: S603,S607
        ["git", "-C", str(root), "ls-files", "-z", "--others", "--exclude-standard", "--", *PRODUCT_FOLDERS], capture_output=True
    )
    if diff.returncode != 0 or untracked.returncode != 0:
        return "unknown"
    digest = hashlib.sha256(diff.stdout)
    for name in sorted(filter(None, untracked.stdout.split(b"\0"))):
        path = root / name.decode("utf-8", "surrogateescape")
        if not path.is_file():
            continue
        digest.update(b"\0untracked\0" + name + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    changed = bool(diff.stdout) or any(untracked.stdout.split(b"\0"))
    return head.stdout.strip() + (f"-dirty-{digest.hexdigest()[:12]}" if changed else "")


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


def seed_folder(scenario: Scenario) -> Path | None:
    """The folder the scenario's `record.seed` names, relative to the scenario file; None when it names none."""
    named = scenario.record.get("seed")
    if named is None:
        return None
    return (scenario.path.parent / named if scenario.path else Path(named)).resolve()


def record(scenario: Scenario, arm: Arm, target: Path, scratch: Path) -> list[str]:
    """Write the arm's recording of one scenario to `target` unless it exists. Returns the problems found."""
    if target.exists():
        kept = load_recording(target)
        if scenario.record.get("ready_graph") and "ready" not in kept:
            # A recording made before the graph was built holds the graph warming, so it would measure nothing.
            return [f"{scenario.id}: {target} was recorded without building the graph; delete it to record again"]
        now = checkout_sha(arm.root)
        if kept.get("product_sha") != now:
            # Its replies came from other code than the skill, tool text and hooks the agent would now be shown.
            return [
                f"{scenario.id}: {target} was recorded from {arm.label} at {kept.get('product_sha') or 'an unrecorded state'}, "
                f"and the checkout is now at {now}; delete it to record again"
            ]
        return []
    if not scenario.record.get("steps"):
        return [f"{scenario.id} has no record steps"]
    seed = seed_folder(scenario)
    if seed is not None and not seed.is_dir():
        return [f"{scenario.id}: the seed {seed} is not a folder"]
    scratch.mkdir(parents=True, exist_ok=True)
    plan = scratch / f"{scenario.id}.plan.json"
    plan.write_text(
        json.dumps({**scenario.record, **({"engagement": scenario.engagement} if scenario.engagement else {})}),
        encoding="utf-8",
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".partial")
    try:
        done = subprocess.run(  # noqa: S603
            [sys.executable, str(WORKER), str(plan), str(arm.root), str(partial), *([str(seed)] if seed else [])],
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


def served_sizes(target: Path) -> dict[str, int]:
    """The characters of each recorded reply as the scripted server serves it: the reply as indented JSON."""
    return {name: len(json.dumps(shape, indent=2)) for name, shape in load_recording(target).get("shapes", {}).items()}


def graph_states(target: Path) -> dict[str, str]:
    """What the relation graph is reported as in each recorded reply that has a map block, by shape name."""
    shapes = load_recording(target).get("shapes", {})
    return {
        name: str(shape["map"].get("membership_graph", "not reported"))
        for name, shape in shapes.items()
        if isinstance(shape, dict) and isinstance(shape.get("map"), dict)
    }
