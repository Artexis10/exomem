"""Governance for open categories and extensible semantic kinds.

The registry never assigns semantics to an unknown category. It preserves
portable rich-block kinds while allowing explicitly reviewed, vault-owned
category aliases and custom rich headings.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from . import semantic_blocks, vault
from .kbdir import kb_dirname

SCHEMA_VERSION = 1
_STATUSES = frozenset({"active", "deprecated"})
_SEPARATORS_RE = re.compile(r"[\s_-]+")
_DEFINITION_FIELDS = frozenset(
    {"description", "aliases", "status", "replaced_by", "scope"}
)
_KIND_DEFINITION_FIELDS = _DEFINITION_FIELDS | {"heading_aliases"}


@dataclass(frozen=True, slots=True)
class RegistryFinding(Mapping[str, str]):
    """Deeply immutable, mapping-compatible registry validation finding."""

    code: str
    path: str
    span: str
    severity: str
    detail: str

    def __getitem__(self, key: str) -> str:
        if key not in {"code", "path", "span", "severity", "detail"}:
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self) -> Iterator[str]:
        return iter(("code", "path", "span", "severity", "detail"))

    def __len__(self) -> int:
        return 5

    def as_dict(self) -> dict[str, str]:
        return {key: self[key] for key in self}


@dataclass(frozen=True, slots=True)
class CategoryDefinition:
    key: str
    description: str
    aliases: tuple[str, ...] = ()
    projects: frozenset[str] = frozenset()
    page_types: frozenset[str] = frozenset()
    status: str = "active"
    replaced_by: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return _definition_dict(self)


@dataclass(frozen=True, slots=True)
class KindDefinition:
    key: str
    description: str
    aliases: tuple[str, ...] = ()
    heading_aliases: tuple[str, ...] = ()
    projects: frozenset[str] = frozenset()
    page_types: frozenset[str] = frozenset()
    status: str = "active"
    replaced_by: str | None = None
    core: bool = False

    def as_dict(self) -> dict[str, Any]:
        out = _definition_dict(self)
        if self.heading_aliases:
            out["heading_aliases"] = sorted(self.heading_aliases)
        if self.core:
            out["core"] = True
        return out


@dataclass(frozen=True, slots=True)
class LabelResolution:
    raw: str
    key: str
    resolved: str | None
    status: str
    definition: CategoryDefinition | KindDefinition | None
    replacement: str | None = None
    findings: tuple[RegistryFinding, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "key": self.key,
            "resolved": self.resolved,
            "status": self.status,
            "definition": self.definition.as_dict() if self.definition else None,
            "replacement": self.replacement,
            "findings": [item.as_dict() for item in self.findings],
        }


@dataclass(frozen=True, slots=True)
class SemanticLanguageRegistry:
    schema_version: int
    content_hash: str
    core_kinds: Mapping[str, KindDefinition]
    categories: Mapping[str, CategoryDefinition] = field(default_factory=dict)
    kinds: Mapping[str, KindDefinition] = field(default_factory=dict)
    category_aliases: Mapping[str, str] = field(default_factory=dict)
    kind_aliases: Mapping[str, str] = field(default_factory=dict)
    heading_aliases: Mapping[str, str] = field(default_factory=dict)
    findings: tuple[RegistryFinding, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "core_kinds",
            "categories",
            "kinds",
            "category_aliases",
            "kind_aliases",
            "heading_aliases",
        ):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))
        object.__setattr__(self, "findings", tuple(self.findings))

    def as_dict(self) -> dict[str, Any]:
        """Return deterministic semantic content (the byte hash is transport metadata)."""
        return {
            "schema_version": self.schema_version,
            "core_kinds": {
                key: self.core_kinds[key].as_dict() for key in sorted(self.core_kinds)
            },
            "categories": {
                key: self.categories[key].as_dict() for key in sorted(self.categories)
            },
            "kinds": {key: self.kinds[key].as_dict() for key in sorted(self.kinds)},
            "category_aliases": dict(sorted(self.category_aliases.items())),
            "kind_aliases": dict(sorted(self.kind_aliases.items())),
            "heading_aliases": dict(sorted(self.heading_aliases.items())),
            "findings": [item.as_dict() for item in self.findings],
        }

    def resolve_category(
        self,
        raw: str,
        *,
        project: str | None = None,
        page_type: str | None = None,
    ) -> LabelResolution:
        key = normalize_label(raw)
        if self.findings:
            return LabelResolution(
                raw=str(raw),
                key=key,
                resolved=key,
                status="registry_invalid",
                definition=None,
                findings=self.findings,
            )
        canonical = self.category_aliases.get(key, key)
        definition = self.categories.get(canonical)
        if definition is None:
            return LabelResolution(str(raw), key, key, "unregistered", None)
        scope_findings = _scope_findings(
            "categories", canonical, definition, project=project, page_type=page_type
        )
        if scope_findings:
            return LabelResolution(
                str(raw),
                key,
                key,
                "scope_violation",
                definition,
                definition.replaced_by,
                scope_findings,
            )
        status = _resolution_status(key, canonical, definition.status)
        return LabelResolution(
            str(raw), canonical if key == canonical else key, canonical, status, definition,
            definition.replaced_by,
        )

    def resolve_kind(
        self,
        raw: str,
        *,
        project: str | None = None,
        page_type: str | None = None,
    ) -> LabelResolution:
        key = normalize_label(raw)
        core_canonical = _core_heading_aliases().get(key, key)
        core_definition = self.core_kinds.get(core_canonical)
        if core_definition is not None:
            status = "alias" if core_canonical != key else "core"
            return LabelResolution(
                str(raw), key, core_canonical, status, core_definition
            )
        if self.findings:
            return LabelResolution(
                str(raw), key, None, "registry_invalid", None, findings=self.findings
            )
        canonical = self.kind_aliases.get(key, key)
        return self._resolve_custom_kind(
            raw, key, canonical, project=project, page_type=page_type
        )

    def resolve_heading(
        self,
        raw: str,
        *,
        project: str | None = None,
        page_type: str | None = None,
    ) -> LabelResolution:
        """Resolve rich headings; the compact-only ``observation`` stays inert."""
        key = normalize_label(raw)
        core_canonical = _core_heading_aliases().get(key, key)
        core_definition = self.core_kinds.get(core_canonical)
        if core_definition is not None and core_canonical != "observation":
            status = "alias" if core_canonical != key else "core"
            return LabelResolution(
                str(raw), key, core_canonical, status, core_definition
            )
        if self.findings:
            return LabelResolution(
                str(raw), key, None, "registry_invalid", None, findings=self.findings
            )
        canonical = self.heading_aliases.get(key, key)
        return self._resolve_custom_kind(
            raw, key, canonical, project=project, page_type=page_type
        )

    def _resolve_custom_kind(
        self,
        raw: str,
        key: str,
        canonical: str,
        *,
        project: str | None,
        page_type: str | None,
    ) -> LabelResolution:
        definition = self.kinds.get(canonical)
        if definition is None:
            return LabelResolution(str(raw), key, None, "unregistered", None)
        scope_findings = _scope_findings(
            "kinds", canonical, definition, project=project, page_type=page_type
        )
        if scope_findings:
            return LabelResolution(
                str(raw),
                key,
                None,
                "scope_violation",
                definition,
                definition.replaced_by,
                scope_findings,
            )
        status = _resolution_status(key, canonical, definition.status)
        return LabelResolution(
            str(raw), key, canonical, status, definition, definition.replaced_by
        )


def normalize_label(raw: str) -> str:
    label = str(raw or "").strip().rstrip(":").strip()
    normalized = unicodedata.normalize("NFKC", label).casefold()
    return _SEPARATORS_RE.sub("_", normalized).strip("_")


def registry_path(vault_root: Path) -> Path:
    return Path(vault_root) / kb_dirname() / "_Schema" / "semantic-language-registry.yaml"


@lru_cache(maxsize=1)
def core_registry() -> SemanticLanguageRegistry:
    core_kinds: dict[str, KindDefinition] = {
        key: KindDefinition(
            key=key,
            description=(
                "Compact semantic observation"
                if key == "observation"
                else f"Portable {key.replace('_', ' ')} semantic block"
            ),
            core=True,
        )
        for key in sorted((*semantic_blocks.BLOCK_TYPES, "observation"))
    }
    return SemanticLanguageRegistry(SCHEMA_VERSION, "none", core_kinds)


_CACHE: dict[Path, tuple[str, SemanticLanguageRegistry]] = {}


def load_registry(
    vault_root: Path | None = None,
    *,
    proposal: Any | None = None,
) -> SemanticLanguageRegistry:
    core = core_registry()
    if proposal is not None:
        raw = yaml.safe_dump(proposal, allow_unicode=True, sort_keys=True)
        return _parse_registry_data(proposal, _content_hash(raw), core)
    if vault_root is None:
        return core
    path = registry_path(vault_root)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return core
    digest = _content_hash(raw)
    cached = _CACHE.get(path)
    if cached is not None and cached[0] == digest:
        return cached[1]
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        registry = SemanticLanguageRegistry(
            SCHEMA_VERSION,
            digest,
            core.core_kinds,
            findings=(_finding("invalid_yaml", "registry", str(exc)),),
        )
    else:
        registry = _parse_registry_data(data, digest, core)
    _CACHE[path] = (digest, registry)
    return registry


def validate_proposal(proposal: Any) -> list[dict[str, str]]:
    raw = yaml.safe_dump(proposal, allow_unicode=True, sort_keys=True)
    registry = _parse_registry_data(proposal, _content_hash(raw), core_registry())
    return [finding.as_dict() for finding in registry.findings]


def empty_proposal() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "categories": {}, "kinds": {}}


def registry_proposal(registry: SemanticLanguageRegistry) -> dict[str, Any]:
    """Serialize the complete reviewed document without derived core metadata."""
    return {
        "schema_version": registry.schema_version,
        "categories": {
            key: _definition_proposal(registry.categories[key])
            for key in sorted(registry.categories)
        },
        "kinds": {
            key: _definition_proposal(registry.kinds[key])
            for key in sorted(registry.kinds)
        },
    }


def save_registry(
    vault_root: Path,
    proposal: dict[str, Any],
    *,
    expected_hash: str | None = None,
) -> dict[str, Any]:
    """Atomically save one reviewed, complete semantic-language document."""
    if not isinstance(proposal, dict) or not {"categories", "kinds"} <= set(proposal):
        raise ValueError(
            "INCOMPLETE_SEMANTIC_LANGUAGE_PROPOSAL: "
            "save requires categories and kinds in one reviewed document"
        )
    registry = load_registry(proposal=proposal)
    if registry.findings:
        raise ValueError(
            "INVALID_SEMANTIC_LANGUAGE_REGISTRY: "
            f"{[item.as_dict() for item in registry.findings]!r}"
        )

    path = registry_path(vault_root)
    current_hash: str | None = None
    if path.exists():
        current_raw = path.read_text(encoding="utf-8")
        current_hash = _content_hash(current_raw)
        if expected_hash is None:
            raise ValueError(
                "SEMANTIC_LANGUAGE_REGISTRY_EXISTS: provide current expected_hash"
            )
        if expected_hash != current_hash:
            raise ValueError(
                "STALE_SEMANTIC_LANGUAGE_REGISTRY: "
                "expected_hash does not match current hash"
            )
    elif expected_hash is not None:
        raise ValueError(
            "SEMANTIC_LANGUAGE_REGISTRY_MISSING: "
            "expected_hash was provided but the registry does not exist"
        )

    rendered = yaml.safe_dump(
        registry_proposal(registry),
        allow_unicode=True,
        sort_keys=True,
    )
    vault.batch_atomic_write(
        [vault.PlannedWrite(path=path, content=rendered)], vault_root=Path(vault_root)
    )
    _CACHE.pop(path, None)
    return {
        "path": path.relative_to(vault_root).as_posix(),
        "content_hash": _content_hash(rendered),
        "previous_hash": current_hash,
        "created": current_hash is None,
    }


def _parse_registry_data(
    data: Any,
    digest: str,
    core: SemanticLanguageRegistry,
) -> SemanticLanguageRegistry:
    findings: list[RegistryFinding] = []
    if not isinstance(data, dict):
        return SemanticLanguageRegistry(
            SCHEMA_VERSION,
            digest,
            core.core_kinds,
            findings=(_finding("invalid_registry", "registry", "must be an object"),),
        )
    unknown_root_fields = set(data) - {"schema_version", "categories", "kinds"}
    for key in sorted(unknown_root_fields, key=str):
        findings.append(_finding("unknown_field", str(key), "unknown registry field"))
    schema_version = data.get("schema_version")
    if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
        findings.append(
            _finding("invalid_version", "schema_version", f"must be {SCHEMA_VERSION}")
        )

    raw_categories = data.get("categories", {})
    if not isinstance(raw_categories, dict):
        findings.append(_finding("invalid_categories", "categories", "must be an object"))
        raw_categories = {}
    raw_kinds = data.get("kinds", {})
    if not isinstance(raw_kinds, dict):
        findings.append(_finding("invalid_kinds", "kinds", "must be an object"))
        raw_kinds = {}

    categories = _parse_definitions(
        raw_categories, "categories", CategoryDefinition, _DEFINITION_FIELDS, findings
    )
    kinds = _parse_definitions(
        raw_kinds, "kinds", KindDefinition, _KIND_DEFINITION_FIELDS, findings
    )

    category_aliases = _build_aliases(
        categories, "categories", core_labels={}, findings=findings
    )
    core_labels = {key: key for key in core.core_kinds}
    core_labels.update(_core_heading_aliases())
    kind_aliases = _build_aliases(
        kinds, "kinds", core_labels=core_labels, findings=findings
    )
    heading_aliases = _build_heading_aliases(
        kinds, core_labels=core_labels, kind_aliases=kind_aliases, findings=findings
    )

    _validate_replacements(categories, "categories", findings)
    _validate_replacements(kinds, "kinds", findings)
    findings.extend(_replacement_cycle_findings(categories, "categories"))
    findings.extend(_replacement_cycle_findings(kinds, "kinds"))
    sorted_findings = tuple(
        sorted(findings, key=lambda item: (item["path"], item["code"], item["detail"]))
    )
    return SemanticLanguageRegistry(
        SCHEMA_VERSION,
        digest,
        core.core_kinds,
        categories,
        kinds,
        category_aliases,
        kind_aliases,
        heading_aliases,
        sorted_findings,
    )


def _parse_definitions(
    raw_definitions: dict[Any, Any],
    namespace: str,
    definition_type: type[CategoryDefinition] | type[KindDefinition],
    allowed_fields: frozenset[str],
    findings: list[RegistryFinding],
) -> dict[str, CategoryDefinition] | dict[str, KindDefinition]:
    definitions: dict[str, CategoryDefinition] | dict[str, KindDefinition] = {}
    canonical_sources: dict[str, str] = {}
    for raw_key, value in raw_definitions.items():
        source_key = str(raw_key)
        key = normalize_label(source_key)
        span = f"{namespace}.{source_key}"
        if source_key != key or not _valid_label(key):
            findings.append(
                _finding(
                    "invalid_key",
                    span,
                    "must be a canonical NFKC/casefold semantic label",
                )
            )
        prior_source = canonical_sources.get(key)
        if prior_source is not None and prior_source != source_key:
            findings.append(
                _finding(
                    "canonical_collision",
                    span,
                    f"normalizes to existing canonical key {key!r}",
                )
            )
        canonical_sources[key] = source_key
        if namespace == "kinds" and key in _all_core_kind_labels():
            findings.append(
                _finding(
                    "canonical_collision", span, "collides with a portable built-in kind"
                )
            )
        if not isinstance(value, dict):
            findings.append(_finding("invalid_definition", span, "must be an object"))
            continue
        for unknown in sorted(set(value) - allowed_fields, key=str):
            findings.append(
                _finding("unknown_field", f"{span}.{unknown}", "unknown definition field")
            )
        raw_description = value.get("description")
        if raw_description is None:
            findings.append(
                _finding("missing_description", f"{span}.description", "is required")
            )
            description = ""
        elif not isinstance(raw_description, str):
            findings.append(
                _finding(
                    "invalid_type", f"{span}.description", "must be a string"
                )
            )
            description = ""
        else:
            description = raw_description.strip()
        if isinstance(raw_description, str) and not description:
            findings.append(
                _finding("missing_description", f"{span}.description", "is required")
            )
        raw_status = value.get("status", "active")
        if not isinstance(raw_status, str):
            findings.append(
                _finding("invalid_type", f"{span}.status", "must be a string")
            )
            status = "active"
        else:
            status = raw_status
        if isinstance(raw_status, str) and status not in _STATUSES:
            findings.append(
                _finding(
                    "invalid_status",
                    f"{span}.status",
                    f"must be one of {sorted(_STATUSES)}",
                )
            )
        scope = value.get("scope", {})
        if not isinstance(scope, dict):
            findings.append(
                _finding("invalid_scope", f"{span}.scope", "must be an object")
            )
            scope = {}
        elif set(scope) - {"projects", "page_types"}:
            findings.append(
                _finding(
                    "invalid_scope",
                    f"{span}.scope",
                    "only projects and page_types are allowed",
                )
            )
        aliases = tuple(
            normalize_label(item)
            for item in _strings(value.get("aliases", []), f"{span}.aliases", findings)
        )
        projects = frozenset(
            _strings(scope.get("projects", []), f"{span}.scope.projects", findings)
        )
        page_types = frozenset(
            _strings(scope.get("page_types", []), f"{span}.scope.page_types", findings)
        )
        raw_replaced_by = value.get("replaced_by")
        if raw_replaced_by not in (None, "") and not isinstance(
            raw_replaced_by, str
        ):
            findings.append(
                _finding(
                    "invalid_type", f"{span}.replaced_by", "must be a string or null"
                )
            )
            replaced_by = None
        else:
            replaced_by = _optional(raw_replaced_by)
        common: dict[str, Any] = {
            "key": key,
            "description": description,
            "aliases": aliases,
            "projects": projects,
            "page_types": page_types,
            "status": status,
            "replaced_by": replaced_by,
        }
        if definition_type is KindDefinition:
            common["heading_aliases"] = tuple(
                normalize_label(item)
                for item in _strings(
                    value.get("heading_aliases", []),
                    f"{span}.heading_aliases",
                    findings,
                )
            )
        definitions[key] = definition_type(**common)
    return definitions


def _build_aliases(
    definitions: Mapping[str, CategoryDefinition | KindDefinition],
    namespace: str,
    *,
    core_labels: Mapping[str, str],
    findings: list[RegistryFinding],
) -> dict[str, str]:
    aliases: dict[str, str] = {}
    canonical = set(definitions)
    for key in sorted(definitions):
        for alias in definitions[key].aliases:
            span = f"{namespace}.{key}.aliases"
            if not _valid_label(alias):
                findings.append(_finding("invalid_alias", span, f"invalid alias {alias!r}"))
            if alias in core_labels or alias in canonical:
                findings.append(
                    _finding("alias_collision", span, f"alias {alias!r} collides")
                )
                continue
            prior = aliases.get(alias)
            if prior is not None and prior != key:
                findings.append(
                    _finding(
                        "alias_conflict",
                        span,
                        f"alias {alias!r} maps to both {prior!r} and {key!r}",
                    )
                )
                continue
            aliases[alias] = key
    return aliases


def _build_heading_aliases(
    kinds: Mapping[str, KindDefinition],
    *,
    core_labels: Mapping[str, str],
    kind_aliases: Mapping[str, str],
    findings: list[RegistryFinding],
) -> dict[str, str]:
    aliases: dict[str, str] = {}
    canonical = set(kinds)
    for key in sorted(kinds):
        aliases[key] = key
        for alias in kinds[key].heading_aliases:
            span = f"kinds.{key}.heading_aliases"
            if not _valid_label(alias):
                findings.append(_finding("invalid_alias", span, f"invalid alias {alias!r}"))
            if alias in core_labels or alias in canonical:
                findings.append(
                    _finding("alias_collision", span, f"alias {alias!r} collides")
                )
                continue
            prior = aliases.get(alias) or kind_aliases.get(alias)
            if prior is not None and prior != key:
                findings.append(
                    _finding(
                        "alias_conflict",
                        span,
                        f"alias {alias!r} maps to both {prior!r} and {key!r}",
                    )
                )
                continue
            aliases[alias] = key
    return aliases


def _validate_replacements(
    definitions: Mapping[str, CategoryDefinition | KindDefinition],
    namespace: str,
    findings: list[RegistryFinding],
) -> None:
    for key in sorted(definitions):
        definition = definitions[key]
        span = f"{namespace}.{key}.replaced_by"
        replacement = definition.replaced_by
        if definition.status == "deprecated" and not replacement:
            findings.append(
                _finding(
                    "missing_replacement", span, "deprecated definitions require a replacement"
                )
            )
            continue
        if definition.status != "deprecated" and replacement:
            findings.append(
                _finding(
                    "invalid_replacement",
                    span,
                    "only deprecated definitions may declare a replacement",
                )
            )
        if not replacement:
            continue
        if replacement != normalize_label(replacement) or replacement not in definitions:
            findings.append(
                _finding(
                    "invalid_replacement",
                    span,
                    "must name an active canonical key in the same namespace",
                )
            )
            continue
        target = definitions[replacement]
        if target.status == "deprecated":
            findings.append(
                _finding("invalid_replacement", span, "replacement must be active")
            )


def _replacement_cycle_findings(
    definitions: Mapping[str, CategoryDefinition | KindDefinition], namespace: str
) -> list[RegistryFinding]:
    findings: list[RegistryFinding] = []
    reported: set[frozenset[str]] = set()
    for start in sorted(definitions):
        order: list[str] = []
        positions: dict[str, int] = {}
        current: str | None = start
        while current in definitions and current not in positions:
            positions[current] = len(order)
            order.append(current)
            current = definitions[current].replaced_by
        if current is None or current not in positions:
            continue
        cycle = order[positions[current] :]
        cycle_key = frozenset(cycle)
        if cycle_key in reported:
            continue
        reported.add(cycle_key)
        findings.append(
            _finding(
                "replacement_cycle",
                f"{namespace}.{start}.replaced_by",
                f"replacement cycle is not allowed: {' -> '.join(cycle + [cycle[0]])}",
            )
        )
    return findings


def _scope_findings(
    namespace: str,
    canonical: str,
    definition: CategoryDefinition | KindDefinition,
    *,
    project: str | None,
    page_type: str | None,
) -> tuple[RegistryFinding, ...]:
    findings: list[RegistryFinding] = []
    for field_name, allowed, actual in (
        ("project", definition.projects, project),
        ("page_type", definition.page_types, page_type),
    ):
        if allowed and actual not in allowed:
            findings.append(
                _finding(
                    "scope_violation",
                    f"{namespace}.{canonical}.{field_name}",
                    f"{actual!r} is outside {sorted(allowed)!r}",
                    severity="warning",
                )
            )
    return tuple(
        sorted(findings, key=lambda item: (item["path"], item["code"], item["detail"]))
    )


def _resolution_status(key: str, canonical: str, status: str) -> str:
    if status == "deprecated":
        return "deprecated"
    return "alias" if key != canonical else "extension"


def _definition_dict(definition: CategoryDefinition | KindDefinition) -> dict[str, Any]:
    out: dict[str, Any] = {
        "key": definition.key,
        "description": definition.description,
        "status": definition.status,
    }
    if definition.aliases:
        out["aliases"] = sorted(definition.aliases)
    if definition.projects:
        out["projects"] = sorted(definition.projects)
    if definition.page_types:
        out["page_types"] = sorted(definition.page_types)
    if definition.replaced_by:
        out["replaced_by"] = definition.replaced_by
    return out


def _definition_proposal(
    definition: CategoryDefinition | KindDefinition,
) -> dict[str, Any]:
    out: dict[str, Any] = {"description": definition.description}
    if definition.aliases:
        out["aliases"] = sorted(definition.aliases)
    if isinstance(definition, KindDefinition) and definition.heading_aliases:
        out["heading_aliases"] = sorted(definition.heading_aliases)
    if definition.status != "active":
        out["status"] = definition.status
    if definition.replaced_by is not None:
        out["replaced_by"] = definition.replaced_by
    scope: dict[str, list[str]] = {}
    if definition.projects:
        scope["projects"] = sorted(definition.projects)
    if definition.page_types:
        scope["page_types"] = sorted(definition.page_types)
    if scope:
        out["scope"] = scope
    return out


def _strings(value: Any, path: str, findings: list[RegistryFinding]) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        findings.append(_finding("invalid_list", path, "must be a list of strings"))
        return []
    return list(dict.fromkeys(item.strip() for item in value if item.strip()))


def _optional(value: Any) -> str | None:
    return str(value).strip() if value not in (None, "") else None


def _valid_label(label: str) -> bool:
    if not label or len(label) > 64 or not label[0].isalpha():
        return False
    return all(char.isalpha() or char.isdigit() or char == "_" for char in label)


@lru_cache(maxsize=1)
def _core_heading_aliases() -> dict[str, str]:
    return {
        normalize_label(alias): canonical
        for alias, canonical in semantic_blocks._BLOCK_TYPE_ALIASES.items()
    }


@lru_cache(maxsize=1)
def _all_core_kind_labels() -> frozenset[str]:
    return frozenset((*core_registry().core_kinds, *_core_heading_aliases()))


def _finding(
    code: str, path: str, detail: str, *, severity: str = "error"
) -> RegistryFinding:
    return RegistryFinding(
        code=code,
        path=path,
        span=path,
        severity=severity,
        detail=detail,
    )


def _content_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
