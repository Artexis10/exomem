"""Immutable identities for governed authorization-projection namespaces.

This module owns the principal-free half of projected retrieval: the exact
namespace key and the finite, content-addressed representations which later
lexical/vector/graph lanes measure.  It deliberately does not persist a
principal, purpose, session, grant, query, score, or request decision.  Those
remain request-local selectors over these immutable rows.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from . import bridges
from .decisions import Decision, decide
from .policy import Policy, StandingGrant
from .principal import OWNER_AUDIENCE

MAX_PROJECTION_VARIANTS_PER_ITEM = 256
PROJECTOR_SCHEMA_VERSION = 1

_NAMESPACE_DOMAIN = b"exomem.authorization-projection-namespace-key.v1"
_VARIANT_DOMAIN = b"exomem.authorization-projection.v1\0"
_HEX = frozenset("0123456789abcdef")
_MEASUREMENT_LANES = frozenset({"lexical", "vector", "rerank", "clip", "graph"})
_OPTION_STRING_KEYS = frozenset({"notice", "constraint", "abstract", "bridge"})
_OPTION_DERIVED_KEYS = frozenset({"constraint_source", "constraint_ambiguous"})
_OPTION_KEYS = _OPTION_STRING_KEYS | _OPTION_DERIVED_KEYS
_OPTION_LEVELS = {"notice": 1, "constraint": 2, "abstract": 3, "bridge": 4}
_MAX_SAFE_INTEGER = (1 << 53) - 1
_VARIANT_VALUE_KEYS = frozenset(
    {
        "item_identity",
        "content_hash",
        "decision_level",
        "options",
        "release_strip",
        "bridge_id",
        "bridge_dependency_content_hash",
        "projector_schema_version",
    }
)
_SEARCH_FIELDS_BY_LEVEL = {
    1: frozenset({"notice"}),
    2: frozenset({"constraint"}),
    3: frozenset({"abstract"}),
    4: frozenset({"bridge"}),
    5: frozenset({"body"}),
}


class ProjectionError(RuntimeError):
    """Base class for fail-closed projection construction errors."""


class ProjectionCanonicalizationError(ProjectionError):
    """A value is outside the closed RFC 8785 subset used by projections."""


class ProjectionVariantOverflow(ProjectionError):
    """One item has more unique reachable variants than the fixed cap."""


class ProjectionVariantMismatch(ProjectionError):
    """One variant identity names two different immutable representations."""


def _digest(value: str, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX for character in value)
    ):
        raise ProjectionCanonicalizationError(f"{name} must be one lowercase SHA-256 digest")
    return value


def _bounded_text(value: object, name: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ProjectionCanonicalizationError(f"{name} must be bounded non-empty text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ProjectionCanonicalizationError(f"{name} contains an invalid Unicode scalar") from error
    return value


def _framed(domain: bytes, fields: Sequence[bytes]) -> bytes:
    out = bytearray(domain)
    out.append(0)
    for field in fields:
        if len(field) > (1 << 32) - 1:
            raise ProjectionCanonicalizationError("projection identity field is too large")
        out.extend(len(field).to_bytes(4, "big"))
        out.extend(field)
    return bytes(out)


def _jcs_string(value: str) -> bytes:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ProjectionCanonicalizationError("JCS text contains an invalid Unicode scalar") from error
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")


def _jcs_key(value: str) -> bytes:
    try:
        # RFC 8785 / ECMAScript property sorting compares UTF-16 code units.
        return value.encode("utf-16-be")
    except UnicodeEncodeError as error:
        raise ProjectionCanonicalizationError("JCS key contains an invalid Unicode scalar") from error


def _canonical_jcs(value: object) -> bytes:
    if value is None:
        return b"null"
    if value is True:
        return b"true"
    if value is False:
        return b"false"
    if isinstance(value, int) and not isinstance(value, bool):
        if value < -_MAX_SAFE_INTEGER or value > _MAX_SAFE_INTEGER:
            raise ProjectionCanonicalizationError(
                "JCS integer is outside the closed interoperable range"
            )
        return str(value).encode("ascii")
    if isinstance(value, float):
        raise ProjectionCanonicalizationError("floating-point values are not in projection JCS")
    if isinstance(value, str):
        return _jcs_string(value)
    if isinstance(value, Mapping):
        items: list[tuple[bytes, str, object]] = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProjectionCanonicalizationError("JCS object keys must be strings")
            items.append((_jcs_key(key), key, item))
        items.sort(key=lambda item: item[0])
        encoded = [
            _jcs_string(key) + b":" + _canonical_jcs(item)
            for _sort_key, key, item in items
        ]
        return b"{" + b",".join(encoded) + b"}"
    if isinstance(value, (list, tuple)):
        return b"[" + b",".join(_canonical_jcs(item) for item in value) + b"]"
    raise ProjectionCanonicalizationError(
        f"{type(value).__name__} is not in the closed projection JCS subset"
    )


def canonical_jcs(value: object) -> bytes:
    """Encode the closed projection value subset with RFC 8785 key ordering."""

    return _canonical_jcs(value)


@dataclass(frozen=True, slots=True)
class ProjectionNamespaceKey:
    """The complete persistent namespace key; no measurement key may widen it."""

    policy_fingerprint: str
    projector_schema_version: int
    catalog_generation: int

    def __post_init__(self) -> None:
        _digest(self.policy_fingerprint, "policy_fingerprint")
        if type(self.projector_schema_version) is not int or self.projector_schema_version <= 0:
            raise ProjectionCanonicalizationError("projector_schema_version must be positive")
        if type(self.catalog_generation) is not int or self.catalog_generation < 0:
            raise ProjectionCanonicalizationError("catalog_generation must be non-negative")

    def as_tuple(self) -> tuple[str, int, int]:
        return (
            self.policy_fingerprint,
            self.projector_schema_version,
            self.catalog_generation,
        )

    @property
    def namespace_id(self) -> str:
        preimage = _framed(
            _NAMESPACE_DOMAIN,
            (
                self.policy_fingerprint.encode("ascii"),
                str(self.projector_schema_version).encode("ascii"),
                str(self.catalog_generation).encode("ascii"),
            ),
        )
        return hashlib.sha256(preimage).hexdigest()


@dataclass(frozen=True, slots=True)
class MeasurementKey:
    """A per-variant lane measurement subkey, never a namespace component."""

    projection_variant_id: str
    lane: str
    extractor_version: str
    model_version: str

    def __post_init__(self) -> None:
        _digest(self.projection_variant_id, "projection_variant_id")
        if self.lane not in _MEASUREMENT_LANES:
            raise ProjectionCanonicalizationError("measurement lane is not registered")
        _bounded_text(self.extractor_version, "extractor_version", maximum=256)
        _bounded_text(self.model_version, "model_version", maximum=256)


@dataclass(frozen=True, slots=True)
class ProjectionVariant:
    """One principal-free fixed representation under a namespace item row."""

    projection_variant_id: str
    item_identity: str
    content_hash: str
    decision_level: int
    value_jcs: bytes
    search_fields: Mapping[str, str]

    def __post_init__(self) -> None:
        _digest(self.projection_variant_id, "projection_variant_id")
        _bounded_text(self.item_identity, "item_identity", maximum=4096)
        _digest(self.content_hash, "content_hash")
        if type(self.decision_level) is not int or not 1 <= self.decision_level <= 6:
            raise ProjectionCanonicalizationError("decision_level must be L1-L6")
        if not isinstance(self.value_jcs, bytes) or not self.value_jcs:
            raise ProjectionCanonicalizationError("value_jcs must be canonical bytes")
        try:
            parsed = json.loads(self.value_jcs)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProjectionCanonicalizationError("value_jcs is invalid") from error
        if canonical_jcs(parsed) != self.value_jcs:
            raise ProjectionCanonicalizationError("value_jcs is not canonical")
        _validate_variant_value(
            parsed,
            item_identity=self.item_identity,
            content_hash=self.content_hash,
            decision_level=self.decision_level,
        )
        if hashlib.sha256(_VARIANT_DOMAIN + self.value_jcs).hexdigest() != (
            self.projection_variant_id
        ):
            raise ProjectionCanonicalizationError("projection_variant_id does not bind value_jcs")
        if not isinstance(self.search_fields, Mapping) or not self.search_fields:
            raise ProjectionCanonicalizationError("search_fields must be non-empty")
        copied = dict(self.search_fields)
        for key, value in copied.items():
            _bounded_text(key, "search field name", maximum=128)
            _bounded_text(value, f"search field {key}", maximum=1_048_576)
        expected_fields = _SEARCH_FIELDS_BY_LEVEL.get(self.decision_level)
        if expected_fields is not None and frozenset(copied) != expected_fields:
            raise ProjectionCanonicalizationError(
                "search field shape does not match decision level"
            )
        object.__setattr__(self, "search_fields", MappingProxyType(copied))


def _validate_variant_value(
    value: object,
    *,
    item_identity: str,
    content_hash: str,
    decision_level: int,
) -> None:
    if not isinstance(value, dict) or frozenset(value) != _VARIANT_VALUE_KEYS:
        raise ProjectionCanonicalizationError("value_jcs has an invalid field set")
    if value["item_identity"] != item_identity:
        raise ProjectionCanonicalizationError("value item_identity does not match row")
    if value["content_hash"] != content_hash:
        raise ProjectionCanonicalizationError("value content_hash does not match row")
    if value["decision_level"] != decision_level:
        raise ProjectionCanonicalizationError("value decision_level does not match row")
    projector_schema_version = value["projector_schema_version"]
    if type(projector_schema_version) is not int or projector_schema_version <= 0:
        raise ProjectionCanonicalizationError("value projector_schema_version is invalid")
    options = value["options"]
    if not isinstance(options, dict) or _canonical_options(options) != options:
        raise ProjectionCanonicalizationError("value options are not canonical")
    release_strip = value["release_strip"]
    if not isinstance(release_strip, list):
        raise ProjectionCanonicalizationError("value release_strip is invalid")
    prior_key: tuple[bytes, bytes, bytes] | None = None
    for item in release_strip:
        if not isinstance(item, dict) or frozenset(item) != {"path", "ref", "title"}:
            raise ProjectionCanonicalizationError("value release_strip row is invalid")
        key = _canonical_strip_key(item["path"], item["ref"], item["title"])
        if prior_key is not None and key <= prior_key:
            raise ProjectionCanonicalizationError("value release_strip is not a canonical set")
        prior_key = key
    bridge_id = value["bridge_id"]
    bridge_dependency = value["bridge_dependency_content_hash"]
    if decision_level == 4:
        _bounded_text(bridge_id, "value bridge_id", maximum=4096)
        _digest(bridge_dependency, "value bridge_dependency_content_hash")
    elif bridge_id is not None or bridge_dependency is not None:
        raise ProjectionCanonicalizationError("value bridge binding is invalid for level")


def fixed_excerpt(body: str, *, limit: int = 600) -> str:
    """Return the query-independent L5 first-600-code-point representation."""

    if not isinstance(body, str) or type(limit) is not int or limit <= 0:
        raise ProjectionCanonicalizationError("fixed excerpt requires text and a positive limit")
    text = " ".join(body.split())
    if len(text) <= limit:
        return text
    prefix = text[:limit]
    head, separator, _tail = prefix.rpartition(" ")
    if separator:
        prefix = head
    return prefix + " …"


def _canonical_options(options: Mapping[str, Any]) -> dict[str, str | bool]:
    unknown = set(options) - _OPTION_KEYS
    if unknown:
        raise ProjectionCanonicalizationError("decision contains an unregistered option")
    canonical: dict[str, str | bool] = {}
    for key in sorted(options, key=_jcs_key):
        value = options[key]
        if key in _OPTION_STRING_KEYS:
            canonical[key] = _bounded_text(value, f"option {key}", maximum=4096)
        elif key == "constraint_source":
            if value != "scope":
                raise ProjectionCanonicalizationError("constraint_source is invalid")
            canonical[key] = "scope"
        elif key == "constraint_ambiguous":
            if type(value) is not bool:
                raise ProjectionCanonicalizationError("constraint_ambiguous must be boolean")
            canonical[key] = value
    return canonical


def _options_at_level(
    options: Mapping[str, Any], level: int
) -> dict[str, str | bool]:
    canonical = _canonical_options(options)
    projected = {
        key: value
        for key, value in canonical.items()
        if key in _OPTION_LEVELS and _OPTION_LEVELS[key] <= level
    }
    if "constraint" in projected and canonical.get("constraint_source") == "scope":
        projected["constraint_source"] = "scope"
    if level == 1 and canonical.get("constraint_ambiguous") is True:
        projected["constraint_ambiguous"] = True
    return projected


def _canonical_strip(
    values: Iterable[Any],
) -> list[dict[str, str]]:
    unique: dict[tuple[str, str, str], dict[str, str]] = {}
    for value in values:
        if not isinstance(value, bridges.StripIdentity):
            raise ProjectionCanonicalizationError("release strip identity is not canonical")
        path = _bounded_text(value.path, "release strip path", maximum=4096)
        ref = _bounded_text(value.ref, "release strip ref", maximum=4096)
        title = _bounded_text(value.title, "release strip title", maximum=1024)
        unique[(path, ref, title)] = {"path": path, "ref": ref, "title": title}
    return [
        unique[key]
        for key in sorted(
            unique,
            key=lambda item: _canonical_strip_key(*item),
        )
    ]


def _canonical_strip_key(path: object, ref: object, title: object) -> tuple[bytes, bytes, bytes]:
    return (
        _jcs_key(_bounded_text(path, "release strip path", maximum=4096)),
        _jcs_key(_bounded_text(ref, "release strip ref", maximum=4096)),
        _jcs_key(_bounded_text(title, "release strip title", maximum=1024)),
    )


def _canonical_full_fields(value: Mapping[str, str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    if not isinstance(value, Mapping):
        raise ProjectionCanonicalizationError("full search fields must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise ProjectionCanonicalizationError("search field names must be strings")
    for key in sorted(value, key=_jcs_key):
        fields[_bounded_text(key, "search field name", maximum=128)] = _bounded_text(
            value[key], f"search field {key}", maximum=1_048_576
        )
    return fields


def _fixed_search_fields(
    decision: Decision,
    full_search_fields: Mapping[str, str],
) -> tuple[int, dict[str, str]] | None:
    level = decision.level
    if type(level) is not int or not 0 <= level <= 6:
        raise ProjectionCanonicalizationError("decision level is outside L0-L6")
    if level == 0:
        return None
    options = _canonical_options(decision.options)
    fields = _canonical_full_fields(full_search_fields)
    if decision.release_strip:
        stripped = bridges.strip_provenance(fields, tuple(decision.release_strip))
        if not isinstance(stripped, Mapping):
            return None
        fields = _canonical_full_fields(
            {key: value for key, value in stripped.items() if isinstance(key, str) and isinstance(value, str)}
        )
    for candidate_level in range(level, 0, -1):
        if candidate_level == 6 and fields:
            return candidate_level, fields
        if candidate_level == 5:
            body = fields.get("body")
            excerpt = fixed_excerpt(body) if body is not None else ""
            if excerpt:
                return candidate_level, {"body": excerpt}
        elif candidate_level == 4:
            abstraction = decision.bridge_abstraction
            if (
                isinstance(abstraction, str)
                and abstraction
                and decision.release_grant_id
                and decision.release_dependency_digest
            ):
                return candidate_level, {"bridge": abstraction}
        elif candidate_level == 3:
            abstract = options.get("abstract")
            if isinstance(abstract, str):
                return candidate_level, {"abstract": abstract}
        elif candidate_level == 2:
            constraint = options.get("constraint")
            if isinstance(constraint, str):
                return candidate_level, {"constraint": constraint}
        elif candidate_level == 1:
            notice = decision.notice or options.get("notice")
            if isinstance(notice, str) and notice:
                return candidate_level, {"notice": notice}
    return None


def build_projection_variant(
    *,
    item_identity: str,
    content_hash: str,
    decision: Decision,
    projector_schema_version: int,
    full_search_fields: Mapping[str, str],
) -> ProjectionVariant | None:
    """Build one immutable non-L0 variant, lowering missing content to absence."""

    identity = _bounded_text(item_identity, "item_identity", maximum=4096)
    digest = _digest(content_hash, "content_hash")
    if type(projector_schema_version) is not int or projector_schema_version <= 0:
        raise ProjectionCanonicalizationError("projector_schema_version must be positive")
    canonical_strip = _canonical_strip(decision.release_strip)
    fixed = _fixed_search_fields(decision, full_search_fields)
    if fixed is None:
        return None
    projected_level, fields = fixed

    bridge_id: str | None = None
    bridge_dependency: str | None = None
    if projected_level == 4:
        bridge_id = _bounded_text(decision.release_grant_id, "bridge_id", maximum=4096)
        bridge_dependency = _digest(
            decision.release_dependency_digest,
            "bridge_dependency_content_hash",
        )

    value = {
        "item_identity": identity,
        "content_hash": digest,
        "decision_level": projected_level,
        "options": _options_at_level(decision.options, projected_level),
        "release_strip": canonical_strip,
        "bridge_id": bridge_id,
        "bridge_dependency_content_hash": bridge_dependency,
        "projector_schema_version": projector_schema_version,
    }
    value_jcs = canonical_jcs(value)
    variant_id = hashlib.sha256(_VARIANT_DOMAIN + value_jcs).hexdigest()
    return ProjectionVariant(
        projection_variant_id=variant_id,
        item_identity=identity,
        content_hash=digest,
        decision_level=projected_level,
        value_jcs=value_jcs,
        search_fields=fields,
    )


def deduplicate_variants(
    variants: Iterable[ProjectionVariant],
) -> tuple[ProjectionVariant, ...]:
    """Return variant-id order, refusing collision or per-item overflow."""

    unique: dict[str, ProjectionVariant] = {}
    item_identity: str | None = None
    for variant in variants:
        if not isinstance(variant, ProjectionVariant):
            raise ProjectionCanonicalizationError("projection variant has an invalid type")
        if item_identity is None:
            item_identity = variant.item_identity
        elif variant.item_identity != item_identity:
            raise ProjectionCanonicalizationError("variant set crosses item identities")
        prior = unique.get(variant.projection_variant_id)
        if prior is not None and prior != variant:
            raise ProjectionVariantMismatch("projection variant identity collision")
        unique[variant.projection_variant_id] = variant
        if len(unique) > MAX_PROJECTION_VARIANTS_PER_ITEM:
            raise ProjectionVariantOverflow(
                f"item exceeds {MAX_PROJECTION_VARIANTS_PER_ITEM} projection variants"
            )
    return tuple(unique[key] for key in sorted(unique))


DecisionResolver = Callable[[str, str | None, Decision], Decision]


def _unlisted_equivalence_class(existing: set[str], label: str) -> str:
    index = 0
    while True:
        candidate = f"projection-unlisted-{label}-{index}"
        if candidate not in existing:
            return candidate
        index += 1


def _resolved_variant(
    *,
    audience: str,
    purpose: str | None,
    decision: Decision,
    resolve_decision: DecisionResolver | None,
    item_identity: str,
    content_hash: str,
    projector_schema_version: int,
    full_search_fields: Mapping[str, str],
) -> ProjectionVariant | None:
    resolved = (
        resolve_decision(audience, purpose, decision)
        if resolve_decision is not None
        else decision
    )
    if not isinstance(resolved, Decision):
        raise ProjectionCanonicalizationError("decision resolver returned an invalid value")
    return build_projection_variant(
        item_identity=item_identity,
        content_hash=content_hash,
        decision=resolved,
        projector_schema_version=projector_schema_version,
        full_search_fields=full_search_fields,
    )


def enumerate_projection_variants(
    *,
    item_identity: str,
    content_hash: str,
    scope_ids: Iterable[str],
    policy: Policy,
    projector_schema_version: int,
    full_search_fields: Mapping[str, str],
    resolve_decision: DecisionResolver | None = None,
) -> tuple[ProjectionVariant, ...]:
    """Enumerate unique outputs from the finite compiled decision domain.

    Audience and purpose values are reduced to their compiled equivalence
    classes.  Session grants contribute only one exact scope and one of the
    seven disclosure ceilings.  After each scope, states are collapsed by the
    immutable variant they already produce; the conservative decision meet is
    associative, so equivalent prefixes remain equivalent when later scopes
    are added.  This avoids syntactic principal/grant products while retaining
    every reachable output and enforcing the 256-row cap during construction.
    """

    if not isinstance(policy, Policy) or policy.empty or policy.blocked:
        raise ProjectionCanonicalizationError("projection enumeration requires an active policy")
    scopes = tuple(sorted(set(scope_ids)))
    if any(not isinstance(scope_id, str) or not scope_id for scope_id in scopes):
        raise ProjectionCanonicalizationError("scope_ids must be canonical strings")
    unknown_scopes = set(scopes) - set(policy.scopes)
    if unknown_scopes:
        raise ProjectionCanonicalizationError("item membership names an unknown scope")

    audience_values = {rule.audience for rule in policy.rules}
    audience_values.update(grant.audience for grant in policy.grants)
    audience_values.add(OWNER_AUDIENCE)
    audience_values.add(_unlisted_equivalence_class(audience_values, "audience"))

    authored_purposes = {
        rule.purpose for rule in policy.rules if rule.purpose is not None
    }
    other_purpose = _unlisted_equivalence_class(authored_purposes, "purpose")
    purposes: tuple[str | None, ...] = (None, *sorted(authored_purposes), other_purpose)

    variants: list[ProjectionVariant] = []
    for audience in sorted(audience_values):
        for purpose in purposes:
            # One representative synthetic-grant tuple per distinct prefix
            # projection.  None is the L0/absent representative key.
            states: dict[str | None, tuple[StandingGrant, ...]] = {None: ()}
            for index, scope_id in enumerate(scopes):
                prefix = scopes[: index + 1]
                next_states: dict[str | None, tuple[StandingGrant, ...]] = {}
                for grants in states.values():
                    for ceiling in (None, *range(7)):
                        synthetic = grants
                        if ceiling is not None:
                            synthetic = (
                                *grants,
                                StandingGrant(
                                    id=f"projection-session-{index}-{ceiling}",
                                    source="authorization-projection-enumerator",
                                    scope_ids=(scope_id,),
                                    audience=audience,
                                    ceiling=ceiling,
                                ),
                            )
                        current = decide(
                            prefix,
                            audience=audience,
                            purpose=purpose,
                            policy=policy,
                            active_grants=(*policy.grants, *synthetic),
                        )
                        variant = _resolved_variant(
                            audience=audience,
                            purpose=purpose,
                            decision=current,
                            resolve_decision=resolve_decision,
                            item_identity=item_identity,
                            content_hash=content_hash,
                            projector_schema_version=projector_schema_version,
                            full_search_fields=full_search_fields,
                        )
                        key = None if variant is None else variant.projection_variant_id
                        next_states.setdefault(key, synthetic)
                non_l0 = sum(key is not None for key in next_states)
                if non_l0 > MAX_PROJECTION_VARIANTS_PER_ITEM:
                    raise ProjectionVariantOverflow(
                        f"item exceeds {MAX_PROJECTION_VARIANTS_PER_ITEM} projection variants"
                    )
                states = next_states

            if not scopes:
                current = decide(
                    (),
                    audience=audience,
                    purpose=purpose,
                    policy=policy,
                    active_grants=policy.grants,
                )
                variant = _resolved_variant(
                    audience=audience,
                    purpose=purpose,
                    decision=current,
                    resolve_decision=resolve_decision,
                    item_identity=item_identity,
                    content_hash=content_hash,
                    projector_schema_version=projector_schema_version,
                    full_search_fields=full_search_fields,
                )
                if variant is not None:
                    variants.append(variant)
            else:
                for grants in states.values():
                    current = decide(
                        scopes,
                        audience=audience,
                        purpose=purpose,
                        policy=policy,
                        active_grants=(*policy.grants, *grants),
                    )
                    variant = _resolved_variant(
                        audience=audience,
                        purpose=purpose,
                        decision=current,
                        resolve_decision=resolve_decision,
                        item_identity=item_identity,
                        content_hash=content_hash,
                        projector_schema_version=projector_schema_version,
                        full_search_fields=full_search_fields,
                    )
                    if variant is not None:
                        variants.append(variant)
            # Enforce the global per-item cap across every equivalence class,
            # not separately per audience or purpose.
            variants[:] = deduplicate_variants(variants)
    return deduplicate_variants(variants)


__all__ = [
    "MAX_PROJECTION_VARIANTS_PER_ITEM",
    "PROJECTOR_SCHEMA_VERSION",
    "MeasurementKey",
    "ProjectionCanonicalizationError",
    "ProjectionError",
    "ProjectionNamespaceKey",
    "ProjectionVariant",
    "ProjectionVariantMismatch",
    "ProjectionVariantOverflow",
    "build_projection_variant",
    "canonical_jcs",
    "deduplicate_variants",
    "enumerate_projection_variants",
    "fixed_excerpt",
]
