"""Interval accounting for opt-in whole-call recall diagnostics.

Process CPU snapshots bracket serialized boundaries. Native thread identifiers
detect overlap; they never assign encoder-pool CPU to a Python thread.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, Literal

CURRENT: ContextVar[Trace | None] = ContextVar("recall_timing_trace", default=None)


def runqueue_ns() -> int | None:
    """Read the synchronous request thread's scheduler wait, separately from CPU."""
    try:
        values = Path(f"/proc/self/task/{threading.get_native_id()}/schedstat").read_text().split()
        return int(values[1])
    except (OSError, ValueError, IndexError):
        return None


@dataclass
class Interval:
    name: str
    thread: int
    parent: int | None
    start: int
    end: int | None = None
    # These are the closed kinds of the flat fork/join timing protocol.
    kind: Literal["stage", "parallel", "branch"] = "stage"


@dataclass
class Parallel:
    trace: Trace | None
    index: int | None

    @contextmanager
    def branch(self):
        """Bracket a complete branch, including its gaps and waits."""
        if self.trace is None:
            yield
            return
        marker = CURRENT.set(self.trace)
        try:
            with self.trace._span("", kind="branch", parent=self.index) as index:
                yield index
        finally:
            CURRENT.reset(marker)


class Trace:
    """One transient call; its raw intervals and clocks never enter public output."""

    def __init__(self, *, known_serial: bool = False) -> None:
        self.lock = threading.RLock()
        self.intervals: list[Interval] = []
        self.stacks: dict[int, list[int]] = {}
        self.cpu_supported = Path("/proc/self/task").is_dir()
        self.edges: list[tuple[int, int | None]] = []
        self.started = self._stamp()
        self.queue_started = runqueue_ns()
        self.ended: int | None = None
        self.queue_ended: int | None = None
        self.known_serial = known_serial

    def _stamp(self) -> int:
        wall = time.perf_counter_ns()
        cpu = time.process_time_ns() if self.cpu_supported else None
        self.edges.append((wall, cpu))
        return wall

    @contextmanager
    def span(self, name: str):
        with self._span(name) as index:
            yield index

    @contextmanager
    def parallel(self):
        with self._span("", kind="parallel") as index:
            yield Parallel(self, index)

    @contextmanager
    def _span(self, name: str, *, kind: Literal["stage", "parallel", "branch"] = "stage",
              parent: int | None = None):
        thread = threading.get_native_id()
        with self.lock:
            stack = self.stacks.setdefault(thread, [])
            index = len(self.intervals)
            self.intervals.append(Interval(
                name, thread, parent if parent is not None else stack[-1] if stack else None,
                self._stamp(), kind=kind,
            ))
            stack.append(index)
        try:
            yield index
        finally:
            with self.lock:
                self.intervals[index].end = self._stamp()
                stack.pop()

    def finish(self) -> None:
        with self.lock:
            self.ended = self._stamp()
            self.queue_ended = runqueue_ns()

    def result(self, rows: dict[str, list[str]]) -> dict[str, Any]:
        """Partition every clock segment once; exclude separately owned children.

        Row names and span aliases come from the latency contract. Matching a
        dotted prefix or suffix describes instrumentation names, never intent.
        """
        if self.ended is None or any(item.end is None for item in self.intervals):
            raise ValueError("recall timing intervals are not closed")
        assignments: dict[int, str] = {}
        for index, item in enumerate(self.intervals):
            if item.kind != "stage":
                continue
            matches = [
                (len(alias), row)
                for row, aliases in rows.items()
                for alias in aliases
                if item.name == alias or item.name.startswith(alias + ".")
                or item.name.endswith("." + alias)
            ]
            if matches:
                assignments[index] = max(matches)[1]
        wall: dict[str, int] = {}
        cpu: dict[str, int] = {}
        critical: set[str] = set()
        unbudgeted: set[str] = set()
        groups = {i: item for i, item in enumerate(self.intervals) if item.kind == "parallel"}
        winners: set[int] = set()
        complete = bool(groups) or self.known_serial
        for group in groups:
            branches = {i: item for i, item in enumerate(self.intervals)
                        if item.kind == "branch" and item.parent == group}
            if not branches:
                complete = False
                continue
            if any(item.start < groups[group].start or item.end > groups[group].end
                   for item in branches.values()):
                complete = False
            latest = max(item.end for item in branches.values())
            winners.update(i for i, item in branches.items() if item.end == latest)
        outside_threads: set[int] = set()
        overlap_cpu = uncovered_cpu = uncovered_wall = 0
        edges = self.edges
        starting: dict[int, list[int]] = {}
        ending: dict[int, list[int]] = {}
        for index, item in enumerate(self.intervals):
            starting.setdefault(item.start, []).append(index)
            ending.setdefault(int(item.end), []).append(index)
        active: set[int] = set()
        for (start, cpu_start), (end, cpu_end) in zip(edges, edges[1:], strict=False):
            active.difference_update(ending.get(start, ()))
            active.update(starting.get(start, ()))
            if end <= start:
                continue
            real = {index for index in active if self.intervals[index].kind == "stage"}
            leaves: dict[int, int] = {}
            for index in real:
                thread = self.intervals[index].thread
                leaves[thread] = max(index, leaves.get(thread, -1))
            shadowed: set[int] = set()
            for index in leaves.values():
                ancestor = self.intervals[index].parent
                while ancestor is not None:
                    shadowed.add(ancestor)
                    ancestor = self.intervals[ancestor].parent
            owners: dict[int, str] = {}
            for thread, index in leaves.items():
                item = self.intervals[index]
                ancestors = []
                ancestor: int | None = index
                while ancestor is not None:
                    ancestors.append(ancestor)
                    ancestor = self.intervals[ancestor].parent
                branches = {i for i in ancestors if self.intervals[i].kind == "branch"}
                active_groups = set(groups) & active
                if not branches and not active_groups:
                    outside_threads.add(thread)
                elif not branches:
                    # A raw container may cover a fork/join, but anonymous work cannot supply causality.
                    for group in active_groups:
                        ancestor = self.intervals[group].parent
                        while ancestor is not None and ancestor != index:
                            ancestor = self.intervals[ancestor].parent
                        if ancestor is None:
                            complete = False
                # vector.wait is a closed instrumentation token from the contract.
                if item.name == "vector.wait" or index in shadowed:
                    continue
                ancestor = index
                while ancestor is not None and ancestor not in assignments:
                    ancestor = self.intervals[ancestor].parent
                if ancestor is None:
                    owner = "unbudgeted:" + item.name
                    unbudgeted.add(item.name)
                else:
                    owner = assignments[ancestor]
                owners[thread] = owner
                if branches:
                    if branches <= winners:
                        critical.add(owner)
                elif not active_groups:
                    critical.add(owner)
            nanoseconds = end - start
            for owner in set(owners.values()):
                wall[owner] = wall.get(owner, 0) + nanoseconds
            if not real:
                uncovered_wall += nanoseconds
            delta = cpu_end - cpu_start if cpu_start is not None else None
            if delta is not None:
                if len(leaves) > 1:
                    overlap_cpu += delta
                elif owners:
                    owner = next(iter(owners.values()))
                    cpu[owner] = cpu.get(owner, 0) + delta
                else:
                    # An excluded wait can leave raw coverage but no CPU owner.
                    uncovered_cpu += delta
        if len(outside_threads) > 1:
            complete = False
        total_cpu = (
            (edges[-1][1] - edges[0][1]) / 1e6 if self.cpu_supported else None
        )
        return {
            "total_ms": (self.ended - self.started) / 1e6,
            "cpu_ms": total_cpu,
            "cpu_source": "process_time_ns" if self.cpu_supported else "unknown",
            "runqueue_ms": (
                (self.queue_ended - self.queue_started) / 1e6
                if self.queue_ended is not None and self.queue_started is not None else None
            ),
            "unattributed_ms": uncovered_wall / 1e6,
            "overlap_cpu_ms": overlap_cpu / 1e6 if self.cpu_supported else None,
            "unattributed_cpu_ms": uncovered_cpu / 1e6 if self.cpu_supported else None,
            "unbudgeted": sorted(unbudgeted),
            "critical_path": sorted(critical) if complete else None,
            "stages": {
                row: {"ms": value / 1e6, "cpu_ms": cpu.get(row, 0) / 1e6 if self.cpu_supported else None}
                for row, value in sorted(wall.items())
            },
        }


def collect_span(manager):
    """Observe an existing context manager without replacing its body."""
    @wraps(manager)
    def measured(*args, **kwargs):
        trace = CURRENT.get()
        if trace is None and args and not isinstance(args[0], str):
            trace = getattr(args[0], "_request_trace", None)
        if trace is None:
            return manager(*args, **kwargs)
        name = kwargs["name"] if "name" in kwargs else (args[1] if not isinstance(args[0], str) else args[0])
        @contextmanager
        def observed():
            marker = CURRENT.set(trace)
            try:
                with trace.span(name.removeprefix("recall.")), manager(*args, **kwargs) as value:
                    yield value
            finally:
                CURRENT.reset(marker)
        return observed()
    return measured


@contextmanager
def measure_recall(enabled: bool):
    """Keep the complete synchronous command inside its opt-in clock."""
    if not enabled:
        yield None
        return
    # The current synchronous ask producer runs its named stages serially.
    trace = Trace(known_serial=True)
    marker = CURRENT.set(trace)
    try:
        yield trace
    finally:
        trace.finish()
        CURRENT.reset(marker)


def completed(result: Any, trace: Trace | None) -> Any:
    """Finish diagnostics only when the product's terminal policy disclosed them."""
    if (trace is None or not isinstance(result, dict)
        or not isinstance(result.get("timings"), dict)
        or not isinstance(result["timings"].get("stages"), dict)
        or "total_ms" not in result["timings"]):
        return result
    import sqlite3

    from .request_latency_contract import load_contract

    rows = load_contract().references["governed-reference"]["stage_rows"]
    finished = trace.result(rows)
    return {**result, "timings": {**result["timings"], **{
        key: value for key, value in finished.items() if key != "stages"
    }, "stage_rows": finished["stages"], "sqlite_version": sqlite3.sqlite_version}}
