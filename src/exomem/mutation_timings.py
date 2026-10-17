"""Opt-in per-stage timing collector for one governed write.

The read path has had `find_types.FindTimings` from the beginning; the write
path had nothing, so a 55-70s commit on a large vault was one opaque number
with no phase attribution. This is the write-side twin: same span/skipped/
error/as_dict shape, deliberately NOT a subclass of `FindTimings` — the two
surfaces describe different things and must be free to diverge (a write has a
mutation boundary to wait on; a read has a cache and a retrieval profile).

The collector is a pure measuring instrument: it never changes control flow,
never raises, and is entirely optional. `mutation_timing_span` is the
null-object helper that lets an instrumented code path stay a single
expression whether or not a collector was supplied.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from contextlib import contextmanager, nullcontext
from typing import Any

_TRUE = frozenset({"1", "true", "yes", "on"})


class MutationTimings:
    """Opt-in per-stage timing collector for one write (preflight + commit)."""

    def __init__(self) -> None:
        self._t0 = time.perf_counter()
        self.stages: dict[str, dict[str, Any]] = {}
        # Write-specific counterpart to FindTimings' cache/profile: how long
        # the caller waited for the vault mutation boundary. Recorded whether
        # or not the boundary was ever acquired — a wait that never resolves
        # is the one worth reading. A `retries` field belongs here too, but
        # only once it can be sourced from the lease manager; an always-zero
        # counter reads as "never retried", which would be a lie.
        self.boundary: dict[str, Any] = {"waited_ms": None}

    @contextmanager
    def span(self, name: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            entry = self.stages.setdefault(name, {})
            entry["ms"] = round((time.perf_counter() - t0) * 1000.0, 3)

    def skipped(self, name: str) -> None:
        self.stages.setdefault(name, {})["skipped"] = True

    def error(self, name: str, exc: BaseException) -> None:
        self.stages.setdefault(name, {})["error"] = type(exc).__name__

    def emit_call_spans(self) -> None:
        """Attribute the collected stages to the in-flight MCP call.

        Unconditional, and that is the whole point. `EXOMEM_WRITE_TIMINGS`
        decides whether a *caller* sees a timing envelope on its response --
        a governed surface that must not grow a key nobody asked for. It was
        never meant to decide whether the operator can see the same numbers,
        but it did: these stages existed for a year and never reached a ledger
        row, so a 78 s write on the 0.84.1 personal service had 46 s inside
        `derived.canonical_to_committed` with no span and no log line, while
        `commit.stamp_check` and its siblings had measured pieces of it all
        along.

        A stage with no `ms` is one that was skipped or raised before it was
        timed. It is left out rather than recorded as zero, which is the same
        contract every other span here keeps: a missing span means that path
        did not run, not that it was instant.

        Never raises: `call_spans` is a leaf that swallows its own failures,
        and a collector is a measuring instrument.
        """
        from . import call_spans

        for name, entry in self.stages.items():
            elapsed = entry.get("ms")
            if elapsed is None:
                continue
            try:
                call_spans.record_span(str(name), float(elapsed))
            except (TypeError, ValueError):
                continue

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_ms": round((time.perf_counter() - self._t0) * 1000.0, 3),
            "boundary": dict(self.boundary),
            "stages": {key: dict(value) for key, value in self.stages.items()},
        }


def mutation_timing_span(timings: MutationTimings | None, name: str):
    """A timing span when a collector is present, else a no-op context."""
    return timings.span(name) if timings is not None else nullcontext()


def write_timings_enabled(env: Mapping[str, str] | None = None) -> bool:
    """`EXOMEM_WRITE_TIMINGS` — attach the timing envelope to write responses.

    Off by default: the collector costs nothing worth measuring, but the
    response payload is a governed surface and must not grow a key nobody
    asked for. The service-side `write.*` metrics are emitted regardless of
    this flag (the metrics module does its own gating).
    """
    values = os.environ if env is None else env
    return str(values.get("EXOMEM_WRITE_TIMINGS", "")).strip().lower() in _TRUE
