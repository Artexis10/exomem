"""Classify a page: record which sensitivity classes it holds, and nothing else.

A class is metadata. This operation writes the page's `sensitivity` field through the governed
pointer write and no other byte, so a captured Source or Evidence page keeps its body, its path
and every other frontmatter line. It is the only operation that writes the field on a page in an
append-only tree (`page-fields` names it the field's `classify` writer). The page never moves.

One reason record lets a revert return the exact earlier bytes. An empty list clears the field to
`[]` rather than deleting it, because a pointer write that removes a field cannot be reverted.
The page then reads as unclassified.

A restricted caller meets an existing page it may not see exactly as an absent one: the same
refusal as a missing path, never a different one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import page_fields, sensitivity_classes, set_frontmatter_field
from .vault import VaultPathError, parse_frontmatter, read_guarded_text, resolve_under_vault


@dataclass
class ClassifyError(Exception):
    code: str
    reason: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code}: {self.reason}"


def _held(front: dict[str, Any]) -> tuple[str, ...]:
    value = front.get(sensitivity_classes.FIELD)
    return tuple(item for item in value if isinstance(item, str)) if isinstance(value, list) else ()


def classify(
    vault_root: Path,
    *,
    path: str,
    reason: str | None,
    sensitivity: list[str] | None = None,
    revert_record: str | None = None,
    expected_hash: str | None = None,
    validate_only: bool = False,
) -> dict[str, Any]:
    """Set a page's classes, or revert an earlier classification, and report the result.

    Give `sensitivity` (a list of registered class keys; `[]` clears) or `revert_record` (the
    `pointer_write` id of an earlier classification of this page), not both. `reason` is required
    because a classification restates a judgement and its record explains it later. With
    `validate_only` nothing is written and the result is the preview.
    """
    root = Path(vault_root)
    why = " ".join((reason or "").split())
    if not why:
        raise ClassifyError(
            "REASON_REQUIRED",
            "classifying a captured page records a judgement; supply `reason` naming why it holds these classes.",
        )
    if (sensitivity is None) == (revert_record is None):
        raise ClassifyError(
            "INVALID_CLASSIFY", "give `sensitivity` (a list of class keys, `[]` to clear) or `revert_record`, not both."
        )
    try:
        # A page the caller may not see is answered exactly as a missing one.
        _absolute, rel = resolve_under_vault(
            root, path, must_exist=True, must_be_file=True, refuse_withheld=True
        )
        classes = (
            sensitivity_classes.validated(root, sensitivity) if sensitivity is not None else ()
        )
    except VaultPathError as error:
        raise ClassifyError(error.code, error.reason) from None
    except sensitivity_classes.SensitivityError as error:
        raise ClassifyError(error.code, error.reason) from None
    try:
        text, _guard = read_guarded_text(root, root / rel)
    except (OSError, UnicodeError):
        raise ClassifyError("UNREADABLE", f"could not safely read {rel}") from None
    previous = _held(parse_frontmatter(text)[0])
    field = sensitivity_classes.FIELD
    try:
        if revert_record is not None:
            written = set_frontmatter_field.revert_pointer_field(
                root, path=rel, record_id=revert_record, fields={field}, why=why,
                expected_hash=expected_hash, validate_only=validate_only,
            )
        elif classes == previous:
            return _report(root, rel, previous, previous, changed=False)
        else:
            written = set_frontmatter_field.set_pointer_field(
                root, path=rel, field=field, value=list(classes), why=why,
                expected_hash=expected_hash, validate_only=validate_only,
                writer=page_fields.WRITER_CLASSIFY,
            )
    except set_frontmatter_field.SetFrontmatterError as error:
        raise ClassifyError(error.code, error.reason) from None
    now = written.new_value if isinstance(written.new_value, list) else []
    return _report(
        root, rel, previous, tuple(now), changed=True, validate_only=validate_only,
        record=None if validate_only else written.pointer_write, semantic=written.semantic,
    )


def _report(
    root: Path,
    rel: str,
    previous: tuple[str, ...],
    now: tuple[str, ...],
    *,
    changed: bool,
    validate_only: bool = False,
    record: str | None = None,
    semantic: dict | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": rel,
        "changed": changed,
        "previous_classes": list(previous),
        "sensitivity": sensitivity_classes.report(
            root,
            sensitivity_classes.Classification(
                now, sensitivity_classes.BASIS_DECLARED if now else sensitivity_classes.BASIS_NONE
            ),
            rel_path=rel,
        ),
    }
    if validate_only:
        result["validate_only"] = True
    if record is not None:
        result["pointer_write"] = record
        result["revert_with"] = {"operation": "classify", "path": rel, "revert_record": record}
    if semantic is not None:
        result["semantic"] = semantic
    return result
