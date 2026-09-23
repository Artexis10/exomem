"""Fail-closed content redaction for process logs."""

from __future__ import annotations

import hashlib
import logging
import os
import re
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


#: Client families a content-private log may name. MCP `clientInfo` and
#: `User-Agent` are caller-chosen text of any shape, so under content-private
#: logging only this fixed vocabulary reaches a log line, never the value.
_CLIENT_FAMILIES = (
    ("codex", "codex"),
    ("claude-code", "claude-code"),
    ("claude code", "claude-code"),
    ("chatgpt", "chatgpt"),
    ("openai", "chatgpt"),
    ("claude", "claude-ai"),
)
_CLIENT_VERSION = re.compile(r"[0-9]{1,6}(\.[0-9]{1,6}){0,3}")


def log_client_label(value: object, *, env: Mapping[str, str] | None = None) -> str | None:
    """A caller-supplied client name, as a log line may carry it.

    Kept verbatim outside content-private mode. Inside it, the name becomes
    its client family (`"claude-ai"`, `"chatgpt"`, `"codex"`, `"claude-code"`)
    or `"other"`, which still answers "which client?" without carrying text.
    """

    if value is None:
        return None
    text = str(value)
    if not content_private_logging_enabled(env):
        return text
    if not text.strip():
        return None
    folded = text.casefold()
    for needle, family in _CLIENT_FAMILIES:
        if needle in folded:
            return family
    return "other"


def log_client_version(value: object, *, env: Mapping[str, str] | None = None) -> str | None:
    """A caller-supplied client version: verbatim, or digits and dots only.

    Under content-private logging anything but a plain dotted version
    (`"1.2.3"`) is dropped.
    """

    if value is None:
        return None
    text = str(value)
    if not content_private_logging_enabled(env):
        return text
    return text if _CLIENT_VERSION.fullmatch(text) else None


_HTTP_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "DELETE", "CONNECT", "OPTIONS", "TRACE", "PATCH"}
)


def log_http_method(value: object, *, env: Mapping[str, str] | None = None) -> str:
    """A request method, as a log line may carry it.

    Kept verbatim outside content-private mode. Inside it, anything but a
    standard method becomes `"OTHER"`: the method token is caller-chosen, and
    a server answers an unknown one with 405 after logging it.
    """

    text = str(value or "")
    if not content_private_logging_enabled(env):
        return text
    return text if text in _HTTP_METHODS else "OTHER"


def log_session_ref(value: object, *, env: Mapping[str, str] | None = None) -> str | None:
    """A caller-supplied MCP session id, as a log line may carry it.

    Kept verbatim outside content-private mode. Inside it, a short digest
    stands in: rows of one session still correlate across the access log and
    the call ledger, but the header's own text, which the caller chooses,
    never lands in either.
    """

    if value is None:
        return None
    text = str(value)
    if not content_private_logging_enabled(env):
        return text
    if not text:
        return None
    digest = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
    return f"sha256:{digest[:16]}"


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
            method if method in _HTTP_METHODS else "OTHER",
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
