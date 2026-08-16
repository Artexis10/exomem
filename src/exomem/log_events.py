"""Structured event logging shared by every log call site.

`log_event()` is the one seam that attaches a machine-readable `event` name,
a content-free `fields` mapping, and an optional catalog-restricted `content`
mapping to a `LogRecord`. Two formatters read those same attributes:
`JsonLinesFormatter` for the on-disk JSONL log, `KeyValueFormatter` for a
human-readable `key=value` rendering. `EVENT_CATALOG` is the single source of
truth for which events may carry `content` at all, and for which top-level
keys of that `content` are allowed — `privacy_log.py`'s hosted redaction
factory relies on this to drop only the content of a classified record while
keeping its content-free skeleton, instead of fully blanking it.

Nothing here may raise into a caller: a broken log call must never break the
tool call, HTTP request, or mutation it was describing.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import traceback
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class EventSpec:
    """What a cataloged event is allowed to carry in `content`."""

    content_fields: frozenset[str] = frozenset()


# Every event that passes through `log_event()` must be declared here. An
# event with a non-empty `content_fields` may carry those (and only those)
# keys in `content`; anything else drops undeclared keys rather than shipping
# them. An event not present in this catalog at all gets its `content`
# dropped entirely — classification must be earned, never assumed.
EVENT_CATALOG: dict[str, EventSpec] = {
    "tool_start": EventSpec(),
    "tool_success": EventSpec(),
    "tool_failure": EventSpec(content_fields=frozenset({"message"})),
    "rest_failure": EventSpec(content_fields=frozenset({"message"})),
    "hosted_call": EventSpec(),
    "log_write_error": EventSpec(content_fields=frozenset({"message"})),
    "observability_internal_error": EventSpec(content_fields=frozenset({"message"})),
    "http_request": EventSpec(content_fields=frozenset({"client_ip", "path"})),
    "mutation_lock_acquired": EventSpec(),
    "mutation_lock_released": EventSpec(),
    "mutation_lock_long_hold": EventSpec(),
    "mutation_holder_unverified": EventSpec(),
    "lease_idle_released": EventSpec(),
    "lease_acquired": EventSpec(),
    "lease_reclaimed": EventSpec(),
    "lease_renew_rejected": EventSpec(),
    "prevalidated_commit": EventSpec(),
}


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    fields: Mapping[str, Any] | None = None,
    content: Mapping[str, Any] | None = None,
    exc_info: Any = None,
) -> None:
    """Log one structured event. Never raises."""
    try:
        clean_fields = dict(fields) if isinstance(fields, Mapping) else {}
        clean_content = dict(content) if isinstance(content, Mapping) else {}
        spec = EVENT_CATALOG.get(event)
        if spec is None:
            clean_content = {}
        elif clean_content:
            for key in set(clean_content) - spec.content_fields:
                clean_content.pop(key, None)
        logger.log(
            level,
            event,
            extra={"event": event, "fields": clean_fields, "content": clean_content},
            exc_info=exc_info,
        )
    except Exception:  # noqa: BLE001 - a logging defect must never break the caller
        pass


def bounded_traceback(
    exc_info: Any, *, limit: int = 20, max_chars: int = 4000
) -> str:
    """Render a bounded traceback string, or "" when there is nothing to show."""
    try:
        if not exc_info:
            return ""
        if exc_info is True:
            exc_info = sys.exc_info()
        if isinstance(exc_info, BaseException):
            exc_info = (type(exc_info), exc_info, exc_info.__traceback__)
        exc_type, exc, tb = exc_info
        if exc_type is None:
            return ""
        text = "".join(traceback.format_exception(exc_type, exc, tb, limit=limit))
        if len(text) > max_chars:
            # Keep the tail: the exception type + message line is the most
            # useful part for grepping a truncated traceback, and it is
            # always last in `format_exception`'s output.
            text = "...<truncated>\n" + text[-max_chars:]
        return text
    except Exception:  # noqa: BLE001 - traceback rendering must never break the caller
        return ""


def _utc_timestamp(record: logging.LogRecord) -> str:
    return datetime.fromtimestamp(record.created, tz=UTC).isoformat(
        timespec="milliseconds"
    )


def _thread_name(record: logging.LogRecord) -> str:
    """The name of the thread that EMITTED `record`.

    `record.threadName` is captured at record creation, on the emitting thread,
    which is the only correct source here — a handler may format on a different
    thread than the one that logged, so `threading.current_thread().name` read
    at format time would misattribute every background line to the formatter's
    thread. The fallback only covers `logging.logThreads = False`, where the
    attribute is None, so the field is present on every record either way.
    """
    return getattr(record, "threadName", None) or threading.current_thread().name


class JsonLinesFormatter(logging.Formatter):
    """One JSON object per line; works for structured and plain records alike.

    `thread` names the emitting thread on every record. Request threads and
    background daemons (`exomem-graph-rebuild`, `exomem-lexical-repair-*`)
    interleave in one file, and without it adjacency reads as causation — that
    misled issue #576 twice, once into asserting a rebuild ran inside a request
    when it runs on a daemon, and once into nearly mis-attributing a 63 s stall.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _utc_timestamp(record),
            "level": record.levelname,
            "logger": record.name,
            "thread": _thread_name(record),
            "event": getattr(record, "event", None),
            "message": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if fields:
            payload["fields"] = fields
        content = getattr(record, "content", None)
        if content:
            payload["content"] = content
        if record.exc_info:
            traceback_text = bounded_traceback(record.exc_info)
            if traceback_text:
                payload["traceback"] = traceback_text
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


class KeyValueFormatter(logging.Formatter):
    """Human-readable `key=value` rendering of the same structured attributes."""

    def format(self, record: logging.LogRecord) -> str:
        parts = [f"ts={_utc_timestamp(record)}", f"level={record.levelname}"]
        event = getattr(record, "event", None)
        if event:
            parts.append(f"event={event}")
        for mapping_name in ("fields", "content"):
            mapping = getattr(record, mapping_name, None)
            if mapping:
                parts.extend(f"{key}={value}" for key, value in mapping.items())
        message = record.getMessage()
        if not event and message:
            parts.append(message)
        return " ".join(parts)
