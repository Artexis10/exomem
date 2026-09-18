#!/usr/bin/env python
"""Local-only real-turn acceptance check for `activate_context`.

For ``openspec/changes/make-anchor-resolution-sound`` task 7/9: a private list
of turns, run one at a time through the real product surface
(``commands.op_activate_context``) against a real vault snapshot, in one
resident process so the recall and activation indexes warm once and stay
warm for every turn after the first. Prints each turn's status, its anchors
(kind, title, status, evidence) and, where the turns file gives an expected
shape, a pass/fail line -- the acceptance evidence for this change, kept in
the owner's knowledge base, never in this repository.

Usage::

    python scripts/activation_real_turns.py --snapshot /path/outside/any/repo \\
        --turns /path/outside/any/repo/turns.json [--json]

Both ``--snapshot`` and ``--turns`` are refused outright if they resolve
inside any git checkout (reusing ``private_vault_snapshot``'s guard), so a
run can never read this repository as if it were the private vault, and
never write state into it either.

Turns file: a JSON list of objects, each ``{"turn": "...", "expect":
["Knowledge Base/...md", ...]}`` (every listed path must be among the
packet's `resolved` OR `partial` anchor paths) or ``{"turn": "...",
"expect_abstain": true}`` (the packet must abstain). A turn with neither key
is run and reported with no pass/fail judgement.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from private_vault_snapshot import (  # noqa: E402
    SnapshotError,
    refuse_unsafe_destination,
)

#: EXOMEM_* env vars this script keeps from the inherited environment; every
#: other one is stripped before the first turn runs (module docstring).
_KEPT_EXOMEM_ENV = frozenset({"EXOMEM_DISABLE_EMBEDDINGS"})


class TurnsFileError(ValueError):
    """Raised for a malformed turns file."""


def refuse_path_inside_a_repository(path: Path, *, label: str) -> Path:
    """Refuse ``path`` if it resolves inside any git checkout (N4 guard,
    reused from :mod:`private_vault_snapshot`, worded for this script's own
    ``--snapshot``/``--turns`` arguments rather than a copy destination.
    """
    try:
        return refuse_unsafe_destination(path)
    except SnapshotError as exc:
        raise SnapshotError(f"{label} {path} sits inside a git checkout: {exc}") from exc


def prepare_environment(snapshot: Path, *, state_dir: str | None) -> None:
    """Strip inherited `EXOMEM_*` env vars except the kept set, set
    `EXOMEM_VAULT_PATH` to the snapshot, and set `XDG_STATE_HOME` under the
    snapshot's parent UNLESS `--state-dir` was given explicitly.

    Never honours an inherited `XDG_STATE_HOME` (review round 3, MINOR 8): on
    a machine that exports one, this script would otherwise write the
    snapshot's sidecars under that LIVE state root instead of a scratch
    directory next to the snapshot — exactly the shared-live-state boundary
    this script exists to stay outside of.
    """
    for key in [k for k in os.environ if k.startswith("EXOMEM_") and k not in _KEPT_EXOMEM_ENV]:
        del os.environ[key]
    os.environ["EXOMEM_VAULT_PATH"] = str(snapshot)
    os.environ["XDG_STATE_HOME"] = (
        state_dir if state_dir else str(snapshot.parent / f"{snapshot.name}-activation-state")
    )


def load_turns(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TurnsFileError(f"cannot read turns file {path}: {exc}") from exc
    if not isinstance(data, list):
        raise TurnsFileError(f"turns file {path} must be a JSON list")
    turns: list[dict[str, Any]] = []
    for index, entry in enumerate(data):
        if not isinstance(entry, dict) or not str(entry.get("turn", "")).strip():
            raise TurnsFileError(f"turns file {path}: entry {index} missing a non-empty 'turn'")
        turns.append(entry)
    return turns


def _anchor_paths(packet: dict[str, Any], *, statuses: frozenset[str]) -> set[str]:
    return {
        str(anchor.get("path") or anchor.get("ref") or "")
        for anchor in packet.get("anchors", ()) or ()
        if anchor.get("status") in statuses
    }


def run_turn(vault_root: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """Run one turn through the real product surface and judge it against
    its own `expect`/`expect_abstain`, if either is given.
    """
    from exomem import commands

    turn = str(entry["turn"])
    packet = commands.op_activate_context(vault_root, turn=turn)
    anchors = [
        {
            "path": anchor.get("path") or anchor.get("ref"),
            "title": anchor.get("title"),
            "kind": anchor.get("kind"),
            "status": anchor.get("status"),
            "evidence": list(anchor.get("evidence") or ()),
        }
        for anchor in packet.get("anchors", ()) or ()
    ]
    abstained = bool(packet.get("abstained"))
    # Checked BEFORE `abstained` (review round 3, MINOR 9): the packet's own
    # `abstained` field is true for BOTH `unresolved` and `ambiguous` (neither
    # is `resolved`), so branching on `abstained` first made the `ambiguous`
    # case unreachable — every ambiguous packet reported "unresolved" instead.
    if packet.get("ambiguity"):
        status = "ambiguous"
    elif abstained:
        status = "unresolved"
    else:
        status = "resolved"
    result: dict[str, Any] = {
        "turn": turn,
        "status": status,
        "abstained": abstained,
        "abstention_reason": (packet.get("abstention") or {}).get("reason") if abstained else None,
        "anchors": anchors,
    }

    expect_abstain = entry.get("expect_abstain")
    expect = entry.get("expect")
    if expect_abstain is True:
        result["judged"] = True
        result["passed"] = abstained
        result["expectation"] = "abstain"
    elif isinstance(expect, list) and expect:
        reached = _anchor_paths(packet, statuses=frozenset({"resolved", "partial"}))
        missing = [path for path in expect if path not in reached]
        result["judged"] = True
        result["passed"] = not missing
        result["expectation"] = list(expect)
        result["missing"] = missing
    else:
        result["judged"] = False
        result["passed"] = None
        result["expectation"] = None
    return result


def run_all(vault_root: Path, turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [run_turn(vault_root, entry) for entry in turns]


def _print_report(results: list[dict[str, Any]]) -> None:
    for result in results:
        print(f"turn: {result['turn']!r}")
        print(f"  status: {result['status']}" + (f" ({result['abstention_reason']})" if result["abstention_reason"] else ""))
        for anchor in result["anchors"]:
            print(
                f"    anchor kind={anchor['kind']} title={anchor['title']!r} "
                f"status={anchor['status']} evidence={anchor['evidence']}"
            )
        if not result["anchors"]:
            print("    (no anchors)")
        if result["judged"]:
            verdict = "PASS" if result["passed"] else "FAIL"
            detail = f" missing={result['missing']}" if result.get("missing") else ""
            print(f"  [{verdict}] expected {result['expectation']!r}{detail}")
        print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True, help="Vault snapshot root (read-only).")
    parser.add_argument("--turns", type=Path, required=True, help="Path to the private turns JSON file.")
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON instead of text.")
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="Override XDG_STATE_HOME (default: a fresh dir under the snapshot's parent).",
    )
    args = parser.parse_args(argv)

    try:
        snapshot = refuse_path_inside_a_repository(args.snapshot, label="--snapshot")
        turns_path = refuse_path_inside_a_repository(args.turns, label="--turns")
    except SnapshotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not snapshot.is_dir():
        print(f"error: --snapshot {snapshot} is not a directory", file=sys.stderr)
        return 2

    try:
        turns = load_turns(turns_path)
    except TurnsFileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    prepare_environment(snapshot, state_dir=str(args.state_dir) if args.state_dir else None)

    results = run_all(snapshot, turns)

    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        _print_report(results)

    judged = [r for r in results if r["judged"]]
    failed = [r for r in judged if not r["passed"]]
    if failed:
        print(f"{len(failed)}/{len(judged)} judged turn(s) failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
