"""set_take: fill a `[take: ]` opinion row by its natural leading text.

Filling blank `[take: ]` rows in an ever-living taste note is the single most
repeated edit. Doing it through surgical `edit` means the caller must first
fetch the whole (~tens-of-KB) note just to copy an exact byte-match string —
a big *client-token* read for a one-line write. But the server already has the
file on disk for free. So `set_take` does the locating server-side: give it the
row's natural leading text (`row_key`, e.g. "Whiplash (2014)") and it finds the
one fillable row, then delegates to `edit`'s surgical core — inheriting
atomicity, the `updated:` bump, the single log entry, and embedding re-sync.

Markdown stays the single source of truth: no row IDs, no sidecar. `row_key` is
matched against the text already in the row.

The `[take:` field is the only film/opinion-specific piece (see FIELD / the
regexes below); a future `set_field(field=...)` would parametrize it.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

from . import edit as edit_module
from .edit import EditError, load_editable
from .vault import _mask_code_spans


FIELD = "take"
# An empty placeholder: `[take: ]` or `[take:]`.
_FIELD_EMPTY_RE = re.compile(rf"\[{FIELD}:\s*\]")
# Any take field, capturing the current value ("" when unfilled).
_FIELD_RE = re.compile(rf"\[{FIELD}:\s*([^\]]*?)\s*\]")
# Markdown list item (allowing leading indent + `-` or `*` bullet).
_LIST_ITEM_RE = re.compile(r"^\s*[-*]\s")


@dataclass
class SetTakeResult:
    path: str            # vault-relative, with .md
    row: str             # the row after filling (for caller confirmation)
    warnings: list[str]

    def as_dict(self) -> dict:
        return {"path": self.path, "row": self.row, "warnings": self.warnings}


@dataclass
class SetTakeError(Exception):
    code: str
    candidates: list[str]  # matched rows when AMBIGUOUS_ROW; else []
    reason: str

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "candidates": self.candidates,
            "reason": self.reason,
        }


def _normalize(s: str) -> str:
    """Fold for comparison only: lowercase, em/en-dash → hyphen, collapse space."""
    s = s.lower().replace("—", "-").replace("–", "-")
    return re.sub(r"\s+", " ", s).strip()


def set_take(
    vault_root: Path,
    *,
    path: str,
    row_key: str,
    take: str,
    why: str,
    overwrite: bool = False,
    today: dt.date | None = None,
) -> SetTakeResult:
    """Fill the `[take: ]` of the unique row whose item text matches `row_key`.

    By default only fills an EMPTY `[take: ]`; pass `overwrite=True` to replace
    a filled one. Raises ROW_NOT_FOUND (no fillable match), AMBIGUOUS_ROW
    (matches more than one — candidates listed), or any `edit` error
    (INVALID_EDIT for Sources/Evidence, NOT_FOUND, ALREADY_SUPERSEDED, …).
    """
    if not row_key or not row_key.strip():
        raise SetTakeError(code="INVALID_ARG", candidates=[], reason="row_key is empty")
    if not take or not take.strip():
        raise SetTakeError(code="INVALID_ARG", candidates=[], reason="take is empty")

    # Read + guard server-side (caller doesn't re-send the body).
    try:
        editable = load_editable(vault_root, path)
    except EditError as e:
        raise SetTakeError(code=e.code, candidates=[], reason=e.reason) from e

    body = editable.body
    masked = _mask_code_spans(body)
    key_norm = _normalize(row_key)

    lines = body.split("\n")
    masked_lines = masked.split("\n")

    # candidates: (original_line, existing_take) for list items carrying a take
    # field (outside code) whose pre-field head matches row_key.
    candidates: list[tuple[str, str]] = []
    for line, mline in zip(lines, masked_lines):
        if not _LIST_ITEM_RE.match(line):
            continue
        field_in_code = _FIELD_RE.search(mline)  # masked → None if inside code
        if field_in_code is None:
            continue
        head = line[: field_in_code.start()]  # offsets align: masked is same length
        if key_norm not in _normalize(head):
            continue
        existing = (_FIELD_RE.search(line) or field_in_code).group(1)
        candidates.append((line, existing))

    fillable = (
        candidates if overwrite else [c for c in candidates if c[1] == ""]
    )

    if not fillable:
        filled = [c for c in candidates if c[1] != ""]
        if filled and not overwrite:
            reason = (
                f"no UNFILLED `[{FIELD}: ]` row matching '{row_key}' in "
                f"{editable.rel_path}; {len(filled)} matching row(s) already have "
                "a take — pass overwrite=True to replace."
            )
        else:
            reason = (
                f"no `[{FIELD}: ]` row whose item text matches '{row_key}' in "
                f"{editable.rel_path}."
            )
        raise SetTakeError(code="ROW_NOT_FOUND", candidates=[], reason=reason)

    if len(fillable) > 1:
        raise SetTakeError(
            code="AMBIGUOUS_ROW",
            candidates=[c[0] for c in fillable],
            reason=(
                f"row_key '{row_key}' matches {len(fillable)} fillable rows in "
                f"{editable.rel_path}; refine it. Candidates listed."
            ),
        )

    target_line = fillable[0][0]
    take_clean = take.strip()
    new_line = _FIELD_RE.sub(lambda _m: f"[{FIELD}: {take_clean}]", target_line, count=1)

    # Delegate the actual write to edit's surgical core. `target_line` is unique
    # among take-rows matching row_key (else AMBIGUOUS_ROW above); edit's own
    # count check is the backstop.
    try:
        result = edit_module.edit(
            vault_root,
            path=path,
            why=why,
            old_string=target_line,
            new_string=new_line,
            today=today,
        )
    except EditError as e:
        raise SetTakeError(code=e.code, candidates=[], reason=e.reason) from e

    return SetTakeResult(path=result.path, row=new_line, warnings=result.warnings)
