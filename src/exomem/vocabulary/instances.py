"""Select one core-plus-extension registry through portable canonical bindings."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from .registry import PUBLIC_INSTANCE, RegistryError, RegistrySpec, Snapshot


class AssignmentRequired(RegistryError):
    """A version-1 armed vault: no instance namespace or page binding is assigned."""


@dataclass(frozen=True)
class PageDefinitions:
    """Admitted operation inputs; never published into the shared corpus cache."""

    instance_id: str
    binding_revision: str | None
    snapshots: Mapping[str, Snapshot]
    path: str
    frontmatter: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshots", MappingProxyType(dict(self.snapshots)))
        object.__setattr__(self, "frontmatter", MappingProxyType(dict(self.frontmatter)))

    def current(self, root: Path) -> bool:
        from . import registry, registry_spec

        try:
            if (page_scope(root, self.path, dict(self.frontmatter)) or PUBLIC_INSTANCE) != self.instance_id:
                return False
            for subject, previous in self.snapshots.items():
                spec = select(root, registry_spec(subject), self.instance_id)
                if spec.binding_revision != self.binding_revision:
                    return False
                fresh = registry.load(spec, root)
                if (fresh.content_hash, fresh.effective_digest) != (previous.content_hash, previous.effective_digest):
                    return False
        except (ValueError, OSError):
            return False
        return True


def page_definitions(root: Path, path: str, frontmatter: dict, subjects: tuple[str, ...]) -> PageDefinitions:
    """Select once from canonical membership, then admit only requested subjects."""
    from . import registry, registry_spec

    scope = page_scope(root, path, frontmatter)
    snapshots = {}
    binding = None
    for subject in subjects:
        spec = select(root, registry_spec(subject), scope)
        snapshots[subject] = registry.load(spec, root)
        binding = spec.binding_revision
    return PageDefinitions(scope or PUBLIC_INSTANCE, binding, snapshots, path, frontmatter)


def _path(value: object) -> str:
    if not isinstance(value, str):
        raise RegistryError("INVALID_REGISTRY_BINDING: path must be a string")
    parsed = PurePosixPath(value)
    if (not parsed.parts or parsed.is_absolute() or parsed.as_posix() != value
            or ".." in parsed.parts or "\\" in value or "\x00" in value):
        raise RegistryError("INVALID_REGISTRY_BINDING: path must be vault-relative")
    return value


def _contains(directory: str, path: str) -> bool:
    from ..structured_collections import _portable_path_key

    parent, child = _portable_path_key(directory), _portable_path_key(path)
    return child == parent or child.startswith(parent + "/")


def storage(spec: RegistrySpec, binding: dict[str, Any]) -> tuple[str, str]:
    override = binding.get("overrides", {}).get(spec.stem, {})
    return (
        override.get("overlay", f"{binding['namespace']}/{spec.overlay(Path()).name}"),
        override.get("history", f"{binding['history']}/{spec.stem}"),
    )


def validate(document: object, compiled: Any) -> dict[str, Any]:
    """Validate assignment data without consulting host configuration or membership."""
    from .. import registry_history
    from . import registry_specs

    # These field names form the closed portable binding protocol, not vocabulary.
    fields = {"public", "private", "destinations", "selections"}
    if not isinstance(document, dict) or set(document) != fields:
        raise RegistryError("INVALID_REGISTRY_BINDING: explicit instance assignment required")
    private = document["private"]
    if not isinstance(private, dict) or not set(private) <= compiled.scopes.keys():
        raise RegistryError("INVALID_REGISTRY_BINDING: unknown canonical Scope")
    bindings = {None: document["public"], **private}
    occupied: list[tuple[str | None, str]] = []
    for scope, binding in bindings.items():
        if (not isinstance(binding, dict) or not {"namespace", "history"} <= set(binding)
                or set(binding) - {"namespace", "history", "overrides"}):
            raise RegistryError("INVALID_REGISTRY_BINDING: invalid instance storage")
        _path(binding["namespace"])
        _path(binding["history"])
        overrides = binding.get("overrides", {})
        if not isinstance(overrides, dict):
            raise RegistryError("INVALID_REGISTRY_BINDING: invalid storage overrides")
        specs = registry_specs()
        storage_specs = {spec.stem: spec for spec in specs.values()}
        if set(overrides) - storage_specs.keys():
            raise RegistryError("INVALID_REGISTRY_BINDING: unknown storage override")
        for stem, override in overrides.items():
            if not isinstance(override, dict) or set(override) - {"overlay", "history"}:
                raise RegistryError("INVALID_REGISTRY_BINDING: invalid storage override")
            for value in override.values():
                _path(value)
            # Each maintained adapter fixes its storage format and file extension.
            if "overlay" in override and Path(override["overlay"]).suffix != storage_specs[stem].overlay(Path()).suffix:
                raise RegistryError("INVALID_REGISTRY_BINDING: overlay format changed")
        paths = {binding["namespace"], registry_history.history_dir(Path(), binding["history"]).as_posix()}
        for spec in specs.values():
            overlay, stem = storage(spec, binding)
            paths.update((overlay, registry_history.history_dir(Path(), stem).as_posix()))
        for path in paths:
            for other_scope, other_path in occupied:
                if scope != other_scope and (_contains(path, other_path) or _contains(other_path, path)):
                    raise RegistryError("INVALID_REGISTRY_BINDING: instance storage overlaps")
            occupied.append((scope, path))
    for field in ("destinations", "selections"):
        values = document[field]
        if not isinstance(values, dict):
            raise RegistryError("INVALID_REGISTRY_BINDING: invalid page bindings")
        for path, scope in values.items():
            _path(path)
            if scope is not None and scope not in private:
                raise RegistryError("INVALID_REGISTRY_BINDING: unknown page instance")
    return document


def configuration(root: Path) -> dict[str, Any] | None:
    from ..governance import connector_boundary

    requirement = connector_boundary.read_requirement(root)
    if requirement is None:
        return None
    if requirement["version"] == 1:
        raise AssignmentRequired(
            "REGISTRY_ASSIGNMENT_REQUIRED: registry interpretation is unavailable; "
            "assign namespaces and pages through stopped connector-boundary arming"
        )
    return requirement["vocabulary"]


def verify_legacy_assignment(root: Path, document: dict[str, Any]) -> None:
    """Require the host to assign existing definitions and history before arming."""
    from .. import registry_history
    from . import registry_specs

    bindings = (document["public"], *document["private"].values())
    assigned = {storage(spec, binding) for spec in registry_specs().values()
                for binding in bindings}
    for spec in registry_specs().values():
        legacy = spec.overlay(root)
        if legacy.exists() and legacy.relative_to(root).as_posix() not in {path for path, _ in assigned}:
            raise RegistryError("REGISTRY_ASSIGNMENT_REQUIRED: legacy definitions need explicit assignment")
        if (registry_history.history_dir(root, spec.stem).exists()
                and spec.stem not in {stem for _, stem in assigned}):
            raise RegistryError("REGISTRY_ASSIGNMENT_REQUIRED: legacy history needs explicit assignment")


def revision(document: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _legacy_storage(root: Path, spec: RegistrySpec) -> bool:
    """Whether an unassigned overlay or history for this subject exists at its default path."""
    from .. import registry_history

    return spec.overlay(root).exists() or registry_history.history_dir(root, spec.stem).exists()


def unassigned_pack_only(root: Path, spec: RegistrySpec) -> bool:
    """A version-1 vault with no legacy storage for `spec`: the shipped pack is all of it."""
    try:
        configuration(root)
    except AssignmentRequired:
        return not _legacy_storage(Path(root), spec)
    return False


def select(root: Path, spec: RegistrySpec, scope: str | None = None, *,
           authoring: bool = False) -> RegistrySpec:
    """Select storage only; the canonical release decision still owns admission.

    `authoring` marks a registry operation or overlay write. A version-1 armed
    vault assigns nothing: authoring refuses, because a write would make an
    overlay public implicitly. Interpretation of the public instance reads the
    shipped pack exactly while no legacy definition or history exists to assign.
    """
    scope = None if scope == PUBLIC_INSTANCE else scope
    try:
        document = configuration(root)
    except AssignmentRequired:
        if authoring or scope is not None or _legacy_storage(Path(root), spec):
            raise
        return spec
    if document is None:
        if scope is not None:
            raise RegistryError("REGISTRY_UNAVAILABLE: selected instance is unavailable")
        return spec
    binding = document["public"] if scope is None else document["private"].get(scope)
    if binding is None:
        raise RegistryError("REGISTRY_UNAVAILABLE: selected instance is unavailable")
    overlay, stem = storage(spec, binding)
    return replace(spec, instance_id=scope or PUBLIC_INSTANCE, binding_revision=revision(document),
                   overlay=lambda vault: Path(vault) / overlay, stem=stem)


def bound_scopes(root: Path, path: str, compiled: Any) -> frozenset[str] | None:
    """Supply assigned registry membership without decoding its definitions."""
    from .. import registry_history
    from ..governance import connector_boundary
    from . import registry_specs

    requirement = connector_boundary.read_requirement(root)
    if requirement is None or requirement["version"] == 1:
        return None
    document = requirement["vocabulary"]
    for scope, binding in ((None, document["public"]), *document["private"].items()):
        if scope is not None and scope not in compiled.scopes:
            raise RegistryError("REGISTRY_UNAVAILABLE: canonical binding is unavailable")
        assigned = frozenset((scope,)) if scope is not None else frozenset()
        history_root = registry_history.history_dir(root, binding["history"]).relative_to(root).as_posix()
        if path == binding["namespace"] or _contains(history_root, path):
            return assigned
        for spec in registry_specs().values():
            overlay, stem = storage(spec, binding)
            history = registry_history.history_dir(root, stem).relative_to(root).as_posix()
            if (_contains(overlay, path) and _contains(path, overlay)) or _contains(history, path):
                return assigned
    return None


def page_scope(root: Path, path: str, frontmatter: dict, *, registry_scope: str | None = None,
               prospective: bool = False) -> str | None:
    """Resolve canonical page membership before looking up any definition or folder."""
    from .. import find_types
    from ..governance import membership, policy

    if registry_scope is None:
        registry_scope = frontmatter.get("registry_scope")
    if registry_scope is not None and not isinstance(registry_scope, str):
        raise RegistryError("REGISTRY_UNAVAILABLE: invalid page instance selector")
    try:
        document = configuration(root)
    except AssignmentRequired:
        # Version 1 assigns no private instance, so every page selects public.
        document = None
    if document is None:
        if registry_scope not in (None, PUBLIC_INSTANCE):
            raise RegistryError("REGISTRY_UNAVAILABLE: selected instance is unavailable")
        return None
    compiled = policy.load(root)
    page = find_types.ParsedPage(path=Path(root) / path, rel_path=path,
                                frontmatter=frontmatter, body="", title="", mtime=0)
    scopes = membership._evaluate_markdown_scopes(page, compiled)
    candidates = set(scopes).intersection(document["private"])
    destinations = {scope for folder, scope in document["destinations"].items() if _contains(folder, path)}
    selected = document["selections"].get(path, registry_scope)
    explicit = path in document["selections"] or registry_scope is not None
    selected = None if selected == PUBLIC_INSTANCE else selected
    if selected is not None and selected not in document["private"]:
        raise RegistryError("REGISTRY_UNAVAILABLE: selected instance is unavailable")
    if not explicit and len(candidates) == 1:
        selected = next(iter(candidates))
    if not explicit and not candidates and prospective and len(destinations) == 1:
        selected = next(iter(destinations))
    if (prospective and not explicit and not candidates and not destinations
            or len(candidates) > 1 and selected not in candidates
            or selected is not None and not prospective and selected not in candidates
            or candidates and selected not in candidates
            or destinations and destinations != {selected}):
        # Refuse only dependent interpretation; ordinary admitted bytes remain readable.
        raise RegistryError("REGISTRY_UNAVAILABLE: page instance binding is ambiguous")
    return selected


def recheck(root: Path, spec: RegistrySpec) -> None:
    """Recheck the same binding and its current admission at the existing write seam."""
    from . import registry_spec
    from .contract import admission_refusal

    current = select(root, registry_spec(spec.name),
                     None if spec.instance_id == PUBLIC_INSTANCE else spec.instance_id)
    if (current.binding_revision != spec.binding_revision or current.stem != spec.stem
            or current.overlay(root) != spec.overlay(root) or admission_refusal(root, current)):
        raise RegistryError("REGISTRY_UNAVAILABLE: selected instance changed or is unavailable")
