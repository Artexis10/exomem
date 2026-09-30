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
import logging
import re
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

#: Unique per call, unlike the client-supplied request id, so concurrent calls
#: sharing a request id can never cross-attribute their measurements.
MCP_CALL_TOKEN: ContextVar[str | None] = ContextVar(
    "exomem_mcp_call_token", default=None
)

log = logging.getLogger(__name__)

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
#: Named integer measurements a span may carry beside its duration -- how many
#: texts an encode took, how many characters they held, how many items a loop
#: covered. Bounded like everything else here: a ledger row is hash-chained and
#: no single call may grow it without limit. Values are summed across a span's
#: calls, exactly as `ms` and `count` are, so an aggregated span still reports
#: one honest total.
MAX_FIELDS_PER_SPAN = 4
#: A field key is a measurement name, never a value. Structural rather than a
#: declared list, so a new count needs no registry edit, but a key carrying
#: content cannot pass: no slashes, dots, spaces, digits or capitals, which is
#: every shape a path, title or identifier takes. Without this,
#: `measured["Knowledge Base/Notes/<title>"] = 1` would reach a hash-chained
#: ledger row -- the same hole the closed phase vocabulary exists to prevent.
FIELD_KEY_PATTERN = re.compile(r"^[a-z_]{1,32}$")


#: Eviction is a *loss* of measurement, so it warns rather than informs -- but a
#: process that is evicting is evicting often, and one line per drop would bury
#: the condition it reports. One line per window, carrying the count since the
#: last one, says the same thing without becoming the noise.
EVICTION_LOG_INTERVAL_SECONDS = 60.0
_evictions_since_log = 0
_last_eviction_log = 0.0


def _evict_oldest_locked() -> None:
    """Drop the oldest tracked call to stay under `MAX_CALLS`.

    Reported, because this is the one way a live call's measurements disappear
    without anyone asking: everything else is a pop by the middleware or a TTL
    expiry. A diagnosis reading an empty `spans` list would otherwise be unable
    to tell "this call was not instrumented" from "this call was evicted".

    Called under `_LOCK`, which is what makes the counters below safe.
    """
    global _evictions_since_log, _last_eviction_log

    if not _SPANS:
        return
    token = min(_SPANS, key=lambda key: _SPANS[key]["at"])
    dropped = _SPANS.pop(token, None)
    if dropped is None:
        return
    _evictions_since_log += 1
    now = time.monotonic()
    if now - _last_eviction_log < EVICTION_LOG_INTERVAL_SECONDS:
        return
    log.warning(
        "call span eviction dropped %d in-flight entr%s tracked=%d names=%d age_s=%.1f",
        _evictions_since_log,
        "y" if _evictions_since_log == 1 else "ies",
        len(_SPANS) + 1,
        len(dropped.get("names", {})),
        now - float(dropped["at"]),
    )
    _last_eviction_log = now
    _evictions_since_log = 0


def _sweep_locked(now: float) -> None:
    stale = [
        token
        for token, entry in _SPANS.items()
        if now - float(entry["at"]) > _TTL_SECONDS
    ]
    for token in stale:
        _SPANS.pop(token, None)


def record_span(
    name: str, elapsed_ms: float, fields: Mapping[str, Any] | None = None
) -> None:
    """Attribute `elapsed_ms` to phase `name` on the in-flight MCP call.

    A no-op outside an MCP call, so the same instrumentation is safe on CLI,
    watcher, and test paths that never mint a token.

    `fields` carries named integer measurements beside the duration, summed
    across the span's calls the way `ms` is. A duration alone cannot say
    whether an encode was slow because the model was cold or because it was
    handed the whole note body: 15.6 s with `texts=1` and 15.6 s with
    `texts=400` are different defects and read identically without this.
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
                    _evict_oldest_locked()
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
            if fields:
                _merge_fields_locked(entry, clean, fields)
    except Exception:  # noqa: BLE001 - instrumentation must never break a call
        pass


def _merge_fields_locked(
    entry: dict[str, Any], name: str, fields: Mapping[str, Any]
) -> None:
    """Sum `fields` into this span's running totals. Called under `_LOCK`."""
    store: dict[str, dict[str, int]] = entry.setdefault("fields", {})
    totals = store.get(name)
    if totals is None:
        if len(store) >= MAX_NAMES_PER_CALL:
            return
        totals = {}
        store[name] = totals
    for key, value in fields.items():
        clean_key = str(key)
        if not FIELD_KEY_PATTERN.match(clean_key):
            _warn_rejected_field_key(clean_key)
            continue
        if clean_key not in totals and len(totals) >= MAX_FIELDS_PER_SPAN:
            continue
        try:
            totals[clean_key] = totals.get(clean_key, 0) + int(value)
        except (TypeError, ValueError):
            # A field that is not a count is not a measurement; drop it rather
            # than put an uninterpretable value in a hash-chained row.
            continue


def _warn_rejected_field_key(key: str) -> None:
    """Say that a field key was dropped, without repeating the key itself.

    The key is the thing suspected of carrying content, so the warning reports
    its shape and length rather than its text -- a log line is operator-readable
    and lives outside the vault, exactly like the ledger row this refused.
    """
    log.warning(
        "span field key rejected: %d chars, not [a-z_]{1,32}", len(key)
    )


@contextmanager
def span(name: str, fields: dict[str, Any] | None = None):
    """Time one named phase of the current MCP call.

    Records on the way out whatever happened, the exception path included: a
    phase that raised after eighteen seconds is exactly the one worth seeing.
    Aggregated by name, so a phase entered once per changed path reports
    `count` and a total instead of hundreds of rows.

    The `fields` dict is yielded so the body can fill it with what it learned
    -- how many items it covered, how many characters it encoded -- and it is
    read on the way out. A caller that measures nothing but time passes
    nothing and the span is shaped exactly as it always was.
    """
    started = time.perf_counter()
    try:
        yield fields
    finally:
        record_span(name, (time.perf_counter() - started) * 1000.0, fields)


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


def mark(name: str) -> None:
    """Stamp a monotonic point on the in-flight call for a later site to close.

    `span()` needs both ends of an interval in one frame. Some intervals do not
    have that: the gap between canonical bytes landing and the mutation row
    reaching `canonically_committed` spans two modules and two call frames, and
    it is exactly where the 0.83.1 deploy left ~32 s per write unattributed.

    Marks live in the same token-keyed map as the spans, for the reason the
    module docstring gives: a ContextVar set deep inside the write path is not
    a reliable channel back out.
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
                    _evict_oldest_locked()
                entry = {"at": now, "names": {}}
                _SPANS[token] = entry
            marks: dict[str, float] = entry.setdefault("marks", {})
            if clean not in marks and len(marks) >= MAX_NAMES_PER_CALL:
                return
            marks[clean] = now
    except Exception:  # noqa: BLE001 - instrumentation must never break a call
        pass


def record_span_since(name: str, mark_name: str) -> None:
    """Record the interval from `mark_name` to now as span `name`.

    A no-op when the mark was never stamped: the mark's own site is soft, so a
    missing mark means that path did not run, not that this one should guess.
    """
    try:
        token = MCP_CALL_TOKEN.get()
        if token is None:
            return
        with _LOCK:
            entry = _SPANS.get(token)
            started = (entry or {}).get("marks", {}).get(str(mark_name)[:NAME_MAX_CHARS])
        if started is None:
            return
        record_span(name, (time.monotonic() - float(started)) * 1000.0)
    except Exception:  # noqa: BLE001 - instrumentation must never break a call
        pass


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
        measured: dict[str, dict[str, int]] = entry.get("fields", {})
        spans = []
        for name, slot in entry["names"].items():
            shaped: dict[str, Any] = {
                "name": name,
                "count": int(slot[0]),
                "ms": round(slot[1], 2),
            }
            # Omitted entirely when a span measured only time, so the shape of
            # every existing span -- and every ledger row already written -- is
            # exactly what it was.
            totals = measured.get(name)
            if totals:
                shaped["fields"] = dict(totals)
            spans.append(shaped)
        spans.sort(key=lambda item: float(item["ms"]), reverse=True)
        return spans
    except Exception:  # noqa: BLE001 - instrumentation must never break a call
        return []


def reset() -> None:
    """Drop all in-flight measurements. For tests that assert on isolation."""
    global _evictions_since_log, _last_eviction_log

    with _LOCK:
        _SPANS.clear()
        _evictions_since_log = 0
        _last_eviction_log = 0.0
