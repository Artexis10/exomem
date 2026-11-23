"""The `set_frontmatter_field` Tier 2 op: surgical edit of one frontmatter key.

For when `edit` is overkill (it rewrites whole body or tags). This patches
exactly one key, leaves the body alone, and always bumps `updated:`.

Refuses Sources/ and Evidence/. A field that the `page-fields` registry declares a
pointer is corrected there only by a curation `edit` step, whose plan the user confirms
and whose compensation reverts it: `set_pointer_field` is that step's writer, through the
governed pointer write, which patches the value span alone and sets no `updated`. Curated
trees need `allow_curated=true`. `why` is required — lands in the log entry.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (
    capabilities,
    page_fields,
    page_ids,
    page_lineage,
    pointer_write,
    project_keys,
    semantic_writes,
    temporal,
    vocabulary_resolution,
)
from .vault import _FM_PATTERN as _VAULT_FM_PATTERN
from .vault import (
    EXCLUDED_FIELD_CODE,
    UPDATED_FIELD,
    PlannedWrite,
    VaultPathError,
    _format_yaml_line,
    document_newline,
    excluded_frontmatter_reason,
    governed_frontmatter_reason,
    in_append_only_tree,
    in_curated_tree,
    kb_root,
    parse_frontmatter,
    plan_log_writes,
    read_guarded_text,
    render_frontmatter_document,
    resolve_under_vault,
)

log = logging.getLogger(__name__)

# One definition, shared with `vault`: these modules fold CRLF to LF before
# matching, but a private copy is free to drift out of that agreement.
_FM_PATTERN = _VAULT_FM_PATTERN


@dataclass
class SetFrontmatterResult:
    path: str
    field: str
    old_value: Any
    new_value: Any
    warnings: list[str]
    semantic: dict | None = None
    #: The record of a pointer write to an append-only page, which its revert names.
    pointer_write: str | None = None

    def as_dict(self) -> dict:
        result = {
            "path": self.path,
            "field": self.field,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "warnings": self.warnings,
        }
        if self.semantic is not None:
            result["semantic"] = self.semantic
        if self.pointer_write is not None:
            result["pointer_write"] = self.pointer_write
        return result


@dataclass
class SetFrontmatterValidation:
    path: str
    field: str
    old_value: Any
    new_value: Any
    validate_only: bool
    semantic: dict | None = None

    def as_dict(self) -> dict:
        result = {
            "path": self.path,
            "field": self.field,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "validate_only": self.validate_only,
        }
        if self.semantic is not None:
            result["semantic"] = self.semantic
        return result


@dataclass
class SetFrontmatterError(Exception):
    code: str
    reason: str

    def as_dict(self) -> dict:
        return {"code": self.code, "reason": self.reason}


def set_frontmatter_field(
    vault_root: Path,
    *,
    path: str,
    field: str,
    value: Any,
    why: str,
    allow_curated: bool = False,
    expected_hash: str | None = None,
    today: dt.date | None = None,
    validate_only: bool = False,
    semantic_transition_token: str | None = None,
    relation_disposition: str | None = None,
    relation_review_hash: str | None = None,
    relation_review_reason: str | None = None,
    _validate_frontmatter: Callable[[str, str], None] | None = None,
) -> SetFrontmatterResult | SetFrontmatterValidation:
    """Set one frontmatter field."""
    _require_settable(field, why)

    try:
        abs_path, rel_path = resolve_under_vault(
            vault_root, path, must_exist=True, must_be_file=True
        )
    except VaultPathError as e:
        raise SetFrontmatterError(code=e.code, reason=e.reason) from e

    append_only = in_append_only_tree(rel_path)
    if append_only:
        reason = (
            f"{rel_path} is in {append_only}/ which is append-only. "
            f"Frontmatter edits would violate rule 2."
        )
        if field in page_fields.pointer_fields(vault_root):
            reason += " " + (owned_field_route(vault_root, field) or _curation_route())
        else:
            from . import source_corrections

            if route := source_corrections.correction_route(vault_root, rel_path):
                reason += " " + route
        raise SetFrontmatterError(code="APPEND_ONLY", reason=reason)

    curated = in_curated_tree(rel_path)
    if curated and not allow_curated:
        raise SetFrontmatterError(
            code="CURATED_PROTECTED",
            reason=(
                f"{rel_path} is in curated tree {curated!r} (desk-managed). "
                f"Pass `allow_curated=true` to override."
            ),
        )

    try:
        text, guard = read_guarded_text(vault_root, abs_path)
    except (OSError, UnicodeError) as error:
        raise SetFrontmatterError(
            code="UNREADABLE", reason=f"could not safely read {rel_path}"
        ) from error
    # Match the public read/edit contract: parse and render logical Markdown
    # with LF newlines while semantic_writes retains the raw-byte PathGuard for
    # the commit. Windows-authored CRLF must not make valid frontmatter unreadable.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    m = _FM_PATTERN.match(text)
    if not m:
        raise SetFrontmatterError(
            code="FRONTMATTER_REQUIRED",
            reason=(
                f"{rel_path} has no frontmatter delimiters; this operation "
                "requires frontmatter and refuses to synthesize it."
            ),
        )
    fm_text = m.group(1)
    body = m.group(2)

    now = today or temporal.now()
    try:
        date_iso = semantic_writes.resolve_reviewed_date_iso(
            semantic_transition_token, now
        )
    except semantic_writes.SemanticWriteError as error:
        raise SetFrontmatterError(error.code, error.reason) from error

    value = _governed_enum_value(
        field,
        value,
        page_type=_read_yaml_field(fm_text, "type"),
        page=(vault_root, rel_path, parse_frontmatter(text)[0]),
    )

    topic_writes: tuple[PlannedWrite, ...] = ()
    topic_warnings: list[str] = []
    if field == "topics":
        try:
            value, topic_plan, topic_warnings = vocabulary_resolution.plan_topic_registrations(vault_root, value)
        except vocabulary_resolution.VocabularyResolutionError as error:
            raise SetFrontmatterError(error.code, error.reason) from error
        topic_writes = topic_plan.writes

    old_value = _read_yaml_field(fm_text, field)
    fm_text = _remove_yaml_key(fm_text, field)
    new_line = _format_yaml_line(field, value)
    fm_text = fm_text.rstrip() + "\n" + new_line

    # Always bump updated:
    fm_text = _remove_yaml_key(fm_text, "updated")
    fm_text = fm_text.rstrip() + f"\nupdated: {date_iso}"

    new_text = render_frontmatter_document(
        fm_text, body, newline=document_newline(text)
    )
    if _validate_frontmatter is not None:
        _validate_frontmatter(text, new_text)

    project_plan = _plan_project_keys(vault_root, field, value)
    # A short id names a version by a tail of its hash, which the preflight's full-hash guard cannot
    # compare. The preflight is this door's admission, so the id is compared only after it, against
    # the bytes read above: a caller it refuses learns nothing of the page's version.
    by_id = expected_hash is not None and page_ids.is_page_id(expected_hash)
    try:
        preflight = semantic_writes.preflight_existing(
            vault_root,
            path=rel_path,
            after_source=new_text,
            operation="edit",
            expected_before_hash=guard.expected_content_hash
            if by_id
            else expected_hash or guard.expected_content_hash,
            transition_token=semantic_transition_token,
            relation_disposition=relation_disposition,
            relation_review_hash=relation_review_hash,
            relation_review_reason=relation_review_reason,
            stamp=date_iso,
            validate_only=validate_only,
        )
    except semantic_writes.SemanticWriteError as error:
        raise SetFrontmatterError(error.code, error.reason) from error
    if by_id and not page_ids.guard_matches(
        str(expected_hash), guard.expected_content_hash or ""
    ):
        raise SetFrontmatterError(
            code="STALE_EDIT",
            reason=(
                f"{rel_path} changed on disk since you read it (expected_hash mismatch). "
                "Re-read the page with `get` and retry the edit against the current content."
            ),
        )

    if validate_only:
        return SetFrontmatterValidation(
            path=rel_path,
            field=field,
            old_value=old_value,
            new_value=value,
            validate_only=True,
            semantic=preflight.as_dict(),
        )

    warnings: list[str] = list(topic_warnings)
    rel_no_ext = rel_path.removesuffix(".md") if rel_path.endswith(".md") else rel_path
    log_body = (
        f"set_frontmatter_field via exomem. {why.strip()} "
        f"Field: {field!r}. Old: {old_value!r}. New: {value!r}."
    )
    if curated and allow_curated:
        log_body += f" allow_curated=true (target tree: {curated})."
    try:
        log_plan = plan_log_writes(
            vault_root,
            date_iso=date_iso,
            op="set_frontmatter_field",
            rel_path_no_ext=rel_no_ext,
            body=log_body,
            operation_token=preflight.transition_token,
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise SetFrontmatterError(
            "LOG_PLAN_CONFLICT", "frontmatter log update could not be planned safely"
        ) from error
    if log_plan.warning is not None:
        warnings.append(log_plan.warning)
    if log_plan.rotation_note is not None:
        warnings.append(log_plan.rotation_note)

    auxiliaries = [*project_plan, *topic_writes, *log_plan.writes]
    try:
        committed = semantic_writes.commit_existing(
            vault_root,
            preflight=preflight,
            auxiliary_writes=tuple(auxiliaries),
            derived_auxiliary_writes=tuple(("operation-log", write) for write in log_plan.writes),
        )
    except semantic_writes.SemanticWriteError as error:
        raise SetFrontmatterError(error.code, error.reason) from error
    except Exception as error:
        log.exception("set_frontmatter_field write failed for %s", rel_path)
        warnings.append(f"partial write — reconcile on desktop: {error}")
        raise

    return SetFrontmatterResult(
        path=rel_path,
        field=field,
        old_value=old_value,
        new_value=value,
        warnings=warnings,
        semantic=committed.as_dict(),
    )


def _require_settable(field: str, why: str) -> None:
    """Refuse a field that no frontmatter write may set: the write stamp, lineage or an excluded key."""
    if not field or not field.strip():
        raise SetFrontmatterError(
            code="INVALID_SET", reason="field is required"
        )
    if not why or not why.strip():
        raise SetFrontmatterError(
            code="INVALID_SET",
            reason="why is required — frontmatter edits without rationale aren't auditable",
        )
    if field == UPDATED_FIELD:
        raise SetFrontmatterError(
            code="INVALID_SET",
            reason=(
                "cannot set `updated:` directly — it is always bumped to the "
                "write instant by this operation"
            ),
        )
    if field in page_lineage.FIELDS:
        raise SetFrontmatterError(
            code="INVALID_SET",
            reason=(
                f"cannot set `{field}:` directly — a page's lineage is written only by the move, "
                "undo or reclassification that relocates it"
            ),
        )
    excluded_reason = excluded_frontmatter_reason(field)
    if excluded_reason is not None:
        raise SetFrontmatterError(code=EXCLUDED_FIELD_CODE, reason=excluded_reason)


def _curation_route() -> str:
    return (
        "Correct a pointer field there with a curation `edit` step: propose it with "
        + capabilities.active_call_text(
            "maintain_memory", {"mode": "curation", "curation_action": "propose"}
        )
        + ", and apply it after the user confirms the previewed plan; its compensation reverts it."
    )


def owned_field_route(vault_root: Path, field: str) -> str | None:
    """The sentence naming the one operation that writes `field` on a captured page, or None.

    A pointer field with a registry `writer` changes there only through that operation, because
    it checks what a plain correction cannot. Code maps the closed `writer` values, never keys.
    """
    writer = page_fields.writers(vault_root).get(field)
    if writer is None:
        return None
    if writer == page_fields.WRITER_RECLASSIFY:
        call = capabilities.active_call_text(
            "manage_memory_file", {"operation": "reclassify", "reason": "..."}
        )
        return f"`{field}` changes only through {call}, which relocates the source and records its history."
    if writer == page_fields.WRITER_CUSTODY:
        call = capabilities.active_call_text(
            "manage_memory_file", {"operation": "reclassify", "artifact_path": "...", "reason": "..."}
        )
        return f"`{field}` changes only through {call}, which checks the raw file's recorded identity."
    return (
        f"`{field}` is the back-reference that a compiled page's `sources:` citation writes; "
        "edit the citing page instead."
    )


def set_pointer_field(
    vault_root: Path,
    *,
    path: str,
    field: str,
    value: Any,
    why: str,
    expected_hash: str | None,
    today: dt.date | None = None,
    validate_only: bool = False,
    operation: str | None = None,
) -> SetFrontmatterResult | SetFrontmatterValidation:
    """Correct a pointer field of a page through the governed pointer write.

    Only a curation `edit` step calls this: the user confirms its exact plan, and `operation`
    files the record where the step's compensation finds it. The value span is patched where
    the YAML parser places it and every other byte, line endings included, is kept: `updated`
    is not a pointer, so the write's span proof would refuse it. A field that another operation
    owns (`owned_field_route`) refuses. A page outside the append-only trees takes the same
    route, so a change that touches only where it belongs is reversible for every page type; a
    page in a curated tree refuses, as `set_frontmatter_field` refuses it without `allow_curated`.
    """
    from .record_formats import FrontmatterSpliceError, splice_markdown_frontmatter

    _require_settable(field, why)
    try:
        abs_path, rel_path = resolve_under_vault(
            vault_root, path, must_exist=True, must_be_file=True
        )
    except VaultPathError as e:
        raise SetFrontmatterError(code=e.code, reason=e.reason) from e
    curated = in_curated_tree(rel_path) if in_append_only_tree(rel_path) is None else None
    if curated:
        raise SetFrontmatterError(
            code="CURATED_PROTECTED",
            reason=f"{rel_path} is in curated tree {curated!r} (desk-managed); edit it there.",
        )
    if field not in page_fields.pointer_fields(vault_root):
        if in_append_only_tree(rel_path) is None:
            raise SetFrontmatterError(
                code="INVALID_SET",
                reason=f"`{field}` is not a pointer field; edit it in {rel_path} as any page.",
            )
        raise SetFrontmatterError(
            code="APPEND_ONLY",
            reason=f"`{field}` is not a pointer field, so it keeps its captured bytes in {rel_path}.",
        )
    owner = owned_field_route(vault_root, field)
    if owner is not None:
        raise SetFrontmatterError(code="POINTER_FIELD_OWNED", reason=owner)
    try:
        text, guard = read_guarded_text(vault_root, abs_path)
    except (OSError, UnicodeError) as error:
        raise SetFrontmatterError(
            code="UNREADABLE", reason=f"could not safely read {rel_path}"
        ) from error
    front, _body, block = parse_frontmatter(text)
    if block is None:
        raise SetFrontmatterError(
            code="FRONTMATTER_REQUIRED",
            reason=f"{rel_path} has no frontmatter block; a pointer correction never synthesizes one.",
        )
    old_value = front.get(field)
    value = _governed_enum_value(field, value, page_type=front.get("type"), page=(vault_root, rel_path, front))
    project_plan = _plan_project_keys(vault_root, field, value)
    try:
        after = splice_markdown_frontmatter(text, {field: value})
        write = pointer_write.plan(
            vault_root, rel_path, text, after, pointer_write.Declared(fields={field}),
            reason=why.strip(),
        )
    except (FrontmatterSpliceError, pointer_write.PointerWriteError) as error:
        raise SetFrontmatterError(error.code, error.reason) from error
    now = today or temporal.now()
    try:
        date_iso = semantic_writes.resolve_reviewed_date_iso(None, now)
    except semantic_writes.SemanticWriteError as error:
        raise SetFrontmatterError(error.code, error.reason) from error
    by_id = expected_hash is not None and page_ids.is_page_id(expected_hash)
    try:
        preflight = semantic_writes.preflight_existing(
            vault_root,
            path=rel_path,
            after_source=after,
            operation="edit",
            expected_before_hash=guard.expected_content_hash
            if by_id
            else expected_hash or guard.expected_content_hash,
            stamp=date_iso,
            validate_only=validate_only,
        )
    except semantic_writes.SemanticWriteError as error:
        raise SetFrontmatterError(error.code, error.reason) from error
    if by_id and not page_ids.guard_matches(str(expected_hash), guard.expected_content_hash or ""):
        raise SetFrontmatterError(
            code="STALE_EDIT",
            reason=(
                f"{rel_path} changed on disk since you read it (expected_hash mismatch). "
                "Re-read the page with `get` and retry the edit against the current content."
            ),
        )
    if validate_only:
        return SetFrontmatterValidation(
            path=rel_path, field=field, old_value=old_value, new_value=value,
            validate_only=True, semantic=preflight.as_dict(),
        )
    rel_no_ext = rel_path.removesuffix(".md")
    record_id = pointer_write.new_record_id()
    try:
        log_plan = plan_log_writes(
            vault_root,
            date_iso=date_iso,
            op="set_frontmatter_field",
            rel_path_no_ext=rel_no_ext,
            body=(
                f"set_frontmatter_field via exomem (pointer write {record_id}). {why.strip()} "
                f"Field: {field!r}. Old: {old_value!r}. New: {value!r}."
            ),
            operation_token=preflight.transition_token,
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise SetFrontmatterError(
            "LOG_PLAN_CONFLICT", "frontmatter log update could not be planned safely"
        ) from error
    warnings = [note for note in (log_plan.warning, log_plan.rotation_note) if note is not None]
    try:
        with pointer_write.recording(vault_root, [(write, record_id)], operation=operation):
            committed = semantic_writes.commit_existing(
                vault_root,
                preflight=preflight,
                auxiliary_writes=(*project_plan, *log_plan.writes),
                derived_auxiliary_writes=tuple(
                    ("operation-log", write) for write in log_plan.writes
                ),
            )
    except semantic_writes.SemanticWriteError as error:
        raise SetFrontmatterError(error.code, error.reason) from error
    return SetFrontmatterResult(
        path=rel_path, field=field, old_value=old_value, new_value=value,
        warnings=warnings, semantic=committed.as_dict(), pointer_write=record_id,
    )


def apply_pointer_patch(
    vault_root: Path, rel_path: str, text: str, fields: Mapping[str, Any]
) -> str:
    """`text` with each field of `fields` set, as `set_frontmatter_field` would set it.

    A rehome moves a page and changes where it belongs in one commit (a curation `move` step's
    `fields` patch). The caller limits `fields` to registry pointer fields, and
    `pointer_patch_writes` plans the registry writes that their values need.
    """
    from .record_formats import FrontmatterSpliceError, splice_markdown_frontmatter

    front, _body, block = parse_frontmatter(text)
    if block is None:
        raise SetFrontmatterError(
            code="FRONTMATTER_REQUIRED",
            reason=f"{rel_path} has no frontmatter block to hold the patched fields",
        )
    values = {
        field: _governed_enum_value(
            field, value, page_type=front.get("type"), page=(vault_root, rel_path, front)
        )
        for field, value in fields.items()
    }
    try:
        return splice_markdown_frontmatter(text, values)
    except FrontmatterSpliceError as error:
        raise SetFrontmatterError(error.code, error.reason) from error


def pointer_patch_writes(
    vault_root: Path, fields: Mapping[str, Any], *, folders: Mapping[str, str] | None = None
) -> tuple[PlannedWrite, ...]:
    """The registry writes that the values of a `fields` patch need, one per registry file.

    `folders` names the folder of each project key the patch introduces; a key it omits
    gets the registry's default folder.
    """
    writes: dict[Path, PlannedWrite] = {}
    for field, value in fields.items():
        for write in _plan_project_keys(vault_root, field, value, folders=folders):
            held = writes.get(write.path)
            if held is not None and held.content != write.content:
                raise SetFrontmatterError(
                    code="FIELDS_PATCH_CONFLICT",
                    reason="the patched fields register different keys in one registry; "
                    "patch them in separate steps",
                )
            writes[write.path] = write
    return tuple(writes.values())


def _plan_project_keys(
    vault_root: Path, field: str, value: Any, *, folders: Mapping[str, str] | None = None
) -> tuple[PlannedWrite, ...]:
    """If the patched field carries project keys (page-field registry), validate via the registry.

    Mirrors note.py's behaviour: slug-shaped new keys auto-register,
    Levenshtein-close keys raise as PROJECT_KEY_TYPO so the agent self-
    corrects. Other fields are unaffected.
    """
    if field not in page_fields.fields_carrying(vault_root, page_fields.CARRIES_PROJECT_KEY):
        return ()

    candidates = page_fields.carried_keys(value)
    if not candidates:
        return ()

    try:
        plan = project_keys.plan_project_keys(
            vault_root, candidates, folders=dict(folders) if folders else None
        )
    except project_keys.ProjectKeyTypoError as error:
        raise SetFrontmatterError(
            code="PROJECT_KEY_TYPO", reason=str(error)
        ) from error
    except ValueError:
        # Preserve the legacy escape hatch: invalid values land and are later
        # surfaced by audit rather than mutating the registry.
        return ()
    if plan.writes:
        return plan.writes

    # Keep the auxiliary target/content digest stable across an exact retry
    # after a newly introduced key was written but before the primary page.
    registry_path = kb_root(vault_root) / "_Schema" / "project-keys.yaml"
    try:
        current, guard = read_guarded_text(vault_root, registry_path)
    except FileNotFoundError:
        return ()
    return (PlannedWrite(registry_path, current, guard=guard),)


def _governed_enum_value(
    field: str, value: Any, *, page_type: Any, page: tuple[Path, str, dict[str, Any]]
) -> Any:
    """Return the canonical value, or refuse a governed field written wrongly.

    The policy itself lives in `vault.governed_frontmatter_reason`, beside the
    excluded-field policy, so this boundary and `create_file` enforce one rule
    rather than two that can drift. This wrapper adds only the thing a
    field-setter needs and a creation path does not: normalizing an accepted
    spelling on the way in, so a file never carries two spellings of one state.
    """
    reason = governed_frontmatter_reason(field, value, page_type, page=page)
    if reason is not None:
        raise SetFrontmatterError(code="INVALID_OUTCOME", reason=reason)
    if field.strip().casefold() == "outcome":
        return str(value).strip().casefold()
    return value


def _read_yaml_field(fm_text: str, field: str) -> Any:
    """Best-effort: parse the frontmatter block and return field's value (or None)."""
    import yaml
    try:
        parsed = yaml.safe_load(fm_text) or {}
        if isinstance(parsed, dict):
            return parsed.get(field)
    except yaml.YAMLError:
        return None
    return None


def _remove_yaml_key(fm_text: str, key: str) -> str:
    """Remove `key: <inline>` line OR `key:\\n  - item\\n  - item` block.

    Copied from edit.py to keep this module self-contained.
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
            rest = line[len(key_prefix):].strip()
            if rest == "":
                in_block = True
                continue
            continue
        out.append(line)
    return "\n".join(out)
