"""Configurable vocabulary: shipped packs, vault overlays and one registry contract.

`registry` holds the loader, the delta rules and the snapshot identity every
registry shares. Each registry module declares one `RegistrySpec` next to the
adapter for its overlay grammar; `registry_specs` collects them in the order
the bootstrap and `schema_memory` present them.
"""

from __future__ import annotations

from collections.abc import Mapping

from .registry import (
    Entry,
    RegistryError,
    RegistrySpec,
    Snapshot,
    clear_cache,
    invalidate,
    load,
)

__all__ = [
    "Entry",
    "RegistryError",
    "RegistrySpec",
    "Snapshot",
    "clear_cache",
    "invalidate",
    "load",
    "registry_spec",
    "registry_specs",
]


def registry_specs() -> Mapping[str, RegistrySpec]:
    """Every vocabulary registry, keyed by its `schema_memory` subject."""
    from .. import (
        entity_types,
        lifecycle_statuses,
        note_types,
        planning_values,
        relation_registry,
        semantic_language_registry,
        source_taxonomy,
        sync_providers,
    )

    return {
        spec.name: spec
        for spec in (
            entity_types.SPEC,
            relation_registry.SPEC,
            source_taxonomy.KIND_SPEC,
            source_taxonomy.DOMAIN_SPEC,
            semantic_language_registry.CATEGORY_SPEC,
            lifecycle_statuses.SPEC,
            planning_values.SPEC,
            note_types.SPEC,
            sync_providers.SPEC,
        )
    }


def registry_spec(name: str) -> RegistrySpec:
    specs = registry_specs()
    try:
        return specs[name]
    except KeyError:
        raise RegistryError(
            f"UNKNOWN_REGISTRY: {name!r}; registries are {', '.join(specs)}"
        ) from None
