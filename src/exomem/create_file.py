"""The `create_file` Tier 2 op: write a file at an arbitrary vault path.

Tier 2 escape hatch. Use when the file doesn't fit a Tier 1 typed-note
shape — new folder structures (`Identity/`, `Templates/`), skill files,
config, scratch. For typed notes, use `note`/`add`/`link`/`preserve`.

Refuses by default:
- Sources/ and Evidence/ (append-only — use `add` / `preserve`).
- Any subtree marked `readonly`/`excluded` in `Knowledge Base/_access.yaml`
  (curated, read-only material) — a hard refusal with no override.

If `frontmatter` is supplied, it's prepended to `content` as a YAML block
with `created`/`updated` filled to today (unless caller specified them).
Otherwise `content` is written verbatim — caller is responsible for any
frontmatter in the body.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import access, indexes, memory_refs, relation_review, semantic_writes
from . import vault as vault_module
from .vault import (
    PlannedWrite,
    VaultPathError,
    batch_atomic_write,
    excluded_frontmatter_reason,
    in_append_only_tree,
    in_curated_tree,
    kb_root,
    normalize_body_wikilinks,
    plan_log_writes,
    read_guarded_text,
    resolve_under_vault,
    serialize_frontmatter,
)

log = logging.getLogger(__name__)


@dataclass
class CreateFileResult:
    path: str
    warnings: list[str]
    creation: dict | None = None

    def as_dict(self) -> dict:
        value = {"path": self.path, "warnings": self.warnings}
        if self.creation is not None:
            value["creation"] = self.creation
        return value


@dataclass
class CreateFileError(Exception):
    code: str
    reason: str

    def as_dict(self) -> dict:
        return {"code": self.code, "reason": self.reason}


def create_file(
    vault_root: Path,
    *,
    path: str,
    content: str,
    frontmatter: dict[str, Any] | None = None,
    overwrite: bool = False,
    allow_curated: bool = False,
    today: dt.date | None = None,
    validate_only: bool = False,
    draft_id: str | None = None,
    draft_hash: str | None = None,
    draft_token: str | None = None,
    relation_disposition: str | None = None,
    relation_review_hash: str | None = None,
    relation_review_reason: str | None = None,
) -> CreateFileResult | semantic_writes.CreationPreflight:
    if frontmatter:
        for key in frontmatter:
            reason = excluded_frontmatter_reason(str(key))
            if reason is not None:
                raise CreateFileError(code="EXCLUDED_FIELD", reason=reason)

    try:
        abs_path, rel_path = resolve_under_vault(
            vault_root, path, must_be_under_kb=True
        )
    except VaultPathError as e:
        raise CreateFileError(code=e.code, reason=e.reason) from e

    append_only = in_append_only_tree(rel_path)
    if append_only:
        raise CreateFileError(
            code="APPEND_ONLY",
            reason=(
                f"{rel_path} is in {append_only}/ which is append-only. "
                f"Use `add` for sources or `preserve` for evidence."
            ),
        )

    # Read-only protection: a subtree marked `readonly`/`excluded` in
    # _access.yaml is a hard refusal (no override). This replaces the old
    # hardcoded curated-tree list (now empty) — see `vault.CURATED_TREES`.
    access_reason = access.writable_reason(vault_root, rel_path)
    if access_reason is not None:
        raise CreateFileError(code="READONLY_PROTECTED", reason=access_reason)

    curated = in_curated_tree(rel_path)
    if curated and not allow_curated:
        raise CreateFileError(
            code="CURATED_PROTECTED",
            reason=(
                f"{rel_path} is in curated tree {curated!r} (desk-managed). "
                f"Pass `allow_curated=true` only if you are genuinely "
                f"building infrastructure inside this tree."
            ),
        )

    existing_text: str | None = None
    if abs_path.exists():
        if not overwrite:
            raise CreateFileError(
                code="FILE_EXISTS",
                reason=(
                    f"{rel_path} already exists. Pass `overwrite=true` to "
                    f"replace, or use `edit` / `set_frontmatter_field` / "
                    f"`append_to_file` for surgical changes."
                ),
            )
        if not abs_path.is_file():
            raise CreateFileError(
                code="NOT_A_FILE",
                reason=f"{rel_path} exists but is not a regular file",
            )
        if rel_path.casefold().endswith(".md"):
            existing_text = abs_path.read_text(encoding="utf-8")

    is_markdown = rel_path.casefold().endswith(".md")
    today = today or dt.date.today()
    date_iso = today.isoformat()
    if draft_token is not None and is_markdown and not overwrite:
        try:
            token_value = semantic_writes.DraftToken.decode(draft_token)
        except semantic_writes.SemanticWriteError as error:
            raise CreateFileError(error.code, error.reason) from error
        if (
            token_value.writer != "create_file"
            or token_value.operation != "tier2_create"
            or token_value.destination != rel_path
            or token_value.registrations
        ):
            raise CreateFileError(
                "INVALID_DRAFT_TOKEN", "draft token does not match this creation"
            )
        date_iso = token_value.render_date

    # For markdown files, normalize wikilinks in the body to canonical form.
    # Skip non-md files (skill manifests, JSON, scratch) — their `[[...]]`
    # patterns may not be Obsidian wikilinks.
    warnings: list[str] = []
    if is_markdown:
        from . import find as find_module
        resolver = find_module.writer_resolver_snapshot(vault_root)
        content, body_warnings = normalize_body_wikilinks(
            content, vault_root, resolver=resolver
        )
        warnings.extend(body_warnings)

    if frontmatter is not None:
        fm = dict(frontmatter)
        fm.setdefault("created", date_iso)
        fm.setdefault("updated", date_iso)
        fm_block = serialize_frontmatter(fm)
        body = content if content.endswith("\n") else content + "\n"
        full_text = f"---\n{fm_block}\n---\n{body}"
    else:
        full_text = content

    def _semantic_family(text: str | None) -> str | None:
        if text is None:
            return None
        try:
            fm, _, _ = vault_module.parse_frontmatter(text, strict=True)
        except ValueError:
            return "semantic"
        page_type = fm.get("type")
        if page_type in {
            "research-note", "insight", "failure", "pattern", "experiment",
            "production-log", "entity",
        }:
            return "semantic"
        return None

    if overwrite and (_semantic_family(existing_text) or _semantic_family(full_text)):
        raise CreateFileError(
            code="SEMANTIC_OVERWRITE_NOT_WIRED",
            reason="semantic Markdown overwrite is deferred until lifecycle wiring is available",
        )

    identity = draft_id
    if is_markdown and not overwrite:
        try:
            fm, _, _ = vault_module.parse_frontmatter(full_text, strict=True)
        except ValueError as error:
            raise CreateFileError(
                "INVALID_FRONTMATTER", "Markdown frontmatter is invalid"
            ) from error
        if fm.get("type") in {
            "research-note", "insight", "failure", "pattern", "experiment", "production-log"
        }:
            try:
                full_text, identity = memory_refs.add_id_to_markdown(
                    full_text, identity or memory_refs.new_id()
                )
            except memory_refs.ReferenceError as error:
                raise CreateFileError(error.code, error.reason) from error

    rel_no_ext = rel_path.removesuffix(".md") if is_markdown else rel_path
    creation_token = (
        draft_token
        or semantic_writes.DraftToken(
            "create_file", "tier2_create", rel_path, date_iso
        ).encode()
        if is_markdown and not overwrite
        else "create-file:" + hashlib.sha256(
            f"{date_iso}\0{rel_path}\0{full_text}".encode()
        ).hexdigest()
    )
    log_body_parts = [f"Created via exomem Tier 2. {len(full_text):,} chars."]
    if frontmatter is not None:
        log_body_parts.append(f"Frontmatter keys: {list(frontmatter.keys())}.")
    if curated and allow_curated:
        log_body_parts.append(f"allow_curated=true (target tree: {curated}).")
    op_word = "create_file (overwrite)" if overwrite else "create_file"
    try:
        log_plan = plan_log_writes(
            vault_root,
            date_iso=date_iso,
            op=op_word,
            rel_path_no_ext=rel_no_ext,
            body=" ".join(log_body_parts),
            operation_token=creation_token,
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise CreateFileError("LOG_PLAN_CONFLICT", str(error)) from error
    if log_plan.warning is not None:
        warnings.append(log_plan.warning)
    if log_plan.rotation_note is not None:
        warnings.append(log_plan.rotation_note)

    creation: dict | None = None
    if is_markdown and not overwrite:
        token = creation_token
        try:
            preflight = semantic_writes.preflight_creation(
                vault_root,
                path=rel_path,
                source=full_text,
                operation="tier2_create",
                writer="create_file",
                draft_id=identity,
                draft_token=token,
            )
        except semantic_writes.SemanticWriteError as error:
            raise CreateFileError(error.code, error.reason) from error
        if draft_hash is not None and preflight.draft_hash != draft_hash:
            raise CreateFileError(
                "DRAFT_HASH_MISMATCH", "draft requires fresh validation"
            )
        if validate_only:
            return preflight
        auxiliary: list[PlannedWrite] = []
        top_index = kb_root(vault_root) / "index.md"
        if top_index.is_file() and preflight.applicability in {"full", "structural"}:
            top_text, top_guard = read_guarded_text(vault_root, top_index)
            new_top, _ = indexes._prepend_recent_activity(
                top_text,
                date_iso=date_iso,
                summary=f"`{rel_no_ext}` (Tier 2 create)",
            )
            sub_writes, counted_top = indexes.compute_subindex_writes(
                vault_root,
                top_index_text=new_top,
                pending_paths=[rel_no_ext],
                include_unchanged=True,
            )
            auxiliary.append(
                PlannedWrite(top_index, counted_top or new_top, guard=top_guard)
            )
            auxiliary.extend(sub_writes)
        auxiliary.extend(log_plan.writes)
        try:
            committed = semantic_writes.commit_creation(
                vault_root,
                preflight=preflight,
                auxiliary_writes=tuple(auxiliary),
                relation_disposition=relation_disposition,
                relation_review_hash=relation_review_hash,
                relation_review_reason=relation_review_reason,
                operation="tier2_create",
            )
        except (
            semantic_writes.SemanticWriteError,
            relation_review.RelationReviewError,
        ) as error:
            raise CreateFileError(error.code, error.reason) from error
        creation = committed.as_dict()
    else:
        writes = list(log_plan.writes)
        writes.append(PlannedWrite(path=abs_path, content=full_text))
        try:
            batch_atomic_write(writes, vault_root=vault_root)
        except Exception as e:
            log.exception("create_file write failed for %s", rel_path)
            warnings.append(f"partial write — reconcile on desktop: {e}")
            raise

    return CreateFileResult(path=rel_path, warnings=warnings, creation=creation)
