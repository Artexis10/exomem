#!/usr/bin/env python3
"""Fail when an active OpenSpec change has no unchecked task remaining."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_TASK = re.compile(r"^\s*-\s+\[([ xX])\]\s+\S")
_CHECKBOX_LIKE = re.compile(r"^\s*-\s+\[[^]]*\]")
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")


def _task_content(lines: Sequence[str]) -> list[str]:
    """Return Markdown lines outside fenced code blocks."""
    content: list[str] = []
    fence: tuple[str, int] | None = None
    for line in lines:
        marker = _FENCE.match(line)
        if fence is not None:
            if marker:
                run, suffix = marker.groups()
                if run[0] == fence[0] and len(run) >= fence[1] and not suffix.strip():
                    fence = None
            continue
        if marker:
            run, suffix = marker.groups()
            if run[0] == "~" or "`" not in suffix:
                fence = (run[0], len(run))
                continue
        content.append(line)
    return content


def audit(root: Path) -> dict[str, Any]:
    """Classify direct active change directories without mutating them."""
    changes_dir = root / "openspec" / "changes"
    active = sorted(
        path
        for path in changes_dir.iterdir()
        if path.is_dir() and path.name != "archive"
    )
    complete: list[str] = []
    unchecked: list[str] = []
    unclassified: list[dict[str, str]] = []

    for change in active:
        tasks = change / "tasks.md"
        if not tasks.is_file():
            unclassified.append({"change": change.name, "reason": "missing_tasks"})
            continue

        lines = _task_content(tasks.read_text(encoding="utf-8").splitlines())
        parsed = [match for line in lines if (match := _TASK.match(line))]
        malformed = any(_CHECKBOX_LIKE.match(line) and not _TASK.match(line) for line in lines)
        if malformed:
            unclassified.append({"change": change.name, "reason": "malformed_checkbox"})
        elif not parsed:
            unclassified.append({"change": change.name, "reason": "no_task_checkboxes"})
        elif any(match.group(1) == " " for match in parsed):
            unchecked.append(change.name)
        else:
            complete.append(change.name)

    return {
        "active_change_count": len(active),
        "complete_active_changes": complete,
        "status": "archive_debt" if complete else "ok",
        "unchecked_active_changes": unchecked,
        "unclassified_active_changes": unclassified,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repository root containing openspec/changes (default: current directory)",
    )
    parser.add_argument("--json", action="store_true", help="emit a stable JSON result")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = audit(args.root.resolve())
    complete = result["complete_active_changes"]

    if args.json:
        print(json.dumps(result, sort_keys=True))
    elif complete:
        print("OpenSpec archive debt: task-complete active changes must be archived:")
        for change in complete:
            print(f"- {change}: openspec archive {change}")
    else:
        print(
            "OpenSpec archive discipline: OK "
            f"({result['active_change_count']} active changes; none task-complete)."
        )

    return 1 if complete else 0


if __name__ == "__main__":
    raise SystemExit(main())
