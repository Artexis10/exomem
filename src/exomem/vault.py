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
import secrets
import stat
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Literal

import yaml
from slugify import slugify as _slugify

from . import freshness, held_fs, privacy_log, reserved_paths
from .kbdir import kb_dirname, kb_prefix

if TYPE_CHECKING:
    from .graph_sync import GraphSyncCheckpoint

if os.name == "nt":  # pragma: no cover - imported only on Windows
    import msvcrt
else:  # pragma: no cover - platform branch exercised on POSIX
    import fcntl

_SUPPORTS_DIRECTORY_FD = bool(
    os.open in getattr(os, "supports_dir_fd", set())
    and os.mkdir in getattr(os, "supports_dir_fd", set())
)
# Guarded Windows snapshots must keep every verified ancestor from being
# renamed, deleted, or changed into a reparse point until the leaf has been
# opened and read. Child mutation needs WRITE sharing but cannot substitute a
# retained non-empty ancestor; withholding DELETE pins the directory name.
_WINDOWS_GUARDED_DIRECTORY_SHARE = 0x00000001 | 0x00000002
_WINDOWS_DEFAULT_SHARE = 0x00000001 | 0x00000002 | 0x00000004
# Requesting metadata-only access does not establish a Windows share
# reservation. FILE_LIST_DIRECTORY is the least directory access that makes
# omission of FILE_SHARE_DELETE pin the verified namespace.
_WINDOWS_FILE_LIST_DIRECTORY = 0x00000001

log = logging.getLogger(__name__)


SLUG_MAX_LENGTH = 100
#: Characters no derived filename may contain, on any platform.
#:
#: The union of what Windows, macOS and Linux forbid, applied everywhere rather
#: than per host. A vault is a portable artifact synced between machines, so a
#: name written on Linux containing ':' or '?' would make the vault impossible to
#: check out on Windows at all -- the failure would land on whoever opened it
#: next, not on whoever wrote it.
_RESERVED_FILENAME_CHARS = frozenset(r'<>:"/\|?*')
#: Names Windows refuses regardless of extension, matched case-insensitively
#: against the stem.
_RESERVED_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{digit}" for digit in range(1, 10)}
    | {f"lpt{digit}" for digit in range(1, 10)}
)
#: The two styles a vault may name derived files in. `slug` is the historical
#: behaviour and stays the default: the filename is the note's identity here
#: (there is no permalink field), so changing the default would change the
#: address of every note written after an upgrade in vaults nobody asked.
FILENAME_STYLES = ("slug", "title")
DEFAULT_FILENAME_STYLE = "slug"
_FILENAME_STYLE_ENV = "EXOMEM_FILENAME_STYLE"
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
EXCLUDED_FIELD_CODE = "EXCLUDED_FIELD"

# The broader `auto_*` exclusion documented in
# `_scaffold/_Schema/references/frontmatter.md` is deliberately deferred: it is
# a prefix rule with a different compatibility surface from this exact-name set.


def first_excluded_field(names: Iterable[object]) -> tuple[str, str] | None:
    """Return the first excluded string name and its reason, without raising."""
    for name in names:
        if not isinstance(name, str):
            continue
        reason = excluded_frontmatter_reason(name)
        if reason is not None:
            return name, reason
    return None


def excluded_field_in_collection_frontmatter(
    frontmatter: Mapping[str, Any],
) -> tuple[str, str] | None:
    """First excluded name a collection manifest's frontmatter declares, else None.

    A collection declares item field names on two surfaces, and both must be
    fenced. `item_schema.fields` is the obvious one. The Markdown-log note field
    under `storage.item_heading.note.field` is the other: `_validate_values`
    admits that name as a legal item key *outside* the schema, so an excluded
    name declared there is fully operable. It is also the more dangerous of the
    two, because it lives in the immutable storage descriptor — revising it away
    refuses with IMMUTABLE_COLLECTION_REPRESENTATION, so a manifest that slips
    past this check can never be repaired.
    """
    item_schema = frontmatter.get("item_schema")
    fields = item_schema.get("fields") if isinstance(item_schema, Mapping) else None
    if isinstance(fields, Mapping):
        excluded = first_excluded_field(fields)
        if excluded is not None:
            return excluded
    storage = frontmatter.get("storage")
    if not isinstance(storage, Mapping) or storage.get("strategy") != "markdown-log":
        return None
    heading = storage.get("item_heading")
    note = heading.get("note") if isinstance(heading, Mapping) else None
    field = note.get("field") if isinstance(note, Mapping) else None
    return first_excluded_field((field,)) if isinstance(field, str) else None


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


def governed_frontmatter_reason(field: str, value: Any, page_type: Any) -> str | None:
    """A refusal reason if `field` breaks a governed enum contract, else None.

    This lives beside `excluded_frontmatter_reason` deliberately. `confidence`
    is refused by that function at every governed write boundary, and
    `outcome` is its categorical twin — the same doctrine, one step further in.
    Splitting the two checks across different modules is exactly how one of
    them ends up enforced on only one boundary while the other is enforced on
    both, so both policies are stated once, here, and every boundary calls both.
    """
    if field.strip().casefold() != "outcome":
        return None
    # Deferred: `semantic_units` reaches `semantic_language_registry`, which
    # imports this module. The vocabulary still has exactly one definition.
    from .semantic_units import EPISTEMIC_OUTCOMES

    normalized_type = str(page_type or "").strip().casefold()
    if normalized_type != "experiment":
        return (
            "`outcome:` is an experiment-only field; this page has type "
            f"{normalized_type or 'none'}. Record how a non-experiment "
            "conclusion turned out with a semantic unit's `verdict:` metadata "
            "instead."
        )
    if not isinstance(value, str) or value.strip().casefold() not in EPISTEMIC_OUTCOMES:
        return (
            f"`outcome:` must be exactly one of {', '.join(EPISTEMIC_OUTCOMES)}. "
            "It is categorical lifecycle state, not a confidence score, so a "
            "number or a free-text hedge is never valid."
        )
    return None


#: Where the PRODUCT-owned governance markdown lives in a vault.
#:
#: A dot-directory, so it inherits the treatment every other non-note directory
#: in the vault already gets -- from Obsidian, and from any other indexer the
#: user runs -- instead of exomem having to ask each of them separately.
#: `VAULT_SCAN_SKIP_DIRS` already excludes `_Schema` from exomem's own index, but
#: nothing else honours that, so 265 KB of shipped documentation sat in the note
#: namespace and ranked above real notes in a second tool's search (#488).
#:
#: Per-vault configuration -- the YAML registries, `contracts/`,
#: `relation-reviews/`, `private-skills/`, the activation manifest -- deliberately
#: stays under `Knowledge Base/_Schema/`. It belongs to the user, it is small, and
#: it is not markdown, so it is not what pollutes a note index.
SHIPPED_SCHEMA_DIRNAME = ".exomem"
_SHIPPED_SCHEMA_SUBDIR = "schema"
_SCHEMA_SENTINEL = "SKILL.md"


# When scanning the full vault for inbound wikilinks, skip these.
#
# `_Governance` and `_Adoption` are operational state, not knowledge: they name
# items whose own disclosure decisions may be restrictive, so surfacing them as
# content would release by the back door what the release plane withholds at the
# front. `find_corpus.EXCLUDED_DIR_NAMES` already excludes them from the KB
# corpus; this set is the walker `find(scope="vault")` reaches through
# (`bm25.py` -> `walk_vault_md`), and the two must not disagree — a name excluded
# from one walk and indexed by the other is exactly the bypass the exclusion
# exists to prevent.
VAULT_SCAN_SKIP_DIRS = frozenset(
    {
        ".obsidian",
        ".git",
        ".graph-coordination",
        ".graph-commit-receipts",
        ".trash",
        "_attachments",
        "_archive",
        "_trash",
        "_Schema",
        # The shipped contract's new home (#488). It has to be skipped here for
        # the same reason `_Schema` is: `find(scope="vault")` reaches through
        # this walk, and moving 265 KB of product-owned markdown out of the note
        # namespace would only relocate the pollution if exomem's own vault-wide
        # search started indexing it at the new path.
        SHIPPED_SCHEMA_DIRNAME,
        "_Governance",
        "_Adoption",
    }
)
VAULT_SCAN_SKIP_DIR_PREFIXES = (".exomem-batch-",)
_GRAPH_RESET_RUNTIME_DIR_NAME = re.compile(r"^\.graph-reset-[0-9a-f]{24}$", re.ASCII)
_GRAPH_REBUILD_RUNTIME_FILE_NAME = re.compile(
    r"^\.graph-rebuild-[0-9a-f]{64}-[0-9a-f]{24}\.sqlite"
    r"(?:-(?:journal|wal|shm))?$",
    re.ASCII,
)
# The lexical sidecar's detached-build temp, minted at lexstore.py's
# `LexicalStore.rebuild_atomic` as
# `self.path.with_name(f"{self.path.name}.rebuild-{uuid.uuid4().hex}.tmp")`
# where `self.path.name` is always `.lexical.sqlite` (`lexstore.lexical_path`,
# issue #551). This is the one PUBLIC matcher for that shape — both
# `graph_sync.sweep_abandoned_temporaries` and `doctor.py`'s
# `_check_rebuild_temp_orphans` call `is_lexical_rebuild_runtime_file_name`
# rather than keep their own copy. `governance/tool.py`'s
# `_LEXICAL_REBUILD_TEMP_RE` still independently encodes this same shape for
# its own unrelated membership-classification purpose (out of this module's
# scope to touch) — keep it, and `lexstore.py`'s mint site itself, in sync if
# this shape ever changes.
_LEXICAL_REBUILD_RUNTIME_FILE_NAME = re.compile(
    r"^\.lexical\.sqlite\.rebuild-[0-9a-f]{32}\.tmp"
    r"(?:-(?:journal|wal|shm))?$",
    re.ASCII,
)

#: Age (by mtime) above which a matching rebuild-temp file is treated as an
#: orphan/abandoned candidate rather than a legitimate in-flight rebuild. This
#: is NOT "written continuously" for the lexical family: under
#: `PRAGMA journal_mode=WAL` (lexstore.py's `_connect_setup`) an in-flight
#: build's writes land in the temp's `-wal` companion, and the MAIN temp
#: file's own mtime only advances at SQLite's periodic auto-checkpoints
#: (default: every ~1000 pages, ~4 MB) and at the final WAL fold
#: (`_fold_to_single_file`) — not on every write. 60 minutes is comfortably
#: above the write gap between checkpoints on any plausible corpus (issue
#: #551's own incident: abandoned lexical-rebuild temps sat unmodified for
#: days-to-weeks, not minutes) while still catching a truly abandoned temp
#: promptly.
#:
#: One window is genuinely uncovered by mtime freshness, as a documented
#: trade-off rather than a defect: after the fold and `conn.close()`
#: (lexstore.py:2576) `rebuild_atomic` runs a full second `_walk_entries()`
#: corpus re-walk plus guard checks before the publishing `os.replace`
#: (lexstore.py:2646), touching the temp not at all. If that tail alone ever
#: exceeded this threshold on some pathological corpus, the sweep could in
#: principle reap a build still finishing that walk. This threshold is the
#: same one `doctor.py` already chose for its own orphan diagnostic before
#: this file's `sweep_abandoned_temporaries` reused it for deletion — raise
#: it if that tail is ever observed to approach it in practice.
#:
#: Shared by two independent consumers rather than copied a third time:
#: `graph_sync.sweep_abandoned_temporaries` (the LEXICAL family's only
#: abandonment signal — that family cannot participate in
#: `claim_rebuild_owner`/`register_temporary` the way the graph family does,
#: so a matching name alone is never sufficient there) and `doctor.py`'s
#: read-only `_check_rebuild_temp_orphans` diagnostic (for both families).
REBUILD_TEMP_STALE_AGE_SECONDS = 60 * 60


def is_graph_reset_runtime_dir_name(name: str) -> bool:
    """Whether ``name`` is one exact graph-lineage reset workspace."""
    return _GRAPH_RESET_RUNTIME_DIR_NAME.fullmatch(name) is not None


def is_graph_rebuild_runtime_file_name(name: str) -> bool:
    """Whether ``name`` is one exact graph rebuild SQLite artifact."""
    return _GRAPH_REBUILD_RUNTIME_FILE_NAME.fullmatch(name) is not None


def is_lexical_rebuild_runtime_file_name(name: str) -> bool:
    """Whether ``name`` is one exact lexical rebuild SQLite artifact."""
    return _LEXICAL_REBUILD_RUNTIME_FILE_NAME.fullmatch(name) is not None


def in_excluded_scan_dir(rel_path: str) -> bool:
    """True when any segment of `rel_path` is a reserved scan directory.

    The incremental-path counterpart of the exclusion every FULL walk applies
    (walk_vault_md, find's walker, the inbound scan): event-driven patchers
    must not index a path their index's full rebuild would skip. The concrete
    bug this guards: `delete_file` moves a note into `Knowledge Base/_trash/`,
    the watcher sees that as a fresh markdown file, and the trashed content
    gets re-embedded under its trash path — invisible to find (walks exclude
    `_trash/`) but not to the corpus-aware near-dup sweep, which reads the raw
    sidecar (observed 2026-07-04: dup warnings flagging trash entries).
    """
    segments = rel_path.replace("\\", "/").split("/")
    if (
        len(segments) >= 2
        and segments[1].startswith(".graph-reset-")
        and segments[0] == kb_dirname()
        and is_graph_reset_runtime_dir_name(segments[1])
    ):
        return True
    for segment in segments:
        if segment in VAULT_SCAN_SKIP_DIRS or segment.startswith(
            VAULT_SCAN_SKIP_DIR_PREFIXES
        ):
            return True
    return False


# `[[Target]]` or `[[Target|Alias]]`.
_WIKILINK_PATTERN = re.compile(r"\[\[([^\]\|\n]+?)(?:\|[^\]\n]*)?\]\]")
_FM_PATTERN = re.compile(r"^---\r?\n(.*?)\r?\n---(?:\r?\n)?(.*)", re.DOTALL)
_LOCK_NAMESPACES = frozenset(
    {"activation-manifest", "semantic-creation", "lexical-catalog-publication"}
)
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
            f'  Windows:      setx {env_var} "C:/path/to/your/Obsidian"'
        )
    path = Path(override)
    if not _is_vault(path):
        raise RuntimeError(
            f"{env_var}={override!r} does not look like a vault "
            f"(no schema contract in .exomem/schema/ or {kb_prefix()}_Schema/)"
        )
    return path


def shipped_schema_target(vault_root: Path) -> Path:
    """Where shipped governance markdown is WRITTEN. Always the new location.

    Separate from `shipped_schema_root` so a refresh moves the read path forward
    without deleting anything: after it runs, both locations hold the content and
    the resolver prefers the new one. Reclaiming the old copy is its own explicit
    step, because an upgrade that silently deletes 404 KB from inside a user's
    Obsidian vault is the wrong default even when the bytes are reproducible.
    """
    return Path(vault_root) / SHIPPED_SCHEMA_DIRNAME / _SHIPPED_SCHEMA_SUBDIR


def legacy_shipped_schema_root(vault_root: Path) -> Path:
    """The pre-#488 location, still read and still valid."""
    return Path(vault_root) / kb_dirname() / "_Schema"


def shipped_schema_root(vault_root: Path) -> Path:
    """Where shipped governance markdown is READ from.

    New location when it holds the sentinel, else the legacy one. Preferring the
    new location matters when both exist: a refresh writes the new one, so
    reading the old one would serve the bytes the refresh just superseded.
    """
    target = shipped_schema_target(vault_root)
    if (target / _SCHEMA_SENTINEL).exists():
        return target
    return legacy_shipped_schema_root(vault_root)


def _is_vault(path: Path) -> bool:
    """Whether this directory is an exomem vault.

    Either sentinel counts. This function decides whether `resolve_vault`,
    `product_invoke`, `doctor` and the hosted runtime will speak to a directory
    at all, so a vault has to stay a vault across the move in both directions --
    one that has migrated, and one that never will.
    """
    return (
        (path / kb_dirname() / "_Schema" / _SCHEMA_SENTINEL).exists()
        or (shipped_schema_target(path) / _SCHEMA_SENTINEL).exists()
    )


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


def sanitize_title_filename(title: str, max_length: int = SLUG_MAX_LENGTH) -> str:
    """A human title reduced to a filename, or "" when nothing survives.

    Removes rather than transliterates. `Q3: revenue / margin` becomes
    `Q3 revenue margin`, not `Q3- revenue - margin`: a substitution invents a
    character the author did not write, and the frontmatter `title` still carries
    the exact original either way, so an omission loses nothing a reader cannot
    recover.

    Applies the union of platform restrictions, never the running host's -- see
    `_RESERVED_FILENAME_CHARS`. NFC because that is what Obsidian and git expect;
    HFS+ stores NFD and normalising at the boundary keeps one form on disk.

    Returns "" for a title that is entirely reserved characters, which the caller
    resolves by falling back to the slug style for that note rather than by
    failing the write.
    """
    normalized = unicodedata.normalize("NFC", title)
    kept: list[str] = []
    for character in normalized:
        # Whitespace is classified BEFORE the category filter. A tab is category
        # Cc, so filtering first would delete it and weld the words on either
        # side together; it is a word separator and has to survive as a space.
        if character.isspace() or character in _RESERVED_FILENAME_CHARS:
            kept.append(" ")
        elif unicodedata.category(character)[0] == "C":
            # Everything else in C is invisible: control codes, and format
            # characters like U+200B that would make two differently-named files
            # look identical in every listing.
            continue
        else:
            kept.append(character)
    collapsed = " ".join("".join(kept).split())
    if max_length and len(collapsed) > max_length:
        # Word boundary, matching what `slugify_title` does, so a truncated name
        # still ends on something readable.
        head = collapsed[: max_length + 1]
        cut = head.rfind(" ")
        collapsed = (head[:cut] if cut > 0 else collapsed[:max_length]).strip()
    # A trailing dot or space is silently dropped by the Windows API, so a name
    # ending in one is not the name that ends up on disk.
    collapsed = collapsed.rstrip(". ")
    if not collapsed:
        return ""
    stem = collapsed.split(".")[0].lower()
    if stem in _RESERVED_DEVICE_NAMES:
        return ""
    return collapsed


def resolve_filename_style(vault_root: Path | None = None) -> str:
    """The filename style in force, by documented precedence.

    Environment first so a run can be pinned without editing the vault, then the
    vault's own key, then the default. An unrecognised value is refused rather
    than quietly treated as the default -- a typo in a config key that silently
    means "carry on" is how a user concludes the setting does not work.
    """
    configured = os.environ.get(_FILENAME_STYLE_ENV)
    source = "environment"
    if configured is None and vault_root is not None:
        from . import project_keys

        configured = project_keys.filename_style(vault_root)
        source = "project-keys.yaml"
    if configured is None:
        return DEFAULT_FILENAME_STYLE
    normalized = str(configured).strip().lower()
    if normalized not in FILENAME_STYLES:
        raise InvalidSlugError(
            f"filename style {configured!r} from {source} is not one of "
            f"{', '.join(FILENAME_STYLES)}"
        )
    return normalized


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
            f"SLUG_TRUNCATED: slug truncated to {slug!r} (full would have been "
            f"{full!r}); link to this note using {slug!r} — re-deriving a slug "
            f"from the title will not resolve. Shorten the title if the "
            f"truncation drops meaning."
        )
    return slug, None


def resolve_filename_slug(
    title: str, slug: str | None = None, *, vault_root: Path | None = None
) -> tuple[str, list[str]]:
    """Resolve a new filename component without conflating it with display title.

    Explicit slugs are deliberately strict and portable. Automatic slugging is
    kept for compatibility, including its language-blind transliteration, but
    callers get a warning whenever non-ASCII title text enters that lossy path.

    `vault_root` selects the vault's filename style for the DERIVED branch only.
    An explicit `slug` keeps its contract under every style, because it is how a
    caller pins a name it already intends to link to. Omitting `vault_root`
    resolves the style from the environment alone, which keeps every existing
    caller on today's behaviour.
    """
    if slug is not None:
        if not isinstance(slug, str) or not _EXPLICIT_SLUG_PATTERN.fullmatch(slug):
            raise InvalidSlugError(
                "slug must be lowercase ASCII kebab-case (letters, digits, and single hyphens only)"
            )
        if len(slug) > SLUG_MAX_LENGTH:
            raise InvalidSlugError(f"slug exceeds the {SLUG_MAX_LENGTH}-character filename limit")
        return slug, []

    if resolve_filename_style(vault_root) == "title":
        readable = sanitize_title_filename(title)
        if readable:
            # No transliteration warning here: nothing was transliterated. That
            # warning exists because the slug path is lossy for non-ASCII text,
            # and this path is what a vault sets to stop paying that cost.
            warnings = []
            if readable != sanitize_title_filename(title, max_length=0):
                warnings.append(
                    f"SLUG_TRUNCATED: filename truncated to {readable!r}; link to "
                    "this note using that name or its frontmatter title -- "
                    "re-deriving a name from the full title will not resolve."
                )
            return readable, warnings
        # A title that is entirely reserved characters has no readable form, so
        # fall through to the slug path rather than failing the write. One note
        # named the old way beats a refused write.

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
    """Return a path that doesn't exist yet, appending -2, -3, ... on collision.

    Collision is tested case-INSENSITIVELY on every platform, not with
    `Path.exists()`. Windows and default macOS already answer that way, so a
    `Path.exists()` check produced a vault whose contents depended on where the
    write happened: on Linux `Budget Review.md` and `budget review.md` are two
    files, and the same vault opened on Windows is one file with one of the two
    notes gone. Readable filenames make this reachable -- under slug style
    everything was lowercased, so two titles differing only by case already
    landed on the same name and hit the loop below.

    Listing the directory costs one syscall on a write path, against a data-loss
    class that only appears after a sync to another machine.
    """
    try:
        taken = {entry.name.casefold() for entry in directory.iterdir()}
    except OSError:
        # Unlistable, most often because it does not exist yet. `exists()` is
        # the weaker test -- case-sensitive on Linux -- but it is the only one
        # available without a listing, and it must still be applied rather than
        # assumed to pass, or an unlistable-but-populated directory would hand
        # back a path that already holds another note.
        candidate = directory / f"{stem}{suffix}"
        i = 2
        while candidate.exists():
            candidate = directory / f"{stem}-{i}{suffix}"
            i += 1
        return candidate
    candidate = f"{stem}{suffix}"
    i = 2
    while candidate.casefold() in taken:
        candidate = f"{stem}-{i}{suffix}"
        i += 1
    return directory / candidate


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
    namespace: Literal["activation-manifest", "semantic-creation", "lexical-catalog-publication"],
    *,
    timeout: float = 30.0,
):
    """Serialize one vault-scoped creation namespace under one shared deadline.

    ``lexical-catalog-publication`` serializes atomic lexical-catalog publication
    (background ``rebuild_atomic`` replacement and the bounded foreground
    ``apply_catalog_delta`` patch) so a background publish can re-check the live
    catalog's generation and never overwrite a newer catalog a concurrent
    foreground delta produced.
    """
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


@dataclass(frozen=True, slots=True)
class BatchTargetSummary:
    """Bounded public summary of the logical targets in one batch."""

    affected_count: int
    targets: tuple[str, ...]
    omitted_target_count: int

    def __post_init__(self) -> None:
        if (
            type(self.affected_count) is not int
            or self.affected_count < 0
            or type(self.omitted_target_count) is not int
            or self.omitted_target_count < 0
            or type(self.targets) is not tuple
            or len(self.targets) > 16
            or self.omitted_target_count != self.affected_count - len(self.targets)
        ):
            raise ValueError("invalid batch target summary")
        for target in self.targets:
            if (
                type(target) is not str
                or not target
                or target.startswith("/")
                or "\\" in target
                or "\0" in target
                or any(part in {"", ".", ".."} for part in target.split("/"))
                or len(target.encode("utf-8")) > 1024
            ):
                raise ValueError("invalid batch target summary")


_BATCH_WRITE_ERROR_FIELDS = {
    "BATCH_ROLLBACK_INCOMPLETE": (
        "rollback_incomplete",
        "The batch could not be fully rolled back.",
    ),
    "BATCH_CLEANUP_INCOMPLETE": (
        "cleanup_incomplete",
        "The batch workspace cleanup is incomplete.",
    ),
}
_BATCH_RETRY_REMEDIATION = (
    "Reconcile retained workspace state, then retry with fresh guards if the intended "
    "write is still needed."
)
_BATCH_COMMITTED_REMEDIATION = (
    "Do not retry the write; committed destinations are preserved. Reconcile retained "
    "workspace state."
)


class BatchWriteError(ValueError):
    """Sanitized public outcome for a batch that retained workspace state."""

    def __init__(
        self,
        code: str,
        summary: BatchTargetSummary,
        committed: bool,
        *,
        diagnostics: Iterable[BaseException] = (),
    ) -> None:
        if (
            code not in _BATCH_WRITE_ERROR_FIELDS
            or not isinstance(summary, BatchTargetSummary)
            or type(committed) is not bool
        ):
            raise ValueError("invalid batch write outcome")
        if code == "BATCH_ROLLBACK_INCOMPLETE" and committed:
            raise ValueError("rollback-incomplete outcome cannot be committed")
        self.code = code
        self.summary = summary
        self.outcome_kind, self.message = _BATCH_WRITE_ERROR_FIELDS[code]
        self.committed = committed
        self.incomplete = True
        self.affected_count = summary.affected_count
        self.targets = summary.targets
        self.omitted_target_count = summary.omitted_target_count
        self.remediation = _BATCH_COMMITTED_REMEDIATION if committed else _BATCH_RETRY_REMEDIATION
        self._diagnostics = tuple(diagnostics)
        ValueError.__init__(self, self.__str__())

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "remediation": self.remediation,
            "outcome": {
                "kind": self.outcome_kind,
                "committed": self.committed,
                "incomplete": self.incomplete,
                "affected_count": self.affected_count,
                "targets": list(self.targets),
                "omitted_target_count": self.omitted_target_count,
            },
        }

    def __str__(self) -> str:
        return json.dumps(
            self.as_public_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def __reduce__(self) -> tuple[Any, tuple[str, BatchTargetSummary, bool]]:
        return type(self), (self.code, self.summary, self.committed)


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
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
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
    expected_content_size: int | None = field(default=None, init=False)

    @classmethod
    def capture(
        cls,
        vault_root: Path,
        target: str,
        *,
        leaf_policy: Literal["absent", "stable", "content"],
        expected_content_hash: str | None = None,
        expected_content_size: int | None = None,
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
        if expected_content_size is not None and (
            leaf_policy != "content"
            or type(expected_content_size) is not int
            or expected_content_size < 0
        ):
            raise PathGuardError(
                "PATH_GUARD_INVALID", "content size requires a content leaf policy"
            )
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
        object.__setattr__(guard, "expected_content_size", expected_content_size)
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
            expected_content_size=self.expected_content_size,
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
        if self.leaf_policy == "content":
            # Content guards created by the bounded reader never reopen this
            # path through Path.  Re-open descriptor-rooted and cap the read at
            # the exact byte count captured with the hash.
            if self.expected_content_size is not None:
                if info.st_size != self.expected_content_size:
                    raise PathGuardError(
                        "PATH_GUARD_CONTENT", "guarded content changed"
                    )
                data, rebound = _read_bounded_guarded_snapshot(
                    root, self.target, self.expected_content_size
                )
                if (
                    rebound.ancestors != self.ancestors
                    or rebound.leaf_identity != self.leaf_identity
                    or hashlib.sha256(data).hexdigest() != self.expected_content_hash
                ):
                    raise PathGuardError("PATH_GUARD_CONTENT", "guarded content changed")
            elif _leaf_hash(leaf, self.leaf_identity) != self.expected_content_hash:
                # Compatibility for callers that predate bounded snapshots.
                raise PathGuardError("PATH_GUARD_CONTENT", "guarded content changed")


def _same_captured_identity(first: PathIdentity, second: PathIdentity) -> bool:
    return (
        first.device == second.device and first.inode == second.inode and first.mode == second.mode
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


def _same_file_object(first: PathIdentity, second: PathIdentity) -> bool:
    return first.device == second.device and first.inode == second.inode


def _descriptor_hash(descriptor: int, expected: PathIdentity) -> str:
    try:
        offset = os.lseek(descriptor, 0, os.SEEK_CUR)
        os.lseek(descriptor, 0, os.SEEK_SET)
    except OSError as error:
        raise PathGuardError("PATH_GUARD_IO", "batch artifact is not seekable") from error
    digest = hashlib.sha256()
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not _same_identity(expected, info):
            raise PathGuardError("PATH_GUARD_CHANGED", "batch artifact identity changed")
        while chunk := os.read(descriptor, 65536):
            digest.update(chunk)
        if not _same_identity(expected, os.fstat(descriptor)):
            raise PathGuardError("PATH_GUARD_CHANGED", "batch artifact identity changed")
    finally:
        os.lseek(descriptor, offset, os.SEEK_SET)
    return digest.hexdigest()


def _write_all(descriptor: int, content: bytes) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    os.ftruncate(descriptor, 0)
    view = memoryview(content)
    written = 0
    while written < len(view):
        try:
            count = os.write(descriptor, view[written:])
        except InterruptedError:
            continue
        if count <= 0:
            raise OSError("descriptor write made no progress")
        written += count


def _descriptor_bytes(descriptor: int, expected: PathIdentity) -> bytes:
    try:
        offset = os.lseek(descriptor, 0, os.SEEK_CUR)
        os.lseek(descriptor, 0, os.SEEK_SET)
    except OSError as error:
        raise PathGuardError("PATH_GUARD_IO", "batch artifact is not seekable") from error
    chunks: list[bytes] = []
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not _same_identity(expected, info):
            raise PathGuardError("PATH_GUARD_CHANGED", "batch artifact identity changed")
        while True:
            try:
                chunk = os.read(descriptor, 65536)
            except InterruptedError:
                continue
            if not chunk:
                break
            chunks.append(chunk)
        if not _same_identity(expected, os.fstat(descriptor)):
            raise PathGuardError("PATH_GUARD_CHANGED", "batch artifact identity changed")
    finally:
        os.lseek(descriptor, offset, os.SEEK_SET)
    return b"".join(chunks)


_UNSUPPORTED_XATTR_ERRNOS = frozenset(
    value
    for value in (
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
        getattr(errno, "ENOSYS", None),
    )
    if value is not None
)


def _capture_descriptor_xattrs(descriptor: int) -> dict[str, bytes] | None:
    if not all(hasattr(os, name) for name in ("listxattr", "getxattr", "setxattr")):
        return None
    try:
        names = os.listxattr(descriptor)
    except OSError as error:
        if error.errno in _UNSUPPORTED_XATTR_ERRNOS:
            return None
        raise
    values: dict[str, bytes] = {}
    for name in sorted(names, key=os.fsencode):
        try:
            values[name] = os.getxattr(descriptor, name)
        except OSError as error:
            if error.errno in _UNSUPPORTED_XATTR_ERRNOS:
                return None
            raise
    return values


@dataclass(frozen=True, slots=True)
class _BatchSnapshot:
    content: bytes
    content_hash: str
    mode: int
    atime_ns: int
    mtime_ns: int
    xattrs: dict[str, bytes] | None


@dataclass(frozen=True, slots=True)
class _BatchReplaceContext:
    vault_root: Path | None
    expected_destination: PathIdentity | None
    published_metadata: _BatchSnapshot | None = None


_BATCH_REPLACE_CONTEXT: ContextVar[_BatchReplaceContext | None] = ContextVar(
    "exomem_batch_replace_context", default=None
)


@contextmanager
def _batch_replace_context(
    vault_root: Path | None,
    expected_destination: PathIdentity | None,
    published_metadata: _BatchSnapshot | None = None,
) -> Iterator[None]:
    """Carry held-publication inputs without widening the legacy test seam."""

    token = _BATCH_REPLACE_CONTEXT.set(
        _BatchReplaceContext(vault_root, expected_destination, published_metadata)
    )
    try:
        yield
    finally:
        _BATCH_REPLACE_CONTEXT.reset(token)


@dataclass(slots=True)
class _WorkspaceArtifact:
    workspace: _BatchWorkspace
    name: str
    descriptor: int
    identity: PathIdentity
    content_hash: str
    held_file: held_fs.HeldFile | None = None
    content_bound: bool = False
    closed: bool = False

    @property
    def path(self) -> Path:
        return self.workspace.path / self.name

    def recheck(self, *, verify_content: bool = True) -> None:
        self.workspace.recheck_identity()
        if self.closed:
            raise PathGuardError("PATH_GUARD_CHANGED", "batch stage handle is closed")
        try:
            descriptor_info = os.fstat(self.descriptor)
            path_info = self.workspace.stat_child(self.name)
        except OSError as error:
            raise PathGuardError("PATH_GUARD_CHANGED", "batch stage changed") from error
        if (
            not stat.S_ISREG(descriptor_info.st_mode)
            or _is_reparse(descriptor_info)
            or not _same_identity(self.identity, descriptor_info)
            or not stat.S_ISREG(path_info.st_mode)
            or stat.S_ISLNK(path_info.st_mode)
            or _is_reparse(path_info)
            or not _same_identity(self.identity, path_info)
        ):
            raise PathGuardError("PATH_GUARD_CHANGED", "batch stage changed")
        if verify_content and _descriptor_hash(self.descriptor, self.identity) != self.content_hash:
            raise PathGuardError("PATH_GUARD_CONTENT", "batch stage content changed")

    def refresh_identity(self) -> None:
        info = os.fstat(self.descriptor)
        refreshed = _identity(self.name, info)
        if (
            not _same_file_object(self.identity, refreshed)
            or not stat.S_ISREG(info.st_mode)
            or _is_reparse(info)
        ):
            raise PathGuardError("PATH_GUARD_CHANGED", "batch stage changed")
        self.identity = refreshed
        self.recheck(verify_content=False)

    def bind_initializing_content(self) -> None:
        if self.content_bound:
            return
        self.recheck(verify_content=False)
        self.content_hash = _descriptor_hash(self.descriptor, self.identity)
        self.content_bound = True
        self.recheck()

    def close(self) -> None:
        if not self.closed:
            if self.held_file is not None:
                self.held_file.close()
            else:
                os.close(self.descriptor)
            self.closed = True


def _remove_created_workspace(
    parent: Path,
    name: str,
    parent_descriptor: int,
    parent_identity: PathIdentity,
    workspace_identity: PathIdentity,
) -> bool:
    """Remove a newly-created empty workspace only while its binding is exact."""
    path = parent / name
    try:
        parent_descriptor_info = os.fstat(parent_descriptor)
        parent_path_info = parent.lstat()
        if (
            not _same_identity(parent_identity, parent_descriptor_info)
            or not _same_identity(parent_identity, parent_path_info)
            or not stat.S_ISDIR(parent_path_info.st_mode)
            or stat.S_ISLNK(parent_path_info.st_mode)
            or _is_reparse(parent_path_info)
        ):
            return False
        if not os.path.lexists(path):
            return True
        if os.stat in getattr(os, "supports_dir_fd", set()):
            info = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        else:  # pragma: no cover - Windows fallback
            info = path.lstat()
        if (
            not _same_identity(workspace_identity, info)
            or not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or _is_reparse(info)
        ):
            return False
        if os.rmdir in getattr(os, "supports_dir_fd", set()):
            os.rmdir(name, dir_fd=parent_descriptor)
        else:  # pragma: no cover - Windows fallback
            path.rmdir()
        return not os.path.lexists(path)
    except OSError:
        return False


def _batch_workspace_token(size: int) -> str:
    return secrets.token_hex(size)


def _after_batch_destination_published(_path: Path) -> None:
    """Crash-injection seam after one canonical destination is installed."""


def _before_batch_destination_published(
    _path: Path, *, restoring: bool
) -> None:
    """Fault-injection seam immediately before one held publication attempt."""


def _after_batch_parent_created(_vault_root: Path, _relative: str) -> None:
    """Race-injection seam after one retained parent is created."""


def _remove_held_workspace(
    filesystem: held_fs.HeldFilesystem,
    relative: str,
    expected: held_fs.StableIdentity,
) -> bool:
    """Remove one exact workspace through a fresh mutation-capable handle."""

    mutable = filesystem.parent(relative, access="mutate")
    if not mutable.ok:
        return False
    with mutable.require() as directory:
        if directory.identity != expected:
            return False
        return filesystem.unlink_directory(directory).ok


@dataclass(slots=True)
class _BatchWorkspace:
    parent: Path
    name: str
    parent_descriptor: int
    descriptor: int
    parent_identity: PathIdentity
    identity: PathIdentity
    artifacts: dict[str, _WorkspaceArtifact]
    held_filesystem: held_fs.HeldFilesystem | None = None
    held_parent: held_fs.HeldDirectory | None = None
    held_directory: held_fs.HeldDirectory | None = None
    held_workspace_relative: str | None = None
    closed: bool = False

    @property
    def path(self) -> Path:
        return self.parent / self.name

    @classmethod
    def create(
        cls,
        parent: Path,
        *,
        vault_root: Path | None = None,
    ) -> _BatchWorkspace:
        if vault_root is not None:
            return cls._create_held(parent, vault_root=Path(vault_root))
        absolute_parent = Path(os.path.abspath(parent))
        absolute_parent.mkdir(parents=True, exist_ok=True)
        try:
            parent_info = absolute_parent.lstat()
            parent_descriptor = _open_directory_path(absolute_parent)
        except OSError as error:
            raise PathGuardError("PATH_GUARD_IO", "batch parent is unavailable") from error
        workspace: _BatchWorkspace | None = None
        workspace_descriptor: int | None = None
        workspace_identity: PathIdentity | None = None
        created = False
        try:
            opened_parent = os.fstat(parent_descriptor)
            if (
                not stat.S_ISDIR(parent_info.st_mode)
                or stat.S_ISLNK(parent_info.st_mode)
                or _is_reparse(parent_info)
                or not _same_identity(_identity(".", parent_info), opened_parent)
            ):
                raise PathGuardError("PATH_GUARD_UNSAFE", "batch parent is unsafe")
            for _attempt in range(16):
                name = f".exomem-batch-{_batch_workspace_token(16)}"
                try:
                    # Python 3.13 gives mode 0700 special restrictive ACL
                    # semantics on Windows. A LocalSystem service would then
                    # move that SYSTEM-only ACL onto the user's final note.
                    workspace_mode = 0o700 if os.name != "nt" else 0o777
                    if os.mkdir in getattr(os, "supports_dir_fd", set()):
                        os.mkdir(name, workspace_mode, dir_fd=parent_descriptor)
                    else:  # pragma: no cover - Windows fallback
                        os.mkdir(absolute_parent / name, workspace_mode)
                except FileExistsError:
                    continue
                created = True
                break
            else:  # pragma: no cover - cryptographic collisions are not practical
                raise PathGuardError("PATH_GUARD_IO", "batch workspace allocation failed")
            workspace_path = absolute_parent / name
            if os.stat in getattr(os, "supports_dir_fd", set()):
                workspace_path_info = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            else:  # pragma: no cover - Windows fallback
                workspace_path_info = workspace_path.lstat()
            if (
                not stat.S_ISDIR(workspace_path_info.st_mode)
                or stat.S_ISLNK(workspace_path_info.st_mode)
                or _is_reparse(workspace_path_info)
            ):
                raise PathGuardError("PATH_GUARD_UNSAFE", "batch workspace is unsafe")
            workspace_identity = _identity(name, workspace_path_info)
            if _SUPPORTS_DIRECTORY_FD:
                workspace_descriptor = _open_directory_at(parent_descriptor, name)
            else:  # pragma: no cover - Windows fallback
                workspace_descriptor = _open_directory_path(workspace_path)
            workspace_info = os.fstat(workspace_descriptor)
            if (
                not stat.S_ISDIR(workspace_info.st_mode)
                or _is_reparse(workspace_info)
                or not _same_identity(workspace_identity, workspace_info)
            ):
                raise PathGuardError("PATH_GUARD_UNSAFE", "batch workspace is unsafe")
            workspace = cls(
                absolute_parent,
                name,
                parent_descriptor,
                workspace_descriptor,
                _identity(".", opened_parent),
                workspace_identity,
                {},
            )
            if os.name != "nt" and hasattr(os, "fchmod"):
                os.fchmod(workspace_descriptor, 0o700)
            workspace.refresh_identity()
            return workspace
        except BaseException as init_error:
            if workspace is not None:
                cleaned = workspace.cleanup()
            elif created and workspace_identity is not None:
                if workspace_descriptor is not None:
                    os.close(workspace_descriptor)
                cleaned = _remove_created_workspace(
                    absolute_parent,
                    name,
                    parent_descriptor,
                    _identity(".", opened_parent),
                    workspace_identity,
                )
                os.close(parent_descriptor)
            elif not created:
                if workspace_descriptor is not None:  # pragma: no cover - defensive
                    os.close(workspace_descriptor)
                os.close(parent_descriptor)
                raise
            else:
                if workspace_descriptor is not None:
                    os.close(workspace_descriptor)
                os.close(parent_descriptor)
                cleaned = False
            if not cleaned:
                _raise_cleanup_retained(init_error)
            raise

    @classmethod
    def _create_held(
        cls,
        parent: Path,
        *,
        vault_root: Path,
    ) -> _BatchWorkspace:
        root = Path(os.path.abspath(vault_root))
        absolute_parent = Path(os.path.abspath(parent))
        try:
            parent_relative = absolute_parent.relative_to(root).as_posix()
        except ValueError as error:
            raise PathGuardError(
                "PATH_GUARD_TARGET", "batch parent escaped the vault"
            ) from error
        if parent_relative == ".":
            parent_relative = "."
        acquired = held_fs.acquire(root)
        if not acquired.ok:
            raise PathGuardError(
                "PATH_GUARD_UNSAFE", "held filesystem route is unavailable"
            )
        filesystem = acquired.require()
        parent_result = filesystem.parent(parent_relative)
        if not parent_result.ok:
            filesystem.close()
            raise PathGuardError("PATH_GUARD_UNSAFE", "batch parent is unsafe")
        held_parent = parent_result.require()
        held_directory: held_fs.HeldDirectory | None = None
        try:
            for _attempt in range(16):
                name = f".exomem-batch-{_batch_workspace_token(16)}"
                workspace_relative = (
                    name
                    if parent_relative == "."
                    else f"{parent_relative}/{name}"
                )
                created = filesystem.parent(
                    workspace_relative,
                    create=True,
                    exclusive=True,
                    access="read",
                )
                if created.ok:
                    held_directory = created.require()
                    break
                if (
                    created.error is not None
                    and created.error.code == "DESTINATION_EXISTS"
                ):
                    continue
                raise PathGuardError(
                    "PATH_GUARD_UNSAFE", "batch workspace allocation failed"
                )
            else:  # pragma: no cover - cryptographic collisions are not practical
                raise PathGuardError(
                    "PATH_GUARD_IO", "batch workspace allocation failed"
                )
            parent_descriptor = getattr(held_parent, "descriptor", None)
            workspace_descriptor = getattr(held_directory, "descriptor", None)
            if not isinstance(parent_descriptor, int) or not isinstance(
                workspace_descriptor, int
            ):
                raise PathGuardError(
                    "PATH_GUARD_UNSAFE", "held workspace descriptors are unavailable"
                )
            parent_info = os.fstat(parent_descriptor)
            workspace_info = os.fstat(workspace_descriptor)
            workspace = cls(
                absolute_parent,
                name,
                parent_descriptor,
                workspace_descriptor,
                _identity(".", parent_info),
                _identity(name, workspace_info),
                {},
                filesystem,
                held_parent,
                held_directory,
                workspace_relative,
            )
            if os.name != "nt" and hasattr(os, "fchmod"):
                os.fchmod(workspace_descriptor, 0o700)
            workspace.refresh_identity()
            return workspace
        except BaseException:
            if held_directory is not None:
                _remove_held_workspace(
                    filesystem,
                    workspace_relative,
                    held_directory.identity,
                )
                held_directory.close()
            held_parent.close()
            filesystem.close()
            raise

    def stat_child(self, name: str) -> os.stat_result:
        if self.held_filesystem is not None and self.held_directory is not None:
            opened = self.held_filesystem.file(self.held_directory, name)
            if not opened.ok:
                raise OSError(errno.ESTALE, "batch stage changed")
            with opened.require() as child:
                descriptor = getattr(child, "descriptor", None)
                if not isinstance(descriptor, int):
                    raise OSError(errno.ESTALE, "batch stage changed")
                return os.fstat(descriptor)
        if os.stat in getattr(os, "supports_dir_fd", set()):
            return os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)
        return (self.path / name).lstat()  # pragma: no cover - Windows fallback

    def recheck_identity(self) -> None:
        if self.closed:
            raise PathGuardError("PATH_GUARD_CHANGED", "batch workspace handle is closed")
        if (
            self.held_filesystem is not None
            and self.held_parent is not None
            and self.held_directory is not None
        ):
            parent_valid = self.held_filesystem.validate_directory(
                self.held_parent,
                require_name=getattr(self.held_parent, "named", False),
            )
            workspace_valid = self.held_filesystem.validate_directory(
                self.held_directory
            )
            if not parent_valid.ok or not workspace_valid.ok:
                raise PathGuardError("PATH_GUARD_CHANGED", "batch workspace changed")
            try:
                parent_info = os.fstat(self.parent_descriptor)
                workspace_info = os.fstat(self.descriptor)
            except OSError as error:
                raise PathGuardError(
                    "PATH_GUARD_CHANGED", "batch workspace changed"
                ) from error
            if (
                not _same_identity(self.parent_identity, parent_info)
                or not _same_identity(self.identity, workspace_info)
                or not stat.S_ISDIR(parent_info.st_mode)
                or not stat.S_ISDIR(workspace_info.st_mode)
            ):
                raise PathGuardError("PATH_GUARD_CHANGED", "batch workspace changed")
            return
        try:
            parent_descriptor_info = os.fstat(self.parent_descriptor)
            workspace_descriptor_info = os.fstat(self.descriptor)
            parent_path_info = self.parent.lstat()
            workspace_path_info = self.path.lstat()
        except OSError as error:
            raise PathGuardError("PATH_GUARD_CHANGED", "batch workspace changed") from error
        if (
            not _same_identity(self.parent_identity, parent_descriptor_info)
            or not _same_identity(self.parent_identity, parent_path_info)
            or not stat.S_ISDIR(parent_path_info.st_mode)
            or stat.S_ISLNK(parent_path_info.st_mode)
            or _is_reparse(parent_path_info)
            or not _same_identity(self.identity, workspace_descriptor_info)
            or not _same_identity(self.identity, workspace_path_info)
            or not stat.S_ISDIR(workspace_path_info.st_mode)
            or stat.S_ISLNK(workspace_path_info.st_mode)
            or _is_reparse(workspace_path_info)
        ):
            raise PathGuardError("PATH_GUARD_CHANGED", "batch workspace changed")

    def refresh_identity(self) -> None:
        if self.closed:
            raise PathGuardError("PATH_GUARD_CHANGED", "batch workspace handle is closed")
        if self.held_filesystem is not None:
            self.recheck_identity()
            workspace_info = os.fstat(self.descriptor)
            refreshed = _identity(self.name, workspace_info)
            if not _same_file_object(self.identity, refreshed):
                raise PathGuardError("PATH_GUARD_CHANGED", "batch workspace changed")
            self.identity = refreshed
            self.recheck()
            return
        try:
            parent_descriptor_info = os.fstat(self.parent_descriptor)
            workspace_descriptor_info = os.fstat(self.descriptor)
            parent_path_info = self.parent.lstat()
            workspace_path_info = self.path.lstat()
        except OSError as error:
            raise PathGuardError("PATH_GUARD_CHANGED", "batch workspace changed") from error
        refreshed = _identity(self.name, workspace_descriptor_info)
        if (
            not _same_identity(self.parent_identity, parent_descriptor_info)
            or not _same_identity(self.parent_identity, parent_path_info)
            or not stat.S_ISDIR(parent_path_info.st_mode)
            or stat.S_ISLNK(parent_path_info.st_mode)
            or _is_reparse(parent_path_info)
            or not _same_file_object(self.identity, refreshed)
            or not _same_identity(refreshed, workspace_path_info)
            or not stat.S_ISDIR(workspace_descriptor_info.st_mode)
            or _is_reparse(workspace_descriptor_info)
            or stat.S_ISLNK(workspace_path_info.st_mode)
            or _is_reparse(workspace_path_info)
        ):
            raise PathGuardError("PATH_GUARD_CHANGED", "batch workspace changed")
        self.identity = refreshed
        self.recheck()

    def recheck(self) -> None:
        if self.held_filesystem is not None and self.held_directory is not None:
            self.recheck_identity()
            enumerated = self.held_filesystem.enumerate(self.held_directory)
            if not enumerated.ok:
                raise PathGuardError("PATH_GUARD_CHANGED", "batch workspace changed")
            seen: set[str] = set()
            for record in enumerated.require():
                artifact = self.artifacts.get(record.relative_path)
                if (
                    artifact is None
                    or "/" in record.relative_path
                    or record.identity.kind != "file"
                    or record.identity.link_count != 1
                    or artifact.identity.device != record.identity.device
                    or artifact.identity.inode != record.identity.inode
                ):
                    raise PathGuardError(
                        "PATH_GUARD_CHANGED", "batch workspace census changed"
                    )
                seen.add(record.relative_path)
            if seen != self.artifacts.keys():
                raise PathGuardError(
                    "PATH_GUARD_CHANGED", "batch workspace census changed"
                )
            self.recheck_identity()
            return
        self.recheck_identity()
        iterator = None
        try:
            descriptor_relative = os.scandir in getattr(os, "supports_fd", set())
            iterator = os.scandir(self.descriptor if descriptor_relative else self.path)
            seen: set[str] = set()
            for entry in iterator:
                name = entry.name
                artifact = self.artifacts.get(name)
                if artifact is None:
                    raise PathGuardError("PATH_GUARD_CHANGED", "batch workspace census changed")
                info = self.stat_child(name)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or stat.S_ISLNK(info.st_mode)
                    or _is_reparse(info)
                    or not _same_identity(artifact.identity, info)
                ):
                    raise PathGuardError("PATH_GUARD_CHANGED", "batch stage changed")
                seen.add(name)
            if seen != self.artifacts.keys():
                raise PathGuardError("PATH_GUARD_CHANGED", "batch workspace census changed")
            self.recheck_identity()
        finally:
            if iterator is not None:
                iterator.close()

    def create_artifact(self, name: str, content: bytes) -> _WorkspaceArtifact:
        self.recheck()
        held_file: held_fs.HeldFile | None = None
        if self.held_filesystem is not None and self.held_directory is not None:
            opened = self.held_filesystem.file(
                self.held_directory,
                name,
                access="write",
                create=True,
                exclusive=True,
            )
            if not opened.ok:
                raise PathGuardError("PATH_GUARD_UNSAFE", "batch stage create was refused")
            held_file = opened.require()
            descriptor = getattr(held_file, "descriptor", None)
            if not isinstance(descriptor, int):
                held_file.close()
                raise PathGuardError("PATH_GUARD_UNSAFE", "batch stage is unavailable")
        else:
            flags = (
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            if os.open in getattr(os, "supports_dir_fd", set()):
                descriptor = os.open(name, flags, 0o600, dir_fd=self.descriptor)
            else:  # pragma: no cover - legacy rootless Windows route
                descriptor = os.open(self.path / name, flags, 0o600)
        artifact: _WorkspaceArtifact | None = None
        try:
            self.recheck_identity()
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or _is_reparse(info):
                raise PathGuardError("PATH_GUARD_UNSAFE", "batch stage is unsafe")
            identity = _identity(name, info)
            digest = hashlib.sha256(content).hexdigest()
            artifact = _WorkspaceArtifact(
                self,
                name,
                descriptor,
                identity,
                digest,
                held_file=held_file,
            )
            self.artifacts[name] = artifact
            _write_all(descriptor, content)
            if _descriptor_hash(descriptor, identity) != digest:
                raise PathGuardError("PATH_GUARD_CONTENT", "batch stage content changed")
            artifact.content_bound = True
            artifact.recheck()
            self.recheck()
            return artifact
        except Exception:
            if artifact is None:
                if held_file is not None:
                    held_file.close()
                else:
                    os.close(descriptor)
            raise

    def _replace_once(self, artifact: _WorkspaceArtifact, final: Path) -> None:
        if os.replace in getattr(os, "supports_dir_fd", set()):
            os.replace(
                artifact.name,
                final.name,
                src_dir_fd=self.descriptor,
                dst_dir_fd=self.parent_descriptor,
            )
        else:  # pragma: no cover - Windows fallback
            os.replace(artifact.path, final)

    def replace_artifact(
        self,
        artifact: _WorkspaceArtifact,
        final: Path,
        *,
        vault_root: Path | None = None,
        expected_destination: PathIdentity | None = None,
        published_metadata: _BatchSnapshot | None = None,
    ) -> PathIdentity:
        context = _BATCH_REPLACE_CONTEXT.get()
        if context is not None:
            if vault_root is None:
                vault_root = context.vault_root
            if expected_destination is None:
                expected_destination = context.expected_destination
            if published_metadata is None:
                published_metadata = context.published_metadata
        self.recheck()
        artifact.recheck()
        if vault_root is not None:
            root = Path(os.path.abspath(vault_root))
            absolute_final = Path(os.path.abspath(final))
            try:
                relative = absolute_final.relative_to(root)
            except ValueError as error:
                raise PathGuardError(
                    "PATH_GUARD_TARGET", "batch destination escaped the vault"
                ) from error
            content = _descriptor_bytes(artifact.descriptor, artifact.identity)
            acquired = held_fs.acquire(root)
            if not acquired.ok:
                raise PathGuardError(
                    "PATH_GUARD_UNSAFE", "held filesystem route is unavailable"
                )
            with acquired.require() as filesystem:
                parent_relative = relative.parent.as_posix()
                parent_result = filesystem.parent(
                    parent_relative if parent_relative != "." else "."
                )
                if not parent_result.ok:
                    raise PathGuardError(
                        "PATH_GUARD_UNSAFE", "batch destination parent is unsafe"
                    )
                with parent_result.require() as parent:
                    expected_stable: held_fs.StableIdentity | None = None
                    if expected_destination is not None:
                        existing_result = filesystem.file(parent, relative.name)
                        if not existing_result.ok:
                            raise PathGuardError(
                                "PATH_GUARD_CHANGED", "batch destination changed"
                            )
                        with existing_result.require() as existing:
                            descriptor = getattr(existing, "descriptor", None)
                            if not isinstance(descriptor, int):
                                raise PathGuardError(
                                    "PATH_GUARD_UNSAFE", "batch destination is unavailable"
                                )
                            info = os.fstat(descriptor)
                            if (
                                not _same_identity(expected_destination, info)
                                or existing.identity.link_count != 1
                            ):
                                raise PathGuardError(
                                    "PATH_GUARD_CHANGED", "batch destination changed"
                                )
                            expected_stable = existing.identity
                    published: held_fs.HeldResult[held_fs.StableIdentity] | None = None

                    def recheck_publish_precondition() -> None:
                        self.recheck()
                        artifact.recheck()
                        current = filesystem.file(parent, relative.name)
                        if expected_stable is None:
                            if current.ok:
                                current.require().close()
                                raise PathGuardError(
                                    "PATH_GUARD_CHANGED", "batch destination appeared"
                                )
                            if current.error is None or current.error.code != "MISSING":
                                raise PathGuardError(
                                    "PATH_GUARD_CHANGED", "batch destination changed"
                                )
                            return
                        if not current.ok:
                            raise PathGuardError(
                                "PATH_GUARD_CHANGED", "batch destination changed"
                            )
                        with current.require() as checked:
                            if (
                                checked.identity != expected_stable
                                or checked.identity.link_count != 1
                            ):
                                raise PathGuardError(
                                    "PATH_GUARD_CHANGED", "batch destination changed"
                                )

                    def publish_once() -> None:
                        nonlocal published

                        def prepare_published(file: held_fs.HeldFile) -> None:
                            if published_metadata is not None:
                                _apply_held_snapshot_metadata(file, published_metadata)

                        _before_batch_destination_published(
                            final, restoring=published_metadata is not None
                        )
                        published = held_fs.publish_bytes(
                            filesystem,
                            parent,
                            relative.name,
                            content,
                            expected_identity=expected_stable,
                            prepare=prepare_published,
                        )
                        if (
                            not published.ok
                            and published.error is not None
                            and published.error.code == "IO_REFUSED"
                            and published.error.cause is not None
                        ):
                            cause = published.error.cause
                            if isinstance(cause, PermissionError):
                                source_leaf = published.error.source_leaf
                                if not isinstance(cause.filename, str):
                                    if source_leaf is not None:
                                        cause.filename = os.fspath(final.parent / source_leaf)
                                elif not Path(cause.filename).is_absolute():
                                    cause.filename = os.fspath(final.parent / cause.filename)
                                if not isinstance(cause.filename2, str) or not Path(
                                    cause.filename2
                                ).is_absolute():
                                    cause.filename2 = os.fspath(final)
                            raise cause

                    replace_tolerating_transient_sharing(
                        publish_once,
                        recheck=recheck_publish_precondition,
                    )
                    if published is None:  # pragma: no cover - wrapper always calls once
                        raise AssertionError("held publication did not run")
                    if not published.ok:
                        code = (
                            "PATH_GUARD_CHANGED"
                            if published.error is not None
                            and published.error.code
                            in {"DESTINATION_EXISTS", "IDENTITY_CHANGED"}
                            else "PATH_GUARD_UNSAFE"
                        )
                        raise PathGuardError(code, "batch destination publish was refused")
                    installed_result = filesystem.file(parent, relative.name)
                    if not installed_result.ok:
                        raise PathGuardError(
                            "PATH_GUARD_CHANGED", "batch destination is unavailable"
                        )
                    with installed_result.require() as installed:
                        descriptor = getattr(installed, "descriptor", None)
                        if not isinstance(descriptor, int):
                            raise PathGuardError(
                                "PATH_GUARD_UNSAFE", "batch destination is unavailable"
                            )
                        installed_info = os.fstat(descriptor)
                        if installed.identity != published.require():
                            raise PathGuardError(
                                "PATH_GUARD_CHANGED", "batch destination changed"
                            )
                        return _identity(relative.as_posix(), installed_info)

        identity = artifact.identity
        if os.name == "nt":  # pragma: no cover - Windows does not replace open CRT files
            artifact.close()
        # Re-pin the workspace directory on every attempt so waiting out a reader
        # never widens the window this guard covers. The artifact itself is
        # already closed on Windows and its identity was proved immediately
        # above, and the mutation boundary is held throughout, so no other
        # governed writer can intervene.
        replace_tolerating_transient_sharing(
            lambda: self._replace_once(artifact, final), recheck=self.recheck
        )
        artifact.close()
        self.artifacts.pop(artifact.name)
        return identity

    def bind_installed_after_error(
        self,
        artifact: _WorkspaceArtifact,
        final: Path,
        *,
        expected_destination: PathIdentity | None = None,
    ) -> PathIdentity | None:
        """Record a flip whose wrapper raised after the kernel replacement."""
        try:
            absolute_final = Path(os.path.abspath(final))
            if absolute_final.parent != self.parent:
                return None
            if self.held_filesystem is not None and self.held_parent is not None:
                self.recheck()
                artifact.recheck()
                current = self.held_filesystem.file(
                    self.held_parent,
                    absolute_final.name,
                )
                if not current.ok:
                    return None
                with current.require() as installed:
                    descriptor = getattr(installed, "descriptor", None)
                    if not isinstance(descriptor, int) or installed.identity.link_count != 1:
                        return None
                    info = os.fstat(descriptor)
                    installed_identity = _identity(absolute_final.name, info)
                    if expected_destination is not None and _same_file_object(
                        expected_destination, installed_identity
                    ):
                        return None
                    read = self.held_filesystem.read(installed)
                    if (
                        not read.ok
                        or hashlib.sha256(read.require()).hexdigest()
                        != artifact.content_hash
                    ):
                        return None
                self.recheck()
                artifact.recheck()
                return installed_identity
            self.recheck_identity()
            if artifact.closed:
                if os.path.lexists(artifact.path):
                    descriptor = os.open(
                        artifact.path,
                        os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    )
                    try:
                        descriptor_info = os.fstat(descriptor)
                        path_info = artifact.path.lstat()
                        if (
                            not stat.S_ISREG(descriptor_info.st_mode)
                            or _is_reparse(descriptor_info)
                            or not _same_identity(artifact.identity, descriptor_info)
                            or not _same_identity(artifact.identity, path_info)
                            or stat.S_ISLNK(path_info.st_mode)
                            or _is_reparse(path_info)
                            or _descriptor_hash(descriptor, artifact.identity)
                            != artifact.content_hash
                        ):
                            return None
                        artifact.descriptor = descriptor
                        artifact.closed = False
                        descriptor = -1
                        artifact.recheck()
                        self.recheck()
                        return None
                    finally:
                        if descriptor >= 0:
                            os.close(descriptor)
                installed = _BatchArtifactGuard.capture(
                    final,
                    expected_content_hash=artifact.content_hash,
                    expected_identity=artifact.identity,
                )
                installed.recheck()
                self.artifacts.pop(artifact.name, None)
                self.recheck()
                return artifact.identity
            descriptor_info = os.fstat(artifact.descriptor)
            final_info = final.lstat()
            if (
                not stat.S_ISREG(descriptor_info.st_mode)
                or _is_reparse(descriptor_info)
                or not _same_identity(artifact.identity, descriptor_info)
                or _descriptor_hash(artifact.descriptor, artifact.identity) != artifact.content_hash
                or not stat.S_ISREG(final_info.st_mode)
                or stat.S_ISLNK(final_info.st_mode)
                or _is_reparse(final_info)
                or not _same_identity(artifact.identity, final_info)
                or os.path.lexists(artifact.path)
            ):
                return None
            artifact.close()
            self.artifacts.pop(artifact.name)
            self.recheck()
            return artifact.identity
        except (OSError, PathGuardError):
            return None

    def unlink_installed(self, final: Path, expected: PathIdentity) -> None:
        """Remove one exact batch-created destination through its held parent."""

        self.recheck()
        absolute_final = Path(os.path.abspath(final))
        if absolute_final.parent != self.parent:
            raise PathGuardError(
                "PATH_GUARD_TARGET", "batch rollback target changed parent"
            )
        if self.held_filesystem is not None and self.held_parent is not None:
            current = self.held_filesystem.file(
                self.held_parent,
                absolute_final.name,
                access="mutate",
            )
            if not current.ok:
                raise PathGuardError(
                    "PATH_GUARD_CHANGED", "batch rollback target disappeared"
                )
            with current.require() as installed:
                descriptor = getattr(installed, "descriptor", None)
                if (
                    not isinstance(descriptor, int)
                    or not _same_identity(expected, os.fstat(descriptor))
                    or installed.identity.link_count != 1
                ):
                    raise PathGuardError(
                        "PATH_GUARD_CHANGED", "batch rollback target changed"
                    )
                removed = self.held_filesystem.unlink(installed)
                if not removed.ok:
                    raise PathGuardError(
                        "PATH_GUARD_UNSAFE", "batch rollback unlink was refused"
                    )
            missing = self.held_filesystem.file(self.held_parent, absolute_final.name)
            if missing.ok:
                missing.require().close()
                raise PathGuardError(
                    "PATH_GUARD_CHANGED", "committed batch artifact remains"
                )
            if missing.error is None or missing.error.code != "MISSING":
                raise PathGuardError(
                    "PATH_GUARD_UNSAFE", "batch rollback absence was not proven"
                )
            self.recheck()
            return
        if os.unlink in getattr(os, "supports_dir_fd", set()):
            os.unlink(absolute_final.name, dir_fd=self.parent_descriptor)
        else:  # pragma: no cover - legacy rootless Windows route
            absolute_final.unlink()
        if os.path.lexists(absolute_final):
            raise PathGuardError(
                "PATH_GUARD_CHANGED", "committed batch artifact remains"
            )
        self.recheck()

    def remove_artifact(self, artifact: _WorkspaceArtifact) -> bool:
        try:
            self.recheck()
            artifact.bind_initializing_content()
            artifact.recheck()
            if self.held_filesystem is not None and self.held_directory is not None:
                expected = artifact.identity
                artifact.close()
                mutable = self.held_filesystem.file(
                    self.held_directory,
                    artifact.name,
                    access="mutate",
                )
                if not mutable.ok:
                    return False
                with mutable.require() as current:
                    descriptor = getattr(current, "descriptor", None)
                    if (
                        not isinstance(descriptor, int)
                        or not _same_identity(expected, os.fstat(descriptor))
                    ):
                        return False
                    removed = self.held_filesystem.unlink(current)
                    if not removed.ok:
                        return False
                self.artifacts.pop(artifact.name)
                self.recheck()
                return True
            if os.name == "nt":  # Windows cannot unlink an open CRT file
                artifact.close()
            if os.unlink in getattr(os, "supports_dir_fd", set()):
                os.unlink(artifact.name, dir_fd=self.descriptor)
            else:  # pragma: no cover - Windows fallback
                artifact.path.unlink()
            if os.path.lexists(artifact.path):
                return False
            artifact.close()
            self.artifacts.pop(artifact.name)
            self.recheck()
            return True
        except (OSError, PathGuardError):
            return False

    def cleanup(self) -> bool:
        try:
            self.recheck()
        except PathGuardError:
            self.close()
            return False
        for artifact in tuple(self.artifacts.values()):
            if not self.remove_artifact(artifact):
                self.close()
                return False
        if self.held_filesystem is not None and self.held_directory is not None:
            try:
                self.recheck()
                if self.held_workspace_relative is None:
                    raise PathGuardError(
                        "PATH_GUARD_CHANGED",
                        "held workspace name is unavailable",
                    )
                removed = _remove_held_workspace(
                    self.held_filesystem,
                    self.held_workspace_relative,
                    self.held_directory.identity,
                )
            except PathGuardError:
                removed = False
            self.close()
            return removed
        try:
            self.recheck()
            if os.rmdir in getattr(os, "supports_dir_fd", set()):
                os.rmdir(self.name, dir_fd=self.parent_descriptor)
            else:  # pragma: no cover - Windows fallback
                self.path.rmdir()
            removed = not os.path.lexists(self.path)
        except (OSError, PathGuardError):
            removed = False
        self.close()
        return removed

    def close(self) -> None:
        if self.closed:
            return
        for artifact in self.artifacts.values():
            artifact.close()
        if self.held_directory is not None:
            self.held_directory.close()
        if self.held_parent is not None:
            self.held_parent.close()
        if self.held_filesystem is not None:
            self.held_filesystem.close()
        else:
            os.close(self.descriptor)
            os.close(self.parent_descriptor)
        self.closed = True


def _open_bound_artifact(artifact: _BatchArtifactGuard) -> int:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(artifact.path, flags)
    except OSError as error:
        raise PathGuardError("PATH_GUARD_IO", "batch source could not be opened") from error
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or _is_reparse(info)
            or not _same_identity(artifact.identity, info)
        ):
            raise PathGuardError("PATH_GUARD_CHANGED", "batch source changed")
        artifact.recheck()
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _capture_batch_snapshot(path: Path) -> tuple[_BatchSnapshot, _BatchArtifactGuard]:
    absolute = Path(os.path.abspath(path))
    stable_guard = PathGuard.capture(absolute.parent, absolute.name, leaf_policy="stable")
    stable_artifact = _BatchArtifactGuard(absolute.parent, stable_guard)
    source_descriptor = _open_bound_artifact(stable_artifact)
    try:
        source_info = os.fstat(source_descriptor)
        try:
            xattrs = _capture_descriptor_xattrs(source_descriptor)
            content = _descriptor_bytes(source_descriptor, stable_artifact.identity)
            content_hash = hashlib.sha256(content).hexdigest()
            source_guard = _BatchArtifactGuard.capture(
                absolute,
                expected_content_hash=content_hash,
                expected_identity=stable_artifact.identity,
            )
            source_guard.recheck()
            if not _same_identity(source_guard.identity, os.fstat(source_descriptor)):
                raise PathGuardError("PATH_GUARD_CHANGED", "batch source changed")
            _restore_bound_source_timestamps(
                source_guard,
                source_descriptor,
                source_info.st_atime_ns,
                source_info.st_mtime_ns,
            )
        except BaseException as capture_error:
            try:
                _restore_bound_source_timestamps(
                    stable_artifact,
                    source_descriptor,
                    source_info.st_atime_ns,
                    source_info.st_mtime_ns,
                )
            except BaseException as restore_error:
                raise restore_error from capture_error
            raise
        return (
            _BatchSnapshot(
                content,
                content_hash,
                stat.S_IMODE(source_info.st_mode),
                source_info.st_atime_ns,
                source_info.st_mtime_ns,
                xattrs,
            ),
            source_guard,
        )
    finally:
        os.close(source_descriptor)


def _restore_bound_source_timestamps(
    source: _BatchArtifactGuard,
    descriptor: int,
    atime_ns: int,
    mtime_ns: int,
) -> None:
    source.recheck()
    before = os.fstat(descriptor)
    if not _same_identity(source.identity, before):
        raise PathGuardError("PATH_GUARD_CHANGED", "batch source changed")
    if os.utime in getattr(os, "supports_fd", set()):
        os.utime(descriptor, ns=(atime_ns, mtime_ns))
    elif os.utime in getattr(os, "supports_follow_symlinks", set()):
        os.utime(source.path, ns=(atime_ns, mtime_ns), follow_symlinks=False)
    elif os.name == "nt":
        _set_windows_path_timestamps(
            source.path,
            source.identity,
            atime_ns,
            mtime_ns,
            verify_atime=False,
        )
    else:  # pragma: no cover - supported Python platforms expose one safe form
        raise PathGuardError("PATH_GUARD_IO", "batch timestamp restore is unavailable")
    restored = os.fstat(descriptor)
    restored_path = source.path.lstat()
    if (
        not _same_identity(source.identity, restored)
        or not _same_identity(source.identity, restored_path)
        or not stat.S_ISREG(restored_path.st_mode)
        or stat.S_ISLNK(restored_path.st_mode)
        or _is_reparse(restored_path)
        or restored.st_mtime_ns != mtime_ns
        or restored_path.st_mtime_ns != mtime_ns
    ):
        raise PathGuardError("PATH_GUARD_CHANGED", "batch metadata capture changed")


def _apply_workspace_mode(artifact: _WorkspaceArtifact, mode: int) -> None:
    descriptor = artifact.descriptor
    fchmod = getattr(os, "fchmod", None)
    if callable(fchmod):
        fchmod(descriptor, mode)
        return
    if os.chmod in getattr(os, "supports_fd", set()):  # pragma: no cover
        os.chmod(descriptor, mode)
        return
    artifact.recheck(verify_content=False)
    if os.chmod in getattr(os, "supports_dir_fd", set()):
        kwargs: dict[str, Any] = {"dir_fd": artifact.workspace.descriptor}
        if os.chmod in getattr(os, "supports_follow_symlinks", set()):
            kwargs["follow_symlinks"] = False
        os.chmod(artifact.name, mode, **kwargs)
    elif os.chmod in getattr(os, "supports_follow_symlinks", set()):
        os.chmod(artifact.path, mode, follow_symlinks=False)
    else:  # pragma: no cover - platform has no exact path-based chmod
        raise PathGuardError("PATH_GUARD_IO", "batch mode restore is unavailable")
    artifact.refresh_identity()


def _apply_workspace_timestamps(artifact: _WorkspaceArtifact, atime_ns: int, mtime_ns: int) -> None:
    descriptor = artifact.descriptor
    if os.utime in getattr(os, "supports_fd", set()):
        os.utime(descriptor, ns=(atime_ns, mtime_ns))
        return
    artifact.recheck(verify_content=False)
    if os.utime in getattr(os, "supports_dir_fd", set()):
        kwargs: dict[str, Any] = {"dir_fd": artifact.workspace.descriptor}
        if os.utime in getattr(os, "supports_follow_symlinks", set()):
            kwargs["follow_symlinks"] = False
        os.utime(artifact.name, ns=(atime_ns, mtime_ns), **kwargs)
    elif os.utime in getattr(os, "supports_follow_symlinks", set()):
        os.utime(
            artifact.path,
            ns=(atime_ns, mtime_ns),
            follow_symlinks=False,
        )
    elif os.name == "nt":
        _set_windows_path_timestamps(artifact.path, artifact.identity, atime_ns, mtime_ns)
    else:  # pragma: no cover - platform has no exact path-based utime
        raise PathGuardError("PATH_GUARD_IO", "batch timestamp restore is unavailable")
    artifact.refresh_identity()


def _apply_snapshot_metadata(artifact: _WorkspaceArtifact, snapshot: _BatchSnapshot) -> None:
    artifact.recheck()
    descriptor = artifact.descriptor
    _apply_workspace_mode(artifact, snapshot.mode)
    artifact.refresh_identity()
    if snapshot.xattrs is not None:
        current = _capture_descriptor_xattrs(descriptor)
        if current is None:
            raise PathGuardError("PATH_GUARD_IO", "batch metadata restore is unavailable")
        extras = current.keys() - snapshot.xattrs.keys()
        if extras and not hasattr(os, "removexattr"):
            raise PathGuardError("PATH_GUARD_IO", "batch metadata restore is unavailable")
        for name in sorted(extras, key=os.fsencode):
            os.removexattr(descriptor, name)
        for name, value in snapshot.xattrs.items():
            os.setxattr(descriptor, name, value)
    _apply_workspace_timestamps(artifact, snapshot.atime_ns, snapshot.mtime_ns)
    info = os.fstat(descriptor)
    if stat.S_IMODE(info.st_mode) != snapshot.mode:
        raise PathGuardError("PATH_GUARD_CHANGED", "batch metadata restore changed")
    if info.st_atime_ns != snapshot.atime_ns or info.st_mtime_ns != snapshot.mtime_ns:
        raise PathGuardError("PATH_GUARD_CHANGED", "batch metadata restore changed")
    if snapshot.xattrs is not None and _capture_descriptor_xattrs(descriptor) != snapshot.xattrs:
        raise PathGuardError("PATH_GUARD_CHANGED", "batch metadata restore changed")
    artifact.refresh_identity()


def _apply_held_snapshot_metadata(
    file: held_fs.HeldFile,
    snapshot: _BatchSnapshot,
) -> None:
    descriptor = getattr(file, "descriptor", None)
    if not isinstance(descriptor, int):
        raise PathGuardError("PATH_GUARD_IO", "batch metadata restore is unavailable")
    before = os.fstat(descriptor)
    expected = _identity(".", before)
    fchmod = getattr(os, "fchmod", None)
    if callable(fchmod):
        fchmod(descriptor, snapshot.mode)
    elif os.chmod in getattr(os, "supports_fd", set()):  # pragma: no cover
        os.chmod(descriptor, snapshot.mode)
    elif os.name != "nt":  # pragma: no cover - supported POSIX exposes fchmod
        raise PathGuardError("PATH_GUARD_IO", "batch metadata restore is unavailable")
    if snapshot.xattrs is not None:
        current = _capture_descriptor_xattrs(descriptor)
        if current is None:
            raise PathGuardError("PATH_GUARD_IO", "batch metadata restore is unavailable")
        extras = current.keys() - snapshot.xattrs.keys()
        if extras and not hasattr(os, "removexattr"):
            raise PathGuardError("PATH_GUARD_IO", "batch metadata restore is unavailable")
        for name in sorted(extras, key=os.fsencode):
            os.removexattr(descriptor, name)
        for name, value in snapshot.xattrs.items():
            os.setxattr(descriptor, name, value)
    if os.utime in getattr(os, "supports_fd", set()):
        os.utime(descriptor, ns=(snapshot.atime_ns, snapshot.mtime_ns))
    elif os.name == "nt":
        _set_windows_descriptor_timestamps(
            descriptor,
            expected,
            snapshot.atime_ns,
            snapshot.mtime_ns,
        )
    else:  # pragma: no cover - supported POSIX exposes descriptor utime
        raise PathGuardError("PATH_GUARD_IO", "batch timestamp restore is unavailable")
    restored = os.fstat(descriptor)
    if (
        not _same_file_object(expected, _identity(".", restored))
        or (os.name != "nt" and stat.S_IMODE(restored.st_mode) != snapshot.mode)
        or restored.st_mtime_ns != snapshot.mtime_ns
        or (
            snapshot.xattrs is not None
            and _capture_descriptor_xattrs(descriptor) != snapshot.xattrs
        )
    ):
        raise PathGuardError("PATH_GUARD_CHANGED", "batch metadata restore changed")


def _reset_restored_timestamps(
    path: Path, expected_identity: PathIdentity, snapshot: _BatchSnapshot
) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or _is_reparse(info)
            or not _same_identity(expected_identity, info)
        ):
            raise PathGuardError("PATH_GUARD_CHANGED", "restored batch artifact changed")
        if os.utime in getattr(os, "supports_fd", set()):
            os.utime(descriptor, ns=(snapshot.atime_ns, snapshot.mtime_ns))
        elif os.name == "nt":
            _set_windows_path_timestamps(
                path,
                expected_identity,
                snapshot.atime_ns,
                snapshot.mtime_ns,
                verify_atime=False,
            )
        elif os.utime in getattr(os, "supports_follow_symlinks", set()):
            os.utime(
                path,
                ns=(snapshot.atime_ns, snapshot.mtime_ns),
                follow_symlinks=False,
            )
        elif os.utime in getattr(os, "supports_dir_fd", set()):
            parent_descriptor = _open_directory_path(path.parent)
            try:
                parent_info = path.parent.lstat()
                if not _same_identity(_identity(".", parent_info), os.fstat(parent_descriptor)):
                    raise PathGuardError("PATH_GUARD_CHANGED", "restored batch parent changed")
                os.utime(
                    path.name,
                    ns=(snapshot.atime_ns, snapshot.mtime_ns),
                    dir_fd=parent_descriptor,
                )
            finally:
                os.close(parent_descriptor)
        else:  # pragma: no cover - platform has no exact timestamp operation
            raise PathGuardError("PATH_GUARD_IO", "batch timestamp restore is unavailable")
        restored = os.fstat(descriptor)
        restored_path = path.lstat()
        if (
            not _same_identity(expected_identity, restored)
            or not _same_identity(expected_identity, restored_path)
            or restored.st_mtime_ns != snapshot.mtime_ns
            or restored_path.st_mtime_ns != snapshot.mtime_ns
        ):
            raise PathGuardError("PATH_GUARD_CHANGED", "batch metadata restore changed")
    finally:
        os.close(descriptor)


def _cleanup_batch_workspaces(
    workspaces: Iterable[_BatchWorkspace],
    *,
    retained: Iterable[_BatchWorkspace] = (),
) -> bool:
    retained_ids = {id(workspace) for workspace in retained}
    cleanup_retained = False
    for workspace in workspaces:
        try:
            if id(workspace) in retained_ids:
                for artifact in tuple(workspace.artifacts.values()):
                    if artifact.name.startswith("stage-") and not workspace.remove_artifact(
                        artifact
                    ):
                        cleanup_retained = True
                workspace.close()
                cleanup_retained = True
                continue
            if not workspace.cleanup():
                cleanup_retained = True
        except Exception:  # noqa: BLE001 - continue every independent cleanup
            cleanup_retained = True
            try:
                workspace.close()
            except Exception:  # noqa: BLE001 - the public outcome remains bounded
                pass
    return cleanup_retained


class _BatchCleanupRetained(RuntimeError):
    """Private marker for initialization paths that could not clean safely."""


def _raise_cleanup_retained(primary_error: BaseException | None = None) -> None:
    error = _BatchCleanupRetained("batch cleanup retained changed artifacts")
    if primary_error is None:
        raise error
    raise error from primary_error


def _recheck_rollback_directory_guards(
    guards: Iterable[DirectoryCensusGuard],
    vault_root: Path,
    final: Path,
    *,
    allowed_changes: Iterable[Path],
) -> None:
    """Recheck censuses whose guarded namespace contains this direct child."""
    final_parent = os.path.abspath(final.parent)
    for guard in guards:
        guarded_directory = Path(vault_root) / guard.target
        if final_parent == os.path.abspath(guarded_directory):
            guard.recheck(vault_root, allowed_changes=allowed_changes)


#: Windows refuses `os.replace` onto a target another handle holds open without
#: FILE_SHARE_DELETE (ERROR_SHARING_VIOLATION, 32) and reports
#: ERROR_ACCESS_DENIED (5) for a target already marked delete-pending. Both are
#: *transient by nature*: the holder is a reader that closes microseconds later.
#:
#: exomem's own derived-index readers no longer pin canonical pages -- see
#: `read_bytes_without_pinning` -- but this writer cannot assume it is the only
#: process on a user's vault. Obsidian, OneDrive, a backup agent and an
#: antivirus scanner all open Markdown files, and on Windows any one of them
#: turns an ordinary governed write into RECORD_PUBLICATION_FAILED. Waiting out
#: a transient reader is the correct platform accommodation, not a workaround.
#:
#: ~200 ms of total patience: long enough that no realistic reader outlives it,
#: short enough that a genuinely pinned file still fails inside the request
#: rather than hanging it. POSIX never enters the loop -- `rename(2)` over an
#: open file is always permitted -- so this costs nothing there.
#:
#: Measured headroom, against a reader re-opening the same page in a tight loop
#: with no pause -- far more hostile than any real rebuild, which reads a page
#: once and moves on: 200 consecutive replacements all succeeded, 96 needed at
#: least one retry, and the worst needed 8 of the 20 attempts.
_WINDOWS_SHARING_ERRORS = frozenset({5, 32})
_REPLACE_SHARING_ATTEMPTS = 20
_REPLACE_SHARING_SLEEP_SECONDS = 0.01


def replace_tolerating_transient_sharing(
    replace_once: Callable[[], None], *, recheck: Callable[[], None] = lambda: None
) -> int:
    """Perform a replacement, waiting out a transient Windows sharing refusal.

    Returns the zero-based attempt that succeeded, so a caller (or a test) can
    see how much of the budget a workload actually consumes rather than only
    whether it fit.

    `recheck` runs after every failed attempt. Waiting necessarily widens the
    interval between proving a precondition and acting on it, so a caller that
    holds a guarded precondition MUST re-prove it here rather than replace
    against one established before the wait.

    Note that this and `read_bytes_without_pinning` are a **pair**, not two
    independent accommodations, and neither is sufficient alone. Without the
    non-pinning read, a rebuild sweeping the corpus dies the moment it opens a
    page a writer has marked delete-pending. With it, the reader survives -- but
    FILE_SHARE_DELETE is precisely what lets a replacement mark the target
    delete-pending while that handle lives, so a re-opening reader makes the
    *writer* fail more often, not less. Only the retry closes that second gap.
    """
    for attempt in range(_REPLACE_SHARING_ATTEMPTS):
        try:
            replace_once()
            return attempt
        except PermissionError as error:
            if (
                os.name != "nt"
                or getattr(error, "winerror", None) not in _WINDOWS_SHARING_ERRORS
                or attempt == _REPLACE_SHARING_ATTEMPTS - 1
            ):
                raise
            time.sleep(_REPLACE_SHARING_SLEEP_SECONDS)
            recheck()
    raise AssertionError("unreachable: the final attempt re-raises")  # pragma: no cover


def read_bytes_without_pinning(path: Path) -> bytes:
    """Read a canonical file without blocking a concurrent replacement of it.

    Python's Windows `open()` requests FILE_SHARE_READ | FILE_SHARE_WRITE and
    omits FILE_SHARE_DELETE, so for as long as the file is open an `os.replace`
    onto it is refused with WinError 32. That is the right trade for a
    *canonical* reader, which has verified a path and must pin what it verified.

    It is the wrong trade for a **derived-index** reader. A graph or embedding
    rebuild is building a cache from canonical bytes; it already re-proves the
    source versions it read (`_source_versions_current`) and treats a page that
    moved under it as a reason to discard the pass. Pinning buys it nothing, and
    costs it this: a rebuild sweeping the corpus makes every concurrent write to
    a page it happens to be reading fail, which surfaced as WinError 32 on
    `log.md` and `_collection.md` the moment writes stopped joining their
    rebuild (#576).

    POSIX has no equivalent restriction -- `rename(2)` over an open file is
    always permitted -- so there this is exactly `path.read_bytes()`.
    """
    if os.name != "nt":
        return path.read_bytes()
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        0x80000000,  # GENERIC_READ
        0x00000001 | 0x00000002 | 0x00000004,  # SHARE_READ | SHARE_WRITE | SHARE_DELETE
        None,
        3,  # OPEN_EXISTING
        0x00000080,  # FILE_ATTRIBUTE_NORMAL
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        error = ctypes.get_last_error()
        # Preserve the exception types every caller of `read_bytes()` already
        # handles; a derived-index reader treats a vanished page as absent, not
        # as an unclassified OS failure.
        if error in {2, 3}:
            raise FileNotFoundError(error, "no such file", str(path))
        raise ctypes.WinError(error)
    import msvcrt

    # `open_osfhandle` transfers ownership: closing the descriptor closes the
    # handle, so there is no path here that leaks it.
    descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    with os.fdopen(descriptor, "rb") as stream:
        return stream.read()


def _is_registered_internal_state_artifact(relative: str) -> bool:
    """Whether one full vault-relative entry is private Exomem state."""
    return (
        reserved_paths.classify_logical(relative).disposition
        is reserved_paths.PathDisposition.RESERVED
    )


_BATCH_RESIDUE_PREFIX = ".exomem-batch-"
_BATCH_RESIDUE_NAME = re.compile(r"^\.exomem-batch-[0-9a-f]{32}$", re.ASCII)
_BATCH_RESIDUE_CHILD = re.compile(
    r"^(?:stage|restore)-[0-9]+\.tmp$",
    re.ASCII,
)
_BATCH_RESIDUE_WORKSPACE_LIMIT = 64
_BATCH_RESIDUE_CHILD_LIMIT = 4_096


def _batch_residue_error(code: str) -> PathGuardError:
    reason = (
        "private batch residue exceeds its inspection limit"
        if code == "BATCH_RESIDUE_LIMIT"
        else "private batch residue is unsafe"
    )
    return PathGuardError(code, reason)


_BATCH_RESIDUE_NOATIME_FALLBACK_ERRNOS = frozenset(
    value
    for value in (
        getattr(errno, "EACCES", None),
        getattr(errno, "EPERM", None),
        getattr(errno, "EINVAL", None),
        getattr(errno, "ENOSYS", None),
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
    )
    if value is not None
)


def _open_batch_residue_directory(
    parent: Path,
    parent_descriptor: int,
    name: str,
    *,
    descriptor_relative: bool,
) -> tuple[int, bool]:
    """Open residue for scanning, using no-atime access when safely available.

    The false return means enumeration may observably update access timestamps;
    classification deliberately never writes metadata to hide that side effect.
    """
    base_flags = _directory_flags()
    noatime_flag = getattr(os, "O_NOATIME", 0) if descriptor_relative else 0

    def open_with(flags: int) -> int:
        if os.open in getattr(os, "supports_dir_fd", set()):
            return os.open(name, flags, dir_fd=parent_descriptor)
        return _open_directory_path(parent / name)  # pragma: no cover - Windows fallback

    if noatime_flag:
        try:
            return open_with(base_flags | noatime_flag), True
        except OSError as error:
            if error.errno not in _BATCH_RESIDUE_NOATIME_FALLBACK_ERRNOS:
                raise
    return open_with(base_flags), False


def _scan_batch_residue_children(
    workspace_path: Path,
    workspace_descriptor: int,
    *,
    descriptor_relative: bool,
) -> tuple[tuple[str, PathIdentity], ...]:
    """Return one bounded, validated observation of residue children."""
    iterator = None
    child_names: list[str] = []
    try:
        iterator = os.scandir(workspace_descriptor if descriptor_relative else workspace_path)
        for child in iterator:
            child_names.append(child.name)
            if len(child_names) > _BATCH_RESIDUE_CHILD_LIMIT:
                raise _batch_residue_error("BATCH_RESIDUE_LIMIT")
    finally:
        if iterator is not None:
            iterator.close()

    observations: list[tuple[str, PathIdentity]] = []
    for child_name in sorted(child_names):
        if _BATCH_RESIDUE_CHILD.fullmatch(child_name) is None:
            raise _batch_residue_error("BATCH_RESIDUE_UNSAFE")
        if os.stat in getattr(os, "supports_dir_fd", set()):
            child_info = os.stat(
                child_name,
                dir_fd=workspace_descriptor,
                follow_symlinks=False,
            )
        else:  # pragma: no cover - Windows fallback
            child_info = (workspace_path / child_name).lstat()
        if (
            not stat.S_ISREG(child_info.st_mode)
            or stat.S_ISLNK(child_info.st_mode)
            or _is_reparse(child_info)
        ):
            raise _batch_residue_error("BATCH_RESIDUE_UNSAFE")
        observations.append((child_name, _identity(child_name, child_info)))
    return tuple(observations)


def _classify_batch_residue(
    parent: Path,
    parent_descriptor: int,
    name: str,
) -> None:
    """Validate bounded stale residue without reading or adopting its content."""
    workspace_path = parent / name
    workspace_descriptor: int | None = None
    try:
        if _BATCH_RESIDUE_NAME.fullmatch(name) is None:
            raise _batch_residue_error("BATCH_RESIDUE_UNSAFE")
        if os.stat in getattr(os, "supports_dir_fd", set()):
            workspace_info = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        else:  # pragma: no cover - Windows fallback
            workspace_info = workspace_path.lstat()
        if (
            not stat.S_ISDIR(workspace_info.st_mode)
            or stat.S_ISLNK(workspace_info.st_mode)
            or _is_reparse(workspace_info)
            or (os.name == "posix" and stat.S_IMODE(workspace_info.st_mode) & 0o077)
        ):
            raise _batch_residue_error("BATCH_RESIDUE_UNSAFE")
        workspace_identity = _identity(name, workspace_info)
        descriptor_relative = os.scandir in getattr(os, "supports_fd", set())
        workspace_descriptor, noatime_active = _open_batch_residue_directory(
            parent,
            parent_descriptor,
            name,
            descriptor_relative=descriptor_relative,
        )
        opened = os.fstat(workspace_descriptor)
        if (
            not _same_identity(workspace_identity, opened)
            or not stat.S_ISDIR(opened.st_mode)
            or _is_reparse(opened)
        ):
            raise _batch_residue_error("BATCH_RESIDUE_UNSAFE")
        baseline_children = _scan_batch_residue_children(
            workspace_path,
            workspace_descriptor,
            descriptor_relative=descriptor_relative,
        )

        final_descriptor_info = os.fstat(workspace_descriptor)
        if os.stat in getattr(os, "supports_dir_fd", set()):
            final_path_info = os.stat(
                name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        else:  # pragma: no cover - Windows fallback
            final_path_info = workspace_path.lstat()
        metadata_changed = (
            final_descriptor_info.st_mtime_ns != opened.st_mtime_ns
            or final_descriptor_info.st_ctime_ns != opened.st_ctime_ns
            or (noatime_active and final_descriptor_info.st_atime_ns != opened.st_atime_ns)
            or final_path_info.st_mtime_ns != workspace_info.st_mtime_ns
            or final_path_info.st_ctime_ns != workspace_info.st_ctime_ns
            or (noatime_active and final_path_info.st_atime_ns != workspace_info.st_atime_ns)
        )
        if (
            not _same_identity(workspace_identity, final_descriptor_info)
            or not _same_identity(workspace_identity, final_path_info)
            or not stat.S_ISDIR(final_descriptor_info.st_mode)
            or not stat.S_ISDIR(final_path_info.st_mode)
            or stat.S_ISLNK(final_path_info.st_mode)
            or _is_reparse(final_descriptor_info)
            or _is_reparse(final_path_info)
            or (
                os.name == "posix"
                and (
                    stat.S_IMODE(final_descriptor_info.st_mode) & 0o077
                    or stat.S_IMODE(final_path_info.st_mode) & 0o077
                )
            )
            or metadata_changed
        ):
            raise _batch_residue_error("BATCH_RESIDUE_UNSAFE")

        final_children = _scan_batch_residue_children(
            workspace_path,
            workspace_descriptor,
            descriptor_relative=descriptor_relative,
        )
        if final_children != baseline_children:
            raise _batch_residue_error("BATCH_RESIDUE_UNSAFE")
    except PathGuardError:
        raise
    except (OSError, ValueError) as error:
        raise _batch_residue_error("BATCH_RESIDUE_UNSAFE") from error
    finally:
        if workspace_descriptor is not None:
            os.close(workspace_descriptor)


def _recheck_bounded_parent_path(path: Path, expected: PathIdentity) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise PathGuardError("PATH_GUARD_CHANGED", "guarded directory changed") from error
    if (
        not _same_identity(expected, info)
        or not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or _is_reparse(info)
    ):
        raise PathGuardError("PATH_GUARD_CHANGED", "guarded directory changed")


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
        descriptor = _open_directory_path(path)
    except OSError as error:
        raise PathGuardError("PATH_GUARD_CHANGED", "guarded directory changed") from error
    iterator = None
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode) or not _same_identity(expected, opened):
            raise PathGuardError("PATH_GUARD_CHANGED", "guarded directory changed")
        _recheck_bounded_parent_path(path, expected)
        descriptor_relative = os.scandir in getattr(os, "supports_fd", set())
        iterator = os.scandir(descriptor if descriptor_relative else path)
        residue_names: list[str] = []
        ordinary_names: list[str] = []
        for entry in iterator:
            name = entry.name
            if name in ignored_names:
                continue
            if name.startswith(_BATCH_RESIDUE_PREFIX):
                residue_names.append(name)
                if len(residue_names) > _BATCH_RESIDUE_WORKSPACE_LIMIT:
                    raise _batch_residue_error("BATCH_RESIDUE_LIMIT")
                continue
            entry_relative = f"{relative}/{name}" if relative else name
            if _is_registered_internal_state_artifact(entry_relative):
                continue
            if len(ordinary_names) <= max_entries:
                ordinary_names.append(name)
        iterator.close()
        iterator = None

        for name in sorted(residue_names):
            _classify_batch_residue(path, descriptor, name)
        if len(ordinary_names) > max_entries:
            raise PathGuardError("PATH_GUARD_LIMIT", "guarded directory exceeds its entry limit")

        entries: list[PathIdentity] = []
        for name in sorted(ordinary_names):
            try:
                encoded = name.encode("utf-8")
            except UnicodeEncodeError as error:
                raise PathGuardError(
                    "PATH_GUARD_UNSAFE", "guarded directory entry is unsafe"
                ) from error
            if not name or name in {".", ".."} or "/" in name or "\\" in name or b"\0" in encoded:
                raise PathGuardError("PATH_GUARD_UNSAFE", "guarded directory entry is unsafe")
            if len(entries) >= max_entries:
                raise PathGuardError(
                    "PATH_GUARD_LIMIT", "guarded directory exceeds its entry limit"
                )
            try:
                info = (
                    os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if descriptor_relative
                    else (path / name).lstat()
                )
            except OSError as error:
                raise PathGuardError(
                    "PATH_GUARD_CHANGED", "guarded directory entry changed"
                ) from error
            entry_relative = f"{relative}/{name}"
            entries.append(_identity(entry_relative, info))
        if not _same_identity(expected, os.fstat(descriptor)):
            raise PathGuardError("PATH_GUARD_CHANGED", "guarded directory changed")
        _recheck_bounded_parent_path(path, expected)
    finally:
        if iterator is not None:
            iterator.close()
        os.close(descriptor)
    return tuple(sorted(entries, key=lambda item: item.relative_path.encode("utf-8")))


def _allowed_census_names(directory: Path, allowed_changes: Iterable[Path]) -> frozenset[str]:
    directory_path = Path(os.path.abspath(directory))
    names: set[str] = set()
    for change in allowed_changes:
        try:
            relative = Path(os.path.abspath(change)).relative_to(directory_path)
        except ValueError:
            continue
        if relative.parts:
            names.add(relative.parts[0])
    return frozenset(names)


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
                    raise PathGuardError("PATH_GUARD_CHANGED", "guarded directory ancestor changed")
            if self.missing_paths and os.path.lexists(root / self.missing_paths[-1]):
                directory = root / self.target
                allowed_names = _allowed_census_names(directory, allowed_changes)
                if not allowed_names:
                    raise PathGuardError("PATH_GUARD_CHANGED", "guarded directory appeared")
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
                    raise PathGuardError("PATH_GUARD_CHANGED", "guarded directory changed")
                current = _bounded_directory_entries(
                    directory,
                    relative=self.target,
                    expected=_identity(self.target, info),
                    max_entries=self.max_entries,
                    ignored_names=allowed_names,
                )
                if current:
                    raise PathGuardError("PATH_GUARD_CHANGED", "guarded directory census changed")
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
        allowed_names = _allowed_census_names(directory, allowed_changes)
        current = _bounded_directory_entries(
            directory,
            relative=self.target,
            expected=self.directory_identity,
            max_entries=self.max_entries,
            ignored_names=allowed_names,
        )
        expected_entries = tuple(
            entry for entry in self.entries if Path(entry.relative_path).name not in allowed_names
        )
        if current != expected_entries:
            raise PathGuardError("PATH_GUARD_CHANGED", "guarded directory census changed")


def _read_bounded_guarded_snapshot(
    vault_root: Path, target: str, limit: int
) -> tuple[bytes, PathGuard]:
    """Return bytes and a guard captured entirely from safe descriptors."""
    parts = _safe_guard_target(target)
    if _uses_windows_guarded_reader():  # pragma: no cover - exercised on Windows CI
        return _read_bounded_windows_snapshot(Path(vault_root), parts, target, limit)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    root_flags = flags | getattr(os, "O_DIRECTORY", 0)
    descriptors: list[int] = []
    try:
        root = Path(vault_root)
        root_info = root.lstat()
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or stat.S_ISLNK(root_info.st_mode)
            or _is_reparse(root_info)
        ):
            raise PathGuardError("PATH_GUARD_ROOT", "vault root is unsafe")
        descriptor = os.open(root, root_flags)
        descriptors.append(descriptor)
        opened_root = os.fstat(descriptor)
        if not stat.S_ISDIR(opened_root.st_mode) or not _same_identity(
            _identity(".", root_info), opened_root
        ):
            raise PathGuardError("PATH_GUARD_ROOT", "vault root changed")
        ancestors = [_identity(".", opened_root)]
        for index, part in enumerate(parts[:-1]):
            child_flags = root_flags | getattr(os, "O_NONBLOCK", 0)
            child = os.open(part, child_flags, dir_fd=descriptor)
            descriptors.append(child)
            info = os.fstat(child)
            if not stat.S_ISDIR(info.st_mode):
                raise PathGuardError("PATH_GUARD_UNSAFE", "guard ancestor is unsafe")
            ancestors.append(_identity("/".join(parts[: index + 1]), info))
            descriptor = child
        leaf = os.open(parts[-1], flags | getattr(os, "O_NONBLOCK", 0), dir_fd=descriptor)
        descriptors.append(leaf)
        before = os.fstat(leaf)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > limit
        ):
            raise PathGuardError(
                "PATH_GUARD_UNSAFE", "guarded content is not a bounded regular file"
            )
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(leaf, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(leaf)
        if (
            len(data) > limit
            or not _same_identity(_identity(target, before), after)
            or before.st_size != len(data)
        ):
            raise PathGuardError("PATH_GUARD_CHANGED", "guarded content changed")
        guard = PathGuard(
            target=target,
            ancestors=tuple(ancestors),
            missing_parents=(),
            leaf_identity=_identity(target, before),
            leaf_policy="content",
            expected_content_hash=hashlib.sha256(data).hexdigest(),
        )
        object.__setattr__(guard, "expected_content_size", len(data))
        return data, guard
    except PathGuardError:
        raise
    except OSError as error:
        raise PathGuardError("PATH_GUARD_IO", "guarded content could not be opened") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_bounded_guarded_snapshot_tolerating_transient_sharing(
    vault_root: Path, target: str, limit: int
) -> tuple[bytes, PathGuard]:
    """Retry a fresh Windows snapshot after a short-lived sharing refusal."""
    for attempt in range(_REPLACE_SHARING_ATTEMPTS):
        try:
            return _read_bounded_guarded_snapshot(vault_root, target, limit)
        except PathGuardError as error:
            cause = error.__cause__
            if (
                not _uses_windows_guarded_reader()
                or error.code != "PATH_GUARD_IO"
                or not isinstance(cause, PermissionError)
                or getattr(cause, "winerror", None) not in _WINDOWS_SHARING_ERRORS
                or attempt == _REPLACE_SHARING_ATTEMPTS - 1
            ):
                raise
            # The failed snapshot has already closed its entire descriptor
            # chain. Reacquire from the vault root; never reuse a stale guard.
            time.sleep(_REPLACE_SHARING_SLEEP_SECONDS)
    raise AssertionError("unreachable: the final attempt re-raises")  # pragma: no cover


def _uses_windows_guarded_reader() -> bool:
    """Small dispatch seam that tests can override without mutating global ``os.name``."""
    return os.name == "nt"


def _read_bounded_windows_snapshot(
    root: Path, parts: tuple[str, ...], target: str, limit: int
) -> tuple[bytes, PathGuard]:
    """Windows equivalent of openat: every component is identity-checked."""
    descriptors: list[int] = []
    try:
        root_info = root.lstat()
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or stat.S_ISLNK(root_info.st_mode)
            or _is_reparse(root_info)
        ):
            raise PathGuardError("PATH_GUARD_ROOT", "vault root is unsafe")
        root_fd = _open_directory_path(
            root,
            desired_access=_WINDOWS_FILE_LIST_DIRECTORY,
            share_mode=_WINDOWS_GUARDED_DIRECTORY_SHARE,
        )
        descriptors.append(root_fd)
        opened_root = os.fstat(root_fd)
        if not _same_identity(_identity(".", root_info), opened_root):
            raise PathGuardError("PATH_GUARD_ROOT", "vault root changed")
        ancestors = [_identity(".", opened_root)]
        current = root
        for index, part in enumerate(parts[:-1]):
            current /= part
            before = current.lstat()
            if (
                not stat.S_ISDIR(before.st_mode)
                or stat.S_ISLNK(before.st_mode)
                or _is_reparse(before)
            ):
                raise PathGuardError("PATH_GUARD_UNSAFE", "guard ancestor is unsafe")
            descriptor = _open_directory_path(
                current,
                desired_access=_WINDOWS_FILE_LIST_DIRECTORY,
                share_mode=_WINDOWS_GUARDED_DIRECTORY_SHARE,
            )
            descriptors.append(descriptor)
            opened = os.fstat(descriptor)
            if not _same_identity(_identity("/".join(parts[: index + 1]), before), opened):
                raise PathGuardError("PATH_GUARD_CHANGED", "guard ancestor changed")
            ancestors.append(_identity("/".join(parts[: index + 1]), opened))
        leaf_path = root.joinpath(*parts)
        before = leaf_path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or _is_reparse(before)
            or before.st_nlink != 1
            or before.st_size > limit
        ):
            raise PathGuardError(
                "PATH_GUARD_UNSAFE", "guarded content is not a bounded regular file"
            )
        leaf = _open_windows_path_descriptor(
            leaf_path,
            desired_access=0x80000000,
            attributes=0x00200000,
            crt_flags=os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
        descriptors.append(leaf)
        opened = os.fstat(leaf)
        if not _same_identity(_identity(target, before), opened):
            raise PathGuardError("PATH_GUARD_CHANGED", "guarded content changed")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(leaf, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(leaf)
        if (
            len(data) > limit
            or len(data) != before.st_size
            or not _same_identity(_identity(target, before), after)
        ):
            raise PathGuardError("PATH_GUARD_CHANGED", "guarded content changed")
        # Check the named path too: a replacement after open must not be bound.
        if not _same_identity(_identity(target, before), leaf_path.lstat()):
            raise PathGuardError("PATH_GUARD_CHANGED", "guarded content changed")
        for ancestor in ancestors:
            ancestor_path = root if ancestor.relative_path == "." else root / ancestor.relative_path
            current_info = ancestor_path.lstat()
            if (
                not stat.S_ISDIR(current_info.st_mode)
                or stat.S_ISLNK(current_info.st_mode)
                or _is_reparse(current_info)
                or not _same_identity(ancestor, current_info)
            ):
                raise PathGuardError("PATH_GUARD_CHANGED", "guard ancestor changed")
        guard = PathGuard(
            target,
            tuple(ancestors),
            (),
            _identity(target, before),
            "content",
            hashlib.sha256(data).hexdigest(),
        )
        object.__setattr__(guard, "expected_content_size", len(data))
        return data, guard
    except PathGuardError:
        raise
    except OSError as error:
        raise PathGuardError("PATH_GUARD_IO", "guarded content could not be opened") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def read_bounded_guarded_bytes(
    vault_root: Path,
    target: str,
    *,
    limit: int,
    expected_hash: str | None = None,
) -> tuple[bytes, PathGuard]:
    """Read one bounded regular vault file and bind its exact descriptor snapshot."""
    if type(limit) is not int or limit < 0:
        raise PathGuardError("PATH_GUARD_INVALID", "guarded read limit is invalid")
    data, guard = _read_bounded_guarded_snapshot_tolerating_transient_sharing(
        Path(vault_root), target, limit
    )
    if expected_hash is not None and guard.expected_content_hash != expected_hash:
        raise PathGuardError("PATH_GUARD_CONTENT", "guarded content changed")
    guard.recheck(Path(vault_root))
    return data, guard


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
    try:
        limit = absolute.lstat().st_size
    except FileNotFoundError:
        raise
    except OSError as error:
        raise PathGuardError(
            "PATH_GUARD_IO", "guarded content could not be opened"
        ) from error
    raw, guard = read_bounded_guarded_bytes(root, relative, limit=limit)
    return raw.decode("utf-8"), guard


@dataclass
class PlannedWrite:
    """One target file in a batch write, with an optional commit-time CAS guard."""

    path: Path
    content: str
    create_only: bool = False
    guard: PathGuard | None = None
    expected_hash: str | None = None
    ensure_directories: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class DeferredGraphCompletion:
    """Exact graph handoff retained after a canonical deferred-completion batch."""

    replaced: tuple[Path, ...]
    checkpoint: GraphSyncCheckpoint
    predecessor: GraphSyncCheckpoint | None


def _summarize_batch_targets(
    writes: Iterable[PlannedWrite], vault_root: Path | None
) -> BatchTargetSummary:
    planned = tuple(writes)
    affected_count = len(planned)
    if vault_root is None:
        return BatchTargetSummary(affected_count, (), affected_count)
    root = Path(os.path.abspath(vault_root))
    targets: list[str] = []
    for write in planned:
        try:
            relative = Path(os.path.abspath(write.path)).relative_to(root)
            parts = relative.parts
            logical_target = relative.as_posix()
            encoded = logical_target.encode("utf-8")
        except (UnicodeEncodeError, ValueError):
            continue
        if (
            not parts
            or logical_target.startswith("/")
            or "\\" in logical_target
            or "\0" in logical_target
            or any(part in {"", ".", ".."} for part in parts)
            or len(encoded) > 1024
        ):
            continue
        if len(targets) < 16:
            targets.append(logical_target)
    return BatchTargetSummary(
        affected_count,
        tuple(targets),
        affected_count - len(targets),
    )


def _safe_write_target(path: Path, vault_root: Path | None) -> str:
    if vault_root is None:
        return path.name
    try:
        return Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(vault_root))).as_posix()
    except ValueError:
        return path.name


def _prepare_path_guards(
    vault_root: Path,
    guards: Iterable[PathGuard],
    *,
    created_dirs: list[Path | _CreatedDirectory] | None = None,
) -> tuple[PathGuard, ...]:
    original = tuple(guards)
    for guard in original:
        guard.recheck(vault_root)
    missing = sorted(
        {relative for guard in original for relative in guard.missing_parents},
        key=lambda value: (len(Path(value).parts), value),
    )
    try:
        _create_missing_guard_parents(
            vault_root,
            missing,
            expected_ancestors=tuple(
                identity for guard in original for identity in guard.ancestors
            ),
            created_dirs=created_dirs,
        )
        prepared: list[PathGuard] = []
        for guard in original:
            rebound = PathGuard.capture(
                vault_root,
                guard.target,
                leaf_policy=guard.leaf_policy,
                expected_content_hash=guard.expected_content_hash,
                expected_content_size=guard.expected_content_size,
            )
            prepared.append(rebound)
        return tuple(prepared)
    except BaseException:
        if created_dirs is not None:
            _remove_empty_created_dirs(created_dirs)
        raise


def _directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _open_windows_path_descriptor(
    path: Path,
    *,
    desired_access: int,
    attributes: int,
    crt_flags: int,
    share_mode: int = _WINDOWS_DEFAULT_SHARE,
) -> int:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    handle = create_file(
        os.path.abspath(path),
        desired_access,
        share_mode,
        None,
        3,
        attributes,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(handle, crt_flags)
    except BaseException:
        close_handle(handle)
        raise


def _open_directory_path(
    path: Path,
    *,
    desired_access: int = 0,
    share_mode: int | None = None,
) -> int:
    """Open a directory as a CRT descriptor on every supported platform."""
    if os.name != "nt":
        return os.open(path, _directory_flags())

    # Windows' CRT os.open() refuses directories. A Win32 directory handle
    # created with backup semantics can still be owned by a CRT descriptor,
    # giving the batch guard the same fstat/close lifecycle used on POSIX.
    return _open_windows_path_descriptor(
        path,
        desired_access=desired_access,
        attributes=0x02000000 | 0x00200000,
        crt_flags=os.O_RDONLY,
        share_mode=_WINDOWS_DEFAULT_SHARE if share_mode is None else share_mode,
    )


def _set_windows_path_timestamps(
    path: Path,
    expected_identity: PathIdentity,
    atime_ns: int,
    mtime_ns: int,
    *,
    verify_atime: bool = True,
) -> None:
    """Set Windows timestamps through the exact identity-checked file handle."""
    import ctypes
    from ctypes import wintypes

    class FileTime(ctypes.Structure):
        _fields_ = (("low", wintypes.DWORD), ("high", wintypes.DWORD))

    def filetime(value_ns: int) -> FileTime:
        value = value_ns // 100 + 116_444_736_000_000_000
        return FileTime(value & 0xFFFFFFFF, value >> 32)

    descriptor = _open_windows_path_descriptor(
        path,
        desired_access=0x00000100,
        attributes=0x00200000,
        crt_flags=os.O_RDWR,
    )
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or _is_reparse(before)
            or not _same_identity(expected_identity, before)
        ):
            raise PathGuardError("PATH_GUARD_CHANGED", "batch artifact changed")
        atime = filetime(atime_ns)
        mtime = filetime(mtime_ns)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        set_file_time = kernel32.SetFileTime
        set_file_time.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
        )
        set_file_time.restype = wintypes.BOOL
        handle = msvcrt.get_osfhandle(descriptor)
        if not set_file_time(handle, None, ctypes.byref(atime), ctypes.byref(mtime)):
            raise ctypes.WinError(ctypes.get_last_error())
        after = os.fstat(descriptor)
        if (
            not _same_identity(expected_identity, after)
            or (verify_atime and after.st_atime_ns != atime_ns)
            or after.st_mtime_ns != mtime_ns
        ):
            raise PathGuardError("PATH_GUARD_CHANGED", "batch metadata restore changed")
    finally:
        os.close(descriptor)


def _set_windows_descriptor_timestamps(
    descriptor: int,
    expected_identity: PathIdentity,
    atime_ns: int,
    mtime_ns: int,
) -> None:
    import ctypes
    from ctypes import wintypes

    class FileTime(ctypes.Structure):
        _fields_ = (("low", wintypes.DWORD), ("high", wintypes.DWORD))

    def filetime(value_ns: int) -> FileTime:
        value = value_ns // 100 + 116_444_736_000_000_000
        return FileTime(value & 0xFFFFFFFF, value >> 32)

    before = os.fstat(descriptor)
    if not _same_identity(expected_identity, before):
        raise PathGuardError("PATH_GUARD_CHANGED", "batch artifact changed")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_file_time = kernel32.SetFileTime
    set_file_time.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
    )
    set_file_time.restype = wintypes.BOOL
    atime = filetime(atime_ns)
    mtime = filetime(mtime_ns)
    handle = msvcrt.get_osfhandle(descriptor)
    if not set_file_time(handle, None, ctypes.byref(atime), ctypes.byref(mtime)):
        raise ctypes.WinError(ctypes.get_last_error())
    after = os.fstat(descriptor)
    if not _same_identity(expected_identity, after) or after.st_mtime_ns != mtime_ns:
        raise PathGuardError("PATH_GUARD_CHANGED", "batch metadata restore changed")


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


@dataclass(frozen=True, slots=True)
class _CreatedDirectory:
    vault_root: Path
    relative_path: str
    identity: held_fs.StableIdentity

    @property
    def path(self) -> Path:
        return self.vault_root / self.relative_path


def _create_missing_guard_parents(
    vault_root: Path,
    missing_parents: Iterable[str],
    *,
    expected_ancestors: Iterable[PathIdentity],
    created_dirs: list[Path | _CreatedDirectory] | None = None,
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
    acquired = held_fs.acquire(vault_root)
    if not acquired.ok:
        raise PathGuardError(
            "PATH_GUARD_UNSAFE", "held filesystem route is unavailable"
        )
    with acquired.require() as filesystem:
        for relative, expected in expected_by_path.items():
            current = filesystem.parent(relative)
            if not current.ok:
                raise PathGuardError(
                    "PATH_GUARD_CHANGED", "guard ancestor changed during creation"
                )
            with current.require() as directory:
                if (
                    directory.identity.device != expected.device
                    or directory.identity.inode != expected.inode
                    or directory.identity.kind != "directory"
                ):
                    raise PathGuardError(
                        "PATH_GUARD_CHANGED", "guard ancestor changed during creation"
                    )
        for relative in missing:
            created = filesystem.parent(
                relative,
                create=True,
                exclusive=True,
                access="mutate",
            )
            if not created.ok:
                code = (
                    "PATH_GUARD_CHANGED"
                    if created.error is not None
                    and created.error.code in {"DESTINATION_EXISTS", "MISSING"}
                    else "PATH_GUARD_UNSAFE"
                )
                raise PathGuardError(code, "guard ancestor creation was refused")
            with created.require() as directory:
                expected_by_path[relative] = PathIdentity(
                    relative,
                    directory.identity.device,
                    directory.identity.inode,
                    stat.S_IFDIR,
                )
                if created_dirs is not None:
                    created_dirs.append(
                        _CreatedDirectory(vault_root, relative, directory.identity)
                    )
            _after_batch_parent_created(vault_root, relative)


def post_commit_batch_fanout(
    vault_root: Path | None,
    replaced: list[Path],
    index_reports: list[Any] | None,
    semantic_states: Mapping[str, Any] | None,
    *,
    created_paths: Iterable[Path] = (),
) -> bool:
    if vault_root is None or not replaced:
        return True
    # Register the self-authored replacements so the live watcher drops
    # their echo instead of re-embedding the same files a second time.
    corpus_published = False
    try:
        from . import file_watcher

        file_watcher.register_self_write(vault_root, replaced)
        corpus_published = True
    except Exception:  # noqa: BLE001 — suppression is best-effort
        logging.getLogger(__name__).debug(
            "self-write suppression registration failed", exc_info=True
        )
    try:
        from . import index_sync

        kwargs: dict[str, Any] = {
            "created_paths": list(created_paths),
            "publish_corpus_change": not corpus_published,
        }
        if semantic_states:
            kwargs["semantic_states"] = semantic_states
        report = index_sync.upsert_after_write(vault_root, replaced, **kwargs)
        # A graph-relevant epoch is a canonical promise, not a best-effort
        # side effect.  The graph leaf returns an exact result, but keep this
        # final boundary check here so a future fanout branch cannot return a
        # committed batch with an unacknowledged checkpoint and nothing
        # arranged to converge it.  What counts as "arranged" is
        # `graph_sync.repair_is_provisioned`: a registered flight was the only
        # answer before repair moved off the write path, and asking only about
        # that failed the write whose repair was correctly queued instead.
        graph = next(
            (
                item
                for item in getattr(report, "components", ())
                if getattr(item, "component", None) == "epistemic_graph"
            ),
            None,
        )
        if graph is not None and graph.outcome != "not_required":
            from . import graph_sync

            required = graph_sync.read_checkpoint(vault_root)
            handoff_missing = required is not None and (
                (
                    graph.outcome == "completed"
                    and graph_sync.status(vault_root).get("state") != "current"
                )
                or (
                    graph.outcome in {"registered", "deferred", "failed"}
                    and not graph_sync.repair_is_provisioned(
                        vault_root, required, outcome=graph.outcome
                    )
                )
            )
            if handoff_missing:
                assert required is not None
                graph_sync.register_failure(
                    vault_root,
                    required,
                    code="GRAPH_SYNC_HANDOFF_MISSING",
                )
                report = index_sync.with_component(
                    report,
                    index_sync.IndexComponentOutcome(
                        "epistemic_graph", "failed", "GRAPH_SYNC_HANDOFF_MISSING"
                    ),
                )
        if index_reports is not None:
            index_reports.append(report)
        if not index_sync.full_upsert_succeeded(vault_root, replaced, report):
            index_sync.record_failed_refresh(vault_root, replaced)
            logging.getLogger(__name__).warning(
                "index upsert incomplete after batch_atomic_write; "
                "durable full-index refresh recorded"
            )
            return False
        return True
    except Exception:  # noqa: BLE001 — embeddings are best-effort
        try:
            from . import graph_sync

            graph_sync.register_outer_fanout_failure(vault_root)
        except Exception:  # noqa: BLE001 - canonical commit must still survive
            logging.getLogger(__name__).exception(
                "failed to register graph handoff after dispatch failure"
            )
        try:
            from . import index_sync as failed_index_sync

            failed_index_sync.record_failed_refresh(vault_root, replaced)
        except Exception:  # noqa: BLE001 - canonical commit must still survive
            logging.getLogger(__name__).exception(
                "failed to persist deferred index refresh after dispatch failure"
            )
        logging.getLogger(__name__).exception(
            "index upsert failed after batch_atomic_write; deferred refresh recorded"
        )
        if index_reports is not None:
            try:
                index_reports.append(failed_index_sync.failed_upsert_report(vault_root, replaced))
            except Exception:  # noqa: BLE001 — failure feedback remains best-effort
                logging.getLogger(__name__).exception(
                    "failed to construct bounded index degradation feedback"
                )
        return False


class ContentHashMismatchError(RuntimeError):
    """A planned destination changed before its guarded batch could commit."""

    def __init__(self, path: Path, expected_hash: str, actual_hash: str | None):
        self.path = path
        self.expected_hash = expected_hash
        self.actual_hash = actual_hash
        actual = actual_hash or "<missing>"
        super().__init__(
            f"content changed before commit: {path} (expected {expected_hash}, found {actual})"
        )


_BATCH_COMMIT_LOCK = threading.RLock()
MISSING_CONTENT_HASH = "<missing>"


def _create_parent_dirs(parent: Path, created_dirs: list[Path | _CreatedDirectory]) -> None:
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


def _create_parent_dirs_held(
    vault_root: Path,
    parent: Path,
    created_dirs: list[Path | _CreatedDirectory],
) -> None:
    root = Path(os.path.abspath(vault_root))
    absolute_parent = Path(os.path.abspath(parent))
    try:
        relative_parent = absolute_parent.relative_to(root)
    except ValueError as error:
        raise PathGuardError(
            "PATH_GUARD_TARGET", "batch directory escaped the vault"
        ) from error
    if not root.exists():
        # Bootstrap is the one point where no vault-root handle can exist yet.
        # The configured root is owner-trusted; after creation every child
        # operation immediately switches to the held no-follow substrate.
        _create_parent_dirs(root, created_dirs)
    if relative_parent == Path("."):
        return
    acquired = held_fs.acquire(root)
    if not acquired.ok:
        raise PathGuardError(
            "PATH_GUARD_UNSAFE", "held filesystem route is unavailable"
        )
    with acquired.require() as filesystem:
        for index in range(1, len(relative_parent.parts) + 1):
            relative = Path(*relative_parent.parts[:index]).as_posix()
            existing = filesystem.parent(relative)
            if existing.ok:
                existing.require().close()
                continue
            if existing.error is None or existing.error.code != "MISSING":
                raise PathGuardError(
                    "PATH_GUARD_UNSAFE", "batch directory is unsafe"
                )
            created = filesystem.parent(
                relative,
                create=True,
                exclusive=True,
                access="mutate",
            )
            if not created.ok:
                code = (
                    "PATH_GUARD_CHANGED"
                    if created.error is not None
                    and created.error.code == "DESTINATION_EXISTS"
                    else "PATH_GUARD_UNSAFE"
                )
                raise PathGuardError(code, "batch directory creation was refused")
            with created.require() as directory:
                created_dirs.append(
                    _CreatedDirectory(root, relative, directory.identity)
                )
            _after_batch_parent_created(root, relative)


def _remove_empty_created_dirs(
    created_dirs: list[Path | _CreatedDirectory],
) -> None:
    """Best-effort rollback for empty parent directories created during staging."""
    for created in reversed(created_dirs):
        if isinstance(created, _CreatedDirectory):
            acquired = held_fs.acquire(created.vault_root)
            if not acquired.ok:
                continue
            with acquired.require() as filesystem:
                current = filesystem.parent(created.relative_path, access="mutate")
                if not current.ok:
                    continue
                with current.require() as directory:
                    if directory.identity != created.identity:
                        continue
                    filesystem.unlink_directory(directory)
            continue
        directory = created
        try:
            directory.rmdir()
        except OSError:
            pass


def _enqueue_graph_debt(vault_root: Path, checkpoint_write: PlannedWrite) -> None:
    """Record this batch's graph debt durably, *before* the batch commits.

    The ordering is the whole argument. Enqueue-then-write can leave a path
    queued whose content never changed, and re-indexing an unchanged path writes
    nothing -- the cost is one wasted read. Write-then-enqueue can leave the
    markdown committed with no record that the graph owes it anything, and the
    only repair for an unknown dirty set is the whole-vault rebuild this change
    exists to retire. The failures are not comparable, so the safe order is not
    a preference.

    Best-effort by construction: a deferred queue that could refuse a canonical
    write would be a worse availability risk than the drift it prevents. A lost
    enqueue costs a reconcile; a refused write costs the user their edit.
    """
    from . import deferred_index, graph_sync

    checkpoint = graph_sync.GraphSyncCheckpoint.parse(checkpoint_write.content)
    if checkpoint is None:  # pragma: no cover - graph_sync renders its own token
        return
    try:
        deferred_index.enqueue_graph_checkpoint(vault_root, checkpoint)
    except Exception:  # noqa: BLE001 - never fail a canonical write on derived bookkeeping
        log.warning("graph dirty-path enqueue failed; reconcile will repair", exc_info=True)


def batch_atomic_write(
    writes: Iterable[PlannedWrite],
    *,
    vault_root: Path | None = None,
    required_guards: Iterable[PathGuard | DirectoryCensusGuard] = (),
    index_reports: list[Any] | None = None,
    semantic_states: Mapping[str, Any] | None = None,
    post_commit_fanout: bool = True,
    commit_point: bool = True,
    defer_graph_completion: bool = False,
) -> list[Path] | DeferredGraphCompletion:
    """Commit one batch while serializing all in-process vault writers.

    A process-shared lock closes the gap between validating any ``expected_hash``
    guards and replacing destinations. The locked implementation uses private
    descriptor-owned staging, exact rollback snapshots, and one post-commit
    index fan-out.
    """
    with _BATCH_COMMIT_LOCK:
        if defer_graph_completion and (post_commit_fanout or vault_root is None):
            raise ValueError(
                "defer_graph_completion requires vault_root and post_commit_fanout=False"
            )
        return _batch_atomic_write_locked(
            writes,
            vault_root=vault_root,
            required_guards=required_guards,
            index_reports=index_reports,
            semantic_states=semantic_states,
            post_commit_fanout=post_commit_fanout,
            commit_point=commit_point,
            defer_graph_completion=defer_graph_completion,
        )


def _batch_atomic_write_locked(
    writes: Iterable[PlannedWrite],
    *,
    vault_root: Path | None = None,
    required_guards: Iterable[PathGuard | DirectoryCensusGuard] = (),
    index_reports: list[Any] | None = None,
    semantic_states: Mapping[str, Any] | None = None,
    post_commit_fanout: bool = True,
    commit_point: bool = True,
    defer_graph_completion: bool = False,
) -> list[Path] | DeferredGraphCompletion:
    """Stage writes in private workspaces, then replace destinations in order.

    Existing destinations are snapshotted into memory before the first flip. A
    caught mid-flip failure restores those bytes and supported metadata through
    fresh private restore stages, and removes unchanged destinations created by
    the failed batch. Ordinary caught ``Exception`` failures run that rollback;
    ``BaseException`` is treated as abrupt interruption and may expose a partial
    batch for exact higher-level retry. This does not claim cross-file power-loss
    atomicity.

    When `vault_root` is supplied, the embedding sidecar at
    `<vault>/Knowledge Base/.embeddings.sqlite` is refreshed for every
    embeddable file in the batch after the markdown writes succeed. Failures
    in the embedding pass are logged and swallowed — keyword-mode find()
    still works, and `audit_fix(rebuild_embeddings=True)` recovers drift.  An
    opt-in ``index_reports`` collector receives the report from that same
    fan-out; requesting feedback never dispatches indexes a second time.
    """
    caller_writes = list(writes)
    writes = list(caller_writes)
    graph_checkpoint_path: Path | None = None
    graph_floor_path: Path | None = None
    graph_debt_checkpoint: PlannedWrite | None = None
    deferred_checkpoint: GraphSyncCheckpoint | None = None
    deferred_predecessor: GraphSyncCheckpoint | None = None
    if vault_root is not None:
        from . import graph_sync

        if defer_graph_completion:
            deferred_epoch_writes = graph_sync.deferred_epoch_writes(Path(vault_root), writes)
            if deferred_epoch_writes is None:
                epoch_writes = None
            else:
                floor_write, checkpoint_write, deferred_predecessor = deferred_epoch_writes
                epoch_writes = (floor_write, checkpoint_write)
        else:
            epoch_writes = graph_sync.epoch_writes(Path(vault_root), writes)
        if epoch_writes is not None:
            floor_write, checkpoint_write = epoch_writes
            if defer_graph_completion:
                deferred_checkpoint = graph_sync.GraphSyncCheckpoint.parse(checkpoint_write.content)
                if deferred_checkpoint is None:  # pragma: no cover - graph_sync renders its own token
                    raise ValueError("deferred graph completion checkpoint is invalid")
                writes = [floor_write, *writes]
            else:
                writes = [floor_write, *writes, checkpoint_write]
            graph_floor_path = floor_write.path
            if not defer_graph_completion:
                graph_checkpoint_path = checkpoint_write.path
            graph_debt_checkpoint = checkpoint_write
        elif defer_graph_completion:
            raise ValueError("defer_graph_completion requires graph-relevant writes")
    destinations: set[str] = set()
    for write in writes:
        destination = os.path.abspath(write.path)
        portable_destination = "/".join(
            unicodedata.normalize("NFC", part).casefold() for part in Path(destination).parts
        )
        if portable_destination in destinations:
            raise PathGuardError("PATH_GUARD_TARGET", "batch destinations collide")
        destinations.add(portable_destination)
    for write in writes:
        absolute_parts = Path(os.path.abspath(write.path)).parts
        if any(part.startswith(_BATCH_RESIDUE_PREFIX) for part in absolute_parts):
            raise _batch_residue_error("BATCH_RESIDUE_UNSAFE")
        if write.expected_hash is not None:
            try:
                current = write.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                actual_hash = None
            else:
                actual_hash = content_hash(current)
            expected_missing = write.expected_hash == MISSING_CONTENT_HASH
            if (
                not (expected_missing and actual_hash is None)
                and actual_hash != write.expected_hash
            ):
                raise ContentHashMismatchError(write.path, write.expected_hash, actual_hash)
    all_required_guards = tuple(required_guards)
    if any(
        not isinstance(guard, (PathGuard, DirectoryCensusGuard)) for guard in all_required_guards
    ):
        raise PathGuardError("PATH_GUARD_INVALID", "unsupported required guard")
    read_only_guards = tuple(guard for guard in all_required_guards if isinstance(guard, PathGuard))
    directory_guards = tuple(
        guard for guard in all_required_guards if isinstance(guard, DirectoryCensusGuard)
    )
    # Access-tier backstop: check before staging and again immediately before
    # every destination replace so a live `_access.yaml` change cannot race a
    # long staging interval.
    if vault_root is not None:
        _validate_batch_write_access(Path(vault_root), writes)
    target_summary = _summarize_batch_targets(writes, vault_root)
    if (
        read_only_guards or directory_guards or any(write.guard is not None for write in writes)
    ) and vault_root is None:
        raise PathGuardError("PATH_GUARD_ROOT", "guarded writes require vault_root")
    created_dirs: list[Path | _CreatedDirectory] = []
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
        prepared = _prepare_path_guards(
            root,
            (*write_guards, *read_only_guards),
            created_dirs=created_dirs,
        )
        for position, guard in zip(guard_positions, prepared[: len(write_guards)], strict=True):
            bound_guards[position] = guard
        read_only_guards = prepared[len(write_guards) :]
        try:
            for guard in (
                *read_only_guards,
                *(item for item in bound_guards if item is not None),
            ):
                guard.recheck(root)
            for guard in directory_guards:
                guard.recheck(root, allowed_changes=(write.path for write in writes))
        except BaseException:
            _remove_empty_created_dirs(created_dirs)
            raise
    else:
        bound_guards = [None] * len(writes)
    for write in writes:
        if write.create_only and os.path.lexists(write.path):
            _remove_empty_created_dirs(created_dirs)
            raise CreateOnlyConflict(_safe_write_target(write.path, vault_root))

    # Record the graph debt here: after the guards have taken custody of the
    # namespace and created whatever parents they recorded as missing, and
    # still strictly before any canonical byte is replaced.
    #
    # Earlier is wrong even though the debt is known earlier. Opening the queue
    # database creates `Knowledge Base/` when it does not exist, and a batch
    # that is creating that directory has already captured it as a *missing*
    # parent it will create itself, in order. Creating it first fails the
    # guard's own recheck with `missing guard ancestor appeared`, surfaced to
    # the caller as `STALE_SEMANTIC_WRITE` -- a write invalidated by its own
    # bookkeeping, on every first write into a new directory.
    #
    # Later is also wrong: after the replace, a crash cut between the markdown
    # and the enqueue loses the dirty set, which is the whole property the
    # pre-commit ordering buys.
    if graph_debt_checkpoint is not None:
        _enqueue_graph_debt(Path(vault_root), graph_debt_checkpoint)

    workspace_by_parent: dict[Path, _BatchWorkspace] = {}
    staged: list[tuple[Path, _BatchWorkspace, _WorkspaceArtifact]] = []
    snapshots: list[_BatchSnapshot | None] = []
    source_guards: list[_BatchArtifactGuard | None] = []
    try:
        for write in writes:
            for directory in write.ensure_directories:
                if vault_root is None:
                    _create_parent_dirs(directory, created_dirs)
                else:
                    _create_parent_dirs_held(Path(vault_root), directory, created_dirs)
            if vault_root is None:
                _create_parent_dirs(write.path.parent, created_dirs)
            else:
                _create_parent_dirs_held(
                    Path(vault_root), write.path.parent, created_dirs
                )
            parent = Path(os.path.abspath(write.path.parent))
            if parent not in workspace_by_parent:
                workspace_by_parent[parent] = _BatchWorkspace.create(
                    parent,
                    vault_root=Path(vault_root) if vault_root is not None else None,
                )
        for index, write in enumerate(writes):
            workspace = workspace_by_parent[Path(os.path.abspath(write.path.parent))]
            artifact = workspace.create_artifact(
                f"stage-{index}.tmp", write.content.encode("utf-8")
            )
            staged.append((write.path, workspace, artifact))
        for final, _workspace, _artifact in staged:
            if not os.path.lexists(final):
                snapshots.append(None)
                source_guards.append(None)
                continue
            snapshot, source_guard = _capture_batch_snapshot(final)
            snapshots.append(snapshot)
            source_guards.append(source_guard)
    except BaseException as stage_error:
        if not isinstance(stage_error, Exception):
            _cleanup_batch_workspaces(workspace_by_parent.values())
            _remove_empty_created_dirs(created_dirs)
            raise
        retained_during_init = isinstance(stage_error, _BatchCleanupRetained)
        cause = stage_error.__cause__ if retained_during_init else stage_error
        if cause is not None and not isinstance(cause, Exception):
            _cleanup_batch_workspaces(workspace_by_parent.values())
            _remove_empty_created_dirs(created_dirs)
            raise cause from None
        cleanup_retained = _cleanup_batch_workspaces(workspace_by_parent.values())
        if retained_during_init or cleanup_retained:
            public_cause = cause or stage_error
            _remove_empty_created_dirs(created_dirs)
            raise BatchWriteError(
                "BATCH_CLEANUP_INCOMPLETE",
                target_summary,
                False,
                diagnostics=(public_cause,),
            ) from public_cause
        _remove_empty_created_dirs(created_dirs)
        raise

    allowed_census_changes = (
        *(write.path for write in writes),
        *(directory for write in writes for directory in write.ensure_directories),
        *(
            created.path if isinstance(created, _CreatedDirectory) else created
            for created in created_dirs
        ),
        *(item.path for item in workspace_by_parent.values()),
    )
    replaced: list[Path] = []
    final_guards: dict[Path, _BatchArtifactGuard] = {}
    try:
        from .writer_lease import (
            log_active_mutation_phase,
            mark_active_mutation_committed,
            validate_active_write_fence,
        )

        validate_active_write_fence()
        log_active_mutation_phase("canonical_commit_started", affected_count=len(staged))
        for index, (final, workspace, artifact) in enumerate(staged):
            for candidate_workspace in workspace_by_parent.values():
                candidate_workspace.recheck()
            for _pending_final, _pending_workspace, pending_artifact in staged[index:]:
                pending_artifact.recheck()
            for pending_index in range(index, len(staged)):
                source_guard = source_guards[pending_index]
                if source_guard is None:
                    if os.path.lexists(staged[pending_index][0]):
                        raise PathGuardError("PATH_GUARD_CHANGED", "batch destination appeared")
                else:
                    source_guard.recheck()
                    snapshot = snapshots[pending_index]
                    if snapshot is None:  # pragma: no cover - guard implies a snapshot
                        raise PathGuardError("PATH_GUARD_CHANGED", "batch snapshot is unavailable")
                    if staged[pending_index][0] not in {
                        graph_floor_path,
                        graph_checkpoint_path,
                    }:
                        _reset_restored_timestamps(
                            staged[pending_index][0], source_guard.identity, snapshot
                        )
            for guard in final_guards.values():
                guard.recheck()
            if vault_root is not None:
                root = Path(vault_root)
                _validate_batch_write_access(root, writes[index:])
                for guard in read_only_guards:
                    guard.recheck(root)
                for guard in bound_guards[index:]:
                    if guard is not None:
                        guard.recheck(root)
                for guard in directory_guards:
                    guard.recheck(root, allowed_changes=allowed_census_changes)
            if writes[index].create_only and os.path.lexists(final):
                raise CreateOnlyConflict(_safe_write_target(final, vault_root))
            artifact.recheck()
            expected_destination = (
                source_guards[index].identity
                if source_guards[index] is not None
                else None
            )
            with _batch_replace_context(
                Path(vault_root) if vault_root is not None else None,
                expected_destination,
            ):
                try:
                    installed_identity = workspace.replace_artifact(artifact, final)
                except BaseException:
                    installed_identity = workspace.bind_installed_after_error(
                        artifact,
                        final,
                        expected_destination=expected_destination,
                    )
                    if installed_identity is not None:
                        replaced.append(final)
                        final_guards[final] = _BatchArtifactGuard.capture(
                            final,
                            expected_content_hash=artifact.content_hash,
                            expected_identity=installed_identity,
                        )
                    raise
            replaced.append(final)
            final_guards[final] = _BatchArtifactGuard.capture(
                final,
                expected_content_hash=artifact.content_hash,
                expected_identity=installed_identity,
            )
            _after_batch_destination_published(final)
            workspace.recheck()
        for guard in read_only_guards:
            guard.recheck(Path(vault_root))
        for workspace in workspace_by_parent.values():
            workspace.recheck()
        for guard in final_guards.values():
            guard.recheck()
        if vault_root is not None:
            root = Path(vault_root)
            for guard in directory_guards:
                guard.recheck(root, allowed_changes=allowed_census_changes)
        for guard in final_guards.values():
            guard.recheck()
        log_active_mutation_phase("canonical_files_committed", affected_count=len(replaced))
        if replaced and commit_point:
            mark_active_mutation_committed()
    except Exception as commit_error:
        rollback_errors: list[BaseException] = []
        implicated_workspaces: list[_BatchWorkspace] = []
        replaced_indexes = range(len(replaced) - 1, -1, -1)
        for replaced_index in replaced_indexes:
            final, workspace, _artifact = staged[replaced_index]
            snapshot = snapshots[replaced_index]
            try:
                final_guard = final_guards.get(final)
                if final_guard is None:
                    raise PathGuardError(
                        "PATH_GUARD_CHANGED", "committed batch artifact is unbound"
                    )
                final_guard.recheck()
                workspace.recheck()
                if vault_root is not None:
                    _recheck_rollback_directory_guards(
                        directory_guards,
                        Path(vault_root),
                        final,
                        allowed_changes=allowed_census_changes,
                    )
                if snapshot is None:
                    workspace.unlink_installed(final, final_guard.identity)
                else:
                    restore = workspace.create_artifact(
                        f"restore-{replaced_index}.tmp", snapshot.content
                    )
                    _apply_snapshot_metadata(restore, snapshot)
                    restore.recheck(verify_content=False)
                    final_guard.recheck()
                    with _batch_replace_context(
                        Path(vault_root) if vault_root is not None else None,
                        final_guard.identity,
                        snapshot,
                    ):
                        restored_identity = workspace.replace_artifact(restore, final)
                    _BatchArtifactGuard.capture(
                        final,
                        expected_content_hash=snapshot.content_hash,
                        expected_identity=restored_identity,
                    )
                    _reset_restored_timestamps(final, restored_identity, snapshot)
                    workspace.recheck()
                final_guards.pop(final, None)
            except Exception as rollback_error:  # noqa: BLE001 - report every restore failure
                rollback_errors.append(rollback_error)
                if all(workspace is not item for item in implicated_workspaces):
                    implicated_workspaces.append(workspace)
        cleanup_retained = _cleanup_batch_workspaces(
            workspace_by_parent.values(), retained=implicated_workspaces
        )
        if rollback_errors:
            _remove_empty_created_dirs(created_dirs)
            raise BatchWriteError(
                "BATCH_ROLLBACK_INCOMPLETE",
                target_summary,
                False,
                diagnostics=rollback_errors,
            ) from commit_error
        if cleanup_retained:
            _remove_empty_created_dirs(created_dirs)
            raise BatchWriteError(
                "BATCH_CLEANUP_INCOMPLETE",
                target_summary,
                False,
            ) from commit_error
        _remove_empty_created_dirs(created_dirs)
        raise
    except BaseException:
        _cleanup_batch_workspaces(workspace_by_parent.values())
        _remove_empty_created_dirs(created_dirs)
        raise
    else:
        cleanup_retained = _cleanup_batch_workspaces(workspace_by_parent.values())

    if post_commit_fanout:
        created_paths = [
            final
            for (final, _workspace, _artifact), snapshot in zip(
                staged, snapshots, strict=True
            )
            if snapshot is None
        ]
        graph_epoch_paths = {graph_floor_path, graph_checkpoint_path}
        fanout_replaced = [path for path in replaced if path not in graph_epoch_paths]
        created_paths = [path for path in created_paths if path not in graph_epoch_paths]
        post_commit_batch_fanout(
            vault_root,
            fanout_replaced,
            index_reports,
            semantic_states,
            created_paths=created_paths,
        )
    if cleanup_retained:
        raise BatchWriteError(
            "BATCH_CLEANUP_INCOMPLETE",
            target_summary,
            True,
        )
    if deferred_checkpoint is not None:
        return DeferredGraphCompletion(
            tuple(path for path in replaced if path != graph_floor_path),
            deferred_checkpoint,
            deferred_predecessor,
        )
    return [write.path for write in caller_writes]


def _validate_batch_write_access(
    vault_root: Path,
    writes: Iterable[PlannedWrite],
) -> None:
    """Fail closed when any planned target is outside or newly write-protected."""
    from . import access

    vault_resolved = vault_root.resolve()
    for write in writes:
        for target in (write.path, *write.ensure_directories):
            try:
                rel = target.resolve().relative_to(vault_resolved).as_posix()
            except (ValueError, OSError) as error:
                raise ValueError(
                    f"WRITE_REFUSED: {target} resolves outside the vault root"
                ) from error
            reason = access.writable_reason(vault_root, rel)
            if reason is not None:
                raise ValueError(f"WRITE_REFUSED: {rel}: {reason}")


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


def _canonical_kb_segment(rel: str) -> str:
    """Re-spell a leading KB segment to the literal ``kb_dirname()`` spelling.

    On a case-insensitive filesystem the real on-disk casing of the governed
    folder may differ from the configured name (`knowledge base` vs
    `Knowledge Base`). The identity census keys segment 1 literally and every
    `rel.startswith(kb_prefix())` check is byte-exact (e.g. memory_refs), so a
    canonicalized rel-form must still lead with the configured spelling.
    """
    head, sep, tail = rel.partition("/")
    kb = kb_dirname()
    if head != kb and head.casefold() == kb.casefold():
        return f"{kb}{sep}{tail}" if sep else kb
    return rel


def is_casing_only_rewrite(canonical: str, original: str) -> bool:
    """True when `canonical` differs from `original` in letter casing alone.

    THIS IS A SECURITY GUARD, not a nicety. `Path.resolve()` re-spells a path
    to its real on-disk casing — but it also follows symlinks, expands Windows
    8.3 short names and junctions, and collapses `..`. Any of those rewrite
    *which file* the path addresses. `hosted_transfer_routes.
    _open_bounded_vault_file` re-opens the returned rel-form component by
    component under `O_NOFOLLOW` precisely so a symlink raises `ELOOP`; handing
    it a rel-form that resolution already replaced with the symlink's target
    launders the link into a real file and defeats the check. So the canonical
    form is adopted only when the sole difference is casing; otherwise the
    caller's own spelling is returned and the downstream guards see what the
    caller actually asked for.

    `str.casefold()` — not `os.path.normcase()`. `posixpath.normcase()` is the
    identity function, so a normcase comparison is byte-exact on every
    non-Windows platform: it would reject the very re-spell this module exists
    to perform on macOS APFS and on case-folding Linux mounts (ext4 `+F`,
    CIFS), and would make the invariant mean something different per platform.
    `casefold()` is the Unicode-correct, platform-independent answer.

    The length guard closes casefold's non-length-preserving expansions.
    `'ß'.casefold() == 'ss'`, `'ﬁ'` folds to `'fi'`, `'İ'` to `'i'` plus a
    combining dot — spellings that name a *genuinely different* file, so a
    bare casefold comparison would call `straße.md -> STRASSE.md` a casing-only
    rewrite and launder that symlink. Comparing the pre-fold lengths rejects
    every such pair. (This is the same hazard `_probe_casefolds` documents for
    `swapcase()`.) The guard can only ever err toward returning the caller's
    literal form, which is the fail-open behavior this module already promises.
    """
    return len(canonical) == len(original) and canonical.casefold() == original.casefold()


def _respelled_against_disk(root: Path, relative: str) -> str | None:
    """Re-spell each existing component of *relative* with its on-disk casing.

    `Path.resolve()` reports the true casing on Windows, so this module got the
    re-spell for free there and assumed every platform behaved that way. macOS
    folds case exactly as NTFS does, but `realpath()` only resolves symlinks --
    it hands back whatever spelling it was given -- so `canonical_vault_rel`
    was an identity transform on the one other platform that needs it, and two
    spellings of one page kept two identities.

    Walks the components instead, taking each existing entry's real name. An
    exact match always wins, so a genuinely case-sensitive volume is never
    redirected to a differently-cased sibling. The first component that does
    not exist ends the walk and the remainder is preserved verbatim, matching
    the non-strict `resolve()` this supplements. Returns None if a directory
    cannot be read, so the caller keeps its fail-open behaviour.
    """
    parts = tuple(part for part in relative.split("/") if part)
    if not parts:
        return None
    current = root
    spelled: list[str] = []
    for index, part in enumerate(parts):
        folded = part.casefold()
        match: str | None = None
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.name == part:
                        match = part
                        break
                    if match is None and entry.name.casefold() == folded:
                        match = entry.name
        except OSError:
            return None
        if match is None:
            spelled.extend(parts[index:])
            break
        spelled.append(match)
        current = current / match
    return "/".join(spelled)


def canonical_vault_rel(vault_root: Path, rel: str) -> str:
    """Return `rel` re-spelled with the real on-disk casing under `vault_root`.

    On a case-insensitive filesystem (Windows NTFS, macOS APFS by default) a
    caller may address `Notes/Polly/x.md` while the directory on disk is
    `POLLY`. Both open the same file, but identity comparisons keyed on the
    path string then see two owners for one physical page. Non-strict
    `Path.resolve()` returns the real casing for the components that exist and
    preserves a not-yet-existing tail verbatim, so this handles both an edit
    (whole path exists) and a create into an existing differently-cased parent.

    Casing-only invariant: `Path.resolve()` also follows symlinks, expands
    Windows 8.3 short names and junctions, and collapses `..` — rewrites that
    change *which file* the rel-form addresses, laundering a symlink past the
    `O_NOFOLLOW` guard in `hosted_transfer_routes._open_bounded_vault_file`.
    So the resolved form is adopted only when `is_casing_only_rewrite` says the
    sole difference from the caller's path is casing; otherwise `rel` comes
    back untouched. A consequence: `Knowledge Base/../Knowledge Base/x.md` is
    no longer incidentally collapsed. That is intended — vault-escape is
    checked separately in `resolve_under_vault`, against the *resolved* path,
    and is unaffected.

    Fails open: any `OSError`/`ValueError` (vault momentarily unreachable, a
    path that escapes the root) returns `rel` unchanged — exactly today's
    behavior. On a case-sensitive filesystem this is an identity transform for
    existing paths.
    """
    try:
        root = Path(vault_root)
        canonical = (root / rel).resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return rel
    if not canonical or canonical == ".":
        return rel
    # Compare against the slash-normalized caller form: `Path` accepts `\` and
    # a leading `/`, and `as_posix()` has already normalized those away, so
    # comparing raw would reject a legitimate re-spell over pure spelling noise.
    cleaned = str(rel).replace("\\", "/").lstrip("/")
    if canonical == cleaned and os.name != "nt" and vault_casefolds(root):
        # `resolve()` handed back the caller's own spelling. Either it was
        # already correct or this platform does not re-spell at all; only a
        # folding volume can tell those apart, and only there are the extra
        # directory reads worth taking.
        respelled = _respelled_against_disk(root, canonical)
        if respelled:
            canonical = respelled
    canonical = _canonical_kb_segment(canonical)
    if not is_casing_only_rewrite(canonical, cleaned):
        return rel
    return canonical


# Probing the filesystem costs a couple of syscalls; the answer is a property
# of the mount, so cache it per normcased root spelling. Tests reset it.
# Deliberately NOT keyed on a *resolved* root: `Path.resolve()` is itself the
# expensive syscall this cache exists to avoid (~120 us on Windows), and the
# guard path calls this once per page — `evaluate_posthoc_batch` loops over the
# whole vault. Two spellings of one root simply get two entries holding the
# same answer, which is harmless because the answer describes the volume.
_CASEFOLD_PROBE_CACHE: dict[str, bool] = {}
_CASEFOLD_PROBE_LOCK = threading.Lock()


def reset_casefold_probe_cache() -> None:
    """Drop the memoized `vault_casefolds` answers (tests, vault relocation)."""
    with _CASEFOLD_PROBE_LOCK:
        _CASEFOLD_PROBE_CACHE.clear()


def _probe_casefolds(root: Path) -> bool | None:
    """Ask the filesystem whether it folds case, or None when unprobeable.

    Takes one existing entry below the KB root (falling back to the vault root)
    whose name `swapcase()`s to a purely re-cased sibling, and checks that the
    swapped spelling both exists and is the *same* file. Returns None when no
    suitable entry is available, so the caller can fall back to platform policy
    instead of guessing.

    Only a candidate whose swap is a pure re-casing can answer the question.
    `swapcase()` is not length-preserving for every character — `straße` swaps
    to `STRASSE`, `ﬁ` to `FI`, `ŉ` to `ʼN`, `İ` to `i` plus a combining dot —
    and those spellings name a genuinely *different* file, so concluding from
    them would report a case-folding volume as case-sensitive. Unsuitable
    candidates are skipped rather than answered from, as is one that vanishes
    mid-probe; only a candidate that round-trips can yield `False`.
    """
    for base in (kb_root(root), root):
        try:
            for entry in base.iterdir():
                name = entry.name
                swapped_name = name.swapcase()
                if (
                    swapped_name == name
                    or len(swapped_name) != len(name)
                    or swapped_name.swapcase() != name
                    or not entry.exists()
                ):
                    continue
                swapped = base / swapped_name
                if not swapped.exists():
                    if not entry.exists():
                        # Deleted between the two probes: a race, not a verdict.
                        continue
                    return False
                return os.path.samefile(entry, swapped)
        except OSError:
            continue
    return None


def vault_casefolds(vault_root: Path) -> bool:
    """True when `vault_root` lives on a case-insensitive filesystem.

    `EXOMEM_CASEFOLD_PATHS=1|0` overrides the answer without probing — that is
    how Linux CI exercises the folding branch. Its reach is exactly this
    predicate: the case-folded identity *comparison* (whether two spellings
    count as one owner), and the supplementary re-spell walk described below.
    It does not disable path canonicalization — `canonical_vault_rel` /
    `resolve_under_vault` still re-spell through `Path.resolve()` on every
    platform. That is the whole re-spell on Windows, where `resolve()` reports
    true casing; POSIX `realpath()` does not, so on a folding volume there
    `canonical_vault_rel` walks the components itself, and this predicate is
    what says the volume folds. Otherwise the filesystem is probed once
    per root; when nothing is probeable the platform default
    (`os.path.normcase`) decides.
    """
    override = os.environ.get("EXOMEM_CASEFOLD_PATHS", "").strip()
    if override in {"0", "1"}:
        return override == "1"

    root = Path(vault_root)
    cache_key = os.path.normcase(str(root))
    with _CASEFOLD_PROBE_LOCK:
        cached = _CASEFOLD_PROBE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    probed = _probe_casefolds(root)
    folds = os.path.normcase("Aa") == os.path.normcase("aa") if probed is None else probed
    with _CASEFOLD_PROBE_LOCK:
        _CASEFOLD_PROBE_CACHE[cache_key] = folds
    return folds


@dataclass(frozen=True)
class VaultPathResolution:
    """Lexical and resolved forms from one vault-containment resolution."""

    candidate: Path
    relative: str
    resolved: Path
    resolved_relative: str


def resolve_under_vault(
    vault_root: Path,
    path: str,
    *,
    must_exist: bool = False,
    must_be_file: bool = False,
    must_be_dir: bool = False,
    must_be_under_kb: bool = False,
    return_details: bool = False,
) -> tuple[Path, str] | VaultPathResolution:
    """Resolve a vault-relative path; guard against escape; normalize.

    Returns `(absolute_path, vault_relative_posix)`. With
    `return_details=True`, returns both lexical and resolved forms from the
    same containment resolution. The ordinary relative form remains lexical
    across symlinks so downstream no-follow guards still see the path the
    caller supplied.

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

    # Normalize the *returned* rel-form to the real on-disk casing, reusing the
    # `resolved` computed for the escape check above (zero extra syscalls). The
    # drive-lowercasing that made us prefer the literal candidate-form is not a
    # concern here: `relative_to` strips the drive entirely. Fail open to the
    # literal form if the relative computation can't be established.
    #
    # Adopted ONLY when the rewrite is casing-only. `resolved` has followed any
    # symlink, and `_open_bounded_vault_file` re-opens this rel-form under
    # `O_NOFOLLOW` expecting to *reject* one — handing it the link's target
    # would launder the link into a real file. See `is_casing_only_rewrite`.
    try:
        canonical = resolved.relative_to(vault_resolved).as_posix()
    except ValueError:
        canonical = ""
    resolved_rel = rel
    if canonical and canonical != ".":
        canonical = _canonical_kb_segment(canonical)
        resolved_rel = canonical
        if is_casing_only_rewrite(canonical, rel):
            rel = canonical
    if return_details:
        return VaultPathResolution(
            candidate=candidate,
            relative=rel,
            resolved=resolved,
            resolved_relative=resolved_rel,
        )
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
    # A CRLF page leaves the blank line after `---` at the head of the body,
    # so stripping only LF made this contract silently platform-dependent.
    if body.startswith("\r\n"):
        body = body[2:]
    elif body.startswith("\n"):
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
        if all(not isinstance(item, (dict, list)) for item in value):
            items = ", ".join(_yaml_scalar(item) for item in value)
            return f"{key}: [{items}]"
        block = yaml.safe_dump({key: value}, default_flow_style=False, sort_keys=False)
        return block.rstrip("\n")
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
            try:
                relative = child.relative_to(vault_root).as_posix()
            except ValueError:
                continue
            if reserved_paths.classify_logical(relative).blocked:
                continue
            try:
                info = child.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
                continue
            if stat.S_ISDIR(info.st_mode):
                if in_excluded_scan_dir(relative):
                    continue
                yield from walk(child)
            elif (
                stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1
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
            try:
                text, _guard = read_guarded_text(vault_root, abs_path)
            except (OSError, UnicodeDecodeError, PathGuardError):
                if rel in self.known_rels:
                    stem = Path(rel).stem
                    count = self.stem_counts.get(stem, 0) - 1
                    if count > 0:
                        self.stem_counts[stem] = count
                    else:
                        self.stem_counts.pop(stem, None)
                    self.known_rels.discard(rel)
                continue
            if rel not in self.known_rels:
                stem = Path(rel).stem
                self.stem_counts[stem] = self.stem_counts.get(stem, 0) + 1
                self.known_rels.add(rel)
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
    seq = 0
    for md in walk_vault_md(vault_root):
        try:
            md_rel = md.relative_to(vault_root).as_posix()
            text, _guard = read_guarded_text(vault_root, md)
        except (OSError, UnicodeDecodeError, PathGuardError, ValueError):
            continue
        stem_counts[md.stem] = stem_counts.get(md.stem, 0) + 1
        known_rels.add(md_rel)
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


def evict_inbound_index(vault_root: Path) -> bool:
    """Withdraw one vault's rebuildable inbound-link projection."""
    return _INBOUND_INDEX.pop(str(Path(vault_root).resolve()), None) is not None


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

    def title_key_for_path(self, rel_path: str) -> str | None:
        """Return the normalized resolver title contributed by one path."""
        no_ext = str(rel_path).replace("\\", "/").lstrip("/").removesuffix(".md")
        return self._title_by_rel.get(no_ext)

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

    def on_entries_changed(
        self,
        entries: Iterable[tuple[str, str | None]],
        deleted_rels: Iterable[str],
    ) -> None:
        """Apply already-guarded resolver entries without another file read.

        Event-driven callers that have bound a title to an exact source
        snapshot use this instead of ``on_files_changed``.  Keeping the map
        mutation separate from I/O prevents a later read from being stamped as
        an earlier freshness target.
        """

        def _no_ext(rel: str) -> str:
            return str(rel).replace("\\", "/").lstrip("/").removesuffix(".md")

        changed = {
            _no_ext(rel): (str(title).strip().lower() if title and str(title).strip() else None)
            for rel, title in entries
        }
        deleted = {_no_ext(rel) for rel in deleted_rels}
        for no_ext in deleted | set(changed):
            self._remove_entry(no_ext)
        for no_ext, title_lower in sorted(changed.items()):
            self._add_entry(no_ext, title_lower)

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


def document_newline(text: str) -> str:
    """Report the line ending a document already uses, so edits match it.

    Mixing endings inside one page breaks more than tidiness:
    `prepend_log_entry` proves idempotency by asking whether the rendered
    entry is already present, and an LF entry is never found in a CRLF log,
    so a replayed write appends a duplicate instead of returning unchanged.
    """
    return "\r\n" if "\r\n" in text else "\n"


def render_frontmatter_document(
    fm_text: str, body: str, *, newline: str = "\n", blank_line: bool = False
) -> str:
    """Compose `---`-delimited frontmatter and body in one line ending.

    Every rewrite path used to hardcode LF delimiters and hardcode LF when
    appending a YAML key. A page a Windows editor had saved as CRLF then came
    back with both endings mixed inside it, and the rewrite fired even when
    the pass had nothing to change -- which is how `audit_fix` reported
    spurious wikilink fixes for pages it was contractually leaving alone.

    `body` is passed through byte-for-byte; only the delimiters and the
    frontmatter block, which callers build themselves, are normalized.
    """
    block = fm_text.replace("\r\n", "\n").replace("\n", newline)
    lead = newline if blank_line else ""
    return f"---{newline}{block}{newline}---{newline}{lead}{body}"


def _find_log_separator(log_text: str) -> tuple[int, str]:
    """Locate the log's header separator whatever line endings the file carries.

    exomem emits `log.md` with LF, but any Windows editor that rewrites the
    file leaves CRLF behind, and the separator then carries a CR before each
    LF. Both callers degrade silently when the lookup misses: `prepend_log_entry`
    appends the entry at the bottom instead of below the header, and
    `_plan_log_content` stops rotating and lets the log grow unbounded, which
    puts every write back to O(log size). Return the separator that actually
    matched so callers keep the file's own newlines.
    """
    for separator in ("\n---\n", "\r\n---\r\n"):
        index = log_text.find(separator)
        if index != -1:
            return index, separator
    return -1, "\n---\n"


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
    newline = document_newline(log_text)
    new_entry = f"## [{date_iso}] {op} | {title}\n\n{escape_wikilinks_for_log(body)}\n"
    new_entry = new_entry.replace("\r\n", "\n").replace("\n", newline)
    if new_entry in log_text:
        return log_text
    # Reuse the same separator the indexes module emits.
    sep_idx, separator = _find_log_separator(log_text)
    if sep_idx == -1:
        return log_text.rstrip() + newline * 2 + new_entry + newline
    insertion_point = sep_idx + len(separator)
    return (
        log_text[:insertion_point]
        + newline
        + new_entry
        + newline
        + log_text[insertion_point:]
    )


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
    sep_idx, separator = _find_log_separator(log_text)
    starts: list[int] = []
    if rotate and sep_idx != -1:
        head_end = sep_idx + len(separator)
        starts = [match.start() for match in _LOG_ENTRY_START_RE.finditer(log_text[head_end:])]
    if rotate and sep_idx != -1 and len(starts) > LOG_ROTATE_KEEP_ENTRIES:
        head_end = sep_idx + len(separator)
        entries_text = log_text[head_end:]
        cut = starts[LOG_ROTATE_KEEP_ENTRIES]
        live_text = log_text[:head_end] + entries_text[:cut]
        tail = entries_text[cut:]
        moved = len(starts) - LOG_ROTATE_KEEP_ENTRIES
        newline = document_newline(log_text)
        archive_text = (
            f"# log.md archive segment ({token_hash}){newline}{newline}"
            f"Rotated out of `{kb_prefix()}log.md` — {moved} entrie(s), newest "
            f"first, byte-exact.{newline}{separator}{tail}"
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
        return LogWritePlan((), warning=f"{kb_prefix()}log.md missing; skipped log entry")
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
            operation_token="standalone-rotation:"
            + hashlib.sha256(text.encode("utf-8")).hexdigest(),
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


# Matches a single log.md entry header, at either recorded precision:
#   `## [2026-06-23] edit | Notes/Insights/foo`
#   `## [2026-06-23T09:12:33Z] edit | Notes/Insights/foo`
# `op` is a single whitespace-free token; the title runs to end-of-line.
#
# The optional time group is what makes same-day edits orderable — three
# entries written within one afternoon used to read identically, leaving
# position in the file as the only ordering signal. A header this fails to
# match is not an error anywhere downstream: `read_log_entries` simply returns
# nothing, so `read_memory(include_history=true)` would lose the page's history
# silently. Widen this before anything can write the longer form.
_LOG_ENTRY_HEADER_RE = re.compile(
    r"^## \[(\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}Z)?)\] (\S+) \| (.+)$",
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
