"""Immutable Source corrections, owned receipts, and admitted current-page reads."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from pydantic import ConfigDict, Field, TypeAdapter, ValidationError
from typing_extensions import TypedDict

from . import access, memory_refs, note_types, reserved_paths, source_capture, vault
from .governance import connector_boundary, egress

RECEIPT_MAX_BYTES = 16 * 1024
MAX_HOPS = 32


class SourceCorrection(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid")

    supersedes: Annotated[str, Field(strict=True, min_length=1, max_length=4096)]
    expected_hash: Annotated[str, Field(strict=True, min_length=64, max_length=64)]
    reason: Annotated[str, Field(strict=True, min_length=1, max_length=2048)]


_CORRECTION = TypeAdapter(SourceCorrection)


class SourceCorrectionUnavailable(ValueError):
    def __init__(self) -> None:
        super().__init__("SOURCE_UNAVAILABLE: source is unavailable")


def validate_correction(value: object) -> SourceCorrection:
    try:
        correction = _CORRECTION.validate_python(value)
        source_capture.validate_sha256(correction["expected_hash"])
        if not correction["reason"].strip():
            raise ValueError
    except (ValidationError, ValueError):
        raise ValueError(
            "INVALID_SOURCE_CORRECTION: supply supersedes, its historical page expected_hash, and a nonempty reason"
        ) from None
    return correction


def receipt_root(root: Path) -> Path:
    return vault.kb_root(root) / "_Sources" / "corrections"


def _receipt_path(root: Path, reference: str) -> Path:
    identity = memory_refs.parse_memory_ref(reference)
    if identity is None or memory_refs.memory_ref(identity) != reference:
        raise SourceCorrectionUnavailable
    return receipt_root(root) / f"{identity}.json"


def _path(root: Path, value: str) -> str:
    resolved = memory_refs.resolve_identifier(root, value)
    if not resolved.endswith(".md"):
        resolved += ".md"
    return reserved_paths.resolve_physical_relative(root, resolved)


def _authorizer(root: Path) -> Callable[[str], bool]:
    from . import record_governance

    return record_governance.full_release_filter(root)


def _read_page(root: Path, path: str, authorize: Callable[[str], bool]):
    if not authorize(path):
        raise SourceCorrectionUnavailable
    text, guard = vault.read_guarded_text(root, root / path)
    metadata, _, marker = vault.parse_frontmatter(text, strict=True)
    return text, guard, metadata, marker


def _receipt(root: Path, reference: str) -> tuple[dict | None, vault.PathGuard]:
    target = _receipt_path(root, reference).relative_to(root).as_posix()
    with reserved_paths._subsystem_authority_scope("source_corrections"):
        try:
            (root / target).lstat()
        except FileNotFoundError:
            return None, vault.PathGuard.capture(root, target, leaf_policy="absent")
        raw, guard = vault.read_bounded_guarded_bytes(root, target, limit=RECEIPT_MAX_BYTES)
    entry = json.loads(raw)
    # These keys and the version are the fixed correction receipt schema.
    if (not isinstance(entry, dict) or set(entry) != {
        "version", "old_ref", "new_ref", "new_path", "expected_hash", "reason", "timestamp"
    } or type(entry["version"]) is not int or entry["version"] != 1
        or entry["old_ref"] != reference
        or not isinstance(entry["new_path"], str)
        or memory_refs.parse_memory_ref(entry["new_ref"]) is None
        or memory_refs.memory_ref(memory_refs.parse_memory_ref(entry["new_ref"])) != entry["new_ref"]):
        raise SourceCorrectionUnavailable
    validate_correction({
        "supersedes": entry["old_ref"], "expected_hash": entry["expected_hash"], "reason": entry["reason"]
    })
    if not isinstance(entry["timestamp"], str):
        raise SourceCorrectionUnavailable
    dt.datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
    return entry, guard


def has_correction(root: Path, metadata: dict) -> bool:
    """Inspect only an already-admitted page's deterministic correction slot."""
    identity = memory_refs.normalize_id(metadata.get(memory_refs.ID_FIELD))
    if identity is None:
        return False
    try:
        entry, _guard = _receipt(root, memory_refs.memory_ref(identity))
        return entry is not None
    except (OSError, ValueError, TypeError, KeyError):
        raise SourceCorrectionUnavailable from None


@dataclass(frozen=True, slots=True)
class CurrentSourceResolution:
    requested_path: str
    requested_ref: str | None
    selected_path: str
    selected_ref: str | None
    source: str
    guard: vault.PathGuard
    guards: tuple[vault.PathGuard, ...]

    def as_dict(self) -> dict:
        return {
            "requested_path": self.requested_path,
            "requested_ref": self.requested_ref,
            "selected_path": self.selected_path,
            "selected_ref": self.selected_ref,
            "corrected": self.requested_path != self.selected_path,
        }


def resolve_current(
    root: Path, path: str, *, authorize_path: Callable[[str], bool] | None = None
) -> CurrentSourceResolution:
    """Authorize each page before inspecting its one deterministic receipt."""
    root = Path(root)
    authorize = authorize_path or _authorizer(root)
    try:
        selected = requested = _path(root, path)
        requested_ref = None
        expected_ref = None
        predecessor_ref = None
        seen: set[str] = set()
        guards: list[vault.PathGuard] = []
        basis = note_types.Basis(root)
        for hop in range(MAX_HOPS + 1):
            text, guard, metadata, marker = _read_page(root, selected, authorize)
            reference = memory_refs.ref_from_markdown(text)
            if expected_ref is not None and reference != expected_ref:
                raise SourceCorrectionUnavailable
            if predecessor_ref is not None and metadata.get("supersedes") != predecessor_ref:
                raise SourceCorrectionUnavailable
            if hop == 0:
                requested_ref = reference
            guards.append(guard)
            is_source = marker is not None and basis.selects(metadata.get("type"), note_types.captured_source)
            if not is_source or reference is None:
                if hop:
                    raise SourceCorrectionUnavailable
                return CurrentSourceResolution(requested, requested_ref, selected, reference, text, guard, tuple(guards))
            if reference in seen:
                raise SourceCorrectionUnavailable
            seen.add(reference)
            entry, receipt_guard = _receipt(root, reference)
            guards.append(receipt_guard)
            if entry is None:
                for retained in guards:
                    retained.recheck(root)
                return CurrentSourceResolution(requested, requested_ref, selected, reference, text, guard, tuple(guards))
            if hop == MAX_HOPS:
                raise SourceCorrectionUnavailable
            # The historical hash guards capture, while supported extraction/back-reference metadata can advance later.
            predecessor_ref = reference
            expected_ref = entry["new_ref"]
            # The guarded hint keeps ordinary hops to point reads; stable refs still survive a rename.
            try:
                selected = _path(root, entry["new_path"])
            except reserved_paths.ReservedPathLeafError as error:
                if error.code != "MISSING":
                    raise SourceCorrectionUnavailable from None
                selected = _path(root, expected_ref)
        raise SourceCorrectionUnavailable
    except (OSError, ValueError, TypeError, KeyError, memory_refs.ReferenceError, reserved_paths.ReservedPathLeafError):
        raise SourceCorrectionUnavailable from None


@dataclass(frozen=True, slots=True)
class CorrectionPlan:
    correction: SourceCorrection
    old_path: str
    old_ref: str
    old_source: str
    guards: tuple[vault.PathGuard, ...]

    def require_authority(self, root: Path) -> None:
        if (not _authorizer(root)(self.old_path)
                or access.writable_reason(root, self.old_path) is not None
                or egress.write_target_withheld(root, self.old_path)):
            raise SourceCorrectionUnavailable
        connector_boundary.require_write(root, self.old_path, content=self.old_source.encode("utf-8"))

    def receipt(self, root: Path, *, new_ref: str, new_path: str, timestamp: str) -> vault.PlannedWrite:
        entry = {
            "version": 1, "old_ref": self.old_ref, "new_ref": new_ref, "new_path": new_path,
            "expected_hash": self.correction["expected_hash"], "reason": self.correction["reason"],
            "timestamp": timestamp,
        }
        text = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        if len(text.encode("utf-8")) > RECEIPT_MAX_BYTES:
            raise ValueError("INVALID_SOURCE_CORRECTION: correction receipt exceeds its byte limit")
        return vault.PlannedWrite(
            _receipt_path(root, self.old_ref), text, create_only=True,
            guard=self.guards[-1], expected_hash=vault.MISSING_CONTENT_HASH,
        )


def prepare_correction(root: Path, value: SourceCorrection) -> CorrectionPlan:
    correction = validate_correction(value)
    try:
        path = _path(root, correction["supersedes"])
        text, guard, metadata, marker = _read_page(root, path, _authorizer(root))
        if marker is None or not note_types.Basis(root).selects(metadata.get("type"), note_types.captured_source):
            raise SourceCorrectionUnavailable
        reference = memory_refs.ref_from_markdown(text)
        if reference is None or metadata.get(memory_refs.ID_FIELD) != memory_refs.parse_memory_ref(reference):
            raise ValueError("SOURCE_CORRECTION_ID_REQUIRED: use maintain_memory(mode='backfill-ids', dry_run=false) with maintenance authority, then read the Source again and retry")
        if vault.content_hash(text) != correction["expected_hash"]:
            raise ValueError("SOURCE_CORRECTION_STALE: historical Source page changed; read it again and retry")
        entry, receipt_guard = _receipt(root, reference)
        if entry is not None:
            raise ValueError("SOURCE_ALREADY_CORRECTED: correct the current Source instead")
        plan = CorrectionPlan(correction, path, reference, text, (guard, receipt_guard))
        plan.require_authority(root)
        return plan
    except (OSError, memory_refs.ReferenceError, vault.PathGuardError, reserved_paths.ReservedPathLeafError):
        raise SourceCorrectionUnavailable from None
