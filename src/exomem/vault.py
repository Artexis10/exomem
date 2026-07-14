"""Vault path resolution + safe-write helpers used by the add tool.

Also hosts the Tier 2 shared helpers — curated/append-only tree guards,
generic path resolution, frontmatter parse/serialize, inbound-wikilink
scan — used by the filesystem-parity operations (create_file,
list_directory, etc.).
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
import time
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Literal

import yaml
from slugify import slugify as _slugify

from . import freshness
from .kbdir import kb_dirname, kb_prefix

if os.name == "nt":  # pragma: no cover - imported only on Windows
    import msvcrt
else:  # pragma: no cover - platform branch exercised on POSIX
    import fcntl

_SUPPORTS_DIRECTORY_FD = bool(
    os.open in getattr(os, "supports_dir_fd", set())
    and os.mkdir in getattr(os, "supports_dir_fd", set())
)

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
EXCLUDED_FRONTMATTER_FIELDS: frozenset[str] = frozenset({"confidence", "decay_at", "expires_at"})


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
VAULT_SCAN_SKIP_DIRS = frozenset(
    {
        ".obsidian",
        ".git",
        ".trash",
        "_attachments",
        "_archive",
        "_trash",
        "_Schema",
    }
)


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
    return any(seg in VAULT_SCAN_SKIP_DIRS for seg in rel_path.replace("\\", "/").split("/"))


# `[[Target]]` or `[[Target|Alias]]`.
_WIKILINK_PATTERN = re.compile(r"\[\[([^\]\|\n]+?)(?:\|[^\]\n]*)?\]\]")
_FM_PATTERN = re.compile(r"^---\n(.*?)\n---\n?(.*)", re.DOTALL)
_LOCK_NAMESPACES = frozenset({"activation-manifest", "semantic-creation"})
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_HELD_LOCKS = threading.local()


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
                "slug must be lowercase ASCII kebab-case (letters, digits, and single hyphens only)"
            )
        if len(slug) > SLUG_MAX_LENGTH:
            raise InvalidSlugError(f"slug exceeds the {SLUG_MAX_LENGTH}-character filename limit")
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
class VaultLockError(ValueError):
    code: str
    reason: str

    def __post_init__(self) -> None:
        ValueError.__init__(self, f"{self.code}: {self.reason}")


class VaultLockTimeout(VaultLockError):
    pass


def _lock_key(vault_root: Path, namespace: str) -> tuple[str, str]:
    if namespace not in _LOCK_NAMESPACES:
        raise VaultLockError("VAULT_LOCK_NAMESPACE", "unsupported vault lock namespace")
    try:
        root = str(Path(vault_root).resolve(strict=True))
    except OSError as error:
        raise VaultLockError("VAULT_LOCK_ROOT", "vault root is not safely resolvable") from error
    return root, hashlib.sha256(f"{root}\0{namespace}".encode()).hexdigest()


def _private_lock_directory() -> Path:
    owner = os.getuid() if hasattr(os, "getuid") else None
    suffix = str(owner) if owner is not None else os.environ.get("USERNAME", "user")
    directory = Path(tempfile.gettempdir()).resolve() / f"exomem-locks-{suffix}"
    try:
        info = directory.lstat()
    except FileNotFoundError:
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = directory.lstat()
    except OSError as error:
        raise VaultLockError("VAULT_LOCK_DIRECTORY", "lock directory is unreadable") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or _is_reparse(info):
        raise VaultLockError("VAULT_LOCK_DIRECTORY", "lock directory is unsafe")
    if owner is not None:
        if info.st_uid != owner or stat.S_IMODE(info.st_mode) != 0o700:
            raise VaultLockError(
                "VAULT_LOCK_DIRECTORY",
                "lock directory must be private and owned by the current user",
            )
    return directory


class _InterprocessFileLock:
    def __init__(self, path: Path, *, deadline: float):
        self.path = path
        self.deadline = deadline
        self._handle: BinaryIO | None = None

    def __enter__(self) -> _InterprocessFileLock:
        while True:
            try:
                handle = self.path.open("a+b")
            except OSError as error:
                raise VaultLockError("VAULT_LOCK_IO", "could not open vault lock") from error
            try:
                if os.name == "nt":  # pragma: no cover - Windows deployment
                    handle.seek(0)
                    if not handle.read(1):
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                handle.close()
                if error.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise VaultLockError("VAULT_LOCK_IO", "could not acquire vault lock") from error
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise VaultLockTimeout(
                        "VAULT_LOCK_TIMEOUT", "timed out acquiring vault lock"
                    ) from error
                time.sleep(min(0.01, remaining))
                continue
            self._handle = handle
            return self

    def __exit__(self, *_: object) -> None:
        if self._handle is None:
            return
        if os.name == "nt":  # pragma: no cover - Windows deployment
            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


@contextmanager
def vault_creation_lock(
    vault_root: Path,
    namespace: Literal["activation-manifest", "semantic-creation"],
    *,
    timeout: float = 30.0,
):
    """Serialize one vault-scoped creation namespace under one shared deadline."""
    if type(timeout) not in {int, float} or isinstance(timeout, bool) or timeout < 0:
        raise VaultLockError("VAULT_LOCK_TIMEOUT_VALUE", "lock timeout must be nonnegative")
    root, digest = _lock_key(Path(vault_root), namespace)
    held = getattr(_HELD_LOCKS, "keys", set())
    if held:
        raise VaultLockError("VAULT_LOCK_NESTED", "nested vault creation locks are forbidden")
    key = f"{root}\0{namespace}"
    with _THREAD_LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(key, threading.Lock())
    deadline = time.monotonic() + float(timeout)
    remaining = max(0.0, deadline - time.monotonic())
    if not thread_lock.acquire(timeout=remaining):
        raise VaultLockTimeout("VAULT_LOCK_TIMEOUT", "timed out acquiring vault lock")
    _HELD_LOCKS.keys = {key}
    try:
        lock_path = _private_lock_directory() / f"{digest}.lock"
        with _InterprocessFileLock(lock_path, deadline=deadline):
            yield lock_path
    finally:
        _HELD_LOCKS.keys = set()
        thread_lock.release()


@dataclass
class CreateOnlyConflict(ValueError):
    target: str
    code: str = "CREATE_ONLY_CONFLICT"

    def __post_init__(self) -> None:
        ValueError.__init__(self, f"{self.code}: {self.target}")


@dataclass
class PathGuardError(ValueError):
    code: str
    reason: str

    def __post_init__(self) -> None:
        ValueError.__init__(self, f"{self.code}: {self.reason}")


@dataclass(frozen=True, slots=True)
class PathIdentity:
    relative_path: str
    device: int | None
    inode: int | None
    mode: int


def _is_reparse(info: os.stat_result) -> bool:
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(getattr(info, "st_file_attributes", 0) & marker)


def _identity(relative_path: str, info: os.stat_result) -> PathIdentity:
    return PathIdentity(
        relative_path,
        getattr(info, "st_dev", None),
        getattr(info, "st_ino", None),
        info.st_mode,
    )


def _same_identity(expected: PathIdentity, actual: os.stat_result) -> bool:
    return (
        expected.device == getattr(actual, "st_dev", None)
        and expected.inode == getattr(actual, "st_ino", None)
        and expected.mode == actual.st_mode
    )


def _safe_guard_target(target: str) -> tuple[str, ...]:
    if type(target) is not str or not target or "\\" in target or "\0" in target:
        raise PathGuardError("PATH_GUARD_INVALID", "guard target must be a safe relative path")
    posix = Path(target)
    parts = tuple(target.split("/"))
    if posix.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise PathGuardError("PATH_GUARD_INVALID", "guard target must be a safe relative path")
    if re.match(r"^[A-Za-z]:", target):
        raise PathGuardError("PATH_GUARD_INVALID", "guard target must be a safe relative path")
    return parts


def _leaf_hash(path: Path, expected: PathIdentity) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise PathGuardError("PATH_GUARD_IO", "guarded content could not be opened") from error
    digest = hashlib.sha256()
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not _same_identity(expected, info):
            raise PathGuardError("PATH_GUARD_CHANGED", "guarded content identity changed")
        while chunk := os.read(descriptor, 65536):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    try:
        current = path.lstat()
    except OSError as error:
        raise PathGuardError("PATH_GUARD_CHANGED", "guarded content identity changed") from error
    if not _same_identity(expected, current):
        raise PathGuardError("PATH_GUARD_CHANGED", "guarded content identity changed")
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class PathGuard:
    target: str
    ancestors: tuple[PathIdentity, ...]
    missing_parents: tuple[str, ...]
    leaf_identity: PathIdentity | None
    leaf_policy: Literal["absent", "stable", "content"]
    expected_content_hash: str | None

    @classmethod
    def capture(
        cls,
        vault_root: Path,
        target: str,
        *,
        leaf_policy: Literal["absent", "stable", "content"],
        expected_content_hash: str | None = None,
    ) -> PathGuard:
        parts = _safe_guard_target(target)
        if leaf_policy not in {"absent", "stable", "content"}:
            raise PathGuardError("PATH_GUARD_INVALID", "unsupported leaf policy")
        if leaf_policy == "content" and not re.fullmatch(
            r"[0-9a-f]{64}", expected_content_hash or ""
        ):
            raise PathGuardError("PATH_GUARD_INVALID", "content guard requires a lowercase SHA-256")
        if leaf_policy != "content" and expected_content_hash is not None:
            raise PathGuardError("PATH_GUARD_INVALID", "content hash requires content leaf policy")
        root = Path(vault_root)
        try:
            root_info = root.lstat()
        except OSError as error:
            raise PathGuardError("PATH_GUARD_ROOT", "vault root is unavailable") from error
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or stat.S_ISLNK(root_info.st_mode)
            or _is_reparse(root_info)
        ):
            raise PathGuardError("PATH_GUARD_ROOT", "vault root is unsafe")
        ancestors = [_identity(".", root_info)]
        parent = root
        missing: list[str] = []
        for index, part in enumerate(parts[:-1]):
            parent /= part
            relative = "/".join(parts[: index + 1])
            if missing:
                missing.append(relative)
                continue
            try:
                info = parent.lstat()
            except FileNotFoundError:
                missing.append(relative)
                continue
            except OSError as error:
                raise PathGuardError("PATH_GUARD_IO", "guard ancestor is unreadable") from error
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or _is_reparse(info):
                raise PathGuardError("PATH_GUARD_UNSAFE", "guard ancestor is unsafe")
            ancestors.append(_identity(relative, info))
        leaf = root.joinpath(*parts)
        try:
            leaf_info = leaf.lstat()
        except FileNotFoundError:
            leaf_info = None
        except OSError as error:
            raise PathGuardError("PATH_GUARD_IO", "guard leaf is unreadable") from error
        if leaf_info is not None and (
            stat.S_ISLNK(leaf_info.st_mode)
            or _is_reparse(leaf_info)
            or not stat.S_ISREG(leaf_info.st_mode)
        ):
            raise PathGuardError("PATH_GUARD_UNSAFE", "guard leaf is unsafe")
        if leaf_policy == "absent" and leaf_info is not None:
            raise PathGuardError("PATH_GUARD_CHANGED", "guarded leaf must be absent")
        if leaf_policy in {"stable", "content"} and leaf_info is None:
            raise PathGuardError("PATH_GUARD_CHANGED", "guarded leaf must exist")
        guard = cls(
            target,
            tuple(ancestors),
            tuple(missing),
            _identity(target, leaf_info) if leaf_info is not None else None,
            leaf_policy,
            expected_content_hash,
        )
        guard.recheck(root)
        return guard

    def prepare_and_bind_parents(self, vault_root: Path) -> PathGuard:
        self.recheck(vault_root)
        root = Path(vault_root)
        _create_missing_guard_parents(
            root,
            self.missing_parents,
            expected_ancestors=self.ancestors,
        )
        return PathGuard.capture(
            root,
            self.target,
            leaf_policy=self.leaf_policy,
            expected_content_hash=self.expected_content_hash,
        )

    def recheck(self, vault_root: Path) -> None:
        root = Path(vault_root)
        for expected in self.ancestors:
            path = root if expected.relative_path == "." else root / expected.relative_path
            try:
                info = path.lstat()
            except OSError as error:
                raise PathGuardError("PATH_GUARD_CHANGED", "guard ancestor changed") from error
            if (
                not _same_identity(expected, info)
                or not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or _is_reparse(info)
            ):
                raise PathGuardError("PATH_GUARD_CHANGED", "guard ancestor changed")
        for relative in self.missing_parents:
            if os.path.lexists(root / relative):
                raise PathGuardError("PATH_GUARD_CHANGED", "missing guard ancestor appeared")
        leaf = root / self.target
        exists = os.path.lexists(leaf)
        if self.leaf_policy == "absent":
            if exists:
                raise PathGuardError("PATH_GUARD_CHANGED", "guarded leaf appeared")
            return
        if not exists or self.leaf_identity is None:
            raise PathGuardError("PATH_GUARD_CHANGED", "guarded leaf disappeared")
        try:
            info = leaf.lstat()
        except OSError as error:
            raise PathGuardError("PATH_GUARD_CHANGED", "guarded leaf changed") from error
        if (
            not _same_identity(self.leaf_identity, info)
            or not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or _is_reparse(info)
        ):
            raise PathGuardError("PATH_GUARD_CHANGED", "guarded leaf changed")
        if (
            self.leaf_policy == "content"
            and _leaf_hash(leaf, self.leaf_identity) != self.expected_content_hash
        ):
            raise PathGuardError("PATH_GUARD_CONTENT", "guarded content changed")


def _same_captured_identity(first: PathIdentity, second: PathIdentity) -> bool:
    return (
        first.device == second.device
        and first.inode == second.inode
        and first.mode == second.mode
    )


@dataclass(frozen=True, slots=True)
class _BatchArtifactGuard:
    """Bind one batch-owned file to its parent, identity, and exact bytes."""

    root: Path
    guard: PathGuard

    @property
    def path(self) -> Path:
        return self.root / self.guard.target

    @property
    def identity(self) -> PathIdentity:
        identity = self.guard.leaf_identity
        if identity is None:  # pragma: no cover - content guards always bind a leaf
            raise PathGuardError("PATH_GUARD_CHANGED", "batch artifact disappeared")
        return identity

    @property
    def content_hash(self) -> str:
        digest = self.guard.expected_content_hash
        if digest is None:  # pragma: no cover - content guards always bind a hash
            raise PathGuardError("PATH_GUARD_CONTENT", "batch artifact hash is unavailable")
        return digest

    @classmethod
    def capture(
        cls,
        path: Path,
        *,
        expected_content_hash: str | None = None,
        expected_identity: PathIdentity | None = None,
    ) -> _BatchArtifactGuard:
        absolute = Path(os.path.abspath(path))
        root = absolute.parent
        if expected_content_hash is None:
            stable = PathGuard.capture(root, absolute.name, leaf_policy="stable")
            identity = stable.leaf_identity
            if identity is None:  # pragma: no cover - stable capture requires a leaf
                raise PathGuardError("PATH_GUARD_CHANGED", "batch artifact disappeared")
            expected_content_hash = _leaf_hash(absolute, identity)
            stable.recheck(root)
            expected_identity = identity
        guard = PathGuard.capture(
            root,
            absolute.name,
            leaf_policy="content",
            expected_content_hash=expected_content_hash,
        )
        identity = guard.leaf_identity
        if identity is None or (
            expected_identity is not None
            and not _same_captured_identity(expected_identity, identity)
        ):
            raise PathGuardError("PATH_GUARD_CHANGED", "batch artifact identity changed")
        artifact = cls(root, guard)
        artifact.recheck()
        return artifact

    def recheck(self) -> None:
        self.guard.recheck(self.root)


def _bounded_directory_entries(
    path: Path,
    *,
    relative: str,
    expected: PathIdentity,
    max_entries: int,
    ignored_names: frozenset[str] = frozenset(),
) -> tuple[PathIdentity, ...]:
    """Capture a bounded descriptor-relative directory census."""
    try:
        descriptor = os.open(path, _directory_flags())
    except OSError as error:
        raise PathGuardError("PATH_GUARD_CHANGED", "guarded directory changed") from error
    iterator = None
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode) or not _same_identity(expected, opened):
            raise PathGuardError("PATH_GUARD_CHANGED", "guarded directory changed")
        descriptor_relative = os.scandir in getattr(os, "supports_fd", set())
        iterator = os.scandir(descriptor if descriptor_relative else path)
        entries: list[PathIdentity] = []
        for entry in iterator:
            name = entry.name
            try:
                encoded = name.encode("utf-8")
            except UnicodeEncodeError as error:
                raise PathGuardError(
                    "PATH_GUARD_UNSAFE", "guarded directory entry is unsafe"
                ) from error
            if not name or name in {".", ".."} or "/" in name or "\\" in name or b"\0" in encoded:
                raise PathGuardError(
                    "PATH_GUARD_UNSAFE", "guarded directory entry is unsafe"
                )
            if name in ignored_names:
                continue
            if len(entries) >= max_entries:
                raise PathGuardError(
                    "PATH_GUARD_LIMIT", "guarded directory exceeds its entry limit"
                )
            try:
                info = (
                    os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if descriptor_relative
                    else entry.stat(follow_symlinks=False)
                )
            except OSError as error:
                raise PathGuardError(
                    "PATH_GUARD_CHANGED", "guarded directory entry changed"
                ) from error
            entry_relative = f"{relative}/{name}"
            entries.append(_identity(entry_relative, info))
        if not _same_identity(expected, os.fstat(descriptor)):
            raise PathGuardError("PATH_GUARD_CHANGED", "guarded directory changed")
    finally:
        if iterator is not None:
            iterator.close()
        os.close(descriptor)
    return tuple(sorted(entries, key=lambda item: item.relative_path.encode("utf-8")))


@dataclass(frozen=True, slots=True)
class DirectoryCensusGuard:
    """Bind an absent directory or a bounded exact child census to commit time."""

    target: str
    ancestors: tuple[PathIdentity, ...]
    missing_paths: tuple[str, ...]
    directory_identity: PathIdentity | None
    entries: tuple[PathIdentity, ...]
    max_entries: int

    @classmethod
    def capture(
        cls,
        vault_root: Path,
        target: str,
        *,
        max_entries: int,
    ) -> DirectoryCensusGuard:
        parts = _safe_guard_target(target)
        if type(max_entries) is not int or max_entries < 0:
            raise PathGuardError("PATH_GUARD_INVALID", "directory entry limit is invalid")
        root = Path(vault_root)
        try:
            root_info = root.lstat()
        except OSError as error:
            raise PathGuardError("PATH_GUARD_ROOT", "vault root is unavailable") from error
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or stat.S_ISLNK(root_info.st_mode)
            or _is_reparse(root_info)
        ):
            raise PathGuardError("PATH_GUARD_ROOT", "vault root is unsafe")
        ancestors = [_identity(".", root_info)]
        current = root
        missing: list[str] = []
        for index, part in enumerate(parts):
            current /= part
            relative = "/".join(parts[: index + 1])
            if missing:
                missing.append(relative)
                continue
            try:
                info = current.lstat()
            except FileNotFoundError:
                missing.append(relative)
                continue
            except OSError as error:
                raise PathGuardError("PATH_GUARD_IO", "guard directory is unreadable") from error
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or _is_reparse(info):
                raise PathGuardError("PATH_GUARD_UNSAFE", "guard directory is unsafe")
            if index < len(parts) - 1:
                ancestors.append(_identity(relative, info))
                continue
            directory_identity = _identity(relative, info)
            entries = _bounded_directory_entries(
                current,
                relative=relative,
                expected=directory_identity,
                max_entries=max_entries,
            )
            guard = cls(
                target,
                tuple(ancestors),
                (),
                directory_identity,
                entries,
                max_entries,
            )
            guard.recheck(root)
            return guard
        guard = cls(target, tuple(ancestors), tuple(missing), None, (), max_entries)
        guard.recheck(root)
        return guard

    def recheck(
        self,
        vault_root: Path,
        *,
        allowed_changes: Iterable[Path] = (),
    ) -> None:
        root = Path(vault_root)
        for expected in self.ancestors:
            path = root if expected.relative_path == "." else root / expected.relative_path
            try:
                info = path.lstat()
            except OSError as error:
                raise PathGuardError("PATH_GUARD_CHANGED", "guard ancestor changed") from error
            if (
                not _same_identity(expected, info)
                or not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or _is_reparse(info)
            ):
                raise PathGuardError("PATH_GUARD_CHANGED", "guard ancestor changed")
        if self.directory_identity is None:
            for relative in self.missing_paths[:-1]:
                path = root / relative
                if not os.path.lexists(path):
                    return
                try:
                    info = path.lstat()
                except OSError as error:
                    raise PathGuardError(
                        "PATH_GUARD_CHANGED", "guarded directory ancestor changed"
                    ) from error
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or stat.S_ISLNK(info.st_mode)
                    or _is_reparse(info)
                ):
                    raise PathGuardError(
                        "PATH_GUARD_CHANGED", "guarded directory ancestor changed"
                    )
            if self.missing_paths and os.path.lexists(root / self.missing_paths[-1]):
                directory = root / self.target
                allowed_names = frozenset(
                    path.name
                    for path in allowed_changes
                    if os.path.abspath(path.parent) == os.path.abspath(directory)
                )
                if not allowed_names:
                    raise PathGuardError(
                        "PATH_GUARD_CHANGED", "guarded directory appeared"
                    )
                try:
                    info = directory.lstat()
                except OSError as error:
                    raise PathGuardError(
                        "PATH_GUARD_CHANGED", "guarded directory changed"
                    ) from error
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or stat.S_ISLNK(info.st_mode)
                    or _is_reparse(info)
                ):
                    raise PathGuardError(
                        "PATH_GUARD_CHANGED", "guarded directory changed"
                    )
                current = _bounded_directory_entries(
                    directory,
                    relative=self.target,
                    expected=_identity(self.target, info),
                    max_entries=self.max_entries,
                    ignored_names=allowed_names,
                )
                if current:
                    raise PathGuardError(
                        "PATH_GUARD_CHANGED", "guarded directory census changed"
                    )
            return
        directory = root / self.target
        try:
            info = directory.lstat()
        except OSError as error:
            raise PathGuardError("PATH_GUARD_CHANGED", "guarded directory changed") from error
        if (
            not _same_identity(self.directory_identity, info)
            or not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or _is_reparse(info)
        ):
            raise PathGuardError("PATH_GUARD_CHANGED", "guarded directory changed")
        allowed_names = frozenset(
            path.name
            for path in allowed_changes
            if os.path.abspath(path.parent) == os.path.abspath(directory)
        )
        current = _bounded_directory_entries(
            directory,
            relative=self.target,
            expected=self.directory_identity,
            max_entries=self.max_entries,
            ignored_names=allowed_names,
        )
        expected = tuple(
            entry
            for entry in self.entries
            if Path(entry.relative_path).name not in allowed_names
        )
        if current != expected:
            raise PathGuardError("PATH_GUARD_CHANGED", "guarded directory census changed")


def read_guarded_text(vault_root: Path, path: Path) -> tuple[str, PathGuard]:
    """Read UTF-8 text once and bind a guard to those exact source bytes."""
    root = Path(vault_root)
    absolute = Path(path)
    try:
        relative = absolute.relative_to(root).as_posix()
    except ValueError as error:
        raise PathGuardError(
            "PATH_GUARD_INVALID", "guarded read target is outside the vault"
        ) from error
    raw = absolute.read_bytes()
    text = raw.decode("utf-8")
    guard = PathGuard.capture(
        root,
        relative,
        leaf_policy="content",
        expected_content_hash=hashlib.sha256(raw).hexdigest(),
    )
    return text, guard


@dataclass
class PlannedWrite:
    """One target file in a batch write: destination path + final content."""

    path: Path
    content: str
    create_only: bool = False
    guard: PathGuard | None = None


def _safe_write_target(path: Path, vault_root: Path | None) -> str:
    if vault_root is None:
        return path.name
    try:
        return Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(vault_root))).as_posix()
    except ValueError:
        return path.name


def _prepare_path_guards(vault_root: Path, guards: Iterable[PathGuard]) -> tuple[PathGuard, ...]:
    original = tuple(guards)
    for guard in original:
        guard.recheck(vault_root)
    missing = sorted(
        {relative for guard in original for relative in guard.missing_parents},
        key=lambda value: (len(Path(value).parts), value),
    )
    _create_missing_guard_parents(
        vault_root,
        missing,
        expected_ancestors=tuple(identity for guard in original for identity in guard.ancestors),
    )
    return tuple(
        PathGuard.capture(
            vault_root,
            guard.target,
            leaf_policy=guard.leaf_policy,
            expected_content_hash=guard.expected_content_hash,
        )
        for guard in original
    )


def _directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _open_directory_at(
    parent_descriptor: int,
    name: str,
    *,
    expected: PathIdentity | None = None,
) -> int:
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_descriptor)
    except OSError as error:
        raise PathGuardError(
            "PATH_GUARD_CHANGED", "guard ancestor changed during creation"
        ) from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise PathGuardError("PATH_GUARD_UNSAFE", "guard ancestor is unsafe")
        if expected is not None and not _same_identity(expected, info):
            raise PathGuardError("PATH_GUARD_CHANGED", "guard ancestor changed during creation")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _create_missing_guard_parents(
    vault_root: Path,
    missing_parents: Iterable[str],
    *,
    expected_ancestors: Iterable[PathIdentity],
) -> None:
    missing = tuple(
        sorted(
            set(missing_parents),
            key=lambda value: (len(Path(value).parts), value),
        )
    )
    if not missing:
        return
    expected_by_path: dict[str, PathIdentity] = {}
    for identity in expected_ancestors:
        existing = expected_by_path.get(identity.relative_path)
        if existing is not None and existing != identity:
            raise PathGuardError("PATH_GUARD_CHANGED", "guard ancestors disagree")
        expected_by_path[identity.relative_path] = identity
    if not _SUPPORTS_DIRECTORY_FD:  # pragma: no cover - Windows fallback
        for relative in missing:
            path = vault_root / relative
            try:
                path.mkdir()
            except FileExistsError as error:
                raise PathGuardError(
                    "PATH_GUARD_CHANGED", "missing guard ancestor appeared"
                ) from error
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or _is_reparse(info):
                raise PathGuardError("PATH_GUARD_UNSAFE", "created guard ancestor is unsafe")
        return

    try:
        root_before = vault_root.lstat()
        root_descriptor = os.open(vault_root, _directory_flags())
    except OSError as error:
        raise PathGuardError(
            "PATH_GUARD_CHANGED", "vault root changed during parent creation"
        ) from error
    try:
        expected_root = expected_by_path.get(".", _identity(".", root_before))
        if not _same_identity(expected_root, os.fstat(root_descriptor)):
            raise PathGuardError("PATH_GUARD_CHANGED", "vault root changed during parent creation")
        for relative in missing:
            parts = Path(relative).parts
            parent_descriptor = os.dup(root_descriptor)
            try:
                for index, part in enumerate(parts[:-1]):
                    traversed = "/".join(parts[: index + 1])
                    next_descriptor = _open_directory_at(
                        parent_descriptor,
                        part,
                        expected=expected_by_path.get(traversed),
                    )
                    os.close(parent_descriptor)
                    parent_descriptor = next_descriptor
                try:
                    os.mkdir(parts[-1], dir_fd=parent_descriptor)
                except FileExistsError as error:
                    raise PathGuardError(
                        "PATH_GUARD_CHANGED", "missing guard ancestor appeared"
                    ) from error
                created_descriptor = _open_directory_at(parent_descriptor, parts[-1])
                expected_by_path[relative] = _identity(relative, os.fstat(created_descriptor))
                os.close(created_descriptor)
            finally:
                os.close(parent_descriptor)
    finally:
        os.close(root_descriptor)


def batch_atomic_write(
    writes: Iterable[PlannedWrite],
    *,
    vault_root: Path | None = None,
    required_guards: Iterable[PathGuard | DirectoryCensusGuard] = (),
    index_reports: list[Any] | None = None,
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
    still works, and `audit_fix(rebuild_embeddings=True)` recovers drift.  An
    opt-in ``index_reports`` collector receives the report from that same
    fan-out; requesting feedback never dispatches indexes a second time.
    """
    # Several high-level writers independently refresh the same navigation
    # file in one logical batch. Preserve the original destination order but
    # commit only the last planned content for each path.
    deduped: dict[Path, PlannedWrite] = {}
    for write in writes:
        deduped[write.path] = write
    writes = list(deduped.values())
    all_required_guards = tuple(required_guards)
    if any(
        not isinstance(guard, (PathGuard, DirectoryCensusGuard))
        for guard in all_required_guards
    ):
        raise PathGuardError("PATH_GUARD_INVALID", "unsupported required guard")
    read_only_guards = tuple(
        guard for guard in all_required_guards if isinstance(guard, PathGuard)
    )
    directory_guards = tuple(
        guard
        for guard in all_required_guards
        if isinstance(guard, DirectoryCensusGuard)
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
                raise ValueError(f"WRITE_REFUSED: {w.path} resolves outside the vault root") from e
            reason = access.writable_reason(vault_root, rel)
            if reason is not None:
                raise ValueError(f"WRITE_REFUSED: {rel}: {reason}")
    if (
        read_only_guards
        or directory_guards
        or any(write.guard is not None for write in writes)
    ) and vault_root is None:
        raise PathGuardError("PATH_GUARD_ROOT", "guarded writes require vault_root")
    bound_guards: list[PathGuard | None] = []
    if vault_root is not None:
        root = Path(vault_root)
        write_guards: list[PathGuard] = []
        guard_positions: list[int] = []
        for write in writes:
            guard = write.guard
            if guard is None:
                bound_guards.append(None)
                continue
            expected_path = root / guard.target
            if os.path.abspath(write.path) != os.path.abspath(expected_path):
                raise PathGuardError("PATH_GUARD_TARGET", "write path does not match guard target")
            guard_positions.append(len(bound_guards))
            write_guards.append(guard)
            bound_guards.append(None)
        prepared = _prepare_path_guards(root, (*write_guards, *read_only_guards))
        for position, guard in zip(guard_positions, prepared[: len(write_guards)], strict=True):
            bound_guards[position] = guard
        read_only_guards = prepared[len(write_guards) :]
        for guard in (*read_only_guards, *(item for item in bound_guards if item is not None)):
            guard.recheck(root)
        for guard in directory_guards:
            guard.recheck(root, allowed_changes=(write.path for write in writes))
    else:
        bound_guards = [None] * len(writes)
    for write in writes:
        if write.create_only and os.path.lexists(write.path):
            raise CreateOnlyConflict(_safe_write_target(write.path, vault_root))
    staged: list[tuple[Path, Path]] = []  # (final, tmp)
    staged_guards: dict[Path, _BatchArtifactGuard] = {}
    try:
        for w in writes:
            w.path.parent.mkdir(parents=True, exist_ok=True)
            # NamedTemporaryFile would need delete=False + cross-platform care;
            # explicit tmp sibling is simpler and survives os.replace.
            fd, tmp_str = tempfile.mkstemp(
                prefix=f".{w.path.name}.", suffix=".tmp", dir=str(w.path.parent)
            )
            os.close(fd)
            tmp = Path(tmp_str)
            staged.append((w.path, tmp))
            tmp.write_text(w.content, encoding="utf-8", newline="\n")
            planned_hash = hashlib.sha256(w.content.encode("utf-8")).hexdigest()
            staged_guards[tmp] = _BatchArtifactGuard.capture(
                tmp, expected_content_hash=planned_hash
            )
    except Exception:
        for _, tmp in staged:
            tmp.unlink(missing_ok=True)
        raise

    backups: dict[Path, Path | None] = {}
    backup_guards: dict[Path, _BatchArtifactGuard] = {}
    try:
        for final, _tmp in staged:
            if not os.path.lexists(final):
                backups[final] = None
                continue
            source_guard = _BatchArtifactGuard.capture(final)
            fd, backup_str = tempfile.mkstemp(
                prefix=f".{final.name}.", suffix=".bak", dir=str(final.parent)
            )
            os.close(fd)
            backup = Path(backup_str)
            backups[final] = backup
            shutil.copy2(final, backup)
            source_guard.recheck()
            backup_guards[final] = _BatchArtifactGuard.capture(
                backup,
                expected_content_hash=source_guard.content_hash,
            )
    except Exception:
        for _, tmp in staged:
            tmp.unlink(missing_ok=True)
        for backup in backups.values():
            if backup is not None:
                backup.unlink(missing_ok=True)
        raise

    replaced: list[Path] = []
    final_guards: dict[Path, _BatchArtifactGuard] = {}
    try:
        for index, (final, tmp) in enumerate(staged):
            for _, staged_tmp in staged[index:]:
                staged_guards[staged_tmp].recheck()
            for guard in backup_guards.values():
                guard.recheck()
            for guard in final_guards.values():
                guard.recheck()
            if vault_root is not None:
                root = Path(vault_root)
                for guard in read_only_guards:
                    guard.recheck(root)
                for guard in bound_guards[index:]:
                    if guard is not None:
                        guard.recheck(root)
                allowed_census_changes = (
                    *(write.path for write in writes),
                    *(
                        staged_guards[staged_tmp].path
                        for _, staged_tmp in staged[index:]
                    ),
                    *(guard.path for guard in backup_guards.values()),
                )
                for guard in directory_guards:
                    guard.recheck(root, allowed_changes=allowed_census_changes)
            if writes[index].create_only and os.path.lexists(final):
                raise CreateOnlyConflict(_safe_write_target(final, vault_root))
            staged_guard = staged_guards[tmp]
            staged_guard.recheck()
            os.replace(tmp, final)
            replaced.append(final)
            final_guards[final] = _BatchArtifactGuard.capture(
                final,
                expected_content_hash=staged_guard.content_hash,
                expected_identity=staged_guard.identity,
            )
        for guard in read_only_guards:
            guard.recheck(Path(vault_root))
        for guard in backup_guards.values():
            guard.recheck()
        for guard in final_guards.values():
            guard.recheck()
        if vault_root is not None:
            root = Path(vault_root)
            allowed_census_changes = (
                *(write.path for write in writes),
                *(guard.path for guard in backup_guards.values()),
            )
            for guard in directory_guards:
                guard.recheck(root, allowed_changes=allowed_census_changes)
        for guard in backup_guards.values():
            guard.recheck()
        for guard in final_guards.values():
            guard.recheck()
    except Exception as commit_error:
        rollback_errors: list[str] = []
        for final in reversed(replaced):
            backup = backups.get(final)
            try:
                final_guard = final_guards.get(final)
                if final_guard is None:
                    raise PathGuardError(
                        "PATH_GUARD_CHANGED", "committed batch artifact is unbound"
                    )
                final_guard.recheck()
                if backup is None:
                    final.unlink(missing_ok=True)
                    if os.path.lexists(final):
                        raise PathGuardError(
                            "PATH_GUARD_CHANGED", "committed batch artifact remains"
                        )
                else:
                    backup_guard = backup_guards[final]
                    backup_guard.recheck()
                    os.replace(backup, final)
                    _BatchArtifactGuard.capture(
                        final,
                        expected_content_hash=backup_guard.content_hash,
                        expected_identity=backup_guard.identity,
                    )
                    backups[final] = None
            except Exception as rollback_error:  # noqa: BLE001 - report every restore failure
                rollback_errors.append(f"{final}: {rollback_error}")
        replaced_paths = set(replaced)
        for final, tmp in staged:
            if final not in replaced_paths and tmp.exists():
                tmp.unlink(missing_ok=True)
        if rollback_errors:
            retained = [str(path) for path in backups.values() if path is not None]
            raise RuntimeError(
                f"batch commit failed ({commit_error}); rollback also failed: "
                + "; ".join(rollback_errors)
                + (f"; retained backups: {retained}" if retained else "")
            ) from commit_error
        for backup in backups.values():
            if backup is not None:
                backup.unlink(missing_ok=True)
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

            report = index_sync.upsert_after_write(vault_root, replaced)
            if index_reports is not None:
                index_reports.append(report)
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


class _DuplicateYamlKey(yaml.YAMLError):
    pass


class _UniqueKeySafeLoader(_YAML_SAFE_LOADER):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    seen: set[Any] = set()
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in seen
        except TypeError as error:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from error
        if duplicate:
            raise _DuplicateYamlKey("duplicate mapping key")
        seen.add(key)
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass
class FrontmatterError(ValueError):
    code: str
    reason: str

    def __post_init__(self) -> None:
        ValueError.__init__(self, f"{self.code}: {self.reason}")


def yaml_safe_load(text: str):
    """`yaml.safe_load` via libyaml when available (hot-path frontmatter seam).

    SAFETY: `_YAML_SAFE_LOADER` is CSafeLoader or SafeLoader — both the safe
    schema; `!!python/*` tags raise ConstructorError instead of constructing.
    Pinned by tests/test_yaml_loader_safety.py — do not widen the loader.
    """
    return yaml.load(text, Loader=_YAML_SAFE_LOADER)  # noqa: S506 — safe schema, see above


def parse_frontmatter(text: str, *, strict: bool = False) -> tuple[dict[str, Any], str, str | None]:
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
        if strict:
            fm = yaml.load(  # noqa: S506 - custom loader retains SafeLoader schema
                fm_text, Loader=_UniqueKeySafeLoader
            )
        else:
            fm = yaml_safe_load(fm_text)
        fm = fm or {}
        if not isinstance(fm, dict):
            if strict:
                raise FrontmatterError("INVALID_FRONTMATTER", "frontmatter root must be a mapping")
            fm = {}
    except _DuplicateYamlKey as error:
        if strict:
            raise FrontmatterError(
                "DUPLICATE_FRONTMATTER_KEY", "frontmatter contains a duplicate key"
            ) from error
        fm = {}
    except yaml.YAMLError as error:
        if strict:
            raise FrontmatterError(
                "INVALID_FRONTMATTER", "frontmatter is not valid safe YAML"
            ) from error
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
    path: str  # vault-relative POSIX of the file containing the link
    line_number: int  # 1-based
    context: str  # the line text (trimmed)
    raw_target: str  # the exact text inside [[...]]

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
    seq: int  # global scan order: (file walk order, line, match)
    path: str  # vault-relative POSIX of the file containing the link
    line_number: int
    context: str
    raw_target: str


@dataclass
class _InboundIndexData:
    buckets: dict[str, list[_InboundEntry]]  # normalized target -> entries
    stem_counts: dict[str, int]  # basename -> occurrences in walk
    known_rels: set[str]  # vault-relative POSIX paths already
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
                self.buckets.setdefault(normalized, []).append(
                    _InboundEntry(
                        seq=next_seq,
                        path=rel,
                        line_number=lineno,
                        context=context,
                        raw_target=raw,
                    )
                )
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
            buckets.setdefault(normalized, []).append(
                _InboundEntry(
                    seq=seq,
                    path=md_rel,
                    line_number=lineno,
                    context=context,
                    raw_target=raw,
                )
            )
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


def find_inbound_wikilinks(vault_root: Path, target_rel_path: str) -> list[InboundLink]:
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

    @classmethod
    def from_entries(
        cls,
        vault_root: Path,
        entries: Iterable[tuple[str, str | None]],
    ) -> WikilinkResolver:
        """Build resolver maps from already-read paths/titles without I/O."""
        resolver = cls.__new__(cls)
        resolver.vault_root = Path(vault_root)
        resolver.full_paths = set()
        resolver.kb_stripped = set()
        resolver.stems = {}
        resolver.titles = {}
        resolver._title_by_rel = {}
        normalized = sorted(
            (
                str(rel_path).replace("\\", "/").lstrip("/").removesuffix(".md"),
                str(title).strip().lower() if title and str(title).strip() else None,
            )
            for rel_path, title in entries
        )
        for no_ext, title_lower in normalized:
            resolver._add_entry(no_ext, title_lower)
        return resolver

    def _build(self) -> None:
        vault_resolved = self.vault_root.resolve()
        for md in sorted(walk_vault_md(self.vault_root), key=lambda item: item.as_posix()):
            try:
                rel = md.resolve().relative_to(vault_resolved).as_posix()
            except ValueError:
                continue
            self._add_entry(rel.removesuffix(".md"), self._read_title_lower(md))

    def fork(self) -> WikilinkResolver:
        """Return an I/O-free detached copy suitable for write preparation.

        Writers may add their pending primary to the copy without polluting the
        graph lane's process-shared resolver when validation later fails.
        """
        resolver = self.__class__.__new__(self.__class__)
        resolver.vault_root = self.vault_root
        resolver.full_paths = set(self.full_paths)
        resolver.kb_stripped = set(self.kb_stripped)
        resolver.stems = {key: list(values) for key, values in self.stems.items()}
        resolver.titles = {key: list(values) for key, values in self.titles.items()}
        resolver._title_by_rel = dict(self._title_by_rel)
        return resolver

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
        self._add_entry(no_ext, title.strip().lower() if title and title.strip() else None)


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
        canonical = cleaned if cleaned.startswith(kb_prefix()) else kb_prefix() + cleaned
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
    return fallback + anchor, (f"wikilink {target!r} does not resolve to any file in the vault")


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
        replacement = f"[[{rendered}|{alias}]]" if alias is not None else f"[[{rendered}]]"
        new_body = new_body[: m.start()] + replacement + new_body[m.end() :]
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
        title = title[len(kb_prefix()) :]
    new_entry = f"## [{date_iso}] {op} | {title}\n\n{escape_wikilinks_for_log(body)}\n"
    if new_entry in log_text:
        return log_text
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


@dataclass(frozen=True, slots=True)
class LogWritePlan:
    """Pure, ordered log update/rotation writes for one stable operation."""

    writes: tuple[PlannedWrite, ...]
    warning: str | None = None
    rotation_note: str | None = None


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


def _plan_log_content(
    vault_root: Path,
    *,
    log_text: str,
    live_guard: PathGuard,
    operation_token: str,
) -> LogWritePlan:
    """Plan deterministic rotation for already-final live-log bytes."""
    root = Path(vault_root)
    log_file = kb_root(root) / "log.md"
    token_hash = hashlib.sha256(operation_token.encode("utf-8")).hexdigest()
    archive_path = kb_root(root) / "_archive" / "logs" / f"log-{token_hash[:20]}.md"
    archive_rel = archive_path.relative_to(root).as_posix()
    try:
        current_archive, archive_guard = read_guarded_text(root, archive_path)
        existing_archive = True
    except FileNotFoundError:
        current_archive = None
        archive_guard = PathGuard.capture(root, archive_rel, leaf_policy="absent")
        existing_archive = False

    rotate = len(log_text.encode("utf-8")) > _log_rotate_bytes()
    separator = "\n---\n"
    sep_idx = log_text.find(separator)
    starts: list[int] = []
    if rotate and sep_idx != -1:
        head_end = sep_idx + len(separator)
        starts = [
            match.start()
            for match in _LOG_ENTRY_START_RE.finditer(log_text[head_end:])
        ]
    if rotate and sep_idx != -1 and len(starts) > LOG_ROTATE_KEEP_ENTRIES:
        head_end = sep_idx + len(separator)
        entries_text = log_text[head_end:]
        cut = starts[LOG_ROTATE_KEEP_ENTRIES]
        live_text = log_text[:head_end] + entries_text[:cut]
        tail = entries_text[cut:]
        moved = len(starts) - LOG_ROTATE_KEEP_ENTRIES
        archive_text = (
            f"# log.md archive segment ({token_hash})\n\n"
            f"Rotated out of `{kb_prefix()}log.md` — {moved} entrie(s), newest "
            f"first, byte-exact.\n{separator}{tail}"
        )
        if current_archive is not None and current_archive != archive_text:
            raise ValueError("LOG_ARCHIVE_COLLISION: deterministic archive already differs")
        return LogWritePlan(
            (
                PlannedWrite(
                    archive_path,
                    archive_text,
                    create_only=not existing_archive,
                    guard=archive_guard,
                ),
                PlannedWrite(log_file, live_text, guard=live_guard),
            ),
            rotation_note=f"log.md rotated: {moved} older entrie(s) → {archive_rel}",
        )

    # A completed partial semantic batch may already have rotated the live log.
    # Include its exact deterministic archive again so the auxiliary target set
    # and digest remain identical on retry.
    writes: list[PlannedWrite] = []
    if current_archive is not None:
        writes.append(PlannedWrite(archive_path, current_archive, guard=archive_guard))
    writes.append(PlannedWrite(log_file, log_text, guard=live_guard))
    return LogWritePlan(tuple(writes))


def plan_log_writes(
    vault_root: Path,
    *,
    date_iso: str,
    op: str,
    rel_path_no_ext: str,
    body: str,
    operation_token: str,
) -> LogWritePlan:
    """Purely plan one idempotent log entry and any deterministic rotation."""
    log_file = kb_root(vault_root) / "log.md"
    if not log_file.is_file():
        return LogWritePlan(
            (), warning=f"{kb_prefix()}log.md missing; skipped log entry"
        )
    current, live_guard = read_guarded_text(vault_root, log_file)
    updated = prepend_log_entry(
        current,
        date_iso=date_iso,
        op=op,
        rel_path_no_ext=rel_path_no_ext,
        body=body,
    )
    return _plan_log_content(
        vault_root,
        log_text=updated,
        live_guard=live_guard,
        operation_token=operation_token,
    )


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
        text, live_guard = read_guarded_text(vault_root, log_file)
        plan = _plan_log_content(
            vault_root,
            log_text=text,
            live_guard=live_guard,
            operation_token="standalone-rotation:" + hashlib.sha256(
                text.encode("utf-8")
            ).hexdigest(),
        )
        if plan.rotation_note is None:
            return None
        batch_atomic_write(plan.writes, vault_root=vault_root)
        log.info(plan.rotation_note)
        return plan.rotation_note
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
    operation_token = hashlib.sha256(
        json.dumps(
            [date_iso, op, rel_path_no_ext, body],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    plan = plan_log_writes(
        vault_root,
        date_iso=date_iso,
        op=op,
        rel_path_no_ext=rel_path_no_ext,
        body=body,
        operation_token="standalone-entry:" + operation_token,
    )
    if plan.warning is not None:
        return plan.warning
    try:
        batch_atomic_write(plan.writes, vault_root=vault_root)
        return None
    except Exception as error:  # noqa: BLE001 — standalone logging is best-effort
        log.warning("log write skipped (%s)", error)
        return f"log entry skipped: {error}"


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
        title = title[len(kb_prefix()) :]

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
        entries.append(
            {
                "date": m.group(1),
                "op": m.group(2),
                "summary": text[body_start:body_end].strip(),
            }
        )
    return entries
