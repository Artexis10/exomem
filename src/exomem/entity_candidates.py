"""Read-only exact entity identity resolution over the active registry."""

from __future__ import annotations

import unicodedata
from pathlib import Path

from . import memory_refs
from .entity_types import ENTITY_TYPE_REGISTRY, resolve_entity_type
from .kbdir import kb_prefix
from .vault import kb_root, parse_frontmatter, read_guarded_text


def _identity_key(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(normalized.casefold().split())


def _aliases(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str))
    return ()


def resolve_entity_candidate(
    vault_root: Path,
    *,
    name: str,
    entity_type: str | None = None,
    limit: int = 8,
) -> dict[str, object]:
    """Return an exact active title/alias match, no match, or bounded ambiguity."""
    needle = _identity_key(name)
    if not needle:
        return {"status": "no_match", "candidates": [], "omitted_candidate_count": 0}
    kind_filter = None
    if entity_type is not None:
        kind = resolve_entity_type(entity_type)
        if kind is None:
            raise ValueError(f"INVALID_LINK: unregistered entity_type {entity_type!r}")
        kind_filter = kind.id

    matches: list[dict[str, str]] = []
    entities_root = kb_root(vault_root) / "Entities"
    for definition in ENTITY_TYPE_REGISTRY:
        folder = entities_root / definition.folder
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.md"), key=lambda item: item.name.casefold()):
            if path.name.casefold() == "index.md":
                continue
            try:
                source, _guard = read_guarded_text(vault_root, path)
                logical_source = source.replace("\r\n", "\n").replace("\r", "\n")
                frontmatter, _body, _raw = parse_frontmatter(logical_source)
            except (OSError, UnicodeError, ValueError):
                continue
            if (
                str(frontmatter.get("type") or "").casefold() != "entity"
                or str(frontmatter.get("status") or "").casefold() != "active"
            ):
                continue
            registered = resolve_entity_type(str(frontmatter.get("entity_type") or ""))
            if registered is None or (
                kind_filter is not None and registered.id != kind_filter
            ):
                continue
            title = str(frontmatter.get("title") or path.stem).strip()
            title_matches = needle == _identity_key(title)
            alias_matches = any(needle == _identity_key(alias) for alias in _aliases(frontmatter.get("aliases")))
            if not title_matches and not alias_matches:
                continue
            candidate = {
                "path": path.relative_to(vault_root).as_posix(),
                "title": title,
                "entity_type": registered.id,
                "matched_by": "title" if title_matches else "alias",
            }
            if exomem_id := str(frontmatter.get("exomem_id") or "").strip():
                candidate["ref"] = memory_refs.memory_ref(exomem_id)
            matches.append(candidate)

    bounded_limit = max(1, min(int(limit), 16))
    candidates = matches[:bounded_limit]
    status = "no_match" if not matches else "match" if len(matches) == 1 else "ambiguous"
    return {
        "status": status,
        "candidates": candidates,
        "omitted_candidate_count": max(0, len(matches) - len(candidates)),
        "scope": f"{kb_prefix()}Entities",
    }
