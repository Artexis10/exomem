"""Whether a method that activation served was then used, joined at owner readout from records the service keeps.

The served-context rows name each method a response served. The local read and write logs name what was read and
written afterwards. A later read of the method page, or of a file in its folder, is `opened`; a later successful write
to a page the method links to, or into a collection it links to, is `followed`. Activation does no work for this: the
join runs only when the owner asks. The logs belong to one host and service and carry no conversation identity, so
concurrent work by the same principal can produce the evidence, and the report says so.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import query_log, usage, vault

#: How long after a serving a read or a write still counts as use of it (method-use-evidence contract).
METHOD_USE_WINDOW_MINUTES = 120
#: At most this many retained rows are joined, newest first, so a readout stays bounded as rows grow.
MAX_JOINED_ROWS = 2000
OBSERVED, NOT_OBSERVED, UNKNOWN = "observed", "not_observed", "unknown"
MEANING = (
    "Use evidence from this host's read and write logs within "
    f"{METHOD_USE_WINDOW_MINUTES} minutes of each serving. The logs carry no conversation identity, so other work "
    "by the same principal in that window can produce it; not observed means the logs hold no matching event."
)


def _events(path: Path, field: str) -> tuple[list[tuple[float, str]], float | None]:
    """`(events, earliest)`: each successful event's UTC epoch and path, oldest first, and the earliest time logged."""
    events: list[tuple[float, str]] = []
    for row in usage.read_jsonl(path):
        if row.get("outcome", "success") != "success" or not row.get(field) or not row.get("ts_utc"):
            continue
        try:
            events.append((datetime.fromisoformat(str(row["ts_utc"])).timestamp(), str(row[field])))
        except ValueError:
            continue
    events.sort()
    return events, (events[0][0] if events else None)


def _destinations(root: Path, method_path: str, resolver_for: Callable[[], Any]) -> tuple[str, ...]:
    """The pages the method page links to; a link to a collection manifest stands for the collection's folder."""
    from . import capture_method, structured_collections

    try:
        text = (root / method_path).read_text(encoding="utf-8")
    except OSError:
        return ()
    _basename, rows = vault.inbound_rows(method_path, text)
    found: list[str] = []
    for _line, _context, target, _bucket in rows:
        path = capture_method.locate(root, target, resolver_for=resolver_for)
        if not path or path == method_path:
            continue
        if path.rpartition("/")[2] == structured_collections.MANIFEST_FILENAME:
            path = path.rpartition("/")[0] + "/"
        if path not in found:
            found.append(path)
    return tuple(found)


def _hit(events: Sequence[tuple[float, str]], start: float, targets: Iterable[str]) -> bool:
    end = start + METHOD_USE_WINDOW_MINUTES * 60
    wanted = tuple(targets)
    return any(
        start <= when <= end and any(path == target or (target.endswith("/") and path.startswith(target)) for target in wanted)
        for when, path in events
    )


def _state(events: Sequence[tuple[float, str]], earliest: float | None, start: float, targets: Sequence[str],
           *, logging_off: bool) -> str:
    if logging_off or not targets:
        return UNKNOWN
    if _hit(events, start, targets):
        return OBSERVED
    # A log that begins after the serving may have rotated the matching event away.
    if earliest is None or earliest > start:
        return UNKNOWN
    return NOT_OBSERVED


def report(root: Path, sidecar: Path, *, resolver_for: Callable[[], Any], disabled: bool | None = None) -> dict[str, Any]:
    """Per served method: servings, and how many were opened and followed, each observed, not observed or unknown."""
    from . import memory_refs

    logging_off = query_log._disabled() if disabled is None else disabled  # noqa: SLF001 - the logs' own switch
    log_dir = query_log.current_log_dir()
    reads, read_start = _events(log_dir / "reads.jsonl", "read_path")
    writes, write_start = _events(log_dir / "writes.jsonl", "written_path")
    with closing(sqlite3.connect(sidecar.as_uri() + "?mode=ro", uri=True, timeout=1.0)) as connection:
        rows = connection.execute(
            "SELECT timestamp, task_context FROM served_context "
            "WHERE json_array_length(task_context, '$.methods.items') > 0 ORDER BY timestamp DESC LIMIT ?",
            (MAX_JOINED_ROWS,),
        ).fetchall()
    methods: dict[str, dict[str, Any]] = {}
    places: dict[str, tuple[str | None, tuple[str, ...], tuple[str, ...]]] = {}
    for served_at, task_json in rows:
        for item in (json.loads(task_json).get("methods") or {}).get("items", ()):
            reference = str(item.get("read_ref") or item.get("source_ref") or "")
            if not reference:
                continue
            if reference not in places:
                try:
                    path = memory_refs.resolve_identifier_read_only(root, reference.split("#", 1)[0])
                except (OSError, ValueError):
                    path = None
                if path and not (root / path).is_file():
                    path = None
                folder = path.rpartition("/")[0] + "/" if path and path.endswith("/SKILL.md") else None
                opened_targets = tuple(target for target in (path, folder) if target)
                places[reference] = (path, opened_targets, _destinations(root, path, resolver_for) if path else ())
            path, opened_targets, destinations = places[reference]
            entry = methods.setdefault(reference, {
                "ref": reference, "path": path, "destinations": list(destinations), "served": 0,
                "opened": {OBSERVED: 0, NOT_OBSERVED: 0, UNKNOWN: 0},
                "followed": {OBSERVED: 0, NOT_OBSERVED: 0, UNKNOWN: 0},
            })
            entry["served"] += 1
            entry["opened"][_state(reads, read_start, float(served_at), opened_targets, logging_off=logging_off)] += 1
            entry["followed"][_state(writes, write_start, float(served_at), destinations, logging_off=logging_off)] += 1
    return {
        "state": "ready", "window_minutes": METHOD_USE_WINDOW_MINUTES, "rows_joined": len(rows),
        "rows_capped": len(rows) == MAX_JOINED_ROWS, "logging": "off" if logging_off else "on",
        "methods": sorted(methods.values(), key=lambda entry: (-entry["served"], entry["ref"])),
        "meaning": MEANING,
    }


def summary_line(entry: Mapping[str, Any]) -> str:
    """One method's evidence in words a person can repeat."""
    opened, followed = entry["opened"], entry["followed"]
    return (
        f"{entry['ref']}: served {entry['served']}; opened {opened[OBSERVED]}, not {opened[NOT_OBSERVED]}, "
        f"unknown {opened[UNKNOWN]}; followed {followed[OBSERVED]}, not {followed[NOT_OBSERVED]}, "
        f"unknown {followed[UNKNOWN]}"
    )
