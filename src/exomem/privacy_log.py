"""Fail-closed content redaction for process logs."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Mapping

_FALSE = frozenset({"", "0", "false", "no", "off"})
_INSTALL_LOCK = threading.Lock()
_INSTALLED = False
_HOSTED_RESERVED_VAULT_NAMES = frozenset({".exomem-hosted-cell.json"})


def content_private_logging_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Treat any non-false hosted or cloud flag as content-private for logging.

    Hosted and cloud configuration each validate their own flag separately.
    Logging takes the safer path even while configuration is malformed so a
    startup error cannot expose tenant paths or parser excerpts. Cloud cells
    (design D1.2) share this same content-free boundary with hosted cells.
    """

    values = os.environ if env is None else env
    hosted_raw = str(values.get("EXOMEM_HOSTED_CELL", "")).strip().lower()
    if hosted_raw not in _FALSE:
        return True
    cloud_raw = str(values.get("EXOMEM_CLOUD_CELL", "")).strip().lower()
    return cloud_raw not in _FALSE


def is_reserved_hosted_vault_path(path: str) -> bool:
    """Reject runtime ownership markers from hosted user-file surfaces."""

    if not content_private_logging_enabled():
        return False
    parts = tuple(part for part in str(path).replace("\\", "/").split("/") if part)
    return any(part in _HOSTED_RESERVED_VAULT_NAMES for part in parts)


_UVICORN_ACCESS_ARITY = 5
_UVICORN_ACCESS_REDACTED = "-"


def _redact_uvicorn_access_record(record: logging.LogRecord) -> logging.LogRecord:
    """Blank the client address and path on a uvicorn access record.

    `uvicorn.access` logs `'%s - "%s %s HTTP/%s" %d', client_addr, method,
    full_path, http_version, status` — a fixed 5-tuple that
    `uvicorn.logging.AccessFormatter.formatMessage` unpacks positionally
    (`client_addr, method, full_path, http_version, status_code = args`).
    Full blanking (`record.args = ()`) breaks that unpack with
    `ValueError: not enough values to unpack (expected 5, got 0)` on every
    single access line in a hosted or cloud cell — this instead keeps the
    5-tuple shape and the content-free method/protocol/status fields, and
    replaces only the client address and the request path (which can carry
    a query string) with a fixed placeholder. `record.msg` (the format
    string above) is left as-is: it has no free-text slots of its own, so
    `record.getMessage()` renders content-free too.
    """

    args = record.args
    if isinstance(args, tuple) and len(args) == _UVICORN_ACCESS_ARITY:
        _client_addr, method, _full_path, http_version, status_code = args
        record.args = (
            _UVICORN_ACCESS_REDACTED,
            method,
            _UVICORN_ACCESS_REDACTED,
            http_version,
            status_code,
        )
    else:
        # An unrecognized shape: fail closed exactly like the general case
        # below rather than guess at arity.
        record.msg = "event=hosted_log_redacted code=HOSTED_CONTENT_REDACTED"
        record.args = ()
    record.exc_info = None
    record.exc_text = None
    record.stack_info = None
    return record


def _redact_for_hosted_cell(record: logging.LogRecord) -> logging.LogRecord:
    if not content_private_logging_enabled():
        return record
    if record.name == "uvicorn.access":
        return _redact_uvicorn_access_record(record)
    is_call_trace = (
        record.name == "exomem.calls"
        and isinstance(record.msg, str)
        and record.msg.startswith("event=hosted_call ")
    )
    if is_call_trace:
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return record
    from .log_events import EVENT_CATALOG

    event = getattr(record, "event", None)
    if event is not None and event in EVENT_CATALOG:
        record.content = {}
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return record
    record.msg = "event=hosted_log_redacted code=HOSTED_CONTENT_REDACTED"
    record.args = ()
    # Full blanking must also cover `extra=`-injected structured attributes:
    # JsonLinesFormatter emits `content`/`fields` whenever truthy, so an
    # uncataloged record carrying them would leak around the blanked message.
    record.content = {}
    record.fields = {}
    record.exc_info = None
    record.exc_text = None
    record.stack_info = None
    return record


def install_hosted_log_redaction() -> None:
    """Install one process-wide, dynamically gated content log boundary.

    A record produced by `log_events.log_event()` for a cataloged event is
    "structured": its `content` is the only content-bearing part, so a hosted
    cell drops only `content` and keeps the content-free `event`/`fields`
    skeleton. Any other record — the large existing body of plain `log.info()`
    calls, and the `exomem.calls` hosted-call trace line — is "unstructured"
    and keeps today's full-blanking, fail-closed behavior (the trace line is
    the one pre-vetted exception, kept verbatim). `exc_info` is always
    stripped in a hosted cell regardless of classification.

    This hooks `Logger.makeRecord` rather than `logging.setLogRecordFactory`:
    `extra=` attributes (`event`/`fields`/`content`) are applied by
    `makeRecord` to the record the factory already returned, so a factory
    hook runs too early to see them. Wrapping `makeRecord` runs after `extra`
    is applied but still before any handler (including a test's `caplog`)
    observes the record, so every observer sees the same redacted record.
    """

    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        original_make_record = logging.Logger.makeRecord

        def make_record(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
            record = original_make_record(self, *args, **kwargs)
            return _redact_for_hosted_cell(record)

        logging.Logger.makeRecord = make_record
        _INSTALLED = True
