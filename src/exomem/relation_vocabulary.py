"""Deterministic, read-only relation vocabulary discovery helpers."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from . import relation_registry


def resolve_relation(
    registry: relation_registry.RelationRegistry,
    *,
    query: str | None = None,
    requested_relation: str | None = None,
    limit: int = 20,
    observations: Iterable[Mapping[str, Any]] = (),
    continuation: str | None = None,
) -> dict[str, Any]:
    """Return bounded vocabulary evidence without choosing a relation for a caller."""
    if not query and not requested_relation:
        raise ValueError("RELATION_QUERY_REQUIRED: query or requested_relation is required")
    if limit < 1:
        raise ValueError("RELATION_LIMIT_INVALID: limit must be at least 1")

    extension_offset, observation_offset = _continuation_offsets(continuation)
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
    observation_rows = sorted(
        (dict(item) for item in observations),
        key=lambda item: (-int(item.get("count", 0)), str(item.get("raw_relation", ""))),
    )
    extension_page = extensions[extension_offset : extension_offset + limit]
    observation_page = observation_rows[
        observation_offset : observation_offset + limit
    ]
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
            len(observation_rows),
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
    observations: Iterable[Mapping[str, Any]] = (),
    continuation: str | None = None,
) -> dict[str, Any]:
    """Compose a reviewed delta with the resolver's complete duplicate evidence."""
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
        "truncated": total > next_offset,
        "continuation": f"{prefix}:{next_offset}" if total > next_offset else None,
    }


def _continuation_offsets(continuation: str | None) -> tuple[int, int]:
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


def _terms(value: str | None) -> list[str]:
    normalized = relation_registry.normalize_relation(value or "")
    return re.findall(r"[a-z0-9]+", normalized)


def _candidate_sort_key(item: Mapping[str, Any]) -> tuple[int, int, str]:
    evidence = item["evidence"]
    matched_terms = sum(len(component["terms"]) for component in evidence)
    return -matched_terms, -len(evidence), str(item["canonical"])
