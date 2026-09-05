"""Content-free structured logging."""

from __future__ import annotations

import json
import logging
from enum import Enum
from typing import Any

from .conflict_reason import ConflictReason

_ALLOWED_FIELDS = (
    "event",
    "action",
    # The *internal* operation ID is confidential -- the recovery runbook forbids
    # it in any log. Emit `operation_digest` instead; it correlates against the
    # operations table without publishing the identity.
    "operation_id",
    "operation_digest",
    "request_id",
    "checkpoint",
    "state",
    "code",
    "reason",
    "duration_ms",
)

# Fields whose value must be a member of a closed vocabulary. This handler is
# installed on the root logger and carries `uvicorn` too, so any library record
# that happens to set one of these common attribute names -- `reason` is set by
# HTTP responses, SSL errors, Kubernetes API exceptions and websocket close
# frames -- would otherwise render its free text through the one formatter whose
# entire purpose is content-freedom. Membership is enforced, not documented.
_CLOSED_SET_FIELDS: dict[str, type[Enum]] = {"reason": ConflictReason}


class ContentFreeFormatter(logging.Formatter):
    """Render only an explicit allowlist of operational metadata."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname.lower(),
            "logger": record.name,
        }
        for name in _ALLOWED_FIELDS:
            value = getattr(record, name, None)
            vocabulary = _CLOSED_SET_FIELDS.get(name)
            if vocabulary is not None:
                if not isinstance(value, vocabulary):
                    continue
                value = value.value
            if isinstance(value, (str, int, float, bool)) and value != "":
                payload[name] = value
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def configure_content_free_logging() -> None:
    """Install one content-free stderr path for application and server logs."""

    handler = logging.StreamHandler()
    handler.setFormatter(ContentFreeFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "exomem_provisioner"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
