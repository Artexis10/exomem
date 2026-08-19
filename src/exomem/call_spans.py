"""Per-call phase timing for MCP calls.

Deliberately a leaf: standard library only, importing nothing from the rest of
the package. The phases worth timing live in the write path -- corpus context,
indexing, graph fan-out, commit -- and those modules must be able to import this
without dragging in `command_surface`, which pulls `writer_lease` behind it.
A timer that creates an import cycle does not get installed.

Why the store is keyed rather than a plain ContextVar value: spans are recorded
deep inside the sync wrapper running in FastMCP's threadpool, and ContextVar
mutations do not propagate back out to the middleware that writes the ledger
row. The token propagates *in*; the measurements come back through this map.
That is the same bridge the failure breadcrumb in `command_surface` uses, for
the same reason.

The gap this closes was measured. A live `edit_memory` recorded
`total_ms=24,394` with `boundary_wait_ms=7` and `boundary_hold_ms=3,348`: enough
to rule out lock contention, and nothing whatsoever about the other 21 seconds.
Two boundary clocks can prove a call was slow. They cannot locate the defect
between them.
"""

from __future__ import annotations

import functools
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

#: Unique per call, unlike the client-supplied request id, so concurrent calls
#: sharing a request id can never cross-attribute their measurements.
MCP_CALL_TOKEN: ContextVar[str | None] = ContextVar(
    "exomem_mcp_call_token", default=None
)

_LOCK = threading.Lock()
_SPANS: dict[str, dict[str, Any]] = {}
_TTL_SECONDS = 300.0
#: Distinct phase names kept for one call. Exceeding this means the caller is
#: generating names, which is a caller bug; truncate rather than let one call
#: grow without bound.
MAX_NAMES_PER_CALL = 64
#: Calls tracked at once, so a missed pop cannot leak indefinitely. The
#: middleware pops unconditionally; this and the TTL are independent guards for
#: paths that never reach it, such as a direct test harness.
MAX_CALLS = 256
NAME_MAX_CHARS = 64


def _sweep_locked(now: float) -> None:
    stale = [
        token
        for token, entry in _SPANS.items()
        if now - float(entry["at"]) > _TTL_SECONDS
    ]
    for token in stale:
        _SPANS.pop(token, None)


def record_span(name: str, elapsed_ms: float) -> None:
    """Attribute `elapsed_ms` to phase `name` on the in-flight MCP call.

    A no-op outside an MCP call, so the same instrumentation is safe on CLI,
    watcher, and test paths that never mint a token.
    """
    try:
        token = MCP_CALL_TOKEN.get()
        if token is None:
            return
        now = time.monotonic()
        clean = str(name)[:NAME_MAX_CHARS]
        with _LOCK:
            _sweep_locked(now)
            entry = _SPANS.get(token)
            if entry is None:
                if len(_SPANS) >= MAX_CALLS:
                    _SPANS.pop(min(_SPANS, key=lambda key: _SPANS[key]["at"]), None)
                entry = {"at": now, "names": {}}
                _SPANS[token] = entry
            names: dict[str, list[float]] = entry["names"]
            slot = names.get(clean)
            if slot is None:
                if len(names) >= MAX_NAMES_PER_CALL:
                    return
                names[clean] = [1.0, float(elapsed_ms)]
            else:
                slot[0] += 1.0
                slot[1] += float(elapsed_ms)
    except Exception:  # noqa: BLE001 - instrumentation must never break a call
        pass


@contextmanager
def span(name: str):
    """Time one named phase of the current MCP call.

    Records on the way out whatever happened, the exception path included: a
    phase that raised after eighteen seconds is exactly the one worth seeing.
    Aggregated by name, so a phase entered once per changed path reports
    `count` and a total instead of hundreds of rows.
    """
    started = time.perf_counter()
    try:
        yield
    finally:
        record_span(name, (time.perf_counter() - started) * 1000.0)


def timed(name: str):
    """Attribute every call of the decorated function to phase `name`.

    A decorator rather than an inline `with` at each site: these are long
    existing functions, and reindenting a body to wrap it makes the diff about
    whitespace instead of about the measurement. Aggregation means a function
    called many times within one request reports `count` and a total.
    """

    def decorate(func):
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any):
            with span(name):
                return func(*args, **kwargs)

        return wrapper

    return decorate


def pop_call_spans(token: str | None) -> list[dict[str, Any]]:
    """Pop this call's phase timings, slowest first.

    Unconditional, like the failure breadcrumb: call it once per completed call
    and an uninstrumented one simply yields an empty list.
    """
    try:
        if token is None:
            return []
        with _LOCK:
            _sweep_locked(time.monotonic())
            entry = _SPANS.pop(token, None)
        if not entry:
            return []
        spans = [
            {"name": name, "count": int(slot[0]), "ms": round(slot[1], 2)}
            for name, slot in entry["names"].items()
        ]
        spans.sort(key=lambda item: float(item["ms"]), reverse=True)
        return spans
    except Exception:  # noqa: BLE001 - instrumentation must never break a call
        return []


def reset() -> None:
    """Drop all in-flight measurements. For tests that assert on isolation."""
    with _LOCK:
        _SPANS.clear()
