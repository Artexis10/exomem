"""Crash-safe typed outer-seal state for governed vault consolidation."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NoReturn

from .. import reserved_paths
from . import consolidation_authority, consolidation_plan

_DESCRIPTOR_ID = "consolidation-tree"
_OWNER = "consolidation.run"
_SNAPSHOT_SCHEMA = "exomem.consolidation-seal-snapshot/v1"
_ACTIVE_SCHEMA = "exomem.consolidation-seal-active/v1"
_OPEN_SCHEMA = "exomem.vault-open/v1"
_DELETION_SCHEMA = "exomem.deletion-seal/v1"
_CONSOLIDATION_SCHEMA = "exomem.consolidation-seal/v1"
_SNAPSHOT_DOMAIN = _SNAPSHOT_SCHEMA.encode("ascii")
_MAX_ACTIVE_BYTES = 4096
_MAX_SNAPSHOT_BYTES = 16 * 1024
_MAX_SAFE_INTEGER = (1 << 53) - 1
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_UUID4 = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_KINDS = frozenset({"open", "deletion-sealed", "consolidation-sealed"})
_PHASES = frozenset(
    {
        "sealing",
        "sealed",
        "preimage-ready",
        "policy-active",
        "publishing",
        "rebuilding",
        "verifying",
        "verified",
        "transport-stopping",
        "transport-verifying",
        "transport-verified",
        "routing-opening",
        "complete",
        "rollback-planning",
        "rollback-restoring",
        "rollback-verifying",
        "rollback-complete",
        "aborting",
        "aborted",
        "recovering",
    }
)
_ACTIONS = frozenset({"apply", "verify", "recover", "abort", "rollback", "probe"})
_UNSEAL_PHASES = frozenset({"complete", "rollback-complete", "aborted"})
_SNAPSHOT_FIELDS = frozenset(
    {"schema", "revision", "kind", "vault_binding_digest", "recorded_at", "state"}
)
_ACTIVE_FIELDS = frozenset(
    {"schema", "revision", "kind", "vault_binding_digest", "snapshot_digest"}
)


class ConsolidationSealUnavailable(RuntimeError):
    """Content-free refusal for invalid, stale, or unavailable seal state."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> NoReturn:
    raise ConsolidationSealUnavailable(code) from None


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail("SEAL_INPUT_INVALID")
    return value


def _uuid4(value: object) -> str:
    if not isinstance(value, str) or _UUID4.fullmatch(value) is None:
        _fail("SEAL_INPUT_INVALID")
    return value


def _timestamp(value: object) -> str:
    try:
        checked, _parsed = consolidation_plan._timestamp(value)  # noqa: SLF001
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail("SEAL_INPUT_INVALID")
    return checked


def _revision(value: object) -> int:
    if type(value) is not int or value < 0 or value > _MAX_SAFE_INTEGER:
        _fail("SEAL_INPUT_INVALID")
    return value


def _kind(value: object) -> str:
    if not isinstance(value, str) or value not in _KINDS:
        _fail("SEAL_INPUT_INVALID")
    return value


def _phase(value: object) -> str:
    if not isinstance(value, str) or value not in _PHASES:
        _fail("SEAL_INPUT_INVALID")
    return value


def _action(value: object) -> str:
    if not isinstance(value, str) or value not in _ACTIONS:
        _fail("SEAL_INPUT_INVALID")
    return value


def _mapping(value: object, fields: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or frozenset(value) != fields:
        _fail("SEAL_STORE_CORRUPT")
    return value


def _canonical(value: Mapping[str, object]) -> bytes:
    try:
        return consolidation_plan.canonical_closed_jcs(value)
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail("SEAL_INPUT_INVALID")


def _exact_mapping(raw: bytes, *, maximum: int) -> Mapping[str, object]:
    try:
        parsed = consolidation_plan._parse_canonical_mapping(raw, maximum=maximum)  # noqa: SLF001
        if consolidation_plan.canonical_closed_jcs(parsed) != raw:
            _fail("SEAL_STORE_CORRUPT")
    except consolidation_plan.ConsolidationPlanUnavailable:
        _fail("SEAL_STORE_CORRUPT")
    return parsed


def _snapshot_digest(raw: bytes) -> str:
    framed = (
        len(_SNAPSHOT_DOMAIN).to_bytes(4, "big")
        + _SNAPSHOT_DOMAIN
        + len(raw).to_bytes(8, "big")
        + raw
    )
    return hashlib.sha256(framed).hexdigest()


@dataclass(frozen=True, slots=True)
class ConsolidationSealState:
    """One exact effective member of the durable seal union."""

    kind: str
    revision: int
    vault_binding_digest: str
    recorded_at: str
    state_digest: str
    checkpoint_digest: str | None = None
    run_id: str | None = None
    operation_id: str | None = None
    journal_digest: str | None = None
    phase: str | None = None
    sealed_at: str | None = None


@contextmanager
def _authority(vault_root: Path, *, mutation: bool) -> Iterator[None]:
    try:
        with reserved_paths._subsystem_authority_scope(_OWNER):  # noqa: SLF001
            with reserved_paths._identity_coordination_scope(  # noqa: SLF001
                vault_root,
                descriptor_ids=(_DESCRIPTOR_ID,),
                identity_may_change=mutation,
            ):
                yield
    except ConsolidationSealUnavailable:
        raise
    except (OSError, RuntimeError, ValueError):
        _fail("SEAL_STORE_UNAVAILABLE")


class ConsolidationSealStore:
    """Persist immutable seal snapshots behind one exact CAS active pointer."""

    def __init__(self, vault_root: Path | str):
        self.vault_root = Path(vault_root).absolute()
        self.base = self.vault_root / "Knowledge Base" / "_Consolidation" / "seal"

    def _snapshot_path(self, revision: int) -> Path:
        return self.base / "snapshots" / f"{revision}.json"

    def _read_optional(self, path: Path, *, limit: int) -> bytes | None:
        try:
            return reserved_paths._read_owner_bytes(  # noqa: SLF001
                self.vault_root,
                path,
                _DESCRIPTOR_ID,
                limit=limit,
            )
        except FileNotFoundError:
            return None

    def _publish_missing(self, path: Path, raw: bytes) -> None:
        reserved_paths._publish_owner_bytes(  # noqa: SLF001
            self.vault_root,
            path,
            _DESCRIPTOR_ID,
            raw,
            require_missing=True,
        )

    def _publish_active(
        self,
        raw: bytes,
        *,
        expected_sha256: str | None,
    ) -> None:
        reserved_paths._publish_owner_bytes(  # noqa: SLF001
            self.vault_root,
            self.base / "active.json",
            _DESCRIPTOR_ID,
            raw,
            expected_sha256=expected_sha256,
            require_missing=expected_sha256 is None,
        )

    @staticmethod
    def _state_mapping(state: ConsolidationSealState) -> Mapping[str, object]:
        if state.kind == "open":
            return {"schema": _OPEN_SCHEMA}
        if state.kind == "deletion-sealed":
            return {
                "schema": _DELETION_SCHEMA,
                "checkpoint_digest": _digest(state.checkpoint_digest),
                "sealed_at": _timestamp(state.sealed_at),
            }
        if state.kind == "consolidation-sealed":
            return {
                "schema": _CONSOLIDATION_SCHEMA,
                "run_id": _uuid4(state.run_id),
                "operation_id": _uuid4(state.operation_id),
                "journal_digest": _digest(state.journal_digest),
                "phase": _phase(state.phase),
                "sealed_at": _timestamp(state.sealed_at),
            }
        _fail("SEAL_INPUT_INVALID")

    @classmethod
    def _snapshot_bytes(cls, state: ConsolidationSealState) -> bytes:
        return _canonical(
            {
                "schema": _SNAPSHOT_SCHEMA,
                "revision": _revision(state.revision),
                "kind": _kind(state.kind),
                "vault_binding_digest": _digest(state.vault_binding_digest),
                "recorded_at": _timestamp(state.recorded_at),
                "state": cls._state_mapping(state),
            }
        )

    @staticmethod
    def _active_bytes(state: ConsolidationSealState, *, snapshot_digest: str) -> bytes:
        return _canonical(
            {
                "schema": _ACTIVE_SCHEMA,
                "revision": _revision(state.revision),
                "kind": _kind(state.kind),
                "vault_binding_digest": _digest(state.vault_binding_digest),
                "snapshot_digest": _digest(snapshot_digest),
            }
        )

    @staticmethod
    def _restore_snapshot(raw: bytes, *, expected_digest: str) -> ConsolidationSealState:
        if _snapshot_digest(raw) != expected_digest:
            _fail("SEAL_STORE_CORRUPT")
        snapshot = _mapping(
            _exact_mapping(raw, maximum=_MAX_SNAPSHOT_BYTES),
            _SNAPSHOT_FIELDS,
        )
        if snapshot["schema"] != _SNAPSHOT_SCHEMA:
            _fail("SEAL_STORE_CORRUPT")
        kind = _kind(snapshot["kind"])
        revision = _revision(snapshot["revision"])
        vault_binding_digest = _digest(snapshot["vault_binding_digest"])
        recorded_at = _timestamp(snapshot["recorded_at"])
        state = snapshot["state"]
        if kind == "open":
            opened = _mapping(state, frozenset({"schema"}))
            if opened["schema"] != _OPEN_SCHEMA:
                _fail("SEAL_STORE_CORRUPT")
            return ConsolidationSealState(
                kind=kind,
                revision=revision,
                vault_binding_digest=vault_binding_digest,
                recorded_at=recorded_at,
                state_digest=expected_digest,
            )
        if kind == "deletion-sealed":
            deletion = _mapping(
                state,
                frozenset({"schema", "checkpoint_digest", "sealed_at"}),
            )
            if deletion["schema"] != _DELETION_SCHEMA:
                _fail("SEAL_STORE_CORRUPT")
            return ConsolidationSealState(
                kind=kind,
                revision=revision,
                vault_binding_digest=vault_binding_digest,
                recorded_at=recorded_at,
                state_digest=expected_digest,
                checkpoint_digest=_digest(deletion["checkpoint_digest"]),
                sealed_at=_timestamp(deletion["sealed_at"]),
            )
        consolidation = _mapping(
            state,
            frozenset(
                {"schema", "run_id", "operation_id", "journal_digest", "phase", "sealed_at"}
            ),
        )
        if consolidation["schema"] != _CONSOLIDATION_SCHEMA:
            _fail("SEAL_STORE_CORRUPT")
        return ConsolidationSealState(
            kind=kind,
            revision=revision,
            vault_binding_digest=vault_binding_digest,
            recorded_at=recorded_at,
            state_digest=expected_digest,
            run_id=_uuid4(consolidation["run_id"]),
            operation_id=_uuid4(consolidation["operation_id"]),
            journal_digest=_digest(consolidation["journal_digest"]),
            phase=_phase(consolidation["phase"]),
            sealed_at=_timestamp(consolidation["sealed_at"]),
        )

    def _load_locked(self) -> tuple[ConsolidationSealState, bytes]:
        active_raw = self._read_optional(self.base / "active.json", limit=_MAX_ACTIVE_BYTES)
        if active_raw is None:
            if self._read_optional(self._snapshot_path(0), limit=_MAX_SNAPSHOT_BYTES) is not None:
                _fail("SEAL_STORE_CORRUPT")
            _fail("SEAL_NOT_INITIALIZED")
        try:
            active = _mapping(
                _exact_mapping(active_raw, maximum=_MAX_ACTIVE_BYTES),
                _ACTIVE_FIELDS,
            )
            if active["schema"] != _ACTIVE_SCHEMA:
                _fail("SEAL_STORE_CORRUPT")
            revision = _revision(active["revision"])
            kind = _kind(active["kind"])
            vault_binding_digest = _digest(active["vault_binding_digest"])
            snapshot_digest = _digest(active["snapshot_digest"])
            snapshot_raw = self._read_optional(
                self._snapshot_path(revision),
                limit=_MAX_SNAPSHOT_BYTES,
            )
            if snapshot_raw is None:
                _fail("SEAL_STORE_CORRUPT")
            state = self._restore_snapshot(snapshot_raw, expected_digest=snapshot_digest)
            if (
                state.revision != revision
                or state.kind != kind
                or state.vault_binding_digest != vault_binding_digest
            ):
                _fail("SEAL_STORE_CORRUPT")
            return state, active_raw
        except ConsolidationSealUnavailable as error:
            if error.code == "SEAL_INPUT_INVALID":
                _fail("SEAL_STORE_CORRUPT")
            raise

    @staticmethod
    def _require_vault(state: ConsolidationSealState, expected: str) -> None:
        if state.vault_binding_digest != expected:
            _fail("SEAL_VAULT_MISMATCH")

    def _commit_locked(
        self,
        target: ConsolidationSealState,
        *,
        current_active_raw: bytes | None,
    ) -> ConsolidationSealState:
        snapshot_raw = self._snapshot_bytes(target)
        target = replace(target, state_digest=_snapshot_digest(snapshot_raw))
        snapshot_path = self._snapshot_path(target.revision)
        existing = self._read_optional(snapshot_path, limit=_MAX_SNAPSHOT_BYTES)
        if existing is None:
            self._publish_missing(snapshot_path, snapshot_raw)
        elif existing != snapshot_raw:
            _fail("SEAL_STATE_CONFLICT")
        active_raw = self._active_bytes(target, snapshot_digest=target.state_digest)
        self._publish_active(
            active_raw,
            expected_sha256=(
                hashlib.sha256(current_active_raw).hexdigest()
                if current_active_raw is not None
                else None
            ),
        )
        return target

    def load(self, *, vault_binding_digest: str) -> ConsolidationSealState:
        """Load the exact typed seal state; absence is never interpreted as open."""

        expected = _digest(vault_binding_digest)
        with _authority(self.vault_root, mutation=False):
            state, _active = self._load_locked()
        self._require_vault(state, expected)
        return state

    def initialize_open(
        self,
        *,
        vault_binding_digest: str,
        recorded_at: str,
    ) -> ConsolidationSealState:
        """Create the explicit initial open member or adopt its exact retry."""

        expected_vault = _digest(vault_binding_digest)
        checked_time = _timestamp(recorded_at)
        target = ConsolidationSealState(
            kind="open",
            revision=0,
            vault_binding_digest=expected_vault,
            recorded_at=checked_time,
            state_digest="0" * 64,
        )
        with _authority(self.vault_root, mutation=True):
            try:
                current, _active = self._load_locked()
            except ConsolidationSealUnavailable as error:
                if error.code == "SEAL_STORE_CORRUPT":
                    initial_raw = self._read_optional(
                        self._snapshot_path(0),
                        limit=_MAX_SNAPSHOT_BYTES,
                    )
                    later_raw = self._read_optional(
                        self._snapshot_path(1),
                        limit=_MAX_SNAPSHOT_BYTES,
                    )
                    if initial_raw == self._snapshot_bytes(target) and later_raw is None:
                        return self._commit_locked(target, current_active_raw=None)
                    raise
                if error.code != "SEAL_NOT_INITIALIZED":
                    raise
                current = None
            if current is not None:
                self._require_vault(current, expected_vault)
                if current.kind == "deletion-sealed":
                    _fail("DELETION_SEAL_IRREVERSIBLE")
                if current.kind != "open":
                    _fail("SEAL_STATE_CONFLICT")
                if current.recorded_at != checked_time:
                    _fail("SEAL_STATE_CONFLICT")
                return current
            return self._commit_locked(target, current_active_raw=None)

    def begin_consolidation(
        self,
        *,
        vault_binding_digest: str,
        run_id: str,
        operation_id: str,
        journal_digest: str,
        sealed_at: str,
        expected_revision: int,
    ) -> ConsolidationSealState:
        """Transition explicit open state to one exact consolidation seal intent."""

        expected_vault = _digest(vault_binding_digest)
        checked_run = _uuid4(run_id)
        checked_operation = _uuid4(operation_id)
        checked_journal = _digest(journal_digest)
        checked_time = _timestamp(sealed_at)
        expected_revision = _revision(expected_revision)
        with _authority(self.vault_root, mutation=True):
            current, active_raw = self._load_locked()
            self._require_vault(current, expected_vault)
            if current.kind == "deletion-sealed":
                _fail("DELETION_SEAL_IRREVERSIBLE")
            if current.kind == "consolidation-sealed":
                if (
                    current.run_id == checked_run
                    and current.operation_id == checked_operation
                    and current.journal_digest == checked_journal
                    and current.phase == "sealing"
                    and current.sealed_at == checked_time
                ):
                    return current
                _fail("SEAL_STATE_CONFLICT")
            if current.revision != expected_revision:
                _fail("SEAL_REVISION_CONFLICT")
            target = ConsolidationSealState(
                kind="consolidation-sealed",
                revision=current.revision + 1,
                vault_binding_digest=expected_vault,
                recorded_at=checked_time,
                state_digest="0" * 64,
                run_id=checked_run,
                operation_id=checked_operation,
                journal_digest=checked_journal,
                phase="sealing",
                sealed_at=checked_time,
            )
            return self._commit_locked(target, current_active_raw=active_raw)

    def seal_for_deletion(
        self,
        *,
        vault_binding_digest: str,
        checkpoint_digest: str,
        sealed_at: str,
        expected_revision: int,
    ) -> ConsolidationSealState:
        """Enter the terminal deletion member; no API transitions it back to open."""

        expected_vault = _digest(vault_binding_digest)
        checked_checkpoint = _digest(checkpoint_digest)
        checked_time = _timestamp(sealed_at)
        expected_revision = _revision(expected_revision)
        with _authority(self.vault_root, mutation=True):
            current, active_raw = self._load_locked()
            self._require_vault(current, expected_vault)
            if current.kind == "deletion-sealed":
                if (
                    current.checkpoint_digest == checked_checkpoint
                    and current.sealed_at == checked_time
                ):
                    return current
                _fail("DELETION_SEAL_IRREVERSIBLE")
            if current.kind != "open":
                _fail("SEAL_STATE_CONFLICT")
            if current.revision != expected_revision:
                _fail("SEAL_REVISION_CONFLICT")
            target = ConsolidationSealState(
                kind="deletion-sealed",
                revision=current.revision + 1,
                vault_binding_digest=expected_vault,
                recorded_at=checked_time,
                state_digest="0" * 64,
                checkpoint_digest=checked_checkpoint,
                sealed_at=checked_time,
            )
            return self._commit_locked(target, current_active_raw=active_raw)

    def _require_consolidation_authority(
        self,
        current: ConsolidationSealState,
        authority: object,
        *,
        vault_binding_digest: str,
        action: str,
    ) -> None:
        if current.kind == "deletion-sealed":
            _fail("DELETION_SEAL_IRREVERSIBLE")
        if current.kind != "consolidation-sealed":
            _fail("SEAL_KIND_CONFLICT")
        try:
            consolidation_authority.require_authority(
                authority,
                vault_binding_digest=vault_binding_digest,
                run_id=current.run_id,
                operation_id=current.operation_id,
                journal_digest=current.journal_digest,
                phase=current.phase,
                action=_action(action),
            )
        except consolidation_authority.ConsolidationAuthorityUnavailable:
            _fail("SEAL_AUTHORITY_MISMATCH")

    def advance_consolidation(
        self,
        authority: object,
        *,
        vault_binding_digest: str,
        action: str,
        target_phase: str,
        recorded_at: str,
        expected_revision: int,
    ) -> ConsolidationSealState:
        """Advance only the consolidation member bound to the exact current authority."""

        expected_vault = _digest(vault_binding_digest)
        checked_phase = _phase(target_phase)
        checked_time = _timestamp(recorded_at)
        expected_revision = _revision(expected_revision)
        with _authority(self.vault_root, mutation=True):
            current, active_raw = self._load_locked()
            self._require_vault(current, expected_vault)
            self._require_consolidation_authority(
                current,
                authority,
                vault_binding_digest=expected_vault,
                action=action,
            )
            if current.phase == checked_phase:
                return current
            if current.phase in _UNSEAL_PHASES:
                _fail("SEAL_PHASE_CONFLICT")
            if current.revision != expected_revision:
                _fail("SEAL_REVISION_CONFLICT")
            target = ConsolidationSealState(
                kind=current.kind,
                revision=current.revision + 1,
                vault_binding_digest=current.vault_binding_digest,
                recorded_at=checked_time,
                state_digest="0" * 64,
                run_id=current.run_id,
                operation_id=current.operation_id,
                journal_digest=current.journal_digest,
                phase=checked_phase,
                sealed_at=current.sealed_at,
            )
            return self._commit_locked(target, current_active_raw=active_raw)

    def unseal_consolidation(
        self,
        authority: object,
        *,
        vault_binding_digest: str,
        action: str,
        recorded_at: str,
        expected_revision: int,
    ) -> ConsolidationSealState:
        """Remove only an exact terminal consolidation seal; deletion never crosses here."""

        expected_vault = _digest(vault_binding_digest)
        checked_time = _timestamp(recorded_at)
        expected_revision = _revision(expected_revision)
        with _authority(self.vault_root, mutation=True):
            current, active_raw = self._load_locked()
            self._require_vault(current, expected_vault)
            self._require_consolidation_authority(
                current,
                authority,
                vault_binding_digest=expected_vault,
                action=action,
            )
            if current.phase not in _UNSEAL_PHASES:
                _fail("SEAL_PHASE_CONFLICT")
            if current.revision != expected_revision:
                _fail("SEAL_REVISION_CONFLICT")
            target = ConsolidationSealState(
                kind="open",
                revision=current.revision + 1,
                vault_binding_digest=current.vault_binding_digest,
                recorded_at=checked_time,
                state_digest="0" * 64,
            )
            return self._commit_locked(target, current_active_raw=active_raw)


__all__ = [
    "ConsolidationSealState",
    "ConsolidationSealStore",
    "ConsolidationSealUnavailable",
]
