"""Versioned product-visible input shared by both Exomem LME transports."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import unicodedata
from collections.abc import Sequence

from protocol.models import ProtocolEvent

CAPTURE_CONTRACT = "exomem.lme.capture/v2"
NAMESPACE_PATTERN = "exomem-container-tag-sha256-24hex"
PRODUCT_FIELDS = frozenset({"content", "title", "slug", "source_type", "tags", "compile_guidance"})
TRANSPORT_FIELDS = frozenset({"request_id", "idempotency_key"})


def payload_digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def capture_payload(events: Sequence[ProtocolEvent]) -> dict:
    if not events or len({(event.case_id, event.session_ordinal) for event in events}) != 1:
        raise ValueError("capture requires one nonempty neutral session")
    first = events[0]
    timestamp = dt.datetime.fromisoformat(first.original_timestamp.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=dt.UTC)
    stamp = timestamp.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [f"Session timestamp: {stamp}", f"Session ordinal: {first.session_ordinal}", ""]
    for event in events:
        if event.role not in {"user", "assistant", "system"}:
            raise ValueError("unsupported session role")
        lines.append(f"{event.role}: {unicodedata.normalize('NFC', event.content)}")
    content = "\n".join(lines)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    return {
        "content": content,
        "title": f"LongMemEval session {first.session_ordinal} {digest}",
        "slug": f"lme-session-{first.session_ordinal:04d}-{digest}",
        "source_type": "session", "tags": ["longmemeval"], "compile_guidance": False,
    }


def product_payload(body: dict) -> dict:
    """Keep every product field; only the two declared transport fields differ."""
    if not isinstance(body, dict) or set(body) - TRANSPORT_FIELDS != PRODUCT_FIELDS:
        raise ValueError("capture product fields differ from the declared contract")
    payload = {key: body[key] for key in PRODUCT_FIELDS}
    if any(not isinstance(payload[key], str) or not payload[key] for key in ("content", "title", "slug")):
        raise ValueError("capture text fields must be nonempty strings")
    if payload["source_type"] != "session" or payload["tags"] != ["longmemeval"] or payload["compile_guidance"] is not False:
        raise ValueError("capture metadata differs from the declared contract")
    return payload


def capture_read_body(events: Sequence[ProtocolEvent]) -> str:
    """Exact public source body produced by capture/v2 and read_memory.

    Export provenance requires this complete body; excerpts or changed wrappers
    do not establish that a path came from the pinned public source.
    """
    payload = capture_payload(events)
    return f"# {payload['title']}\n\n## Capture\n\n{payload['content'].strip()}\n"
