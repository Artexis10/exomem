"""Governed, read-only traversal lenses over the derived relation graph."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from . import relation_registry, vault
from .kbdir import kb_dirname

SCHEMA_VERSION = 1
MAX_DEPTH = 5
MAX_NODES = 200
MAX_EDGES = 400
_DIRECTIONS = {"both", "outgoing", "incoming"}
_FIELDS = {
    "extends", "add_families", "remove_families", "add_relations", "remove_relations",
    "direction", "priority", "include_extensions", "max_depth", "max_nodes", "max_edges",
}


@dataclass(frozen=True)
class TraversalProfile:
    name: str
    families: frozenset[str]
    relations: frozenset[str] = frozenset()
    direction: str = "both"
    priority: tuple[str, ...] = ()
    include_extensions: bool = True
    max_depth: int = MAX_DEPTH
    max_nodes: int = MAX_NODES
    max_edges: int = MAX_EDGES
    extends: str | None = None
    builtin: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "extends": self.extends,
            "families": sorted(self.families),
            "relations": sorted(self.relations),
            "direction": self.direction,
            "priority": list(self.priority),
            "include_extensions": self.include_extensions,
            "max_depth": self.max_depth,
            "max_nodes": self.max_nodes,
            "max_edges": self.max_edges,
            "builtin": self.builtin,
        }


@dataclass(frozen=True)
class ProfileRegistry:
    profiles: dict[str, TraversalProfile]
    content_hash: str
    findings: tuple[dict[str, str], ...] = ()

    def resolve(self, name: str | None) -> TraversalProfile:
        selected = name or "all"
        profile = self.profiles.get(selected)
        if profile is None:
            raise ValueError(f"INVALID_TRAVERSAL_PROFILE: unknown or invalid profile {selected!r}")
        return profile


def builtin_profiles(registry: relation_registry.RelationRegistry | None = None) -> dict[str, TraversalProfile]:
    registry = registry or relation_registry.core_registry()
    all_families = frozenset(item.family for item in registry.core.values())
    values = {
        "epistemic": {"support", "contradiction", "refinement", "duplication", "supersession", "question", "answer"},
        "provenance": {"derivation", "evidence", "citation", "observation"},
        "causal": {"causality", "dependency", "mitigation", "blocking", "resolution"},
        "decision": {"evidence", "derivation", "dependency", "implementation", "use", "mitigation", "resolution"},
        "all": set(all_families),
    }
    return {
        name: TraversalProfile(
            name=name,
            families=frozenset(families),
            priority=tuple(sorted(families)),
        )
        for name, families in values.items()
    }


def profile_path(vault_root: Path) -> Path:
    return Path(vault_root) / kb_dirname() / "_Schema" / "traversal-profiles.yaml"


_CACHE: dict[Path, tuple[str, ProfileRegistry]] = {}


def load_profiles(
    vault_root: Path | None = None,
    *,
    proposal: dict[str, Any] | None = None,
    registry: relation_registry.RelationRegistry | None = None,
) -> ProfileRegistry:
    registry = registry or relation_registry.load_registry(vault_root)
    builtins = builtin_profiles(registry)
    if proposal is not None:
        raw = yaml.safe_dump(proposal, sort_keys=True)
        return _parse(proposal, _hash(raw), registry, builtins)
    if vault_root is None:
        return ProfileRegistry(builtins, "none")
    path = profile_path(vault_root)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ProfileRegistry(builtins, "none")
    digest = _hash(raw)
    cached = _CACHE.get(path)
    if cached and cached[0] == digest:
        return cached[1]
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        result = ProfileRegistry(builtins, digest, (_finding("invalid_yaml", "profiles", str(exc)),))
    else:
        result = _parse(data, digest, registry, builtins)
    _CACHE[path] = (digest, result)
    return result


def save_profiles(
    vault_root: Path,
    proposal: dict[str, Any],
    *,
    expected_hash: str | None = None,
) -> dict[str, Any]:
    parsed = load_profiles(vault_root, proposal=proposal)
    if parsed.findings:
        raise ValueError(f"INVALID_TRAVERSAL_PROFILES: {list(parsed.findings)!r}")
    path = profile_path(vault_root)
    previous: str | None = None
    if path.exists():
        previous = _hash(path.read_text(encoding="utf-8"))
        if expected_hash is None:
            raise ValueError("TRAVERSAL_PROFILES_EXIST: provide current expected_hash")
        if expected_hash != previous:
            raise ValueError("STALE_TRAVERSAL_PROFILES: expected_hash does not match current hash")
    rendered = yaml.safe_dump(proposal, sort_keys=False, allow_unicode=True)
    vault.batch_atomic_write([vault.PlannedWrite(path=path, content=rendered)], vault_root=vault_root)
    _CACHE.pop(path, None)
    return {"path": path.relative_to(vault_root).as_posix(), "content_hash": _hash(rendered), "previous_hash": previous, "created": previous is None}


def relation_allowed(
    profile: TraversalProfile,
    definition: relation_registry.RelationDefinition,
) -> bool:
    if definition.key in profile.relations:
        return True
    if definition.parent and not profile.include_extensions:
        return False
    return definition.family in profile.families


def narrow_relations(
    profile: TraversalProfile,
    requested: list[str] | None,
    registry: relation_registry.RelationRegistry,
) -> frozenset[str] | None:
    if not requested:
        return None
    selected: set[str] = set()
    for raw in requested:
        resolution = registry.resolve(raw)
        if resolution.canonical is None or resolution.definition is None:
            continue
        requested_definition = resolution.definition
        for definition in (*registry.core.values(), *registry.extensions.values()):
            if definition.key == requested_definition.key or (
                profile.include_extensions and definition.parent == requested_definition.key
            ):
                if relation_allowed(profile, definition):
                    selected.add(definition.key)
    return frozenset(selected)


def _parse(
    data: Any,
    digest: str,
    registry: relation_registry.RelationRegistry,
    builtins: dict[str, TraversalProfile],
) -> ProfileRegistry:
    findings: list[dict[str, str]] = []
    if not isinstance(data, dict):
        return ProfileRegistry(builtins, digest, (_finding("invalid_profiles", "profiles", "must be an object"),))
    for key in sorted(set(data) - {"schema_version", "profiles"}):
        findings.append(_finding("unknown_field", key, "unknown profile registry field"))
    if data.get("schema_version") != SCHEMA_VERSION:
        findings.append(_finding("invalid_version", "schema_version", f"must be {SCHEMA_VERSION}"))
    values = data.get("profiles") or {}
    if not isinstance(values, dict):
        findings.append(_finding("invalid_profiles", "profiles", "must be an object"))
        values = {}
    profiles = dict(builtins)
    for raw_name, value in values.items():
        name = str(raw_name)
        span = f"profiles.{name}"
        if name in builtins:
            findings.append(_finding("immutable_builtin", span, "built-in profiles cannot be redefined"))
            continue
        if not isinstance(value, dict):
            findings.append(_finding("invalid_profile", span, "must be an object"))
            continue
        for unknown in sorted(set(value) - _FIELDS):
            findings.append(_finding("unknown_field", f"{span}.{unknown}", "unknown profile field"))
        base_name = value.get("extends")
        if base_name not in builtins:
            findings.append(_finding("invalid_parent", f"{span}.extends", "must extend one built-in profile"))
            continue
        base = builtins[str(base_name)]
        add_families = _list(value.get("add_families"), f"{span}.add_families", findings)
        remove_families = _list(value.get("remove_families"), f"{span}.remove_families", findings)
        add_relations = _list(value.get("add_relations"), f"{span}.add_relations", findings)
        remove_relations = _list(value.get("remove_relations"), f"{span}.remove_relations", findings)
        for family in (*add_families, *remove_families):
            if family not in registry.families:
                findings.append(_finding("unknown_family", span, f"unregistered family {family!r}"))
        canonical_add = _canonical(add_relations, registry, span, findings)
        canonical_remove = _canonical(remove_relations, registry, span, findings)
        direction = str(value.get("direction", base.direction))
        if direction not in _DIRECTIONS:
            findings.append(_finding("invalid_direction", f"{span}.direction", f"must be one of {sorted(_DIRECTIONS)}"))
        priority = _list(value.get("priority"), f"{span}.priority", findings) or list(base.priority)
        for item in priority:
            if item not in registry.families and registry.resolve(item).canonical is None:
                findings.append(_finding("unknown_priority", f"{span}.priority", f"unregistered item {item!r}"))
        caps = {}
        for key, maximum, default in (
            ("max_depth", MAX_DEPTH, base.max_depth),
            ("max_nodes", MAX_NODES, base.max_nodes),
            ("max_edges", MAX_EDGES, base.max_edges),
        ):
            try:
                cap = int(value.get(key, default))
            except (TypeError, ValueError):
                cap = maximum + 1
            if cap < 0 or cap > maximum or cap > default:
                findings.append(_finding("invalid_cap", f"{span}.{key}", f"must be between 0 and built-in bound {default}"))
                cap = default
            caps[key] = cap
        profiles[name] = replace(
            base,
            name=name,
            families=frozenset((set(base.families) | set(add_families)) - set(remove_families)),
            relations=frozenset((set(base.relations) | canonical_add) - canonical_remove),
            direction=direction,
            priority=tuple(priority),
            include_extensions=bool(value.get("include_extensions", base.include_extensions)),
            extends=str(base_name),
            builtin=False,
            **caps,
        )
    return ProfileRegistry(profiles, digest, tuple(sorted(findings, key=lambda x: (x["path"], x["code"], x["detail"]))))


def _canonical(values: list[str], registry: relation_registry.RelationRegistry, span: str, findings: list[dict[str, str]]) -> set[str]:
    out: set[str] = set()
    for value in values:
        resolved = registry.resolve(value)
        if resolved.canonical is None:
            findings.append(_finding("unknown_relation", span, f"unregistered relation {value!r}"))
        else:
            out.add(resolved.canonical)
    return out


def _list(value: Any, path: str, findings: list[dict[str, str]]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        findings.append(_finding("invalid_list", path, "must be a list of strings"))
        return []
    return list(dict.fromkeys(str(item) for item in value))


def _finding(code: str, path: str, detail: str) -> dict[str, str]:
    return {"code": code, "path": path, "span": path, "severity": "error", "detail": detail}


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
