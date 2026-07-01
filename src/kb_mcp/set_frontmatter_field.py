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

from .vault import (
    PlannedWrite,
    VaultPathError,
    _format_yaml_line,
    batch_atomic_write,
    in_append_only_tree,
    in_curated_tree,
    resolve_under_vault,
    write_log_entry,
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

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "field": self.field,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "warnings": self.warnings,
        }


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
) -> SetFrontmatterResult:
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

    # Project-key guard: route project/projects values through the same
    # auto-register + typo-distance check that note() uses. Without this,
    # a typo via this surgical op silently lands in frontmatter and the
    # registry stays clean while the page is broken.
    _autoregister_project_keys_or_typo_block(vault_root, field, value)

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

    text = abs_path.read_text(encoding="utf-8")
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

    warnings: list[str] = []
    try:
        batch_atomic_write(
            [PlannedWrite(path=abs_path, content=new_text)],
            vault_root=vault_root,
        )
    except Exception as e:
        log.exception("set_frontmatter_field write failed for %s", rel_path)
        warnings.append(f"partial write — reconcile on desktop: {e}")
        raise

    rel_no_ext = rel_path.removesuffix(".md") if rel_path.endswith(".md") else rel_path
    log_body = (
        f"set_frontmatter_field via exomem. {why.strip()} "
        f"Field: {field!r}. Old: {old_value!r}. New: {value!r}."
    )
    if curated and allow_curated:
        log_body += f" allow_curated=true (target tree: {curated})."
    log_warning = write_log_entry(
        vault_root,
        date_iso=date_iso,
        op="set_frontmatter_field",
        rel_path_no_ext=rel_no_ext,
        body=log_body,
    )
    if log_warning:
        warnings.append(log_warning)

    return SetFrontmatterResult(
        path=rel_path,
        field=field,
        old_value=old_value,
        new_value=value,
        warnings=warnings,
    )


def _autoregister_project_keys_or_typo_block(
    vault_root: Path, field: str, value: Any
) -> None:
    """If the patched field is project/projects, validate via the registry.

    Mirrors note.py's behaviour: slug-shaped new keys auto-register,
    Levenshtein-close keys raise as PROJECT_KEY_TYPO so the agent self-
    corrects. Non-project fields are unaffected.
    """
    if field not in ("project", "projects"):
        return
    from . import project_keys as project_keys_module

    if field == "project":
        candidates: list[str] = [value] if isinstance(value, str) and value else []
    else:
        candidates = [v for v in (value or []) if isinstance(v, str) and v]

    if not candidates:
        return

    registry = project_keys_module.load_project_registry(vault_root)
    for cand in candidates:
        if cand in registry.project_to_folder:
            continue
        try:
            project_keys_module.register_project_key(vault_root, cand)
        except project_keys_module.ProjectKeyTypoError as e:
            raise SetFrontmatterError(
                code="PROJECT_KEY_TYPO", reason=str(e)
            ) from e
        except ValueError:
            # Invalid slug — leave as-is; the frontmatter value will land
            # but downstream audit will flag it via unregistered_project_key.
            pass


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
