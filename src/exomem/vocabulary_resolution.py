"""Strict write-time resolution for the Notes experiment domain projection."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from yaml.resolver import BaseResolver

from . import source_taxonomy, vault
from .vault import kb_root


class _DuplicateTaxonomyKey(yaml.YAMLError):
    """A strict write registry cannot hide a YAML key behind a later value."""


class _StrictTaxonomyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise _DuplicateTaxonomyKey("domain taxonomy mapping key is invalid") from error
        if duplicate:
            raise _DuplicateTaxonomyKey("domain taxonomy has duplicate YAML keys")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictTaxonomyLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


class VocabularyResolutionError(ValueError):
    def __init__(self, code: str, reason: str, details: dict[str, Any] | None = None) -> None:
        self.code = code
        self.reason = reason
        self.details = details or {}
        super().__init__(f"{code}: {reason}")


@dataclass(frozen=True, slots=True)
class VocabularyPreparation:
    """A non-mutating nearby-meaning decision point with no destination."""

    evidence: dict[str, Any]
    deferred: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "mutated": False,
            "vocabulary_preparation": self.evidence,
            "vocabulary_decision": "deferred" if self.deferred else "required",
        }


@dataclass(frozen=True, slots=True)
class DomainBinding:
    requested: str
    canonical: str
    destination_folder: str
    match_kind: str
    snapshot: str
    registry_guard: vault.PathGuard
    parent_guard: vault.DirectoryCensusGuard

    @property
    def destination(self) -> str:
        return self.destination_folder

    def as_dict(self) -> dict[str, str]:
        return {
            "family": "domain",
            "requested": self.requested,
            "canonical": self.canonical,
            "destination": self.destination,
            "match_kind": self.match_kind,
            "snapshot": self.snapshot,
        }


def resolve_notes_domain(
    vault_root: Path,
    requested: object,
    *,
    decision: dict[str, Any] | None = None,
) -> DomainBinding:
    """Resolve one experiment domain against a strict registry snapshot.

    Source captures deliberately retain a permissive fallback. Notes creation is
    a distinct write boundary: a damaged or ambiguous registry cannot choose a
    destination here.
    """
    root = Path(vault_root)
    taxonomy, registry_guard, snapshot = _strict_taxonomy(root)
    try:
        resolved = taxonomy.resolve_domain(requested)
    except source_taxonomy.TaxonomyError as error:
        raise VocabularyResolutionError("INVALID_DOMAIN", str(error)) from error
    requested_text = str(requested)
    canonical = resolved.key
    match_kind = _match_kind(requested_text, resolved)
    candidates = _nearby_definitions(taxonomy, requested_text, resolved)
    if resolved.status == "unregistered" and candidates:
        evidence = _preparation_evidence(taxonomy, requested_text, candidates, snapshot)
        if decision is None:
            raise VocabularyResolutionError(
                "VOCABULARY_DECISION_REQUIRED",
                "nearby domain meanings require an explicit vocabulary decision",
                {"vocabulary_preparation": evidence},
            )
        canonical, match_kind = _apply_decision(taxonomy, resolved, decision, evidence)
    elif decision is not None:
        raise VocabularyResolutionError(
            "VOCABULARY_DECISION_NOT_APPLICABLE",
            "a vocabulary decision is only valid for a nearby domain preparation",
        )

    definition = taxonomy.domains.get(canonical)
    folder = definition.path_label if definition is not None else source_taxonomy.derive_path_label(canonical)
    parent = kb_root(root) / "Notes" / "Experiments"
    parent_guard = vault.DirectoryCensusGuard.capture(
        root, parent.relative_to(root).as_posix(), max_entries=256
    )
    existing = _existing_projection_spelling(
        parent,
        canonical=canonical,
        path_label=folder,
        aliases=definition.aliases if definition is not None else (),
    )
    parent_guard.recheck(root)
    return DomainBinding(
        requested=requested_text,
        canonical=canonical,
        destination_folder=existing or folder,
        match_kind=match_kind,
        snapshot=snapshot,
        registry_guard=registry_guard,
        parent_guard=parent_guard,
    )


def binding_from_dict(vault_root: Path, value: object) -> DomainBinding:
    """Re-resolve an encoded binding and refuse a changed snapshot or target."""
    if not isinstance(value, dict) or set(value) != {
        "family", "requested", "canonical", "destination", "match_kind", "snapshot"
    }:
        raise VocabularyResolutionError("INVALID_DRAFT_TOKEN", "draft token has invalid vocabulary binding")
    if value["family"] != "domain" or not all(
        isinstance(value[key], str) and value[key]
        for key in ("requested", "canonical", "destination", "match_kind", "snapshot")
    ):
        raise VocabularyResolutionError("INVALID_DRAFT_TOKEN", "draft token has invalid vocabulary binding")
    decision = None
    if value["match_kind"] == "decision-create":
        decision = {"evidence_fingerprint": None, "outcome": "create", "canonical": value["canonical"]}
    elif value["match_kind"] == "decision-reuse":
        decision = {"evidence_fingerprint": None, "outcome": "reuse", "canonical": value["canonical"]}
    binding = _resolve_bound_notes_domain(Path(vault_root), value["requested"], decision, value["snapshot"])
    if binding.as_dict() != value:
        raise VocabularyResolutionError(
            "STALE_VOCABULARY_BINDING", "domain vocabulary changed; validate a fresh draft"
        )
    return binding


def _resolve_bound_notes_domain(
    vault_root: Path, requested: str, decision: dict[str, Any] | None, snapshot: str
) -> DomainBinding:
    """Rebuild a binding from its stored snapshot without accepting a new choice."""
    root = Path(vault_root)
    taxonomy, registry_guard, current_snapshot = _strict_taxonomy(root)
    if current_snapshot != snapshot:
        raise VocabularyResolutionError("STALE_VOCABULARY_BINDING", "domain vocabulary changed; validate a fresh draft")
    resolved = taxonomy.resolve_domain(requested)
    canonical = resolved.key
    match_kind = _match_kind(requested, resolved)
    candidates = _nearby_definitions(taxonomy, requested, resolved)
    if resolved.status == "unregistered" and candidates:
        if decision is None:
            raise VocabularyResolutionError("STALE_VOCABULARY_BINDING", "domain decision requires fresh validation")
        outcome = decision["outcome"]
        if outcome == "create" and decision["canonical"] == resolved.key:
            canonical, match_kind = resolved.key, "decision-create"
        elif outcome == "reuse" and decision["canonical"] in taxonomy.domains:
            canonical, match_kind = decision["canonical"], "decision-reuse"
        else:
            raise VocabularyResolutionError("STALE_VOCABULARY_BINDING", "domain decision requires fresh validation")
    definition = taxonomy.domains.get(canonical)
    folder = definition.path_label if definition is not None else source_taxonomy.derive_path_label(canonical)
    parent = kb_root(root) / "Notes" / "Experiments"
    parent_guard = vault.DirectoryCensusGuard.capture(root, parent.relative_to(root).as_posix(), max_entries=256)
    existing = _existing_projection_spelling(
        parent,
        canonical=canonical,
        path_label=folder,
        aliases=definition.aliases if definition is not None else (),
    )
    parent_guard.recheck(root)
    return DomainBinding(requested, canonical, existing or folder, match_kind, current_snapshot, registry_guard, parent_guard)


def _strict_taxonomy(root: Path) -> tuple[source_taxonomy.SourceTaxonomy, vault.PathGuard, str]:
    path = source_taxonomy.registry_path(root)
    relative = path.relative_to(root).as_posix()
    try:
        path.lstat()
    except FileNotFoundError:
        return (
            source_taxonomy.core_taxonomy(),
            vault.PathGuard.capture(root, relative, leaf_policy="absent"),
            _snapshot({"registry": "missing"}),
        )
    except OSError as error:
        raise VocabularyResolutionError(
            "INVALID_DOMAIN_TAXONOMY", "domain taxonomy is unreadable"
        ) from error
    try:
        raw, guard = vault.read_guarded_text(root, path)
        data = yaml.load(raw, Loader=_StrictTaxonomyLoader)
    except (OSError, UnicodeError, vault.PathGuardError) as error:
        raise VocabularyResolutionError(
            "INVALID_DOMAIN_TAXONOMY", "domain taxonomy is unreadable"
        ) from error
    except yaml.YAMLError as error:
        raise VocabularyResolutionError(
            "INVALID_DOMAIN_TAXONOMY", "domain taxonomy is malformed"
        ) from error
    _validate_strict_domain_registry(data)
    _reject_duplicate_domain_owners(data)
    taxonomy = source_taxonomy.taxonomy_from_data(data)
    if taxonomy.findings:
        raise VocabularyResolutionError(
            "INVALID_DOMAIN_TAXONOMY", "domain taxonomy is malformed", {"findings": list(taxonomy.findings)}
        )
    _reject_equivalent_owners(taxonomy)
    return taxonomy, guard, _snapshot({"registry": raw, "domains": _domain_snapshot(taxonomy)})


def _validate_strict_domain_registry(data: object) -> None:
    if not isinstance(data, dict) or not isinstance(data.get("domains"), dict):
        raise VocabularyResolutionError("INVALID_DOMAIN_TAXONOMY", "domain taxonomy is malformed")
    for key, definition in data["domains"].items():
        if not isinstance(key, str) or not isinstance(definition, dict):
            raise VocabularyResolutionError("INVALID_DOMAIN_TAXONOMY", "domain taxonomy is malformed")
        if "aliases" in definition and (
            not isinstance(definition["aliases"], list)
            or any(not isinstance(alias, str) for alias in definition["aliases"])
        ):
            raise VocabularyResolutionError("INVALID_DOMAIN_TAXONOMY", "domain taxonomy is malformed")


def _reject_duplicate_domain_owners(data: object) -> None:
    assert isinstance(data, dict) and isinstance(data["domains"], dict)
    canonical_owners: dict[str, str] = {}
    alias_owners: dict[str, str] = {}
    for raw_key, entry in data["domains"].items():
        try:
            key = source_taxonomy.normalize(raw_key, axis="domain")
        except source_taxonomy.TaxonomyError as error:
            raise VocabularyResolutionError("INVALID_DOMAIN_TAXONOMY", str(error)) from error
        if key in canonical_owners:
            raise VocabularyResolutionError("INVALID_DOMAIN_TAXONOMY", "duplicate canonical domain owner")
        canonical_owners[key] = str(raw_key)
        aliases = entry.get("aliases") if isinstance(entry, dict) else []
        if isinstance(aliases, str):
            aliases = [aliases]
        if not isinstance(aliases, (list, tuple)):
            continue
        for raw_alias in aliases:
            try:
                alias = source_taxonomy.normalize(raw_alias, axis="domain alias")
            except source_taxonomy.TaxonomyError as error:
                raise VocabularyResolutionError("INVALID_DOMAIN_TAXONOMY", str(error)) from error
            owner = alias_owners.get(alias)
            if owner is not None and owner != key:
                raise VocabularyResolutionError("INVALID_DOMAIN_TAXONOMY", "duplicate domain alias owner")
            alias_owners[alias] = key


def _existing_projection_spelling(
    parent: Path, *, canonical: str, path_label: str, aliases: tuple[str, ...]
) -> str | None:
    if not parent.exists():
        return None
    equivalents: list[str] = []
    for child in parent.iterdir():
        if not child.is_dir():
            continue
        try:
            normalized = source_taxonomy.normalize(child.name, axis="domain path")
        except source_taxonomy.TaxonomyError:
            normalized = ""
        if (
            normalized == canonical
            or normalized in aliases
            or _fold(child.name) == _fold(path_label)
        ):
            equivalents.append(child.name)
    if len(equivalents) > 1:
        raise VocabularyResolutionError(
            "AMBIGUOUS_DOMAIN_DESTINATION", "multiple equivalent experiment domain folders exist"
        )
    return equivalents[0] if equivalents else None


def _match_kind(requested: str, resolution: source_taxonomy.Resolution) -> str:
    if requested.strip() == resolution.key:
        return "exact"
    if resolution.status == "alias":
        return "alias"
    return "normalized" if resolution.status != "unregistered" else "open"


def _preparation_evidence(
    taxonomy: source_taxonomy.SourceTaxonomy, requested: str, candidates: tuple[str, ...], snapshot: str
) -> dict[str, Any]:
    body = {
        "family": "domain",
        "requested": requested,
        "candidates": [
            {
                "canonical": key,
                "label": taxonomy.domains[key].label,
                "description": taxonomy.domains[key].description,
            }
            for key in candidates
        ],
        "representative_usage": {"state": "unavailable", "items": []},
        "snapshot": snapshot,
    }
    body["evidence_fingerprint"] = _snapshot(body)
    return body


def _apply_decision(
    taxonomy: source_taxonomy.SourceTaxonomy,
    requested: source_taxonomy.Resolution,
    decision: dict[str, Any],
    evidence: dict[str, Any],
) -> tuple[str, str]:
    if not isinstance(decision, dict) or set(decision) - {"evidence_fingerprint", "outcome", "canonical"}:
        raise VocabularyResolutionError("INVALID_VOCABULARY_DECISION", "vocabulary decision is invalid")
    if decision.get("evidence_fingerprint") != evidence["evidence_fingerprint"]:
        raise VocabularyResolutionError("STALE_VOCABULARY_DECISION", "vocabulary evidence changed")
    outcome = decision.get("outcome")
    if outcome == "defer":
        raise VocabularyResolutionError("VOCABULARY_DEFERRED", "domain creation was deferred")
    if outcome == "reuse" and isinstance(decision.get("canonical"), str):
        canonical = decision["canonical"]
        if canonical in {item["canonical"] for item in evidence["candidates"]}:
            return canonical, "decision-reuse"
    if outcome == "create" and decision.get("canonical") == requested.key:
        return requested.key, "decision-create"
    raise VocabularyResolutionError("INVALID_VOCABULARY_DECISION", "vocabulary decision does not match preparation")


def _domain_snapshot(taxonomy: source_taxonomy.SourceTaxonomy) -> dict[str, Any]:
    return {
        key: {"label": value.label, "path_label": value.path_label, "aliases": list(value.aliases)}
        for key, value in sorted(taxonomy.domains.items())
    }


def _snapshot(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _fold(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _nearby_definitions(
    taxonomy: source_taxonomy.SourceTaxonomy,
    requested: str,
    resolution: source_taxonomy.Resolution,
) -> tuple[str, ...]:
    """Return bounded lexical definition overlap; it never chooses a meaning."""
    requested_terms = set(_terms(requested))
    candidates: list[tuple[int, str]] = []
    for key, definition in taxonomy.domains.items():
        terms = set(_terms(" ".join((key, definition.label, definition.description, *definition.aliases))))
        overlap = len(requested_terms & terms)
        if key == resolution.close_match:
            overlap = max(overlap, 1)
        if overlap:
            candidates.append((-overlap, key))
    return tuple(key for _score, key in sorted(candidates)[:8])


def _terms(value: str) -> tuple[str, ...]:
    return tuple(part for part in re.findall(r"[a-z0-9]+", _fold(value)) if len(part) > 2)


def _reject_equivalent_owners(taxonomy: source_taxonomy.SourceTaxonomy) -> None:
    canonicals = set(taxonomy.domains)
    aliases: dict[str, str] = {}
    for key, definition in taxonomy.domains.items():
        for alias in definition.aliases:
            owner = aliases.get(alias)
            if (owner is not None and owner != key) or (alias in canonicals and alias != key):
                raise VocabularyResolutionError(
                    "INVALID_DOMAIN_TAXONOMY", "domain aliases have ambiguous owners"
                )
            aliases[alias] = key


def valid_public_resolution(value: object) -> bool:
    """Validate the bounded domain identity record admitted to public receipts."""
    if not isinstance(value, dict) or set(value) != {
        "family", "requested", "canonical", "destination", "match_kind", "snapshot"
    }:
        return False
    return (
        value["family"] == "domain"
        and all(isinstance(value[key], str) and value[key] for key in value if key != "family")
        and len(value["requested"]) <= 256
        and len(value["destination"]) <= 256
        and len(value["snapshot"]) == 64
    )
