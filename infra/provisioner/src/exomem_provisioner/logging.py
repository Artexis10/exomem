"""Content-free structured logging."""

from __future__ import annotations

import json
import logging
from typing import Any

_ALLOWED_FIELDS = (
    "event",
    "action",
    "operation_id",
    "request_id",
    "checkpoint",
    "state",
    "code",
    "duration_ms",
)


class ContentFreeFormatter(logging.Formatter):
    """Render only an explicit allowlist of operational metadata."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname.lower(),
            "logger": record.name,
        }
        for name in _ALLOWED_FIELDS:
            value = getattr(record, name, None)
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
