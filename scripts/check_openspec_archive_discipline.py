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
_LIST_ITEM = re.compile(
    r"^(?P<indent>[ \t]*)(?P<marker>[-+*]|\d{1,9}[.)])(?P<gap>[ \t]+)(?=\S)"
)


def _columns(value: str) -> int:
    return len(value.expandtabs(4))


def _indent(line: str) -> int:
    return _columns(line[: len(line) - len(line.lstrip(" \t"))])


def _update_list_contexts(line: str, contexts: list[tuple[int, int]]) -> None:
    if not line.strip():
        return
    item = _LIST_ITEM.match(line)
    if item:
        marker_indent = _columns(item.group("indent"))
        content_indent = _columns(
            item.group("indent") + item.group("marker") + item.group("gap")
        )
        while contexts and marker_indent < contexts[-1][1]:
            contexts.pop()
        contexts.append((marker_indent, content_indent))
        return
    line_indent = _indent(line)
    while contexts and line_indent < contexts[-1][1]:
        contexts.pop()


def _task_content(lines: Sequence[str]) -> list[str]:
    """Return Markdown lines outside fenced code blocks."""
    content: list[str] = []
    contexts: list[tuple[int, int]] = []
    fence: tuple[str, int, int] | None = None
    for line in lines:
        marker = _FENCE.match(line)
        if fence is not None:
            line_indent = _indent(line)
            if line.strip() and fence[2] and line_indent < fence[2]:
                fence = None
            elif marker:
                run, suffix = marker.groups()
                relative_indent = line_indent - fence[2]
                if (
                    0 <= relative_indent <= 3
                    and run[0] == fence[0]
                    and len(run) >= fence[1]
                    and not suffix.strip()
                ):
                    fence = None
                continue
            else:
                continue
        _update_list_contexts(line, contexts)
        if marker:
            run, suffix = marker.groups()
            line_indent = _indent(line)
            bases = [0, *(content_indent for _, content_indent in contexts)]
            valid_bases = [base for base in bases if 0 <= line_indent - base <= 3]
            if valid_bases and (run[0] == "~" or "`" not in suffix):
                fence = (run[0], len(run), max(valid_bases))
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
