"""Optional corpus-inferred contracts for governed knowledge patterns."""

from __future__ import annotations

import datetime as dt
import math
import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import (
    epistemic_graph,
    relation_registry,
    semantic_language_registry,
    semantic_units,
    traversal_profiles,
    vault,
)
from . import find as find_module
from .kbdir import kb_dirname

SCHEMA_VERSION = 1
MIN_REQUIRED_SAMPLE = 5
CONTRACT_RELATION_ORIGINS = frozenset({"semantic_relation", "markdown_relation"})
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_VALIDATION_MODES = frozenset({"off", "warn", "strict"})
_UNKNOWN_POLICIES = frozenset({"allow", "forbid"})
_TYPE_ORDER = (
    "null",
    "boolean",
    "integer",
    "number",
    "date",
    "array",
    "object",
    "string",
)
_TYPE_INDEX = {value: index for index, value in enumerate(_TYPE_ORDER)}
_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "name",
        "scope",
        "validation",
        "sample_size",
        "fields",
        "blocks",
        "kinds",
        "categories",
        "relations",
        "unknown_fields",
        "unknown_blocks",
        "unknown_kinds",
        "unknown_categories",
        "unknown_relations",
    }
)
_NAMESPACES = ("fields", "blocks", "kinds", "categories", "relations")
_SPECIFICITY_LABELS = {
    0: "global",
    1: "page_type",
    2: "project",
    3: "project+page_type",
}


@dataclass(frozen=True)
class ContractScope:
    project: str | None = None
    page_type: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {"project": self.project, "page_type": self.page_type}


@dataclass(frozen=True)
class MemoryContract:
    name: str
    scope: ContractScope
    sample_size: int
    fields: dict[str, dict[str, Any]]
    blocks: dict[str, dict[str, Any]]
    relations: dict[str, dict[str, Any]]
    validation: str = "warn"
    kinds: dict[str, dict[str, Any]] | None = None
    categories: dict[str, dict[str, Any]] | None = None
    unknown_fields: str = "allow"
    unknown_blocks: str = "allow"
    unknown_kinds: str = "allow"
    unknown_categories: str = "allow"
    unknown_relations: str = "allow"

    def __post_init__(self) -> None:
        object.__setattr__(self, "kinds", dict(self.kinds or {}))
        object.__setattr__(self, "categories", dict(self.categories or {}))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "name": self.name,
            "scope": self.scope.as_dict(),
            "validation": self.validation,
            "sample_size": self.sample_size,
            "fields": _sorted_rules(self.fields),
            "blocks": _sorted_rules(self.blocks),
            "kinds": _sorted_rules(self.kinds or {}),
            "categories": _sorted_rules(self.categories or {}),
            "relations": _sorted_rules(self.relations),
            "unknown_fields": self.unknown_fields,
            "unknown_blocks": self.unknown_blocks,
            "unknown_kinds": self.unknown_kinds,
            "unknown_categories": self.unknown_categories,
            "unknown_relations": self.unknown_relations,
        }


@dataclass(frozen=True)
class LoadedMemoryContract:
    contract: MemoryContract
    path: str
    content_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract.as_dict(),
            "path": self.path,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True)
class ResolvedContractConstraint:
    namespace: str
    element: str
    constraint: str
    value: Any
    specificity: str
    contracts: tuple[str, ...]
    provenance: tuple[tuple[str, str, str, str], ...]

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.namespace, self.element, self.constraint)

    @property
    def resolved_rule(self) -> tuple[str, str, str]:
        return self.identity

    def as_dict(self) -> dict[str, Any]:
        return {
            "resolved_rule": list(self.identity),
            "value": _json_value(self.value),
            "specificity": self.specificity,
            "contracts": list(self.contracts),
            "provenance": [
                {
                    "contract": contract,
                    "path": path,
                    "raw_element": raw_element,
                    "scope": scope,
                }
                for contract, path, raw_element, scope in self.provenance
            ],
        }


@dataclass(frozen=True)
class ContractResolutionConflict:
    code: str
    resolved_rule: tuple[str, str, str]
    contracts: tuple[str, ...]
    detail: str
    provenance: tuple[tuple[str, str, str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "resolved_rule": list(self.resolved_rule),
            "contracts": list(self.contracts),
            "detail": self.detail,
            "provenance": [
                {
                    "contract": contract,
                    "path": path,
                    "raw_element": raw_element,
                    "scope": scope,
                }
                for contract, path, raw_element, scope in self.provenance
            ],
        }


@dataclass(frozen=True)
class ResolvedMemoryContracts:
    validation: str | None
    matched_contracts: tuple[tuple[str, str], ...]
    constraints: tuple[ResolvedContractConstraint, ...]
    conflicts: tuple[ContractResolutionConflict, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "validation": self.validation,
            "matched_contracts": [
                {"name": name, "path": path} for name, path in self.matched_contracts
            ],
            "constraints": [item.as_dict() for item in self.constraints],
            "conflicts": [item.as_dict() for item in self.conflicts],
        }


def infer_contract(
    vault_root: Path,
    *,
    name: str,
    project: str | None = None,
    page_type: str | None = None,
) -> dict[str, Any]:
    name = _validate_name(name)
    pages = _select_pages(vault_root, ContractScope(project=project, page_type=page_type))
    sample_size = len(pages)
    field_counts: Counter[str] = Counter()
    field_types: dict[str, Counter[str]] = {}
    field_values: dict[str, Counter[str]] = {}
    block_counts: Counter[str] = Counter()
    kind_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    category_authored_keys: dict[str, set[str]] = {}
    relation_counts: Counter[str] = Counter()
    relation_definitions = relation_registry.load_registry(vault_root)
    language = semantic_language_registry.load_registry(vault_root)

    for page in pages:
        for key, value in page.frontmatter.items():
            key = str(key)
            field_counts[key] += 1
            field_types.setdefault(key, Counter())[_value_type(value)] += 1
            if isinstance(value, (str, bool, int, float, dt.date)):
                field_values.setdefault(key, Counter())[str(value)] += 1
        projects = tuple(sorted(_page_projects(page.frontmatter)))
        document = semantic_units.parse_semantic_units(
            page.body,
            path=page.rel_path,
            validate=False,
            language_registry=_AllAttachedProjectsRegistry(language, projects),
            relation_registry=relation_definitions,
            include_legacy_relations=True,
            retain_unknown_relations=True,
            project=None,
            page_type=page.page_type,
        )
        page_blocks = {unit.kind for unit in document.rich_units}
        page_kinds = {unit.kind for unit in document.units}
        page_categories = {unit.category for unit in document.units}
        for unit in document.units:
            category_authored_keys.setdefault(unit.category, set()).add(
                unit.category_key
            )
        page_relations = _page_relations(
            vault_root, page, document, registry=relation_definitions
        )
        block_counts.update(page_blocks)
        kind_counts.update(page_kinds)
        category_counts.update(page_categories)
        relation_counts.update(page_relations)

    fields: dict[str, dict[str, Any]] = {}
    field_profile: dict[str, dict[str, Any]] = {}
    for key in sorted(field_counts):
        count = field_counts[key]
        types = sorted(field_types[key], key=_TYPE_INDEX.__getitem__)
        values = [value for value, _ in field_values.get(key, Counter()).most_common(20)]
        rule: dict[str, Any] = {
            "required": sample_size >= MIN_REQUIRED_SAMPLE and count == sample_size,
            "types": types,
        }
        if (
            sample_size >= MIN_REQUIRED_SAMPLE
            and count == sample_size
            and types == ["string"]
            and 1 < len(values) <= 10
        ):
            rule["enum"] = sorted(values)
        fields[key] = rule
        field_profile[key] = _frequency(count, sample_size, types=types, values=values)

    blocks = {
        key: {
            "required": sample_size >= MIN_REQUIRED_SAMPLE and count == sample_size,
        }
        for key, count in sorted(block_counts.items())
    }
    kinds = {
        key: {
            "required": sample_size >= MIN_REQUIRED_SAMPLE and count == sample_size,
        }
        for key, count in sorted(kind_counts.items())
    }
    categories = {
        key: {
            "required": sample_size >= MIN_REQUIRED_SAMPLE and count == sample_size,
        }
        for key, count in sorted(category_counts.items())
    }
    relations = {
        key: {
            "required": sample_size >= MIN_REQUIRED_SAMPLE and count == sample_size,
        }
        for key, count in sorted(relation_counts.items())
    }
    contract = MemoryContract(
        name=name,
        scope=ContractScope(project=project, page_type=page_type),
        sample_size=sample_size,
        fields=fields,
        blocks=blocks,
        kinds=kinds,
        categories=categories,
        relations=relations,
    )
    return {
        "sample_size": sample_size,
        "matched_paths": [page.rel_path for page in pages],
        "frequencies": {
            "fields": field_profile,
            "blocks": {
                key: _frequency(count, sample_size) for key, count in sorted(block_counts.items())
            },
            "kinds": {
                key: _frequency(count, sample_size)
                for key, count in sorted(kind_counts.items())
            },
            "categories": {
                key: _frequency(
                    count,
                    sample_size,
                    authored_keys=sorted(category_authored_keys.get(key, set())),
                )
                for key, count in sorted(category_counts.items())
            },
            "relations": {
                key: _frequency(count, sample_size)
                for key, count in sorted(relation_counts.items())
            },
        },
        "required_threshold": {
            "minimum_sample": MIN_REQUIRED_SAMPLE,
            "presence": 1.0,
            "eligible": sample_size >= MIN_REQUIRED_SAMPLE,
        },
        "proposal": contract.as_dict(),
    }


def infer_relation_registry(
    vault_root: Path,
    *,
    project: str | None = None,
    page_type: str | None = None,
    include_model_suggestions: bool = False,
) -> dict[str, Any]:
    """Profile explicit relation observations without assigning new semantics."""
    registry = relation_registry.load_registry(vault_root)
    pages, observations = _scan_relation_observations(
        vault_root, project=project, page_type=page_type, registry=registry
    )
    grouped: dict[str, dict[str, Any]] = {}
    for item in observations:
        key = str(item["raw_relation"])
        entry = grouped.setdefault(
            key,
            {
                "raw_relation": key,
                "canonical": item["canonical"],
                "parent": item["parent"],
                "registry_status": item["registry_status"],
                "count": 0,
                "examples": [],
            },
        )
        entry["count"] += 1
        example = {
            "path": item["source_path"],
            "anchor": item["source_anchor"],
        }
        if example not in entry["examples"] and len(entry["examples"]) < 5:
            entry["examples"].append(example)
    counts = Counter(item["registry_status"] for item in observations)
    proposal = relation_registry_proposal(registry)
    for item in grouped.values():
        raw = item["raw_relation"]
        if item["registry_status"] == "unregistered" and "." in raw:
            proposal["extensions"].setdefault(raw, {"parent": None, "description": None})
    warnings: list[dict[str, str]] = []
    suggestions: list[dict[str, Any]] = []
    if include_model_suggestions:
        warnings.append(
            {
                "code": "model_suggestions_unavailable",
                "detail": (
                    "No optional relation suggestion model is configured; "
                    "deterministic inference is complete."
                ),
            }
        )
    return {
        "subject": "relations",
        "sample_size": len(pages),
        "observation_count": len(observations),
        "counts": {
            key: counts.get(key, 0)
            for key in (
                "core",
                "extension",
                "alias",
                "deprecated",
                "scope_violation",
                "unregistered",
            )
        },
        "relations": sorted(
            grouped.values(), key=lambda item: (-item["count"], item["raw_relation"])
        ),
        "proposal": proposal,
        "content_hash": registry.extension_hash,
        "warnings": warnings,
        "model_suggestions": suggestions,
        "model_suggestions_attribution": "optional model; response-only"
        if include_model_suggestions
        else None,
    }


def relation_observations(
    vault_root: Path,
    *,
    project: str | None = None,
    page_type: str | None = None,
    registry: relation_registry.RelationRegistry | None = None,
) -> list[dict[str, Any]]:
    return _scan_relation_observations(
        vault_root,
        project=project,
        page_type=page_type,
        registry=registry,
    )[1]


def _scan_relation_observations(
    vault_root: Path,
    *,
    project: str | None = None,
    page_type: str | None = None,
    registry: relation_registry.RelationRegistry | None = None,
) -> tuple[list[Any], list[dict[str, Any]]]:
    registry = registry or relation_registry.load_registry(vault_root)
    language = semantic_language_registry.load_registry(vault_root)
    pages = _select_pages(vault_root, ContractScope(project, page_type))
    out: list[dict[str, Any]] = []
    for page in pages:
        page_project = next(iter(sorted(_page_projects(page.frontmatter))), None)
        document = semantic_units.parse_semantic_units(
            page.body,
            path=page.rel_path,
            validate=False,
            language_registry=language,
            relation_registry=registry,
            include_legacy_relations=True,
            retain_unknown_relations=True,
            project=page_project,
            page_type=page.page_type,
        )
        for unit in document.rich_units:
            for relation in unit.relations:
                raw = relation.raw.split(":", 1)[0].strip()
                resolution = registry.resolve(
                    raw,
                    project=page_project,
                    page_type=page.page_type,
                    source_kind=unit.kind,
                    origin="semantic_relation",
                )
                out.append(
                    _observation(
                        page.rel_path,
                        unit.anchor or f"line-{relation.line}",
                        raw,
                        resolution,
                    )
                )
        for relation in document.note_relations:
            raw = relation.kind
            resolution = registry.resolve(
                raw,
                project=page_project,
                page_type=page.page_type,
                source_kind="file",
                origin="semantic_relation",
            )
            out.append(
                _observation(page.rel_path, f"line-{relation.line}", raw, resolution)
            )
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in out:
        unique[(item["source_path"], item["source_anchor"], item["raw_relation"])] = item
    return pages, list(unique.values())


def validate_relation_registry(
    vault_root: Path,
    *,
    proposal: dict[str, Any] | None = None,
    project: str | None = None,
    page_type: str | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    registry = (
        relation_registry.load_registry(vault_root, proposal=proposal)
        if proposal is not None
        else relation_registry.load_registry(vault_root)
    )
    findings = list(registry.findings)
    observations = relation_observations(
        vault_root, project=project, page_type=page_type, registry=registry
    )
    for item in observations:
        if item["registry_status"] in {"unregistered", "deprecated", "scope_violation"}:
            findings.append(
                {
                    "code": item["registry_status"],
                    "path": item["source_path"],
                    "span": item["source_anchor"],
                    "severity": "warning"
                    if item["registry_status"] != "scope_violation"
                    else "error",
                    "detail": (
                        f"observed relation {item['raw_relation']!r} "
                        f"is {item['registry_status']}"
                    ),
                }
            )
    return {
        "subject": "relations",
        "valid": not any(item.get("severity") == "error" for item in findings),
        "strict": strict,
        "strict_failed": bool(strict and findings),
        "content_hash": registry.extension_hash,
        "findings": findings,
    }


def diff_relation_registries(
    before: relation_registry.RelationRegistry, after: relation_registry.RelationRegistry
) -> dict[str, Any]:
    before_defs = {key: value.as_dict() for key, value in before.extensions.items()}
    after_defs = {key: value.as_dict() for key, value in after.extensions.items()}
    common = set(before_defs) & set(after_defs)
    changed = {
        key: {"before": before_defs[key], "after": after_defs[key]}
        for key in sorted(common)
        if before_defs[key] != after_defs[key]
    }
    return {
        "subject": "relations",
        "changed": bool(set(before_defs) ^ set(after_defs) or changed),
        "changes": {
            "added": sorted(set(after_defs) - set(before_defs)),
            "removed": sorted(set(before_defs) - set(after_defs)),
            "modified": changed,
        },
    }


def relation_registry_proposal(registry: relation_registry.RelationRegistry) -> dict[str, Any]:
    extensions: dict[str, Any] = {}
    for key, item in registry.extensions.items():
        value: dict[str, Any] = {
            "parent": item.parent,
            "description": item.description,
        }
        if item.family and item.parent and item.family != registry.core[item.parent].family:
            value["family"] = item.family
        for field, candidate in (
            ("direction", item.direction),
            ("inverse", item.inverse),
            ("origins", sorted(item.origins)),
            ("aliases", list(item.aliases)),
            ("source_kinds", sorted(item.source_kinds)),
            ("target_kinds", sorted(item.target_kinds)),
            ("status", item.status),
            ("replaced_by", item.replaced_by),
        ):
            if candidate not in (None, [], "active", "directed", ["semantic_relation"]):
                value[field] = candidate
        scope = {}
        if item.projects:
            scope["projects"] = sorted(item.projects)
        if item.page_types:
            scope["page_types"] = sorted(item.page_types)
        if scope:
            value["scope"] = scope
        extensions[key] = value
    return {"schema_version": 1, "extensions": extensions}


def infer_category_registry(
    vault_root: Path,
    *,
    project: str | None = None,
    page_type: str | None = None,
) -> dict[str, Any]:
    """Profile authored category identity without proposing semantic equivalence."""
    registry = semantic_language_registry.load_registry(vault_root)
    pages, observations = _scan_category_observations(
        vault_root,
        project=project,
        page_type=page_type,
        registry=registry,
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in observations:
        grouped.setdefault(str(item["category_key"]), []).append(item)

    categories: list[dict[str, Any]] = []
    normalization_candidates: list[dict[str, Any]] = []
    for key in sorted(grouped):
        items = grouped[key]
        raw_forms = Counter(str(item["category_raw"]) for item in items)
        forms = Counter(str(item["form"]) for item in items)
        page_types = Counter(str(item["page_type"]) for item in items)
        projects = Counter(
            project_key
            for item in items
            for project_key in item["projects"]
        )
        resolved = Counter(str(item["resolved_category"]) for item in items)
        statuses = Counter(str(item["registry_status"]) for item in items)
        replacements = Counter(
            str(item["replacement"])
            for item in items
            if item["replacement"] is not None
        )
        examples = sorted(
            (
                {
                    "path": item["path"],
                    "line": item["line"],
                    "anchor": item["anchor"],
                    "raw_category": item["category_raw"],
                    "excerpt": item["excerpt"],
                    "excerpt_truncated": item["excerpt_truncated"],
                }
                for item in items
            ),
            key=lambda item: (
                item["path"],
                item["line"],
                item["anchor"] or "",
                item["raw_category"],
            ),
        )[:5]
        raw_form_map = dict(sorted(raw_forms.items()))
        canonical_collision = len(raw_form_map) > 1
        if canonical_collision:
            normalization_candidates.append(
                {
                    "category_key": key,
                    "raw_forms": list(raw_form_map),
                    "basis": "shared_authored_normalization",
                }
            )
        resolved_map = dict(sorted(resolved.items()))
        status_map = dict(sorted(statuses.items()))
        replacement_map = dict(sorted(replacements.items()))
        categories.append(
            {
                "category_key": key,
                "resolved_category": _single_counter_key(resolved),
                "registry_status": _single_counter_key(statuses),
                "replacement": _single_counter_key(replacements),
                "resolved_categories": resolved_map,
                "registry_statuses": status_map,
                "replacements": replacement_map,
                "unit_count": len(items),
                "page_count": len({item["path"] for item in items}),
                "raw_forms": raw_form_map,
                "canonical_collision": canonical_collision,
                "forms": dict(sorted(forms.items())),
                "page_types": dict(sorted(page_types.items())),
                "projects": dict(sorted(projects.items())),
                "examples": examples,
            }
        )

    return {
        "subject": "categories",
        "sample_size": len(pages),
        "page_count": len(pages),
        "unit_count": len(observations),
        "observation_count": len(observations),
        "categories": categories,
        "normalization_candidates": normalization_candidates,
        "explicit_alias_relationships": [
            {"alias": alias, "category": canonical, "basis": "reviewed_registry"}
            for alias, canonical in sorted(registry.category_aliases.items())
        ],
        "candidate_changes": [],
        "proposal": semantic_language_registry.registry_proposal(registry),
        "content_hash": registry.content_hash,
        "registry_findings": [item.as_dict() for item in registry.findings],
    }


def category_observations(
    vault_root: Path,
    *,
    project: str | None = None,
    page_type: str | None = None,
    registry: semantic_language_registry.SemanticLanguageRegistry | None = None,
) -> list[dict[str, Any]]:
    """Return deterministic compact and rich category occurrences."""
    return _scan_category_observations(
        vault_root,
        project=project,
        page_type=page_type,
        registry=registry,
    )[1]


@dataclass(frozen=True)
class _AllAttachedProjectsRegistry:
    registry: semantic_language_registry.SemanticLanguageRegistry
    projects: tuple[str, ...]

    @property
    def findings(self):
        return self.registry.findings

    def resolve_heading(
        self,
        raw: str,
        *,
        project: str | None = None,
        page_type: str | None = None,
    ) -> semantic_language_registry.LabelResolution:
        return self._resolve(self.registry.resolve_heading, raw, page_type=page_type)

    def resolve_category(
        self,
        raw: str,
        *,
        project: str | None = None,
        page_type: str | None = None,
    ) -> semantic_language_registry.LabelResolution:
        return self._resolve(self.registry.resolve_category, raw, page_type=page_type)

    def _resolve(self, resolver, raw: str, *, page_type: str | None):
        resolutions = [
            resolver(raw, project=project, page_type=page_type)
            for project in self.projects or (None,)
        ]
        return next(
            (
                resolution
                for resolution in resolutions
                if resolution.status != "scope_violation"
            ),
            resolutions[0],
        )


def _scan_category_observations(
    vault_root: Path,
    *,
    project: str | None = None,
    page_type: str | None = None,
    registry: semantic_language_registry.SemanticLanguageRegistry | None = None,
) -> tuple[list[Any], list[dict[str, Any]]]:
    registry = registry or semantic_language_registry.load_registry(vault_root)
    pages = _select_pages(vault_root, ContractScope(project, page_type))
    observations: list[dict[str, Any]] = []
    for page in pages:
        projects = tuple(sorted(_page_projects(page.frontmatter)))
        document = semantic_units.parse_semantic_units(
            page.body,
            path=page.rel_path,
            validate=False,
            language_registry=_AllAttachedProjectsRegistry(registry, projects),
            project=None,
            page_type=page.page_type,
        )
        for unit in document.units:
            resolution = _resolve_observed_category(
                registry,
                unit.category_raw,
                projects=projects,
                page_type=page.page_type,
                preferred_project=project,
            )
            excerpt, truncated = _bounded_excerpt(unit.content)
            observations.append(
                {
                    "category_raw": unit.category_raw,
                    "category_key": unit.category_key,
                    "resolved_category": resolution.resolved or unit.category_key,
                    "registry_status": resolution.status,
                    "replacement": resolution.replacement,
                    "form": unit.form,
                    "kind": unit.kind,
                    "path": page.rel_path,
                    "line": unit.line,
                    "anchor": unit.anchor,
                    "excerpt": excerpt,
                    "excerpt_truncated": truncated,
                    "page_type": page.page_type,
                    "projects": projects,
                }
            )
    return pages, sorted(
        observations,
        key=lambda item: (
            item["path"],
            item["line"],
            item["form"],
            item["category_key"],
        ),
    )


def validate_category_registry(
    vault_root: Path,
    *,
    proposal: dict[str, Any] | None = None,
    project: str | None = None,
    page_type: str | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    """Validate registry structure and observed deprecated/scoped categories."""
    registry = (
        semantic_language_registry.load_registry(proposal=proposal)
        if proposal is not None
        else semantic_language_registry.load_registry(vault_root)
    )
    findings: list[dict[str, Any]] = [item.as_dict() for item in registry.findings]
    if not registry.findings:
        for item in category_observations(
            vault_root,
            project=project,
            page_type=page_type,
            registry=registry,
        ):
            status = item["registry_status"]
            if status not in {"deprecated", "scope_violation"}:
                continue
            findings.append(
                {
                    "code": status,
                    "path": item["path"],
                    "span": item["anchor"] or f"line-{item['line']}",
                    "severity": "warning" if status == "deprecated" else "error",
                    "detail": (
                        f"observed category {item['category_raw']!r} is "
                        f"{status}"
                    ),
                }
            )
    findings.sort(
        key=lambda item: (
            str(item.get("path", "")),
            str(item.get("span", "")),
            str(item.get("code", "")),
            str(item.get("detail", "")),
        )
    )
    return {
        "subject": "categories",
        "valid": not any(item.get("severity") == "error" for item in findings),
        "strict": strict,
        "strict_failed": bool(strict and findings),
        "content_hash": registry.content_hash,
        "findings": findings,
    }


def diff_category_registries(
    before: semantic_language_registry.SemanticLanguageRegistry,
    after: semantic_language_registry.SemanticLanguageRegistry,
) -> dict[str, Any]:
    """Diff reviewed categories and custom kinds as distinct namespaces."""
    before_proposal = semantic_language_registry.registry_proposal(before)
    after_proposal = semantic_language_registry.registry_proposal(after)
    changes = {
        namespace: _definition_changes(
            before_proposal[namespace], after_proposal[namespace]
        )
        for namespace in ("categories", "kinds")
    }
    return {
        "subject": "categories",
        "changed": any(_definition_changes_present(value) for value in changes.values()),
        "categories_changed": _definition_changes_present(changes["categories"]),
        "kinds_changed": _definition_changes_present(changes["kinds"]),
        "before_hash": before.content_hash,
        "after_hash": after.content_hash,
        "changes": changes,
    }


def _definition_changes(
    before: dict[str, Any], after: dict[str, Any]
) -> dict[str, Any]:
    common = set(before) & set(after)
    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "modified": {
            key: {"before": before[key], "after": after[key]}
            for key in sorted(common)
            if before[key] != after[key]
        },
    }


def _definition_changes_present(changes: dict[str, Any]) -> bool:
    return bool(changes["added"] or changes["removed"] or changes["modified"])


def _single_counter_key(values: Counter[str]) -> str | None:
    return next(iter(values)) if len(values) == 1 else None


def _resolve_observed_category(
    registry: semantic_language_registry.SemanticLanguageRegistry,
    raw: str,
    *,
    projects: tuple[str, ...],
    page_type: str,
    preferred_project: str | None,
) -> semantic_language_registry.LabelResolution:
    key = semantic_language_registry.normalize_label(raw)
    canonical = registry.category_aliases.get(key, key)
    definition = registry.categories.get(canonical)
    matching_projects = (
        sorted(definition.projects & set(projects)) if definition is not None else []
    )
    effective_project = (
        preferred_project
        if preferred_project in projects
        else matching_projects[0]
        if matching_projects
        else projects[0]
        if projects
        else preferred_project
    )
    if matching_projects and preferred_project not in matching_projects:
        effective_project = matching_projects[0]
    return registry.resolve_category(
        raw,
        project=effective_project,
        page_type=page_type,
    )


def _bounded_excerpt(value: str, limit: int = 160) -> tuple[str, bool]:
    excerpt = " ".join(str(value).split())
    if len(excerpt) <= limit:
        return excerpt, False
    return excerpt[: limit - 1].rstrip() + "…", True


def infer_traversal_profiles(vault_root: Path) -> dict[str, Any]:
    loaded = traversal_profiles.load_profiles(vault_root)
    path = traversal_profiles.profile_path(vault_root)
    if path.exists():
        try:
            proposal = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            proposal = {"schema_version": 1, "profiles": {}}
    else:
        proposal = {"schema_version": 1, "profiles": {}}
    return {
        "subject": "traversal-profiles",
        "profiles": {key: value.as_dict() for key, value in loaded.profiles.items()},
        "proposal": proposal,
        "content_hash": loaded.content_hash,
        "findings": list(loaded.findings),
    }


def _observation(
    path: str, anchor: str, raw: str, resolution: relation_registry.RelationResolution
) -> dict[str, Any]:
    return {
        "raw_relation": relation_registry.normalize_relation(raw),
        "canonical": resolution.canonical,
        "parent": resolution.parent,
        "registry_status": resolution.status,
        "source_path": path,
        "source_anchor": anchor,
    }


def save_contract(
    vault_root: Path,
    contract: dict[str, Any],
    *,
    expected_hash: str | None = None,
) -> dict[str, Any]:
    parsed = contract_from_dict(contract)
    path = contract_path(vault_root, parsed.name)
    current_hash: str | None = None
    if path.exists():
        current = path.read_text(encoding="utf-8")
        current_hash = vault.content_hash(current)
        if expected_hash is None:
            raise ValueError(
                "CONTRACT_EXISTS: contract already exists; provide its current expected_hash"
            )
        if expected_hash != current_hash:
            raise ValueError(
                f"STALE_CONTRACT: expected_hash {expected_hash!r} does not match current hash "
                f"{current_hash!r}"
            )
    rendered = yaml.safe_dump(
        parsed.as_dict(),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    vault.batch_atomic_write(
        [vault.PlannedWrite(path=path, content=rendered)], vault_root=vault_root
    )
    return {
        "path": path.relative_to(vault_root).as_posix(),
        "content_hash": vault.content_hash(rendered),
        "previous_hash": current_hash,
        "created": current_hash is None,
    }


def load_contract(vault_root: Path, name: str) -> tuple[MemoryContract, str, str]:
    path = contract_path(vault_root, name)
    if path.parent.is_symlink():
        raise ValueError(
            "INVALID_CONTRACT: contracts directory must not be a symlink"
        )
    if path.is_symlink():
        raise ValueError(
            f"INVALID_CONTRACT: contract file {path.name!r} must not be a symlink"
        )
    if not path.exists():
        raise ValueError(f"CONTRACT_NOT_FOUND: no saved contract named {name!r}")
    if not path.is_file():
        raise ValueError(
            f"INVALID_CONTRACT: contract path {path.name!r} must be a regular file"
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(
            f"INVALID_CONTRACT: could not read {path.name}: {exc}"
        ) from exc
    try:
        data = _load_contract_yaml(raw)
    except yaml.YAMLError as exc:
        raise ValueError(f"INVALID_CONTRACT: could not parse {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"INVALID_CONTRACT: {path.name} must contain a YAML object")
    contract = contract_from_dict(data)
    if path.stem != contract.name:
        raise ValueError(
            f"INVALID_CONTRACT: filename {path.name!r} must exactly match "
            f"contract name {contract.name!r}"
        )
    return (
        contract,
        vault.content_hash(raw),
        path.relative_to(vault_root).as_posix(),
    )


def load_saved_contracts(vault_root: Path) -> tuple[LoadedMemoryContract, ...]:
    """Load every direct saved contract, failing closed on any bad YAML file."""
    vault_root = Path(vault_root)
    directory = vault_root / kb_dirname() / "_Schema" / "contracts"
    if directory.is_symlink():
        raise ValueError(
            "INVALID_CONTRACT: contracts directory must not be a symlink"
        )
    if not directory.exists():
        return ()
    if not directory.is_dir():
        raise ValueError("INVALID_CONTRACT: contracts path must be a directory")
    loaded: list[LoadedMemoryContract] = []
    logical_names: set[str] = set()
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if path.suffix != ".yaml":
            continue
        if path.is_symlink():
            raise ValueError(
                f"INVALID_CONTRACT: contract file {path.name!r} must not be a symlink"
            )
        if not path.is_file():
            raise ValueError(
                f"INVALID_CONTRACT: contract path {path.name!r} must be a regular file"
            )
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise ValueError(
                f"INVALID_CONTRACT: could not read {path.name}: {error}"
            ) from error
        try:
            data = _load_contract_yaml(raw)
        except yaml.YAMLError as error:
            raise ValueError(
                f"INVALID_CONTRACT: could not parse {path.name}: {error}"
            ) from error
        contract = contract_from_dict(data)
        if path.stem != contract.name:
            raise ValueError(
                f"INVALID_CONTRACT: filename {path.name!r} must exactly match "
                f"contract name {contract.name!r}"
            )
        logical = contract.name.casefold()
        if logical in logical_names:
            raise ValueError(
                f"INVALID_CONTRACT: duplicate logical contract name {contract.name!r}"
            )
        logical_names.add(logical)
        loaded.append(
            LoadedMemoryContract(
                contract=contract,
                path=path.relative_to(vault_root).as_posix(),
                content_hash=vault.content_hash(raw),
            )
        )
    return tuple(sorted(loaded, key=lambda item: item.path))


@dataclass(frozen=True)
class _ConstraintDeclaration:
    identity: tuple[str, str, str]
    value: Any
    specificity: int
    contract: str
    path: str
    raw_element: str

    @property
    def provenance(self) -> tuple[str, str, str, str]:
        return (
            self.contract,
            self.path,
            self.raw_element,
            _SPECIFICITY_LABELS[self.specificity],
        )


def resolve_contracts(
    contracts: tuple[LoadedMemoryContract, ...] | list[LoadedMemoryContract],
    *,
    projects: tuple[str, ...] | list[str],
    page_type: str | None,
    language_registry: semantic_language_registry.SemanticLanguageRegistry | None = None,
) -> ResolvedMemoryContracts:
    """Purely resolve matched contracts into independent deterministic rules."""
    registry = language_registry or semantic_language_registry.core_registry()
    project_keys = tuple(sorted({str(project) for project in projects}))
    ordered = tuple(
        sorted(contracts, key=lambda item: (item.contract.name, item.path))
    )
    identities = [item.contract.name.casefold() for item in ordered]
    if len(set(identities)) != len(identities):
        raise ValueError("INVALID_CONTRACT: duplicate loaded contract identity")

    matched: list[tuple[LoadedMemoryContract, int]] = []
    for loaded in ordered:
        specificity = _contract_specificity(
            loaded.contract, projects=project_keys, page_type=page_type
        )
        if specificity is not None:
            matched.append((loaded, specificity))

    declarations: dict[
        tuple[str, str, str], list[_ConstraintDeclaration]
    ] = {}
    conflicts: list[ContractResolutionConflict] = []
    validation_declarations: list[_ConstraintDeclaration] = []
    for loaded, specificity in matched:
        contract = loaded.contract
        validation_declarations.append(
            _ConstraintDeclaration(
                ("contract", "validation", "mode"),
                contract.validation,
                specificity,
                contract.name,
                loaded.path,
                "validation",
            )
        )
        canonical_elements: dict[str, dict[str, str]] = {}
        for namespace in _NAMESPACES:
            rules = getattr(contract, namespace) or {}
            canonical_elements[namespace] = {}
            for raw_element in sorted(rules):
                element, registry_conflict = _resolve_contract_element(
                    namespace,
                    raw_element,
                    registry=registry,
                    projects=project_keys,
                    page_type=page_type,
                    loaded=loaded,
                )
                canonical_elements[namespace][raw_element] = element
                rule = rules[raw_element]
                if registry_conflict is not None:
                    conflict_constraint = next(iter(sorted(rule)), "declaration")
                    conflicts.append(
                        ContractResolutionConflict(
                            code="CONTRACT_RULE_CONFLICT",
                            resolved_rule=(namespace, element, conflict_constraint),
                            contracts=(contract.name,),
                            detail=registry_conflict,
                            provenance=(
                                (
                                    contract.name,
                                    loaded.path,
                                    raw_element,
                                    _SPECIFICITY_LABELS[specificity],
                                ),
                            ),
                        )
                    )
                for constraint, value in sorted(rule.items()):
                    identity = (namespace, element, constraint)
                    declaration = _ConstraintDeclaration(
                        identity,
                        tuple(value) if isinstance(value, list) else value,
                        specificity,
                        contract.name,
                        loaded.path,
                        raw_element,
                    )
                    declarations.setdefault(identity, []).append(declaration)
            policy = getattr(contract, f"unknown_{namespace}")
            allowed: tuple[str, ...] | None = None
            if policy == "forbid":
                allowed = tuple(sorted(set(canonical_elements[namespace].values())))
            allowed_identity = (namespace, "*", "allowed")
            declarations.setdefault(allowed_identity, []).append(
                _ConstraintDeclaration(
                    allowed_identity,
                    allowed,
                    specificity,
                    contract.name,
                    loaded.path,
                    f"unknown_{namespace}",
                )
            )

    validation, validation_conflict = _resolve_validation(validation_declarations)
    if validation_conflict is not None:
        conflicts.append(validation_conflict)

    resolved_constraints: list[ResolvedContractConstraint] = []
    for identity in sorted(declarations):
        constraint, conflict = _resolve_constraint(identity, declarations[identity])
        if constraint is not None:
            resolved_constraints.append(constraint)
        if conflict is not None:
            conflicts.append(conflict)

    constraint_map = {item.identity: item for item in resolved_constraints}
    field_elements = sorted(
        {
            element
            for namespace, element, _ in constraint_map
            if namespace == "fields" and element != "*"
        }
    )
    for element in field_elements:
        types = constraint_map.get(("fields", element, "types"))
        enum = constraint_map.get(("fields", element, "enum"))
        if types is None or enum is None:
            continue
        if any(_value_type(item) in types.value for item in enum.value):
            continue
        conflicts.append(
            ContractResolutionConflict(
                code="CONTRACT_RULE_CONFLICT",
                resolved_rule=enum.identity,
                contracts=tuple(sorted(set((*types.contracts, *enum.contracts)))),
                detail=(
                    f"resolved enum for field {element!r} has no value permitted "
                    "by its resolved types"
                ),
                provenance=tuple(sorted((*types.provenance, *enum.provenance))),
            )
        )
    for namespace in _NAMESPACES:
        allowed = constraint_map.get((namespace, "*", "allowed"))
        if allowed is None or allowed.value is None:
            continue
        allowed_set = set(allowed.value)
        for constraint in resolved_constraints:
            if (
                constraint.namespace != namespace
                or constraint.constraint != "required"
                or constraint.element == "*"
                or constraint.value is not True
                or constraint.element in allowed_set
            ):
                continue
            provenance = tuple(sorted((*constraint.provenance, *allowed.provenance)))
            conflicts.append(
                ContractResolutionConflict(
                    code="CONTRACT_RULE_CONFLICT",
                    resolved_rule=constraint.identity,
                    contracts=tuple(
                        sorted(set((*constraint.contracts, *allowed.contracts)))
                    ),
                    detail=(
                        f"required {namespace} element {constraint.element!r} is "
                        "excluded by the resolved finite allowed set"
                    ),
                    provenance=provenance,
                )
            )

    return ResolvedMemoryContracts(
        validation=validation,
        matched_contracts=tuple(
            sorted((item.contract.name, item.path) for item, _ in matched)
        ),
        constraints=tuple(sorted(resolved_constraints, key=lambda item: item.identity)),
        conflicts=tuple(
            sorted(
                _dedupe_conflicts(conflicts),
                key=lambda item: (
                    item.code,
                    item.resolved_rule,
                    item.contracts,
                    item.detail,
                ),
            )
        ),
    )


def resolve_saved_contracts(
    vault_root: Path,
    *,
    projects: tuple[str, ...] | list[str],
    page_type: str | None,
) -> ResolvedMemoryContracts:
    """Load saved contracts and the language registry once, then resolve."""
    contracts = load_saved_contracts(vault_root)
    registry = semantic_language_registry.load_registry(vault_root)
    return resolve_contracts(
        contracts,
        projects=projects,
        page_type=page_type,
        language_registry=registry,
    )


def _contract_specificity(
    contract: MemoryContract,
    *,
    projects: tuple[str, ...],
    page_type: str | None,
) -> int | None:
    if contract.scope.project is not None and contract.scope.project not in projects:
        return None
    if contract.scope.page_type is not None and contract.scope.page_type != page_type:
        return None
    return int(contract.scope.project is not None) * 2 + int(
        contract.scope.page_type is not None
    )


def _resolve_contract_element(
    namespace: str,
    raw: str,
    *,
    registry: semantic_language_registry.SemanticLanguageRegistry,
    projects: tuple[str, ...],
    page_type: str | None,
    loaded: LoadedMemoryContract,
) -> tuple[str, str | None]:
    if namespace not in {"categories", "kinds"}:
        return raw, None
    resolver: Callable[..., semantic_language_registry.LabelResolution] = (
        registry.resolve_category
        if namespace == "categories"
        else registry.resolve_kind
    )
    resolutions = [
        resolver(raw, project=project, page_type=page_type)
        for project in projects or (None,)
    ]
    valid = [
        resolution
        for resolution in resolutions
        if resolution.status not in {"scope_violation", "registry_invalid"}
    ]
    canonical = {
        resolution.resolved or semantic_language_registry.normalize_label(raw)
        for resolution in valid
    }
    fallback = semantic_language_registry.normalize_label(raw)
    if len(canonical) == 1:
        return next(iter(canonical)), None
    if len(canonical) > 1:
        return fallback, (
            f"registry resolution for {namespace}.{raw} diverges across attached "
            f"projects in contract {loaded.contract.name!r}: {sorted(canonical)}"
        )
    statuses = sorted({resolution.status for resolution in resolutions})
    return fallback, (
        f"registry resolution for {namespace}.{raw} has no scope-valid result "
        f"across attached projects: {statuses}"
    )


def _resolve_validation(
    declarations: list[_ConstraintDeclaration],
) -> tuple[str | None, ContractResolutionConflict | None]:
    if not declarations:
        return "warn", None
    highest = max(item.specificity for item in declarations)
    selected = [item for item in declarations if item.specificity == highest]
    modes = sorted({str(item.value) for item in selected})
    if len(modes) == 1:
        return modes[0], None
    return None, ContractResolutionConflict(
        code="CONTRACT_VALIDATION_CONFLICT",
        resolved_rule=("contract", "validation", "mode"),
        contracts=tuple(sorted({item.contract for item in selected})),
        detail=f"equal-specificity validation modes conflict: {modes}",
        provenance=tuple(sorted(item.provenance for item in selected)),
    )


def _resolve_constraint(
    identity: tuple[str, str, str],
    declarations: list[_ConstraintDeclaration],
) -> tuple[ResolvedContractConstraint | None, ContractResolutionConflict | None]:
    highest = max(item.specificity for item in declarations)
    selected = sorted(
        (item for item in declarations if item.specificity == highest),
        key=lambda item: item.provenance,
    )
    values = [item.value for item in selected]
    constraint = identity[2]
    conflict_detail: str | None = None
    value: Any
    if constraint == "required":
        value = any(values)
    elif constraint == "types":
        intersection = set(values[0])
        for candidate in values[1:]:
            intersection &= set(candidate)
        value = tuple(sorted(intersection, key=_TYPE_INDEX.__getitem__))
        if not value:
            conflict_detail = "equal-specificity type constraints have an empty intersection"
    elif constraint == "enum":
        intersection = {
            _typed_scalar_identity(item, label="enum"): item for item in values[0]
        }
        for candidate in values[1:]:
            candidate_ids = {
                _typed_scalar_identity(item, label="enum") for item in candidate
            }
            intersection = {
                key: item for key, item in intersection.items() if key in candidate_ids
            }
        value = tuple(sorted(intersection.values(), key=_typed_scalar_sort_key))
        if not value:
            conflict_detail = "equal-specificity enum constraints have an empty intersection"
    elif constraint == "allowed":
        has_unbounded = any(item is None for item in values)
        has_finite = any(item is not None for item in values)
        if has_unbounded and has_finite:
            value = None
            conflict_detail = "equal-specificity allow and forbid policies are incompatible"
        elif has_unbounded:
            value = None
        else:
            intersection = set(values[0])
            for candidate in values[1:]:
                intersection &= set(candidate)
            value = tuple(sorted(intersection))
            distinct = {tuple(candidate) for candidate in values}
            if len(distinct) > 1 and not value:
                conflict_detail = "equal-specificity finite allowed sets have an empty intersection"
    else:
        unique = {_typed_scalar_identity(item, label=constraint) for item in values}
        value = values[0]
        if len(unique) > 1:
            conflict_detail = "equal-specificity scalar declarations are incompatible"

    contracts = tuple(sorted({item.contract for item in selected}))
    provenance = tuple(item.provenance for item in selected)
    if conflict_detail is not None:
        return None, ContractResolutionConflict(
            code="CONTRACT_RULE_CONFLICT",
            resolved_rule=identity,
            contracts=contracts,
            detail=conflict_detail,
            provenance=provenance,
        )
    return (
        ResolvedContractConstraint(
            namespace=identity[0],
            element=identity[1],
            constraint=identity[2],
            value=value,
            specificity=_SPECIFICITY_LABELS[highest],
            contracts=contracts,
            provenance=provenance,
        ),
        None,
    )


def _dedupe_conflicts(
    conflicts: list[ContractResolutionConflict],
) -> list[ContractResolutionConflict]:
    out: dict[tuple[Any, ...], ContractResolutionConflict] = {}
    for item in conflicts:
        key = (item.code, item.resolved_rule, item.contracts, item.detail, item.provenance)
        out[key] = item
    return list(out.values())


def validate_contract(vault_root: Path, contract: MemoryContract, *, strict: bool = False) -> dict:
    pages = _select_pages(vault_root, contract.scope)
    findings: list[dict[str, Any]] = []
    relations_registry = relation_registry.load_registry(vault_root)
    language_registry = semantic_language_registry.load_registry(vault_root)
    for page in pages:
        projects = tuple(sorted(_page_projects(page.frontmatter)))
        document = semantic_units.parse_semantic_units(
            page.body,
            path=page.rel_path,
            validate=False,
            language_registry=_AllAttachedProjectsRegistry(
                language_registry, projects
            ),
            relation_registry=relations_registry,
            include_legacy_relations=True,
            retain_unknown_relations=True,
            project=None,
            page_type=page.page_type,
        )
        blocks = {unit.kind for unit in document.rich_units}
        kinds = {unit.kind for unit in document.units}
        categories = {unit.category for unit in document.units}
        observed_raw_elements: dict[str, dict[str, set[tuple[str, str]]]] = {
            "blocks": {},
            "kinds": {},
            "categories": {},
        }
        for unit in document.rich_units:
            observed_raw_elements["blocks"].setdefault(unit.kind, set()).add(
                (unit.kind_raw, unit.kind_key)
            )
        for unit in document.units:
            observed_raw_elements["kinds"].setdefault(unit.kind, set()).add(
                (unit.kind_raw, unit.kind_key)
            )
            observed_raw_elements["categories"].setdefault(
                unit.category, set()
            ).add((unit.category_raw, unit.category_key))
        relations = _page_relations(
            vault_root, page, document, registry=relations_registry
        )
        canonical_rules: dict[str, dict[str, dict[str, Any]]] = {
            "blocks": contract.blocks,
            "relations": contract.relations,
        }
        canonical_rule_raw_elements: dict[str, dict[str, set[str]]] = {
            "blocks": {
                element: {element}
                for element, rule in contract.blocks.items()
                if rule.get("required")
            },
            "relations": {
                element: {element}
                for element, rule in contract.relations.items()
                if rule.get("required")
            },
        }
        for namespace, rules in (
            ("kinds", contract.kinds or {}),
            ("categories", contract.categories or {}),
        ):
            canonical_rules[namespace] = {}
            canonical_rule_raw_elements[namespace] = {}
            for raw_element in sorted(rules):
                element, registry_conflict = _resolve_contract_element(
                    namespace,
                    raw_element,
                    registry=language_registry,
                    projects=projects,
                    page_type=page.page_type,
                    loaded=LoadedMemoryContract(
                        contract,
                        f"contract:{contract.name}",
                        "",
                    ),
                )
                prior = canonical_rules[namespace].get(element)
                canonical_rules[namespace][element] = _merge_required_rules(
                    prior, rules[raw_element]
                )
                if rules[raw_element].get("required"):
                    canonical_rule_raw_elements[namespace].setdefault(
                        element, set()
                    ).add(raw_element)
                if registry_conflict is not None:
                    conflict_constraint = next(
                        iter(sorted(rules[raw_element])), "declaration"
                    )
                    findings.append(
                        _finding(
                            page.rel_path,
                            f"contract.{namespace}:{raw_element}",
                            registry_conflict,
                            "Revise the registry scope or contract rule.",
                            code="CONTRACT_REGISTRY_CONFLICT",
                            contract=contract.name,
                            resolved_rule=(namespace, element, conflict_constraint),
                            raw_element=raw_element,
                            element_key=semantic_language_registry.normalize_label(
                                raw_element
                            ),
                        )
                    )
        if contract.validation == "off":
            continue
        for field, rule in contract.fields.items():
            if rule.get("required") and field not in page.frontmatter:
                findings.append(
                    _finding(
                        page.rel_path,
                        f"frontmatter.{field}",
                        f"missing required frontmatter field `{field}`",
                        f"Add `{field}` to frontmatter or revise contract `{contract.name}`.",
                        code="CONTRACT_REQUIRED_FIELD",
                        contract=contract.name,
                        resolved_rule=("fields", field, "required"),
                        raw_element=field,
                    )
                )
                continue
            if field not in page.frontmatter:
                continue
            actual_type = _value_type(page.frontmatter[field])
            allowed_types = [str(item) for item in rule.get("types") or []]
            if allowed_types and actual_type not in allowed_types:
                findings.append(
                    _finding(
                        page.rel_path,
                        f"frontmatter.{field}",
                        f"field `{field}` has type {actual_type}; expected {allowed_types}",
                        f"Use one of the contract types or revise contract `{contract.name}`.",
                        code="CONTRACT_FIELD_TYPE",
                        contract=contract.name,
                        resolved_rule=("fields", field, "types"),
                        raw_element=field,
                    )
                )
            enum = list(rule.get("enum") or [])
            actual_identity = _typed_scalar_identity_or_none(page.frontmatter[field])
            enum_identities = {
                _typed_scalar_identity(item, label="enum") for item in enum
            }
            if enum and actual_identity not in enum_identities:
                findings.append(
                    _finding(
                        page.rel_path,
                        f"frontmatter.{field}",
                        f"field `{field}` value is outside enum {enum}",
                        f"Use an allowed value or revise contract `{contract.name}`.",
                        code="CONTRACT_FIELD_ENUM",
                        contract=contract.name,
                        resolved_rule=("fields", field, "enum"),
                        raw_element=field,
                    )
                )
        for namespace, observed, code, span_label, label in (
            (
                "blocks",
                blocks,
                "CONTRACT_REQUIRED_BLOCK",
                "block",
                "semantic block",
            ),
            ("kinds", kinds, "CONTRACT_REQUIRED_KIND", "kind", "semantic kind"),
            (
                "categories",
                categories,
                "CONTRACT_REQUIRED_CATEGORY",
                "category",
                "semantic category",
            ),
            (
                "relations",
                relations,
                "CONTRACT_REQUIRED_RELATION",
                "relation",
                "relation",
            ),
        ):
            for element, rule in canonical_rules[namespace].items():
                if not rule.get("required") or element in observed:
                    continue
                raw_rule_element = sorted(
                    canonical_rule_raw_elements[namespace].get(element, {element})
                )[0]
                findings.append(
                    _finding(
                        page.rel_path,
                        f"body.{span_label}:{element}",
                        f"missing required {label} `{element}`",
                        f"Add `{element}` or revise contract `{contract.name}`.",
                        code=code,
                        contract=contract.name,
                        resolved_rule=(namespace, element, "required"),
                        raw_element=raw_rule_element,
                        element_key=(
                            semantic_language_registry.normalize_label(raw_rule_element)
                            if namespace in {"blocks", "kinds", "categories"}
                            else raw_rule_element
                        ),
                    )
                )
        for namespace, observed, rules, policy, code in (
            (
                "fields",
                set(page.frontmatter),
                contract.fields,
                contract.unknown_fields,
                "CONTRACT_UNKNOWN_FIELD",
            ),
            (
                "blocks",
                blocks,
                canonical_rules["blocks"],
                contract.unknown_blocks,
                "CONTRACT_UNKNOWN_BLOCK",
            ),
            (
                "kinds",
                kinds,
                canonical_rules["kinds"],
                contract.unknown_kinds,
                "CONTRACT_UNKNOWN_KIND",
            ),
            (
                "categories",
                categories,
                canonical_rules["categories"],
                contract.unknown_categories,
                "CONTRACT_UNKNOWN_CATEGORY",
            ),
            (
                "relations",
                relations,
                canonical_rules["relations"],
                contract.unknown_relations,
                "CONTRACT_UNKNOWN_RELATION",
            ),
        ):
            if policy != "forbid":
                continue
            for element in sorted(observed - set(rules)):
                authored_elements = sorted(
                    observed_raw_elements.get(namespace, {}).get(
                        element, {(element, element)}
                    ),
                    key=lambda item: (item[1], item[0]),
                )
                singular = {
                    "fields": "field",
                    "blocks": "block",
                    "kinds": "kind",
                    "categories": "category",
                    "relations": "relation",
                }[namespace]
                findings.append(
                    _finding(
                        page.rel_path,
                        f"{namespace}.{element}",
                        f"unknown {singular} `{element}` is forbidden",
                        f"Declare `{element}` or set unknown_{namespace}: allow.",
                        code=code,
                        contract=contract.name,
                        resolved_rule=(namespace, "*", "allowed"),
                        raw_element=authored_elements[0][0],
                        element_key=authored_elements[0][1],
                        governed_element_identity=(namespace, element),
                    )
                )
    findings.sort(
        key=lambda item: (
            item["path"],
            item["span"],
            item["code"],
            tuple(item["resolved_rule"]),
            item["detail"],
        )
    )
    return {
        "contract": contract.name,
        "validation": contract.validation,
        "sample_size": len(pages),
        "valid": not findings,
        "strict": strict,
        "strict_failed": bool(strict and findings),
        "findings": findings,
    }


def diff_contracts(before: MemoryContract, after: MemoryContract) -> dict[str, Any]:
    changes = {
        "scope": _value_change(before.scope.as_dict(), after.scope.as_dict()),
        "validation": _value_change(before.validation, after.validation),
        "fields": _rule_diff(before.fields, after.fields, include_types=True),
        "blocks": _rule_diff(before.blocks, after.blocks),
        "kinds": _rule_diff(before.kinds or {}, after.kinds or {}),
        "categories": _rule_diff(before.categories or {}, after.categories or {}),
        "relations": _rule_diff(before.relations, after.relations),
        "unknown_fields": _value_change(
            before.unknown_fields, after.unknown_fields
        ),
        "unknown_blocks": _value_change(
            before.unknown_blocks, after.unknown_blocks
        ),
        "unknown_kinds": _value_change(before.unknown_kinds, after.unknown_kinds),
        "unknown_categories": _value_change(
            before.unknown_categories, after.unknown_categories
        ),
        "unknown_relations": _value_change(
            before.unknown_relations, after.unknown_relations
        ),
    }
    return {
        "before": before.name,
        "after": after.name,
        "changed": any(_has_change(value) for value in changes.values()),
        "changes": changes,
    }


def contract_from_dict(data: dict[str, Any]) -> MemoryContract:
    if not isinstance(data, dict):
        raise ValueError("INVALID_CONTRACT: root must be an object")
    unknown_root = sorted((key for key in data if key not in _ROOT_KEYS), key=str)
    if unknown_root:
        raise ValueError(f"INVALID_CONTRACT: unknown root keys: {unknown_root}")
    version = data.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        raise ValueError(f"INVALID_CONTRACT: schema_version must be {SCHEMA_VERSION}")
    name = _validate_name(data.get("name"))
    scope_data = data.get("scope", {})
    if not isinstance(scope_data, dict):
        raise ValueError("INVALID_CONTRACT: scope must be an object")
    unknown_scope = sorted(set(scope_data) - {"project", "page_type"}, key=str)
    if unknown_scope:
        raise ValueError(
            f"INVALID_CONTRACT: scope has unknown keys: {unknown_scope}"
        )
    sample_size = data.get("sample_size", 0)
    if type(sample_size) is not int or sample_size < 0:
        raise ValueError("INVALID_CONTRACT: sample_size must be a nonnegative integer")
    validation = data.get("validation", "warn")
    if not isinstance(validation, str) or validation not in _VALIDATION_MODES:
        raise ValueError(
            f"INVALID_CONTRACT: validation must be one of {sorted(_VALIDATION_MODES)}"
        )
    policies = {
        key: _unknown_policy(data, key)
        for key in (
            "unknown_fields",
            "unknown_blocks",
            "unknown_kinds",
            "unknown_categories",
            "unknown_relations",
        )
    }
    return MemoryContract(
        name=name,
        scope=ContractScope(
            project=_optional_scope_string(scope_data.get("project"), "scope.project"),
            page_type=_optional_scope_string(
                scope_data.get("page_type"), "scope.page_type"
            ),
        ),
        validation=validation,
        sample_size=sample_size,
        fields=_rules(data.get("fields", {}), "fields", field_rules=True),
        blocks=_rules(data.get("blocks", {}), "blocks"),
        kinds=_rules(data.get("kinds", {}), "kinds"),
        categories=_rules(data.get("categories", {}), "categories"),
        relations=_rules(data.get("relations", {}), "relations"),
        **policies,
    )


def contract_path(vault_root: Path, name: str) -> Path:
    filename = f"{_validate_name(name)}.yaml"
    return Path(vault_root) / kb_dirname() / "_Schema" / "contracts" / filename


def _select_pages(vault_root: Path, scope: ContractScope):
    kb = Path(vault_root) / kb_dirname()
    if not kb.is_dir():
        return []
    pages = []
    for path in find_module._walk_md(kb):
        page = find_module._CACHE.get(path, vault_root)
        if page is None:
            continue
        if scope.page_type and page.page_type != scope.page_type:
            continue
        if scope.project and scope.project not in _page_projects(page.frontmatter):
            continue
        pages.append(page)
    return sorted(pages, key=lambda page: page.rel_path)


def _page_projects(frontmatter: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    if value := frontmatter.get("project"):
        out.add(str(value))
    projects = frontmatter.get("projects") or []
    if isinstance(projects, list):
        out.update(str(value) for value in projects)
    elif projects:
        out.add(str(projects))
    return out


def _page_relations(
    vault_root: Path,
    page,
    document: semantic_units.SemanticUnitDocument,
    *,
    registry: relation_registry.RelationRegistry | None = None,
) -> set[str]:
    return {
        edge.relation_type
        for edge in epistemic_graph._edges_for_page(
            vault_root,
            page,
            document,
            registry=registry,
        )
        if edge.origin in CONTRACT_RELATION_ORIGINS and edge.relation_type is not None
    }


def _value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, dt.date):
        return "date"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def _frequency(count: int, sample_size: int, **extra: Any) -> dict[str, Any]:
    return {
        "count": count,
        "frequency": round(count / sample_size, 4) if sample_size else 0.0,
        **extra,
    }


def _finding(
    path: str,
    span: str,
    detail: str,
    remediation: str,
    *,
    code: str,
    contract: str,
    resolved_rule: tuple[str, str, str],
    raw_element: str,
    element_key: str | None = None,
    governed_element_identity: tuple[str, str] | None = None,
) -> dict[str, Any]:
    governed_identity = governed_element_identity or resolved_rule[:2]
    return {
        "code": code,
        "path": path,
        "span": span,
        "severity": "error",
        "detail": detail,
        "remediation": remediation,
        "contract": contract,
        "governed_element_identity": list(governed_identity),
        "resolved_rule": list(resolved_rule),
        "raw_element": raw_element,
        "element_key": element_key or raw_element,
    }


def _merge_required_rules(
    before: dict[str, Any] | None, after: dict[str, Any]
) -> dict[str, Any]:
    if before is None:
        return dict(after)
    return {"required": bool(before.get("required") or after.get("required"))}


def _typed_scalar_identity_or_none(value: Any) -> tuple[str, Any] | None:
    try:
        return _typed_scalar_identity(value, label="field")
    except ValueError:
        return None


def _typed_enum_identities(values: list[Any]) -> tuple[tuple[str, Any], ...]:
    return tuple(_typed_scalar_identity(value, label="enum") for value in values)


def _rule_diff(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    *,
    include_types: bool = False,
) -> dict[str, Any]:
    before_keys = set(before)
    after_keys = set(after)
    common = before_keys & after_keys
    out: dict[str, Any] = {
        "added": sorted(after_keys - before_keys),
        "removed": sorted(before_keys - after_keys),
        "required_added": sorted(
            key for key in common if not before[key].get("required") and after[key].get("required")
        ),
        "required_removed": sorted(
            key for key in common if before[key].get("required") and not after[key].get("required")
        ),
    }
    if include_types:
        out["type_changes"] = {
            key: {"before": before[key].get("types", []), "after": after[key].get("types", [])}
            for key in sorted(common)
            if before[key].get("types", []) != after[key].get("types", [])
        }
        out["enum_changes"] = {
            key: {"before": before[key].get("enum", []), "after": after[key].get("enum", [])}
            for key in sorted(common)
            if _typed_enum_identities(before[key].get("enum", []))
            != _typed_enum_identities(after[key].get("enum", []))
        }
    return out


def _value_change(before: Any, after: Any) -> dict[str, Any]:
    return {} if before == after else {"before": before, "after": after}


def _has_change(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_has_change(child) for child in value.values())
    if isinstance(value, list):
        return bool(value)
    return value not in (None, "", False)


def _rules(
    value: Any, label: str, *, field_rules: bool = False
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise ValueError(f"INVALID_CONTRACT: {label} must be an object")
    out: dict[str, dict[str, Any]] = {}
    for key, rule in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError(
                f"INVALID_CONTRACT: {label} keys must be nonempty strings"
            )
        if not isinstance(rule, dict):
            raise ValueError(f"INVALID_CONTRACT: {label}.{key} must be an object")
        allowed = {"required", "types", "enum"} if field_rules else {"required"}
        unknown = sorted(set(rule) - allowed, key=str)
        if unknown:
            raise ValueError(
                f"INVALID_CONTRACT: {label}.{key} has unknown keys: {unknown}"
            )
        parsed: dict[str, Any] = {}
        if "required" in rule:
            if type(rule["required"]) is not bool:
                raise ValueError(
                    f"INVALID_CONTRACT: {label}.{key}.required must be a boolean"
                )
            parsed["required"] = rule["required"]
        if field_rules and "types" in rule:
            types = rule["types"]
            if not isinstance(types, list) or not types:
                raise ValueError(
                    f"INVALID_CONTRACT: {label}.{key}.types must be a nonempty list"
                )
            if any(not isinstance(item, str) or item not in _TYPE_INDEX for item in types):
                raise ValueError(
                    f"INVALID_CONTRACT: {label}.{key}.types contains an invalid type"
                )
            if len(set(types)) != len(types):
                raise ValueError(
                    f"INVALID_CONTRACT: {label}.{key}.types contains duplicate values"
                )
            parsed["types"] = sorted(types, key=_TYPE_INDEX.__getitem__)
        if field_rules and "enum" in rule:
            enum = rule["enum"]
            if not isinstance(enum, list) or not enum:
                raise ValueError(
                    f"INVALID_CONTRACT: {label}.{key}.enum must be a nonempty list"
                )
            identities: set[tuple[str, Any]] = set()
            parsed_enum: list[Any] = []
            for item in enum:
                identity = _typed_scalar_identity(item, label=f"{label}.{key}.enum")
                if identity in identities:
                    raise ValueError(
                        f"INVALID_CONTRACT: {label}.{key}.enum contains duplicate values"
                    )
                identities.add(identity)
                parsed_enum.append(item)
            if "types" in parsed:
                invalid_types = sorted(
                    {
                        _value_type(item)
                        for item in parsed_enum
                        if _value_type(item) not in parsed["types"]
                    }
                )
                if invalid_types:
                    raise ValueError(
                        f"INVALID_CONTRACT: {label}.{key}.enum types {invalid_types} "
                        "are not permitted by types"
                    )
            parsed["enum"] = sorted(parsed_enum, key=_typed_scalar_sort_key)
        out[key] = parsed
    return dict(sorted(out.items()))


def _validate_name(name: Any) -> str:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ValueError(
            "INVALID_CONTRACT: name must be a lowercase slug of 1-64 letters, digits, or hyphens"
        )
    return name


def _optional_scope_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"INVALID_CONTRACT: {label} must be a nonempty string or null")
    return value


def _unknown_policy(data: dict[str, Any], key: str) -> str:
    value = data.get(key, "allow")
    if not isinstance(value, str) or value not in _UNKNOWN_POLICIES:
        raise ValueError(
            f"INVALID_CONTRACT: {key} must be one of {sorted(_UNKNOWN_POLICIES)}"
        )
    return value


def _typed_scalar_identity(value: Any, *, label: str) -> tuple[str, Any]:
    if value is None:
        return ("null", None)
    if type(value) is bool:
        return ("boolean", value)
    if type(value) is int:
        return ("integer", value)
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"INVALID_CONTRACT: {label} numbers must be finite")
        return ("number", value)
    if isinstance(value, dt.datetime):
        return ("datetime", value.isoformat())
    if isinstance(value, dt.date):
        return ("date", value.isoformat())
    if isinstance(value, str):
        return ("string", value)
    raise ValueError(
        f"INVALID_CONTRACT: {label} values must be typed YAML scalars"
    )


def _typed_scalar_sort_key(value: Any) -> tuple[int, Any]:
    tag, normalized = _typed_scalar_identity(value, label="enum")
    order = {
        "null": 0,
        "boolean": 1,
        "integer": 2,
        "number": 3,
        "date": 4,
        "datetime": 5,
        "string": 6,
    }
    return (order[tag], normalized)


def _sorted_rules(value: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        key: {
            rule_key: list(rule_value) if isinstance(rule_value, tuple) else rule_value
            for rule_key, rule_value in value[key].items()
        }
        for key in sorted(value)
    }


def _json_value(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.nodes.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from error
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _load_contract_yaml(raw: str) -> Any:
    return yaml.load(raw, Loader=_UniqueKeyLoader)  # noqa: S506 - SafeLoader subclass
