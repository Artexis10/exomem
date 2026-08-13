"""Persist and securely replay evidence for deterministic epistemic assertions."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .assertions import AssertionContext, AssertionResult
from .registry import resolve
from .snapshot import EpistemicStateSnapshot, StrictModel


_SHA256 = r"^[0-9a-f]{64}$"


class EvidenceReplayError(ValueError):
    """Stored evidence cannot independently reproduce its frozen result."""


def _canonical_relative_path(value: str) -> str:
    if not value or "\\" in value:
        raise ValueError("evidence path must be a canonical relative POSIX path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("evidence path must be a canonical relative POSIX path")
    return path.as_posix()


class AssertionEvidenceRef(StrictModel):
    path: str
    sha256: str = Field(pattern=_SHA256)

    @field_validator("path")
    @classmethod
    def _canonical_path(cls, value: str) -> str:
        return _canonical_relative_path(value)


class SnapshotEvidenceRef(AssertionEvidenceRef):
    provider: str = Field(min_length=1)
    variant: str = Field(min_length=1)
    phase: str = Field(min_length=1)


class AssertionParameters(StrictModel):
    subject: str | None = None
    counterpart: str | None = None
    freshness_bound_s: float | None = None
    external_edit_at: str | None = None
    tolerance: float = 0.0


class AssertionProbeInputs(StrictModel):
    served_items: tuple[str, ...] | None = None
    foreign_case_hits: tuple[str, ...] | None = None


class AssertionEvidencePayload(StrictModel):
    artifact_type: Literal["assertion-evidence.v1"]
    schema_version: Literal[1]
    provider: str = Field(min_length=1)
    variant: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    scenario_sha256: str = Field(pattern=_SHA256)
    family_id: str = Field(min_length=1)
    phase_id: str = Field(min_length=1)
    expectation_ordinal: int = Field(ge=1)
    assertion: str = Field(min_length=1)
    current_snapshot: SnapshotEvidenceRef
    prior_snapshot: SnapshotEvidenceRef | None
    parameters: AssertionParameters
    probe_inputs: AssertionProbeInputs
    result: AssertionResult

    @model_validator(mode="after")
    def _snapshots_match_provider_row(self) -> "AssertionEvidencePayload":
        expected = (self.provider, self.variant)
        if (self.current_snapshot.provider, self.current_snapshot.variant) != expected:
            raise ValueError("current snapshot provider/variant differs from evidence row")
        if self.prior_snapshot is not None and (
            self.prior_snapshot.provider,
            self.prior_snapshot.variant,
        ) != expected:
            raise ValueError("prior snapshot provider/variant differs from evidence row")
        return self


class EvidenceBoundAssertion(StrictModel):
    result: AssertionResult
    evidence_ref: AssertionEvidenceRef | None = None

    @model_validator(mode="after")
    def _result_has_evidence(self) -> "EvidenceBoundAssertion":
        if self.evidence_ref is None:
            raise ValueError(
                "every bound assertion, including a failed assertion, requires an evidence reference"
            )
        return self


def _json_bytes(value: StrictModel) -> bytes:
    return (json.dumps(value.model_dump(mode="json"), sort_keys=True, indent=2) + "\n").encode()


def _safe_component(value: str) -> str:
    clean = "".join(character if character.isalnum() or character in "-_." else "-" for character in value)
    return clean.strip("-.") or "value"


def _bound_component(value: str) -> str:
    suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{_safe_component(value)}-{suffix}"


def _write_artifact(run_root: Path, relative: str, data: bytes) -> AssertionEvidenceRef:
    path = run_root / _canonical_relative_path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)
    return AssertionEvidenceRef(path=relative, sha256=hashlib.sha256(data).hexdigest())


def _snapshot_ref(
    run_root: Path,
    *,
    snapshot: EpistemicStateSnapshot,
    scenario_id: str,
    phase_id: str,
    ordinal: int,
    role: str,
) -> SnapshotEvidenceRef:
    stem = (
        f"{_bound_component(snapshot.provider)}-{_bound_component(snapshot.variant)}-"
        f"{_safe_component(scenario_id)}-{_safe_component(phase_id)}-{ordinal}-{role}.json"
    )
    basic = _write_artifact(run_root, f"assertion-evidence/snapshots/{stem}", _json_bytes(snapshot))
    return SnapshotEvidenceRef(
        **basic.model_dump(),
        provider=snapshot.provider,
        variant=snapshot.variant,
        phase=snapshot.phase,
    )


def persist_assertion_evidence(
    *,
    run_root: Path | str,
    scenario_id: str,
    scenario_sha256: str,
    family_id: str,
    phase_id: str,
    expectation_ordinal: int,
    assertion: str,
    context: AssertionContext,
    result: AssertionResult,
) -> AssertionEvidenceRef:
    """Persist the bound inputs/result separately from their canonical reference."""

    root = Path(run_root).resolve()
    current = _snapshot_ref(
        root,
        snapshot=context.snapshot,
        scenario_id=scenario_id,
        phase_id=phase_id,
        ordinal=expectation_ordinal,
        role="current",
    )
    prior = None
    if context.prior is not None:
        prior = _snapshot_ref(
            root,
            snapshot=context.prior,
            scenario_id=scenario_id,
            phase_id=phase_id,
            ordinal=expectation_ordinal,
            role="prior",
        )
    payload = AssertionEvidencePayload(
        artifact_type="assertion-evidence.v1",
        schema_version=1,
        provider=context.snapshot.provider,
        variant=context.snapshot.variant,
        scenario_id=scenario_id,
        scenario_sha256=scenario_sha256,
        family_id=family_id,
        phase_id=phase_id,
        expectation_ordinal=expectation_ordinal,
        assertion=assertion,
        current_snapshot=current,
        prior_snapshot=prior,
        parameters=AssertionParameters(
            subject=context.subject,
            counterpart=context.counterpart,
            freshness_bound_s=context.freshness_bound_s,
            external_edit_at=context.external_edit_at,
            tolerance=context.tolerance,
        ),
        probe_inputs=AssertionProbeInputs(
            served_items=context.served_items,
            foreign_case_hits=context.foreign_case_hits,
        ),
        result=result,
    )
    filename = (
        f"{_bound_component(context.snapshot.provider)}-"
        f"{_bound_component(context.snapshot.variant)}-"
        f"{_safe_component(scenario_id)}-{_safe_component(phase_id)}-"
        f"{expectation_ordinal}-{_safe_component(assertion)}.json"
    )
    return _write_artifact(root, f"assertion-evidence/{filename}", _json_bytes(payload))


def _read_no_follow(run_root: Path, relative: str, *, label: str) -> bytes:
    parts = PurePosixPath(_canonical_relative_path(relative)).parts
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(run_root.resolve(), directory_flags)
    current_fd = root_fd
    try:
        for part in parts[:-1]:
            try:
                next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            except FileNotFoundError as exc:
                raise EvidenceReplayError(f"{label} path component is missing: {part}") from exc
            except OSError as exc:
                raise EvidenceReplayError(f"{label} path uses a symlink or violates no-follow") from exc
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = next_fd
        try:
            final_fd = os.open(parts[-1], file_flags, dir_fd=current_fd)
        except FileNotFoundError as exc:
            raise EvidenceReplayError(f"{label} is missing: {relative}") from exc
        except OSError as exc:
            raise EvidenceReplayError(f"{label} uses a symlink or violates no-follow") from exc
        try:
            metadata = os.fstat(final_fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise EvidenceReplayError(f"{label} is not a regular file")
            chunks: list[bytes] = []
            while chunk := os.read(final_fd, 1024 * 1024):
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(final_fd)
    finally:
        if current_fd != root_fd:
            os.close(current_fd)
        os.close(root_fd)


def _load_snapshot(run_root: Path, reference: SnapshotEvidenceRef) -> EpistemicStateSnapshot:
    data = _read_no_follow(run_root, reference.path, label="snapshot")
    if hashlib.sha256(data).hexdigest() != reference.sha256:
        raise EvidenceReplayError("snapshot digest mismatch")
    try:
        snapshot = EpistemicStateSnapshot.model_validate_json(data)
    except Exception as exc:  # noqa: BLE001
        raise EvidenceReplayError("snapshot schema is invalid") from exc
    identity = (snapshot.provider, snapshot.variant, snapshot.phase)
    if identity != (reference.provider, reference.variant, reference.phase):
        raise EvidenceReplayError("snapshot identity differs from bound context")
    return snapshot


def _replay_assertion_evidence_payload(
    run_root: Path | str, reference: AssertionEvidenceRef
) -> tuple[AssertionEvidencePayload, AssertionResult]:
    """Securely reopen evidence and require exact deterministic replay equality."""

    root = Path(run_root)
    data = _read_no_follow(root, reference.path, label="assertion evidence")
    if hashlib.sha256(data).hexdigest() != reference.sha256:
        raise EvidenceReplayError("assertion evidence digest mismatch")
    try:
        payload = AssertionEvidencePayload.model_validate_json(data)
    except Exception as exc:  # noqa: BLE001
        raise EvidenceReplayError("assertion evidence schema is invalid") from exc
    current = _load_snapshot(root, payload.current_snapshot)
    prior = _load_snapshot(root, payload.prior_snapshot) if payload.prior_snapshot else None
    context = AssertionContext(
        snapshot=current,
        prior=prior,
        subject=payload.parameters.subject,
        counterpart=payload.parameters.counterpart,
        served_items=payload.probe_inputs.served_items,
        foreign_case_hits=payload.probe_inputs.foreign_case_hits,
        freshness_bound_s=payload.parameters.freshness_bound_s,
        external_edit_at=payload.parameters.external_edit_at,
        tolerance=payload.parameters.tolerance,
    )
    actual = resolve(payload.assertion)(context)
    if actual != payload.result:
        raise EvidenceReplayError("assertion replay result mismatch")
    return payload, actual


def replay_assertion_evidence(
    run_root: Path | str, reference: AssertionEvidenceRef
) -> AssertionResult:
    """Securely replay an evidence reference and return the reproduced result."""

    return _replay_assertion_evidence_payload(run_root, reference)[1]


__all__ = [
    "AssertionEvidencePayload",
    "AssertionEvidenceRef",
    "EvidenceBoundAssertion",
    "EvidenceReplayError",
    "persist_assertion_evidence",
    "replay_assertion_evidence",
]
