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
import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from . import find as find_module
from . import indexes, memory_refs, relation_review, semantic_writes
from . import note as note_module
from .kbdir import kb_prefix
from .vault import (
    ContentHashMismatchError,
    PathGuard,
    PathGuardError,
    PlannedWrite,
    batch_atomic_write,
    content_hash,
    kb_root,
    parse_frontmatter,
    plan_log_writes,
    render_wikilink_target,
    rotate_log_if_needed,
)

log = logging.getLogger(__name__)


@dataclass
class ReplaceResult:
    old_path: str   # vault-relative, with .md
    new_path: str   # vault-relative, with .md
    warnings: list[str]
    old_ref: str | None = None
    new_ref: str | None = None
    creation: dict | None = None

    def as_dict(self) -> dict:
        value = {
            "old_path": self.old_path,
            "new_path": self.new_path,
            "warnings": self.warnings,
            "old_ref": self.old_ref,
            "new_ref": self.new_ref,
        }
        if self.creation is not None:
            value["creation"] = self.creation
        return value


@dataclass
class ReplaceError(Exception):
    code: str
    missing: list[str]
    reason: str

    def as_dict(self) -> dict:
        return {"code": self.code, "missing": self.missing, "reason": self.reason}


def _legacy_replace(
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

    # Load one content version of the old page for both eligibility and CAS.
    try:
        old_text = old_resolved.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise ReplaceError(
            code="OLD_NOT_FOUND",
            missing=["old_path"],
            reason=str(e),
        ) from e
    except (OSError, UnicodeDecodeError) as e:
        raise ReplaceError(
            code="UNREADABLE",
            missing=["old_path"],
            reason=str(e),
        ) from e
    old_frontmatter, _old_body, _old_frontmatter_text = parse_frontmatter(old_text)
    old_expected_hash = content_hash(old_text)

    if old_frontmatter.get("status") == "superseded":
        raise ReplaceError(
            code="ALREADY_SUPERSEDED",
            missing=["old_path"],
            reason=(
                f"{rel_old_with_ext} is already marked status: superseded. "
                "Supersede the page that already supersedes it, or start fresh."
            ),
        )

    # Construct note()'s complete write plan without committing it yet. The
    # public note() path still commits normally when this private list is absent.
    note_writes: list[PlannedWrite] = []
    new_result = note_module._legacy_note(
        vault_root,
        today=today,
        _planned_writes=note_writes,
        **note_kwargs,
    )
    new_path_str = new_result.path  # vault-relative, with .md
    rel_new_no_ext = new_path_str.removesuffix(".md")
    if not rel_new_no_ext.startswith(kb_prefix()):
        rel_new_no_ext = kb_prefix() + rel_new_no_ext
    new_resolved = vault_root / new_path_str

    warnings: list[str] = list(new_result.warnings)

    # Inject `supersedes:` into the planned new page's frontmatter.
    new_page_write = next(write for write in note_writes if write.path == new_resolved)
    new_text = new_page_write.content
    old_link_target = render_wikilink_target(rel_old_no_ext, vault_root)
    new_text_updated = _inject_supersedes(new_text, old_link_target)
    if new_text_updated == new_text:
        warnings.append(
            "could not inject supersedes: into new page frontmatter — "
            "frontmatter shape unexpected"
        )
    new_page_write.content = new_text_updated

    # Patch old page: status -> superseded, add superseded_by, refresh updated.
    new_link_target = render_wikilink_target(rel_new_no_ext, vault_root)
    old_text_updated = _mark_superseded(old_text, new_link_target, date_iso)
    if old_text_updated == old_text:
        warnings.append(
            "could not patch old page frontmatter (status/superseded_by/updated) — "
            "manual fixup needed"
        )

    # Append a log entry naming the supersession explicitly.
    kb = kb_root(vault_root)
    log_file = kb / "log.md"
    log_write = next(
        (write for write in reversed(note_writes) if write.path == log_file),
        None,
    )
    if log_write is not None:
        log_body_parts = [
            f"Supersedes `{rel_old_no_ext}` via exomem."
        ]
        if reason and reason.strip():
            log_body_parts.append(reason.strip())
        log_body = " ".join(log_body_parts)
        new_log = _prepend_replace_log_entry(
            log_write.content,
            date_iso=date_iso,
            rel_new_no_ext=rel_new_no_ext,
            body=log_body,
        )
        log_write.content = new_log
    elif log_file.exists():
        warnings.append("note write plan omitted log.md; skipped replace log entry")
    else:
        warnings.append(f"{kb_prefix()}log.md missing; skipped replace log entry")

    writes = note_writes + [
        PlannedWrite(
            path=old_resolved,
            content=old_text_updated,
            expected_hash=old_expected_hash,
        )
    ]

    try:
        batch_atomic_write(writes, vault_root=vault_root)
    except ContentHashMismatchError as e:
        _purge_planned_note(vault_root, rel_new_no_ext)
        raise ReplaceError(
            code="STALE_SUPERSEDE",
            missing=["old_path"],
            reason=(
                f"{rel_old_with_ext} changed after supersession eligibility was read; "
                "retry against the current active page"
            ),
        ) from e
    except Exception as e:
        _purge_planned_note(vault_root, rel_new_no_ext)
        log.exception("supersession batch failed and was rolled back")
        warnings.append(f"supersession batch rolled back: {e}")
        raise

    rotate_note = rotate_log_if_needed(vault_root)
    if rotate_note:
        warnings.append(rotate_note)

    return ReplaceResult(
        old_path=rel_old_with_ext,
        new_path=new_path_str,
        warnings=warnings,
        old_ref=memory_refs.ReferenceIndex(vault_root).ref_for_path(rel_old_with_ext),
        new_ref=new_result.ref,
    )


def _purge_planned_note(vault_root: Path, rel_new_no_ext: str) -> None:
    """Remove note()'s pending resolver entry when its composed batch fails."""
    try:
        find_module.on_resolver_files_changed(
            vault_root, [rel_new_no_ext + ".md"], []
        )
    except Exception:  # noqa: BLE001 — purge is best-effort cleanup
        log.debug("resolver pending-purge failed", exc_info=True)


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
    if new_quoted_value in fm_text:
        return fm_text
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
            line = (
                fm_text[cursor + 1 : line_end]
                if fm_text[cursor] == "\n"
                else fm_text[cursor:line_end]
            )
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
    if new_entry in text:
        return text
    sep_idx = text.find(indexes.LOG_SEPARATOR)
    if sep_idx == -1:
        return text.rstrip() + "\n\n" + new_entry + "\n"
    insertion_point = sep_idx + len(indexes.LOG_SEPARATOR)
    return text[:insertion_point] + "\n" + new_entry + "\n" + text[insertion_point:]


def replace(
    vault_root: Path,
    *,
    old_path: str,
    content: str,
    note_type: str,
    title: str,
    slug: str | None = None,
    project: str | None = None,
    projects: list[str] | None = None,
    sources: list[str] | None = None,
    tags: list[str] | None = None,
    status: str | None = None,
    severity: str | None = None,
    pattern_type: str | None = None,
    domain: str | None = None,
    started: str | None = None,
    duration: str | None = None,
    hypothesis: str | None = None,
    n: int | None = None,
    concluded: str | None = None,
    medium: str | None = None,
    recorded: str | None = None,
    published: str | None = None,
    host: str | None = None,
    editor: str | None = None,
    suggestions: bool = True,
    project_category: str | None = None,
    reason: str | None = None,
    today: dt.date | None = None,
    validate_only: bool = False,
    draft_id: str | None = None,
    draft_hash: str | None = None,
    draft_token: str | None = None,
    relation_disposition: str | None = None,
    relation_review_hash: str | None = None,
    relation_review_reason: str | None = None,
) -> ReplaceResult | semantic_writes.CreationPreflight:
    """Supersede through one successor-last semantic creation batch."""
    root = Path(vault_root)
    old_resolved, rel_old_with_ext = _resolve_kb_path(root, old_path)
    rel_old_no_ext = rel_old_with_ext.removesuffix(".md")
    if "/Sources/" in "/" + rel_old_with_ext or "/Evidence/" in "/" + rel_old_with_ext:
        raise ReplaceError(
            "INVALID_REPLACE",
            ["old_path"],
            f"{rel_old_with_ext} is in an append-only tree",
        )
    try:
        old_bytes = old_resolved.read_bytes()
        old_text = old_bytes.decode("utf-8")
        old_parsed = find_module._parse_page(old_resolved, old_resolved.stat().st_mtime, root)
    except (OSError, UnicodeDecodeError) as error:
        raise ReplaceError("UNREADABLE", ["old_path"], "old page is unreadable") from error
    if old_parsed is None:
        raise ReplaceError("UNREADABLE", ["old_path"], "old page is unreadable")

    recovery_receipt = None
    predecessor_hash = hashlib.sha256(old_bytes).hexdigest()
    try:
        predecessor_guard = PathGuard.capture(
            root,
            rel_old_with_ext,
            leaf_policy="content",
            expected_content_hash=predecessor_hash,
        )
    except PathGuardError as error:
        raise ReplaceError(error.code, ["old_path"], error.reason) from error
    if old_parsed.frontmatter.get("status") == "superseded":
        if draft_id is not None and draft_token is not None:
            try:
                token = semantic_writes.DraftToken.decode(draft_token)
                recovery_receipt = relation_review.load_creation_receipt(root, draft_id)
                supplied_token_hash = relation_review.draft_token_hash(draft_token)
            except (semantic_writes.SemanticWriteError, relation_review.RelationReviewError):
                recovery_receipt = None
            exact_backlink = bool(
                recovery_receipt is not None
                and recovery_receipt.schema_version == 2
                and recovery_receipt.page_identity == draft_id
                and recovery_receipt.page_path_at_review == token.destination
                and recovery_receipt.draft_hash == draft_hash
                and recovery_receipt.operation == "replacement"
                and recovery_receipt.draft_token_hash == supplied_token_hash
                and recovery_receipt.predecessor_path == rel_old_with_ext
                and token.writer == "note"
                and token.operation == "replacement"
                and token.destination.removesuffix(".md")
                in str(old_parsed.frontmatter.get("superseded_by", ""))
            )
            if exact_backlink:
                predecessor_hash = recovery_receipt.predecessor_content_hash or predecessor_hash
            else:
                raise ReplaceError(
                    "ALREADY_SUPERSEDED", ["old_path"], "old page is already superseded"
                )
        else:
            raise ReplaceError(
                "ALREADY_SUPERSEDED", ["old_path"], "old page is already superseded"
            )

    old_link_target = render_wikilink_target(rel_old_no_ext, root)
    try:
        prepared = note_module.note(
            root,
            content=content,
            note_type=note_type,
            title=title,
            slug=slug,
            project=project,
            projects=projects,
            sources=sources,
            tags=tags,
            status=status,
            severity=severity,
            pattern_type=pattern_type,
            domain=domain,
            started=started,
            duration=duration,
            hypothesis=hypothesis,
            n=n,
            concluded=concluded,
            medium=medium,
            recorded=recorded,
            published=published,
            host=host,
            editor=editor,
            suggestions=suggestions,
            today=today,
            project_category=project_category,
            draft_id=draft_id,
            draft_hash=draft_hash,
            draft_token=draft_token,
            relation_disposition=relation_disposition,
            relation_review_hash=relation_review_hash,
            relation_review_reason=relation_review_reason,
            _return_prepared=True,
            _supersedes_target=old_link_target,
            _preflight_operation="replacement",
            _predecessor_path=rel_old_with_ext,
            _predecessor_content_hash=predecessor_hash,
        )
    except note_module.NoteError as error:
        if error.code == "DRAFT_ALREADY_COMMITTED" and recovery_receipt is not None:
            destination = semantic_writes.DraftToken.decode(draft_token).destination
            return ReplaceResult(
                rel_old_with_ext,
                destination,
                ["replacement draft was already committed"],
                memory_refs.ReferenceIndex(root).ref_for_path(rel_old_with_ext),
                memory_refs.memory_ref(draft_id),
                {"applicability": "full", "mutated": False, "already_committed": True},
            )
        raise
    assert isinstance(prepared, note_module._PreparedNote)
    if prepared.preflight.applicability != "full":
        raise ReplaceError(
            "INACTIVE_SUCCESSOR",
            ["status"],
            "replacement successor must be an active governed compiled conclusion",
        )
    if validate_only:
        return prepared.preflight

    render_date = semantic_writes.DraftToken.decode(prepared.draft_token).render_date
    rel_new_no_ext = prepared.destination.removesuffix(".md")
    new_link_target = render_wikilink_target(rel_new_no_ext, root)
    old_updated = _mark_superseded(old_text, new_link_target, render_date)
    if old_parsed.frontmatter.get("status") == "superseded" and recovery_receipt is not None:
        old_updated = old_text
    auxiliary = list(prepared.auxiliary_writes)
    log_file = root / "Knowledge Base" / "log.md"
    replacement_body = f"Supersedes `{rel_old_no_ext}` via exomem."
    if reason and reason.strip():
        replacement_body += " " + reason.strip()
    log_archive_dir = root / "Knowledge Base" / "_archive" / "logs"
    auxiliary = [
        write
        for write in auxiliary
        if write.path != log_file and write.path.parent != log_archive_dir
    ]
    try:
        log_plan = plan_log_writes(
            root,
            date_iso=render_date,
            op="replace",
            rel_path_no_ext=rel_new_no_ext,
            body=replacement_body,
            operation_token=prepared.draft_token,
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise ReplaceError(
            "LOG_PLAN_CONFLICT",
            [],
            "replacement log update could not be planned safely",
        ) from error
    auxiliary.extend(log_plan.writes)
    auxiliary.append(PlannedWrite(old_resolved, old_updated, guard=predecessor_guard))
    try:
        committed = semantic_writes.commit_creation(
            root,
            preflight=prepared.preflight,
            auxiliary_writes=tuple(auxiliary),
            relation_disposition=relation_disposition,
            relation_review_hash=relation_review_hash,
            relation_review_reason=relation_review_reason,
            operation="replacement",
            predecessor_path=rel_old_with_ext,
            predecessor_content_hash=predecessor_hash,
        )
    except relation_review.RelationReviewError as error:
        raise ReplaceError(error.code, [], error.reason) from error
    return ReplaceResult(
        rel_old_with_ext,
        prepared.destination,
        list(prepared.warnings),
        memory_refs.ReferenceIndex(root).ref_for_path(rel_old_with_ext),
        memory_refs.memory_ref(prepared.identity),
        committed.as_dict(),
    )
