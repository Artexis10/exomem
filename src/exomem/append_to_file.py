"""The `append_to_file` Tier 2 op: append text to an existing file.

Refuses Sources/ (immutable post-write). Allowed on Evidence/ sidecars
and general vault files. Curated trees require `allow_curated=true`.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path

from . import semantic_writes
from .vault import (
    PlannedWrite,
    VaultPathError,
    batch_atomic_write,
    content_hash,
    in_append_only_tree,
    in_curated_tree,
    plan_log_writes,
    read_guarded_text,
    resolve_under_vault,
)

log = logging.getLogger(__name__)


@dataclass
class AppendResult:
    path: str
    bytes_appended: int
    warnings: list[str]
    semantic: dict | None = None

    def as_dict(self) -> dict:
        value = {
            "path": self.path,
            "bytes_appended": self.bytes_appended,
            "warnings": self.warnings,
        }
        if self.semantic is not None:
            value["semantic"] = self.semantic
        return value


@dataclass
class AppendError(Exception):
    code: str
    reason: str

    def as_dict(self) -> dict:
        return {"code": self.code, "reason": self.reason}


def append_to_file(
    vault_root: Path,
    *,
    path: str,
    content: str,
    allow_curated: bool = False,
    today: dt.date | None = None,
    validate_only: bool = False,
    semantic_transition_token: str | None = None,
    relation_disposition: str | None = None,
    relation_review_hash: str | None = None,
    relation_review_reason: str | None = None,
) -> AppendResult | semantic_writes.ExistingPreflight:
    if content is None:
        raise AppendError(code="INVALID_APPEND", reason="content is required")

    try:
        abs_path, rel_path = resolve_under_vault(
            vault_root, path, must_exist=True, must_be_file=True, must_be_under_kb=True
        )
    except VaultPathError as e:
        raise AppendError(code=e.code, reason=e.reason) from e

    # Sources/ is fully immutable. Evidence/ allows appends to sidecars and
    # description files (description.md style); the raw artifacts there are
    # binary and wouldn't be markdown-appended anyway.
    # Canonical, case-insensitive match (an uppercase `SOURCES/` aliases the real
    # `Sources/` on a case-insensitive filesystem). Evidence/ appends stay allowed.
    if in_append_only_tree(rel_path) == "Sources":
        raise AppendError(
            code="APPEND_ONLY",
            reason=(
                f"{rel_path} is in Sources/ which is immutable per "
                f"SKILL.md rule 2. Add a corrective source or compile a "
                f"downstream note instead."
            ),
        )

    curated = in_curated_tree(rel_path)
    if curated and not allow_curated:
        raise AppendError(
            code="CURATED_PROTECTED",
            reason=(
                f"{rel_path} is in curated tree {curated!r} (desk-managed). "
                f"Pass `allow_curated=true` to override."
            ),
        )

    semantic_append_target = bool(
        rel_path.casefold().endswith(".md")
        and in_append_only_tree(rel_path) != "Evidence"
    )
    semantic_review_requested = validate_only or any(
        value is not None
        for value in (
            semantic_transition_token,
            relation_disposition,
            relation_review_hash,
            relation_review_reason,
        )
    )
    if semantic_review_requested and not semantic_append_target:
        raise AppendError(
            code="INVALID_APPEND_REVIEW_TARGET",
            reason=(
                "append validation and semantic review fields are supported only "
                "for governed Markdown outside Evidence/"
            ),
        )

    try:
        existing, primary_guard = read_guarded_text(vault_root, abs_path)
    except (OSError, UnicodeError) as error:
        raise AppendError(
            code="UNREADABLE", reason=f"could not safely read {rel_path}"
        ) from error
    token_value = None
    if semantic_transition_token is not None:
        try:
            token_value = semantic_writes._decode_existing_transition_token(
                semantic_transition_token
            )
        except semantic_writes.SemanticWriteError as error:
            raise AppendError(error.code, error.reason) from error
    committed_replay = bool(
        token_value is not None
        and token_value["operation"] == "tier2_append"
        and token_value["path"] == rel_path
        and token_value["after_hash"] == content_hash(existing)
    )
    if committed_replay:
        if content and not existing.endswith(content):
            raise AppendError(
                "LIFECYCLE_TRANSITION_MISMATCH",
                "append retry content does not match the committed transition",
            )
        prefix = existing[: -len(content)] if content else existing
        if token_value is None:
            raise AppendError(
                "LIFECYCLE_TRANSITION_MISMATCH",
                "append retry token is unavailable",
            )
        if content_hash(prefix) == token_value["before_hash"]:
            joiner = ""
        elif prefix.endswith("\n") and content_hash(prefix[:-1]) == token_value[
            "before_hash"
        ]:
            joiner = "\n"
        else:
            raise AppendError(
                "LIFECYCLE_TRANSITION_MISMATCH",
                "append retry cannot reconstruct the exact appended bytes",
            )
        new_text = existing
    else:
        # Ensure a single newline boundary between existing tail and new content.
        joiner = "\n" if existing and not existing.endswith("\n") else ""
        new_text = existing + joiner + content

    today = today or dt.date.today()
    date_iso = today.isoformat()
    rel_no_ext = rel_path.removesuffix(".md") if rel_path.endswith(".md") else rel_path
    bytes_appended = len((joiner + content).encode("utf-8"))
    log_body = f"Appended {bytes_appended:,} bytes via exomem Tier 2."
    if curated and allow_curated:
        log_body += f" allow_curated=true (target tree: {curated})."
    operation_before_hash = (
        token_value["before_hash"] if token_value is not None else content_hash(existing)
    )
    operation_after_hash = (
        token_value["after_hash"] if token_value is not None else content_hash(new_text)
    )
    operation_token = (
        f"tier2-append:{operation_before_hash}:{operation_after_hash}"
    )
    try:
        log_plan = plan_log_writes(
            vault_root,
            date_iso=date_iso,
            op="append_to_file",
            rel_path_no_ext=rel_no_ext,
            body=log_body,
            operation_token=operation_token,
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise AppendError(
            "LOG_PLAN_CONFLICT", "append log update could not be planned safely"
        ) from error

    warnings: list[str] = []
    if log_plan.warning is not None:
        warnings.append(log_plan.warning)
    if log_plan.rotation_note is not None:
        warnings.append(log_plan.rotation_note)

    semantic: dict | None = None
    if semantic_append_target:
        try:
            preflight = semantic_writes.preflight_existing(
                vault_root,
                path=rel_path,
                after_source=new_text,
                operation="tier2_append",
                expected_before_hash=content_hash(existing),
                transition_token=semantic_transition_token,
                relation_disposition=relation_disposition,
                relation_review_hash=relation_review_hash,
                relation_review_reason=relation_review_reason,
            )
        except semantic_writes.SemanticWriteError as error:
            raise AppendError(error.code, error.reason) from error
        if validate_only:
            return preflight
        try:
            committed = semantic_writes.commit_existing(
                vault_root,
                preflight=preflight,
                auxiliary_writes=log_plan.writes,
            )
        except semantic_writes.SemanticWriteError as error:
            raise AppendError(error.code, error.reason) from error
        semantic = committed.as_dict()
    else:
        writes = [*log_plan.writes, PlannedWrite(abs_path, new_text, guard=primary_guard)]
        try:
            batch_atomic_write(writes, vault_root=vault_root)
        except Exception as error:
            log.exception("append_to_file write failed for %s", rel_path)
            warnings.append(f"partial write — reconcile on desktop: {error}")
            raise

    return AppendResult(
        path=rel_path,
        bytes_appended=bytes_appended,
        warnings=warnings,
        semantic=semantic,
    )
