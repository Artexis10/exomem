"""Deterministic, read-only relation vocabulary discovery helpers."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from . import relation_registry

RELATION_CANDIDATE_LIMIT_MIN = 1
RELATION_CANDIDATE_LIMIT_MAX = 64


def validate_candidate_limit(limit: int) -> None:
    """Enforce the one public relation-evidence page budget."""
    if (
        type(limit) is not int
        or limit < RELATION_CANDIDATE_LIMIT_MIN
        or limit > RELATION_CANDIDATE_LIMIT_MAX
    ):
        raise ValueError(
            "RELATION_LIMIT_INVALID: limit must be an integer between "
            f"{RELATION_CANDIDATE_LIMIT_MIN} and {RELATION_CANDIDATE_LIMIT_MAX}"
        )


def resolve_relation(
    registry: relation_registry.RelationRegistry,
    *,
    query: str | None = None,
    requested_relation: str | None = None,
    limit: int = 20,
    observations: Iterable[Mapping[str, Any]] | Mapping[str, Any] = (),
    continuation: str | None = None,
) -> dict[str, Any]:
    """Return bounded vocabulary evidence without choosing a relation for a caller."""
    if not query and not requested_relation:
        raise ValueError("RELATION_QUERY_REQUIRED: query or requested_relation is required")
    validate_candidate_limit(limit)

    extension_offset, observation_offset = continuation_offsets(continuation)
    requested = relation_registry.normalize_relation(requested_relation or "")
    exact_matches = _exact_matches(registry, requested) if requested_relation else []
    extensions = [
        _candidate(
            registry,
            definition,
            query=query,
            requested_relation=requested_relation,
        )
        for _, definition in sorted(registry.extensions.items())
    ]
    extensions.sort(key=_candidate_sort_key)
    extension_page = extensions[extension_offset : extension_offset + limit]
    observation_page, observation_total = _observation_page(
        observations,
        limit=limit,
        offset=observation_offset,
    )
    return {
        "core_vocabulary": [
            _definition(registry, definition)
            for _, definition in sorted(registry.core.items())
        ],
        "exact_matches": exact_matches,
        "candidates": extension_page,
        "extensions": _page_metadata(
            "extensions", len(extensions), len(extension_page), limit, extension_offset
        ),
        "unregistered_pressure": observation_page,
        "observations": _page_metadata(
            "observations",
            observation_total,
            len(observation_page),
            limit,
            observation_offset,
        ),
        "registry": {
            "core_version": registry.core_version,
            "extension_hash": registry.extension_hash,
        },
        "honest_outcomes": {
            "relates_to": "available when a meaningful generic connection is justified",
            "no_edge": "available when no durable relationship is established",
        },
        "selected_relation": None,
        "proposed_relation": None,
    }


def propose_relation(
    registry: relation_registry.RelationRegistry,
    *,
    requested_label: str,
    parent: str | None = None,
    description: str | None = None,
    direction: str | None = None,
    namespace: str = "vault",
    aliases: Iterable[str] = (),
    inverse: str | None = None,
    origins: Iterable[str] | None = None,
    source_kinds: Iterable[str] | None = None,
    target_kinds: Iterable[str] | None = None,
    projects: Iterable[str] | None = None,
    page_types: Iterable[str] | None = None,
    query: str | None = None,
    limit: int = 20,
    observations: Iterable[Mapping[str, Any]] | Mapping[str, Any] = (),
    continuation: str | None = None,
) -> dict[str, Any]:
    """Compose a reviewed delta with the resolver's complete duplicate evidence."""
    validate_candidate_limit(limit)
    proposal = relation_registry.propose_extension(
        registry,
        requested_label=requested_label,
        parent=parent,
        description=description,
        direction=direction,
        namespace=namespace,
        aliases=aliases,
        inverse=inverse,
        origins=origins,
        source_kinds=source_kinds,
        target_kinds=target_kinds,
        projects=projects,
        page_types=page_types,
    )
    evidence = resolve_relation(
        registry,
        query=query,
        requested_relation=requested_label,
        limit=limit,
        observations=observations,
        continuation=continuation,
    )
    proposed_key, proposed_value = next(iter(proposal["delta"]["upsert"].items()))
    exact_matches: dict[str, dict[str, Any]] = {}
    for requested in (proposed_key, *proposed_value.get("aliases", [])):
        for match in _exact_matches(registry, requested):
            exact_matches.setdefault(match["canonical"], match)
    evidence["exact_matches"] = list(exact_matches.values())
    return {
        **proposal,
        "duplicate_evidence": {
            key: evidence[key]
            for key in (
                "exact_matches",
                "candidates",
                "extensions",
                "unregistered_pressure",
                "observations",
            )
        },
    }


def _exact_matches(
    registry: relation_registry.RelationRegistry, requested: str
) -> list[dict[str, Any]]:
    canonical = registry.aliases.get(requested, requested)
    definition = registry.definition(canonical)
    if definition is None:
        return []
    return [
        {
            "match": "alias" if canonical != requested else "canonical",
            "requested_relation": requested,
            "canonical": canonical,
            "parent": definition.parent,
            "family": definition.family,
            "description": definition.description,
            "direction": definition.direction,
            **({"inverse": definition.inverse} if definition.inverse else {}),
            "aliases": sorted(definition.aliases),
            "status": definition.status if not definition.core else "core",
            "immediate_replacement": definition.replaced_by,
            "terminal_replacement": registry.terminal_replacement(canonical),
            "predecessors": sorted(registry.predecessors(canonical)),
        }
    ]


def _candidate(
    registry: relation_registry.RelationRegistry,
    definition: relation_registry.RelationDefinition,
    *,
    query: str | None,
    requested_relation: str | None,
) -> dict[str, Any]:
    terms = set(_terms(query)) | set(_terms(requested_relation))
    evidence: list[dict[str, Any]] = []
    for source, value in (
        ("canonical", definition.key),
        ("description", definition.description),
        ("parent", definition.parent or ""),
        ("inverse", definition.inverse or ""),
        ("replacement", definition.replaced_by or ""),
    ):
        matched = sorted(terms & set(_terms(value)))
        if matched:
            evidence.append({"source": source, "terms": matched})
    for alias in definition.aliases:
        matched = sorted(terms & set(_terms(alias)))
        if matched:
            evidence.append({"source": "alias", "terms": matched})
    return {
        **_definition(registry, definition),
        "canonical": definition.key,
        "evidence": evidence,
    }


def _definition(
    registry: relation_registry.RelationRegistry,
    definition: relation_registry.RelationDefinition,
) -> dict[str, Any]:
    return {
        **_definition_metadata(definition),
        "immediate_replacement": definition.replaced_by,
        "terminal_replacement": registry.terminal_replacement(definition.key),
        "predecessors": sorted(registry.predecessors(definition.key)),
    }


def _definition_metadata(
    definition: relation_registry.RelationDefinition,
) -> dict[str, Any]:
    return definition.as_dict()


def _page_metadata(
    prefix: str, total: int, returned: int, limit: int, offset: int
) -> dict[str, Any]:
    next_offset = offset + returned
    return {
        "total": total,
        "returned": returned,
        "omitted": max(total - returned, 0),
        "truncated": total > next_offset,
        "continuation": f"{prefix}:{next_offset}" if total > next_offset else None,
    }


def continuation_offsets(continuation: str | None) -> tuple[int, int]:
    """Decode the opaque resolver token for bounded indexed reads."""
    if continuation is None:
        return 0, 0
    try:
        prefix, raw_offset = continuation.split(":", 1)
        offset = int(raw_offset)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("RELATION_CONTINUATION_INVALID: invalid continuation") from exc
    if prefix not in {"extensions", "observations"} or offset < 0:
        raise ValueError("RELATION_CONTINUATION_INVALID: invalid continuation")
    return (offset, 0) if prefix == "extensions" else (0, offset)


def _observation_page(
    observations: Iterable[Mapping[str, Any]] | Mapping[str, Any],
    *,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int]:
    """Accept either injected evidence or one already-paged indexed result."""
    if isinstance(observations, Mapping) and "items" in observations:
        raw_items = observations.get("items")
        total = observations.get("total")
        page_offset = observations.get("offset")
        if (
            not isinstance(raw_items, (list, tuple))
            or type(total) is not int
            or total < 0
            or type(page_offset) is not int
            or page_offset != offset
            or len(raw_items) > limit
        ):
            raise ValueError(
                "RELATION_CONTINUATION_INVALID: indexed observation page does not "
                "match the requested continuation"
            )
        return [dict(item) for item in raw_items if isinstance(item, Mapping)], total

    rows = sorted(
        (dict(item) for item in observations if isinstance(item, Mapping)),
        key=lambda item: (-int(item.get("count", 0)), str(item.get("raw_relation", ""))),
    )
    return rows[offset : offset + limit], len(rows)


def _terms(value: str | None) -> list[str]:
    normalized = relation_registry.normalize_relation(value or "")
    return re.findall(r"[a-z0-9]+", normalized)


def _candidate_sort_key(item: Mapping[str, Any]) -> tuple[int, int, str]:
    evidence = item["evidence"]
    matched_terms = sum(len(component["terms"]) for component in evidence)
    return -matched_terms, -len(evidence), str(item["canonical"])
