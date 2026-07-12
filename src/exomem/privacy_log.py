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
    """Treat any non-false hosted flag as content-private for logging.

    Hosted configuration validates the flag separately. Logging takes the safer
    path even while configuration is malformed so a startup error cannot expose
    tenant paths or parser excerpts.
    """

    values = os.environ if env is None else env
    raw = str(values.get("EXOMEM_HOSTED_CELL", "")).strip().lower()
    return raw not in _FALSE


def is_reserved_hosted_vault_path(path: str) -> bool:
    """Reject runtime ownership markers from hosted user-file surfaces."""

    if not content_private_logging_enabled():
        return False
    parts = tuple(part for part in str(path).replace("\\", "/").split("/") if part)
    return any(part in _HOSTED_RESERVED_VAULT_NAMES for part in parts)


def install_hosted_log_redaction() -> None:
    """Install one process-wide, dynamically gated content log boundary."""

    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return
        previous = logging.getLogRecordFactory()

        def factory(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            record = previous(*args, **kwargs)
            if not content_private_logging_enabled():
                return record
            is_call_trace = (
                record.name == "exomem.calls"
                and isinstance(record.msg, str)
                and record.msg.startswith("event=hosted_call ")
            )
            if is_call_trace:
                return record
            record.msg = "event=hosted_log_redacted code=HOSTED_CONTENT_REDACTED"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
            return record

        logging.setLogRecordFactory(factory)
        _INSTALLED = True
