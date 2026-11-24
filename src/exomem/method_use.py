"""Whether a method that activation served was then used, joined at owner readout from records the service keeps.

The served-context rows name each method a response served. The local read and write logs name what was read and
written afterwards. A later read of the method page, or of a file in its folder, is `opened`; a later successful write
to a page the method links to, or into a collection it links to, is `followed`. Activation does no work for this: the
join runs only when the owner asks. The logs belong to one host and service and carry no conversation identity, so
concurrent work by the same principal can produce the evidence, and the report says so.
"""

from __future__ import annotations

import bisect
import json
import sqlite3
import time
from collections.abc import Callable, Iterable, Sequence
from contextlib import closing
from datetime import datetime
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
    "by the same principal in that window can produce it; not observed means the logs hold no matching event in a "
    "closed window, and unknown covers an open window, a rotated or disabled log and a skill or collection that links nowhere. "
    "Records import jobs commit outside the logged call, so their batches never count as followed."
)


class _Events:
    """Successful events of one log by path, each path's times sorted, so a serving costs a lookup, not a scan."""

    def __init__(self, path: Path, field: str) -> None:
        times: dict[str, list[float]] = {}
        for row in usage.read_jsonl(path):
            if row.get("outcome", "success") != "success" or not row.get(field) or not row.get("ts_utc"):
                continue
            try:
                times.setdefault(str(row[field]), []).append(datetime.fromisoformat(str(row["ts_utc"])).timestamp())
            except ValueError:
                continue
        self.by_path = {key: sorted(value) for key, value in times.items()}
        self.earliest = min((value[0] for value in self.by_path.values()), default=None)
        self._folders: dict[str, list[float]] = {}

    def _times(self, target: str) -> list[float]:
        if not target.endswith("/"):
            return self.by_path.get(target, [])
        if target not in self._folders:
            # A folder target is the merged times of every path under it, built once per folder per readout.
            self._folders[target] = sorted(
                when for path, times in self.by_path.items() if path.startswith(target) for when in times
            )
        return self._folders[target]

    def any_between(self, targets: Iterable[str], start: float, end: float) -> bool:
        for target in targets:
            times = self._times(target)
            position = bisect.bisect_left(times, start)
            if position < len(times) and times[position] <= end:
                return True
        return False


def _destinations(root: Path, method_path: str, resolver_for: Callable[[], Any]) -> tuple[str, ...]:
    """The pages the method page links to; a link to a collection manifest stands for the collection's folder.

    A method that is itself a collection manifest is followed by writes into its own collection too.
    """
    from . import capture_method, structured_collections

    try:
        text = (root / method_path).read_text(encoding="utf-8")
    except OSError:
        return ()
    found: list[str] = []
    if method_path.rpartition("/")[2] == structured_collections.MANIFEST_FILENAME:
        found.extend((method_path, method_path.rpartition("/")[0] + "/"))
    _basename, rows = vault.inbound_rows(method_path, text)
    for _line, _context, target, _bucket in rows:
        path = capture_method.locate(root, target, resolver_for=resolver_for)
        if not path or path == method_path:
            continue
        if path.rpartition("/")[2] == structured_collections.MANIFEST_FILENAME:
            path = path.rpartition("/")[0] + "/"
        if path not in found:
            found.append(path)
    return tuple(found)


def _state(events: _Events, start: float, targets: Sequence[str], now: float, *, logging_off: bool) -> str:
    if logging_off or not targets:
        return UNKNOWN
    end = start + METHOD_USE_WINDOW_MINUTES * 60
    if events.any_between(targets, start, end):
        return OBSERVED
    # A log that begins after the serving may have rotated the matching event away, and an open window may still
    # see it: neither is evidence that the method went unused.
    if events.earliest is None or events.earliest > start or now < end:
        return UNKNOWN
    return NOT_OBSERVED


def report(
    root: Path, sidecar: Path, *, resolver_for: Callable[[], Any], disabled: bool | None = None, now: float | None = None,
) -> dict[str, Any]:
    """Per served method: servings, and how many were opened and followed, each observed, not observed or unknown."""
    from . import memory_refs

    logging_off = query_log._disabled() if disabled is None else disabled  # noqa: SLF001 - the logs' own switch
    log_dir = query_log.current_log_dir()
    reads = _Events(log_dir / "reads.jsonl", "read_path")
    writes = _Events(log_dir / "writes.jsonl", "written_path")
    now = time.time() if now is None else now
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
            entry["opened"][_state(reads, float(served_at), opened_targets, now, logging_off=logging_off)] += 1
            entry["followed"][_state(writes, float(served_at), destinations, now, logging_off=logging_off)] += 1
    return {
        "state": "ready", "window_minutes": METHOD_USE_WINDOW_MINUTES, "rows_joined": len(rows),
        "rows_capped": len(rows) == MAX_JOINED_ROWS, "logging": "off" if logging_off else "on",
        "methods": sorted(methods.values(), key=lambda entry: (-entry["served"], entry["ref"])),
        "meaning": MEANING,
    }

