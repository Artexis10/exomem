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

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from .kbdir import kb_dirname

log = logging.getLogger(__name__)

TIER_EXCLUDED = "excluded"
TIER_READONLY = "readonly"
TIER_APPEND_ONLY = "append-only"
TIER_READ_WRITE = "read-write"

# Append-only KB subtrees — kept here (not just vault.py) so access_tier is the
# single source of truth for the tier of a path.
_APPEND_ONLY = ("Sources", "Evidence")

# (stat signature, byte fingerprint, parsed config) per config-file path. Find
# refreshes the byte fingerprint once before its hot-cache lookup; page-level
# access checks then reuse this parsed snapshot without rereading the policy.
# ONLY a successful load is ever stored here: caching a degraded read would
# install a policy more permissive than the real one under an unchanged stat
# signature, which is a cached fail-open on a privacy boundary.
_PolicySignature = tuple[int, int, int, int, int]
_CACHE: dict[str, tuple[_PolicySignature, str, dict[str, list[str]]]] = {}
PUBLICATION_POLICY_MAX_BYTES = 64 * 1024

# Identity reported while the policy file exists but has never been read
# successfully in this process. Deliberately stable rather than per-error-type:
# a retry storm must not churn recall identity.
UNAVAILABLE_POLICY_FINGERPRINT = "unavailable"
# Identity of a genuinely absent policy file — a real state, not an error.
MISSING_POLICY_FINGERPRINT = "missing"


@dataclass(frozen=True, slots=True)
class PublicationPolicySnapshot:
    """One bounded exact access-policy identity for derived publication."""

    fingerprint: str


def access_config_path(vault_root: Path) -> Path:
    return vault_root / kb_dirname() / "_access.yaml"


def _load_config(vault_root: Path) -> dict[str, list[str]]:
    """Read ``_access.yaml`` → ``{"readonly": [...], "excluded": [...]}``.

    Missing/malformed → empty policy (never raises — a broken config must not
    take down search). Live-reloaded on content change. A transient stat/read
    error retains the last known-good policy instead of widening; callers that
    ENFORCE tiers must go through :func:`access_tier`, which additionally
    honors the fail-closed state this function cannot express.
    """
    return _policy_state(vault_root)[1]


def policy_fingerprint(vault_root: Path) -> str:
    """Return current access-policy byte identity and refresh its parsed cache.

    This is intentionally content-based rather than mtime-based: access policy
    changes are security boundaries and must invalidate find's hot result cache
    even after a same-size or timestamp-preserving replacement.

    A transient stat/read failure deliberately reports the LAST GOOD identity.
    Moving it would flip ``recall_policy.recall_policy_identity``, which makes
    ``live_recall_checkpoint`` fail closed and drives ``recall_checkpoint``
    into its O(corpus) reprojection branch — advancing the recall generation
    with zero writes and refusing semantic admission until the next
    republication.
    """
    return _policy_state(vault_root)[0]


def publication_policy_snapshot(vault_root: Path) -> PublicationPolicySnapshot | None:
    """Safely snapshot exact policy bytes for a derived publication.

    Unreadable, replaced, or over-limit bytes fail closed; an absent policy has
    the stable explicit ``missing`` identity.
    """
    root = Path(vault_root)
    path = access_config_path(root)
    try:
        path.lstat()
    except FileNotFoundError:
        return PublicationPolicySnapshot(MISSING_POLICY_FINGERPRINT)
    except OSError:
        return None
    try:
        from . import vault

        raw, _guard = vault.read_bounded_guarded_bytes(
            root,
            path.relative_to(root).as_posix(),
            limit=PUBLICATION_POLICY_MAX_BYTES,
        )
    except (OSError, ValueError):
        return None
    return PublicationPolicySnapshot(hashlib.sha256(raw).hexdigest())


def _policy_signature(path: Path) -> _PolicySignature:
    stat = path.stat()
    return (
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        stat.st_size,
        stat.st_dev,
        stat.st_ino,
    )


def _empty_policy() -> dict[str, list[str]]:
    return {"readonly": [], "excluded": []}


def _degraded_state(key: str) -> tuple[str, dict[str, list[str]], bool]:
    """Resolve a transient stat/read failure without ever widening visibility.

    The last successful load stays in force under its own fingerprint — and it
    is reused REGARDLESS of whether the stat signature moved. Reuse can only
    hold visibility narrower or equal to the real policy, never wider, and
    convergence to changed content happens at the next successful read. With no
    successful load to fall back on there is nothing safe to serve, so every
    path is excluded until a read succeeds.
    """
    cached = _CACHE.get(key)
    if cached is not None:
        return cached[1], cached[2], False
    return UNAVAILABLE_POLICY_FINGERPRINT, _empty_policy(), True


def _policy_state(vault_root: Path) -> tuple[str, dict[str, list[str]], bool]:
    """Return ``(fingerprint, config, fail_closed)`` for the access policy.

    An ABSENT file is a real state carrying the stable ``missing`` identity:
    only the built-in defaults apply. A stat/read ERROR is not a state — it is
    a transient failure, and is resolved by :func:`_degraded_state` rather than
    by installing (or caching) an empty policy.
    """
    path = access_config_path(vault_root)
    key = str(path)
    try:
        signature = _policy_signature(path)
    except FileNotFoundError:
        _CACHE.pop(key, None)
        return MISSING_POLICY_FINGERPRINT, _empty_policy(), False
    except OSError as error:
        log.warning(
            "could not stat %s (%s); retaining the last known-good access policy",
            path.name,
            error,
        )
        return _degraded_state(key)
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1], cached[2], False
    try:
        raw = path.read_bytes()
    except OSError as error:
        # Deliberately no FileNotFoundError branch here. The file was present
        # at stat, so its disappearance before the read is a RACE — a
        # delete-then-write save from an editor or sync client — not a settled
        # absence. Treating it as one both widened visibility and popped the
        # last known-good entry, destroying the fallback every later transient
        # error depends on. Only an absence seen at stat position (above) is
        # the genuine missing-policy identity.
        log.warning(
            "could not read %s (%s); retaining the last known-good access policy",
            path.name,
            error,
        )
        return _degraded_state(key)
    fingerprint = hashlib.sha256(raw).hexdigest()
    if cached is not None and cached[1] == fingerprint:
        _CACHE[key] = (signature, fingerprint, cached[2])
        return fingerprint, cached[2], False
    try:
        data = yaml.safe_load(raw.decode("utf-8")) or {}
        if not isinstance(data, dict):
            data = {}
    except (UnicodeError, yaml.YAMLError) as error:
        log.warning("could not parse %s (%s); treating as no access policy", path.name, error)
        data = {}
    cfg = {
        "readonly": [str(x) for x in (data.get("readonly") or [])],
        "excluded": [str(x) for x in (data.get("excluded") or [])],
    }
    _CACHE[key] = (signature, fingerprint, cfg)
    return fingerprint, cfg, False


def _kb_relative(rel_path: str) -> str:
    """Strip a leading ``Knowledge Base/`` so config entries are KB-relative.

    Callers pass either form (``Knowledge Base/Reference/x.md`` or
    ``Reference/x.md``); both normalize to the same KB-relative key.
    """
    rel = rel_path.replace("\\", "/").strip("/")
    parts = rel.split("/")
    if parts and parts[0].casefold() == kb_dirname().casefold():
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

    Resolution order: fail-closed → excluded → readonly (config) →
    append-only (Sources/Evidence) → read-write.
    """
    _fingerprint, cfg, fail_closed = _policy_state(vault_root)
    if fail_closed:
        # The policy file exists but has never been read successfully in this
        # process. Falling back to the built-in defaults here would publish
        # every `excluded` tree, so refuse the whole vault until a read lands.
        return TIER_EXCLUDED
    kb_rel = _kb_relative(rel_path)
    if _matches(cfg["excluded"], kb_rel):
        return TIER_EXCLUDED
    if _matches(cfg["readonly"], kb_rel):
        return TIER_READONLY
    head = kb_rel.split("/", 1)[0]
    # Case-insensitive: an uppercase `SOURCES/` aliases the real `Sources/` on a
    # case-insensitive filesystem, and the tier must not depend on path casing.
    if any(head.casefold() == a.casefold() for a in _APPEND_ONLY):
        return TIER_APPEND_ONLY
    return TIER_READ_WRITE


def is_indexable(vault_root: Path, rel_path: str) -> bool:
    """False only for `excluded` paths — everything else is searchable."""
    return access_tier(vault_root, rel_path) != TIER_EXCLUDED


def refuse_if_excluded(vault_root: Path, rel_path: str) -> bool:
    """True when `rel_path` is `excluded` and a read surface must refuse it.

    The single shared enforcement point every direct-read surface (get_page,
    overview, query_data, video_frames, epistemic_graph) consults at its
    path-resolve point. Refusal must be rendered indistinguishable from a
    missing path by the caller (same code/shape/text, no path echo) — this
    helper only answers "is this excluded", not how to report it.
    """
    return access_tier(vault_root, rel_path) == TIER_EXCLUDED


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
