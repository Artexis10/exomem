"""Identical Evidence bytes are stored once: the lookup a door makes under its boundary, and the family addition.

An Evidence page lists every `(scope, category)` family its original belongs to. The page never moves and
its folder family comes first; a page written before this capability carries no list, and its folder family
stands for it until the first addition writes both. A hand-over into another family adds that one pair to
the page through a content-hash-guarded write of the page's frontmatter, and writes no original and no page.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from . import duplicates, frontmatter_update, preserve
from .held_fs import vault_name_key
from .preserve import DuplicateArtifact
from .vault import kb_root

Family = tuple[str, str]


@dataclass(frozen=True, slots=True)
class EvidenceLookup:
    """What a door learns before it stores Evidence. `available` False means the index could not answer."""

    duplicate: DuplicateArtifact | None = None
    available: bool = True


def same_family(left: Family, right: Family) -> bool:
    return tuple(map(vault_name_key, left)) == tuple(map(vault_name_key, right))


def folder_family(vault_root: Path, rel: str) -> Family | None:
    """The `(scope, category)` folder that the Evidence page at `rel` lives in, or None when it lives elsewhere."""
    parts = [part for part in rel.replace("\\", "/").split("/") if part]
    root = kb_root(vault_root).relative_to(vault_root).parts
    if tuple(parts[: len(root)]) != root:
        return None
    parts = parts[len(root):]
    if len(parts) != 4 or parts[0].casefold() != duplicates.EVIDENCE.casefold():
        return None
    return parts[1], parts[2]


def listed_families(frontmatter: Mapping[str, object], home: Family) -> tuple[Family, ...] | None:
    """The families a page lists, its folder family `home` first. None when its `families` value is malformed."""
    if "families" not in frontmatter:
        return (home,)
    value = frontmatter["families"]
    if not isinstance(value, list):
        return None
    listed: list[Family] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"scope", "category"}:  # nosemgrep: ep-word-set -- the two field names of a family pair, a closed record shape.
            return None
        scope, category = item["scope"], item["category"]
        if not (isinstance(scope, str) and scope and isinstance(category, str) and category):
            return None
        listed.append((scope, category))
    return (home, *(pair for pair in listed if not same_family(pair, home)))


def _add_family(vault_root: Path, page: str, families: tuple[Family, ...], added: Family) -> bool:
    """Write `families` and the added family's tags to the page at `page`. False when it did not land."""
    return frontmatter_update.update_page_frontmatter(
        vault_root,
        page,
        {"families": [{"scope": scope, "category": category} for scope, category in families]},
        appended={"tags": preserve.family_tags(*added)},
    )


def _find(vault_root: Path, *, scope: str, category: str, sha256: str, protected: bool):
    """The lookup both entry points share: the page, its families (None when malformed), the requested family."""
    preserve.admit_destination(vault_root, preserve.evidence_folder(vault_root, scope, category), directory=True)
    wanted = preserve.evidence_family(vault_root, scope, category)

    def listed(hit: duplicates.Duplicate) -> bool:
        home = folder_family(vault_root, hit.path)
        families = None if home is None else listed_families(hit.payload.frontmatter, home)
        return families is not None and any(same_family(pair, wanted) for pair in families)

    lookup = duplicates.find_duplicate(
        vault_root, sha256=sha256, lane=duplicates.EVIDENCE, protected=protected, prefer=listed
    )
    hit = lookup.hit
    if hit is None:
        return lookup, None, None, wanted
    home = folder_family(vault_root, hit.path)
    return lookup, hit, None if home is None else listed_families(hit.payload.frontmatter, home), wanted


def already_in_family(vault_root: Path, *, scope: str, category: str, sha256: str, protected: bool) -> bool:
    """True when the caller can read an Evidence page with these bytes that already lists the requested family.

    A read-only check for a door that refuses a repeat hand-over into the same family and writes nothing itself.
    """
    _lookup, hit, families, wanted = _find(vault_root, scope=scope, category=category, sha256=sha256, protected=protected)
    return hit is not None and families is not None and any(same_family(pair, wanted) for pair in families)


def reuse(vault_root: Path, *, scope: str, category: str, sha256: str, protected: bool) -> EvidenceLookup:
    """The Evidence page that already holds these bytes, with the requested family listed on it.

    Called under the door's mutation boundary hold, which also covers the store that follows a miss. The
    requested destination is decided for the caller first, so a refusal never depends on what the vault holds.
    A hit counts only when the caller can read the page in full (`duplicates.find_duplicate`). Adding a family
    also needs the caller's write admission on that page, and a protected page is never rewritten: its release
    grants bind to the page's content hash, so a rewrite would silently revoke them. Both cases return a miss, so
    the door stores its own copy in the requested family.
    """
    from .governance import connector_boundary

    lookup, hit, families, wanted = _find(vault_root, scope=scope, category=category, sha256=sha256, protected=protected)
    if hit is None:
        return EvidenceLookup(available=lookup.available)
    if families is None:
        return EvidenceLookup()
    duplicate = DuplicateArtifact(
        hash=sha256, path=hit.payload.artifact_path or "", sidecar_path=hit.path, ref=hit.ref, families=families
    )
    if any(same_family(pair, wanted) for pair in families):
        return EvidenceLookup(duplicate)
    if protected or not connector_boundary.effect_admission(vault_root)(hit.path):
        return EvidenceLookup()
    extended = (*families, wanted)
    if not _add_family(vault_root, hit.path, extended, wanted):
        return EvidenceLookup()
    return EvidenceLookup(replace(duplicate, families=extended, family_added=True))
