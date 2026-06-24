"""Access tiers — what the skill may DO to a path, decoupled from WHERE it lives.

A path resolves to exactly one tier:

- ``excluded``    — invisible to find/embedding AND unwritable (truly private).
- ``readonly``    — findable, but every write is refused (no override). The
                    "off-limits" marker: lets a curated-thinking folder be
                    folded into ``Knowledge Base/`` and stay write-protected
                    without moving it back out of the search corpus.
- ``append-only`` — ``Sources/`` and ``Evidence/`` (add/preserve only).
- ``read-write``  — the default (Notes/, compiled material, data subtrees).

Tiers come from a live-loaded ``Knowledge Base/_access.yaml`` (folder paths, one
subtree per entry) layered over built-in defaults. The config is read fresh when
its mtime changes — edit it desk-side and the next call sees the new policy, no
restart (mirrors ``project-keys.yaml``). Decoupling *capability* from *location*
is the same move as decoupling *searchability* from a folder: a single
``Knowledge Base/`` boundary, with per-subtree access governed by this file.

This layer is ADDITIVE and back-compatible: with no ``_access.yaml`` present,
only ``Sources/``/``Evidence/`` differ from ``read-write`` — the existing
curated-tree write guard (``vault.in_curated_tree`` + ``allow_curated``) is
untouched. The migration that folds the curated trees into the KB seeds
``_access.yaml`` with them as ``readonly``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

TIER_EXCLUDED = "excluded"
TIER_READONLY = "readonly"
TIER_APPEND_ONLY = "append-only"
TIER_READ_WRITE = "read-write"

# Append-only KB subtrees — kept here (not just vault.py) so access_tier is the
# single source of truth for the tier of a path.
_APPEND_ONLY = ("Sources", "Evidence")

# (mtime, parsed_config) per config-file path, so an unchanged file isn't re-read.
_CACHE: dict[str, tuple[float, dict[str, list[str]]]] = {}


def access_config_path(vault_root: Path) -> Path:
    return vault_root / "Knowledge Base" / "_access.yaml"


def _load_config(vault_root: Path) -> dict[str, list[str]]:
    """Read ``_access.yaml`` → ``{"readonly": [...], "excluded": [...]}``.

    Missing/malformed → empty policy (never raises — a broken config must not
    take down search). Live-reloaded on mtime change.
    """
    p = access_config_path(vault_root)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {"readonly": [], "excluded": []}
    key = str(p)
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            data = {}
    except (OSError, yaml.YAMLError) as e:
        log.warning("could not read %s (%s); treating as no access policy", p.name, e)
        data = {}
    cfg = {
        "readonly": [str(x) for x in (data.get("readonly") or [])],
        "excluded": [str(x) for x in (data.get("excluded") or [])],
    }
    _CACHE[key] = (mtime, cfg)
    return cfg


def _kb_relative(rel_path: str) -> str:
    """Strip a leading ``Knowledge Base/`` so config entries are KB-relative.

    Callers pass either form (``Knowledge Base/Cognitive Core/x.md`` or
    ``Cognitive Core/x.md``); both normalize to the same KB-relative key.
    """
    rel = rel_path.replace("\\", "/").strip("/")
    parts = rel.split("/")
    if parts and parts[0] == "Knowledge Base":
        return "/".join(parts[1:])
    return rel


def _under(prefix: str, kb_rel: str) -> bool:
    """True if `kb_rel` is the subtree `prefix` or anything inside it."""
    p = prefix.replace("\\", "/").strip("/")
    return bool(p) and (kb_rel == p or kb_rel.startswith(p + "/"))


def _matches(prefixes: list[str], kb_rel: str) -> bool:
    return any(_under(p, kb_rel) for p in prefixes)


def access_tier(vault_root: Path, rel_path: str) -> str:
    """Return the tier governing `rel_path` (vault-relative, either prefix form).

    Resolution order: excluded → readonly (config) → append-only
    (Sources/Evidence) → read-write.
    """
    cfg = _load_config(vault_root)
    kb_rel = _kb_relative(rel_path)
    if _matches(cfg["excluded"], kb_rel):
        return TIER_EXCLUDED
    if _matches(cfg["readonly"], kb_rel):
        return TIER_READONLY
    head = kb_rel.split("/", 1)[0]
    if head in _APPEND_ONLY:
        return TIER_APPEND_ONLY
    return TIER_READ_WRITE


def is_indexable(vault_root: Path, rel_path: str) -> bool:
    """False only for `excluded` paths — everything else is searchable."""
    return access_tier(vault_root, rel_path) != TIER_EXCLUDED


def writable_reason(vault_root: Path, rel_path: str) -> str | None:
    """None if the path accepts ordinary writes; else a refusal reason.

    `readonly` and `excluded` are HARD refusals (no override). `append-only`
    is refused here too — those trees are written via `add`/`preserve`, not the
    general write tools — mirroring the existing append-only guard.
    """
    tier = access_tier(vault_root, rel_path)
    if tier == TIER_EXCLUDED:
        return "path is in an `excluded` tree (_access.yaml): not writable and not indexed"
    if tier == TIER_READONLY:
        return "path is in a `readonly` tree (_access.yaml): findable but write-protected"
    return None
