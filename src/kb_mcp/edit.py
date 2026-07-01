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
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import corpus_aware
from . import find as find_module
from . import indexes
from .vault import (
    PlannedWrite,
    WikilinkResolver,
    batch_atomic_write,
    content_hash,
    escape_wikilinks_for_log,
    kb_root,
    normalize_body_wikilinks,
)


log = logging.getLogger(__name__)


@dataclass
class EditResult:
    path: str  # vault-relative, with .md
    warnings: list[str]

    def as_dict(self) -> dict:
        return {"path": self.path, "warnings": self.warnings}


@dataclass
class EditValidation:
    """Preview returned by `edit(validate_only=True)` — no write performed."""

    path: str            # vault-relative, with .md
    validate_only: bool  # always True
    mode: str            # "surgical"
    match_count: int     # occurrences of old_string in the body
    matches: list[str]   # the line(s) around each occurrence (capped)

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "validate_only": self.validate_only,
            "mode": self.mode,
            "match_count": self.match_count,
            "matches": self.matches,
        }


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
    old_string: str | None = None,
    new_string: str | None = None,
    replace_all: bool = False,
    heading: str | None = None,
    section_position: str = "append",
    expected_hash: str | None = None,
    validate_only: bool = False,
    today: dt.date | None = None,
) -> EditResult | EditValidation:
    """Edit a compiled page in place. Bumps `updated:`.

    Four (composable) modes:
    - `new_body` — replace the whole body. The heavyweight mode.
    - `tags` — replace the `tags:` frontmatter list.
    - `old_string`/`new_string` — **surgical** string-replace inside the body.
      Token-cheap: the caller sends only the changed snippet instead of the
      whole body. By default `old_string` must occur exactly once (an
      ambiguous match is an error so you never edit the wrong row); pass
      `replace_all=True` to replace every occurrence. Surgical mode cannot be
      combined with `new_body` (they both rewrite the body), but may be paired
      with `tags`.
    - `heading` + `section_position` — **section-targeted**. Locate the
      `## Heading` (or its bare text), take its section (from the heading line
      to the next heading of equal-or-higher level, or EOF), and
      replace/prepend/append `new_string` within it. Token-cheap: you don't
      re-send the page or quote the exact snippet. Mutually exclusive with
      `new_body`/`old_string`; may be paired with `tags`.

    `why` is required — it lands in the log entry so the change is auditable.
    """
    missing: list[str] = []
    reasons: list[str] = []

    surgical = old_string is not None
    heading_mode = heading is not None

    if heading_mode and new_body is not None:
        missing.append("heading/new_body")
        reasons.append(
            "section mode (`heading`) and whole-body mode (`new_body`) both rewrite "
            "the body — supply one or the other, not both"
        )
    if heading_mode and surgical:
        missing.append("heading/old_string")
        reasons.append(
            "section mode (`heading`) and surgical mode (`old_string`) are mutually "
            "exclusive — supply one or the other"
        )
    if heading_mode and new_string is None:
        missing.append("new_string")
        reasons.append("`new_string` (the section content) is required in heading mode")
    if heading_mode and section_position not in ("replace", "prepend", "append"):
        missing.append("section_position")
        reasons.append(
            f"section_position must be replace|prepend|append, got {section_position!r}"
        )

    if surgical and new_body is not None:
        missing.append("old_string/new_body")
        reasons.append(
            "surgical mode (`old_string`) and whole-body mode (`new_body`) both "
            "rewrite the body — supply one or the other, not both"
        )
    if surgical and new_string is None:
        missing.append("new_string")
        reasons.append("`new_string` is required when `old_string` is given")
    if surgical and new_string is not None and new_string == old_string:
        missing.append("new_string")
        reasons.append("`new_string` equals `old_string` — that's a no-op edit")
    if not surgical and not heading_mode and new_string is not None:
        missing.append("old_string")
        reasons.append("`new_string` given without `old_string` — nothing to match")
    if new_body is None and tags is None and not surgical and not heading_mode:
        missing.append("new_body, tags, heading, or old_string")
        reasons.append(
            "must supply at least one of `new_body`, `tags`, `heading`, or "
            "`old_string`/`new_string`; otherwise there's nothing to edit"
        )
    if not why or not why.strip():
        missing.append("why")
        reasons.append("why is required — edits without rationale aren't auditable")
    if validate_only and not surgical:
        missing.append("old_string")
        reasons.append(
            "validate_only previews a surgical match — it needs `old_string` "
            "(there's nothing to preview for whole-body or tags edits)"
        )

    if missing:
        raise EditError(
            code="INVALID_EDIT", missing=missing, reason="; ".join(reasons)
        )

    today = today or dt.date.today()
    date_iso = today.isoformat()

    editable = load_editable(vault_root, path, expected_hash=expected_hash)
    abs_path = editable.abs_path
    rel_path = editable.rel_path
    fm_text = editable.fm_text
    body = editable.body

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

    # Resolve the new body across the three modes.
    body_warnings: list[str] = []
    body_changed = False

    if surgical:
        # old_string/new_string are not None here (validated above).
        if validate_only:
            # Preview only — report the count (don't raise on 0 or >1; seeing
            # the count is the whole point) and write nothing.
            return EditValidation(
                path=rel_path,
                validate_only=True,
                mode="surgical",
                match_count=body.count(old_string),  # type: ignore[arg-type]
                matches=_match_contexts(body, old_string),  # type: ignore[arg-type]
            )
        new_body_final, body_warnings = apply_surgical_replace(
            body,
            old_string,  # type: ignore[arg-type]
            new_string,  # type: ignore[arg-type]
            replace_all,
            vault_root,
            rel_path=rel_path,
        )
        body_changed = True
    elif heading_mode:
        # new_string is not None here (validated above).
        new_body_final, body_warnings = apply_section_edit(
            body,
            heading,  # type: ignore[arg-type]
            section_position,
            new_string,  # type: ignore[arg-type]
            vault_root,
            rel_path=rel_path,
        )
        body_changed = True
    elif new_body is not None:
        # Normalize wikilinks to canonical full form. Existing body is left
        # alone to preserve user-intended legacy forms in untouched files.
        resolver = WikilinkResolver(vault_root)
        new_body_final, body_warnings = normalize_body_wikilinks(
            new_body, vault_root, resolver=resolver
        )
        body_changed = True
    else:
        new_body_final = body

    # Normalize trailing newline so we don't accumulate blanks across edits.
    new_body_final = new_body_final.rstrip() + "\n"

    # Conflict (contradiction-band) surfacing — only when the body actually
    # changed (a tags-only edit can't introduce a new claim, so it skips the
    # embed entirely). Best-effort and PRE-commit, so the page's own freshly-
    # edited vectors aren't in the sidecar yet; `self_path` excludes it anyway.
    # Measurement, never a block — appended to the edit's warnings.
    if body_changed and not os.environ.get("KB_MCP_DISABLE_EMBEDDINGS"):
        try:
            body_warnings = list(body_warnings) + [
                corpus_aware.overlap_warning(c)
                for c in corpus_aware.detect_contradictions(
                    vault_root, title="", body=new_body_final, self_path=rel_path
                )
            ]
        except Exception as e:  # noqa: BLE001 — nudges never break an edit
            log.debug("corpus-aware contradiction check failed (non-fatal): %s", e)

    new_text = f"---\n{fm_text}\n---\n{new_body_final}"

    changed: list[str] = []
    if body_changed:
        if surgical:
            changed.append("body (surgical)")
        elif heading_mode:
            changed.append(f"body (section: {heading})")
        else:
            changed.append("body")
    if tags is not None:
        changed.append("tags")

    warnings = commit_edit(
        vault_root,
        abs_path=abs_path,
        rel_path=rel_path,
        new_text=new_text,
        date_iso=date_iso,
        why=why,
        changed=changed,
        extra_warnings=body_warnings,
    )
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


# ---------------- shared load / apply / commit (edit + multi_edit) ----------------


@dataclass
class _Editable:
    """A page resolved + guarded for in-place editing, split into parts."""

    abs_path: Path
    rel_path: str
    original_text: str
    fm_text: str
    body: str


def load_editable(
    vault_root: Path, path: str, *, expected_hash: str | None = None
) -> _Editable:
    """Resolve + guard a page for in-place editing, returning its split parts.

    Runs every safety gate, in the exact order `edit` used inline: append-only
    refusal (Sources/Evidence), NOT_FOUND, superseded refusal, the optimistic-
    concurrency `expected_hash` guard, and the frontmatter-required check.
    Shared by `edit` and `multi_edit`.
    """
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

    # Optimistic-concurrency guard. If the caller passed the hash it read via
    # `get`, refuse when the file changed on disk since — don't clobber another
    # writer. Checked before any mutation; the `updated:` bump happens after
    # this read, so it never self-trips.
    if expected_hash is not None and content_hash(original_text) != expected_hash:
        raise EditError(
            code="STALE_EDIT",
            missing=["expected_hash"],
            reason=(
                f"{rel_path} changed on disk since you read it "
                "(expected_hash mismatch). Re-read the page with `get` and "
                "retry the edit against the current content."
            ),
        )

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
    return _Editable(
        abs_path=abs_path,
        rel_path=rel_path,
        original_text=original_text,
        fm_text=fm_match.group(1),
        body=fm_match.group(2),
    )


def apply_surgical_replace(
    body: str,
    old_string: str,
    new_string: str,
    replace_all: bool,
    vault_root: Path,
    *,
    rel_path: str = "",
    resolver: WikilinkResolver | None = None,
    pair_index: int | None = None,
) -> tuple[str, list[str]]:
    """Count, validate, and apply one surgical replace → (new_body, warnings).

    Raises EditError (STRING_NOT_FOUND / AMBIGUOUS_MATCH) exactly as `edit`'s
    inline surgical mode did. `pair_index` is woven into messages when called
    from `multi_edit` so a failing pair is identifiable. Pass a shared
    `resolver` to avoid rebuilding the wikilink index per pair.
    """
    where = f" in {rel_path}" if rel_path else ""
    which = f" (edit #{pair_index})" if pair_index is not None else ""
    count = body.count(old_string)
    if count == 0:
        raise EditError(
            code="STRING_NOT_FOUND",
            missing=["old_string"],
            reason=(
                f"`old_string` not found{where}{which}. It must match the file "
                "exactly, including whitespace. Read the page (or the section) "
                "first to copy the snippet verbatim."
            ),
        )
    if count > 1 and not replace_all:
        raise EditError(
            code="AMBIGUOUS_MATCH",
            missing=["old_string"],
            reason=(
                f"`old_string` occurs {count}×{where}{which}; refusing to guess "
                "which. Add surrounding context to make it unique, or pass "
                "replace_all=True to replace every occurrence."
            ),
        )
    # Normalize wikilinks only in the inserted snippet — the rest of the body
    # is left byte-for-byte untouched (no incidental legacy rewrites).
    if resolver is None:
        resolver = WikilinkResolver(vault_root)
    new_string_norm, warnings = normalize_body_wikilinks(
        new_string, vault_root, resolver=resolver
    )
    n = -1 if replace_all else 1
    return body.replace(old_string, new_string_norm, n), warnings


_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$")


def _normalize_heading(h: str) -> str:
    """Drop leading `#` markers + surrounding whitespace so `## Foo` == `Foo`."""
    return h.lstrip("#").strip()


def apply_section_edit(
    body: str,
    heading: str,
    position: str,
    content: str,
    vault_root: Path,
    *,
    rel_path: str = "",
    resolver: WikilinkResolver | None = None,
) -> tuple[str, list[str]]:
    """Replace/prepend/append `content` within the section under `heading`.

    The section runs from the matched `## Heading` line to the next heading of
    EQUAL-OR-HIGHER level (or EOF). The heading line itself is preserved; only
    its body is rewritten. Returns `(new_body, warnings)`.

    Raises EditError(code="HEADING_NOT_FOUND") if the heading is absent. Only
    the inserted `content` is wikilink-normalized — the rest of the body is left
    byte-for-byte untouched (matching `apply_surgical_replace`).
    """
    where = f" in {rel_path}" if rel_path else ""
    want = _normalize_heading(heading)
    lines = body.split("\n")

    h_idx = -1
    h_level = 0
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m and m.group(2).strip() == want:
            h_idx = i
            h_level = len(m.group(1))
            break
    if h_idx == -1:
        raise EditError(
            code="HEADING_NOT_FOUND",
            missing=["heading"],
            reason=(
                f"heading {heading!r} not found{where}. Match an existing `## Heading` "
                "(the `#` markers are optional in the arg); read the page first."
            ),
        )

    end = len(lines)
    for j in range(h_idx + 1, len(lines)):
        m = _HEADING_RE.match(lines[j])
        if m and len(m.group(1)) <= h_level:
            end = j
            break

    if resolver is None:
        resolver = WikilinkResolver(vault_root)
    content_norm, warnings = normalize_body_wikilinks(
        content, vault_root, resolver=resolver
    )
    content_lines = content_norm.split("\n")

    before = lines[:h_idx]
    head_line = lines[h_idx]
    section_lines = lines[h_idx + 1 : end]
    after = lines[end:]

    if position == "replace":
        new_section = content_lines
    elif position == "prepend":
        new_section = content_lines + section_lines
    else:  # append — after the section's existing content, before the next heading
        trimmed = list(section_lines)
        while trimmed and trimmed[-1].strip() == "":
            trimmed.pop()
        new_section = trimmed + content_lines

    return "\n".join(before + [head_line] + new_section + after), warnings


def commit_edit(
    vault_root: Path,
    *,
    abs_path: Path,
    rel_path: str,
    new_text: str,
    date_iso: str,
    why: str,
    changed: list[str],
    op: str = "edit",
    extra_warnings: list[str] | None = None,
) -> list[str]:
    """Stage page write + opportunistic index refresh + ONE log entry; commit atomically.

    Shared by `edit` and `multi_edit` so both produce exactly one log entry and
    one embedding re-sync per call. Returns the warnings list (extended with any
    log-missing / partial-write warnings).
    """
    kb = kb_root(vault_root)
    log_file = kb / "log.md"
    writes: list[PlannedWrite] = [PlannedWrite(path=abs_path, content=new_text)]
    warnings: list[str] = list(extra_warnings or [])

    # Opportunistic sub-index refresh — surfacing any drift on every write
    # keeps the indexes self-healing.
    top_index = kb / "index.md"
    if top_index.exists():
        current_top = top_index.read_text(encoding="utf-8")
        sub_writes, new_top = indexes.compute_subindex_writes(
            vault_root, top_index_text=current_top
        )
        if new_top is not None and new_top != current_top:
            writes.append(PlannedWrite(path=top_index, content=new_top))
        writes.extend(sub_writes)

    rel_no_ext = rel_path.removesuffix(".md")
    if log_file.exists():
        log_body_parts = [f"Edit via exomem. {why.strip()}"]
        if changed:
            log_body_parts.append(f"Changed: {', '.join(changed)}.")
        log_body = " ".join(log_body_parts)
        new_log = _prepend_log_entry(
            log_file.read_text(encoding="utf-8"),
            date_iso=date_iso,
            rel_no_ext=rel_no_ext,
            body=log_body,
            op=op,
        )
        writes.append(PlannedWrite(path=log_file, content=new_log))
    else:
        warnings.append("Knowledge Base/log.md missing; skipped log entry")

    try:
        batch_atomic_write(writes, vault_root=vault_root)
    except Exception as e:
        log.exception("partial write during edit(); some files may be updated")
        warnings.append(f"partial write — reconcile on desktop: {e}")
        raise
    return warnings


# ---------------- validate-only preview ----------------


def _match_contexts(body: str, old_string: str, *, max_matches: int = 5) -> list[str]:
    """Return the full line(s) each occurrence of `old_string` spans (capped).

    Used by validate_only so the caller can eyeball *which* rows a match (or a
    replace_all) would touch before committing.
    """
    contexts: list[str] = []
    start = 0
    while len(contexts) < max_matches:
        idx = body.find(old_string, start)
        if idx == -1:
            break
        line_start = body.rfind("\n", 0, idx) + 1
        end = idx + len(old_string)
        nl = body.find("\n", end)
        line_end = nl if nl != -1 else len(body)
        contexts.append(body[line_start:line_end])
        start = end if end > idx else idx + 1
    return contexts


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
    text: str, *, date_iso: str, rel_no_ext: str, body: str, op: str = "edit"
) -> str:
    title = rel_no_ext.replace("Knowledge Base/", "", 1)
    new_entry = f"## [{date_iso}] {op} | {title}\n\n{escape_wikilinks_for_log(body)}\n"
    sep_idx = text.find(indexes.LOG_SEPARATOR)
    if sep_idx == -1:
        return text.rstrip() + "\n\n" + new_entry + "\n"
    insertion_point = sep_idx + len(indexes.LOG_SEPARATOR)
    return text[:insertion_point] + "\n" + new_entry + "\n" + text[insertion_point:]
