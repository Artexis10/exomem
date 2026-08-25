"""Checkpoint-keyed read-only enumeration of authored entity pages."""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from . import memory_refs, recall_policy
from .entity_types import EntityTypeRegistry, load_entity_types
from .find_corpus import CACHE
from .referent_resolution import EntityRecord
from .vault import kb_root

_CACHE_SIZE = 16
_REGISTRY_CACHE: OrderedDict[
    tuple[Path, tuple], Mapping[str, EntityRecord]
] = OrderedDict()
_REGISTRY_CACHE_LOCK = threading.Lock()
_REGISTRY_WARMS: set[tuple[Path, tuple]] = set()

log = logging.getLogger(__name__)


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _is_opaque(value: str) -> bool:
    """URL-shaped values are identifiers, not descriptors.

    A repository URL tokenises to its host (``github``), which every library
    in a vault shares; treating it as attribute evidence would promote any
    retrieved library on evidence that distinguishes nothing.
    """
    lowered = value.casefold()
    return "://" in lowered or lowered.startswith("www.")


def _attribute_strings(value: object) -> tuple[str, ...]:
    if value is None or isinstance(value, dict):
        return ()
    items = value if isinstance(value, list) else [value]
    return tuple(
        rendered
        for rendered in (str(item).strip() for item in items)
        if rendered and not _is_opaque(rendered)
    )


def _build_registry(
    vault_root: Path,
    type_registry: EntityTypeRegistry | None = None,
) -> Mapping[str, EntityRecord]:
    records: dict[str, EntityRecord] = {}
    entities_root = kb_root(vault_root) / "Entities"
    registry = type_registry or load_entity_types(vault_root)
    for definition in registry.active_definitions:
        folder = entities_root / definition.folder
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.md"), key=lambda item: item.name.casefold()):
            if path.name.casefold() == "index.md" or not recall_policy.is_recall_candidate(
                vault_root, path
            ):
                continue
            page = CACHE.get(path, vault_root)
            if page is None:
                continue
            frontmatter = page.frontmatter
            if str(frontmatter.get("type") or "").casefold() != "entity":
                continue
            registered = registry.resolve(str(frontmatter.get("entity_type") or ""))
            if registered is None:
                continue
            rel_path = page.rel_path
            exomem_id = str(frontmatter.get("exomem_id") or "").strip()
            records[rel_path] = EntityRecord(
                path=rel_path,
                title=str(frontmatter.get("title") or page.title or path.stem).strip(),
                entity_type=registered.id,
                status=str(frontmatter.get("status") or "active").strip().casefold(),
                aliases=_strings(frontmatter.get("aliases")),
                tags=_strings(frontmatter.get("tags")),
                relationship=str(frontmatter.get("relationship") or "").strip(),
                affiliation=str(frontmatter.get("affiliation") or "").strip(),
                attributes=tuple(
                    item
                    for field in registered.optional_frontmatter
                    if field not in {"relationship", "affiliation"}
                    for item in _attribute_strings(frontmatter.get(field))
                ),
                ref=memory_refs.memory_ref(exomem_id) if exomem_id else None,
            )
    return MappingProxyType(dict(sorted(records.items())))


def load_entity_registry(
    vault_root: Path,
    *,
    freshness_key: tuple,
    type_registry: EntityTypeRegistry | None = None,
    allow_build: bool = True,
) -> Mapping[str, EntityRecord] | None:
    """Return one immutable registry per vault/checkpoint identity.

    Managed request threads use ``allow_build=False`` so a cold additive
    referent cache can never turn into a directory scan.  The caller schedules
    the same key for background warming and simply omits the optional block.
    """
    root = Path(vault_root).absolute()
    type_registry = type_registry or load_entity_types(root)
    key = (
        root,
        (*tuple(freshness_key), type_registry.core_version, type_registry.extension_hash),
    )
    with _REGISTRY_CACHE_LOCK:
        cached = _REGISTRY_CACHE.get(key)
        if cached is not None:
            _REGISTRY_CACHE.move_to_end(key)
            return cached
        if not allow_build:
            return None
    built = _build_registry(root, type_registry)
    with _REGISTRY_CACHE_LOCK:
        existing = _REGISTRY_CACHE.get(key)
        if existing is not None:
            _REGISTRY_CACHE.move_to_end(key)
            return existing
        _REGISTRY_CACHE[key] = built
        while len(_REGISTRY_CACHE) > _CACHE_SIZE:
            _REGISTRY_CACHE.popitem(last=False)
    return built


def schedule_entity_registry_warm(
    vault_root: Path,
    *,
    freshness_key: tuple,
    type_registry: EntityTypeRegistry | None = None,
) -> None:
    """Single-flight a cold registry build away from the request thread."""
    root = Path(vault_root).absolute()
    type_registry = type_registry or load_entity_types(root)
    key = (
        root,
        (*tuple(freshness_key), type_registry.core_version, type_registry.extension_hash),
    )
    with _REGISTRY_CACHE_LOCK:
        if key in _REGISTRY_CACHE or key in _REGISTRY_WARMS:
            return
        _REGISTRY_WARMS.add(key)

    def _warm() -> None:
        try:
            load_entity_registry(
                root,
                freshness_key=freshness_key,
                type_registry=type_registry,
            )
        except Exception:  # noqa: BLE001 - optional enrichment stays soft-failing
            log.warning("entity registry background warm failed", exc_info=True)
        finally:
            with _REGISTRY_CACHE_LOCK:
                _REGISTRY_WARMS.discard(key)

    thread = threading.Thread(
        target=_warm,
        name="exomem-entity-registry-warm",
        daemon=True,
    )
    try:
        thread.start()
    except Exception:  # noqa: BLE001 - a thread-start failure cannot fail recall
        with _REGISTRY_CACHE_LOCK:
            _REGISTRY_WARMS.discard(key)
        log.warning("entity registry background warm could not start", exc_info=True)


def clear_entity_registry_cache() -> None:
    with _REGISTRY_CACHE_LOCK:
        _REGISTRY_CACHE.clear()
