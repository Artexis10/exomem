"""Project-key registry — single source of truth lives in the vault.

`Knowledge Base/_Schema/project-keys.yaml` declares every valid `project:`
value for research-notes and every valid entry for the `projects:` list on
insights/failures/patterns/production-logs.

Adding a key is a YAML edit, not a code change. Validation in `note.py`
calls into this module to get the current accepted set + folder mapping.

If the YAML is missing or unparseable we fall back to a small neutral
starter set so the writer never refuses every project key; a warning
lands in the service log when it fires.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .kbdir import managed_join
from .vault import (
    FILENAME_STYLES,
    InvalidSlugError,
    PathGuard,
    PlannedWrite,
    UnparseableFilenameStyleError,
    batch_atomic_write,
    corrected_title_name,
    folder_variants,
    kb_root,
    managed_root_rel,
    read_bytes_without_pinning,
    read_guarded_text,
)

log = logging.getLogger(__name__)


def registry_relpath() -> str:
    """The project registry's vault-relative path: the path a release decision names it by."""
    return managed_join("_Schema/project-keys.yaml")


# Fallback used when project-keys.yaml is missing or malformed. A neutral
# starter set matching `_scaffold/_Schema/project-keys.yaml`, so a vault whose
# config disappeared still accepts the shipped defaults. Real scopes live in the
# vault's project-keys.yaml and auto-register on first write.
_FALLBACK_PROJECTS: dict[str, str] = {
    "personal": "Personal",
    "project-alpha": "Project Alpha",
    "project-beta": "Project Beta",
    "work": "Work",
}


@dataclass(frozen=True)
class ProjectRegistry:
    """Snapshot of the project keys + folder mapping at load time.

    Frozen because keys are referenced from multiple write paths; mutation
    would create inconsistent views across calls.
    """

    project_to_folder: dict[str, str]
    project_to_category: dict[str, str]
    # Keys whose registry entry declares `tenant_scoped: true`: the only
    # projects whose pages may carry a `tenant:` field.
    tenant_scoped: frozenset[str] = frozenset()
    # The domain keys a registry entry declares for its pages (`domain:`, a
    # value or a list), as written: the domains resolver reads them, not this
    # loader. Only keys that set the field appear.
    project_to_domains: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(self.project_to_folder.keys())

    def folder_for(self, key: str) -> str | None:
        return self.project_to_folder.get(key)

    def category_for(self, key: str) -> str:
        return self.project_to_category.get(key, "uncategorized")

    def domains_for(self, key: str) -> tuple[str, ...]:
        return self.project_to_domains.get(key, ())

    @property
    def content_hash(self) -> str:
        """Identity of the effective keys, folders, categories and domains, not YAML formatting.

        A key's domains join its tuple only when it sets some, so a registry
        that never uses the field hashes exactly as it did before the field
        existed and an upgrade alone rebuilds nothing keyed on this hash.
        """
        entries: list[tuple] = []
        for key in sorted(self.keys):
            entry = (key, self.folder_for(key), self.category_for(key))
            domains = self.domains_for(key)
            entries.append((*entry, sorted(domains)) if domains else entry)
        return hashlib.sha256(
            json.dumps(entries, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class ProjectKeyIntroduction:
    key: str
    folder: str
    category: str


@dataclass(frozen=True)
class ProjectKeyPlan:
    registry: ProjectRegistry
    introductions: tuple[ProjectKeyIntroduction, ...]
    writes: tuple[PlannedWrite, ...]

    @property
    def introduced_keys(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.introductions)


def load_project_registry(vault_root: Path) -> ProjectRegistry:
    """Read `_Schema/project-keys.yaml` and return a typed registry.

    Returns a fallback registry on any read/parse failure, with a warning
    logged so service-log readers can see the misconfiguration.
    """
    path = Path(vault_root) / registry_relpath()
    try:
        # A store-mode vault keeps this registry in its store; the file is only a projection.
        text = read_bytes_without_pinning(path, vault_root).decode("utf-8")
        data = yaml.safe_load(text) or {}
    except (FileNotFoundError, NotADirectoryError):
        log.warning(
            "project-keys.yaml missing at %s; using built-in fallback set", path
        )
        return _fallback_registry()
    except (OSError, yaml.YAMLError) as e:
        log.warning(
            "project-keys.yaml unreadable (%s); using built-in fallback set", e
        )
        return _fallback_registry()

    registry = _registry_from_data(data)
    if registry is None:
        log.warning(
            "project-keys.yaml has no `projects:` mapping; using fallback"
        )
        return _fallback_registry()
    return registry


#: The parsed `filename_style` of each registry file, keyed by the file's bytes, so that naming a
#: corpus page by page parses the YAML once. Bounded: one entry per vault a process serves.
_STYLE_CACHE: dict[str, tuple[bytes, str | None]] = {}
_STYLE_CACHE_CAP = 32


def filename_style(vault_root: Path) -> str | None:
    """The vault's `filename_style` key, or None when it does not set one.

    Deliberately separate from `load_project_registry`: that reader answers with a fallback
    registry on any failure, which is right for project keys (a vault with none still has to
    route writes somewhere) and wrong here. No file, an empty file and a file without the key
    set no style, so the caller takes the default. Anything else that cannot be read as a style
    name raises `InvalidSlugError` with the file named: a file that cannot be opened, a document
    that is no mapping, and a key whose value is empty or no string. Text that does not parse
    raises its subclass `UnparseableFilenameStyleError`, which a write answers with the default
    style and a warning (`vault.derived_filename_style`). The vault may have chosen `slug`, and
    the default would silently name its new pages in another style, so none of these is silent.

    Returns the raw string; `vault.filename_style_source` checks it against the known styles,
    so an unrecognised name is refused there with the source named.

    Each call reads the file's bytes, from the store when the store owns the file. The parsed
    answer is cached by those bytes, so the cache is correct when the file changes. A refusal
    is not cached: it is rare and must name the file's current state.
    """
    path = Path(vault_root) / registry_relpath()
    try:
        raw = read_bytes_without_pinning(path, vault_root)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _style_refusal(error) from error
    cached = _STYLE_CACHE.get(str(path))
    if cached is not None and cached[0] == raw:
        return cached[1]
    value = _parse_filename_style(raw)
    if len(_STYLE_CACHE) >= _STYLE_CACHE_CAP:
        _STYLE_CACHE.pop(next(iter(_STYLE_CACHE)))
    _STYLE_CACHE[str(path)] = (raw, value)
    return value


def _style_refusal(error: Exception) -> InvalidSlugError:
    """The refusal for a registry whose style cannot be read."""
    reason = (str(error).splitlines() or [type(error).__name__])[0]
    # Text that does not parse is told apart from a file that cannot be opened: a write goes on
    # in the default style for the first, and refuses for the second, whose content is unknown.
    refusal = (
        UnparseableFilenameStyleError
        if isinstance(error, (UnicodeError, yaml.YAMLError))
        else InvalidSlugError
    )
    return refusal(f"filename style cannot be read from {registry_relpath()}: {reason}")


def _parse_filename_style(raw: bytes) -> str | None:
    """`filename_style` parsed from the registry file's bytes."""
    where = registry_relpath()
    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeError, yaml.YAMLError) as error:
        raise _style_refusal(error) from error
    if data is None:
        return None
    if not isinstance(data, dict):
        raise InvalidSlugError(f"filename style cannot be read from {where}: it is not a mapping of keys")
    if "filename_style" not in data:
        return None
    value = data["filename_style"]
    if not isinstance(value, str) or not value.strip():
        raise InvalidSlugError(
            f"filename style {value!r} from {where} is not one of {', '.join(FILENAME_STYLES)}"
        )
    return value


def _registry_from_data(data: object) -> ProjectRegistry | None:
    if not isinstance(data, dict):
        return None
    projects = data.get("projects")
    if not isinstance(projects, dict) or not projects:
        return None

    project_to_folder: dict[str, str] = {}
    project_to_category: dict[str, str] = {}
    tenant_scoped: set[str] = set()
    project_to_domains: dict[str, tuple[str, ...]] = {}
    for key, entry in projects.items():
        if not isinstance(key, str):
            continue
        if isinstance(entry, dict):
            folder = entry.get("folder") or _title_case_slug(key)
            category = entry.get("category") or "uncategorized"
            if entry.get("tenant_scoped") is True:
                tenant_scoped.add(key)
            if domains := _declared_domains(entry.get("domain")):
                project_to_domains[key] = domains
        elif isinstance(entry, str):
            # Tolerate short form `key: FolderName`.
            folder = entry
            category = "uncategorized"
        else:
            continue
        project_to_folder[key] = str(folder)
        project_to_category[key] = str(category)
    if not project_to_folder:
        return None
    return ProjectRegistry(
        project_to_folder=project_to_folder,
        project_to_category=project_to_category,
        tenant_scoped=frozenset(tenant_scoped),
        project_to_domains=project_to_domains,
    )


def _declared_domains(value: object) -> tuple[str, ...]:
    """The distinct non-empty strings of a `domain:` value or list, in written order."""
    items = value if isinstance(value, (list, tuple)) else (value,)
    return tuple(
        dict.fromkeys(item.strip() for item in items if isinstance(item, str) and item.strip())
    )


def keys_hint(vault_root: Path) -> str:
    """One-line, LLM-facing description of the live project-key set.

    Read at tool-registration time so the `note`/`link` tool schemas advertise
    the *current* keys instead of a frozen list that drifts out of sync with the
    YAML. The set is open — unknown slug-shaped keys auto-register on first write
    (see `register_project_key`) — so this is framed as non-exhaustive to stop
    the agent from treating a new scope as illegal. Single line (no newlines) so
    it survives Google-docstring parsing as one parameter description.
    """
    registry = load_project_registry(vault_root)
    keys = ", ".join(registry.keys) or "(none yet)"
    return (
        "Any slug-shaped key is accepted; unknown keys auto-register on first "
        "use (a typo guard rejects near-misses within edit distance 2 of an "
        "existing key) and create the matching Notes/Research/<Folder>/. Pass "
        f"project_category to bucket a new key. Current keys (not exhaustive): {keys}."
    )


def _fallback_registry() -> ProjectRegistry:
    return ProjectRegistry(
        project_to_folder=dict(_FALLBACK_PROJECTS),
        project_to_category={k: "uncategorized" for k in _FALLBACK_PROJECTS},
    )


def reuse_folder_spelling(folder: str, existing: Iterable[str]) -> str:
    """The existing folder name that shares `folder`'s `folder_lookalike_key`, else `folder`.

    An exact match wins; among variants the first in sorted order is stable. `existing` holds
    folder names, so a name that differs only by case or separators is the same folder.
    """
    names = set(existing)
    if folder in names:
        return folder
    return next(iter(folder_variants(names, folder)), folder)


# The managed-root path of the folder that holds each registered project's research folder.
RESEARCH_PARENT = ("Notes", "Research")


def research_parent_rel() -> str:
    """The vault-relative folder that holds each registered project's research folder."""
    return "/".join((managed_root_rel(), *RESEARCH_PARENT))


def child_folder_names(directory: Path) -> list[str]:
    """Names of the folders already on disk directly under `directory`."""
    try:
        with os.scandir(directory) as entries:
            return sorted(entry.name for entry in entries if entry.is_dir())
    except OSError:
        return []


def _title_case_slug(key: str, existing: Iterable[str] = ()) -> str:
    """Auto-derive a folder name from a slug key (`project-alpha` → `Project Alpha`).

    A name in `existing` with the same `folder_lookalike_key` is reused as spelled.
    """
    return reuse_folder_spelling(" ".join(part.capitalize() for part in key.split("-")), existing)


_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}$")


class InvalidProjectFolderError(ValueError):
    """A caller-named project folder that is not one portable folder name."""


def require_portable_folder(folder: object) -> str:
    """`folder` when it is the name the vault's own writer would give it, else refuse.

    The rule is the note writer's (`vault.corrected_title_name`): one segment, NFC, no character
    a platform or the wikilink grammar refuses, no leading dot, no trailing dot or space, and no
    reserved device name.
    """
    if not isinstance(folder, str) or not folder or corrected_title_name(folder).name != folder:
        raise InvalidProjectFolderError(
            f"INVALID_PROJECT_FOLDER: project folder {folder!r} is not one portable folder name"
        )
    return folder


class ProjectKeyTypoError(ValueError):
    """Raised when a new key looks like a typo of an existing registered key.

    The caller's natural response is to re-call with the suggested key
    instead — an LLM agent sees `close_match` in the error and self-corrects.
    Hand-edit YAML if the new key really is a deliberate new concept.
    """

    def __init__(self, key: str, close_match: str, distance: int):
        self.key = key
        self.close_match = close_match
        self.distance = distance
        super().__init__(
            f"project key {key!r} looks like a typo of existing key "
            f"{close_match!r} (edit distance {distance}). Use the existing "
            f"key, or hand-edit _Schema/project-keys.yaml if this is a "
            f"deliberate new concept."
        )


def _levenshtein(a: str, b: str, *, max_dist: int = 3) -> int:
    """Standard Levenshtein with an early-exit cap.

    Returns `max_dist + 1` once the lower bound on remaining work exceeds
    `max_dist` — we don't care about exact distances above the guard
    threshold, only whether the key is "close" or not.
    """
    if a == b:
        return 0
    if abs(len(a) - len(b)) > max_dist:
        return max_dist + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        row_min = i
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(
                cur[j - 1] + 1,        # insertion
                prev[j] + 1,           # deletion
                prev[j - 1] + cost,    # substitution
            )
            if cur[j] < row_min:
                row_min = cur[j]
        if row_min > max_dist:
            return max_dist + 1
        prev = cur
    return prev[-1]


# Distance ≤ this many edits → assume typo and block. Single-char typos
# (e.g. project-alhpa/project-alpha) get caught; deliberately new keys with
# 3+ edits from anything existing flow through silently.
_TYPO_DISTANCE_THRESHOLD = 2


def _closest_existing_key(
    new_key: str, existing_keys: list[str]
) -> tuple[str, int] | None:
    """Return `(closest_key, distance)` if within the typo threshold, else None."""
    best: tuple[str, int] | None = None
    for existing in existing_keys:
        d = _levenshtein(new_key, existing, max_dist=_TYPO_DISTANCE_THRESHOLD)
        if d <= _TYPO_DISTANCE_THRESHOLD:
            if best is None or d < best[1]:
                best = (existing, d)
    return best


def _bootstrap_registry_text() -> str:
    lines = [
        "# Project keys for research-notes and cross-cutting projects: list.",
        "# exomem loads this at startup and auto-appends new keys on use.",
        "",
        "projects:",
    ]
    for key, folder in _FALLBACK_PROJECTS.items():
        lines.extend(
            (f"  {key}:", f"    folder: {folder}", "    category: uncategorized")
        )
    return "\n".join(lines) + "\n"


def plan_project_keys(
    vault_root: Path,
    keys: list[str] | tuple[str, ...],
    *,
    category: str = "uncategorized",
    folders: dict[str, str] | None = None,
    replay_introductions: tuple[ProjectKeyIntroduction, ...] = (),
) -> ProjectKeyPlan:
    """Purely plan a folded registry update for zero or more project keys."""
    if type(keys) not in {list, tuple} or len(keys) > 64:
        raise ValueError("project key plan must contain at most 64 keys")
    root = Path(vault_root)
    path = root / registry_relpath()
    try:
        text, guard = read_guarded_text(root, path)
        path_exists = True
        try:
            parsed = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            parsed = {}
        current = _registry_from_data(parsed) or _fallback_registry()
    except FileNotFoundError:
        text = _bootstrap_registry_text()
        guard = PathGuard.capture(
            root, path.relative_to(root).as_posix(), leaf_policy="absent"
        )
        path_exists = False
        current = _fallback_registry()
    proposed_folders = dict(current.project_to_folder)
    proposed_categories = dict(current.project_to_category)
    introductions: list[ProjectKeyIntroduction] = []
    replay_by_key = {item.key: item for item in replay_introductions}
    seen: set[str] = set()
    known_folders = sorted(set(proposed_folders.values()) | set(child_folder_names(kb_root(root).joinpath(*RESEARCH_PARENT))))
    for raw in keys:
        if type(raw) is not str or not _SLUG_RE.fullmatch(raw):
            raise ValueError(
                f"project key {raw!r} is not a valid slug "
                f"(must match {_SLUG_RE.pattern}; lowercase + dashes)"
            )
        if raw in seen:
            continue
        seen.add(raw)
        replay = replay_by_key.get(raw)
        if raw in proposed_folders:
            if replay is not None:
                if (
                    proposed_folders[raw] != replay.folder
                    or proposed_categories.get(raw, "uncategorized") != replay.category
                ):
                    raise ValueError("prepared project registration no longer matches")
                introductions.append(replay)
            continue
        close = _closest_existing_key(raw, list(proposed_folders))
        if close is not None:
            raise ProjectKeyTypoError(raw, close[0], close[1])
        named = (folders or {}).get(raw) if replay is None else None
        folder_name = replay.folder if replay is not None else (
            require_portable_folder(named) if named is not None else _title_case_slug(raw, known_folders)
        )
        variant = reuse_folder_spelling(folder_name, known_folders)
        if variant != folder_name:
            raise ValueError(
                f"PATH_VARIANT_EXISTS: project folder {folder_name!r} matches the "
                f"existing folder {variant!r} apart from case, Unicode form or separators; "
                "use that spelling"
            )
        known_folders.append(folder_name)
        item_category = replay.category if replay is not None else category
        introduction = ProjectKeyIntroduction(raw, folder_name, item_category)
        introductions.append(introduction)
        proposed_folders[raw] = folder_name
        proposed_categories[raw] = item_category

    registry = ProjectRegistry(proposed_folders, proposed_categories)
    if not introductions:
        return ProjectKeyPlan(registry, (), ())

    if not text.endswith("\n"):
        text += "\n"
    for item in introductions:
        if item.key in current.project_to_folder:
            continue
        # A caller may name the folder, so it is written as a quoted scalar.
        folder = yaml.safe_dump(item.folder, default_style='"', allow_unicode=True, width=1 << 16)
        text += (
            "\n  # auto-registered by exomem\n"
            f"  {item.key}:\n"
            f"    folder: {folder.strip()}\n"
            f"    category: {item.category}\n"
        )
    project_directories = tuple(
        kb_root(root).joinpath(*RESEARCH_PARENT, item.folder)
        for item in introductions
    )
    write = PlannedWrite(
        path,
        text,
        create_only=not path_exists,
        guard=guard,
        ensure_directories=project_directories,
    )
    return ProjectKeyPlan(registry, tuple(introductions), (write,))


def register_project_key(
    vault_root: Path,
    key: str,
    *,
    folder: str | None = None,
    category: str = "uncategorized",
) -> tuple[str, str, bool]:
    """Add a new project key to `_Schema/project-keys.yaml`. Idempotent.

    Returns `(key, folder, was_new)`. `was_new` is True when the key
    didn't exist before (so callers can surface a warning to the user).

    Design rationale: the writer auto-registers unknown project keys on
    use rather than refusing — you usually drive this through an LLM and
    shouldn't have to edit YAML by hand. Typo risk is mitigated by
    surfacing the registration as a warning every time it fires; you
    can revert via `move_file` + manual YAML edit if needed.

    Refuses non-slug keys (must match `^[a-z][a-z0-9-]{0,40}$`) so a stray
    `Vehicles` or `vehicles!` doesn't pollute the registry. The folder
    name is free-form (Title Case allowed).
    """
    plan = plan_project_keys(
        vault_root,
        [key],
        category=category,
        folders={key: folder} if folder is not None else None,
    )
    if not plan.introductions:
        return key, plan.registry.folder_for(key) or folder or _title_case_slug(key), False
    batch_atomic_write(plan.writes, vault_root=vault_root)
    introduction = plan.introductions[0]
    folder_path = kb_root(vault_root).joinpath(*RESEARCH_PARENT, introduction.folder)
    folder_path.mkdir(parents=True, exist_ok=True)
    return key, introduction.folder, True
