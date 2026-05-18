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
from pathlib import Path

from .vault import kb_root


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
        sources_index_content=sources_index_new,
        top_index_content=top_index_new,
        log_content=log_new,
        trim_note=trim_note,
    )


def _count_sources(sources_dir: Path) -> dict[str, int]:
    """Per-folder source count, excluding index.md and _attachments/."""
    out: dict[str, int] = {}
    if not sources_dir.is_dir():
        return out
    for sub in sources_dir.iterdir():
        if not sub.is_dir() or sub.name.startswith("_"):
            continue
        out[sub.name] = sum(
            1 for f in sub.iterdir() if f.is_file() and f.suffix == ".md" and f.name != "index.md"
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
    }
    if folder_title not in known_descriptions:
        known_descriptions[folder_title] = folder_description

    for name in sorted(counts.keys()):
        desc = known_descriptions.get(name, "captured material")
        rows.append(f"- [[Knowledge Base/Sources/{name}/|{name}]] — {desc} ({counts[name]})")

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


def _update_log(
    text: str,
    *,
    date_iso: str,
    rel_source_path: str,
    log_entry_body: str,
) -> str:
    """Prepend `## [<date>] add | <path>` entry right after the `---` separator."""
    title = rel_source_path.replace("Knowledge Base/", "", 1)
    new_entry = f"## [{date_iso}] add | {title}\n\n{log_entry_body}\n"

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
