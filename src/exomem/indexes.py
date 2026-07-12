"""Index/log update routines for SKILL.md rule 7 enforcement.

`add` calls into here to compute the new contents of:
- `Sources/index.md` (bump By-type count + prepend Recent-captures bullet)
- `Knowledge Base/index.md` (prepend Recent-activity cap-50 + bump Counts line)
- `log.md` (prepend most-recent-first entry)

Each function returns a PlannedWrite-ready string; nothing is written here.
The caller batches them with the source file into a single atomic write.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from .kbdir import kb_prefix
from .vault import (
    PlannedWrite,
    escape_wikilinks_for_log,
    kb_root,
    render_wikilinks_for_vault,
)

RECENT_ACTIVITY_CAP = 50

SOURCES_BY_TYPE_HEADER = "## By type"
SOURCES_RECENT_HEADER = "## Recent captures"
INDEX_RECENT_HEADER = "## Recent activity"
INDEX_COUNTS_HEADER = "## Counts"
LOG_SEPARATOR = "\n---\n"


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
) -> IndexUpdate:
    """Build the new contents of Sources/index.md, top-level index.md, and log.md.

    `rel_source_path` should be the vault-relative path WITHOUT `.md` (wikilink form).
    `activity_summary` is the one-liner that appears in the top index's Recent
    activity bullet AND in the log entry's body.
    """
    kb = kb_root(vault_root)
    sources_dir = kb / "Sources"
    sources_index = sources_dir / "index.md"
    top_index = kb / "index.md"
    log_file = kb / "log.md"

    if not sources_index.exists():
        raise FileNotFoundError(f"Sources/index.md missing: {sources_index}")
    if not top_index.exists():
        raise FileNotFoundError(f"top index.md missing: {top_index}")
    if not log_file.exists():
        raise FileNotFoundError(f"log.md missing: {log_file}")

    counts = _count_sources(sources_dir)

    sources_index_new = _update_sources_index(
        sources_index.read_text(encoding="utf-8"),
        folder_title=folder_title,
        folder_description=folder_description,
        counts=counts,
        date_iso=date_iso,
        rel_source_path=rel_source_path,
    )

    top_index_new, trim_note = _update_top_index(
        top_index.read_text(encoding="utf-8"),
        counts=counts,
        date_iso=date_iso,
        activity_summary=activity_summary,
    )

    log_new = _update_log(
        log_file.read_text(encoding="utf-8"),
        date_iso=date_iso,
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


def _count_sources(sources_dir: Path) -> dict[str, int]:
    """Per top-level source-type count, including themed nested folders."""
    out: dict[str, int] = {}
    if not sources_dir.is_dir():
        return out
    for sub in sources_dir.iterdir():
        if not sub.is_dir() or sub.name.startswith("_"):
            continue
        out[sub.name] = sum(
            1
            for f in sub.rglob("*.md")
            if f.name != "index.md"
            and not any(part.startswith("_") for part in f.relative_to(sub).parts[:-1])
        )
    return out


# Map Notes/<folder> → page-type key used in the Counts section
# (e.g. "Research" → "research", "Productions" → "production-log").
_NOTES_FOLDER_TO_TYPE: dict[str, str] = {
    "Research": "research",
    "Insights": "insight",
    "Failures": "failure",
    "Patterns": "pattern",
    "Experiments": "experiment",
    "Productions": "production-log",
}


def _count_notes(notes_dir: Path) -> dict[str, int]:
    """Per-type compiled-note count, recursing into project/domain/medium subfolders.

    Returns a dict keyed by the page-type token used in `index.md` Counts
    (e.g. "research", "insight", "production-log"). Excludes index.md files
    and any folder starting with "_".
    """
    out: dict[str, int] = {}
    if not notes_dir.is_dir():
        return out
    for sub in notes_dir.iterdir():
        if not sub.is_dir() or sub.name.startswith("_"):
            continue
        key = _NOTES_FOLDER_TO_TYPE.get(sub.name)
        if key is None:
            continue  # unknown top-level folder under Notes/; ignore
        count = 0
        for path in sub.rglob("*.md"):
            if path.name == "index.md":
                continue
            count += 1
        out[key] = count
    return out


def _update_sources_index(
    text: str,
    *,
    folder_title: str,
    folder_description: str,
    counts: dict[str, int],
    date_iso: str,
    rel_source_path: str,
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
    )
    text = _prepend_recent_capture(text, date_iso=date_iso, rel_source_path=rel_source_path)
    return text


def _replace_by_type_section(
    text: str,
    *,
    folder_title: str,
    folder_description: str,
    counts: dict[str, int],
) -> str:
    """Rewrite the entire By-type list from disk counts so we don't drift."""
    rows: list[str] = []
    known_descriptions = {
        "Articles": "captured web/PDF content",
        "Sessions": "pasted Claude/conversation transcripts",
        "Books": "book notes/excerpts",
        "Papers": "academic papers",
        "Videos": "captured video transcripts/notes",
        "Other": "miscellaneous captures",
        "Imported": "copied legacy-vault material with provenance",
    }
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
    r"^(- Notes \()([a-z-]+)(\): )(\d+)\s*$", re.MULTILINE
)
_ENTITIES_COUNT_LINE = re.compile(
    r"^(- Entities \()([a-z-]+)(\): )(\d+)\s*$", re.MULTILINE
)
_NOTES_TOTAL_LINE = re.compile(r"^- Notes:\s*\d+\s*$", re.MULTILINE)
_ENTITIES_TOTAL_LINE = re.compile(r"^- Entities:\s*\d+\s*$", re.MULTILINE)


# Map Entities/<folder> → entity_type key used in the Counts section.
_ENTITIES_FOLDER_TO_TYPE: dict[str, str] = {
    "People": "person",
    "Concepts": "concept",
    "Libraries": "library",
    "Decisions": "decision",
}


def _count_entities(entities_dir: Path) -> dict[str, int]:
    """Per-entity-type count, mirrors `_count_sources` shape.

    Returns a dict keyed by entity_type token (person, concept, library,
    decision). Excludes index.md and `_*` folders.
    """
    out: dict[str, int] = {}
    if not entities_dir.is_dir():
        return out
    for sub in entities_dir.iterdir():
        if not sub.is_dir() or sub.name.startswith("_"):
            continue
        key = _ENTITIES_FOLDER_TO_TYPE.get(sub.name)
        if key is None:
            continue
        out[key] = sum(
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


# Per page-type subfolders that show up in Notes/index.md and we keep
# count-current. Order matches the existing real-vault index layout.
_NOTES_SUBINDEX_TYPES: tuple[tuple[str, str], ...] = (
    # (page-type token, folder name on disk)
    ("research", "Research"),
    ("insight", "Insights"),
    ("failure", "Failures"),
    ("pattern", "Patterns"),
    ("experiment", "Experiments"),
    ("production-log", "Productions"),
)


# H3 header `### Research — project-or-domain-scoped synthesis (43)` — capture
# the type name (group 1), the description (group 2), and the count (group 3).
_NOTES_H3_COUNT = re.compile(
    r"^(### (?P<name>[A-Z][\w\- ]*?))( — [^\n]*?)? \((?P<count>\d+)\)\s*$",
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
def _entities_bullet_re(kb_prefix_str: str) -> re.Pattern[str]:
    return re.compile(
        r"^(- \[\[(?:" + re.escape(kb_prefix_str) + r")?"
        + r"Entities/(?P<folder>People|Concepts|Libraries|Decisions)/?(?:\|[^\]]+)?\]\])"
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
        has_subfolders = any(
            child.is_dir() and not child.name.startswith("_")
            for child in type_folder.iterdir()
        )
        if has_subfolders:
            for sub in type_folder.iterdir():
                if not sub.is_dir() or sub.name.startswith("_"):
                    continue
                count = sum(
                    1 for p in sub.rglob("*.md") if p.name != "index.md"
                )
                inner[sub.name] = count
            # Also count top-level .md files (not under any subfolder)
            top_level = sum(
                1 for p in type_folder.glob("*.md") if p.name != "index.md"
            )
            if top_level:
                inner[""] = top_level
        else:
            count = sum(
                1 for p in type_folder.rglob("*.md") if p.name != "index.md"
            )
            inner[""] = count
        out[type_folder.name] = inner
    return out


def _refresh_notes_subindex_text(
    text: str,
    *,
    counts_by_type: dict[str, int],
    counts_by_subfolder: dict[str, dict[str, int]],
) -> str:
    """Rewrite count numbers in `Notes/index.md` without touching descriptions.

    Counts are rewritten in two places:
    - H3 type headers `### Research — desc (43)` → updates `(43)` from `counts_by_type`.
    - Subfolder bullets `- [[link|Project Alpha]] (2) — desc` → updates `(2)` from `counts_by_subfolder`.

    Descriptions and section ordering are left untouched. Subfolders that
    appear on disk but aren't already represented as bullets stay un-added;
    audit's index_drift check surfaces them so you can add a description.
    """
    folder_to_type = {f: t for t, f in _NOTES_SUBINDEX_TYPES}

    def _h3(m: re.Match[str]) -> str:
        name = m.group("name").strip()
        type_key = folder_to_type.get(name)
        if type_key is None:
            return m.group(0)
        actual = counts_by_type.get(type_key)
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
    text = _notes_subfolder_bullet_re(kb_prefix()).sub(_bullet, text)
    return text


def _refresh_entities_subindex_text(
    text: str, *, counts_by_type: dict[str, int]
) -> str:
    """Rewrite `- [[Knowledge Base/Entities/<Folder>/...]] (N)` in Entities/index.md."""
    folder_to_type = {
        "People": "person",
        "Concepts": "concept",
        "Libraries": "library",
        "Decisions": "decision",
    }

    def _bullet(m: re.Match[str]) -> str:
        folder = m.group("folder")
        type_key = folder_to_type.get(folder)
        if type_key is None:
            return m.group(0)
        actual = counts_by_type.get(type_key)
        if actual is None:
            return m.group(0)
        rest = m.group("rest") or ""
        return f"{m.group(1)} ({actual}){rest}"

    return _entities_bullet_re(kb_prefix()).sub(_bullet, text)


def compute_subindex_writes(
    vault_root: Path,
    *,
    top_index_text: str | None = None,
    pending_paths: list[str] | None = None,
) -> tuple[list[PlannedWrite], str | None]:
    """Build the planned writes for sub-folder + top-index count refreshes.

    Returns `(writes, top_index_new_text)`. If `top_index_text` is provided,
    it is used as the base for rewriting top-index counts (so the caller's
    in-flight changes to Recent activity don't get clobbered); the rewritten
    text is returned. Otherwise the caller is responsible for re-reading the
    top index from disk later.

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

    sources_dir = kb / "Sources"
    notes_dir = kb / "Notes"
    entities_dir = kb / "Entities"

    sources_counts = _count_sources(sources_dir)
    notes_counts = _count_notes(notes_dir)
    entities_counts = _count_entities(entities_dir)
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
            type_key = {
                "Research": "research",
                "Insights": "insight",
                "Failures": "failure",
                "Patterns": "pattern",
                "Experiments": "experiment",
                "Productions": "production-log",
            }.get(type_folder)
            if type_key:
                notes_counts[type_key] = notes_counts.get(type_key, 0) + 1
            # For nested types, also bump the per-subfolder count.
            if type_folder in ("Research", "Experiments", "Productions") and len(parts) >= 4:
                sub = parts[2]
                inner = notes_by_subfolder.setdefault(type_folder, {})
                inner[sub] = inner.get(sub, 0) + 1
            elif type_folder in ("Insights", "Failures", "Patterns"):
                inner = notes_by_subfolder.setdefault(type_folder, {})
                inner[""] = inner.get("", 0) + 1
        elif head == "Entities" and len(parts) >= 3:
            ent_folder = parts[1]
            ent_key = _ENTITIES_FOLDER_TO_TYPE.get(ent_folder)
            if ent_key:
                entities_counts[ent_key] = entities_counts.get(ent_key, 0) + 1

    # Top index counts refresh (Sources + Notes + Entities rows).
    new_top_text: str | None = None
    if top_index_text is not None:
        top_index_text = _rewrite_sources_count(top_index_text, counts=sources_counts)
        new_top_text = _rewrite_top_index_notes_and_entities_counts(
            top_index_text,
            notes_counts=notes_counts,
            entities_counts=entities_counts,
        )
        new_top_text = render_wikilinks_for_vault(new_top_text, vault_root)

    # Sources/index.md refresh.
    sources_index = sources_dir / "index.md"
    if sources_index.exists():
        try:
            current = sources_index.read_text(encoding="utf-8")
        except OSError:
            current = None
        if current is not None:
            new = _replace_by_type_section(
                current,
                folder_title="Articles",
                folder_description="captured web/PDF content",
                counts=sources_counts,
            )
            new = render_wikilinks_for_vault(new, vault_root)
            if new != current:
                writes.append(PlannedWrite(path=sources_index, content=new))

    # Notes/index.md refresh.
    notes_index = notes_dir / "index.md"
    if notes_index.exists():
        try:
            current = notes_index.read_text(encoding="utf-8")
        except OSError:
            current = None
        if current is not None:
            new = _refresh_notes_subindex_text(
                current,
                counts_by_type=notes_counts,
                counts_by_subfolder=notes_by_subfolder,
            )
            new = render_wikilinks_for_vault(new, vault_root)
            if new != current:
                writes.append(PlannedWrite(path=notes_index, content=new))

    # Entities/index.md refresh.
    entities_index = entities_dir / "index.md"
    if entities_index.exists():
        try:
            current = entities_index.read_text(encoding="utf-8")
        except OSError:
            current = None
        if current is not None:
            new = _refresh_entities_subindex_text(
                current, counts_by_type=entities_counts
            )
            new = render_wikilinks_for_vault(new, vault_root)
            if new != current:
                writes.append(PlannedWrite(path=entities_index, content=new))

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
