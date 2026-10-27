"""Versioned core relations and optional vault-owned relation refinements.

The core relations ship in `vocabulary/packs/core/relations.yaml`. The vault
overlay `_Schema/relation-registry.yaml` keeps its grammar and is read and
cached through the generic vocabulary loader (`SPEC`); this module is the
registry's adapter and keeps its public API and meaning rules.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Container, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from functools import cached_property, lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from .kbdir import kb_dirname
from .vocabulary import registry as vocabulary_registry

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
#: Explicit spelling of a core relation: `core.<key>` names the core meaning of a
#: bare label that a vault alias also uses.
CORE_PREFIX = "core."
#: Fields a vault extension may set. `generic` is deliberately absent: only the
#: shipped core pack marks the generic relation, so an extension cannot claim it.
_DEFINITION_FIELDS = {
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
    "presentation_role",
    "label",
}
#: Fields a shipped core relation may carry. This closed set is the pack's own schema
#: (field names, not vocabulary); a pack with another field is a build defect.
_CORE_FIELDS = frozenset(
    {"description", "family", "direction", "inverse", "origins", "presentation_role", "generic"}
)


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
    # Optional renderer metadata never changes the relation's meaning or display label.
    presentation_role: str | None = None
    # A declared display label; absent, `display_label` reads the key (core) or its name.
    label: str | None = None
    # Marks the core pack's one relation for "related, with no specific meaning". An
    # extension never carries it, so a specific refinement is never taken for it.
    generic: bool = False

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
            ("presentation_role", self.presentation_role),
            ("label", self.label),
        ):
            if scalar_value:
                out[key] = scalar_value
        if self.generic:
            out["generic"] = True
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
    terminal_replacement: str | None = None
    findings: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class RelationRegistry:
    core_version: int
    extension_hash: str
    core: dict[str, RelationDefinition]
    extensions: dict[str, RelationDefinition] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    findings: tuple[dict[str, str], ...] = ()
    terminal_replacements: dict[str, str] = field(default_factory=dict)
    predecessor_closure: dict[str, frozenset[str]] = field(default_factory=dict)

    @property
    def keys(self) -> frozenset[str]:
        return frozenset((*self.core, *self.extensions))

    @property
    def families(self) -> frozenset[str]:
        return frozenset(item.family for item in (*self.core.values(), *self.extensions.values()))

    @cached_property
    def generic_key(self) -> str:
        """The key of the one generic relation; the core pack guarantees exactly one."""
        return next(key for key, definition in self.core.items() if definition.generic)

    @cached_property
    def active_keys_by_label(self) -> dict[str, tuple[str, ...]]:
        """Each display label with the active entries that carry it."""
        by_label: dict[str, list[str]] = {}
        for definition in (*self.core.values(), *self.extensions.values()):
            if definition.status == "active":
                by_label.setdefault(_own_label(definition), []).append(definition.key)
        return {label: tuple(keys) for label, keys in by_label.items()}

    def definition(self, key: str) -> RelationDefinition | None:
        return self.core.get(key) or self.extensions.get(key)

    def terminal_replacement(self, key: str) -> str | None:
        return self.terminal_replacements.get(key)

    def predecessors(self, key: str) -> frozenset[str]:
        return self.predecessor_closure.get(key, frozenset())

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
        explicit_core = False
        if definition is None and label.startswith(CORE_PREFIX):
            # After every saved key and alias, so no saved label changes meaning.
            canonical = label.removeprefix(CORE_PREFIX)
            definition = self.core.get(canonical)
            explicit_core = definition is not None
        if definition is None:
            return RelationResolution(
                raw=raw, canonical=None, parent=None, status="unregistered", definition=None
            )
        status = (
            "alias"
            if canonical != label and not explicit_core
            else ("core" if definition.core else "extension")
        )
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
            terminal_replacement=self.terminal_replacement(canonical),
            findings=tuple(findings),
        )


def _own_label(definition: RelationDefinition) -> str:
    """A relation's label before any qualifier: declared, else the key (core) or its name."""
    if definition.label:
        return definition.label
    # An extension key is `<namespace>.<name>` (`_KEY_RE`); the name is its default label.
    return definition.key if definition.core else definition.key.partition(".")[2]


def display_label(registry: RelationRegistry, key: str) -> str:
    """How a relation reads to a person; the key stays the machine identity.

    A core relation reads as its label and an extension as `<label> (a kind of
    <parent label>)`. An extension whose label another active entry also carries
    reads with its namespace (`<namespace>.<label>`), and a deprecated entry ends
    in `(deprecated)`. A key the registry does not hold reads as itself.
    """
    definition = registry.definition(key)
    if definition is None:
        return key
    label = _own_label(definition)
    if not definition.core and any(
        other != key for other in registry.active_keys_by_label.get(label, ())
    ):
        label = f"{definition.key.partition('.')[0]}.{label}"
    if definition.parent:
        label = f"{label} (a kind of {display_label(registry, definition.parent)})"
    if definition.status == "deprecated":
        label = f"{label} (deprecated)"
    return label


def traversable_resolution_status(status: str | None) -> bool:
    """Whether a selected relation resolution supplies an authored graph fact."""
    # Deprecated facts remain traversable; unregistered and out-of-scope ones do not.
    # nosemgrep: ep-word-membership -- the closed outcomes that `resolve()` above emits.
    return status in {"core", "alias", "extension", "deprecated"}


def normalize_relation(label: str) -> str:
    return re.sub(r"[\s-]+", "_", str(label or "").strip().lower().rstrip(":"))


def extension_registry_path(vault_root: Path) -> Path:
    return Path(vault_root) / kb_dirname() / "_Schema" / "relation-registry.yaml"


@lru_cache(maxsize=1)
def core_registry() -> RelationRegistry:
    """The shipped core relations. A malformed pack is a build defect, so this raises."""
    data = yaml.safe_load(vocabulary_registry.pack_text("relations.yaml"))
    version = int(data["schema_version"])
    definitions: dict[str, RelationDefinition] = {}
    for key, value in data["relations"].items():
        unknown = sorted(set(value) - _CORE_FIELDS)
        if unknown or not isinstance(value.get("generic", False), bool):
            raise RuntimeError(
                f"shipped relations pack: {key!r} has unknown fields {unknown} "
                "or a generic that is not a boolean"
            )
        definitions[key] = RelationDefinition(
            key=key,
            description=str(value["description"]),
            family=str(value["family"]),
            direction=str(value.get("direction", "directed")),
            inverse=value.get("inverse"),
            origins=frozenset(value.get("origins") or ["semantic_relation"]),
            presentation_role=value.get("presentation_role"),
            generic=value.get("generic", False),
            core=True,
        )
    generic = sorted(key for key, definition in definitions.items() if definition.generic)
    if len(generic) != 1:
        raise RuntimeError(
            f"shipped relations pack must mark exactly one generic relation, found {generic}"
        )
    return RelationRegistry(version, "none", definitions)


def generic_relation_key() -> str:
    """The core pack's generic relation, for code that must not name it."""
    return core_registry().generic_key


class _EveryKey:
    """Container that holds every key: the saved registry is grandfathered whole."""

    def __contains__(self, _key: object) -> bool:
        return True


def _blocking(registry: RelationRegistry) -> list[dict[str, str]]:
    """Findings that refuse a write; warnings are advisories and never do."""
    return [item for item in registry.findings if item["severity"] == "error"]


def load_registry(
    vault_root: Path | None = None, *, proposal: dict[str, Any] | None = None,
    registry_scope: str | None = None,
) -> RelationRegistry:
    """Load the saved registry, or parse a proposal.

    With both a vault and a proposal, an extension already saved with the same
    parent and declared family keeps any `family_mismatch` as a warning, and an
    alias already saved keeps binding to its extension when it shadows a core key:
    a rule or core key added later never turns a saved registry into a refusal.
    """
    core = core_registry()
    if proposal is not None:
        raw = yaml.safe_dump(proposal, sort_keys=True)
        grandfathered = (
            _unchanged_saved_extensions(vault_root, proposal) if vault_root is not None else ()
        )
        saved_aliases = _alias_pairs(load_registry(vault_root)) if vault_root is not None else ()
        return _parse_extension_data(
            proposal,
            _content_hash(raw),
            core,
            grandfathered=grandfathered,
            saved_aliases=saved_aliases,
        )
    if vault_root is None:
        return core
    from .vocabulary import instances

    selected = instances.select(Path(vault_root), SPEC, registry_scope)
    return vocabulary_registry.load(selected, Path(vault_root)).typed


def load_admitted(
    vault_root: Path, registry_scope: str | None = None
) -> RelationRegistry | None:
    """The registry of one instance, or None when this caller cannot admit it.

    None is one answer for an instance that does not exist, a binding that is
    ambiguous and an overlay the caller may not read, so a refusal never says
    which of them it is.
    """
    from .vocabulary import contract, instances

    root = Path(vault_root)
    try:
        selected = instances.select(root, SPEC, registry_scope)
        if contract.admission_refusal(root, selected) is not None:
            return None
        return vocabulary_registry.load(selected, root).typed
    except vocabulary_registry.RegistryError:
        return None


def load_for_page(
    vault_root: Path, rel_path: str, frontmatter: Mapping[str, Any]
) -> RelationRegistry:
    """The registry the page's instance defines; core meanings when this caller cannot admit it."""
    from .vocabulary import instances

    try:
        scope = instances.page_scope(Path(vault_root), rel_path, dict(frontmatter))
    except vocabulary_registry.RegistryError:
        return core_registry()
    return load_admitted(vault_root, scope) or core_registry()


def authored_label(registry: RelationRegistry, requested: str, canonical: str) -> str:
    """The spelling a page carries for `canonical`, chosen as `requested`.

    The bare label stays when this registry resolves it to that one entry; the
    namespaced key (or the explicit `core.` spelling of a shadowed core key)
    stands in when it does not.
    """
    label = normalize_relation(requested)
    for spelling in (label, canonical, f"{CORE_PREFIX}{canonical}"):
        if registry.resolve(spelling).canonical == canonical:
            return spelling
    return canonical


def clear_cache() -> None:
    """Drop every vault's cached relation-registry snapshot."""
    vocabulary_registry.invalidate_registry(SPEC.name)


def _parse_overlay_text(raw: str | None, digest: str) -> RelationRegistry:
    core = core_registry()
    if raw is None:
        return core
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return RelationRegistry(
            core.core_version,
            digest,
            core.core,
            findings=(_finding("invalid_yaml", "registry", str(exc)),),
        )
    return _parse_extension_data(data, digest, core, saved=True)


def _alias_pairs(registry: RelationRegistry) -> frozenset[tuple[str, str]]:
    """Each (extension key, alias) binding a registry holds."""
    return frozenset(
        (key, alias)
        for key, definition in registry.extensions.items()
        for alias in definition.aliases
    )


def _unchanged_saved_extensions(vault_root: Path, proposal: dict[str, Any]) -> frozenset[str]:
    """Saved extensions the proposal keeps with the same parent and declared family."""
    proposed = proposal.get("extensions") if isinstance(proposal, dict) else None
    if not isinstance(proposed, dict):
        return frozenset()
    return frozenset(
        key
        for key, saved in load_registry(vault_root).extensions.items()
        if isinstance(proposed.get(key), dict)
        and proposed[key].get("parent") == saved.parent
        and proposed[key].get("family") == saved.family
    )


def validate_proposal(
    proposal: dict[str, Any], vault_root: Path | None = None
) -> list[dict[str, str]]:
    return _blocking(load_registry(vault_root, proposal=proposal))


def require_current_hash(current_hash: str, expected_hash: str) -> None:
    """Raise the stable optimistic-concurrency error for an outdated proposal."""
    if current_hash != expected_hash:
        raise ValueError("STALE_RELATION_REGISTRY: expected_hash does not match current hash")


def propose_extension(
    registry: RelationRegistry,
    *,
    requested_label: str,
    parent: str | None = None,
    description: str | None = None,
    direction: str | None = None,
    namespace: str = "vault",
    aliases: Iterable[str] = (),
    inverse: str | None = None,
    origins: Iterable[str] | None = None,
    source_kinds: Iterable[str] | None = None,
    target_kinds: Iterable[str] | None = None,
    projects: Iterable[str] | None = None,
    page_types: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build a read-only, reviewed extension delta without inferring meaning."""
    label = normalize_relation(requested_label)
    normalized_namespace = normalize_relation(namespace)
    key = extension_key_for_label(label, normalized_namespace)
    findings: list[dict[str, str]] = []
    missing = [
        field_name
        for field_name, value in (
            ("parent", parent),
            ("description", description),
            ("direction", direction),
        )
        if value is None or (isinstance(value, str) and not value.strip())
    ]
    if missing:
        findings.append(
            _finding(
                "incomplete_proposal",
                "proposal",
                f"required semantic fields are missing: {', '.join(missing)}",
            )
        )
    clean_aliases = list(
        dict.fromkeys([label, *(normalize_relation(alias) for alias in aliases)])
    )
    if key in registry.keys or key in registry.aliases:
        findings.append(
            _finding(
                "collision",
                "proposal.requested_label",
                f"canonical key {key!r} collides",
            )
        )
    value: dict[str, Any] = {
        "parent": parent,
        "description": description,
        "direction": direction,
        "aliases": clean_aliases,
    }
    for field_name, supplied in (
        ("inverse", inverse),
        ("origins", origins),
        ("source_kinds", source_kinds),
        ("target_kinds", target_kinds),
    ):
        if supplied is not None:
            value[field_name] = supplied if field_name == "inverse" else list(supplied)
    scope: dict[str, list[str]] = {}
    if projects is not None:
        scope["projects"] = list(projects)
    if page_types is not None:
        scope["page_types"] = list(page_types)
    if scope:
        value["scope"] = scope

    candidate = {
        "schema_version": EXTENSION_SCHEMA_VERSION,
        "extensions": {
            **{
                existing_key: _definition_document(existing)
                for existing_key, existing in registry.extensions.items()
            },
            key: value,
        },
    }
    if not missing:
        findings.extend(
            finding
            for finding in load_registry(proposal=candidate).findings
            if finding.get("relation") == key
        )
    findings = list(
        {
            tuple(sorted(finding.items())): finding
            for finding in findings
        }.values()
    )
    return {
        "expected_hash": registry.extension_hash,
        "delta": {"upsert": {key: value}},
        "findings": findings,
    }


def _definition_document(definition: RelationDefinition) -> dict[str, Any]:
    value: dict[str, Any] = {
        "parent": definition.parent,
        "description": definition.description,
        "family": definition.family,
        "direction": definition.direction,
        "origins": sorted(definition.origins),
        "aliases": list(definition.aliases),
        "source_kinds": sorted(definition.source_kinds),
        "target_kinds": sorted(definition.target_kinds),
        "status": definition.status,
    }
    if definition.inverse is not None:
        value["inverse"] = definition.inverse
    if definition.replaced_by is not None:
        value["replaced_by"] = definition.replaced_by
    if definition.presentation_role is not None:
        value["presentation_role"] = definition.presentation_role
    if definition.label is not None:
        value["label"] = definition.label
    scope: dict[str, list[str]] = {}
    if definition.projects:
        scope["projects"] = sorted(definition.projects)
    if definition.page_types:
        scope["page_types"] = sorted(definition.page_types)
    if scope:
        value["scope"] = scope
    return value


def merge_extension_delta(current: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    """Merge a registry delta purely, preserving existing relation meaning."""
    if not isinstance(delta, dict) or set(delta) - {"upsert", "deprecate"}:
        raise ValueError("INVALID_RELATION_DELTA: only upsert and deprecate are allowed")
    result = deepcopy(current)
    extensions = result.setdefault("extensions", {})
    if result.get("schema_version") != EXTENSION_SCHEMA_VERSION or not isinstance(extensions, dict):
        raise ValueError("INVALID_RELATION_REGISTRY: current registry is invalid")
    upserts = delta.get("upsert", {})
    if not isinstance(upserts, dict):
        raise ValueError("INVALID_RELATION_DELTA: upsert must be an object")
    for key, patch in upserts.items():
        if not isinstance(patch, dict):
            raise ValueError("INVALID_RELATION_DELTA: extension patch must be an object")
        if set(patch) - _DEFINITION_FIELDS:
            raise ValueError("INVALID_RELATION_DELTA: extension patch has unknown fields")
        old = extensions.get(key)
        if old is None:
            extensions[key] = deepcopy(patch)
            continue
        if not isinstance(old, dict):
            raise ValueError("INVALID_RELATION_REGISTRY: existing extension is invalid")
        _merge_existing_extension(old, patch)
    deprecations = delta.get("deprecate", {})
    if not isinstance(deprecations, dict):
        raise ValueError("INVALID_RELATION_DELTA: deprecate must be an object")
    for key, replacement in deprecations.items():
        old = extensions.get(key)
        if not isinstance(old, dict):
            raise ValueError(f"UNKNOWN_RELATION: {key}")
        _merge_existing_extension(old, {"status": "deprecated", "replaced_by": replacement})
    before = _parse_extension_data(current, "", core_registry(), saved=True)
    # An extension already in `current` keeps its immutable family, so a
    # mismatch it carries is grandfathered; one added by this delta is not.
    registry = _parse_extension_data(
        result,
        _content_hash(yaml.safe_dump(result, sort_keys=True)),
        core_registry(),
        grandfathered=frozenset(map(str, current.get("extensions") or {})),
        # An alias this delta adds to an older extension is not grandfathered.
        saved_aliases=_alias_pairs(before),
    )
    # A finding `current` already carries, such as a field a later release
    # wrote, refuses only the save that introduces it.
    blocking = vocabulary_registry.new_findings(before.findings, registry.findings)
    if blocking:
        raise ValueError(f"INVALID_RELATION_REGISTRY: {blocking!r}")
    return result


def _merge_existing_extension(old: dict[str, Any], patch: dict[str, Any]) -> None:
    for display_field in ("presentation_role", "label"):
        if display_field in patch:
            old[display_field] = patch[display_field]
    immutable = {
        "parent",
        "family",
        "description",
        "direction",
        "inverse",
        "origins",
        "source_kinds",
        "target_kinds",
        "scope",
    }
    for field_name in immutable:
        if field_name in patch and patch[field_name] != old.get(field_name):
            raise ValueError("IMMUTABLE_RELATION_MEANING: create a new canonical key instead")
    if "aliases" in patch:
        old_aliases = list(old.get("aliases") or [])
        if not isinstance(patch["aliases"], list) or any(
            not isinstance(item, str) for item in patch["aliases"]
        ):
            raise ValueError("INVALID_RELATION_DELTA: aliases must be a list of strings")
        requested = [normalize_relation(item) for item in patch["aliases"]]
        if (not requested and old_aliases) or (
            set(requested) & set(old_aliases) and not set(old_aliases) <= set(requested)
        ):
            raise ValueError("IMMUTABLE_RELATION_ALIASES: aliases may only be added")
        old["aliases"] = list(dict.fromkeys([*old_aliases, *requested]))
    if old.get("status") == "deprecated":
        if patch.get("status", "deprecated") != "deprecated":
            raise ValueError("IMMUTABLE_REPLACEMENT: deprecated status is immutable")
        if "replaced_by" in patch and patch["replaced_by"] != old.get("replaced_by"):
            raise ValueError("IMMUTABLE_REPLACEMENT: immediate replacement is immutable")
    else:
        if "status" in patch:
            old["status"] = patch["status"]
        if "replaced_by" in patch:
            old["replaced_by"] = patch["replaced_by"]


def _semantic_continuity_findings(
    before: RelationRegistry, after: RelationRegistry, removed: Container[str] = ()
) -> list[dict[str, str]]:
    """What a save changed that a registered definition forbids.

    `removed` names keys a `remove` delta took out after counting that nothing
    uses them: undoing a promotion is the one hard delete the definition allows.
    """
    findings: list[dict[str, str]] = []
    fields = (
        "parent",
        "family",
        "description",
        "direction",
        "inverse",
        "origins",
        "source_kinds",
        "target_kinds",
        "projects",
        "page_types",
    )
    for key, old in before.extensions.items():
        new = after.extensions.get(key)
        if new is None and key in removed:
            continue
        if new is None:
            findings.append(
                _finding(
                    "immutable_definition",
                    f"extensions.{key}",
                    "registered definitions cannot be hard-deleted; deprecate instead",
                    relation=key,
                )
            )
            continue
        if any(getattr(old, field_name) != getattr(new, field_name) for field_name in fields):
            findings.append(
                _finding(
                    "immutable_meaning",
                    f"extensions.{key}",
                    "meaning fields are immutable",
                    relation=key,
                )
            )
        if not set(old.aliases) <= set(new.aliases):
            findings.append(
                _finding(
                    "immutable_aliases",
                    f"extensions.{key}.aliases",
                    "aliases may only be added",
                    relation=key,
                )
            )
        if old.status == "deprecated" and (
            new.status != "deprecated" or new.replaced_by != old.replaced_by
        ):
            findings.append(
                _finding(
                    "immutable_replacement",
                    f"extensions.{key}.replaced_by",
                    "deprecated status and immediate replacement are immutable",
                    relation=key,
                )
            )
    return findings


def extension_key_for_label(label: str, namespace: str = "vault") -> str:
    """Return a valid two-segment canonical key for a clean authoring label."""
    return f"{namespace}.{_extension_label(normalize_relation(label))}"


def _extension_label(label: str) -> str:
    return label.replace(".", "_")


def save_registry(
    vault_root: Path,
    proposal: dict[str, Any],
    *,
    expected_hash: str | None = None,
    observed_keys: Iterable[str] = (),
    why: str | None = None,
    operation: str = "save-relations",
) -> dict[str, Any]:
    """Replace the overlay with one reviewed document, keeping history."""
    from .governance import connector_boundary, principal
    from .vocabulary import instances

    path = instances.selected_overlay(vault_root, SPEC, authoring=True)
    # Saved continuity is safe only when the whole extension registry is admitted.
    # Otherwise a collision or stale guard would expose a private definition.
    if not connector_boundary.permits(vault_root, path, principal.effective_principal()):
        raise ValueError("GOVERNANCE_OPERATION_UNAVAILABLE: registry is unavailable")
    registry = load_registry(vault_root, proposal=proposal)
    inspected = vocabulary_registry.load(SPEC, Path(vault_root))
    blocking = vocabulary_registry.new_findings(inspected.findings, registry.findings)
    if blocking:
        raise ValueError(f"INVALID_RELATION_REGISTRY: {blocking!r}")
    proposed_keys = set(registry.extensions) | set(registry.aliases)
    removed = sorted(set(observed_keys) - proposed_keys - set(registry.core))
    if removed:
        raise ValueError(f"OBSERVED_RELATION_DELETION: deprecate observed keys instead: {removed}")
    current_hash: str | None = None
    if inspected.overlay_text is not None:
        current_hash = inspected.content_hash
        if expected_hash is None:
            raise ValueError("REGISTRY_EXISTS: provide current expected_hash")
        if expected_hash != current_hash:
            raise ValueError("STALE_RELATION_REGISTRY: expected_hash does not match current hash")
        current_registry = load_registry(vault_root)
        continuity = _semantic_continuity_findings(current_registry, registry)
        if continuity:
            raise ValueError(f"IMMUTABLE_RELATION_MEANING: {continuity!r}")
    rendered = yaml.safe_dump(proposal, sort_keys=False, allow_unicode=True)
    history = vocabulary_registry.commit(
        SPEC,
        Path(vault_root),
        rendered,
        operation=operation,
        why=why,
        before_hash=current_hash or vocabulary_registry.NO_OVERLAY_HASH,
        previous=inspected.overlay_text,
        added=vocabulary_registry.added_keys(
            inspected.entries,
            SPEC.adapter.entries(registry),
        ),
    )
    return {
        "path": path,
        "content_hash": _content_hash(rendered),
        "previous_hash": current_hash,
        "created": current_hash is None,
        "history": history,
    }


def saved_document(vault_root: Path) -> dict[str, Any]:
    """The overlay as saved, each entry with the fields it was written with."""
    return SPEC.adapter.document(vocabulary_registry.load(SPEC, Path(vault_root)).overlay_text)


def empty_proposal() -> dict[str, Any]:
    return {"schema_version": EXTENSION_SCHEMA_VERSION, "extensions": {}}


def _parse_extension_data(
    data: Any,
    digest: str,
    core: RelationRegistry,
    grandfathered: Container[str] = (),
    saved_aliases: Container[tuple[str, str]] = (),
    *,
    saved: bool = False,
) -> RelationRegistry:
    """Parse an overlay document into a registry and its findings.

    `saved` marks the document as the vault's saved overlay: it is grandfathered
    whole, and a field this release does not know is a warning, because a later
    release may have written it. Otherwise `grandfathered` names the extension
    keys and `saved_aliases` the (extension, alias) pairs that a saved overlay
    already holds, so a rule or core key added later never refuses them.
    """
    if saved:
        grandfathered = saved_aliases = _EveryKey()
    unknown_severity = "warning" if saved else "error"
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
        findings.append(
            _finding("unknown_field", key, "unknown registry field", severity=unknown_severity)
        )
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
    core_spellings = {f"{CORE_PREFIX}{key}" for key in core.core}
    occupied = set(core.core) | core_spellings
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
                    # A saved key that already spells a core relation keeps its meaning.
                    severity=(
                        "warning" if key in core_spellings and key in grandfathered else "error"
                    ),
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
        for unknown in sorted(set(value) - _DEFINITION_FIELDS):
            findings.append(
                _finding(
                    "unknown_field",
                    f"{span}.{unknown}",
                    "unknown definition field",
                    relation=key,
                    severity=unknown_severity,
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
        parent_family = core.core[parent].family if parent in core.core else ""
        declared_family = value.get("family")
        if parent_family and declared_family and str(declared_family) != parent_family:
            findings.append(
                _finding(
                    "family_mismatch",
                    f"{span}.family",
                    f"must equal the parent's core family {parent_family!r}, so a declared "
                    "family cannot relabel the parent's meaning",
                    relation=key,
                    severity="warning" if key in grandfathered else "error",
                )
            )
        presentation_role = value.get("presentation_role")
        # Renderer roles are identifier-shaped metadata; unknown roles remain valid.
        if presentation_role is not None and (
            not isinstance(presentation_role, str) or not _KIND_RE.fullmatch(presentation_role)
        ):
            findings.append(_finding(
                "invalid_presentation_role", f"{span}.presentation_role",
                "must be an identifier or null", relation=key,
            ))
            presentation_role = None
        label = value.get("label")
        if label is not None:
            label = normalize_relation(label) if isinstance(label, str) else ""
            if not _LABEL_RE.fullmatch(label):
                findings.append(
                    _finding(
                        "invalid_label",
                        f"{span}.label",
                        "must be a relation label, lowercase with underscores",
                        relation=key,
                    )
                )
                label = None
        definition = RelationDefinition(
            key=key,
            description=description,
            family=str(declared_family or parent_family),
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
            presentation_role=presentation_role,
            label=label,
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
                # Recording the finding is not the refusal. Without this the
                # loop fell through to the collision check, whose `else` branch
                # registered the alias anyway — so a label the grammar rejects
                # resolved with `alias` standing, exactly as though it had
                # passed the validation the registry claims to apply.
                continue
            if alias in aliases:
                findings.append(
                    _finding("collision", span, f"alias {alias!r} collides", relation=key)
                )
            elif alias in core.core or alias in core_spellings:
                # The same code, path and detail serve the warning and the refusal, so a
                # save that keeps a saved shadow is not a new finding to the generic gate.
                kept = (key, alias) in saved_aliases
                findings.append(
                    _finding(
                        "core_shadowed",
                        span,
                        f"alias {alias!r} shadows a core relation; every core relation "
                        f"stays reachable as {CORE_PREFIX}<key>",
                        relation=key,
                        severity="warning" if kept else "error",
                    )
                )
                if kept:
                    aliases[alias] = key
            elif alias in occupied:
                findings.append(
                    _finding("collision", span, f"alias {alias!r} collides", relation=key)
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
    for key, definition in extensions.items():
        # A label a caller is shown must be one it can pass back: `aliases` holds the
        # bindings that survived the collision checks above.
        if definition.label and aliases.get(definition.label) != key:
            findings.append(
                _finding(
                    "unresolved_label",
                    f"extensions.{key}.label",
                    f"label {definition.label!r} must also be one of the relation's aliases",
                    relation=key,
                    severity="warning" if key in grandfathered else "error",
                )
            )
    findings.extend(_cycle_findings(extensions, "replaced_by", allow_pair=False))
    findings.extend(_cycle_findings(extensions, "inverse", allow_pair=True))
    terminals, predecessors, replacement_findings = _replacement_closure(
        core.core, extensions
    )
    findings.extend(replacement_findings)
    return RelationRegistry(
        core.core_version,
        digest,
        core.core,
        extensions,
        aliases,
        tuple(sorted(findings, key=lambda x: (x["path"], x["code"], x["detail"]))),
        terminals,
        predecessors,
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
    severity: str = "error",
) -> dict[str, str]:
    finding = {
        "code": code,
        "path": path,
        "span": path,
        "severity": severity,
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


def _replacement_closure(
    core: dict[str, RelationDefinition], extensions: dict[str, RelationDefinition]
) -> tuple[dict[str, str], dict[str, frozenset[str]], list[dict[str, str]]]:
    """Validate terminal survivors and materialize directed predecessor closure."""
    terminals: dict[str, str] = {}
    predecessors: dict[str, set[str]] = {}
    findings: list[dict[str, str]] = []
    for key, definition in extensions.items():
        if not definition.replaced_by:
            continue
        seen = {key}
        chain = [key]
        current = definition.replaced_by
        terminal_key: str | None = None
        while current in extensions:
            if current in seen:
                break
            seen.add(current)
            chain.append(current)
            replacement = extensions[current].replaced_by
            if extensions[current].status == "active" and not replacement:
                terminal_key = current
                break
            if not replacement:
                break
            current = replacement
        if current in core:
            terminal_key = current
        # Every key whose chain stops short, dangles or cycles carries its own
        # finding, so a save that newly points a key at a broken chain is refused.
        terminal = (
            None if terminal_key is None else core.get(terminal_key) or extensions.get(terminal_key)
        )
        if terminal_key is None or terminal is None or terminal.status != "active":
            findings.append(
                _finding(
                    "invalid_replacement",
                    f"extensions.{key}.replaced_by",
                    "replacement chain must end in an active survivor",
                    relation=key,
                )
            )
            continue
        terminals[key] = terminal_key
        predecessors.setdefault(terminal_key, set()).update(
            item for item in chain if item != terminal_key
        )
    return terminals, {key: frozenset(value) for key, value in predecessors.items()}, findings


# --------------------------------------------------------------------------- #
# The registry adapter (`add-vocabulary-registries`)
# --------------------------------------------------------------------------- #

#: Generic attributes a relation entry carries, in overlay field order.
_MEANING_ATTRIBUTES = ("family", "direction", "inverse", "origins", "source_kinds", "target_kinds")
_ATTRIBUTE_FIELDS = (*_MEANING_ATTRIBUTES, "presentation_role")


class _Adapter:
    """`_Schema/relation-registry.yaml` in its own grammar, as generic entries."""

    def parse(self, text: str | None, digest: str) -> RelationRegistry:
        return _parse_overlay_text(text, digest)

    def parse_document(self, document: Mapping[str, Any]) -> RelationRegistry:
        rendered = self.render(document)
        return _parse_extension_data(
            dict(document), _content_hash(rendered), core_registry(), grandfathered=()
        )

    def entries(self, typed: RelationRegistry) -> dict[str, vocabulary_registry.Entry]:
        out: dict[str, vocabulary_registry.Entry] = {}
        for definition in (*typed.core.values(), *typed.extensions.values()):
            attributes: dict[str, Any] = {
                "family": definition.family,
                "direction": definition.direction,
                "origins": sorted(definition.origins),
            }
            if definition.inverse:
                attributes["inverse"] = definition.inverse
            if definition.presentation_role is not None:
                attributes["presentation_role"] = definition.presentation_role
            # Read-only: `SPEC.attributes` omits it, so no delta can set it on a vault entry.
            if definition.generic:
                attributes["generic"] = True
            for name in ("source_kinds", "target_kinds"):
                if getattr(definition, name):
                    attributes[name] = sorted(getattr(definition, name))
            scope = {
                name: sorted(getattr(definition, name))
                for name in ("projects", "page_types")
                if getattr(definition, name)
            }
            if scope:
                attributes["scope"] = scope
            out[definition.key] = vocabulary_registry.Entry(
                key=definition.key,
                label=definition.label or "",
                description=definition.description,
                aliases=definition.aliases,
                status=definition.status,
                replaced_by=definition.replaced_by,
                parent=definition.parent,
                attributes=MappingProxyType(attributes),
                origin="pack" if definition.core else "vault",
            )
        return out

    def findings(self, typed: RelationRegistry) -> tuple[dict[str, str], ...]:
        return typed.findings

    def document(self, text: str | None) -> dict[str, Any]:
        if text is None:
            return empty_proposal()
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise vocabulary_registry.RegistryError(
                "INVALID_REGISTRY_OVERLAY: relation-registry.yaml does not parse; fix it or "
                f"restore a kept version ({exc})"
            ) from exc
        if not isinstance(data, dict) or not isinstance(data.get("extensions") or {}, dict):
            raise vocabulary_registry.RegistryError(
                "INVALID_REGISTRY_OVERLAY: relation-registry.yaml is not a registry document; "
                "fix it or restore a kept version"
            )
        data.setdefault("schema_version", EXTENSION_SCHEMA_VERSION)
        data["extensions"] = dict(data.get("extensions") or {})
        return data

    def put(
        self,
        document: dict[str, Any],
        key: str,
        entry: vocabulary_registry.Entry,
        *,
        existing: bool,
    ) -> None:
        if key in core_registry().core:
            raise vocabulary_registry.RegistryError(
                f"PACK_ENTRY_FIXED: {key!r} is a portable core relation; extend it with a "
                "namespaced key whose parent it is"
            )
        extensions = document["extensions"]
        raw = extensions.get(key)
        if existing and isinstance(raw, dict):
            # Meaning fields were checked unchanged; only the mutable ones move,
            # so the rest of the row keeps its bytes.
            raw["aliases"] = list(
                dict.fromkeys(normalize_relation(alias) for alias in entry.aliases)
            )
            raw["status"] = entry.status
            if entry.replaced_by:
                raw["replaced_by"] = entry.replaced_by
            if "presentation_role" in entry.attributes:
                raw["presentation_role"] = entry.attributes["presentation_role"]
            if entry.label:
                raw["label"] = entry.label
            return
        row: dict[str, Any] = {"parent": entry.parent, "description": entry.description}
        if entry.label:
            row["label"] = entry.label
        attributes = entry.attributes
        for name in _ATTRIBUTE_FIELDS:
            if name in attributes and attributes[name] not in (None, "", []):
                value = attributes[name]
                row[name] = list(value) if isinstance(value, (list, tuple)) else value
        if "scope" in attributes:
            row["scope"] = dict(attributes["scope"])
        row["aliases"] = list(dict.fromkeys(normalize_relation(alias) for alias in entry.aliases))
        row["status"] = entry.status
        if entry.replaced_by:
            row["replaced_by"] = entry.replaced_by
        extensions[key] = row

    def remove(self, document: dict[str, Any], key: str) -> None:
        document["extensions"].pop(key, None)

    def render(self, document: Mapping[str, Any]) -> str:
        return yaml.safe_dump(dict(document), sort_keys=False, allow_unicode=True)

    def normalize_key(self, raw: str) -> str:
        key = normalize_relation(raw)
        if not _KEY_RE.fullmatch(key):
            raise vocabulary_registry.RegistryError(
                f"INVALID_REGISTRY_KEY: {raw!r} is not a namespaced relation key; use "
                f"{extension_key_for_label(key)!r}"
            )
        return key

    def continuity(
        self,
        before: RelationRegistry,
        after: RelationRegistry,
        removed: Container[str] = (),
    ) -> list[dict[str, str]]:
        return _semantic_continuity_findings(before, after, removed)


def _usage(vault_root: Path, snapshot: vocabulary_registry.Snapshot) -> Any:
    from .vocabulary import usage

    return usage.relations(vault_root, snapshot)


SPEC = vocabulary_registry.RegistrySpec(
    name="relations",
    stem="relation-registry",
    overlay=extension_registry_path,
    adapter=_Adapter(),
    fields=frozenset(
        {"label", "description", "aliases", "status", "replaced_by", "parent", "attributes"}
    ),
    attributes=frozenset({*_ATTRIBUTE_FIELDS, "scope"}),
    # Relations keep their meaning fixed in place; a new meaning is a new key.
    immutable=frozenset(
        {
            "parent", "description",
            *(f"attributes.{name}" for name in (*_MEANING_ATTRIBUTES, "scope")),
        }
    ),
    replacement_required=True,
    family="relation-type/v1",
    cap=512,
    usage=_usage,
)
