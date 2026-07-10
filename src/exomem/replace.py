"""The `replace` MCP tool: supersession of an existing page.

Per SKILL.md rule 6, supersession is metadata-only:
- The old page gets `status: superseded`, `superseded_by: "[[<new>]]"`, and a
  fresh `updated:` date.
- The new page is written with `supersedes: "[[<old>]]"` in its frontmatter.
- Inbound wikilinks STAY pointing at the old page — readers follow the chain.

Sources and Evidence are append-only (rule 2) and rejected with
INVALID_REPLACE. No type allowlist beyond that: any frontmatter-bearing
page outside append-only trees is supersedable. The KB taxonomy grows
over time and gating supersession on a closed type set creates needless
friction.

The new page is constructed via the existing `note.note()` machinery so it
gets full back-ref + index/log treatment for free.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from . import find as find_module
from . import indexes
from . import note as note_module
from . import memory_refs
from .kbdir import kb_prefix
from .vault import PlannedWrite, batch_atomic_write, kb_root


log = logging.getLogger(__name__)


@dataclass
class ReplaceResult:
    old_path: str   # vault-relative, with .md
    new_path: str   # vault-relative, with .md
    warnings: list[str]
    old_ref: str | None = None
    new_ref: str | None = None

    def as_dict(self) -> dict:
        return {
            "old_path": self.old_path,
            "new_path": self.new_path,
            "warnings": self.warnings,
            "old_ref": self.old_ref,
            "new_ref": self.new_ref,
        }


@dataclass
class ReplaceError(Exception):
    code: str
    missing: list[str]
    reason: str

    def as_dict(self) -> dict:
        return {"code": self.code, "missing": self.missing, "reason": self.reason}


def replace(
    vault_root: Path,
    *,
    old_path: str,
    reason: str | None = None,
    today: dt.date | None = None,
    **note_kwargs,
) -> ReplaceResult:
    """Supersede `old_path` with a new page built from `note_kwargs`.

    `note_kwargs` are passed through to `note.note()` — same args, same
    validation, same writes (back-refs, index, log). On top of that, this
    function:
    - Refuses if old_path is in Sources/ or Evidence/ (append-only).
    - Refuses if old_path is already superseded.
    - Patches the new page's frontmatter to include `supersedes:`.
    - Patches the old page's frontmatter to flip status + add `superseded_by:`.
    """
    today = today or dt.date.today()
    date_iso = today.isoformat()

    # Resolve + validate old_path.
    old_resolved, rel_old_with_ext = _resolve_kb_path(vault_root, old_path)
    rel_old_no_ext = rel_old_with_ext.removesuffix(".md")

    if "/Sources/" in "/" + rel_old_with_ext or "/Evidence/" in "/" + rel_old_with_ext:
        raise ReplaceError(
            code="INVALID_REPLACE",
            missing=["old_path"],
            reason=(
                f"{rel_old_with_ext} is in Sources/ or Evidence/, which are "
                "append-only (SKILL.md rule 2). Supersession only applies to "
                "compiled material."
            ),
        )

    # Load old page, validate it's actually supersedable.
    try:
        mtime = old_resolved.stat().st_mtime
    except OSError as e:
        raise ReplaceError(
            code="OLD_NOT_FOUND",
            missing=["old_path"],
            reason=str(e),
        ) from e
    old_parsed = find_module._parse_page(old_resolved, mtime, vault_root)
    if old_parsed is None:
        raise ReplaceError(
            code="UNREADABLE",
            missing=["old_path"],
            reason=f"could not parse {rel_old_with_ext} as markdown",
        )

    if old_parsed.frontmatter.get("status") == "superseded":
        raise ReplaceError(
            code="ALREADY_SUPERSEDED",
            missing=["old_path"],
            reason=(
                f"{rel_old_with_ext} is already marked status: superseded. "
                "Supersede the page that already supersedes it, or start fresh."
            ),
        )

    # Construct the new page via note.note() — full reuse, including
    # index/log/back-ref writes.
    new_result = note_module.note(vault_root, today=today, **note_kwargs)
    new_path_str = new_result.path  # vault-relative, with .md
    rel_new_no_ext = new_path_str.removesuffix(".md")
    if not rel_new_no_ext.startswith(kb_prefix()):
        rel_new_no_ext = kb_prefix() + rel_new_no_ext
    new_resolved = vault_root / new_path_str

    warnings: list[str] = list(new_result.warnings)

    # Inject `supersedes:` into the freshly-written new page's frontmatter.
    new_text = new_resolved.read_text(encoding="utf-8")
    new_text_updated = _inject_supersedes(new_text, rel_old_no_ext)
    if new_text_updated == new_text:
        warnings.append(
            "could not inject supersedes: into new page frontmatter — "
            "frontmatter shape unexpected"
        )

    # Patch old page: status -> superseded, add superseded_by, refresh updated.
    old_text = old_resolved.read_text(encoding="utf-8")
    old_text_updated = _mark_superseded(old_text, rel_new_no_ext, date_iso)
    if old_text_updated == old_text:
        warnings.append(
            "could not patch old page frontmatter (status/superseded_by/updated) — "
            "manual fixup needed"
        )

    # Append a log entry naming the supersession explicitly.
    kb = kb_root(vault_root)
    log_file = kb / "log.md"
    log_writes: list[PlannedWrite] = []
    if log_file.exists():
        log_body_parts = [
            f"Supersedes `{rel_old_no_ext}` via exomem."
        ]
        if reason and reason.strip():
            log_body_parts.append(reason.strip())
        log_body = " ".join(log_body_parts)
        new_log = _prepend_replace_log_entry(
            log_file.read_text(encoding="utf-8"),
            date_iso=date_iso,
            rel_new_no_ext=rel_new_no_ext,
            body=log_body,
        )
        log_writes.append(PlannedWrite(path=log_file, content=new_log))
    else:
        warnings.append(f"{kb_prefix()}log.md missing; skipped replace log entry")

    writes = [
        PlannedWrite(path=new_resolved, content=new_text_updated),
        PlannedWrite(path=old_resolved, content=old_text_updated),
    ] + log_writes

    try:
        batch_atomic_write(writes, vault_root=vault_root)
    except Exception as e:
        log.exception("partial write during replace(); some files may be updated")
        warnings.append(f"partial write — reconcile on desktop: {e}")
        raise

    return ReplaceResult(
        old_path=rel_old_with_ext,
        new_path=new_path_str,
        warnings=warnings,
        old_ref=memory_refs.ReferenceIndex(vault_root).ref_for_path(rel_old_with_ext),
        new_ref=new_result.ref,
    )


# ---------------- path resolution ----------------


def _resolve_kb_path(vault_root: Path, path: str) -> tuple[Path, str]:
    """Resolve a KB-relative path; return (absolute, normalized-relative-with-.md)."""
    if not path or not path.strip():
        raise ReplaceError(
            code="INVALID_PATH",
            missing=["old_path"],
            reason="old_path is empty",
        )
    rel = path.strip().replace("\\", "/").lstrip("/")
    if not rel.startswith(kb_prefix()):
        rel = kb_prefix() + rel
    if not rel.endswith(".md"):
        rel = rel + ".md"
    candidate = vault_root / rel
    try:
        resolved = candidate.resolve()
        resolved.relative_to(kb_root(vault_root).resolve())
    except (ValueError, OSError) as e:
        raise ReplaceError(
            code="INVALID_PATH",
            missing=["old_path"],
            reason=f"path escapes {kb_prefix()}: {e}",
        ) from None
    if not candidate.exists():
        raise ReplaceError(
            code="OLD_NOT_FOUND",
            missing=["old_path"],
            reason=f"file does not exist: {rel}",
        )
    return candidate, rel


# ---------------- frontmatter surgery ----------------


# Match "---\n<frontmatter>\n---\n<body>" exactly as find.py does.
_FM_PATTERN = re.compile(r"^---\n(.*?)\n---\n(.*)", re.DOTALL)


def _inject_supersedes(text: str, rel_old_no_ext: str) -> str:
    """Add `supersedes: "[[<old>]]"` to the new page's frontmatter.

    Inserts just before the closing `---`. If supersedes: already exists,
    leaves text unchanged.
    """
    m = _FM_PATTERN.match(text)
    if not m:
        return text
    fm_text = m.group(1)
    body = m.group(2)
    if re.search(r"^supersedes:", fm_text, re.MULTILINE):
        return text  # already present; idempotent
    wikilink = f'"[[{rel_old_no_ext}]]"'
    new_fm = fm_text.rstrip() + f"\nsupersedes: {wikilink}"
    return f"---\n{new_fm}\n---\n{body}"


def _mark_superseded(text: str, rel_new_no_ext: str, date_iso: str) -> str:
    """Patch an old page's frontmatter: status=superseded, +superseded_by, refresh updated."""
    m = _FM_PATTERN.match(text)
    if not m:
        return text
    fm_text = m.group(1)
    body = m.group(2)
    new_link = f'"[[{rel_new_no_ext}]]"'

    # status
    if re.search(r"^status:", fm_text, re.MULTILINE):
        fm_text = re.sub(
            r"^status:.*$",
            "status: superseded",
            fm_text,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        fm_text = fm_text.rstrip() + "\nstatus: superseded"

    # updated
    if re.search(r"^updated:", fm_text, re.MULTILINE):
        fm_text = re.sub(
            r"^updated:.*$",
            f"updated: {date_iso}",
            fm_text,
            count=1,
            flags=re.MULTILINE,
        )
    else:
        fm_text = fm_text.rstrip() + f"\nupdated: {date_iso}"

    # superseded_by — handle three shapes: missing, flow (`[]` or `[a,b]`),
    # block (multi-line `- ...`). Mirror note._append_to_ingested_into.
    if re.search(r"^superseded_by:", fm_text, re.MULTILINE):
        fm_text = _append_to_yaml_list(fm_text, "superseded_by", new_link)
    else:
        fm_text = fm_text.rstrip() + f"\nsuperseded_by:\n  - {new_link}"

    return f"---\n{fm_text}\n---\n{body}"


def _append_to_yaml_list(fm_text: str, key: str, new_quoted_value: str) -> str:
    """Append `new_quoted_value` (already wrapped in quotes if needed) to the
    `<key>:` list in a frontmatter block. Handles flow and block forms.

    `new_quoted_value` should be the YAML-ready string e.g. `"[[foo]]"` —
    the quotes are part of the value.
    """
    flow_pattern = re.compile(
        rf"^({re.escape(key)}:\s*)(\[\s*\]|\[[^\]\n]*\])\s*$", re.MULTILINE
    )
    block_header_pattern = re.compile(
        rf"^({re.escape(key)}:)\s*$", re.MULTILINE
    )
    flow_match = flow_pattern.search(fm_text)
    if flow_match:
        prefix, current = flow_match.group(1), flow_match.group(2).strip()
        inner = current.strip("[]").strip()
        items: list[str]
        if not inner:
            items = []
        else:
            items = [s.strip() for s in inner.split(",")]
        items.append(new_quoted_value)
        block_lines = [prefix.rstrip().rstrip(":") + ":"] + [
            f"  - {item}" for item in items
        ]
        replacement = "\n".join(block_lines)
        return (
            fm_text[: flow_match.start()] + replacement + fm_text[flow_match.end():]
        )

    block_match = block_header_pattern.search(fm_text)
    if block_match:
        body_start = block_match.end()
        cursor = body_start
        while cursor < len(fm_text):
            line_end = fm_text.find("\n", cursor + 1)
            if line_end == -1:
                line_end = len(fm_text)
            line = fm_text[cursor + 1: line_end] if fm_text[cursor] == "\n" else fm_text[cursor:line_end]
            if line.lstrip().startswith("- "):
                cursor = line_end
            else:
                break
        return fm_text[:cursor] + f"\n  - {new_quoted_value}" + fm_text[cursor:]

    # Should not reach here — caller checks for the key first.
    return fm_text


def _prepend_replace_log_entry(
    text: str, *, date_iso: str, rel_new_no_ext: str, body: str
) -> str:
    title = rel_new_no_ext.replace(kb_prefix(), "", 1)
    new_entry = f"## [{date_iso}] replace | {title}\n\n{body}\n"
    sep_idx = text.find(indexes.LOG_SEPARATOR)
    if sep_idx == -1:
        return text.rstrip() + "\n\n" + new_entry + "\n"
    insertion_point = sep_idx + len(indexes.LOG_SEPARATOR)
    return text[:insertion_point] + "\n" + new_entry + "\n" + text[insertion_point:]
