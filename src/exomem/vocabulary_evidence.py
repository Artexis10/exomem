"""Bounded, read-only evidence for entity and relation vocabulary review."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from . import entity_types, relation_vocabulary

_CONTINUATION_VERSION = 1
_ENTITY_TYPE_LIMIT_MIN = 1
_ENTITY_TYPE_LIMIT_MAX = 64
_PROJECTION_STATUSES = frozenset({"current", "warming", "unavailable"})


def resolve_entity_type(
    registry: entity_types.EntityTypeRegistry,
    *,
    query: str | None = None,
    requested_type: str | None = None,
    limit: int = 20,
    continuation: str | None = None,
) -> dict[str, Any]:
    """Return entity-type definitions and lexical evidence without selecting a type."""
    if not _provided(query) and not _provided(requested_type):
        raise ValueError("ENTITY_TYPE_QUERY_REQUIRED: query or requested_type is required")
    _validate_entity_type_limit(limit)
    offset = _entity_type_offset(
        continuation,
        registry=registry,
        query=query,
        requested_type=requested_type,
    )
    extensions = [
        _entity_candidate(definition, query=query, requested_type=requested_type)
        for _, definition in sorted(registry.extensions.items())
    ]
    page = extensions[offset : offset + limit]
    next_offset = offset + len(page)
    next_continuation = (
        _encode_continuation(
            "entity_types",
            next_offset,
            registry=registry,
            query=query,
            requested=requested_type,
        )
        if next_offset < len(extensions)
        else None
    )
    return {
        "core_vocabulary": [_entity_definition(item) for item in registry.core.values()],
        "exact_matches": _entity_exact_matches(registry, requested_type),
        "candidates": page,
        "extensions": {
            "total": len(extensions),
            "returned": len(page),
            "omitted": max(len(extensions) - len(page), 0),
            "truncated": next_offset < len(extensions),
            "continuation": next_continuation,
        },
        "registry": _registry_identity(registry),
        "selected_type": None,
        "proposed_type": None,
        "decision_owner": "active-agent",
        "next_step": (
            "Choose from the definitions and source evidence. If no type fits a useful "
            "recurring identity, author a definition and rationale, preserve existing "
            "extensions, and submit the reviewed registry through save-entity-types. "
            "The null selection/proposal fields leave that judgment to you."
        ),
        "proposal_format": {
            "schema_version": 1,
            "entity_types": {
                "<canonical-type>": {
                    "folder": "<PluralFolder>", "label": "<Readable label>",
                    "aliases": [], "cue_nouns": [],
                    "capture_guidance": "<Which durable identities belong here>",
                },
            },
        },
    }


def relation_evidence(
    registry: Any,
    *,
    query: str | None = None,
    requested_relation: str | None = None,
    limit: int = 20,
    observations: Iterable[Mapping[str, Any]] | Mapping[str, Any] = (),
    continuation: str | None = None,
    projection_status: str = "current",
    projection_generation: str | None = None,
) -> dict[str, Any]:
    """Compose relation resolver evidence with truthful recurrence availability."""
    if projection_status not in _PROJECTION_STATUSES:
        raise ValueError(
            "RELATION_PROJECTION_STATUS_INVALID: projection_status must be current, "
            "warming, or unavailable"
        )
    if projection_generation is not None and not _provided(projection_generation):
        raise ValueError(
            "RELATION_PROJECTION_GENERATION_INVALID: projection_generation must be a "
            "non-empty string"
        )
    resolver_continuation = _relation_continuation(
        continuation,
        registry=registry,
        query=query,
        requested_relation=requested_relation,
        projection_generation=projection_generation,
    )
    result = relation_vocabulary.resolve_relation(
        registry,
        query=query,
        requested_relation=requested_relation,
        limit=limit,
        observations=observations if projection_status == "current" else (),
        continuation=resolver_continuation,
    )
    continuations = {
        scope: (
            _encode_continuation(
                scope,
                raw,
                registry=registry,
                query=query,
                requested=requested_relation,
                projection_generation=projection_generation,
            )
            if raw is not None and (scope != "observations" or projection_generation is not None)
            else None
        )
        for scope, raw in (
            ("extensions", result["extensions"]["continuation"]),
            ("observations", result["observations"]["continuation"]),
        )
    }
    return {
        **result,
        "extensions": {**result["extensions"], "continuation": continuations["extensions"]},
        "observations": {**result["observations"], "continuation": continuations["observations"]},
        "evidence_availability": _recurrence_availability(
            result,
            projection_status,
            projection_generation=projection_generation,
        ),
        "continuations": continuations,
    }


def _entity_exact_matches(
    registry: entity_types.EntityTypeRegistry, requested_type: str | None
) -> list[dict[str, Any]]:
    if not _provided(requested_type):
        return []
    requested = str(requested_type)
    definition = registry.resolve(requested)
    if definition is None:
        definition = next(
            (
                item
                for item in registry.extensions.values()
                if _entity_match_kind(item, requested) is not None
            ),
            None,
        )
    if definition is None:
        return []
    return [
        {
            "match": _entity_match_kind(definition, requested),
            "requested_type": requested,
            "canonical": definition.id,
            **_entity_definition(definition),
        }
    ]


def _entity_match_kind(definition: entity_types.EntityTypeDefinition, requested: str) -> str | None:
    normalized = entity_types.normalize_entity_token(requested)
    if normalized == definition.id:
        return "canonical"
    if (
        requested.strip().casefold() == definition.folder.casefold()
        or normalized == entity_types.normalize_entity_token(definition.folder)
    ):
        return "folder"
    if normalized in {
        entity_types.normalize_entity_token(value)
        for value in (definition.label, *definition.aliases)
    }:
        return "alias"
    return None


def _entity_candidate(
    definition: entity_types.EntityTypeDefinition,
    *,
    query: str | None,
    requested_type: str | None,
) -> dict[str, Any]:
    terms = set(_terms(query)) | set(_terms(requested_type))
    evidence = []
    for source, value in (
        ("canonical", definition.id),
        ("folder", definition.folder),
        ("label", definition.label),
        ("description", definition.capture_guidance),
        ("parent", definition.parent or ""),
    ):
        matched = sorted(terms & set(_terms(value)))
        if matched:
            evidence.append({"source": source, "terms": matched})
    for alias in definition.aliases:
        matched = sorted(terms & set(_terms(alias)))
        if matched:
            evidence.append({"source": "alias", "terms": matched})
    return {**_entity_definition(definition), "evidence": evidence}


def _entity_definition(definition: entity_types.EntityTypeDefinition) -> dict[str, Any]:
    return {
        "id": definition.id,
        "folder": definition.folder,
        "label": definition.label,
        "aliases": sorted(definition.aliases),
        "description": definition.capture_guidance,
        "optional_frontmatter": sorted(definition.optional_frontmatter),
        "cue_nouns": sorted(definition.cue_nouns),
        "parent": definition.parent,
        "status": definition.status,
        "replaced_by": definition.replaced_by,
        "core": definition.core,
    }


def _validate_entity_type_limit(limit: int) -> None:
    if type(limit) is not int or not _ENTITY_TYPE_LIMIT_MIN <= limit <= _ENTITY_TYPE_LIMIT_MAX:
        raise ValueError(
            "ENTITY_TYPE_LIMIT_INVALID: limit must be an integer between "
            f"{_ENTITY_TYPE_LIMIT_MIN} and {_ENTITY_TYPE_LIMIT_MAX}"
        )


def _entity_type_offset(
    continuation: str | None,
    *,
    registry: entity_types.EntityTypeRegistry,
    query: str | None,
    requested_type: str | None,
) -> int:
    if continuation is None:
        return 0
    cursor = _decode_continuation(
        continuation,
        scope="entity_types",
        registry=registry,
        query=query,
        requested=requested_type,
    )
    if type(cursor) is not int or cursor < 0:
        raise ValueError("VOCABULARY_CONTINUATION_INVALID: invalid continuation")
    return cursor


def _relation_continuation(
    continuation: str | None,
    *,
    registry: Any,
    query: str | None,
    requested_relation: str | None,
    projection_generation: str | None,
) -> str | None:
    if continuation is None:
        return None
    cursor = _decode_continuation(
        continuation,
        scope=None,
        registry=registry,
        query=query,
        requested=requested_relation,
        projection_generation=projection_generation,
    )
    if not isinstance(cursor, str) or not cursor.startswith(("extensions:", "observations:")):
        raise ValueError("VOCABULARY_CONTINUATION_INVALID: invalid continuation")
    return cursor


def _recurrence_availability(
    result: Mapping[str, Any],
    projection_status: str,
    *,
    projection_generation: str | None,
) -> dict[str, Any]:
    projection: dict[str, str] = {"status": projection_status}
    if projection_generation is not None:
        projection["generation"] = projection_generation
    availability: dict[str, Any] = {"projection": projection}
    if projection_status == "current":
        availability["recurrence"] = {
            "status": "available",
            "observations": result["observations"]["total"],
        }
    else:
        availability["recurrence"] = {
            "status": "unavailable",
            "reason": f"projection_{projection_status}",
        }
    if (
        projection_status == "current"
        and result["observations"]["continuation"] is not None
        and projection_generation is None
    ):
        availability["observation_continuation"] = {
            "status": "unavailable",
            "reason": "projection_generation_required",
        }
    return availability


def _encode_continuation(
    scope: str,
    cursor: int | str,
    *,
    registry: Any,
    query: str | None,
    requested: str | None,
    projection_generation: str | None = None,
) -> str:
    payload = {
        "version": _CONTINUATION_VERSION,
        "scope": scope,
        "cursor": cursor,
        "fingerprint": _continuation_fingerprint(
            registry,
            query,
            requested,
            projection_generation=projection_generation if scope == "observations" else None,
        ),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_continuation(
    continuation: str,
    *,
    scope: str | None,
    registry: Any,
    query: str | None,
    requested: str | None,
    projection_generation: str | None = None,
) -> int | str:
    try:
        padded = continuation + "=" * (-len(continuation) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("VOCABULARY_CONTINUATION_INVALID: invalid continuation") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("version") != _CONTINUATION_VERSION
        or not isinstance(payload.get("scope"), str)
        or "cursor" not in payload
        or not isinstance(payload.get("fingerprint"), str)
    ):
        raise ValueError("VOCABULARY_CONTINUATION_INVALID: invalid continuation")
    if scope is not None and payload["scope"] != scope:
        raise ValueError("VOCABULARY_CONTINUATION_INVALID: invalid continuation")
    cursor_scope = payload["scope"]
    if cursor_scope == "observations" and projection_generation is None:
        raise ValueError(
            "VOCABULARY_PROJECTION_GENERATION_REQUIRED: observation continuation "
            "requires projection_generation"
        )
    if payload["fingerprint"] != _continuation_fingerprint(
        registry,
        query,
        requested,
        projection_generation=projection_generation if cursor_scope == "observations" else None,
    ):
        raise ValueError("VOCABULARY_CONTINUATION_STALE: query or registry has changed")
    return payload["cursor"]


def _continuation_fingerprint(
    registry: Any,
    query: str | None,
    requested: str | None,
    *,
    projection_generation: str | None = None,
) -> str:
    state = {
        "registry": _registry_identity(registry),
        "query": query,
        "requested": requested,
        "projection_generation": projection_generation,
    }
    raw = json.dumps(state, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


def _registry_identity(registry: Any) -> dict[str, Any]:
    return {
        "core_version": registry.core_version,
        "extension_hash": registry.extension_hash,
    }


def _provided(value: str | None) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _terms(value: str | None) -> list[str]:
    return re.findall(r"[a-z0-9]+", entity_types.normalize_entity_token(value or ""))
