"""Vault path resolution + safe-write helpers used by the add tool.

Also hosts the Tier 2 shared helpers — curated/append-only tree guards,
generic path resolution, frontmatter parse/serialize, inbound-wikilink
scan — used by the filesystem-parity operations (create_file,
list_directory, etc.).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from slugify import slugify as _slugify

from . import freshness, privacy_log
from .kbdir import kb_dirname, kb_prefix

log = logging.getLogger(__name__)


SLUG_MAX_LENGTH = 100
_EXPLICIT_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_H1_PATTERN = re.compile(r"^# (.+)$", re.MULTILINE)


class InvalidSlugError(ValueError):
    """An explicit filename slug violated the portable ASCII contract."""

# Legacy hardcoded curated-tree list — now EMPTY by default. Curated /
# read-only protection is governed per-subtree by `Knowledge Base/_access.yaml`
# (`readonly:` / `excluded:`), not by a hardcoded folder list: mark any subtree
# read-only there and the write guards (see `access.writable_reason`) refuse
# writes to it. Kept as an extension point — populate it to hard-code extra
# always-protected top-level folders without editing `_access.yaml`.
CURATED_TREES: tuple[str, ...] = ()

# Append-only trees inside the KB. Tier 2 ops refuse writes here regardless
# of any override — use `add` (for Sources) or `preserve` (for Evidence).
APPEND_ONLY_KB_SUBPATHS: tuple[str, ...] = (
    "Sources",
    "Evidence",
)

# Frontmatter keys the schema deliberately excludes (see _scaffold's
# references/frontmatter.md): numeric confidence scores misrepresent the signal
# (trust is citations + link count), and knowledge does not expire on a schedule.
# Governed write paths refuse them so the documented "no confidence floats / no
# retention decay" stance is actually enforced, not just described.
EXCLUDED_FRONTMATTER_FIELDS: frozenset[str] = frozenset(
    {"confidence", "decay_at", "expires_at"}
)


def excluded_frontmatter_reason(field: str) -> str | None:
    """A refusal reason if `field` is a schema-excluded frontmatter key, else None."""
    if field.strip().casefold() in EXCLUDED_FRONTMATTER_FIELDS:
        return (
            f"`{field}` is a schema-excluded frontmatter field. Exomem does not "
            "record numeric confidence scores or time-based decay/expiry — trust "
            "is conveyed by citations and link count, and old material is never "
            "auto-decayed (see SKILL.md). Omit this field."
        )
    return None

# When scanning the full vault for inbound wikilinks, skip these.
VAULT_SCAN_SKIP_DIRS = frozenset({
    ".obsidian", ".git", ".trash", "_attachments", "_archive", "_trash",
    "_Schema",
})

def in_excluded_scan_dir(rel_path: str) -> bool:
    """True when any segment of `rel_path` is one of VAULT_SCAN_SKIP_DIRS.

    The incremental-path counterpart of the exclusion every FULL walk applies
    (walk_vault_md, find's walker, the inbound scan): event-driven patchers
    must not index a path their index's full rebuild would skip. The concrete
    bug this guards: `delete_file` moves a note into `Knowledge Base/_trash/`,
    the watcher sees that as a fresh markdown file, and the trashed content
    gets re-embedded under its trash path — invisible to find (walks exclude
    `_trash/`) but not to the corpus-aware near-dup sweep, which reads the raw
    sidecar (observed 2026-07-04: dup warnings flagging trash entries).
    """
    return any(
        seg in VAULT_SCAN_SKIP_DIRS
        for seg in rel_path.replace("\\", "/").split("/")
    )


# `[[Target]]` or `[[Target|Alias]]`.
_WIKILINK_PATTERN = re.compile(r"\[\[([^\]\|\n]+?)(?:\|[^\]\n]*)?\]\]")
_FM_PATTERN = re.compile(r"^---\n(.*?)\n---\n?(.*)", re.DOTALL)


def resolve_vault(env_var: str = "EXOMEM_VAULT_PATH") -> Path:
    """Return the Obsidian vault root that contains Knowledge Base/.

    Resolved from the ``{env_var}`` environment variable — the vault *root*, i.e.
    the folder that contains ``Knowledge Base/``. Raises if it is unset or does
    not point at a vault. (This is cross-platform: there are no machine-specific
    fallback paths — every host sets the env var to its own vault.)
    """
    override = os.environ.get(env_var)
    if not override:
        raise RuntimeError(
            f"{env_var} is not set. Point it at your vault root — the folder "
            f"that contains '{kb_prefix()}'. For example:\n"
            f'  macOS/Linux:  export {env_var}="/path/to/your/Obsidian"\n'
            f'  Windows:      setx {env_var} "C:\\path\\to\\your\\Obsidian"'
        )
    path = Path(override)
    if not _is_vault(path):
        raise RuntimeError(
            f"{env_var}={override!r} does not look like a vault "
            f"(no {kb_prefix()}_Schema/SKILL.md found)"
        )
    return path


def _is_vault(path: Path) -> bool:
    return (path / kb_dirname() / "_Schema" / "SKILL.md").exists()


def kb_root(vault: Path) -> Path:
    return vault / kb_dirname()


def content_hash(content: str) -> str:
    """sha256 hex of a file's full raw text — the drift-guard token.

    Hashing the WHOLE content (frontmatter + body) means a concurrent
    `tags:`/`status:` change trips the guard too, not just body edits.
    `get` returns this; a writer echoes it back via `edit(expected_hash=...)`
    so a stale read can't silently clobber another writer's change.
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def slugify_title(title: str, max_length: int = SLUG_MAX_LENGTH) -> str:
    """Lowercase, dash-separated, alphanumeric-only, length-capped."""
    slug = _slugify(title, max_length=max_length, word_boundary=True, lowercase=True)
    return slug or "untitled"


def slugify_with_truncation_check(
    title: str, max_length: int = SLUG_MAX_LENGTH
) -> tuple[str, str | None]:
    """Return (slug, warning). `warning` is non-None if the slug was truncated.

    The warning names both the truncated and full slug so the caller can
    decide whether to abort, shorten the title, or accept.
    """
    slug = slugify_title(title, max_length=max_length)
    full = _slugify(title, max_length=0, word_boundary=True, lowercase=True) or "untitled"
    if slug != full:
        return slug, (
            f"slug truncated to {slug!r} (full would have been {full!r}); "
            f"shorten the title if the truncation drops meaning"
        )
    return slug, None


def resolve_filename_slug(title: str, slug: str | None = None) -> tuple[str, list[str]]:
    """Resolve a new filename component without conflating it with display title.

    Explicit slugs are deliberately strict and portable. Automatic slugging is
    kept for compatibility, including its language-blind transliteration, but
    callers get a warning whenever non-ASCII title text enters that lossy path.
    """
    if slug is not None:
        if not isinstance(slug, str) or not _EXPLICIT_SLUG_PATTERN.fullmatch(slug):
            raise InvalidSlugError(
                "slug must be lowercase ASCII kebab-case (letters, digits, and "
                "single hyphens only)"
            )
        if len(slug) > SLUG_MAX_LENGTH:
            raise InvalidSlugError(
                f"slug exceeds the {SLUG_MAX_LENGTH}-character filename limit"
            )
        return slug, []

    resolved, truncation_warning = slugify_with_truncation_check(title)
    warnings = [truncation_warning] if truncation_warning else []
    if any(ord(char) > 127 for char in title):
        warnings.append(
            "automatic filename slug used lossy, language-blind ASCII "
            "transliteration; the Unicode display title was preserved. Pass an "
            "explicit ASCII `slug` to control the filename."
        )
    return resolved, warnings


def ensure_canonical_h1(content: str, title: str) -> str:
    """Return body markdown with exactly one writer-owned title H1 at the top."""
    body = content.strip()
    canonical = f"# {title.strip()}"
    if not body:
        return canonical
    lines = body.splitlines()
    if lines and lines[0].startswith("# "):
        lines[0] = canonical
        return "\n".join(lines)
    return f"{canonical}\n\n{body}"


def resolve_display_title(frontmatter: dict[str, Any], body: str, path: Path | str) -> str:
    """Canonical display-title precedence shared by every read surface."""
    title = frontmatter.get("title") if isinstance(frontmatter, dict) else None
    if title is not None and str(title).strip():
        return str(title).strip()
    h1 = _H1_PATTERN.search(body or "")
    if h1 and h1.group(1).strip():
        return h1.group(1).strip()
    stem = Path(path).stem.replace("-", " ").replace("_", " ").strip()
    return stem or str(path)


def unique_path(directory: Path, stem: str, suffix: str = ".md") -> Path:
    """Return a path that doesn't exist yet, appending -2, -3, ... on collision."""
    candidate = directory / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate
    i = 2
    while True:
        candidate = directory / f"{stem}-{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


@dataclass
class PlannedWrite:
    """One target file in a batch write, with an optional commit-time CAS guard."""

    path: Path
    content: str
    expected_hash: str | None = None
    ensure_directories: tuple[Path, ...] = ()


class ContentHashMismatchError(RuntimeError):
    """A planned destination changed before its guarded batch could commit."""

    def __init__(self, path: Path, expected_hash: str, actual_hash: str | None):
        self.path = path
        self.expected_hash = expected_hash
        self.actual_hash = actual_hash
        actual = actual_hash or "<missing>"
        super().__init__(
            f"content changed before commit: {path} "
            f"(expected {expected_hash}, found {actual})"
        )


_BATCH_COMMIT_LOCK = threading.RLock()
MISSING_CONTENT_HASH = "<missing>"


def _create_parent_dirs(parent: Path, created_dirs: list[Path]) -> None:
    """Create missing parents and record only directories created by this call."""
    missing: list[Path] = []
    cursor = parent
    while not cursor.exists():
        missing.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            continue
        created_dirs.append(directory)


def _remove_empty_created_dirs(created_dirs: list[Path]) -> None:
    """Best-effort rollback for empty parent directories created during staging."""
    for directory in reversed(created_dirs):
        try:
            directory.rmdir()
        except OSError:
            pass


def batch_atomic_write(
    writes: Iterable[PlannedWrite], *, vault_root: Path | None = None
) -> list[Path]:
    """Commit one batch while serializing all in-process vault writers.

    A process-shared lock closes the gap between validating any ``expected_hash``
    guards and replacing destinations. The locked implementation retains the
    existing staging, rollback, and index-refresh behavior.
    """
    with _BATCH_COMMIT_LOCK:
        return _batch_atomic_write_locked(writes, vault_root=vault_root)


def _batch_atomic_write_locked(
    writes: Iterable[PlannedWrite], *, vault_root: Path | None = None
) -> list[Path]:
    """Stage each write as a sibling .tmp file, then os.replace() them into place.

    On any exception during staging, no replacements happen — temps are cleaned.
    Before replacement starts, existing destinations are snapshotted to sibling
    backup files. A mid-flip failure restores every replaced destination and
    removes destinations newly created by the batch before re-raising.

    When `vault_root` is supplied, the embedding sidecar at
    `<vault>/Knowledge Base/.embeddings.sqlite` is refreshed for every
    embeddable file in the batch after the markdown writes succeed. Failures
    in the embedding pass are logged and swallowed — keyword-mode find()
    still works, and `audit_fix(rebuild_embeddings=True)` recovers drift.
    """
    # Several high-level writers independently refresh the same navigation
    # file in one logical batch. Preserve the original destination order but
    # commit only the last planned content for each path.
    deduped: dict[Path, PlannedWrite] = {}
    for write in writes:
        deduped[write.path] = write
    writes = list(deduped.values())
    for write in writes:
        if write.expected_hash is None:
            continue
        try:
            current = write.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            actual_hash = None
        else:
            actual_hash = content_hash(current)
        expected_missing = write.expected_hash == MISSING_CONTENT_HASH
        if not (expected_missing and actual_hash is None) and actual_hash != write.expected_hash:
            raise ContentHashMismatchError(
                write.path, write.expected_hash, actual_hash
            )
    # Access-tier backstop: when the caller knows the vault root, refuse any
    # write that lands in a `readonly`/`excluded` tree (_access.yaml). Central
    # here so every content writer inherits it without per-tool wiring. No
    # `_access.yaml` → writable_reason() is always None → no-op (Sources/Evidence
    # are append-only, not readonly, so add/preserve still write fine).
    if vault_root is not None:
        from . import access

        vault_resolved = vault_root.resolve()
        for w in writes:
            try:
                rel = w.path.resolve().relative_to(vault_resolved).as_posix()
            except (ValueError, OSError) as e:
                # Fail CLOSED: a staged write that resolves outside the vault must
                # never proceed (callers resolve paths first, so this is a
                # defense-in-depth backstop, not a normal path).
                raise ValueError(
                    f"WRITE_REFUSED: {w.path} resolves outside the vault root"
                ) from e
            reason = access.writable_reason(vault_root, rel)
            if reason is not None:
                raise ValueError(f"WRITE_REFUSED: {rel}: {reason}")
    staged: list[tuple[Path, Path]] = []  # (final, tmp)
    created_dirs: list[Path] = []
    try:
        for w in writes:
            for directory in w.ensure_directories:
                _create_parent_dirs(directory, created_dirs)
            _create_parent_dirs(w.path.parent, created_dirs)
            # NamedTemporaryFile would need delete=False + cross-platform care;
            # explicit tmp sibling is simpler and survives os.replace.
            fd, tmp_str = tempfile.mkstemp(
                prefix=f".{w.path.name}.", suffix=".tmp", dir=str(w.path.parent)
            )
            os.close(fd)
            tmp = Path(tmp_str)
            staged.append((w.path, tmp))
            tmp.write_text(w.content, encoding="utf-8", newline="\n")
    except Exception:
        for _, tmp in staged:
            tmp.unlink(missing_ok=True)
        _remove_empty_created_dirs(created_dirs)
        raise

    backups: dict[Path, Path | None] = {}
    try:
        for final, _tmp in staged:
            if not final.exists():
                backups[final] = None
                continue
            fd, backup_str = tempfile.mkstemp(
                prefix=f".{final.name}.", suffix=".bak", dir=str(final.parent)
            )
            os.close(fd)
            backup = Path(backup_str)
            shutil.copy2(final, backup)
            backups[final] = backup
    except Exception:
        for _, tmp in staged:
            tmp.unlink(missing_ok=True)
        for backup in backups.values():
            if backup is not None:
                backup.unlink(missing_ok=True)
        _remove_empty_created_dirs(created_dirs)
        raise

    replaced: list[Path] = []
    try:
        from .writer_lease import validate_active_write_fence

        validate_active_write_fence()
        for final, tmp in staged:
            os.replace(tmp, final)
            replaced.append(final)
    except Exception as commit_error:
        rollback_errors: list[str] = []
        for final in reversed(replaced):
            backup = backups.get(final)
            try:
                if backup is None:
                    final.unlink(missing_ok=True)
                else:
                    os.replace(backup, final)
                    backups[final] = None
            except Exception as rollback_error:  # noqa: BLE001 - report every restore failure
                rollback_errors.append(f"{final}: {rollback_error}")
        replaced_paths = set(replaced)
        for final, tmp in staged:
            if final not in replaced_paths and tmp.exists():
                tmp.unlink(missing_ok=True)
        if rollback_errors:
            retained = [str(path) for path in backups.values() if path is not None]
            _remove_empty_created_dirs(created_dirs)
            raise RuntimeError(
                f"batch commit failed ({commit_error}); rollback also failed: "
                + "; ".join(rollback_errors)
                + (f"; retained backups: {retained}" if retained else "")
            ) from commit_error
        for backup in backups.values():
            if backup is not None:
                backup.unlink(missing_ok=True)
        _remove_empty_created_dirs(created_dirs)
        raise
    else:
        for backup in backups.values():
            if backup is not None:
                backup.unlink(missing_ok=True)

    if vault_root is not None and replaced:
        # Register the self-authored replacements so the live watcher drops
        # their echo instead of re-embedding the same files a second time.
        try:
            from . import file_watcher
            file_watcher.register_self_write(vault_root, replaced)
        except Exception:  # noqa: BLE001 — suppression is best-effort
            import logging
            logging.getLogger(__name__).debug(
                "self-write suppression registration failed", exc_info=True
            )
        try:
            from . import index_sync
            index_sync.upsert_after_write(vault_root, replaced)
        except Exception:  # noqa: BLE001 — embeddings are best-effort
            import logging
            logging.getLogger(__name__).exception(
                "embedding upsert failed after batch_atomic_write; "
                "sidecar may be stale until audit_fix(rebuild_embeddings=True)"
            )
    return replaced


@contextmanager
def chdir(path: Path):
    """Temporary cwd switch — used in tests."""
    prev = Path.cwd()
    os.chdir(path)
    try:
        yield path
    finally:
        os.chdir(prev)


# ---------------- Tier 2 shared helpers ----------------


class VaultPathError(Exception):
    """Raised when a path can't be resolved under the vault root."""

    def __init__(self, code: str, reason: str):
        self.code = code
        self.reason = reason
        super().__init__(reason)


def resolve_under_vault(
    vault_root: Path,
    path: str,
    *,
    must_exist: bool = False,
    must_be_file: bool = False,
    must_be_dir: bool = False,
    must_be_under_kb: bool = False,
) -> tuple[Path, str]:
    """Resolve a vault-relative path; guard against escape; normalize.

    Returns `(absolute_path, vault_relative_posix)`. The relative form is
    always forward-slashed, never starts with `/`. The leading
    `Knowledge Base/` is preserved as-is (we don't auto-strip it like
    `get_page` does — Tier 2 ops take explicit paths).

    `must_be_under_kb` additionally refuses any target that resolves OUTSIDE
    `Knowledge Base/` (checked on the resolved path, so `Knowledge Base/../x`
    can't sneak a write to a vault-root sibling of KB). Governed content writers
    (`create`/`append`) set it — exomem only ever authors under `Knowledge Base/`.

    Raises VaultPathError with code in {INVALID_PATH, NOT_FOUND,
    NOT_A_FILE, NOT_A_DIR}.
    """
    if path is None:
        raise VaultPathError(code="INVALID_PATH", reason="path is required")
    raw = str(path).strip()
    if not raw:
        raise VaultPathError(code="INVALID_PATH", reason="path is empty")

    rel = raw.replace("\\", "/").lstrip("/")
    if privacy_log.is_reserved_hosted_vault_path(rel):
        raise VaultPathError(code="INVALID_PATH", reason="path is reserved by hosted runtime")
    # Reject absolute paths (drive letters or leading drive)
    if re.match(r"^[a-zA-Z]:", rel):
        raise VaultPathError(
            code="INVALID_PATH",
            reason=f"absolute paths are not allowed: {raw!r}",
        )

    if must_be_under_kb:
        # Governed writes are KB-relative: a bare `Reference/x.md` means
        # `Knowledge Base/Reference/x.md` (matching how access tiers are keyed),
        # so root it under KB unless it already is (any case) or leads with `..`
        # (left for the escape guards below to reject). This makes bare and
        # prefixed paths resolve to the SAME governed location instead of a bare
        # path silently writing to a vault-root sibling of Knowledge Base/.
        first = rel.split("/", 1)[0]
        if first.casefold() != kb_dirname().casefold() and first != "..":
            rel = f"{kb_dirname()}/{rel}"

    candidate = vault_root / rel
    try:
        resolved = candidate.resolve()
        vault_resolved = vault_root.resolve()
        resolved.relative_to(vault_resolved)
    except (ValueError, OSError) as e:
        raise VaultPathError(
            code="INVALID_PATH",
            reason=f"path escapes vault root: {raw!r} ({e})",
        ) from None

    if must_be_under_kb:
        kb_resolved = (vault_root / kb_dirname()).resolve()
        try:
            resolved.relative_to(kb_resolved)
        except ValueError:
            raise VaultPathError(
                code="INVALID_PATH",
                reason=(
                    f"path is outside Knowledge Base/: {raw!r} — exomem only "
                    "writes governed content under Knowledge Base/"
                ),
            ) from None

    if must_exist and not candidate.exists():
        raise VaultPathError(
            code="NOT_FOUND",
            reason=f"path does not exist: {rel}",
        )
    if must_be_file and candidate.exists() and not candidate.is_file():
        raise VaultPathError(
            code="NOT_A_FILE",
            reason=f"path is not a regular file: {rel}",
        )
    if must_be_dir and candidate.exists() and not candidate.is_dir():
        raise VaultPathError(
            code="NOT_A_DIR",
            reason=f"path is not a directory: {rel}",
        )

    # Normalize the *returned* rel-form. resolved.relative_to(...) lowercases
    # the drive on Windows; use the literal candidate-form for stability.
    return candidate, rel


def in_curated_tree(rel_path: str) -> str | None:
    """Return the curated-tree name if `rel_path` is inside one, else None.

    `rel_path` is vault-relative POSIX form (e.g. "Reference/foo.md"). Note
    that ``CURATED_TREES`` is empty by default — read-only protection now lives
    in ``_access.yaml`` (see ``access.writable_reason``), so this returns None
    unless ``CURATED_TREES`` has been explicitly populated.
    """
    head = rel_path.split("/", 1)[0]
    if head in CURATED_TREES:
        return head
    return None


def _is_curated_top_level(vault_root: Path, head: str) -> bool:
    """True if `head` names a top-level vault folder that is curated/read-only.

    Used so an unresolved wikilink into such a folder (a forward reference to a
    file that doesn't exist yet) is kept vault-relative rather than promoted
    under ``Knowledge Base/``. Curated/read-only status is sourced from
    ``Knowledge Base/_access.yaml`` (``readonly:`` / ``excluded:``); the legacy
    ``CURATED_TREES`` tuple (empty by default) is also honored.
    """
    if head in CURATED_TREES:
        return True
    try:
        from . import access

        return access.access_tier(vault_root, head) in (
            access.TIER_READONLY,
            access.TIER_EXCLUDED,
        )
    except Exception:  # noqa: BLE001 — access policy is best-effort here
        return False


def in_append_only_tree(rel_path: str) -> str | None:
    """Return the canonical subpath name ("Sources" or "Evidence") if matched.

    Matches both `Knowledge Base/Sources/...` and bare `Sources/...` —
    callers may pass either form. Matching is case-insensitive (see below).
    """
    parts = rel_path.replace("\\", "/").split("/")
    if not parts:
        return None
    if len(parts) > 1 and parts[0].casefold() == kb_dirname().casefold():
        head = parts[1]
    else:
        head = parts[0]
    # Case-insensitive match returning the CANONICAL name: on a case-insensitive
    # filesystem (Windows/macOS) an uppercase `SOURCES/` aliases the real
    # `Sources/` on disk, so a case-sensitive check would let raw Sources/Evidence
    # be edited/appended/deleted through the alias.
    for canonical in APPEND_ONLY_KB_SUBPATHS:
        if head.casefold() == canonical.casefold():
            return canonical
    return None


# libyaml's CSafeLoader is the same safe schema as SafeLoader at ~7x the parse
# speed (measured 609ms -> 89ms over 1,730 frontmatter blocks, 2026-07-04).
# PyYAML wheels bundle libyaml on all supported platforms; fall back silently
# on a custom build without it. Used by the HOT parse seams only (this module's
# parse_frontmatter + find's page parser) — one-off config loads keep safe_load.
_YAML_SAFE_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def yaml_safe_load(text: str):
    """`yaml.safe_load` via libyaml when available (hot-path frontmatter seam).

    SAFETY: `_YAML_SAFE_LOADER` is CSafeLoader or SafeLoader — both the safe
    schema; `!!python/*` tags raise ConstructorError instead of constructing.
    Pinned by tests/test_yaml_loader_safety.py — do not widen the loader.
    """
    return yaml.load(text, Loader=_YAML_SAFE_LOADER)  # noqa: S506 — safe schema, see above


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str, str | None]:
    """Split a markdown file into (frontmatter_dict, body, frontmatter_text).

    Returns ({}, text, None) when no frontmatter block is present.
    `body` has no leading newline (mirrors find._parse_page).
    """
    m = _FM_PATTERN.match(text)
    if not m:
        return {}, text, None
    fm_text = m.group(1)
    body = m.group(2)
    if body.startswith("\n"):
        body = body[1:]
    try:
        fm = yaml_safe_load(fm_text) or {}
        if not isinstance(fm, dict):
            fm = {}
    except yaml.YAMLError:
        fm = {}
    return fm, body, fm_text


def serialize_frontmatter(fm: dict[str, Any]) -> str:
    """YAML-serialize a frontmatter dict into the inner block (no `---` fences).

    Uses block-flow style consistent with the rest of the codebase: scalars
    are inline, lists are inline `[a, b, c]` for short lists.
    """
    if not fm:
        return ""
    lines: list[str] = []
    for key, value in fm.items():
        lines.append(_format_yaml_line(key, value))
    return "\n".join(lines)


def _format_yaml_line(key: str, value: Any) -> str:
    """Format a single `key: value` line matching add/note/link style."""
    if value is None:
        return f"{key}:"
    if isinstance(value, bool):
        return f"{key}: {'true' if value else 'false'}"
    if isinstance(value, (int, float)):
        return f"{key}: {value}"
    if isinstance(value, list):
        if not value:
            return f"{key}: []"
        # Inline form for short string lists; matches add.py's tags rendering.
        items = ", ".join(_yaml_scalar(v) for v in value)
        return f"{key}: [{items}]"
    if isinstance(value, dict):
        # Fall back to PyYAML block-style for nested dicts.
        block = yaml.safe_dump({key: value}, default_flow_style=False, sort_keys=False)
        return block.rstrip("\n")
    return f"{key}: {_yaml_scalar(value)}"


def yaml_scalar(value: Any) -> str:
    """Render a scalar, quoting if it contains YAML-special chars."""
    s = str(value)
    try:
        parsed = yaml.safe_load(s)
    except yaml.YAMLError:
        parsed = None
    needs_quote = (
        not isinstance(parsed, str)
        or parsed != s
        or any(c in s for c in [":", "#", "[", "]", "{", "}", ",", "\n", "\r"])
        or s.strip() != s
    )
    if needs_quote:
        return json.dumps(s, ensure_ascii=False)
    return s


# Backward-compatible private name for existing call sites.
_yaml_scalar = yaml_scalar


def walk_vault_md(vault_root: Path):
    """Yield every .md path under vault_root, skipping config/cruft dirs.

    Walks the FULL vault, not just Knowledge Base/. Used by Tier 2 inbound-
    wikilink scans and move/delete safety checks.
    """
    def walk(d: Path):
        try:
            children = list(d.iterdir())
        except OSError:
            return
        for child in children:
            if child.is_dir():
                if child.name in VAULT_SCAN_SKIP_DIRS:
                    continue
                yield from walk(child)
            elif (
                child.is_file()
                and child.suffix.lower() == ".md"
                and ".sync-conflict-" not in child.name
            ):
                # Skip Obsidian sync-conflict duplicates — they aren't real
                # notes; indexing/scanning them pollutes search and wikilink
                # resolution.
                yield child
    yield from walk(vault_root)


@dataclass
class InboundLink:
    path: str          # vault-relative POSIX of the file containing the link
    line_number: int   # 1-based
    context: str       # the line text (trimmed)
    raw_target: str    # the exact text inside [[...]]

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "line_number": self.line_number,
            "context": self.context,
            "raw_target": self.raw_target,
        }


# ---------------- inbound-link index ----------------
# One full-vault read pass builds normalized-target -> entry buckets plus a
# basename count map; `find_inbound_wikilinks` becomes a lookup with output
# identical (content AND order) to the historical per-call scan. Freshness is
# the digest-strength walk key from find._walk_freshness_key — deliberately
# stronger than count/max-mtime because move_file/delete_file SAFETY checks
# consume this and a pure rename changes neither count nor any mtime.


@dataclass
class _InboundEntry:
    seq: int           # global scan order: (file walk order, line, match)
    path: str          # vault-relative POSIX of the file containing the link
    line_number: int
    context: str
    raw_target: str


@dataclass
class _InboundIndexData:
    buckets: dict[str, list[_InboundEntry]]  # normalized target -> entries
    stem_counts: dict[str, int]              # basename -> occurrences in walk
    known_rels: set[str]                     # vault-relative POSIX paths already
                                              # counted toward stem_counts — lets
                                              # on_files_changed() tell a rename's
                                              # "new" side from an in-place edit.

    def on_files_changed(
        self,
        vault_root: Path,
        changed_rels: Iterable[str],
        deleted_rels: Iterable[str],
    ) -> None:
        """Patch this index in place for one batch of file changes.

        For every affected path: drop its existing edges from `buckets` and
        its stem-count contribution, then — for paths that still exist on
        disk — re-read just that file and re-add its edges + stem-count
        contribution. New entries get `seq` values appended after the
        current max `seq` (design D3): a patched file's relative order vs.
        entries from OTHER files touched at a different time does not mirror
        a fresh full-walk order, but the output SET per target always
        matches a full rebuild.

        A rel present in BOTH `changed_rels` and `deleted_rels` in the same
        batch (two path-string forms of one file collapsing to the same rel
        upstream — Windows 8.3 short names are the concrete vector, #126, but
        this defends against ANY dual-form vector: case aliasing, symlinks,
        a future one) is a same-batch conflict. Trust the filesystem to break
        the tie: a rel whose file still exists is a change, not a delete —
        dropping it would silently remove a live file's inbound-link edges.
        """
        changed = set(changed_rels)
        deleted = set(deleted_rels)
        conflict = changed & deleted
        for rel in conflict:
            if (vault_root / rel).is_file():
                deleted.discard(rel)
            else:
                changed.discard(rel)
        affected = changed | deleted
        if not affected:
            return

        # 1. Drop every affected file's existing edges from every bucket.
        for target in list(self.buckets.keys()):
            kept = [e for e in self.buckets[target] if e.path not in affected]
            if kept:
                self.buckets[target] = kept
            else:
                del self.buckets[target]

        # 2. A "changed" path that vanished between the event firing and this
        #    patch running behaves exactly like a delete.
        still_exists: dict[str, Path] = {}
        for rel in changed:
            abs_path = vault_root / rel
            if abs_path.is_file():
                still_exists[rel] = abs_path
            else:
                deleted.add(rel)

        # 3. Drop the stem-count contribution for every path that is now gone.
        for rel in deleted:
            if rel in self.known_rels:
                stem = Path(rel).stem
                count = self.stem_counts.get(stem, 0) - 1
                if count > 0:
                    self.stem_counts[stem] = count
                else:
                    self.stem_counts.pop(stem, None)
                self.known_rels.discard(rel)

        # 4. Re-read each still-existing changed file and re-add its edges +
        #    stem-count contribution (only if it's a path we didn't already
        #    know about — an in-place edit of a known path leaves the count
        #    alone).
        next_seq = 1 + max(
            (e.seq for entries in self.buckets.values() for e in entries),
            default=-1,
        )
        for rel, abs_path in still_exists.items():
            if rel not in self.known_rels:
                stem = Path(rel).stem
                self.stem_counts[stem] = self.stem_counts.get(stem, 0) + 1
                self.known_rels.add(rel)
            try:
                text = abs_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for lineno, context, raw in _scan_wikilinks(text):
                normalized = raw.split("#", 1)[0].rstrip().removesuffix(".md")
                self.buckets.setdefault(normalized, []).append(_InboundEntry(
                    seq=next_seq,
                    path=rel,
                    line_number=lineno,
                    context=context,
                    raw_target=raw,
                ))
                next_seq += 1


_INBOUND_INDEX: dict[str, tuple[tuple, _InboundIndexData]] = {}


def _scan_wikilinks(text: str) -> list[tuple[int, str, str]]:
    """`(line_number, trimmed_context, raw_target)` for every wikilink match.

    Shared by the full-vault build and the per-file patch so the two stay in
    lockstep — a patched file's entries are byte-identical to what a fresh
    full rebuild would produce for that same file content.
    """
    out: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for m in _WIKILINK_PATTERN.finditer(line):
            out.append((lineno, line.strip()[:240], m.group(1).strip()))
    return out


def _build_inbound_index(vault_root: Path) -> _InboundIndexData:
    buckets: dict[str, list[_InboundEntry]] = {}
    stem_counts: dict[str, int] = {}
    known_rels: set[str] = set()
    vault_resolved = vault_root.resolve()
    seq = 0
    for md in walk_vault_md(vault_root):
        # Basename counts cover every walked file, readable or not — matching
        # the historical uniqueness scan, which never opened files.
        stem_counts[md.stem] = stem_counts.get(md.stem, 0) + 1
        try:
            md_rel = md.resolve().relative_to(vault_resolved).as_posix()
        except ValueError:
            continue
        # Recorded regardless of read success so a later patch can tell this
        # path was already part of the walk (an in-place edit) from a path
        # that's genuinely new (a create, or the new side of a rename).
        known_rels.add(md_rel)
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, context, raw in _scan_wikilinks(text):
            # Strip `#anchor` before comparison — anchors are intra-page
            # jumps, not part of the file path.
            normalized = raw.split("#", 1)[0].rstrip().removesuffix(".md")
            buckets.setdefault(normalized, []).append(_InboundEntry(
                seq=seq,
                path=md_rel,
                line_number=lineno,
                context=context,
                raw_target=raw,
            ))
            seq += 1
    return _InboundIndexData(buckets=buckets, stem_counts=stem_counts, known_rels=known_rels)


def _vault_freshness_key(vault_root: Path):
    """The vault-scope freshness triple — from the event-maintained registry
    when it is live (syscall-free), else a fresh stat-walk. Byte-identical
    either way, so the inbound index's staleness check no longer walks the
    vault per call once the registry is live (P3)."""
    live = freshness.triple(vault_root, "vault")
    if live is not None:
        return live
    from . import find as find_module

    return find_module._walk_freshness_key(walk_vault_md(vault_root))


def _inbound_index(vault_root: Path) -> _InboundIndexData:
    """The cached index, rebuilt when the vault's freshness key moves."""
    key = _vault_freshness_key(vault_root)
    root = str(vault_root.resolve())
    cached = _INBOUND_INDEX.get(root)
    if cached and cached[0] == key:
        return cached[1]
    data = _build_inbound_index(vault_root)
    _INBOUND_INDEX[root] = (key, data)
    return data


def on_inbound_files_changed(
    vault_root: Path,
    changed_rels: Iterable[str],
    deleted_rels: Iterable[str],
) -> None:
    """Patch the process-cached inbound-link index for one batch of changes.

    No-op when `EXOMEM_DISABLE_EVENT_INDEXES` is set (the single kill switch
    reverts inbound maintenance along with freshness/matrix, per design D5),
    or when this vault's index has never been built — nothing cached to
    patch, and the next `find_inbound_wikilinks` call does a full digest-keyed
    rebuild that already reflects current disk state, so skipping here is
    correct, not just cheap. This is what makes the patch path "live-only":
    it only ever mutates an index that already exists.

    After patching, re-syncs the cached freshness key to the patched state's
    current on-disk key, so the next `_inbound_index` call sees a cache HIT
    instead of redundantly re-triggering `_build_inbound_index`'s full
    read-and-reparse pass — the entire point of this patch API (P3).
    """
    if not freshness.event_indexes_enabled():
        return
    root = str(vault_root.resolve())
    cached = _INBOUND_INDEX.get(root)
    if cached is None:
        return
    changed_list = list(changed_rels)
    deleted_list = list(deleted_rels)
    if not (changed_list or deleted_list):
        return
    _, data = cached
    data.on_files_changed(vault_root, changed_list, deleted_list)
    _INBOUND_INDEX[root] = (_vault_freshness_key(vault_root), data)


def clear_inbound_index() -> None:
    """Test hook: drop every cached inbound-link index (patch state included —
    `known_rels`/`buckets`/`stem_counts` all live inside the cached
    `_InboundIndexData`, so clearing the outer dict resets everything)."""
    _INBOUND_INDEX.clear()


def find_inbound_wikilinks(
    vault_root: Path, target_rel_path: str
) -> list[InboundLink]:
    """Return every wikilink in the vault that resolves to `target_rel_path`.

    `target_rel_path` is vault-relative POSIX, with or without `.md`. Matches
    three forms:
    - full path with leading `Knowledge Base/`: `[[Knowledge Base/Notes/Insights/foo]]`
    - KB-stripped path: `[[Notes/Insights/foo]]`
    - bare basename (only if unambiguous in the vault): `[[foo]]`

    The bare-basename match only fires if the target's basename is unique
    across the vault — otherwise an inbound `[[foo]]` could mean any
    same-named file, so we don't claim it.

    Served from the process-cached inbound-link index (one read pass per
    vault revision) — results identical to scanning every file per call.
    """
    target = target_rel_path.replace("\\", "/").removesuffix(".md")
    target_full = target if target.startswith(kb_prefix()) else kb_prefix() + target
    target_stripped = target_full.removeprefix(kb_prefix())
    target_basename = target.rsplit("/", 1)[-1]

    data = _inbound_index(vault_root)
    basename_unique = data.stem_counts.get(target_basename, 0) == 1

    candidates: list[_InboundEntry] = []
    candidates.extend(data.buckets.get(target_full, ()))
    if target_stripped != target_full:
        candidates.extend(data.buckets.get(target_stripped, ()))
    # The basename bucket only contributes when it isn't already one of the
    # path-form buckets (e.g. a KB-root file where stripped == basename).
    if (
        basename_unique
        and "/" not in target_basename
        and target_basename not in (target_full, target_stripped)
    ):
        candidates.extend(data.buckets.get(target_basename, ()))

    self_keys = (target_full, target_stripped)
    return [
        InboundLink(
            path=e.path,
            line_number=e.line_number,
            context=e.context,
            raw_target=e.raw_target,
        )
        for e in sorted(candidates, key=lambda e: e.seq)
        # Skip the target file itself (self-references aren't inbound).
        if e.path.removesuffix(".md") not in self_keys
    ]


# ---------------- wikilink normalization ----------------


class WikilinkError(Exception):
    """Base class for wikilink-resolution problems."""


class UnresolvedWikilinkError(WikilinkError):
    """No file in the vault matches the wikilink target."""


class AmbiguousWikilinkError(WikilinkError):
    """A bare-name wikilink matches more than one file."""


def _discard_from_list(mapping: dict[str, list[str]], key: str, value: str) -> None:
    """Remove `value` from `mapping[key]`'s list; drop the key if it empties.

    Shared by the resolver's stem/title patch paths — keeps a multi-match
    bucket (e.g. two files with the same stem) correct when only one side is
    edited or deleted.
    """
    lst = mapping.get(key)
    if not lst:
        return
    remaining = [v for v in lst if v != value]
    if remaining:
        mapping[key] = remaining
    else:
        mapping.pop(key, None)


class WikilinkResolver:
    """In-memory index of vault paths + frontmatter titles for wikilink resolution.

    Build once per write op; pass to `normalize_wikilink()` and
    `normalize_body_wikilinks()` for each link. Cuts the walk cost from
    once-per-link to once-per-op.

    The resolver knows three keying strategies:
    - `full_paths`: vault-relative POSIX without `.md` (e.g.
      `Knowledge Base/Entities/Concepts/Profile`).
    - `kb_stripped`: same with the leading `Knowledge Base/` removed.
    - `stems`: filename stem (no path) → list of full paths (multi-match if
      the basename collides across folders).
    - `titles`: frontmatter `title:` lower-cased → list of full paths. This
      lets `[[North-Led Content Manual]]` resolve to a source file whose
      stem is date-prefixed (`2026-05-15-tu-north-led-content-manual`) but
      whose title matches.
    """

    def __init__(self, vault_root: Path):
        self.vault_root = vault_root
        self.full_paths: set[str] = set()
        self.kb_stripped: set[str] = set()
        self.stems: dict[str, list[str]] = {}
        self.titles: dict[str, list[str]] = {}
        # no_ext rel path -> the (lower-cased) frontmatter title it contributed
        # to `titles`, so an incremental patch can drop the OLD title edge
        # before re-adding the new one (a title-only edit still needs fixing).
        self._title_by_rel: dict[str, str] = {}
        self._build()

    def _build(self) -> None:
        vault_resolved = self.vault_root.resolve()
        for md in walk_vault_md(self.vault_root):
            try:
                rel = md.resolve().relative_to(vault_resolved).as_posix()
            except ValueError:
                continue
            self._add_entry(rel.removesuffix(".md"), self._read_title_lower(md))

    # ---- shared add/remove primitives -------------------------------------
    # The full build AND the incremental patch both go through these, so a
    # patched resolver's maps are byte-identical to a fresh rebuild's for the
    # same on-disk state (parity is what keeps the graph lane's recall
    # unchanged — only the cost model differs).

    def _add_entry(self, no_ext: str, title_lower: str | None) -> None:
        """Index one file's path/stem (always) and title (when present).

        Mirrors `_build`'s historical per-file body exactly: the path + stem
        edges are added even for an unreadable file (title read failed), the
        title edge only when a non-empty frontmatter `title:` was read.
        """
        self.full_paths.add(no_ext)
        self.kb_stripped.add(no_ext.removeprefix(kb_prefix()))
        stem = no_ext.rsplit("/", 1)[-1]
        self.stems.setdefault(stem, []).append(no_ext)
        if title_lower:
            self.titles.setdefault(title_lower, []).append(no_ext)
            self._title_by_rel[no_ext] = title_lower

    def _remove_entry(self, no_ext: str) -> None:
        """Drop every edge a file contributed (path, stem, title)."""
        self.full_paths.discard(no_ext)
        self.kb_stripped.discard(no_ext.removeprefix(kb_prefix()))
        _discard_from_list(self.stems, no_ext.rsplit("/", 1)[-1], no_ext)
        old_title = self._title_by_rel.pop(no_ext, None)
        if old_title is not None:
            _discard_from_list(self.titles, old_title, no_ext)

    @staticmethod
    def _read_title_lower(abs_path: Path) -> str | None:
        try:
            text = abs_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        fm, body, _ = parse_frontmatter(text)
        title = resolve_display_title(fm, body, abs_path)
        return title.lower() if title else None

    def on_files_changed(
        self,
        vault_root: Path,
        changed_rels: Iterable[str],
        deleted_rels: Iterable[str],
    ) -> None:
        """Patch this resolver in place for one batch of file changes.

        Mirrors `_InboundIndexData.on_files_changed`: drop every affected
        path's edges, then re-read + re-add path/stem/title for the changed
        paths that still exist on disk. The resulting maps equal a full
        rebuild's for the same on-disk state — so wikilink resolution (and thus
        the graph lane's 1-hop recall) is byte-for-byte unchanged; only the
        cost is (patch a handful of files vs. re-read + YAML-parse the whole
        vault). `*_rels` are vault-relative POSIX, with or without `.md`.

        A rel present in BOTH `changed_rels` and `deleted_rels` in the same
        batch (two path-string forms of one file collapsing to the same rel
        upstream — Windows 8.3 short names are the concrete vector, #126, but
        this defends against ANY dual-form vector: case aliasing, symlinks, a
        future one) is a same-batch conflict. Trust the filesystem to break
        the tie: a rel whose file still exists is a change, not a delete —
        dropping it would silently remove a live file from the resolver.
        """
        def _norm(rels: Iterable[str]) -> set[str]:
            out: set[str] = set()
            for r in rels:
                s = str(r).replace("\\", "/")
                if s.lower().endswith(".md"):
                    out.add(s[:-3])
            return out

        changed = _norm(changed_rels)
        deleted = _norm(deleted_rels)
        conflict = changed & deleted
        for no_ext in conflict:
            if (vault_root / (no_ext + ".md")).is_file():
                deleted.discard(no_ext)
            else:
                changed.discard(no_ext)
        if not (deleted or changed):
            return
        for no_ext in deleted | changed:
            self._remove_entry(no_ext)
        for no_ext in changed:
            abs_path = vault_root / (no_ext + ".md")
            if abs_path.is_file():
                self._add_entry(no_ext, self._read_title_lower(abs_path))

    def add_pending(self, no_ext_path: str, *, title: str | None = None) -> None:
        """Register a file the writer is about to create.

        Lets a same-batch reference (e.g. the source's back-ref to the new
        note's path) resolve before the file lands on disk.
        """
        no_ext = no_ext_path.removesuffix(".md").lstrip("/")
        self._add_entry(
            no_ext, title.strip().lower() if title and title.strip() else None
        )


def _strip_wikilink_brackets(s: str) -> str:
    """Strip `[[ ... ]]` wrappers and the trailing `|alias` if present."""
    s = s.strip()
    if s.startswith("[[") and s.endswith("]]"):
        s = s[2:-2].strip()
    return s


def obsidian_uses_kb_root(vault_root: Path) -> bool:
    """Whether Obsidian opens the managed KB directory as its vault root.

    Exomem's API paths stay vault-rooted (``Knowledge Base/...``). Markdown
    targets must instead be relative to the directory containing ``.obsidian``
    or Obsidian interprets the KB prefix as a nested folder.
    """
    return (kb_root(vault_root) / ".obsidian").is_dir()


def render_wikilink_target(target: str, vault_root: Path) -> str:
    """Render a canonical target for the detected Obsidian vault root."""
    if obsidian_uses_kb_root(vault_root) and target.startswith(kb_prefix()):
        return target.removeprefix(kb_prefix())
    return target


def render_wikilinks_for_vault(text: str, vault_root: Path) -> str:
    """Render canonical wikilinks in generated Markdown for this vault root.

    Unlike :func:`normalize_body_wikilinks`, this does not resolve targets. It
    only converts already-canonical ``Knowledge Base/...`` targets to their
    KB-relative display form when Obsidian opens the managed directory itself.
    """
    new_text = text
    for match in reversed(find_body_wikilinks(text)):
        full = match.group(0)
        inner = full[2:-2]
        target, separator, alias = inner.partition("|")
        rendered = render_wikilink_target(target.strip(), vault_root)
        if rendered == target.strip():
            continue
        replacement = f"[[{rendered}|{alias}]]" if separator else f"[[{rendered}]]"
        new_text = new_text[: match.start()] + replacement + new_text[match.end() :]
    return new_text


def normalize_wikilink(
    target: str,
    vault_root: Path,
    *,
    resolver: WikilinkResolver | None = None,
    strict: bool = False,
) -> tuple[str, str | None]:
    """Canonicalize a wikilink target to full vault-rooted form (no `.md`).

    Accepts any input form: bare, KB-relative, full vault-rooted, with or
    without `.md`, with or without `[[ ]]` wrappers, with or without
    `|alias`, with optional `#anchor`. The returned form is always
    `Knowledge Base/<rest>` (or a read-only sibling tree like `Reference/<rest>`)
    with `.md` stripped and `#anchor` preserved.

    Returns `(canonical, warning_or_none)`. On unresolvable target:
    - `strict=True`: raises `UnresolvedWikilinkError` (or
      `AmbiguousWikilinkError` for bare names with multiple matches).
    - `strict=False`: returns the cleaned input + a warning string. The
      caller can choose to surface the warning and leave the link as a
      forward reference, or to abort.
    """
    if resolver is None:
        resolver = WikilinkResolver(vault_root)

    cleaned = _strip_wikilink_brackets(target)
    if "|" in cleaned:
        cleaned = cleaned.split("|", 1)[0].strip()
    # Preserve #anchor across normalization.
    anchor = ""
    if "#" in cleaned:
        cleaned, anchor_part = cleaned.split("#", 1)
        anchor = "#" + anchor_part
        cleaned = cleaned.rstrip()
    cleaned = cleaned.removesuffix(".md").strip().strip("/")
    if not cleaned:
        if strict:
            raise UnresolvedWikilinkError(f"empty wikilink target: {target!r}")
        return "", f"empty wikilink target: {target!r}"

    # Folder-hub link (e.g. `[[Knowledge Base/Notes/Patterns/]]`): we never
    # canonicalize beyond ensuring the Knowledge Base/ prefix.
    if cleaned.endswith("/"):
        canonical = (
            cleaned if cleaned.startswith(kb_prefix())
            else kb_prefix() + cleaned
        )
        return canonical + anchor, None

    # 1. Full vault-rooted (with or without explicit Knowledge Base/ prefix).
    if cleaned in resolver.full_paths:
        return cleaned + anchor, None
    if not cleaned.startswith(kb_prefix()):
        candidate = kb_prefix() + cleaned
        if candidate in resolver.full_paths:
            return candidate + anchor, None

    # 2. KB-stripped match (target looks like KB-relative).
    if cleaned in resolver.kb_stripped:
        return kb_prefix() + cleaned + anchor, None

    # 3. Bare name (no `/`): stem match first, then frontmatter title.
    if "/" not in cleaned:
        stem_matches = resolver.stems.get(cleaned)
        if stem_matches:
            if len(stem_matches) == 1:
                return stem_matches[0] + anchor, None
            if strict:
                raise AmbiguousWikilinkError(
                    f"bare wikilink {target!r} resolves to "
                    f"{len(stem_matches)} files: {stem_matches}"
                )
            return cleaned + anchor, (
                f"bare wikilink {target!r} matches {len(stem_matches)} files "
                f"by stem; left unchanged. Files: {stem_matches}"
            )
        title_matches = resolver.titles.get(cleaned.lower())
        if title_matches:
            if len(title_matches) == 1:
                return title_matches[0] + anchor, None
            if strict:
                raise AmbiguousWikilinkError(
                    f"wikilink {target!r} matches {len(title_matches)} "
                    f"files by frontmatter title: {title_matches}"
                )
            return cleaned + anchor, (
                f"wikilink {target!r} matches {len(title_matches)} files "
                f"by title; left unchanged. Files: {title_matches}"
            )

    # Unresolvable — forward reference or genuinely missing target. Return
    # a sensible fallback canonical form so callers can use the result
    # directly without prefix manipulation:
    # - already starts with `Knowledge Base/` → keep
    # - already starts with a read-only sibling tree (per _access.yaml) → keep
    # - has a path separator → promote to `Knowledge Base/<rest>`
    # - bare name → leave as-is (audit's bare-name lookup will try later)
    if strict:
        raise UnresolvedWikilinkError(
            f"wikilink {target!r} does not resolve to any file in the vault"
        )
    if cleaned.startswith(kb_prefix()):
        fallback = cleaned
    elif "/" in cleaned and _is_curated_top_level(vault_root, cleaned.split("/", 1)[0]):
        fallback = cleaned
    elif "/" in cleaned:
        fallback = kb_prefix() + cleaned
    else:
        fallback = cleaned
    return fallback + anchor, (
        f"wikilink {target!r} does not resolve to any file in the vault"
    )


def _mask_code_spans(text: str) -> str:
    """Replace code-block and inline-code regions with spaces, preserving offsets.

    Result is the same length as input; positions of non-code characters are
    unchanged. Used so wikilink scanners can ignore `[[X]]` inside code while
    still reporting accurate offsets into the original text.
    """
    out = list(text)
    # Fenced code blocks (``` or ~~~), allowing up to 3 leading spaces per CommonMark.
    fence_open = re.compile(r"^( {0,3})(`{3,}|~{3,})[^\n]*$", re.MULTILINE)
    pos = 0
    while True:
        m = fence_open.search(text, pos)
        if not m:
            break
        fence = m.group(2)
        char = fence[0]
        length = len(fence)
        close_re = re.compile(
            rf"^ {{0,3}}{re.escape(char)}{{{length},}}\s*$",
            re.MULTILINE,
        )
        close_m = close_re.search(text, m.end())
        end = close_m.end() if close_m else len(text)
        for i in range(m.start(), end):
            if text[i] != "\n":
                out[i] = " "
        pos = end
    # Inline code: single-line backtick-delimited spans.
    inline_re = re.compile(r"(`+)([^\n`]+?)\1")
    masked_str = "".join(out)
    for m in inline_re.finditer(masked_str):
        for i in range(m.start(), m.end()):
            if out[i] != "\n":
                out[i] = " "
    return "".join(out)


def find_body_wikilinks(text: str) -> list[re.Match[str]]:
    """Return wikilink matches in `text`, skipping fenced code + inline code."""
    masked = _mask_code_spans(text)
    return list(_WIKILINK_PATTERN.finditer(masked))


def normalize_body_wikilinks(
    body: str,
    vault_root: Path,
    *,
    resolver: WikilinkResolver | None = None,
) -> tuple[str, list[str]]:
    """Rewrite every `[[X]]` to the preferred Obsidian-visible form.

    Preserves `[[X|alias]]` aliases. Skips matches inside fenced code blocks
    and inline code spans. Internal resolution remains canonical vault-rooted;
    emitted Markdown is KB-relative when ``Knowledge Base/.obsidian`` marks the
    managed directory as the Obsidian vault root. Returns `(new_body, warnings)`.
    Unresolvable links are left as-is with a warning — forward references are
    intentional.
    """
    if resolver is None:
        resolver = WikilinkResolver(vault_root)
    warnings: list[str] = []
    matches = find_body_wikilinks(body)
    new_body = body
    # Walk back-to-front so earlier rewrites don't shift later positions.
    # _WIKILINK_PATTERN's group(1) is the target without the alias (the alias
    # is consumed by a non-capturing branch), so we parse the full match
    # text to recover the alias.
    for m in reversed(matches):
        full = m.group(0)  # '[[target]]' or '[[target|alias]]'
        inner = full[2:-2]
        alias: str | None = None
        if "|" in inner:
            target_only, alias_part = inner.split("|", 1)
            target_only = target_only.strip()
            alias = alias_part.strip() or None
        else:
            target_only = inner.strip()
        canonical, warning = normalize_wikilink(
            target_only, vault_root, resolver=resolver, strict=False
        )
        if warning:
            warnings.append(warning)
            continue
        rendered = render_wikilink_target(canonical, vault_root)
        if rendered == target_only:
            continue  # already canonical
        replacement = (
            f"[[{rendered}|{alias}]]" if alias is not None else f"[[{rendered}]]"
        )
        new_body = new_body[: m.start()] + replacement + new_body[m.end():]
    return new_body, warnings


# ---------------- log helpers ----------------


_LOG_WIKILINK_RE = re.compile(r"!?\[\[(.+?)\]\]")


def escape_wikilinks_for_log(text: str) -> str:
    """Neutralize wikilink syntax in free text bound for log.md.

    Rationale strings (`why`, descriptions) are interpolated verbatim into
    log.md entries. A literal `[[target]]` there becomes a live wikilink the
    broken_wikilink audit then re-flags — a self-inflicted drift class. Render
    any `[[...]]` / `![[...]]` as backticked code so it stays inert while the
    referenced text is preserved.
    """
    return _LOG_WIKILINK_RE.sub(lambda m: f"`{m.group(1)}`", text)


def prepend_log_entry(
    log_text: str,
    *,
    date_iso: str,
    op: str,
    rel_path_no_ext: str,
    body: str,
) -> str:
    """Insert a `## [date] <op> | <rel>` block after the log's `---` separator.

    `rel_path_no_ext` is vault-relative POSIX without `.md`. The leading
    `Knowledge Base/` is stripped from the title for compactness (matches
    the existing add/edit/preserve log style); paths outside KB keep the
    full vault-relative form so curated-tree writes stay traceable.
    """
    title = rel_path_no_ext
    if title.startswith(kb_prefix()):
        title = title[len(kb_prefix()):]
    new_entry = f"## [{date_iso}] {op} | {title}\n\n{escape_wikilinks_for_log(body)}\n"
    # Reuse the same separator the indexes module emits.
    separator = "\n---\n"
    sep_idx = log_text.find(separator)
    if sep_idx == -1:
        return log_text.rstrip() + "\n\n" + new_entry + "\n"
    insertion_point = sep_idx + len(separator)
    return log_text[:insertion_point] + "\n" + new_entry + "\n" + log_text[insertion_point:]


# ---- log.md rotation (scale-proper activity log) ---------------------------

LOG_ROTATE_BYTES_DEFAULT = 2_000_000  # rotate when the live log exceeds ~2MB
LOG_ROTATE_KEEP_ENTRIES = 200  # newest entries kept live (>= index.md's cap-50)

_LOG_ENTRY_START_RE = re.compile(r"^## \[", re.MULTILINE)


def _log_rotate_bytes() -> int:
    raw = os.environ.get("EXOMEM_LOG_ROTATE_BYTES")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return LOG_ROTATE_BYTES_DEFAULT


def rotate_log_if_needed(vault_root: Path) -> str | None:
    """Size-triggered rotation of `Knowledge Base/log.md`.

    Every write op reads + rewrites log.md WHOLE (append-only feed, newest
    first), so an unbounded log makes every write O(log size). Past
    `EXOMEM_LOG_ROTATE_BYTES` (default 2MB) the tail beyond the newest
    `LOG_ROTATE_KEEP_ENTRIES` entries moves — byte-exact — to
    `Knowledge Base/_archive/logs/log-<utc-stamp>.md`. `_archive/` is excluded
    from find/index walks AND from every incremental index path (the
    exclusion-parity guard), so archives are inert; nothing is ever deleted.
    Keeping the newest 200 entries preserves index.md's cap-50
    Recent-activity derivation and recent `get(include_history)` reads; older
    history lives on in the archive files.

    Returns a one-line note when rotation ran (callers may surface it), None
    otherwise. Best-effort by contract: any failure logs and leaves log.md
    untouched — rotation must never break the write that triggered it.
    """
    log_file = kb_root(vault_root) / "log.md"
    try:
        if not log_file.exists() or log_file.stat().st_size <= _log_rotate_bytes():
            return None
        text = log_file.read_text(encoding="utf-8")
        sep = "\n---\n"  # == indexes.LOG_SEPARATOR (header/entries boundary)
        sep_idx = text.find(sep)
        if sep_idx == -1:
            return None  # unrecognized shape — never rotate what we can't parse
        head_end = sep_idx + len(sep)
        head, entries_text = text[:head_end], text[head_end:]
        starts = [m.start() for m in _LOG_ENTRY_START_RE.finditer(entries_text)]
        if len(starts) <= LOG_ROTATE_KEEP_ENTRIES:
            return None  # entry-count floor reached; size is as small as it gets
        cut = starts[LOG_ROTATE_KEEP_ENTRIES]
        live_entries, tail = entries_text[:cut], entries_text[cut:]
        stamp = _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        archive_dir = kb_root(vault_root) / "_archive" / "logs"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / f"log-{stamp}.md"
        n = 2
        while archive_path.exists():  # same-second rotations must not clobber
            archive_path = archive_dir / f"log-{stamp}-{n}.md"
            n += 1
        n_moved = len(starts) - LOG_ROTATE_KEEP_ENTRIES
        archive_text = (
            f"# log.md archive segment ({stamp})\n\n"
            f"Rotated out of `{kb_prefix()}log.md` — {n_moved} entrie(s), newest "
            f"first, byte-exact.\n{sep}{tail}"
        )
        batch_atomic_write([
            PlannedWrite(path=archive_path, content=archive_text),
            PlannedWrite(path=log_file, content=head + live_entries),
        ])
        rel_archive = archive_path.resolve().relative_to(vault_root.resolve()).as_posix()
        log.info("log.md rotated: %d entrie(s) -> %s", n_moved, rel_archive)
        return f"log.md rotated: {n_moved} older entrie(s) → {rel_archive}"
    except Exception as e:  # noqa: BLE001 — rotation must never break a write
        log.warning("log rotation skipped (%s)", e)
        return None


def write_log_entry(
    vault_root: Path,
    *,
    date_iso: str,
    op: str,
    rel_path_no_ext: str,
    body: str,
) -> str | None:
    """Read, update, and write log.md in one go. Returns warning if missing.

    Returns None on success; a warning string if log.md was missing (so the
    op can include it in its warnings list). Atomic via `replace`.
    """
    log_file = kb_root(vault_root) / "log.md"
    if not log_file.exists():
        return f"{kb_prefix()}log.md missing; skipped log entry"
    text = log_file.read_text(encoding="utf-8")
    new_text = prepend_log_entry(
        text,
        date_iso=date_iso,
        op=op,
        rel_path_no_ext=rel_path_no_ext,
        body=body,
    )
    batch_atomic_write([PlannedWrite(path=log_file, content=new_text)])
    rotate_log_if_needed(vault_root)  # size cap; best-effort, logs its own action
    return None


# Matches a single log.md entry header: `## [2026-06-23] edit | Notes/Insights/foo`.
# `op` is a single whitespace-free token; the title runs to end-of-line.
_LOG_ENTRY_HEADER_RE = re.compile(
    r"^## \[(\d{4}-\d{2}-\d{2})\] (\S+) \| (.+)$",
    re.MULTILINE,
)


def read_log_entries(vault_root: Path, rel_path_no_ext: str) -> list[dict[str, str]]:
    """Return the `log.md` change entries for one page, newest-first.

    The inverse of `prepend_log_entry`: it parses the append-only activity log
    and returns the `why`/rationale history for a single page so a reader can
    verify *why* a note changed. Title matching mirrors how writers record the
    entry (`prepend_log_entry`): a leading `Knowledge Base/` is stripped and the
    `.md` extension dropped. Entries are stored newest-first (prepended), so file
    order is preserved.

    Missing `log.md`, or no matching entries, returns `[]` — never an error;
    surfacing history is best-effort. Each entry is
    ``{"date": "2026-06-23", "op": "edit", "summary": "<rationale + what changed>"}``.
    """
    title = rel_path_no_ext
    if title.endswith(".md"):
        title = title[: -len(".md")]
    if title.startswith(kb_prefix()):
        title = title[len(kb_prefix()):]

    log_file = kb_root(vault_root) / "log.md"
    if not log_file.exists():
        return []
    text = log_file.read_text(encoding="utf-8")

    matches = list(_LOG_ENTRY_HEADER_RE.finditer(text))
    entries: list[dict[str, str]] = []
    for i, m in enumerate(matches):
        if m.group(3).strip() != title:
            continue
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        entries.append({
            "date": m.group(1),
            "op": m.group(2),
            "summary": text[body_start:body_end].strip(),
        })
    return entries
