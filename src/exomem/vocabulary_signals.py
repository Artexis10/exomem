"""Structural vocabulary signals over a bounded, caller-resolved evidence page.

An independence key comes from canonical provenance, never the edge producer,
an incidental word or a count of paths. This module makes no semantic choice.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .vocabulary_workflow import PROJECTION_STATUSES, REVIEW_PREFIX, _hash, _text, family_descriptor

MAX_ORIGINS_PER_PROJECTION = 64
CONTEXT_CHARS = 800
ADVISORY_BYTES = 1024


def consider_generic_pair(
    *,
    source_ref: str,
    target_ref: str,
    origins: Sequence[Mapping[str, Any]],
    resolved: bool,
    relation: str,
    projection_status: str,
    question: str | None = None,
    limit: int = 4,
    continuation: str | None = None,
) -> dict[str, Any]:
    if (
        not _text(source_ref)
        or not _text(target_ref)
        or type(resolved) is not bool
        or projection_status not in PROJECTION_STATUSES
        or not _text(relation)
        or type(limit) is not int
        or not 1 <= limit <= 64
        or (question is not None and (not _text(question) or len(question) > 2000))
    ):
        raise ValueError(
            "VOCABULARY_SIGNAL_INVALID: resolved pair and valid evidence budget required"
        )
    if not isinstance(origins, Sequence) or len(origins) > MAX_ORIGINS_PER_PROJECTION:
        raise ValueError("VOCABULARY_SIGNAL_INVALID: provide a bounded origin projection page")
    for origin in origins:
        if (
            not isinstance(origin, Mapping)
            or not _text(origin.get("ref"))
            or not _text(origin.get("version"))
            or not isinstance(origin.get("context", ""), str)
            or (
                origin.get("independence_key") is not None and not _text(origin["independence_key"])
            )
        ):
            raise ValueError("VOCABULARY_SIGNAL_INVALID: invalid canonical origin evidence")
    entries = sorted(origins, key=lambda origin: (origin["ref"], origin["version"]))
    keys = {origin["independence_key"] for origin in entries if origin.get("independence_key")}
    independence_available = all(origin.get("independence_key") for origin in entries)
    available = resolved and projection_status == "current"
    count = len(keys) if available and (independence_available or len(keys) >= 2) else None
    if not available:
        eligibility, reason = (
            "unavailable",
            "projection_unavailable" if resolved else "identity_unresolved",
        )
    elif question:
        eligibility, reason = "eligible", "explicit_meaning_question"
    elif relation != "relates_to":
        eligibility, reason = "not_eligible", "not_a_generic_relation"
    elif len(keys) >= 2:
        eligibility, reason = "eligible", "independent_origin_recurrence"
    elif not independence_available:
        eligibility, reason = "unavailable", "origin_independence_unavailable"
    else:
        eligibility, reason = "not_eligible", "independent_recurrence_not_observed"
    snapshot = _hash(
        {
            "pair": [source_ref, target_ref],
            "relation": relation,
            "origins": entries,
            "question": question,
            "projection_status": projection_status,
        }
    )
    offset = 0
    if continuation is not None:
        try:
            cursor = json.loads(continuation)
            if not isinstance(cursor, dict) or set(cursor) != {"snapshot", "offset"}:
                raise ValueError
            offset = cursor["offset"]
            if type(offset) is not int or not 0 <= offset <= len(entries):
                raise ValueError
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError("VOCABULARY_CONTINUATION_INVALID: refresh pair evidence") from exc
        if cursor["snapshot"] != snapshot:
            raise ValueError("VOCABULARY_CONTINUATION_STALE: refresh pair evidence")
    next_offset = offset + limit
    return {
        "source_ref": source_ref,
        "target_ref": target_ref,
        "eligibility": eligibility,
        "reason": reason,
        "independent_origins": count,
        "unresolved_origins": sum(not origin.get("independence_key") for origin in entries),
        "question": question,
        "selected_relation": None,
        "proposed_relation": None,
        "evidence": [
            {
                "ref": origin["ref"],
                "version": origin["version"],
                "independence_key": origin.get("independence_key"),
                "context": origin.get("context", "")[:CONTEXT_CHARS],
                "context_truncated": len(origin.get("context", "")) > CONTEXT_CHARS,
            }
            for origin in entries[offset:next_offset]
        ],
        "continuation": json.dumps({"snapshot": snapshot, "offset": next_offset}, sort_keys=True)
        if next_offset < len(entries)
        else None,
        "review_route": {"tool": "connect_memory", "operation": "resolve-relation"},
    }


def compact_advisory(*, ref: str, family: str, fingerprint: str) -> dict[str, Any]:
    descriptor = family_descriptor(family)
    if not ref.startswith(REVIEW_PREFIX) or not re.fullmatch(
        r"[a-f0-9]{24}", ref[len(REVIEW_PREFIX) :]
    ):
        raise ValueError("VOCABULARY_ITEM_INVALID: invalid vocabulary reference")
    if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        raise ValueError("VOCABULARY_ITEM_INVALID: invalid signal fingerprint")
    order = ("reuse", "enrich", "propose-new", "generic", "no-edge", "defer")
    result = {
        "ref": ref,
        "family": family,
        "fingerprint": fingerprint,
        "message": "Review the evidence and existing definitions; choose a useful meaning or abstain.",
        "context_route": {"tool": "review_item_context", "ref": ref},
        "decisions": [value for value in order if value in descriptor.allowed_decisions],
        "permission": "consideration does not authorize mutation",
    }
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > ADVISORY_BYTES:
        raise ValueError("VOCABULARY_ADVISORY_BUDGET_EXCEEDED: use explicit review")
    return result
