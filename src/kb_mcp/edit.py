"""The `edit` MCP tool: lightweight in-place edit of a page.

For tweaks — typo fixes, sentence additions, tag corrections — without
going through full supersession via `replace`. Heavy rewrites still
belong in `replace`; this is for the trivial cases where creating a
whole superseded-link chain would be silly.

What it touches:
- The page body (if `new_body` provided)
- The `tags:` frontmatter field (if `tags` provided)
- The `updated:` frontmatter field (always — bumped to today)

What it leaves alone:
- All other frontmatter fields (type, project, status, sources, etc.).
  If you need to change those, that's a `replace`.

Refuses:
- Sources/ and Evidence/ paths (rule 2: append-only).
- Pages without frontmatter (won't synthesize a frontmatter block).
- Pages already marked `status: superseded` (don't edit history; supersede
  the new page instead).

No type allowlist: any page outside Sources/Evidence with frontmatter is
editable. The KB taxonomy grows over time (`identity`, future types) and
gating editability on a closed type set creates needless friction.
Append-only paths and supersession status are the real safety.

Every edit appends a log entry naming `why` — so changes remain auditable
even though they didn't create a new file.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from . import find as find_module
from . import indexes
from .vault import PlannedWrite, batch_atomic_write, kb_root


log = logging.getLogger(__name__)


@dataclass
class EditResult:
    path: str  # vault-relative, with .md
    warnings: list[str]

    def as_dict(self) -> dict:
        return {"path": self.path, "warnings": self.warnings}


@dataclass
class EditError(Exception):
    code: str
    missing: list[str]
    reason: str

    def as_dict(self) -> dict:
        return {"code": self.code, "missing": self.missing, "reason": self.reason}


def edit(
    vault_root: Path,
    *,
    path: str,
    why: str,
    new_body: str | None = None,
    tags: list[str] | None = None,
    today: dt.date | None = None,
) -> EditResult:
    """Edit body and/or tags of a compiled page in place. Bumps `updated:`.

    `why` is required — it lands in the log entry so the change is auditable.
    """
    missing: list[str] = []
    reasons: list[str] = []

    if new_body is None and tags is None:
        missing.append("new_body or tags")
        reasons.append(
            "must supply at least one of `new_body` or `tags`; otherwise "
            "there's nothing to edit"
        )
    if not why or not why.strip():
        missing.append("why")
        reasons.append("why is required — edits without rationale aren't auditable")

    if missing:
        raise EditError(
            code="INVALID_EDIT", missing=missing, reason="; ".join(reasons)
        )

    today = today or dt.date.today()
    date_iso = today.isoformat()

    abs_path, rel_path = _resolve(vault_root, path)

    if "/Sources/" in "/" + rel_path or "/Evidence/" in "/" + rel_path:
        raise EditError(
            code="INVALID_EDIT",
            missing=["path"],
            reason=(
                f"{rel_path} is in Sources/ or Evidence/, which are append-only "
                "(SKILL.md rule 2). Add a corrective source instead, or compile "
                "a downstream note that supersedes the framing."
            ),
        )

    try:
        mtime = abs_path.stat().st_mtime
    except OSError as e:
        raise EditError(code="NOT_FOUND", missing=["path"], reason=str(e)) from e

    parsed = find_module._parse_page(abs_path, mtime, vault_root)
    if parsed is None:
        raise EditError(
            code="UNREADABLE",
            missing=["path"],
            reason=f"could not parse {rel_path} as markdown",
        )

    if parsed.frontmatter.get("status") == "superseded":
        raise EditError(
            code="ALREADY_SUPERSEDED",
            missing=["path"],
            reason=(
                f"{rel_path} is marked status: superseded. Don't edit history — "
                "supersede the new (active) page instead."
            ),
        )

    original_text = abs_path.read_text(encoding="utf-8")
    fm_match = _FM_PATTERN.match(original_text)
    if not fm_match:
        raise EditError(
            code="UNREADABLE",
            missing=["path"],
            reason=(
                f"{rel_path} has no frontmatter delimiters; edit refuses to "
                "synthesize them."
            ),
        )
    fm_text = fm_match.group(1)
    body = fm_match.group(2)

    # Patch updated: (always).
    fm_text = _set_or_append(fm_text, "updated", date_iso)

    # Patch tags: if provided.
    if tags is not None:
        tags_clean = _clean_tags(tags)
        fm_text = _remove_yaml_key(fm_text, "tags")
        if tags_clean:
            fm_text = fm_text.rstrip() + f"\ntags: [" + ", ".join(tags_clean) + "]"
        else:
            fm_text = fm_text.rstrip() + "\ntags: []"

    # Replace body if provided; otherwise keep original body.
    new_body_final = new_body if new_body is not None else body
    # Normalize trailing newline so we don't accumulate blanks across edits.
    new_body_final = new_body_final.rstrip() + "\n"

    new_text = f"---\n{fm_text}\n---\n{new_body_final}"

    # Log entry.
    kb = kb_root(vault_root)
    log_file = kb / "log.md"
    writes: list[PlannedWrite] = [PlannedWrite(path=abs_path, content=new_text)]
    warnings: list[str] = []

    rel_no_ext = rel_path.removesuffix(".md")
    if log_file.exists():
        log_body_parts = [
            f"Edit via kb-mcp. {why.strip()}"
        ]
        changed: list[str] = []
        if new_body is not None:
            changed.append("body")
        if tags is not None:
            changed.append("tags")
        if changed:
            log_body_parts.append(f"Changed: {', '.join(changed)}.")
        log_body = " ".join(log_body_parts)
        new_log = _prepend_log_entry(
            log_file.read_text(encoding="utf-8"),
            date_iso=date_iso,
            rel_no_ext=rel_no_ext,
            body=log_body,
        )
        writes.append(PlannedWrite(path=log_file, content=new_log))
    else:
        warnings.append("Knowledge Base/log.md missing; skipped log entry")

    try:
        batch_atomic_write(writes)
    except Exception as e:
        log.exception("partial write during edit(); some files may be updated")
        warnings.append(f"partial write — reconcile on desktop: {e}")
        raise

    return EditResult(path=rel_path, warnings=warnings)


# ---------------- path resolution ----------------


def _resolve(vault_root: Path, path: str) -> tuple[Path, str]:
    if not path or not path.strip():
        raise EditError(code="INVALID_PATH", missing=["path"], reason="path is empty")
    rel = path.strip().replace("\\", "/").lstrip("/")
    if not rel.startswith("Knowledge Base/"):
        rel = "Knowledge Base/" + rel
    if not rel.endswith(".md"):
        rel = rel + ".md"
    candidate = vault_root / rel
    try:
        resolved = candidate.resolve()
        resolved.relative_to(kb_root(vault_root).resolve())
    except (ValueError, OSError) as e:
        raise EditError(
            code="INVALID_PATH",
            missing=["path"],
            reason=f"path escapes Knowledge Base/: {e}",
        ) from None
    if not candidate.exists():
        raise EditError(
            code="NOT_FOUND",
            missing=["path"],
            reason=f"file does not exist: {rel}",
        )
    return candidate, rel


# ---------------- frontmatter surgery ----------------


_FM_PATTERN = re.compile(r"^---\n(.*?)\n---\n(.*)", re.DOTALL)


def _set_or_append(fm_text: str, key: str, value: str) -> str:
    """Set `key: value` in YAML frontmatter — patch existing line or append."""
    pattern = re.compile(rf"^{re.escape(key)}:.*$", re.MULTILINE)
    if pattern.search(fm_text):
        return pattern.sub(f"{key}: {value}", fm_text, count=1)
    return fm_text.rstrip() + f"\n{key}: {value}"


def _remove_yaml_key(fm_text: str, key: str) -> str:
    """Remove `key: <inline>` line OR `key:\\n  - item\\n  - item` block.

    Used when we're about to rewrite a key from scratch (e.g. tags) and
    don't want to leave the old form around.
    """
    lines = fm_text.split("\n")
    out: list[str] = []
    in_block = False
    key_prefix = f"{key}:"
    for line in lines:
        if in_block:
            if line.lstrip().startswith("- ") or line.startswith(("  ", "\t")):
                continue
            in_block = False
        if line.startswith(key_prefix):
            # Could be inline ("key: foo" or "key: [a,b]") or block header ("key:")
            rest = line[len(key_prefix):].strip()
            if rest == "":
                # block-style header — swallow following indented items
                in_block = True
                continue
            # inline — drop just this line
            continue
        out.append(line)
    return "\n".join(out)


def _clean_tags(tags: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for t in tags:
        norm = str(t).strip().lower().replace(" ", "-").replace("_", "-")
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def _prepend_log_entry(
    text: str, *, date_iso: str, rel_no_ext: str, body: str
) -> str:
    title = rel_no_ext.replace("Knowledge Base/", "", 1)
    new_entry = f"## [{date_iso}] edit | {title}\n\n{body}\n"
    sep_idx = text.find(indexes.LOG_SEPARATOR)
    if sep_idx == -1:
        return text.rstrip() + "\n\n" + new_entry + "\n"
    insertion_point = sep_idx + len(indexes.LOG_SEPARATOR)
    return text[:insertion_point] + "\n" + new_entry + "\n" + text[insertion_point:]
