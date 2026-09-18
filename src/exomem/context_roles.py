"""The context-role registry: which lenses a working-memory packet is built from.

A role answers one question about an anchor — what it is, what bounds it, what
state it is in, what is already planned for it. The vocabulary lives in a
reviewed YAML registry rather than in code for the same reason relation types and
traversal profiles do: it is a governance artifact the owner extends, and a
server component that edited it would be authoring policy.

Load order mirrors `traversal_profiles`: the shipped registry ships in the skill
scaffold (and its byte-identical plugin copy), a vault override may add to it or
narrow its defaults, and a broken override falls back to the shipped registry
with the failure reported rather than swallowed. Selection is pure: the union of
each resolved anchor kind's defaults and the roles whose cues match the turn, in
registry priority order, bounded — no model call and no randomness.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from .kbdir import kb_dirname

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
REGISTRY_FILENAME = "context-roles.yaml"
MAX_SELECTED_ROLES = 6

#: Retrieval lanes a role may map to. A role that named anything else would
#: select nothing, so an unknown lane is a finding rather than a silent no-op.
LANES: tuple[str, ...] = ("units", "records", "planning", "entity", "graph", "evidence")

#: Anchor kinds a role may default for. Kept in step with `working_set_index`.
ANCHOR_KINDS: tuple[str, ...] = (
    "entity",
    "resource",
    "hub",
    "collection",
    "plan",
    "project",
)

#: Fields an override may set on a shipped role. `remove`/`id` are deliberately
#: absent: a packet that silently lost `constraints` would read as "there are no
#: constraints", and a renamed role would break every override that referenced
#: the old name.
_OVERRIDE_FIELDS = frozenset(
    {
        "description",
        "lane",
        "categories",
        "anchor_defaults",
        "cues",
        "add_categories",
        "add_cues",
    }
)
_NEW_ROLE_FIELDS = frozenset(
    {"description", "lane", "categories", "anchor_defaults", "cues"}
)


@dataclass(frozen=True, slots=True)
class ContextRole:
    """One lens. `priority` is the registry's own declaration order."""

    id: str
    description: str
    lane: str
    categories: frozenset[str]
    anchor_defaults: frozenset[str]
    cues: tuple[str, ...]
    priority: int
    shipped: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "lane": self.lane,
            "categories": sorted(self.categories),
            "anchor_defaults": sorted(self.anchor_defaults),
            "cues": list(self.cues),
            "shipped": self.shipped,
        }


@dataclass(frozen=True, slots=True)
class RoleRegistry:
    """The effective registry for one vault, plus how it was resolved."""

    roles: Mapping[str, ContextRole]
    source: str
    roles_hash: str
    findings: tuple[dict[str, str], ...] = field(default_factory=tuple)

    def generation_block(self) -> dict[str, Any]:
        """What the packet's `generation` block reports about roles."""
        block: dict[str, Any] = {
            "roles_hash": self.roles_hash,
            "roles_source": self.source,
        }
        if self.findings:
            block["roles_warnings"] = [dict(finding) for finding in self.findings]
        return block


_CACHE: dict[Path, tuple[str, RoleRegistry]] = {}
_SHIPPED: RoleRegistry | None = None


def _finding(code: str, field_name: str, message: str) -> dict[str, str]:
    return {"code": code, "field": field_name, "message": message}


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def shipped_registry_text() -> str:
    """The packaged registry's bytes — one source for server, skill and plugin."""
    resource = files("exomem").joinpath("_scaffold", "_Schema", REGISTRY_FILENAME)
    return resource.read_text(encoding="utf-8")


def override_path(vault_root: Path) -> Path:
    """Where a vault's own registry lives, if the owner authored one."""
    return Path(vault_root) / kb_dirname() / "_Schema" / REGISTRY_FILENAME


def clear_cache() -> None:
    """Drop the memoized registries (tests; overrides are edited out of band)."""
    global _SHIPPED
    _CACHE.clear()
    _SHIPPED = None


def shipped_registry() -> RoleRegistry:
    """Parse the packaged registry once per process."""
    global _SHIPPED
    if _SHIPPED is None:
        raw = shipped_registry_text()
        data = yaml.safe_load(raw)
        roles, findings = _parse_shipped(data)
        if findings:
            # A broken SHIPPED registry is a build defect, not a runtime state.
            raise RuntimeError(f"packaged context-role registry is invalid: {findings[0]}")
        _SHIPPED = RoleRegistry(roles=roles, source="shipped", roles_hash=_hash(raw))
    return _SHIPPED


def load_roles(vault_root: Path | None = None, *, proposal: Any | None = None) -> RoleRegistry:
    """Return the effective registry: shipped, or shipped plus a vault override."""
    shipped = shipped_registry()
    if proposal is not None:
        raw = yaml.safe_dump(proposal, sort_keys=True)
        return _merge(shipped, proposal, _hash(raw))
    if vault_root is None:
        return shipped
    path = override_path(vault_root)
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return shipped
    except OSError:
        log.warning("context-role override unreadable at %s", path, exc_info=True)
        return replace(
            shipped, findings=(_finding("unreadable", "roles", "override could not be read"),)
        )
    digest = _hash(raw)
    cached = _CACHE.get(path)
    if cached is not None and cached[0] == digest:
        return cached[1]
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        registry = replace(
            shipped, findings=(_finding("invalid_yaml", "roles", str(exc)),)
        )
    else:
        registry = _merge(shipped, data, digest)
    _CACHE[path] = (digest, registry)
    return registry


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def _strings(value: object, *, lower: bool = True) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip().casefold() if lower else item.strip())
    return tuple(dict.fromkeys(out))


def _parse_shipped(data: Any) -> tuple[dict[str, ContextRole], tuple[dict[str, str], ...]]:
    findings: list[dict[str, str]] = []
    if not isinstance(data, Mapping):
        return {}, (_finding("invalid_registry", "roles", "must be a mapping"),)
    if data.get("schema_version") != SCHEMA_VERSION:
        findings.append(_finding("invalid_version", "schema_version", f"must be {SCHEMA_VERSION}"))
    values = data.get("roles")
    if not isinstance(values, Mapping) or not values:
        findings.append(_finding("invalid_roles", "roles", "must be a non-empty mapping"))
        return {}, tuple(findings)
    roles: dict[str, ContextRole] = {}
    for priority, (name, raw) in enumerate(values.items()):
        if not isinstance(name, str) or not isinstance(raw, Mapping):
            findings.append(_finding("invalid_role", str(name), "role must be a mapping"))
            continue
        unknown = sorted(set(raw) - _NEW_ROLE_FIELDS)
        if unknown:
            findings.append(_finding("unknown_field", f"{name}.{unknown[0]}", "unknown role field"))
        lane = raw.get("lane")
        if lane not in LANES:
            findings.append(_finding("invalid_lane", f"{name}.lane", f"lane must be one of {LANES}"))
            continue
        defaults = _strings(raw.get("anchor_defaults"))
        bad = sorted(set(defaults) - set(ANCHOR_KINDS))
        if bad:
            findings.append(
                _finding("invalid_anchor_kind", f"{name}.anchor_defaults", f"unknown kind {bad[0]}")
            )
        roles[name] = ContextRole(
            id=name,
            description=str(raw.get("description") or "").strip(),
            lane=str(lane),
            categories=frozenset(_strings(raw.get("categories"))),
            anchor_defaults=frozenset(kind for kind in defaults if kind in ANCHOR_KINDS),
            cues=_strings(raw.get("cues")),
            priority=priority,
        )
    return roles, tuple(findings)


def _merge(shipped: RoleRegistry, data: Any, digest: str) -> RoleRegistry:
    """Apply a vault override: add or narrow, never remove and never rename."""
    findings: list[dict[str, str]] = []
    if not isinstance(data, Mapping):
        return replace(
            shipped,
            findings=(_finding("invalid_registry", "roles", "override must be a mapping"),),
        )
    if data.get("schema_version") != SCHEMA_VERSION:
        findings.append(_finding("invalid_version", "schema_version", f"must be {SCHEMA_VERSION}"))
    values = data.get("roles")
    if not isinstance(values, Mapping):
        findings.append(_finding("invalid_roles", "roles", "roles must be a mapping"))
        return replace(shipped, findings=tuple(findings))

    roles = dict(shipped.roles)
    next_priority = len(roles)
    for name, raw in values.items():
        if not isinstance(name, str) or not isinstance(raw, Mapping):
            findings.append(_finding("invalid_role", str(name), "role must be a mapping"))
            continue
        existing = roles.get(name)
        if existing is None:
            role, role_findings = _new_role(name, raw, next_priority)
            findings.extend(role_findings)
            if role is not None:
                roles[name] = role
                next_priority += 1
            continue
        if raw.get("remove") is True or raw.get("removed") is True:
            findings.append(
                _finding(
                    "role_removal_refused",
                    f"{name}.remove",
                    "a shipped role cannot be removed; narrow anchor_defaults instead",
                )
            )
        unknown = sorted(set(raw) - _OVERRIDE_FIELDS)
        for key in unknown:
            if key in {"remove", "removed"}:
                continue
            findings.append(
                _finding("unknown_field", f"{name}.{key}", "unknown override field")
            )
        roles[name] = _narrowed(existing, raw, findings)

    ordered = dict(sorted(roles.items(), key=lambda item: item[1].priority))
    source = "vault" if not any(f["code"] == "invalid_yaml" for f in findings) else "shipped"
    return RoleRegistry(
        roles=ordered,
        source=source,
        roles_hash=_hash(f"{shipped.roles_hash}:{digest}"),
        findings=tuple(findings),
    )


def _new_role(
    name: str, raw: Mapping[str, Any], priority: int
) -> tuple[ContextRole | None, list[dict[str, str]]]:
    findings: list[dict[str, str]] = []
    unknown = sorted(set(raw) - _NEW_ROLE_FIELDS)
    for key in unknown:
        findings.append(_finding("unknown_field", f"{name}.{key}", "unknown role field"))
    lane = raw.get("lane")
    if lane not in LANES:
        findings.append(_finding("invalid_lane", f"{name}.lane", f"lane must be one of {LANES}"))
        return None, findings
    defaults = _strings(raw.get("anchor_defaults"))
    return (
        ContextRole(
            id=name,
            description=str(raw.get("description") or "").strip(),
            lane=str(lane),
            categories=frozenset(_strings(raw.get("categories"))),
            anchor_defaults=frozenset(kind for kind in defaults if kind in ANCHOR_KINDS),
            cues=_strings(raw.get("cues")),
            priority=priority,
            shipped=False,
        ),
        findings,
    )


def _narrowed(
    role: ContextRole, raw: Mapping[str, Any], findings: list[dict[str, str]]
) -> ContextRole:
    categories = set(role.categories)
    cues = list(role.cues)
    defaults = set(role.anchor_defaults)

    if "categories" in raw:
        replacement = _strings(raw.get("categories"))
        if replacement or isinstance(raw.get("categories"), (list, tuple)):
            categories = set(replacement) | set(role.categories)
        else:
            findings.append(
                _finding("invalid_categories", f"{role.id}.categories", "must be a string list")
            )
    categories |= set(_strings(raw.get("add_categories")))
    cues.extend(cue for cue in _strings(raw.get("add_cues")) if cue not in cues)
    if "cues" in raw:
        replacement = _strings(raw.get("cues"))
        if replacement or isinstance(raw.get("cues"), (list, tuple)):
            cues = list(dict.fromkeys([*role.cues, *replacement]))
        else:
            findings.append(_finding("invalid_cues", f"{role.id}.cues", "must be a string list"))
    if "anchor_defaults" in raw:
        narrowed = raw.get("anchor_defaults")
        if isinstance(narrowed, (list, tuple)):
            requested = set(_strings(narrowed))
            # Narrowing only: an override cannot widen a role onto an anchor kind
            # the shipped registry never gave it.
            defaults = requested & set(role.anchor_defaults)
        else:
            findings.append(
                _finding(
                    "invalid_anchor_defaults",
                    f"{role.id}.anchor_defaults",
                    "must be a string list",
                )
            )
    if "lane" in raw and raw.get("lane") not in LANES:
        findings.append(
            _finding("invalid_lane", f"{role.id}.lane", f"lane must be one of {LANES}")
        )
    lane = str(raw["lane"]) if raw.get("lane") in LANES else role.lane
    return replace(
        role,
        lane=lane,
        categories=frozenset(categories),
        anchor_defaults=frozenset(defaults),
        cues=tuple(cues),
    )


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #


def select_roles(
    registry: RoleRegistry,
    *,
    anchor_kinds: Sequence[str],
    analysis: Any,
    limit: int = MAX_SELECTED_ROLES,
) -> tuple[dict[str, str], ...]:
    """Defaults for each resolved anchor kind, plus cue matches, in registry order.

    Returns `[{"id", "source", "lane"}]` where `source` is `anchor_default` or
    `turn_cue`, so a reader can see WHY a lane ran. No anchor kinds means no
    packet: roles are lenses onto an anchor, not a standing subscription.
    """
    if not anchor_kinds:
        return ()
    kinds = frozenset(anchor_kinds)
    text = str(getattr(analysis, "text", "") or "")
    selected: list[dict[str, str]] = []
    for role in sorted(registry.roles.values(), key=lambda item: item.priority):
        if role.anchor_defaults & kinds:
            source = "anchor_default"
        elif role.cues and any(cue in text for cue in role.cues):
            source = "turn_cue"
        else:
            continue
        selected.append({"id": role.id, "source": source, "lane": role.lane})
    return tuple(selected[: max(0, int(limit))])
