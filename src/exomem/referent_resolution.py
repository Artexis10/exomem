"""Pure deterministic referent cue detection and evidence composition."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .bm25 import stem_word
from .entity_candidates import identity_key
from .entity_types import EntityTypeRegistry, core_registry
from .project_keys import _levenshtein

_WORD_RE = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)
_COUNT_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "both": 2,
    "pair": 2,
    "couple": 2,
}
_STOP_WORDS = frozenset(
    {
        "a",
        "about",
        "all",
        "an",
        "and",
        "as",
        "at",
        "be",
        "by",
        "did",
        "do",
        "for",
        "from",
        "had",
        "has",
        "have",
        "he",
        "her",
        "his",
        "i",
        "in",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "our",
        "she",
        "that",
        "the",
        "their",
        "them",
        "they",
        "this",
        "to",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "whose",
        "with",
        "you",
        "your",
    }
)
_PERSON_NOUNS = frozenset(
    {
        "friend",
        "friends",
        "colleague",
        "colleagues",
        "person",
        "people",
        "someone",
        "somebody",
        "mate",
        "mates",
        "guy",
        "guys",
        "woman",
        "women",
        "man",
        "men",
        "who",
        "whom",
        "whose",
    }
)
_TYPE_NOUNS: dict[str, frozenset[str]] = {
    "organization": frozenset(
        {"firm", "firms", "vendor", "vendors", "supplier", "suppliers", "org", "orgs"}
    ),
    "concept": frozenset(
        {"approach", "approaches", "pattern", "patterns", "principle", "principles"}
    ),
    "library": frozenset(
        {
            "framework",
            "frameworks",
            "sdk",
            "sdks",
            "tool",
            "tools",
            "dependency",
            "dependencies",
            "lib",
            "libs",
        }
    ),
    "decision": frozenset({"choice", "choices", "call", "calls", "ruling", "rulings"}),
}
_INTERROGATIVE_NOUNS = frozenset({"who", "whom", "whose"})

REFERENT_CANDIDATE_CAP = 25


def _tokens(text: object) -> tuple[str, ...]:
    return tuple(match.group(0).casefold() for match in _WORD_RE.finditer(str(text or "")))


_CUE_NOUN_CACHE: dict[tuple[int, str], dict[str, str]] = {}


def cue_nouns_for(registry: EntityTypeRegistry) -> dict[str, str]:
    """Return deterministic cue nouns for one immutable registry identity."""
    cache_key = (registry.core_version, registry.extension_hash)
    cached = _CUE_NOUN_CACHE.get(cache_key)
    if cached is not None:
        return cached
    nouns = {noun: "person" for noun in _PERSON_NOUNS}
    for entity_type, supplementary in _TYPE_NOUNS.items():
        for noun in supplementary:
            if noun not in _COUNT_WORDS and noun not in _STOP_WORDS:
                nouns[noun] = entity_type
    for definition in registry.active_definitions:
        values = (
            definition.id,
            definition.label,
            definition.folder,
            *definition.aliases,
            *definition.cue_nouns,
        )
        for value in values:
            key = value.casefold().replace("-", " ")
            if " " not in key:
                nouns[key] = definition.id
    _CUE_NOUN_CACHE[cache_key] = nouns
    return nouns


_CUE_NOUNS = cue_nouns_for(core_registry())
_COUNT_TOKENS = frozenset(_COUNT_WORDS) | frozenset(str(i) for i in range(1, 11))


@dataclass(frozen=True, slots=True)
class ReferentCue:
    entity_type: str
    noun: str
    expected_count: int | None
    descriptors: tuple[str, ...]
    query: str
    cue_nouns: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class EntityRecord:
    path: str
    title: str
    entity_type: str
    status: str
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    relationship: str = ""
    affiliation: str = ""
    attributes: tuple[str, ...] = ()
    ref: str | None = None


@dataclass(frozen=True, slots=True)
class HitFact:
    path: str
    type: str | None
    title: str
    status: str
    rank: int
    bm25_rank: int | None = None
    vector_rank: int | None = None
    keyword_rank: int | None = None
    descriptor_tokens: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EdgeFact:
    seed_path: str
    candidate_path: str
    relation_type: str | None
    direction: str
    family: str


@dataclass(frozen=True, slots=True)
class Evidence:
    kind: str
    detail: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, **dict(self.detail)}


@dataclass(frozen=True, slots=True)
class ReferentMatch:
    path: str
    title: str
    entity_type: str
    evidence: tuple[Evidence, ...]
    ref: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "path": self.path,
            "title": self.title,
            "entity_type": self.entity_type,
            "evidence": [item.as_dict() for item in self.evidence],
        }
        if self.ref:
            out["ref"] = self.ref
        return out


@dataclass(frozen=True, slots=True)
class ReferentResolution:
    status: str
    entity_type: str
    expected_count: int | None
    resolved: tuple[ReferentMatch, ...]
    candidates: tuple[ReferentMatch, ...]
    unresolved_count: int | None
    reasons: Mapping[str, int]
    omitted_candidate_count: int

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": self.status,
            "entity_type": self.entity_type,
            "resolved": [item.as_dict() for item in self.resolved],
            "candidates": [item.as_dict() for item in self.candidates],
            "reasons": dict(sorted(self.reasons.items())),
        }
        if self.expected_count is not None:
            out["expected_count"] = self.expected_count
        if self.unresolved_count is not None:
            out["unresolved_count"] = self.unresolved_count
        if self.omitted_candidate_count:
            out["omitted_candidate_count"] = self.omitted_candidate_count
        return out


def detect_cue(
    query: str,
    *,
    registry: EntityTypeRegistry | None = None,
) -> ReferentCue | None:
    """Return the first deterministic entity cue, or ``None``."""
    cue_nouns = cue_nouns_for(registry or core_registry())
    tokens = _tokens(query)
    selected: tuple[int, str, str] | None = None
    fallback: tuple[int, str, str] | None = None
    for index, token in enumerate(tokens):
        entity_type = cue_nouns.get(token)
        if entity_type is None:
            continue
        match = (index, token, entity_type)
        if token in _INTERROGATIVE_NOUNS:
            fallback = fallback or match
            continue
        selected = match
        break
    chosen = selected or fallback
    if chosen is None:
        return None
    cue_index, noun, entity_type = chosen

    expected_count: int | None = None
    for token in reversed(tokens[max(0, cue_index - 3) : cue_index]):
        if token in _COUNT_WORDS:
            expected_count = _COUNT_WORDS[token]
            break
        if token.isdigit() and 1 <= int(token) <= 10:
            expected_count = int(token)
            break

    descriptors = tuple(
        token
        for token in tokens
        if token not in _STOP_WORDS
        and token not in _COUNT_TOKENS
        and token not in cue_nouns
        and len(token) >= 3
    )
    return ReferentCue(
        entity_type=entity_type,
        noun=noun,
        expected_count=expected_count,
        descriptors=descriptors,
        query=query,
        cue_nouns=frozenset(cue_nouns),
    )


def _exact_name(cue: ReferentCue, entity: EntityRecord) -> str | None:
    query_key = f" {identity_key(cue.query)} "
    for value in (entity.title, *entity.aliases):
        candidate = identity_key(value)
        if candidate and f" {candidate} " in query_key:
            return str(value)
    return None


def _fuzzy_name(cue: ReferentCue, entity: EntityRecord) -> tuple[str, str, int] | None:
    query_tokens = tuple(
        token
        for token in _tokens(cue.query)
        if token not in _STOP_WORDS
        and token not in _COUNT_TOKENS
        and token not in (cue.cue_nouns or frozenset(_CUE_NOUNS))
    )
    identity_tokens = tuple(
        token for value in (entity.title, *entity.aliases) for token in _tokens(value)
    )
    matches: list[tuple[int, str, str]] = []
    for candidate in identity_tokens:
        if len(candidate) < 5:
            continue
        threshold = 1 if len(candidate) <= 7 else 2
        for query_token in query_tokens:
            if (
                not query_token
                or query_token[0] != candidate[0]
                or abs(len(query_token) - len(candidate)) > 2
            ):
                continue
            distance = _levenshtein(query_token, candidate, max_dist=threshold)
            if distance <= threshold:
                matches.append((distance, query_token, candidate))
    if not matches:
        return None
    distance, query_token, candidate = sorted(matches)[0]
    return query_token, candidate, distance


def _matches_attribute(descriptor: str, attribute: str) -> bool:
    if descriptor == attribute or stem_word(descriptor) == stem_word(attribute):
        return True
    shorter, longer = sorted((descriptor, attribute), key=len)
    return len(shorter) >= 4 and longer.startswith(shorter)


def descriptor_tokens_for(*values: object) -> tuple[str, ...]:
    """Return deterministic tokens for descriptor matching in the pure layer."""
    return tuple(sorted({token for value in values for token in _tokens(value)}))


def _attribute_matches(cue: ReferentCue, entity: EntityRecord) -> tuple[str, ...]:
    attributes = tuple(
        token
        for value in (*entity.tags, entity.relationship, entity.affiliation, *entity.attributes)
        for token in _tokens(value)
    )
    return tuple(
        sorted(
            {
                descriptor
                for descriptor in (*cue.descriptors, cue.noun)
                if any(_matches_attribute(descriptor, attribute) for attribute in attributes)
            }
        )
    )


def resolve_referents(
    *,
    cue: ReferentCue,
    hits: tuple[HitFact, ...] | list[HitFact],
    entities: tuple[EntityRecord, ...] | list[EntityRecord],
    edges: tuple[EdgeFact, ...] | list[EdgeFact],
    anchor_cap: int = 10,
) -> ReferentResolution:
    """Compose categorical evidence without changing recall ordering."""
    ordered_hits = tuple(sorted(hits, key=lambda item: (item.rank, item.path)))
    active_anchors = {
        item.path
        for item in ordered_hits[: max(0, anchor_cap)]
        if item.status.casefold() not in {"superseded", "archived", "dropped"}
    }
    hits_by_path = {item.path: item for item in hits}
    edges_by_candidate: dict[str, list[EdgeFact]] = {}
    for edge in edges:
        if edge.seed_path in active_anchors:
            edges_by_candidate.setdefault(edge.candidate_path, []).append(edge)

    resolved: list[ReferentMatch] = []
    candidates: list[ReferentMatch] = []
    reasons = {"inactive": 0, "type_mismatch": 0}
    for entity in sorted(entities, key=lambda item: item.path):
        if entity.status.casefold() != "active":
            reasons["inactive"] += 1
            continue
        evidence: list[Evidence] = []
        exact = _exact_name(cue, entity)
        if exact is not None:
            evidence.append(Evidence("exact_name", {"matched": exact}))
        else:
            fuzzy = _fuzzy_name(cue, entity)
            if fuzzy is not None:
                evidence.append(
                    Evidence(
                        "fuzzy_name",
                        {"query_token": fuzzy[0], "name_token": fuzzy[1], "distance": fuzzy[2]},
                    )
                )

        hit = hits_by_path.get(entity.path)
        if hit is not None:
            lanes = sorted(
                lane
                for lane, value in (
                    ("bm25", hit.bm25_rank),
                    ("keyword", hit.keyword_rank),
                    ("vector", hit.vector_rank),
                )
                if value is not None
            )
            evidence.append(Evidence("retrieval", {"rank": hit.rank, "lanes": lanes}))

        graph_edges = sorted(
            edges_by_candidate.get(entity.path, ()),
            key=lambda item: (
                1 if item.relation_type == "links_to" or item.family in {"", "link"} else 0,
                item.seed_path,
                item.relation_type or "",
                item.direction,
            ),
        )
        descriptor_graph_edges = [
            edge
            for edge in graph_edges
            if any(
                _matches_attribute(descriptor, token)
                for descriptor in cue.descriptors
                for token in hits_by_path[edge.seed_path].descriptor_tokens
            )
        ]
        graph_descriptor_bearing = bool(descriptor_graph_edges)
        if graph_edges:
            edge = (descriptor_graph_edges or graph_edges)[0]
            evidence.append(
                Evidence(
                    "graph",
                    {
                        "seed": edge.seed_path,
                        "relation_type": edge.relation_type or "links_to",
                        "direction": edge.direction,
                        "tier": 1
                        if edge.relation_type == "links_to" or edge.family in {"", "link"}
                        else 0,
                    },
                )
            )

        matched_attributes = _attribute_matches(cue, entity)
        if matched_attributes:
            evidence.append(Evidence("attribute", {"matched": list(matched_attributes)}))

        evidence.sort(key=lambda item: item.kind)
        exact_present = any(item.kind == "exact_name" for item in evidence)
        if entity.entity_type != cue.entity_type and not exact_present:
            reasons["type_mismatch"] += 1
            continue
        match = ReferentMatch(
            path=entity.path,
            title=entity.title,
            entity_type=entity.entity_type,
            evidence=tuple(evidence),
            ref=entity.ref,
        )
        non_exact_kinds = {item.kind for item in evidence if item.kind != "exact_name"}
        attribute_descriptor_bearing = any(
            descriptor in matched_attributes for descriptor in cue.descriptors
        )
        descriptor_gate_passes = (
            not cue.descriptors or attribute_descriptor_bearing or graph_descriptor_bearing
        )
        if exact_present or (len(non_exact_kinds) >= 2 and descriptor_gate_passes):
            resolved.append(match)
        elif evidence:
            candidates.append(match)

    expected = cue.expected_count
    resolved_tuple = tuple(resolved)
    if expected is None:
        status = "resolved" if resolved_tuple else "unresolved"
        unresolved_count = None
    elif len(resolved_tuple) > expected:
        status = "ambiguous"
        unresolved_count = None
    elif len(resolved_tuple) == expected:
        status = "resolved"
        unresolved_count = None
    else:
        status = "partial" if resolved_tuple else "unresolved"
        unresolved_count = expected - len(resolved_tuple)

    omitted_candidate_count = max(0, len(resolved) - REFERENT_CANDIDATE_CAP) + max(
        0, len(candidates) - REFERENT_CANDIDATE_CAP
    )
    return ReferentResolution(
        status=status,
        entity_type=cue.entity_type,
        expected_count=expected,
        resolved=resolved_tuple[:REFERENT_CANDIDATE_CAP],
        candidates=tuple(candidates[:REFERENT_CANDIDATE_CAP]),
        unresolved_count=unresolved_count,
        reasons=reasons,
        omitted_candidate_count=omitted_candidate_count,
    )
