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

from . import semantic_language_registry
from .kbdir import kb_dirname

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
REGISTRY_FILENAME = "context-roles.yaml"
MAX_SELECTED_ROLES = 6

#: Refused before parsing (mirrors `activation_conventions.MAX_FILE_BYTES`,
#: design.md decision 2): a typo-sized override is one thing, an
#: alias-expansion document is another, and this registry now BEARS
#: evidence (decision 1), so the same 256 KiB refusal applies here too.
MAX_FILE_BYTES = 256 * 1024

#: Roles/cues caps (design.md decision 6), new because decision 1 makes this
#: registry evidence-bearing: unbounded per-turn cue evaluation inside a
#: request budget. Entries past a cap are ignored and reported, never
#: silently dropped without a finding.
MAX_ROLES_PER_OVERRIDE = 32
MAX_CUES_PER_ROLE = 48
MAX_CUE_CHARS = 64
MAX_EVIDENCE_CATEGORIES_PER_ROLE = 8

#: An evidence cue must be at least this many characters long (design.md
#: decision 1) -- the bound a bare `?` fails, pinning the one intended
#: shipped difference from the deleted `CUE_PATTERNS` table.
MIN_EVIDENCE_CUE_CHARS = 3

#: `resolve_category` statuses that mean "not a real category" (mirrors
#: `working_set_index._categories`'s `excluded_statuses`): `unregistered`,
#: `registry_invalid` and `scope_violation` each set `.resolved` to the raw
#: label's own key rather than to nothing, so excluding them by status,
#: never by truthiness of `.resolved` alone, is what keeps a broken vault
#: registry from injecting a fabricated category as evidence.
_UNRESOLVED_CATEGORY_STATUSES = frozenset({"unregistered", "registry_invalid", "scope_violation"})

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
        "evidence_cues",
        "evidence_categories",
    }
)
_NEW_ROLE_FIELDS = frozenset(
    {
        "description",
        "lane",
        "categories",
        "anchor_defaults",
        "cues",
        "evidence_cues",
        "evidence_categories",
    }
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
    #: A cue in this list that ALSO passes the three evidence bounds (see
    #: `MIN_EVIDENCE_CUE_CHARS` and `working_set_resolve._is_evidence_cue`)
    #: makes `evidence_categories` eligible for `category_match` on a turn
    #: that contains it. Default none: most roles' cues stay selection-only
    #: (design.md decision 1).
    evidence_cues: tuple[str, ...] = ()
    evidence_categories: frozenset[str] = frozenset()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "lane": self.lane,
            "categories": sorted(self.categories),
            "anchor_defaults": sorted(self.anchor_defaults),
            "cues": list(self.cues),
            "evidence_cues": list(self.evidence_cues),
            "evidence_categories": sorted(self.evidence_categories),
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
        resolver = semantic_language_registry.load_registry(None)
        roles, findings = _parse_shipped(data, resolver)
        if findings:
            # A broken SHIPPED registry is a build defect, not a runtime state.
            raise RuntimeError(f"packaged context-role registry is invalid: {findings[0]}")
        _SHIPPED = RoleRegistry(roles=roles, source="shipped", roles_hash=_hash(raw))
    return _SHIPPED


def load_roles(vault_root: Path | None = None, *, proposal: Any | None = None) -> RoleRegistry:
    """Return the effective registry: shipped, or shipped plus a vault override."""
    shipped = shipped_registry()
    # The vault's OWN semantic-language registry when there is one (so a
    # `evidence_categories` entry naming a category the owner already
    # defined there resolves), the core-only registry otherwise — the same
    # rule `load_registry(None)` already applies for a stateless proposal.
    resolver = semantic_language_registry.load_registry(vault_root)
    if proposal is not None:
        raw = yaml.safe_dump(proposal, sort_keys=True)
        return _merge(shipped, proposal, _hash(raw), resolver)
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
    if len(raw.encode("utf-8")) > MAX_FILE_BYTES:
        return replace(
            shipped,
            findings=(_finding("file_too_large", "roles", f"override exceeds {MAX_FILE_BYTES} bytes"),),
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
        registry = _merge(shipped, data, digest, resolver)
    _CACHE[path] = (digest, registry)
    return registry


def save_roles(vault_root: Path, proposal: Any, *, expected_hash: str) -> dict[str, Any]:
    """Save one reviewed, complete context-role override document.

    The proposal is the raw override document -- the same shape as the file
    on disk -- never a delta (mirrors `traversal_profiles.save_profiles`).
    `expected_hash` is unconditionally required (design.md decision 7's
    `save-relations` pattern: no separate create/update branching, because
    the shipped registry always has a `roles_hash`, even with no override
    file yet) and is checked against the CURRENT effective registry's
    `roles_hash`.

    The rendered text uses the exact same `yaml.safe_dump(proposal,
    sort_keys=True)` call `load_roles`'s `proposal=` path hashes, so the
    round trip through disk reproduces the same `roles_hash` a caller who
    just validated this proposal already saw -- diverging here (say, by
    adding `allow_unicode=True`) would change the file's bytes without
    changing the caller's proposal, and the next load would report a
    DIFFERENT hash than the one just returned.

    Callers are expected to have already rejected a proposal with any
    finding (`op_schema_memory` does, before calling this); the check here
    is defence in depth, matching `semantic_language_registry.save_registry`.
    """
    current = load_roles(vault_root)
    if current.roles_hash != expected_hash:
        raise ValueError(
            "STALE_CONTEXT_ROLE_REGISTRY: expected_hash does not match current hash"
        )
    candidate = load_roles(vault_root, proposal=proposal)
    if candidate.findings:
        raise ValueError(
            f"INVALID_CONTEXT_ROLE_REGISTRY: {[dict(item) for item in candidate.findings]!r}"
        )
    path = override_path(vault_root)
    rendered = yaml.safe_dump(proposal, sort_keys=True)
    from . import vault as vault_module

    vault_module.batch_atomic_write(
        [vault_module.PlannedWrite(path=path, content=rendered)], vault_root=Path(vault_root)
    )
    _CACHE.pop(path, None)
    return {
        "path": path.relative_to(vault_root).as_posix(),
        "content_hash": candidate.roles_hash,
        "previous_hash": current.roles_hash,
        "created": current.source == "shipped",
    }


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


def _shipped_evidence_categories(
    value: object, *, role_name: str, resolver: Any, findings: list[dict[str, str]]
) -> frozenset[str]:
    """Validate shipped `evidence_categories` through `resolve_category` too
    (mirrors `working_set_index._categories`'s excluded-status check): a
    typo here is a build defect, and `shipped_registry()` already raises on
    ANY finding, so this doubles as the invariant that every shipped
    evidence category really is one the semantic-language registry knows.
    """
    accepted: set[str] = set()
    for item in _strings(value):
        resolution = resolver.resolve_category(item)
        if resolution.status in _UNRESOLVED_CATEGORY_STATUSES or not resolution.resolved:
            findings.append(
                _finding(
                    "invalid_evidence_category",
                    f"{role_name}.evidence_categories",
                    f"unregistered category {item!r}",
                )
            )
            continue
        accepted.add(resolution.resolved)
    return frozenset(accepted)


def _parse_shipped(
    data: Any, resolver: Any
) -> tuple[dict[str, ContextRole], tuple[dict[str, str], ...]]:
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
            evidence_cues=_strings(raw.get("evidence_cues")),
            evidence_categories=_shipped_evidence_categories(
                raw.get("evidence_categories"), role_name=name, resolver=resolver, findings=findings
            ),
        )
    return roles, tuple(findings)


def _merge(shipped: RoleRegistry, data: Any, digest: str, resolver: Any) -> RoleRegistry:
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

    items = list(values.items())
    if len(items) > MAX_ROLES_PER_OVERRIDE:
        findings.append(
            _finding(
                "cap_exceeded",
                "roles",
                f"{len(items) - MAX_ROLES_PER_OVERRIDE} role(s) past the {MAX_ROLES_PER_OVERRIDE} cap ignored",
            )
        )
        items = items[:MAX_ROLES_PER_OVERRIDE]

    roles = dict(shipped.roles)
    next_priority = len(roles)
    for name, raw in items:
        if not isinstance(name, str) or not isinstance(raw, Mapping):
            findings.append(_finding("invalid_role", str(name), "role must be a mapping"))
            continue
        existing = roles.get(name)
        if existing is None:
            role, role_findings = _new_role(name, raw, next_priority, resolver)
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
        roles[name] = _narrowed(existing, raw, findings, resolver)

    ordered = dict(sorted(roles.items(), key=lambda item: item[1].priority))
    source = "vault" if not any(f["code"] == "invalid_yaml" for f in findings) else "shipped"
    return RoleRegistry(
        roles=ordered,
        source=source,
        roles_hash=_hash(f"{shipped.roles_hash}:{digest}"),
        findings=tuple(findings),
    )


def _cap(
    items: Sequence[str], *, field_name: str, cap: int, findings: list[dict[str, str]]
) -> tuple[str, ...]:
    """One cap, ONCE, over the EFFECTIVE combined list (mirrors
    `activation_conventions._cap_tuple`): capping the incoming `add_*`/direct
    list on its own too would silently lose more than the cap describes and
    double-report one event."""
    deduped = tuple(dict.fromkeys(items))
    if len(deduped) > cap:
        findings.append(
            _finding("cap_exceeded", field_name, f"{len(deduped) - cap} entry(ies) past the {cap} cap ignored")
        )
    return deduped[:cap]


def _validated_evidence_cues(
    value: object, *, role_id: str, findings: list[dict[str, str]]
) -> tuple[str, ...]:
    """Strip, drop empty/over-length (dropped, with a finding), dedupe.

    A cue that is merely too SHORT or has no terms is KEPT rather than
    dropped -- design.md decision 1: it still SELECTS its role (every cue is
    matched as a substring), only evidence is stricter -- but flagged with a
    finding, since it can never be evidence. No cap here; the caller caps
    the combined list once (`_cap`).
    """
    if not isinstance(value, (list, tuple)):
        return ()
    accepted: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip().casefold()
        if not text:
            continue
        if len(text) > MAX_CUE_CHARS:
            findings.append(
                _finding(
                    "entry_too_long", f"{role_id}.evidence_cues", f"entry exceeds {MAX_CUE_CHARS} characters: {text!r}"
                )
            )
            continue
        from .working_set_index import tokens_of

        if len(text) < MIN_EVIDENCE_CUE_CHARS or not tokens_of(text):
            findings.append(
                _finding(
                    "evidence_cue_too_weak",
                    f"{role_id}.evidence_cues",
                    f"cue cannot be evidence (fewer than {MIN_EVIDENCE_CUE_CHARS} characters or no terms): {text!r}",
                )
            )
        accepted.append(text)
    return tuple(dict.fromkeys(accepted))


def _resolved_evidence_categories(
    value: object, *, role_id: str, resolver: Any, findings: list[dict[str, str]]
) -> tuple[str, ...]:
    """Validate through `resolve_category` (design.md decision 1): accepted
    only when the status is not unregistered/invalid/scope-violating, stored
    as the resolved key. No cap here; the caller caps the combined set once.
    """
    if not isinstance(value, (list, tuple)):
        return ()
    accepted: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            continue
        text = item.strip()
        resolution = resolver.resolve_category(text)
        if resolution.status in _UNRESOLVED_CATEGORY_STATUSES or not resolution.resolved:
            findings.append(
                _finding(
                    "invalid_evidence_category",
                    f"{role_id}.evidence_categories",
                    f"unregistered category {text!r}",
                )
            )
            continue
        accepted.append(resolution.resolved)
    return tuple(dict.fromkeys(accepted))


def _new_role(
    name: str, raw: Mapping[str, Any], priority: int, resolver: Any
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
    cues = _cap(
        _strings(raw.get("cues")), field_name=f"{name}.cues", cap=MAX_CUES_PER_ROLE, findings=findings
    )
    evidence_cues = _cap(
        _validated_evidence_cues(raw.get("evidence_cues"), role_id=name, findings=findings),
        field_name=f"{name}.evidence_cues",
        cap=MAX_CUES_PER_ROLE,
        findings=findings,
    )
    evidence_categories = _cap(
        _resolved_evidence_categories(
            raw.get("evidence_categories"), role_id=name, resolver=resolver, findings=findings
        ),
        field_name=f"{name}.evidence_categories",
        cap=MAX_EVIDENCE_CATEGORIES_PER_ROLE,
        findings=findings,
    )
    return (
        ContextRole(
            id=name,
            description=str(raw.get("description") or "").strip(),
            lane=str(lane),
            categories=frozenset(_strings(raw.get("categories"))),
            anchor_defaults=frozenset(kind for kind in defaults if kind in ANCHOR_KINDS),
            cues=cues,
            priority=priority,
            shipped=False,
            evidence_cues=evidence_cues,
            evidence_categories=frozenset(evidence_categories),
        ),
        findings,
    )


def _narrowed(
    role: ContextRole, raw: Mapping[str, Any], findings: list[dict[str, str]], resolver: Any
) -> ContextRole:
    categories = set(role.categories)
    cues = list(role.cues)
    defaults = set(role.anchor_defaults)
    evidence_cues = list(role.evidence_cues)
    evidence_categories = set(role.evidence_categories)

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
    cues = list(_cap(cues, field_name=f"{role.id}.cues", cap=MAX_CUES_PER_ROLE, findings=findings))
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

    # `evidence_cues`/`evidence_categories` extend by union exactly as
    # `categories` already does above (design.md decision 1): naming the
    # field directly ADDS to the shipped role's list rather than replacing
    # it, so there is no separate `add_evidence_cues`/`add_evidence_categories`.
    if "evidence_cues" in raw:
        replacement = _validated_evidence_cues(raw.get("evidence_cues"), role_id=role.id, findings=findings)
        if replacement or isinstance(raw.get("evidence_cues"), (list, tuple)):
            evidence_cues = list(dict.fromkeys([*role.evidence_cues, *replacement]))
        else:
            findings.append(
                _finding("invalid_evidence_cues", f"{role.id}.evidence_cues", "must be a string list")
            )
    evidence_cues = list(
        _cap(evidence_cues, field_name=f"{role.id}.evidence_cues", cap=MAX_CUES_PER_ROLE, findings=findings)
    )
    if "evidence_categories" in raw:
        replacement = _resolved_evidence_categories(
            raw.get("evidence_categories"), role_id=role.id, resolver=resolver, findings=findings
        )
        if replacement or isinstance(raw.get("evidence_categories"), (list, tuple)):
            evidence_categories = set(replacement) | set(role.evidence_categories)
        else:
            findings.append(
                _finding(
                    "invalid_evidence_categories",
                    f"{role.id}.evidence_categories",
                    "must be a string list",
                )
            )
    evidence_categories_capped = _cap(
        sorted(evidence_categories),
        field_name=f"{role.id}.evidence_categories",
        cap=MAX_EVIDENCE_CATEGORIES_PER_ROLE,
        findings=findings,
    )

    return replace(
        role,
        lane=lane,
        categories=frozenset(categories),
        anchor_defaults=frozenset(defaults),
        cues=tuple(cues),
        evidence_cues=tuple(evidence_cues),
        evidence_categories=frozenset(evidence_categories_capped),
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
        else:
            # Role selection matches EVERY cue, `cues` and `evidence_cues`
            # alike (spec: "Role selection SHALL keep its existing matching
            # over every cue") -- an `evidence_cues` entry that is not among
            # the role's `cues` still selects the role, so an owner who wants
            # a cue to select AND count as evidence writes it once, in
            # `evidence_cues` alone (design.md decision 1).
            all_cues = (*role.cues, *role.evidence_cues)
            if all_cues and any(cue in text for cue in all_cues):
                source = "turn_cue"
            else:
                continue
        selected.append({"id": role.id, "source": source, "lane": role.lane})
    return tuple(selected[: max(0, int(limit))])
