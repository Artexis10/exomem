"""Optional corpus-inferred contracts for governed knowledge patterns."""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import (
    epistemic_graph,
    markdown_relations,
    relation_registry,
    semantic_blocks,
    traversal_profiles,
    vault,
)
from . import find as find_module
from .kbdir import kb_dirname

SCHEMA_VERSION = 1
MIN_REQUIRED_SAMPLE = 5
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


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
    unknown_fields: str = "allow"
    unknown_blocks: str = "allow"
    unknown_relations: str = "allow"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "name": self.name,
            "scope": self.scope.as_dict(),
            "sample_size": self.sample_size,
            "fields": self.fields,
            "blocks": self.blocks,
            "relations": self.relations,
            "unknown_fields": self.unknown_fields,
            "unknown_blocks": self.unknown_blocks,
            "unknown_relations": self.unknown_relations,
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
    relation_counts: Counter[str] = Counter()

    for page in pages:
        for key, value in page.frontmatter.items():
            key = str(key)
            field_counts[key] += 1
            field_types.setdefault(key, Counter())[_value_type(value)] += 1
            if isinstance(value, (str, bool, int, float, dt.date)):
                field_values.setdefault(key, Counter())[str(value)] += 1
        document = semantic_blocks.parse_semantic_blocks(page.body, validate=False)
        page_blocks = {block.type for block in document.blocks}
        page_relations = _page_relations(vault_root, page, document)
        block_counts.update(page_blocks)
        relation_counts.update(page_relations)

    fields: dict[str, dict[str, Any]] = {}
    field_profile: dict[str, dict[str, Any]] = {}
    for key in sorted(field_counts):
        count = field_counts[key]
        types = sorted(field_types[key])
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
    pages = _select_pages(vault_root, ContractScope(project, page_type))
    out: list[dict[str, Any]] = []
    for page in pages:
        page_project = next(iter(sorted(_page_projects(page.frontmatter))), None)
        document = semantic_blocks.parse_semantic_blocks(
            page.body, validate=False, registry=registry
        )
        for block in document.blocks:
            for relation in block.relations:
                raw = relation.raw.split(":", 1)[0].strip()
                resolution = registry.resolve(
                    raw,
                    project=page_project,
                    page_type=page.page_type,
                    source_kind=block.type,
                    origin="semantic_relation",
                )
                out.append(
                    _observation(
                        page.rel_path, block.id or f"line-{relation.line}", raw, resolution
                    )
                )
        note_relations = markdown_relations.parse_markdown_relations(
            page.body,
            include_legacy=True,
            relation_types=registry.keys | frozenset(registry.aliases),
            retain_unknown=True,
        )
        for relation in note_relations.relations:
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
    if not path.exists():
        raise ValueError(f"CONTRACT_NOT_FOUND: no saved contract named {name!r}")
    raw = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError(f"INVALID_CONTRACT: could not parse {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"INVALID_CONTRACT: {path.name} must contain a YAML object")
    return (
        contract_from_dict(data),
        vault.content_hash(raw),
        path.relative_to(vault_root).as_posix(),
    )


def validate_contract(vault_root: Path, contract: MemoryContract, *, strict: bool = False) -> dict:
    pages = _select_pages(vault_root, contract.scope)
    findings: list[dict[str, Any]] = []
    for page in pages:
        document = semantic_blocks.parse_semantic_blocks(page.body, validate=False)
        blocks = {block.type for block in document.blocks}
        relations = _page_relations(vault_root, page, document)
        for field, rule in contract.fields.items():
            if rule.get("required") and field not in page.frontmatter:
                findings.append(
                    _finding(
                        page.rel_path,
                        f"frontmatter.{field}",
                        f"missing required frontmatter field `{field}`",
                        f"Add `{field}` to frontmatter or revise contract `{contract.name}`.",
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
                    )
                )
            enum = [str(item) for item in rule.get("enum") or []]
            if enum and str(page.frontmatter[field]) not in enum:
                findings.append(
                    _finding(
                        page.rel_path,
                        f"frontmatter.{field}",
                        f"field `{field}` value is outside enum {enum}",
                        f"Use an allowed value or revise contract `{contract.name}`.",
                    )
                )
        for block, rule in contract.blocks.items():
            if rule.get("required") and block not in blocks:
                findings.append(
                    _finding(
                        page.rel_path,
                        f"body.block:{block}",
                        f"missing required semantic block `{block}`",
                        f"Add a `{block}` block or revise contract `{contract.name}`.",
                    )
                )
        for relation, rule in contract.relations.items():
            if rule.get("required") and relation not in relations:
                findings.append(
                    _finding(
                        page.rel_path,
                        f"body.relation:{relation}",
                        f"missing required relation `{relation}`",
                        f"Add an observed `{relation}` relation or revise "
                        f"contract `{contract.name}`.",
                    )
                )
    return {
        "contract": contract.name,
        "sample_size": len(pages),
        "valid": not findings,
        "strict": strict,
        "strict_failed": bool(strict and findings),
        "findings": findings,
    }


def diff_contracts(before: MemoryContract, after: MemoryContract) -> dict[str, Any]:
    changes = {
        "scope": _value_change(before.scope.as_dict(), after.scope.as_dict()),
        "fields": _rule_diff(before.fields, after.fields, include_types=True),
        "blocks": _rule_diff(before.blocks, after.blocks),
        "relations": _rule_diff(before.relations, after.relations),
    }
    return {
        "before": before.name,
        "after": after.name,
        "changed": any(_has_change(value) for value in changes.values()),
        "changes": changes,
    }


def contract_from_dict(data: dict[str, Any]) -> MemoryContract:
    if int(data.get("schema_version", 0)) != SCHEMA_VERSION:
        raise ValueError(f"INVALID_CONTRACT: schema_version must be {SCHEMA_VERSION}")
    name = _validate_name(str(data.get("name") or ""))
    scope_data = data.get("scope") or {}
    if not isinstance(scope_data, dict):
        raise ValueError("INVALID_CONTRACT: scope must be an object")
    return MemoryContract(
        name=name,
        scope=ContractScope(
            project=_optional_string(scope_data.get("project")),
            page_type=_optional_string(scope_data.get("page_type")),
        ),
        sample_size=max(0, int(data.get("sample_size", 0))),
        fields=_rules(data.get("fields"), "fields"),
        blocks=_rules(data.get("blocks"), "blocks"),
        relations=_rules(data.get("relations"), "relations"),
        unknown_fields=str(data.get("unknown_fields") or "allow"),
        unknown_blocks=str(data.get("unknown_blocks") or "allow"),
        unknown_relations=str(data.get("unknown_relations") or "allow"),
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


def _page_relations(vault_root: Path, page, document) -> set[str]:
    return {
        edge.relation_type
        for edge in epistemic_graph._edges_for_page(vault_root, page, tuple(document.blocks))
        if edge.origin == "semantic_relation" and edge.relation_type is not None
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


def _finding(path: str, span: str, detail: str, remediation: str) -> dict[str, str]:
    return {
        "path": path,
        "span": span,
        "severity": "error",
        "detail": detail,
        "remediation": remediation,
    }


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
            if before[key].get("enum", []) != after[key].get("enum", [])
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


def _rules(value: Any, label: str) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"INVALID_CONTRACT: {label} must be an object")
    out: dict[str, dict[str, Any]] = {}
    for key, rule in value.items():
        if not isinstance(rule, dict):
            raise ValueError(f"INVALID_CONTRACT: {label}.{key} must be an object")
        out[str(key)] = dict(rule)
    return out


def _validate_name(name: str) -> str:
    clean = str(name or "").strip().lower()
    if not _NAME_RE.fullmatch(clean):
        raise ValueError(
            "INVALID_CONTRACT: name must be a lowercase slug of 1-64 letters, digits, or hyphens"
        )
    return clean


def _optional_string(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None
