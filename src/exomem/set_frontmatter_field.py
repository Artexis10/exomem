"""The `set_frontmatter_field` Tier 2 op: surgical edit of one frontmatter key.

For when `edit` is overkill (it rewrites whole body or tags). This patches
exactly one key, leaves the body alone, and always bumps `updated:`.

Refuses Sources/ and Evidence/. Curated trees need `allow_curated=true`.
`why` is required — lands in the log entry.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import project_keys, semantic_writes
from .vault import (
    PlannedWrite,
    VaultPathError,
    _format_yaml_line,
    content_hash,
    excluded_frontmatter_reason,
    in_append_only_tree,
    in_curated_tree,
    kb_root,
    plan_log_writes,
    read_guarded_text,
    resolve_under_vault,
)

log = logging.getLogger(__name__)

_FM_PATTERN = re.compile(r"^---\n(.*?)\n---\n(.*)", re.DOTALL)


@dataclass
class SetFrontmatterResult:
    path: str
    field: str
    old_value: Any
    new_value: Any
    warnings: list[str]
    semantic: dict | None = None

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
    today: dt.date | None = None,
    validate_only: bool = False,
    semantic_transition_token: str | None = None,
    relation_disposition: str | None = None,
    relation_review_hash: str | None = None,
    relation_review_reason: str | None = None,
) -> SetFrontmatterResult | SetFrontmatterValidation:
    if not field or not field.strip():
        raise SetFrontmatterError(
            code="INVALID_SET", reason="field is required"
        )
    if not why or not why.strip():
        raise SetFrontmatterError(
            code="INVALID_SET",
            reason="why is required — frontmatter edits without rationale aren't auditable",
        )
    if field == "updated":
        raise SetFrontmatterError(
            code="INVALID_SET",
            reason="cannot set `updated:` directly — it's always bumped to today by this op",
        )
    excluded_reason = excluded_frontmatter_reason(field)
    if excluded_reason is not None:
        raise SetFrontmatterError(code="EXCLUDED_FIELD", reason=excluded_reason)

    try:
        abs_path, rel_path = resolve_under_vault(
            vault_root, path, must_exist=True, must_be_file=True
        )
    except VaultPathError as e:
        raise SetFrontmatterError(code=e.code, reason=e.reason) from e

    append_only = in_append_only_tree(rel_path)
    if append_only:
        raise SetFrontmatterError(
            code="APPEND_ONLY",
            reason=(
                f"{rel_path} is in {append_only}/ which is append-only. "
                f"Frontmatter edits would violate rule 2."
            ),
        )

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
        text, _ = read_guarded_text(vault_root, abs_path)
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
            code="UNREADABLE",
            reason=(
                f"{rel_path} has no frontmatter delimiters; this op refuses "
                f"to synthesize them. Use `create_file` for new files."
            ),
        )
    fm_text = m.group(1)
    body = m.group(2)

    today = today or dt.date.today()
    date_iso = today.isoformat()

    old_value = _read_yaml_field(fm_text, field)
    fm_text = _remove_yaml_key(fm_text, field)
    new_line = _format_yaml_line(field, value)
    fm_text = fm_text.rstrip() + "\n" + new_line

    # Always bump updated:
    fm_text = _remove_yaml_key(fm_text, "updated")
    fm_text = fm_text.rstrip() + f"\nupdated: {date_iso}"

    new_text = f"---\n{fm_text}\n---\n{body}"

    project_plan = _plan_project_keys(vault_root, field, value)
    try:
        preflight = semantic_writes.preflight_existing(
            vault_root,
            path=rel_path,
            after_source=new_text,
            operation="edit",
            expected_before_hash=content_hash(text),
            transition_token=semantic_transition_token,
            relation_disposition=relation_disposition,
            relation_review_hash=relation_review_hash,
            relation_review_reason=relation_review_reason,
        )
    except semantic_writes.SemanticWriteError as error:
        raise SetFrontmatterError(error.code, error.reason) from error

    if validate_only:
        return SetFrontmatterValidation(
            path=rel_path,
            field=field,
            old_value=old_value,
            new_value=value,
            validate_only=True,
            semantic=preflight.as_dict(),
        )

    warnings: list[str] = []
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

    auxiliaries = [*project_plan, *log_plan.writes]
    try:
        committed = semantic_writes.commit_existing(
            vault_root,
            preflight=preflight,
            auxiliary_writes=tuple(auxiliaries),
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


def _plan_project_keys(
    vault_root: Path, field: str, value: Any
) -> tuple[PlannedWrite, ...]:
    """If the patched field is project/projects, validate via the registry.

    Mirrors note.py's behaviour: slug-shaped new keys auto-register,
    Levenshtein-close keys raise as PROJECT_KEY_TYPO so the agent self-
    corrects. Non-project fields are unaffected.
    """
    if field not in ("project", "projects"):
        return ()

    if field == "project":
        candidates: list[str] = [value] if isinstance(value, str) and value else []
    else:
        candidates = (
            [v for v in value if isinstance(v, str) and v]
            if isinstance(value, list)
            else []
        )

    if not candidates:
        return ()

    try:
        plan = project_keys.plan_project_keys(vault_root, candidates)
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
