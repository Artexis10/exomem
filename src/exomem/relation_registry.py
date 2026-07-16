"""Versioned core relations and optional vault-owned relation refinements."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from . import vault
from .kbdir import kb_dirname

EXTENSION_SCHEMA_VERSION = 1
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
_LABEL_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")
_STATUSES = {"active", "deprecated"}
_DIRECTIONS = {"directed", "symmetric"}
_ORIGINS = {
    "semantic_relation",
    "markdown_relation",
    "semantic_block",
    "semantic_unit",
    "frontmatter",
    "wikilink",
}
_KIND_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class RelationDefinition:
    key: str
    description: str
    family: str
    direction: str = "directed"
    parent: str | None = None
    inverse: str | None = None
    origins: frozenset[str] = frozenset({"semantic_relation"})
    aliases: tuple[str, ...] = ()
    source_kinds: frozenset[str] = frozenset()
    target_kinds: frozenset[str] = frozenset()
    projects: frozenset[str] = frozenset()
    page_types: frozenset[str] = frozenset()
    status: str = "active"
    replaced_by: str | None = None
    core: bool = False

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "key": self.key,
            "description": self.description,
            "family": self.family,
            "direction": self.direction,
            "origins": sorted(self.origins),
            "status": self.status,
        }
        for key, scalar_value in (
            ("parent", self.parent),
            ("inverse", self.inverse),
            ("replaced_by", self.replaced_by),
        ):
            if scalar_value:
                out[key] = scalar_value
        for key, collection_value in (
            ("aliases", self.aliases),
            ("source_kinds", self.source_kinds),
            ("target_kinds", self.target_kinds),
            ("projects", self.projects),
            ("page_types", self.page_types),
        ):
            if collection_value:
                out[key] = sorted(collection_value)
        return out


@dataclass(frozen=True)
class RelationResolution:
    raw: str
    canonical: str | None
    parent: str | None
    status: str
    definition: RelationDefinition | None
    replacement: str | None = None
    findings: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class RelationRegistry:
    core_version: int
    extension_hash: str
    core: dict[str, RelationDefinition]
    extensions: dict[str, RelationDefinition] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    findings: tuple[dict[str, str], ...] = ()

    @property
    def keys(self) -> frozenset[str]:
        return frozenset((*self.core, *self.extensions))

    @property
    def families(self) -> frozenset[str]:
        return frozenset(item.family for item in (*self.core.values(), *self.extensions.values()))

    def definition(self, key: str) -> RelationDefinition | None:
        return self.core.get(key) or self.extensions.get(key)

    def resolve(
        self,
        raw: str,
        *,
        project: str | None = None,
        page_type: str | None = None,
        source_kind: str | None = None,
        target_kind: str | None = None,
        origin: str | None = None,
    ) -> RelationResolution:
        label = normalize_relation(raw)
        canonical = self.aliases.get(label, label)
        definition = self.definition(canonical)
        if definition is None:
            return RelationResolution(
                raw=raw, canonical=None, parent=None, status="unregistered", definition=None
            )
        status = "alias" if canonical != label else ("core" if definition.core else "extension")
        findings: list[dict[str, str]] = []
        if definition.status == "deprecated":
            status = "deprecated"
        scope_checks = (
            (definition.projects, project, "project"),
            (definition.page_types, page_type, "page_type"),
            (definition.source_kinds, source_kind, "source_kind"),
            (definition.target_kinds, target_kind, "target_kind"),
            (definition.origins, origin, "origin"),
        )
        for allowed, actual, label_name in scope_checks:
            if allowed and actual is not None and actual not in allowed:
                findings.append(
                    _finding(
                        "scope_violation",
                        f"relations.{canonical}.{label_name}",
                        f"{actual!r} is outside {sorted(allowed)!r}",
                        relation=canonical if not definition.core else None,
                    )
                )
        if findings:
            status = "scope_violation"
        return RelationResolution(
            raw=raw,
            canonical=canonical,
            parent=definition.parent,
            status=status,
            definition=definition,
            replacement=definition.replaced_by,
            findings=tuple(findings),
        )


def normalize_relation(label: str) -> str:
    return re.sub(r"[\s-]+", "_", str(label or "").strip().lower().rstrip(":"))


def extension_registry_path(vault_root: Path) -> Path:
    return Path(vault_root) / kb_dirname() / "_Schema" / "relation-registry.yaml"


@lru_cache(maxsize=1)
def core_registry() -> RelationRegistry:
    raw = files("exomem").joinpath("core-relations.yaml").read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    version = int(data["schema_version"])
    definitions: dict[str, RelationDefinition] = {}
    for key, value in data["relations"].items():
        definitions[key] = RelationDefinition(
            key=key,
            description=str(value["description"]),
            family=str(value["family"]),
            direction=str(value.get("direction", "directed")),
            inverse=value.get("inverse"),
            origins=frozenset(value.get("origins") or ["semantic_relation"]),
            core=True,
        )
    return RelationRegistry(version, "none", definitions)


_CACHE: dict[Path, tuple[str, RelationRegistry]] = {}


def load_registry(
    vault_root: Path | None = None, *, proposal: dict[str, Any] | None = None
) -> RelationRegistry:
    core = core_registry()
    if proposal is not None:
        raw = yaml.safe_dump(proposal, sort_keys=True)
        return _parse_extension_data(proposal, _content_hash(raw), core)
    if vault_root is None:
        return core
    path = extension_registry_path(vault_root)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return core
    digest = _content_hash(raw)
    cached = _CACHE.get(path)
    if cached and cached[0] == digest:
        return cached[1]
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        registry = RelationRegistry(
            core.core_version,
            digest,
            core.core,
            findings=(_finding("invalid_yaml", "registry", str(exc)),),
        )
    else:
        registry = _parse_extension_data(data, digest, core)
    _CACHE[path] = (digest, registry)
    return registry


def validate_proposal(proposal: dict[str, Any]) -> list[dict[str, str]]:
    return list(load_registry(proposal=proposal).findings)


def save_registry(
    vault_root: Path,
    proposal: dict[str, Any],
    *,
    expected_hash: str | None = None,
    observed_keys: Iterable[str] = (),
) -> dict[str, Any]:
    registry = load_registry(proposal=proposal)
    if registry.findings:
        raise ValueError(f"INVALID_RELATION_REGISTRY: {list(registry.findings)!r}")
    proposed_keys = set(registry.extensions) | set(registry.aliases)
    removed = sorted(set(observed_keys) - proposed_keys - set(registry.core))
    if removed:
        raise ValueError(f"OBSERVED_RELATION_DELETION: deprecate observed keys instead: {removed}")
    path = extension_registry_path(vault_root)
    current_hash: str | None = None
    if path.exists():
        current = path.read_text(encoding="utf-8")
        current_hash = _content_hash(current)
        if expected_hash is None:
            raise ValueError("REGISTRY_EXISTS: provide current expected_hash")
        if expected_hash != current_hash:
            raise ValueError("STALE_RELATION_REGISTRY: expected_hash does not match current hash")
    rendered = yaml.safe_dump(proposal, sort_keys=False, allow_unicode=True)
    vault.batch_atomic_write(
        [vault.PlannedWrite(path=path, content=rendered)], vault_root=vault_root
    )
    _CACHE.pop(path, None)
    return {
        "path": path.relative_to(vault_root).as_posix(),
        "content_hash": _content_hash(rendered),
        "previous_hash": current_hash,
        "created": current_hash is None,
    }


def empty_proposal() -> dict[str, Any]:
    return {"schema_version": EXTENSION_SCHEMA_VERSION, "extensions": {}}


def _parse_extension_data(data: Any, digest: str, core: RelationRegistry) -> RelationRegistry:
    findings: list[dict[str, str]] = []
    if not isinstance(data, dict):
        return RelationRegistry(
            core.core_version,
            digest,
            core.core,
            findings=(_finding("invalid_registry", "registry", "must be an object"),),
        )
    allowed_root = {"schema_version", "extensions"}
    for key in sorted(set(data) - allowed_root):
        findings.append(_finding("unknown_field", key, "unknown registry field"))
    if data.get("schema_version") != EXTENSION_SCHEMA_VERSION:
        findings.append(
            _finding("invalid_version", "schema_version", f"must be {EXTENSION_SCHEMA_VERSION}")
        )
    raw_extensions = data.get("extensions") or {}
    if not isinstance(raw_extensions, dict):
        findings.append(_finding("invalid_extensions", "extensions", "must be an object"))
        raw_extensions = {}
    extensions: dict[str, RelationDefinition] = {}
    aliases: dict[str, str] = {}
    occupied = set(core.core)
    allowed_fields = {
        "description",
        "parent",
        "family",
        "direction",
        "inverse",
        "origins",
        "aliases",
        "source_kinds",
        "target_kinds",
        "scope",
        "status",
        "replaced_by",
    }
    for raw_key, value in raw_extensions.items():
        key = str(raw_key)
        span = f"extensions.{key}"
        if not _KEY_RE.fullmatch(key):
            findings.append(
                _finding(
                    "invalid_key",
                    span,
                    "must be lowercase namespaced <namespace>.<name>",
                    relation=key,
                )
            )
        if key in occupied:
            findings.append(
                _finding(
                    "collision",
                    span,
                    "canonical key collides with an existing relation",
                    relation=key,
                )
            )
        occupied.add(key)
        if not isinstance(value, dict):
            findings.append(
                _finding(
                    "invalid_definition",
                    span,
                    "must be an object",
                    relation=key,
                )
            )
            continue
        for unknown in sorted(set(value) - allowed_fields):
            findings.append(
                _finding(
                    "unknown_field",
                    f"{span}.{unknown}",
                    "unknown definition field",
                    relation=key,
                )
            )
        parent = value.get("parent")
        parent_key = parent if isinstance(parent, str) else ""
        description = str(value.get("description") or "").strip()
        if parent not in core.core:
            findings.append(
                _finding(
                    "invalid_parent",
                    f"{span}.parent",
                    "must name exactly one core relation",
                    relation=key,
                )
            )
        if not description:
            findings.append(
                _finding(
                    "missing_description",
                    f"{span}.description",
                    "is required",
                    relation=key,
                )
            )
        direction = str(
            value.get("direction")
            or core.core.get(parent_key, RelationDefinition("", "", "")).direction
        )
        if direction not in _DIRECTIONS:
            findings.append(
                _finding(
                    "invalid_direction",
                    f"{span}.direction",
                    f"must be one of {sorted(_DIRECTIONS)}",
                    relation=key,
                )
            )
        origins = _strings(
            value.get("origins") or ["semantic_relation"],
            f"{span}.origins",
            findings,
            relation=key,
        )
        if not origins or not set(origins) <= _ORIGINS:
            findings.append(
                _finding(
                    "invalid_origins",
                    f"{span}.origins",
                    f"must use {sorted(_ORIGINS)}",
                    relation=key,
                )
            )
        status = str(value.get("status") or "active")
        if status not in _STATUSES:
            findings.append(
                _finding(
                    "invalid_status",
                    f"{span}.status",
                    f"must be one of {sorted(_STATUSES)}",
                    relation=key,
                )
            )
        scope = value.get("scope") or {}
        if not isinstance(scope, dict) or set(scope) - {"projects", "page_types"}:
            findings.append(
                _finding(
                    "invalid_scope",
                    f"{span}.scope",
                    "only projects and page_types are allowed",
                    relation=key,
                )
            )
            scope = {}
        definition = RelationDefinition(
            key=key,
            description=description,
            family=str(
                value.get("family") or (core.core[parent].family if parent in core.core else "")
            ),
            direction=direction,
            parent=parent if isinstance(parent, str) else None,
            inverse=_optional(value.get("inverse")),
            origins=frozenset(origins),
            aliases=tuple(
                normalize_relation(alias)
                for alias in _strings(
                    value.get("aliases") or [],
                    f"{span}.aliases",
                    findings,
                    relation=key,
                )
            ),
            source_kinds=frozenset(
                _strings(
                    value.get("source_kinds") or [],
                    f"{span}.source_kinds",
                    findings,
                    relation=key,
                )
            ),
            target_kinds=frozenset(
                _strings(
                    value.get("target_kinds") or [],
                    f"{span}.target_kinds",
                    findings,
                    relation=key,
                )
            ),
            projects=frozenset(
                _strings(
                    scope.get("projects") or [],
                    f"{span}.scope.projects",
                    findings,
                    relation=key,
                )
            ),
            page_types=frozenset(
                _strings(
                    scope.get("page_types") or [],
                    f"{span}.scope.page_types",
                    findings,
                    relation=key,
                )
            ),
            status=status,
            replaced_by=_optional(value.get("replaced_by")),
            core=False,
        )
        extensions[key] = definition
    canonical = set(core.core) | set(extensions)
    for key, definition in extensions.items():
        for alias in definition.aliases:
            span = f"extensions.{key}.aliases"
            if not _LABEL_RE.fullmatch(alias):
                findings.append(
                    _finding(
                        "invalid_alias",
                        span,
                        f"invalid alias {alias!r}",
                        relation=key,
                    )
                )
            if alias in occupied or alias in aliases:
                findings.append(
                    _finding(
                        "collision",
                        span,
                        f"alias {alias!r} collides",
                        relation=key,
                    )
                )
            else:
                aliases[alias] = key
                occupied.add(alias)
        if definition.inverse and definition.inverse not in canonical:
            findings.append(
                _finding(
                    "invalid_inverse",
                    f"extensions.{key}.inverse",
                    "must resolve to a canonical relation",
                    relation=key,
                )
            )
        if definition.replaced_by and definition.replaced_by not in canonical:
            findings.append(
                _finding(
                    "invalid_replacement",
                    f"extensions.{key}.replaced_by",
                    "must resolve to a canonical relation",
                    relation=key,
                )
            )
        if definition.replaced_by:
            replacement = extensions.get(definition.replaced_by)
            if replacement and replacement.status == "deprecated":
                findings.append(
                    _finding(
                        "invalid_replacement",
                        f"extensions.{key}.replaced_by",
                        "replacement must be active",
                        relation=key,
                    )
                )
        if definition.status == "deprecated" and not definition.replaced_by:
            findings.append(
                _finding(
                    "missing_replacement",
                    f"extensions.{key}.replaced_by",
                    "deprecated relations require a replacement",
                    relation=key,
                )
            )
        if definition.status == "active" and definition.replaced_by:
            findings.append(
                _finding(
                    "invalid_replacement",
                    f"extensions.{key}.replaced_by",
                    "only deprecated relations may declare a replacement",
                    relation=key,
                )
            )
        if definition.replaced_by == key or definition.inverse == key:
            findings.append(
                _finding(
                    "relation_cycle",
                    f"extensions.{key}",
                    "self cycles are not allowed",
                    relation=key,
                )
            )
        for kind_field, kinds in (
            ("source_kinds", definition.source_kinds),
            ("target_kinds", definition.target_kinds),
        ):
            for kind in kinds:
                if not _KIND_RE.fullmatch(kind):
                    findings.append(
                        _finding(
                            "invalid_node_kind",
                            f"extensions.{key}.{kind_field}",
                            f"invalid node kind {kind!r}",
                            relation=key,
                        )
                    )
    findings.extend(_cycle_findings(extensions, "replaced_by", allow_pair=False))
    findings.extend(_cycle_findings(extensions, "inverse", allow_pair=True))
    return RelationRegistry(
        core.core_version,
        digest,
        core.core,
        extensions,
        aliases,
        tuple(sorted(findings, key=lambda x: (x["path"], x["code"], x["detail"]))),
    )


def _strings(
    value: Any,
    path: str,
    findings: list[dict[str, str]],
    *,
    relation: str,
) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        findings.append(
            _finding(
                "invalid_list",
                path,
                "must be a list of strings",
                relation=relation,
            )
        )
        return []
    return list(dict.fromkeys(item.strip() for item in value if item.strip()))


def _optional(value: Any) -> str | None:
    return str(value).strip() if value not in (None, "") else None


def _finding(
    code: str,
    path: str,
    detail: str,
    *,
    relation: str | None = None,
) -> dict[str, str]:
    finding = {
        "code": code,
        "path": path,
        "span": path,
        "severity": "error",
        "detail": detail,
    }
    if relation is not None:
        finding["relation"] = relation
    return finding


def _content_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cycle_findings(
    extensions: dict[str, RelationDefinition],
    field_name: str,
    *,
    allow_pair: bool,
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    reported: set[frozenset[str]] = set()
    for start in extensions:
        order: list[str] = []
        positions: dict[str, int] = {}
        current: str | None = start
        while current in extensions and current not in positions:
            positions[current] = len(order)
            order.append(current)
            current = getattr(extensions[current], field_name)
        if current is None or current not in positions:
            continue
        cycle = order[positions[current] :]
        cycle_key = frozenset(cycle)
        if cycle_key in reported or (allow_pair and len(cycle) == 2):
            continue
        reported.add(cycle_key)
        findings.append(
            _finding(
                "relation_cycle",
                f"extensions.{start}.{field_name}",
                f"{field_name} cycle is not allowed: {' -> '.join(cycle + [cycle[0]])}",
                relation=start,
            )
        )
    return findings
