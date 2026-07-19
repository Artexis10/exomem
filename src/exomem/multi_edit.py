"""multi_edit: several surgical replaces against one page in a single commit.

Token-cheap like `edit`'s surgical mode, but batches N old/new pairs into ONE
atomic write → one embedding re-sync → one log entry → one `updated:` bump.

Pairs apply sequentially in memory — pair K matches the result of pair K-1
(Claude Code MultiEdit semantics). Any pair that fails to match (or matches
ambiguously) raises BEFORE the write, so nothing partial lands: fix that pair
and resend the whole list.

This is deliberately a thin orchestrator over `edit`'s shared helpers
(`load_editable`, `apply_surgical_replace`, `commit_edit`) — it must NOT call
`edit()` in a loop, which would produce N commits / N log entries / N embedding
re-syncs and defeat the entire point.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from pydantic import BeforeValidator

from . import guards, semantic_writes
from .edit import (
    EditError,
    _set_or_append,
    apply_surgical_replace,
    commit_edit,
    load_editable,
)
from .vault import content_hash

_INVALID_EDIT_ITEM = "__exomem_invalid_edit_item__"


def normalize_edit_item(item: object) -> dict:
    """Decode one connector-encoded object while retaining INVALID_EDIT routing."""
    if isinstance(item, dict):
        return item
    if isinstance(item, str):
        try:
            decoded = json.loads(item)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict):
            return decoded
    return {_INVALID_EDIT_ITEM: item}


EditItem = Annotated[
    dict,
    BeforeValidator(normalize_edit_item, json_schema_input_type=dict),
]


@dataclass
class MultiEditResult:
    path: str             # vault-relative, with .md
    edits_applied: int
    warnings: list[str]
    semantic: dict | None = None

    def as_dict(self) -> dict:
        value = {
            "path": self.path,
            "edits_applied": self.edits_applied,
            "warnings": self.warnings,
        }
        if self.semantic is not None:
            value["semantic"] = self.semantic
        return value


@dataclass
class MultiEditValidation:
    """Preview returned by multi_edit(validate_only=True) — no write performed."""

    path: str
    validate_only: bool  # always True
    edits: list[dict]    # [{index, match_count, replace_all}] against evolving body
    semantic: dict | None = None

    def as_dict(self) -> dict:
        value = {
            "path": self.path,
            "validate_only": self.validate_only,
            "edits": self.edits,
        }
        if self.semantic is not None:
            value["semantic"] = self.semantic
        return value


def multi_edit(
    vault_root: Path,
    *,
    path: str,
    why: str,
    edits: list[dict],
    expected_hash: str | None = None,
    validate_only: bool = False,
    today: dt.date | None = None,
    semantic_transition_token: str | None = None,
    relation_disposition: str | None = None,
    relation_review_hash: str | None = None,
    relation_review_reason: str | None = None,
) -> MultiEditResult | MultiEditValidation:
    """Apply a list of surgical {old_string, new_string, replace_all?} pairs.

    All pairs land in ONE commit (or none, on failure). Reuses every `edit`
    guard via `load_editable` (append-only refusal, NOT_FOUND, superseded,
    `expected_hash` drift guard, frontmatter-required).
    """
    normalized_edits = [normalize_edit_item(item) for item in edits]
    for item in normalized_edits:
        guards.guard_text_content(
            item.get("new_string"), tool="edit_memory", field="edits[].new_string"
        )

    # ---- argument validation ----
    missing: list[str] = []
    reasons: list[str] = []
    if not why or not why.strip():
        missing.append("why")
        reasons.append("why is required — edits without rationale aren't auditable")
    if not normalized_edits:
        missing.append("edits")
        reasons.append(
            "edits is empty — supply at least one {old_string, new_string} pair"
        )
    else:
        for i, e in enumerate(normalized_edits):
            if (
                not isinstance(e, dict)
                or "old_string" not in e
                or "new_string" not in e
            ):
                missing.append(f"edits[{i}]")
                reasons.append(
                    f"edit #{i} must be an object with old_string and new_string"
                )
            elif e["old_string"] == e["new_string"]:
                missing.append(f"edits[{i}]")
                reasons.append(f"edit #{i} is a no-op (new_string equals old_string)")
    if missing:
        raise EditError(
            code="INVALID_EDIT", missing=missing, reason="; ".join(reasons)
        )

    edits = normalized_edits

    today = today or dt.date.today()
    date_iso = today.isoformat()

    editable = load_editable(vault_root, path, expected_hash=expected_hash)

    # ---- validate-only: per-pair counts against the evolving body, no write ----
    if validate_only:
        work = editable.body
        previews: list[dict] = []
        fully_renderable = True
        for i, e in enumerate(edits):
            old = e["old_string"]
            new = e["new_string"]
            ra = bool(e.get("replace_all", False))
            count = work.count(old)
            previews.append({"index": i, "match_count": count, "replace_all": ra})
            if count == 0 or (count > 1 and not ra):
                fully_renderable = False
            # Apply (raw — normalization is irrelevant to a count preview) so
            # later pairs see realistic state.
            if count >= 1:
                work = work.replace(old, new, -1 if ra else 1)
        semantic: dict | None = None
        if fully_renderable:
            from . import find as find_module

            resolver = find_module.writer_resolver_snapshot(vault_root)
            rendered = editable.body
            try:
                for i, e in enumerate(edits):
                    rendered, _ = apply_surgical_replace(
                        rendered,
                        e["old_string"],
                        e["new_string"],
                        bool(e.get("replace_all", False)),
                        vault_root,
                        rel_path=editable.rel_path,
                        resolver=resolver,
                        pair_index=i,
                    )
            except EditError:
                semantic = None
            else:
                fm_text = _set_or_append(editable.fm_text, "updated", date_iso)
                rendered = rendered.rstrip() + "\n"
                proposed = f"---\n{fm_text}\n---\n{rendered}"
                semantic = semantic_writes.preflight_existing(
                    vault_root,
                    path=editable.rel_path,
                    after_source=proposed,
                    operation="edit",
                    expected_before_hash=content_hash(editable.original_text),
                    transition_token=semantic_transition_token,
                    relation_disposition=relation_disposition,
                    relation_review_hash=relation_review_hash,
                    relation_review_reason=relation_review_reason,
                ).as_dict()
        return MultiEditValidation(
            path=editable.rel_path,
            validate_only=True,
            edits=previews,
            semantic=semantic,
        )

    # ---- apply sequentially in memory; any failure raises before the write ----
    from . import find as find_module
    resolver = find_module.writer_resolver_snapshot(vault_root)
    body = editable.body
    warnings: list[str] = []
    for i, e in enumerate(edits):
        body, w = apply_surgical_replace(
            body,
            e["old_string"],
            e["new_string"],
            bool(e.get("replace_all", False)),
            vault_root,
            rel_path=editable.rel_path,
            resolver=resolver,
            pair_index=i,
        )
        warnings.extend(w)

    # ---- ONE commit: updated: bump + body + index refresh + one log entry ----
    fm_text = _set_or_append(editable.fm_text, "updated", date_iso)
    new_body_final = body.rstrip() + "\n"
    new_text = f"---\n{fm_text}\n---\n{new_body_final}"
    committed = commit_edit(
        vault_root,
        abs_path=editable.abs_path,
        rel_path=editable.rel_path,
        new_text=new_text,
        date_iso=date_iso,
        why=why,
        changed=[f"body ({len(edits)} surgical edits)"],
        op="multi_edit",
        extra_warnings=warnings,
        expected_before_hash=content_hash(editable.original_text),
        semantic_transition_token=semantic_transition_token,
        relation_disposition=relation_disposition,
        relation_review_hash=relation_review_hash,
        relation_review_reason=relation_review_reason,
    )
    return MultiEditResult(
        path=editable.rel_path,
        edits_applied=len(edits),
        warnings=committed.warnings,
        semantic=committed.semantic,
    )
