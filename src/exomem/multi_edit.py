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
from dataclasses import dataclass
from pathlib import Path

from .edit import (
    EditError,
    _set_or_append,
    apply_surgical_replace,
    commit_edit,
    load_editable,
)
from .vault import WikilinkResolver


@dataclass
class MultiEditResult:
    path: str             # vault-relative, with .md
    edits_applied: int
    warnings: list[str]

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "edits_applied": self.edits_applied,
            "warnings": self.warnings,
        }


@dataclass
class MultiEditValidation:
    """Preview returned by multi_edit(validate_only=True) — no write performed."""

    path: str
    validate_only: bool  # always True
    edits: list[dict]    # [{index, match_count, replace_all}] against evolving body

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "validate_only": self.validate_only,
            "edits": self.edits,
        }


def multi_edit(
    vault_root: Path,
    *,
    path: str,
    why: str,
    edits: list[dict],
    expected_hash: str | None = None,
    validate_only: bool = False,
    today: dt.date | None = None,
) -> MultiEditResult | MultiEditValidation:
    """Apply a list of surgical {old_string, new_string, replace_all?} pairs.

    All pairs land in ONE commit (or none, on failure). Reuses every `edit`
    guard via `load_editable` (append-only refusal, NOT_FOUND, superseded,
    `expected_hash` drift guard, frontmatter-required).
    """
    # ---- argument validation ----
    missing: list[str] = []
    reasons: list[str] = []
    if not why or not why.strip():
        missing.append("why")
        reasons.append("why is required — edits without rationale aren't auditable")
    if not edits:
        missing.append("edits")
        reasons.append(
            "edits is empty — supply at least one {old_string, new_string} pair"
        )
    else:
        for i, e in enumerate(edits):
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

    today = today or dt.date.today()
    date_iso = today.isoformat()

    editable = load_editable(vault_root, path, expected_hash=expected_hash)

    # ---- validate-only: per-pair counts against the evolving body, no write ----
    if validate_only:
        work = editable.body
        previews: list[dict] = []
        for i, e in enumerate(edits):
            old = e["old_string"]
            new = e["new_string"]
            ra = bool(e.get("replace_all", False))
            count = work.count(old)
            previews.append({"index": i, "match_count": count, "replace_all": ra})
            # Apply (raw — normalization is irrelevant to a count preview) so
            # later pairs see realistic state.
            if count >= 1:
                work = work.replace(old, new, -1 if ra else 1)
        return MultiEditValidation(
            path=editable.rel_path, validate_only=True, edits=previews
        )

    # ---- apply sequentially in memory; any failure raises before the write ----
    resolver = WikilinkResolver(vault_root)
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
    warnings = commit_edit(
        vault_root,
        abs_path=editable.abs_path,
        rel_path=editable.rel_path,
        new_text=new_text,
        date_iso=date_iso,
        why=why,
        changed=[f"body ({len(edits)} surgical edits)"],
        op="multi_edit",
        extra_warnings=warnings,
    )
    return MultiEditResult(
        path=editable.rel_path, edits_applied=len(edits), warnings=warnings
    )
