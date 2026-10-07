"""Read-only exact entity identity resolution over the active registry."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping
from pathlib import Path

from . import memory_refs
from .entity_types import load_entity_types
from .kbdir import kb_prefix
from .vault import kb_root, parse_frontmatter, read_guarded_text


def identity_key(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(normalized.casefold().split())


_identity_key = identity_key


def candidate_fingerprint(
    *, name: str, entity_type: str, resolution: Mapping[str, object]
) -> str:
    """Bind a shared-name decision to exactly the candidates it was made against.

    Covers the requested name and type, each returned candidate's identity,
    path, type and match kind, and the omitted count, so any change to what
    the name resolves to makes an earlier decision stale.
    """
    candidates = resolution.get("candidates") or []
    payload = {
        "name": identity_key(name),
        "entity_type": entity_type,
        "candidates": sorted(
            [
                str(item.get("ref") or ""),
                str(item.get("path") or ""),
                str(item.get("entity_type") or ""),
                str(item.get("matched_by") or ""),
            ]
            for item in candidates
            if isinstance(item, Mapping)
        ),
        "omitted": int(resolution.get("omitted_candidate_count") or 0),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def alias_claim_fingerprint(
    vault_root: Path, alias: str, *, exclude_path: str | None = None
) -> str:
    """The fingerprint a `distinct` decision on an alias claim must carry.

    Bound to the claimants the guard refuses on: every page `claimed_names`
    reports for this alias (notes, stems and the apostrophe, hyphen and
    soft-hyphen folds included, not just entities), each as its path and the
    hash of its current text, together with the alias's own folds. A new
    claimant or a claimant's edit therefore makes an older decision stale, and
    the value cannot be computed without a claimant. For a caller other than
    the owner the claimants are the ones it may see, so a withheld page
    neither enters the value nor moves it. A distinct entity-type marker keeps
    a title decision from ever authorizing an alias.
    """
    from . import working_set_index
    from .vault import content_hash

    claimants = claimed_names(vault_root, [alias], exclude_path=exclude_path).get(alias, ())
    versions: list[list[str]] = []
    for path in claimants:
        try:
            digest = content_hash((vault_root / path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            digest = ""
        versions.append([path, digest])
    payload = {
        "kind": "alias_claim",
        "folds": sorted({working_set_index.normalize(alias), identity_key(alias)} - {""}),
        "claimants": sorted(versions),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def claim_set_fingerprint(
    vault_root: Path,
    aliases: list[str] | tuple[str, ...],
    *,
    exclude_path: str | None = None,
    title_resolution: Mapping[str, object] | None = None,
) -> str:
    """The one fingerprint a write's `distinct` decision binds to.

    A write can claim a title and several aliases at once, and one decision
    has to decide all of them. The value is the union of every claimant the
    guard refuses on: each alias's `alias_claim_fingerprint` (its claimants and
    their content versions) and, for a create, the title's own candidates
    (identity, path, type, match kind and the omitted count, not the spelling
    of the title). A claimant appearing, changing or disappearing for any name
    in the write makes an older decision stale, and a decision made for a
    subset of the names never matches.
    """
    title: object = None
    if title_resolution is not None and (
        title_resolution.get("candidates") or title_resolution.get("omitted_candidate_count")
    ):
        title = {
            "candidates": sorted(
                [
                    str(item.get("ref") or ""),
                    str(item.get("path") or ""),
                    str(item.get("entity_type") or ""),
                    str(item.get("matched_by") or ""),
                ]
                for item in (title_resolution.get("candidates") or [])
                if isinstance(item, Mapping)
            ),
            "omitted": int(title_resolution.get("omitted_candidate_count") or 0),
        }
    payload = {
        "kind": "claim_set",
        "title": title,
        "aliases": sorted(
            alias_claim_fingerprint(vault_root, alias, exclude_path=exclude_path)
            for alias in aliases
        ),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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
    entity_family: str | None = None,
    limit: int = 8,
) -> dict[str, object]:
    """Return an exact active title/alias match, no match, or bounded ambiguity.

    For a caller other than the owner, each matched entity is decided before
    the status and counts are computed, so an identity only a withheld entity
    holds answers exactly as `no_match`. (A write that creates such an entity
    then proceeds; reconciling the duplicate is the owner's work.)
    """
    from .entity_types import extension_registry_path
    from .governance import egress
    from .governance.principal import effective_principal

    registry_path = extension_registry_path(vault_root).relative_to(vault_root).as_posix()
    # Private registry aliases and folders cannot influence public resolution;
    # this operation is unavailable until its whole registry is admitted.
    if not egress.content_permits(vault_root, registry_path, effective_principal()):
        raise ValueError("GOVERNANCE_OPERATION_UNAVAILABLE: registry observation is unavailable")

    needle = identity_key(name)
    if not needle:
        return {"status": "no_match", "candidates": [], "omitted_candidate_count": 0}
    registry = load_entity_types(vault_root)
    kind_filter = None
    if entity_type is not None:
        kind = registry.resolve(entity_type)
        if kind is None:
            raise ValueError(
                f"ENTITY_TYPE_UNKNOWN: entity_type {entity_type!r} is not active. "
                f"Active ids: {list(registry.active_ids)}"
            )
        kind_filter = kind.id
    family_filter = None
    if entity_family is not None:
        family_filter = registry.family_of(entity_family)
        if family_filter is None:
            raise ValueError(
                f"ENTITY_FAMILY_UNKNOWN: entity_family {entity_family!r} is not active. "
                f"Active families: {list(registry.families)}"
            )

    visible = egress.restricted_release_filter(vault_root)
    matches: list[dict[str, str]] = []
    entities_root = kb_root(vault_root) / "Entities"
    for definition in registry.active_definitions:
        folder = entities_root / definition.folder
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.md"), key=lambda item: item.name.casefold()):
            if path.name.casefold() == "index.md":
                continue
            rel_path = path.relative_to(vault_root).as_posix()
            if visible is not None and not visible(rel_path):
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
            registered = registry.resolve(str(frontmatter.get("entity_type") or ""))
            if registered is None or (
                kind_filter is not None and registered.id != kind_filter
            ) or (
                family_filter is not None
                and not registry.matches_family(registered.id, family_filter)
            ):
                continue
            title = str(frontmatter.get("title") or path.stem).strip()
            title_matches = needle == identity_key(title)
            alias_matches = any(needle == identity_key(alias) for alias in _aliases(frontmatter.get("aliases")))
            if not title_matches and not alias_matches:
                continue
            candidate = {
                "path": rel_path,
                "title": title,
                "entity_type": registered.id,
                "entity_family": registry.family_of(registered.id) or registered.id,
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


def claimed_names(
    vault_root: Path,
    names: list[str] | tuple[str, ...],
    *,
    exclude_path: str | None = None,
) -> dict[str, tuple[str, ...]]:
    """`{name: paths}` for each of `names` another visible page already answers to.

    An alias is a name the page answers to, and activation, wikilinks and the
    egress name map match EVERY indexed page's title, stem and aliases through
    `working_set_index.normalize`, which also folds typographic apostrophes and
    hyphens and drops soft hyphens. So the lookup is that index's own
    `resolve_names`, plus one pass over the Entities tree for an entity the
    index has not caught up with yet (compared by `identity_key` and
    `normalize` both). `exclude_path` (a page rewriting its own aliases) is
    never a collision. For a caller other than the owner, a page it may not
    see is filtered out first, so it reads exactly as absent.
    """
    from . import working_set_index
    from .governance import egress

    wanted: dict[str, str] = {}
    for name in names:
        for key in (working_set_index.normalize(name), identity_key(name)):
            if key:
                wanted.setdefault(key, name)
    if not wanted:
        return {}
    found: dict[str, set[str]] = {}
    try:
        resolved = working_set_index.WorkingSetIndex(vault_root).resolve_names(list(names))
    except working_set_index.WorkingSetIndexUnavailable:
        # Integrity, not disclosure: the Entities pass below still answers.
        resolved = {}
    for key, paths in resolved.items():
        if key in wanted:
            found.setdefault(wanted[key], set()).update(paths)

    registry = load_entity_types(vault_root)
    entities_root = kb_root(vault_root) / "Entities"
    for definition in registry.active_definitions:
        folder = entities_root / definition.folder
        if not folder.is_dir():
            continue
        for path in folder.glob("*.md"):
            if path.name.casefold() == "index.md":
                continue
            try:
                source, _guard = read_guarded_text(vault_root, path)
                frontmatter, _body, _raw = parse_frontmatter(
                    source.replace("\r\n", "\n").replace("\r", "\n")
                )
            except (OSError, UnicodeError, ValueError):
                continue
            if str(frontmatter.get("type") or "").casefold() != "entity":
                continue
            rel_path = path.relative_to(vault_root).as_posix()
            for spelling in (
                str(frontmatter.get("title") or path.stem),
                *_aliases(frontmatter.get("aliases")),
            ):
                for key in (working_set_index.normalize(spelling), identity_key(spelling)):
                    if key in wanted:
                        found.setdefault(wanted[key], set()).add(rel_path)

    visible = egress.restricted_release_filter(vault_root)
    out: dict[str, tuple[str, ...]] = {}
    for name, paths in found.items():
        kept = sorted(
            path
            for path in paths
            if path != exclude_path and (visible is None or visible(path))
        )
        if kept:
            out[name] = tuple(kept)
    return out
