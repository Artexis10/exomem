"""Index/log update routines for SKILL.md rule 7 enforcement.

`add` calls into here to compute the new contents of:
- `Sources/index.md` (bump By-type count + prepend Recent-captures bullet)
- `Knowledge Base/index.md` (prepend Recent-activity cap-50 + bump Counts line)
- `log.md` (prepend most-recent-first entry)

Each function returns a PlannedWrite-ready string; nothing is written here.
The caller batches them with the source file into a single atomic write.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePosixPath

from . import call_spans, homes, note_types
from .entity_types import EntityTypeRegistry, core_registry, load_entity_types
from .kbdir import kb_prefix
from .vault import (
    PlannedWrite,
    document_newline,
    escape_wikilinks_for_log,
    kb_root,
    read_guarded_text,
    render_wikilinks_for_vault,
)

log = logging.getLogger(__name__)

RECENT_ACTIVITY_CAP = 50

SOURCES_BY_TYPE_HEADER = "## By type"
SOURCES_RECENT_HEADER = "## Recent captures"
INDEX_RECENT_HEADER = "## Recent activity"
INDEX_COUNTS_HEADER = "## Counts"
NOTES_BY_TYPE_HEADER = "## By type"
LOG_SEPARATOR = "\n---\n"


def sources_index_path(vault_root: Path) -> Path:
    return kb_root(vault_root) / "Sources" / "index.md"


def _rewritten_in_place(text: str, rewrite: Callable[[str], str]) -> str:
    r"""Run an LF-only section rewriter without changing the document's endings.

    Every splice in this module is written against LF: `find("\n## ")`,
    `"\n".join(rows)`, `lstrip("\n")`. Handing one of them CRLF text does not
    fail loudly. `find("\n## ")` still matches the LF half of the pair, the
    surrounding `rstrip()` eats the `\r`, and the regenerated rows come back
    LF-only -- so the document ends up with mixed line endings and a blank line
    silently consumed.

    The damage is not confined to cosmetics. These rewriters are also the
    change detector: the caller writes only `if new != base`. A rewriter that
    re-emits different bytes for an index that needed no change makes every
    maintenance pass rewrite every index on a CRLF vault, while the report the
    user reads says nothing was rewritten. Normalizing in and restoring out
    keeps "nothing to do" byte-identical, which is what makes that comparison
    mean anything.
    """
    newline = document_newline(text)
    rewritten = rewrite(text.replace("\r\n", "\n").replace("\r", "\n"))
    if newline == "\n":
        return rewritten
    return rewritten.replace("\n", newline)


@dataclass
class IndexUpdate:
    sources_index_content: str
    top_index_content: str
    log_content: str
    trim_note: str | None  # populated if Recent activity cap-50 trimmed entries


def compute_updates(
    vault_root: Path,
    *,
    source_type: str,
    folder_title: str,  # e.g., "Articles", "Papers"
    folder_description: str,  # e.g., "captured web/PDF content"
    rel_source_path: str,  # vault-relative, e.g. "Knowledge Base/Sources/Papers/2026-05-18-foo"
    date_iso: str,
    activity_summary: str,
    log_entry_body: str,
    stamp_iso: str | None = None,
    source_counts: Mapping[str, int] | None = None,
) -> IndexUpdate:
    """Build the new contents of Sources/index.md, top-level index.md, and log.md.

    `rel_source_path` should be the vault-relative path WITHOUT `.md` (wikilink form).
    `activity_summary` is the one-liner that appears in the top index's Recent
    activity bullet AND in the log entry's body.

    `date_iso` is the calendar day: it names index rows and the human-facing
    Recent-activity bullet, where a time would be noise. `stamp_iso` is the
    recorded instant for the `log.md` heading, which is what makes several
    writes on one day orderable. It defaults to `date_iso` so callers that
    genuinely only have a day keep their existing behaviour.

    `source_counts`, when supplied, includes the caller's planned Source.
    The snapshot stays local to this update.
    """
    kb = kb_root(vault_root)
    sources_dir = kb / "Sources"
    sources_index = sources_index_path(vault_root)
    top_index = kb / "index.md"
    log_file = kb / "log.md"

    if not sources_index.exists():
        raise FileNotFoundError(f"Sources/index.md missing: {sources_index}")
    if not top_index.exists():
        raise FileNotFoundError(f"top index.md missing: {top_index}")
    if not log_file.exists():
        raise FileNotFoundError(f"log.md missing: {log_file}")

    counts = _count_sources(sources_dir) if source_counts is None else dict(source_counts)

    sources_index_new = _update_sources_index(
        sources_index.read_text(encoding="utf-8"),
        folder_title=folder_title,
        folder_description=folder_description,
        counts=counts,
        date_iso=date_iso,
        rel_source_path=rel_source_path,
        vault_root=vault_root,
    )

    top_index_new, trim_note = _update_top_index(
        top_index.read_text(encoding="utf-8"),
        counts=counts,
        date_iso=date_iso,
        activity_summary=activity_summary,
    )

    log_new = _update_log(
        log_file.read_text(encoding="utf-8"),
        date_iso=stamp_iso or date_iso,
        rel_source_path=rel_source_path,
        log_entry_body=log_entry_body
        + (f"\n\n{trim_note}" if trim_note else ""),
    )

    return IndexUpdate(
        sources_index_content=render_wikilinks_for_vault(sources_index_new, vault_root),
        top_index_content=render_wikilinks_for_vault(top_index_new, vault_root),
        log_content=render_wikilinks_for_vault(log_new, vault_root),
        trim_note=trim_note,
    )


def _count_markdown_pages(
    root: Path,
    *,
    skip_underscore_dirs: bool = False,
    keep: Callable[[str], bool] | None = None,
    on_error: Callable[[OSError], None] | None = None,
) -> int:
    """Non-index `*.md` entries under `root`, recursively, in one scandir pass.

    The same set `root.rglob("*.md")` minus `index.md` yields (symlinked
    directories are not descended; a directory named `*.md` is counted and
    descended), without a `stat` per entry: this runs inside the commit of
    every write, so its cost is the write's hold time.

    `keep`, when given, receives each page's path as scanned and decides
    whether it counts, so a caller can count only what its audience may see.
    `on_error` reports incomplete reads without changing legacy best-effort counts.
    """
    count = 0
    pending = [os.fspath(root)]
    while pending:
        try:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    if (
                        entry.name.endswith(".md")
                        and entry.name != "index.md"
                        and (keep is None or keep(entry.path))
                    ):
                        count += 1
                    # A directory named `x.md` is counted above and still descended.
                    if entry.is_dir(follow_symlinks=False) and not (
                        skip_underscore_dirs and entry.name.startswith("_")
                    ):
                        pending.append(entry.path)
        except OSError as error:
            if on_error is not None:
                on_error(error)
            continue
    return count


def _count_sources(
    sources_dir: Path, *, on_error: Callable[[OSError], None] | None = None
) -> dict[str, int]:
    """Per top-level source-type count, including themed nested folders."""
    out: dict[str, int] = {}
    try:
        entries = list(sources_dir.iterdir())
    except FileNotFoundError:
        return out
    except OSError as error:
        if on_error is None:
            raise
        on_error(error)
        return out
    for sub in entries:
        if sub.name.startswith("_"):
            continue
        try:
            if not stat.S_ISDIR(sub.stat().st_mode):
                continue
        except OSError as error:
            if on_error is None:
                raise
            on_error(error)
            continue
        out[sub.name] = _count_markdown_pages(
            sub, skip_underscore_dirs=True, on_error=on_error
        )
    return out


@dataclass(frozen=True, slots=True)
class _NotesOwner:
    """A Notes folder that one Counts row and one section of `Notes/index.md` report."""

    #: The folder's segments below Notes, as registered.
    parts: tuple[str, ...]
    #: The token of the folder's `- Notes (<token>): N` row.
    token: str
    #: The section titles that name the folder, preferred first.
    titles: tuple[str, ...]
    #: Set for a home, whose section this module writes when it is missing.
    description: str | None = None
    active: bool = True


def _notes_owners(home_set: homes.HomeSet | None) -> tuple[_NotesOwner, ...]:
    """The shipped compiled types' folders, then the vault's homes."""
    owners = [
        _NotesOwner(
            PurePosixPath(str(note_type.folder)).parts[1:],
            note_type.index_row or note_type.key,
            (PurePosixPath(str(note_type.folder)).name,),
        )
        for note_type in note_types.shipped(note_types.compiled)
    ]
    for home in home_set.homes if home_set is not None else ():
        owners.append(
            _NotesOwner(home.parts, home.key, (home.label, home.parts[-1]), home.description, home.active)
        )
    return tuple(owners)


def _owner_of(owners: tuple[_NotesOwner, ...], parts: tuple[str, ...]) -> _NotesOwner | None:
    """The deepest owner whose folder holds `parts`, so a nested home counts its own pages."""
    folded = tuple(part.casefold() for part in parts)
    best: _NotesOwner | None = None
    for owner in owners:
        own = tuple(part.casefold() for part in owner.parts)
        if folded[: len(own)] == own and (best is None or len(own) > len(best.parts)):
            best = owner
    return best


def _count_notes(notes_dir: Path, *, home_set: homes.HomeSet | None = None) -> dict[str, int]:
    """Per-folder compiled-note count, recursing into project/domain/medium subfolders.

    Returns a dict keyed by the token of each folder's `index.md` Counts row: the type's
    row for a shipped type folder (e.g. "research", "insight", "production-log") and the
    home's key for a home. A page counts once, under the deepest folder that owns it.
    Excludes index.md files and any folder starting with "_".
    """
    out: dict[str, int] = {}
    if not notes_dir.is_dir():
        return out
    owners = _notes_owners(home_set)
    by_name = {owner.parts[0].casefold(): owner for owner in owners if len(owner.parts) == 1}
    for sub in notes_dir.iterdir():
        if not sub.is_dir() or sub.name.startswith("_"):
            continue
        owner = by_name.get(sub.name.casefold())
        if owner is None:
            continue  # unknown top-level folder under Notes/; ignore
        out[owner.token] = _count_markdown_pages(sub)
    for owner in owners:
        if len(owner.parts) < 2:
            continue
        nested = notes_dir.joinpath(*owner.parts)
        if not nested.is_dir():
            continue
        pages = _count_markdown_pages(nested)
        out[owner.token] = out.get(owner.token, 0) + pages
        enclosing = _owner_of(tuple(o for o in owners if o is not owner), owner.parts[:-1])
        if enclosing is not None and enclosing.token in out:
            out[enclosing.token] -= pages
    return out


def _update_sources_index(
    text: str,
    *,
    folder_title: str,
    folder_description: str,
    counts: dict[str, int],
    date_iso: str,
    rel_source_path: str,
    vault_root: Path | None = None,
) -> str:
    """Bump the By-type count row and prepend a Recent-captures bullet.

    If a row for `folder_title` doesn't exist (auto-created folder), inject one
    in alphabetical position under "## By type".
    """
    text = _replace_by_type_section(
        text,
        folder_title=folder_title,
        folder_description=folder_description,
        counts=counts,
        vault_root=vault_root,
    )
    text = _prepend_recent_capture(text, date_iso=date_iso, rel_source_path=rel_source_path)
    return text


#: Descriptions for the folders that existed before the source taxonomy became
#: an open vocabulary, kept verbatim so upgrading never rewrites an existing
#: index row. Everything beyond these is described by the taxonomy registry, so
#: a kind Exomem never shipped still gets a real description — this table is no
#: longer a vocabulary, and nothing should be added to it.
_LEGACY_FOLDER_DESCRIPTIONS: dict[str, str] = {
    "Articles": "captured web/PDF content",
    "Sessions": "pasted Claude/conversation transcripts",
    "Books": "book notes/excerpts",
    "Papers": "academic papers",
    "Videos": "captured video transcripts/notes",
    "Other": "miscellaneous captures",
    "Imported": "copied legacy-vault material with provenance",
}


def _registry_descriptions(vault_root: Path | None) -> dict[str, str]:
    """Registry-declared folder descriptions; never fails an index write."""
    if vault_root is None:
        return {}
    try:
        from .source_taxonomy import load_taxonomy

        return load_taxonomy(vault_root).category_descriptions()
    except Exception:  # noqa: BLE001 — a description is never worth a failed write
        log.debug("source-taxonomy descriptions unavailable", exc_info=True)
        return {}


def _replace_by_type_section(
    text: str,
    *,
    folder_title: str,
    folder_description: str,
    counts: dict[str, int],
    vault_root: Path | None = None,
) -> str:
    """Rewrite the entire By-type list from disk counts so we don't drift."""
    rows: list[str] = []
    known_descriptions = dict(_registry_descriptions(vault_root))
    known_descriptions.update(_LEGACY_FOLDER_DESCRIPTIONS)
    if folder_title not in known_descriptions:
        known_descriptions[folder_title] = folder_description

    for name in sorted(counts.keys()):
        desc = known_descriptions.get(name, "captured material")
        rows.append(f"- [[{kb_prefix()}Sources/{name}/|{name}]] — {desc} ({counts[name]})")

    new_block = SOURCES_BY_TYPE_HEADER + "\n\n" + "\n".join(rows) + "\n"
    return _replace_section(text, SOURCES_BY_TYPE_HEADER, new_block, next_h2_or_end=True)


def _prepend_recent_capture(text: str, *, date_iso: str, rel_source_path: str) -> str:
    """Insert a new bullet at the top of the Recent captures list."""
    entry = f"- {date_iso} — [[{rel_source_path}]]"
    header_idx = text.find(SOURCES_RECENT_HEADER)
    if header_idx == -1:
        # No Recent captures section — append one at the end.
        return text.rstrip() + "\n\n" + SOURCES_RECENT_HEADER + "\n\n" + entry + "\n"
    # Find the blank line after the header.
    body_start = text.find("\n\n", header_idx)
    if body_start == -1:
        return text + "\n\n" + entry + "\n"
    body_start += 2
    return text[:body_start] + entry + "\n" + text[body_start:]


def _update_top_index(
    text: str,
    *,
    counts: dict[str, int],
    date_iso: str,
    activity_summary: str,
) -> tuple[str, str | None]:
    """Prepend Recent activity bullet (cap-50 trim) + rewrite the Sources Counts line."""
    text, trim_note = _prepend_recent_activity(
        text, date_iso=date_iso, summary=activity_summary
    )
    text = _rewrite_sources_count(text, counts=counts)
    return text, trim_note


def _prepend_recent_activity(
    text: str, *, date_iso: str, summary: str
) -> tuple[str, str | None]:
    """Insert `- <date> — <summary>` at the top of Recent activity. Trim to cap-50."""
    header_idx = text.find(INDEX_RECENT_HEADER)
    if header_idx == -1:
        return text, None
    # Find the comment block + blank line that precedes the list, then the list itself.
    section_end = text.find("\n## ", header_idx + len(INDEX_RECENT_HEADER))
    if section_end == -1:
        section_end = len(text)
    section = text[header_idx:section_end]
    lines = section.splitlines()
    # Locate where bullets start (first line beginning with "- ").
    bullet_start = None
    for i, line in enumerate(lines):
        if line.startswith("- "):
            bullet_start = i
            break
    if bullet_start is None:
        # No bullets yet — append after the section.
        new_section = section.rstrip("\n") + f"\n\n- {date_iso} — {summary}\n"
        return text[:header_idx] + new_section + text[section_end:], None

    bullets = [ln for ln in lines[bullet_start:] if ln.startswith("- ")]
    preamble = lines[:bullet_start]

    new_bullet = f"- {date_iso} — {summary}"
    if new_bullet in bullets:
        return text, None
    bullets.insert(0, new_bullet)

    trim_note: str | None = None
    if len(bullets) > RECENT_ACTIVITY_CAP:
        dropped = bullets[RECENT_ACTIVITY_CAP:]
        bullets = bullets[:RECENT_ACTIVITY_CAP]
        bottom_excerpt = dropped[0]
        # Pull just the date + first chunk of the dropped bullet for the note.
        trim_note = (
            f"(bottom entry drops off at cap-{RECENT_ACTIVITY_CAP}; "
            f"trimmed {len(dropped)} this write — bottom was: {bottom_excerpt[:120]}…)"
        )

    new_section = "\n".join(preamble + bullets) + "\n"
    return text[:header_idx] + new_section + text[section_end:], trim_note


SOURCES_COUNT_PATTERN = re.compile(r"^- Sources: .+$", re.MULTILINE)
# Top-index Counts rows: `- Notes (research): 106` or `- Entities (person): 21`.
_NOTES_COUNT_LINE = re.compile(
    r"^(- Notes \()([a-z0-9-]+)(\): )(\d+)\s*$", re.MULTILINE
)
_ENTITIES_COUNT_LINE = re.compile(
    r"^(- Entities \()([a-z-]+)(\): )(\d+)\s*$", re.MULTILINE
)
_NOTES_TOTAL_LINE = re.compile(r"^- Notes:\s*\d+\s*$", re.MULTILINE)
_ENTITIES_TOTAL_LINE = re.compile(r"^- Entities:\s*\d+\s*$", re.MULTILINE)


def _count_entities(
    entities_dir: Path,
    *,
    registry: EntityTypeRegistry | None = None,
) -> dict[str, int]:
    """Per-entity-type count, mirrors `_count_sources` shape.

    Returns a dict keyed by registered entity-type ID. Excludes index.md and
    `_*` folders.
    """
    out: dict[str, int] = {}
    if not entities_dir.is_dir():
        return out
    registry = registry or load_entity_types(entities_dir.parents[1])
    for sub in entities_dir.iterdir():
        if not sub.is_dir() or sub.name.startswith("_"):
            continue
        definition = registry.by_folder.get(sub.name.casefold())
        if definition is None:
            continue
        out[definition.id] = sum(
            1
            for f in sub.rglob("*.md")
            if f.name != "index.md"
            and not any(part.startswith("_") for part in f.relative_to(sub).parts[:-1])
        )
    return out


def _rewrite_top_index_notes_and_entities_counts(
    text: str,
    *,
    notes_counts: dict[str, int],
    entities_counts: dict[str, int],
) -> str:
    """Rewrite per-type rows and ensure true Notes/Entities total rows."""
    def _replace_notes(m: re.Match[str]) -> str:
        type_key = m.group(2)
        actual = notes_counts.get(type_key)
        if actual is None:
            return m.group(0)  # unknown type; leave alone
        return f"{m.group(1)}{type_key}{m.group(3)}{actual}"

    def _replace_entities(m: re.Match[str]) -> str:
        type_key = m.group(2)
        actual = entities_counts.get(type_key)
        if actual is None:
            return m.group(0)
        return f"{m.group(1)}{type_key}{m.group(3)}{actual}"

    text = _NOTES_COUNT_LINE.sub(_replace_notes, text)
    text = _ENTITIES_COUNT_LINE.sub(_replace_entities, text)
    text = _rewrite_or_insert_total_count(
        text, label="Notes", total=sum(notes_counts.values()), pattern=_NOTES_TOTAL_LINE
    )
    text = _rewrite_or_insert_total_count(
        text,
        label="Entities",
        total=sum(entities_counts.values()),
        pattern=_ENTITIES_TOTAL_LINE,
    )
    return text


def _rewrite_or_insert_total_count(
    text: str, *, label: str, total: int, pattern: re.Pattern[str]
) -> str:
    line = f"- {label}: {total}"
    if pattern.search(text):
        return pattern.sub(line, text, count=1)
    counts_idx = text.find(INDEX_COUNTS_HEADER)
    if counts_idx == -1:
        return text
    section_end = text.find("\n## ", counts_idx + len(INDEX_COUNTS_HEADER))
    if section_end == -1:
        section_end = len(text)
    prefix = text[:section_end].rstrip("\n")
    suffix = text[section_end:]
    return f"{prefix}\n{line}\n{suffix}"


def _rewrite_sources_count(text: str, *, counts: dict[str, int]) -> str:
    """Rewrite the `- Sources: <total> (<type>: <n>, ...)` line under Counts."""
    total = sum(counts.values())
    # Lowercase singular type name expected: "articles" not "Articles". Match the
    # convention used in the existing file: lowercased folder name with trailing 's'
    # already (Articles → articles, etc.). For new types: lowercase + plural-ish.
    parts = ", ".join(
        f"{name.lower()}: {n}" for name, n in sorted(counts.items())
    )
    new_line = f"- Sources: {total} ({parts})" if parts else f"- Sources: {total}"
    if SOURCES_COUNT_PATTERN.search(text):
        return SOURCES_COUNT_PATTERN.sub(new_line, text, count=1)
    # Counts section exists but no Sources line — insert at top of Counts list.
    idx = text.find(INDEX_COUNTS_HEADER)
    if idx == -1:
        return text  # No Counts section to update; quiet no-op.
    body_start = text.find("\n\n", idx)
    if body_start == -1:
        return text
    body_start += 2
    return text[:body_start] + new_line + "\n" + text[body_start:]


# ---------------- sub-folder index refresh ----------------


# H3 header `### Research — project-or-domain-scoped synthesis (43)` — capture
# the type name (group 1), the description (group 2), and the count (group 3).
_NOTES_H3_COUNT = re.compile(
    r"^(### (?P<name>[^\W\d_][\w\- ]*?))( — [^\n]*?)? \((?P<count>\d+)\)\s*$",
    re.MULTILINE,
)

# Subfolder bullet in Notes/index.md:
# `- [[<KB>/Notes/Research/Project Alpha/|Project Alpha]] (2) — desc`
@cache
def _notes_subfolder_bullet_re(kb_prefix_str: str) -> re.Pattern[str]:
    return re.compile(
        r"^(- \[\[(?:" + re.escape(kb_prefix_str) + r")?"
        + r"Notes/(?P<typef>[A-Za-z]+)/(?P<sub>[^|\]/]+)/?(?:\|[^\]]+)?\]\])"
        r"( \((?P<count>\d+)\))?(?P<rest>(?:\s+—[^\n]*)?)\s*$",
        re.MULTILINE,
    )

# Entities/index.md top-level bullet:
# `- [[<KB>/Entities/People/|People]] (12)` (optional description)
@cache
def _entities_bullet_re(
    kb_prefix_str: str,
    core_version: int,
    extension_hash: str,
    folders: tuple[str, ...],
) -> re.Pattern[str]:
    del core_version, extension_hash
    folder_pattern = "|".join(re.escape(folder) for folder in folders)
    return re.compile(
        r"^(- \[\[(?:" + re.escape(kb_prefix_str) + r")?"
        + rf"Entities/(?P<folder>{folder_pattern})/?(?:\|[^\]]+)?\]\])"
        r"( \((?P<count>\d+)\))?(?P<rest>(?:\s+—[^\n]*)?)\s*$",
        re.MULTILINE,
    )


def _count_notes_by_subfolder(notes_dir: Path) -> dict[str, dict[str, int]]:
    """Return nested counts: `{type_folder: {subfolder: count}}`.

    For `Research`, `Experiments`, `Productions` (the nested types), the inner
    dict has per-subfolder counts (`Project Alpha`, `Health`, ...). For
    flat types (`Insights`, `Failures`, `Patterns`), the inner dict has a
    single `''` key with the total.
    """
    out: dict[str, dict[str, int]] = {}
    if not notes_dir.is_dir():
        return out
    for type_folder in notes_dir.iterdir():
        if not type_folder.is_dir() or type_folder.name.startswith("_"):
            continue
        inner: dict[str, int] = {}
        # Detect whether this type folder has subfolders or is flat.
        with os.scandir(type_folder) as children:
            has_subfolders = any(
                not child.name.startswith("_") and child.is_dir() for child in children
            )
        if has_subfolders:
            for sub in type_folder.iterdir():
                if not sub.is_dir() or sub.name.startswith("_"):
                    continue
                inner[sub.name] = _count_markdown_pages(sub)
            # Also count top-level .md files (not under any subfolder)
            top_level = sum(
                1 for p in type_folder.glob("*.md") if p.name != "index.md"
            )
            if top_level:
                inner[""] = top_level
        else:
            inner[""] = _count_markdown_pages(type_folder)
        out[type_folder.name] = inner
    return out


def _refresh_notes_subindex_text(
    text: str,
    *,
    counts_by_type: dict[str, int],
    counts_by_subfolder: dict[str, dict[str, int]],
    owners: tuple[_NotesOwner, ...] | None = None,
) -> str:
    """Rewrite count numbers in `Notes/index.md` without touching descriptions.

    Counts are rewritten in two places:
    - H3 section headers `### Research — desc (43)` → updates `(43)` from `counts_by_type`.
    - Subfolder bullets `- [[link|Project Alpha]] (2) — desc` → updates `(2)` from `counts_by_subfolder`.

    Descriptions and section ordering are left untouched. A home without a section gets one
    at the end of `## By type`. Subfolders that appear on disk but aren't already represented
    as bullets stay un-added; audit's index_drift check surfaces them so you can add a description.
    """
    owners = _notes_owners(None) if owners is None else owners

    def _owner_titled(name: str) -> _NotesOwner | None:
        folded = name.strip().casefold()
        return next(
            (owner for owner in owners if folded in {title.casefold() for title in owner.titles}), None
        )

    def _h3(m: re.Match[str]) -> str:
        owner = _owner_titled(m.group("name"))
        actual = counts_by_type.get(owner.token) if owner is not None else None
        if actual is None:
            return m.group(0)
        prefix = m.group(1)  # `### Research`
        desc = m.group(3) or ""  # ` — description`
        return f"{prefix}{desc} ({actual})"

    def _bullet(m: re.Match[str]) -> str:
        type_folder = m.group("typef")
        sub = m.group("sub")
        rest = m.group("rest") or ""
        # `Research`-style: sub is the project folder (`Project Alpha`, `Work`).
        # For flat types where the bullet might not have a sub, we leave alone.
        per_type = counts_by_subfolder.get(type_folder, {})
        actual = per_type.get(sub)
        if actual is None:
            return m.group(0)
        return f"{m.group(1)} ({actual}){rest}"

    text = _NOTES_H3_COUNT.sub(_h3, text)
    text = _add_home_sections(text, owners, counts_by_type, _owner_titled)
    text = _notes_subfolder_bullet_re(kb_prefix()).sub(_bullet, text)
    return text


def _add_home_sections(
    text: str,
    owners: tuple[_NotesOwner, ...],
    counts_by_type: dict[str, int],
    owner_titled: Callable[[str], _NotesOwner | None],
) -> str:
    """Give each home that has no section one, at the end of `## By type`."""
    present = {match.group("name") for match in _NOTES_H3_COUNT.finditer(text)}
    start = text.find(NOTES_BY_TYPE_HEADER)
    if start == -1:
        return text
    sections: list[str] = []
    for owner in owners:
        if owner.description is None or any(owner_titled(name) is owner for name in present):
            continue
        count = counts_by_type.get(owner.token, 0)
        if not owner.active and count == 0:
            continue
        summary = " ".join(owner.description.split())
        for title in owner.titles:
            header = f"### {title}{f' — {summary}' if summary else ''} ({count})"
            # Only a header that reads back as this home's own keeps a later refresh idempotent.
            found = _NOTES_H3_COUNT.fullmatch(header)
            if found is not None and owner_titled(found.group("name")) is owner:
                sections.append(header)
                break
    if not sections:
        return text
    end = text.find("\n## ", start + len(NOTES_BY_TYPE_HEADER))
    end = len(text) if end == -1 else end
    head, tail = text[:end].rstrip("\n"), text[end:].lstrip("\n")
    joined = head + "\n\n" + "\n\n".join(sections) + "\n"
    return joined + ("\n" + tail if tail else "")


def _refresh_entities_subindex_text(
    text: str,
    *,
    counts_by_type: dict[str, int],
    registry: EntityTypeRegistry | None = None,
) -> str:
    """Refresh registered entity rows while preserving hand-authored prose."""
    registry = registry or core_registry()
    pattern = _entities_bullet_re(
        kb_prefix(),
        registry.core_version,
        registry.extension_hash,
        tuple(item.folder for item in registry.active_definitions),
    )

    def _bullet(m: re.Match[str]) -> str:
        folder = m.group("folder")
        definition = registry.by_folder.get(folder.casefold())
        if definition is None:
            return m.group(0)
        actual = counts_by_type.get(definition.id, 0)
        rest = m.group("rest") or ""
        return f"{m.group(1)} ({actual}){rest}"

    refreshed = pattern.sub(_bullet, text)
    present = {match.group("folder").casefold() for match in pattern.finditer(refreshed)}
    missing = [
        definition
        for definition in registry.active_definitions
        if definition.folder.casefold() not in present
    ]
    if not missing:
        return refreshed

    header = "## By type"
    section_start = refreshed.find(header)
    if section_start == -1:
        return refreshed
    section_end = refreshed.find("\n## ", section_start + len(header))
    if section_end == -1:
        section_end = len(refreshed)
    rows = [
        f"- [[{kb_prefix()}Entities/{definition.folder}/|{definition.folder}]] "
        f"({counts_by_type.get(definition.id, 0)})"
        for definition in missing
    ]
    prefix = refreshed[:section_end].rstrip()
    suffix = refreshed[section_end:].lstrip("\n")
    joined = f"{prefix}\n" + "\n".join(rows) + "\n"
    if suffix:
        joined += f"\n{suffix}"
    return joined


def compute_subindex_writes(
    vault_root: Path,
    *,
    top_index_text: str | None = None,
    base_writes: Mapping[Path, PlannedWrite] | None = None,
    pending_paths: list[str] | None = None,
    include_unchanged: bool = False,
    include_sources_index: bool = True,
    sources_counts: Mapping[str, int] | None = None,
) -> tuple[list[PlannedWrite], str | None]:
    """Build the planned writes for sub-folder + top-index count refreshes.

    Returns `(writes, top_index_new_text)`. If `top_index_text` is provided,
    it is used as the base for rewriting top-index counts (so the caller's
    in-flight changes to Recent activity don't get clobbered); the rewritten
    text is returned. `base_writes` similarly supplies already-planned text and
    its guard for sub-indexes. This lets a caller compose independent edits to
    an index into one guarded write instead of scheduling competing replacements.
    Disk reads still bind a guard for indexes with no base write. Otherwise the
    caller is responsible for re-reading the top index from disk later.

    `sources_counts` is a Sources census the caller already holds for the state
    before `pending_paths`; it replaces the walk, which a caller that commits many
    batches in a row would otherwise repeat for each.

    Set `include_sources_index=False` when another planned write supplies that
    index. The census and guarded read still run; only its rendering is omitted.

    `pending_paths` are vault-relative KB paths (with or without `.md`) of
    files the caller is about to write in the same atomic batch. They are
    virtually counted into the totals so the index reflects post-write state
    without re-scanning twice. Pass `[]` or omit for ops that don't add
    counted files (edit, preserve).

    Writes are only included for files that exist. Missing sub-indexes
    are silently skipped — not every machine maintains the full hierarchy.
    """
    kb = kb_root(vault_root)
    writes: list[PlannedWrite] = []
    planned_bases = {
        path.resolve(): write for path, write in (base_writes or {}).items()
    }

    sources_dir = kb / "Sources"
    notes_dir = kb / "Notes"
    entities_dir = kb / "Entities"
    entity_registry = load_entity_types(vault_root)

    if sources_counts is None:
        with call_spans.span("indexes.source_census"):
            sources_counts = _count_sources(sources_dir)
    else:
        sources_counts = dict(sources_counts)
    home_set = homes.load(vault_root)
    notes_owners = _notes_owners(home_set)
    notes_counts = _count_notes(notes_dir, home_set=home_set)
    entities_counts = _count_entities(entities_dir, registry=entity_registry)
    notes_by_subfolder = _count_notes_by_subfolder(notes_dir)

    # Virtually count each pending path so the index shows post-write state.
    for raw_path in pending_paths or []:
        rel = raw_path.removesuffix(".md").replace("\\", "/")
        rel = rel.removeprefix(kb_prefix()).lstrip("/")
        parts = rel.split("/")
        if len(parts) < 2:
            continue
        head = parts[0]
        if head == "Sources" and len(parts) >= 3:
            source_folder = parts[1]
            if not source_folder.startswith("_"):
                sources_counts[source_folder] = sources_counts.get(source_folder, 0) + 1
        elif head == "Notes" and len(parts) >= 3:
            type_folder = parts[1]
            owner = _owner_of(notes_owners, tuple(parts[1:-1]))
            if owner is not None:
                notes_counts[owner.token] = notes_counts.get(owner.token, 0) + 1
                enclosing = _owner_of(
                    tuple(o for o in notes_owners if o is not owner), tuple(owner.parts[:-1])
                )
                if enclosing is not None and enclosing.token in notes_counts:
                    notes_counts[enclosing.token] -= 1
            # A folder with sub-folders counts a page under its own; a flat one under "".
            inner = notes_by_subfolder.setdefault(type_folder, {})
            sub = parts[2] if len(parts) >= 4 else ""
            inner[sub] = inner.get(sub, 0) + 1
        elif head == "Entities" and len(parts) >= 3:
            ent_folder = parts[1]
            definition = entity_registry.by_folder.get(ent_folder.casefold())
            if definition is not None:
                entities_counts[definition.id] = entities_counts.get(definition.id, 0) + 1

    # Top index counts refresh (Sources + Notes + Entities rows).
    new_top_text: str | None = None
    if top_index_text is not None:
        new_top_text = _rewritten_in_place(
            top_index_text,
            lambda logical: _rewrite_top_index_notes_and_entities_counts(
                _rewrite_sources_count(logical, counts=sources_counts),
                notes_counts=notes_counts,
                entities_counts=entities_counts,
            ),
        )
        new_top_text = render_wikilinks_for_vault(new_top_text, vault_root)

    # Sources/index.md refresh.
    sources_index = sources_index_path(vault_root)
    if sources_index.exists():
        try:
            current, guard = read_guarded_text(vault_root, sources_index)
        except OSError:
            current = None
        if current is not None and include_sources_index:
            base_write = planned_bases.get(sources_index.resolve())
            base = base_write.content if base_write is not None else current
            # Keep guarded reading outside the rendering cost: callers may
            # discard this text while still depending on its validation.
            with call_spans.span("indexes.sources_render"):
                new = _rewritten_in_place(
                    base,
                    lambda logical: _replace_by_type_section(
                        logical,
                        folder_title="Articles",
                        folder_description=_LEGACY_FOLDER_DESCRIPTIONS["Articles"],
                        counts=sources_counts,
                        vault_root=vault_root,
                    ),
                )
                new = render_wikilinks_for_vault(new, vault_root)
            if include_unchanged or new != base:
                writes.append(PlannedWrite(
                    path=sources_index,
                    content=new,
                    guard=base_write.guard if base_write and base_write.guard else guard,
                ))

    # Notes/index.md refresh.
    notes_index = notes_dir / "index.md"
    if notes_index.exists():
        try:
            current, guard = read_guarded_text(vault_root, notes_index)
        except OSError:
            current = None
        if current is not None:
            base_write = planned_bases.get(notes_index.resolve())
            base = base_write.content if base_write is not None else current
            new = _rewritten_in_place(
                base,
                lambda logical: _refresh_notes_subindex_text(
                    logical,
                    counts_by_type=notes_counts,
                    counts_by_subfolder=notes_by_subfolder,
                    owners=notes_owners,
                ),
            )
            new = render_wikilinks_for_vault(new, vault_root)
            if include_unchanged or new != base:
                writes.append(PlannedWrite(
                    path=notes_index,
                    content=new,
                    guard=base_write.guard if base_write and base_write.guard else guard,
                ))

    # Entities/index.md refresh.
    entities_index = entities_dir / "index.md"
    if entities_index.exists():
        try:
            current, guard = read_guarded_text(vault_root, entities_index)
        except OSError:
            current = None
        if current is not None:
            base_write = planned_bases.get(entities_index.resolve())
            base = base_write.content if base_write is not None else current
            new = _rewritten_in_place(
                base,
                lambda logical: _refresh_entities_subindex_text(
                    logical,
                    counts_by_type=entities_counts,
                    registry=entity_registry,
                ),
            )
            new = render_wikilinks_for_vault(new, vault_root)
            if include_unchanged or new != base:
                writes.append(PlannedWrite(
                    path=entities_index,
                    content=new,
                    guard=base_write.guard if base_write and base_write.guard else guard,
                ))

    return writes, new_top_text


def _update_log(
    text: str,
    *,
    date_iso: str,
    rel_source_path: str,
    log_entry_body: str,
) -> str:
    """Prepend `## [<date>] add | <path>` entry right after the `---` separator."""
    title = rel_source_path.replace(kb_prefix(), "", 1)
    new_entry = f"## [{date_iso}] add | {title}\n\n{escape_wikilinks_for_log(log_entry_body)}\n"

    sep_idx = text.find(LOG_SEPARATOR)
    if sep_idx == -1:
        # No separator — append at end.
        return text.rstrip() + "\n\n" + new_entry + "\n"

    insertion_point = sep_idx + len(LOG_SEPARATOR)
    return text[:insertion_point] + "\n" + new_entry + "\n" + text[insertion_point:]


def _replace_section(
    text: str, header: str, new_block: str, *, next_h2_or_end: bool
) -> str:
    """Replace the section starting at `header` with `new_block`.

    Section ends at the next `## ` heading or end-of-file.
    """
    start = text.find(header)
    if start == -1:
        # Section missing — append before any trailing `## Counts` or just at end.
        return text.rstrip() + "\n\n" + new_block
    if next_h2_or_end:
        next_h2 = text.find("\n## ", start + len(header))
        end = next_h2 + 1 if next_h2 != -1 else len(text)
    else:
        end = len(text)
    return text[:start] + new_block.rstrip() + "\n\n" + text[end:].lstrip("\n")
