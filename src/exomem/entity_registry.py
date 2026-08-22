"""Checkpoint-keyed read-only enumeration of authored entity pages."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from . import memory_refs, recall_policy
from .entity_types import ENTITY_TYPE_REGISTRY, resolve_entity_type
from .find_corpus import CACHE
from .referent_resolution import EntityRecord
from .vault import kb_root

_CACHE_SIZE = 16
_REGISTRY_CACHE: OrderedDict[
    tuple[Path, tuple], Mapping[str, EntityRecord]
] = OrderedDict()
_REGISTRY_CACHE_LOCK = threading.Lock()


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _attribute_strings(value: object) -> tuple[str, ...]:
    if value is None or isinstance(value, dict):
        return ()
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    rendered = str(value).strip()
    return (rendered,) if rendered else ()


def _build_registry(vault_root: Path) -> Mapping[str, EntityRecord]:
    records: dict[str, EntityRecord] = {}
    entities_root = kb_root(vault_root) / "Entities"
    for definition in ENTITY_TYPE_REGISTRY:
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
            registered = resolve_entity_type(str(frontmatter.get("entity_type") or ""))
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
    vault_root: Path, *, freshness_key: tuple
) -> Mapping[str, EntityRecord]:
    """Return one immutable registry per vault/checkpoint identity."""
    root = Path(vault_root).absolute()
    key = (root, tuple(freshness_key))
    with _REGISTRY_CACHE_LOCK:
        cached = _REGISTRY_CACHE.get(key)
        if cached is not None:
            _REGISTRY_CACHE.move_to_end(key)
            return cached
    built = _build_registry(root)
    with _REGISTRY_CACHE_LOCK:
        existing = _REGISTRY_CACHE.get(key)
        if existing is not None:
            _REGISTRY_CACHE.move_to_end(key)
            return existing
        _REGISTRY_CACHE[key] = built
        while len(_REGISTRY_CACHE) > _CACHE_SIZE:
            _REGISTRY_CACHE.popitem(last=False)
    return built


def clear_entity_registry_cache() -> None:
    with _REGISTRY_CACHE_LOCK:
        _REGISTRY_CACHE.clear()
