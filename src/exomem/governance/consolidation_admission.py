"""Owner-inclusive process-safe admission and drain for consolidation seals."""

from __future__ import annotations

import hashlib
import math
import os
import platform
import re
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread
from typing import NoReturn, cast

from .. import held_fs, reserved_paths, writer_lease
from ..cli_ops import OpError
from ..mutation_lock import VaultMutationCoordinator
from . import consolidation_authority, consolidation_plan, consolidation_seal

_DESCRIPTOR_ID = "consolidation-tree"
_OWNER = "consolidation.run"
_PARTICIPANT_SCHEMA = "exomem.consolidation-admission-participant/v1"
_CONTROL_SCHEMA = "exomem.consolidation-control-participant/v1"
_PARTICIPANT_KINDS = frozenset({"read", "mutation", "transfer", "background"})
_PARTICIPANT_FIELDS = frozenset({"schema", "participant_id", "kind", "state_domain_digest"})
_CONTROL_FIELDS = frozenset(
    {
        "schema",
        "participant_id",
        "kind",
        "state_domain_digest",
        "run_id",
        "operation_id",
        "journal_digest",
        "request_digest",
        "phase",
        "action",
    }
)
_PARTICIPANT_NAME = re.compile(r"([0-9a-f]{32})\.json\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_MAX_PARTICIPANT_BYTES = 1024
_MAX_SAFE_INTEGER = (1 << 53) - 1
_HANDLE_SEAL = object()


class ConsolidationAdmissionUnavailable(RuntimeError):
    """Stable content-free refusal from the outer consolidation boundary."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> NoReturn:
    raise ConsolidationAdmissionUnavailable(code) from None


@dataclass(frozen=True, slots=True)
class ConsolidationAdmissionSnapshot:
    """Content-private durable coordination state for trusted control code."""

    state: consolidation_seal.ConsolidationSealState
    active_reads: int
    active_mutations: int
    active_transfers: int
    active_background: int
    draining: bool

    @property
    def active_total(self) -> int:
        return (
            self.active_reads
            + self.active_mutations
            + self.active_transfers
            + self.active_background
        )


@dataclass(frozen=True, slots=True)
class _Participant:
    participant_id: str
    kind: str
    state_domain_digest: str
    run_id: str | None = None
    operation_id: str | None = None
    journal_digest: str | None = None
    request_digest: str | None = None
    phase: str | None = None
    action: str | None = None


@dataclass(slots=True)
class _HandleLease:
    active: bool = True


class ConsolidationMutationAdmission:
    """Opaque process-local handle for one ordinary mutation admission."""

    __slots__ = ("__converted", "__lease", "__owner", "__participant_id", "__seal")

    def __init__(
        self,
        owner: object,
        participant_id: str,
        lease: _HandleLease,
        seal: object,
    ) -> None:
        self.__owner = owner
        self.__participant_id = participant_id
        self.__lease = lease
        self.__seal = seal
        self.__converted = False

    def _matches(self, owner: object, participant_id: str) -> bool:
        return (
            self.__seal is _HANDLE_SEAL
            and self.__owner is owner
            and self.__participant_id == participant_id
            and self.__lease.active
        )

    def _participant(self, owner: object) -> str:
        if not self._matches(owner, self.__participant_id):
            _fail("CONSOLIDATION_CONTROL_UNAVAILABLE")
        return self.__participant_id

    def _convert(self) -> None:
        self.__converted = True

    def _is_converted(self) -> bool:
        return self.__converted

    def _lease(self) -> _HandleLease:
        return self.__lease

    def _finish(self) -> None:
        self.__lease.active = False

    def __reduce__(self) -> NoReturn:
        raise TypeError("consolidation mutation admission is process-local")


class ConsolidationControlAdmission:
    """Opaque process-local handle for one exact durable control participant."""

    __slots__ = ("__lease", "__owner", "__participant", "__seal")

    def __init__(
        self,
        owner: object,
        participant: _Participant,
        lease: _HandleLease,
        seal: object,
    ) -> None:
        self.__owner = owner
        self.__participant = participant
        self.__lease = lease
        self.__seal = seal

    def _matches(self, owner: object, participant: _Participant) -> bool:
        return (
            self.__seal is _HANDLE_SEAL
            and self.__owner is owner
            and self.__participant == participant
            and self.__lease.active
        )

    def _participant_for(self, owner: object) -> _Participant:
        if not self._matches(owner, self.__participant):
            _fail("CONSOLIDATION_CONTROL_UNAVAILABLE")
        return self.__participant

    def _finish(self) -> None:
        self.__lease.active = False

    def __reduce__(self) -> NoReturn:
        raise TypeError("consolidation control admission is process-local")


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
    except ConsolidationAdmissionUnavailable:
        raise
    except (OSError, RuntimeError, ValueError):
        _fail("CONSOLIDATION_ADMISSION_UNAVAILABLE")


class ConsolidationAdmission:
    """Coordinate ordinary participants with one vault's durable outer seal.

    Every admitted operation owns an OS lock and a content-free durable record.
    The seal intent is published under a shared gate before the controller waits
    for those locks. This makes the boundary common to sibling objects and
    processes rather than relying on process-local counters.
    """

    def __init__(
        self,
        vault_root: Path | str,
        *,
        vault_binding_digest: str,
        store: consolidation_seal.ConsolidationSealStore | None = None,
    ) -> None:
        self.vault_root = Path(vault_root).absolute()
        self.vault_binding_digest = vault_binding_digest
        self._store = store or consolidation_seal.ConsolidationSealStore(self.vault_root)
        self._participants = (
            self.vault_root / "Knowledge Base" / "_Consolidation" / "admission" / "participants"
        )
        try:
            self._state_root = (
                Path(writer_lease.active_manager().config.state_dir)
                .expanduser()
                .resolve(strict=False)
            )
            self._store.load(vault_binding_digest=vault_binding_digest)
        except consolidation_seal.ConsolidationSealUnavailable:
            _fail("CONSOLIDATION_SEAL_UNAVAILABLE")
        except (OSError, RuntimeError, ValueError):
            _fail("CONSOLIDATION_ADMISSION_UNAVAILABLE")
        try:
            with self._gate().hold(
                operation="admission_bootstrap",
                holder_kind="reserved-state",
                publish_holder_metadata=False,
            ):
                state_identity = self._state_root.stat()
        except (OSError, OpError):
            _fail("CONSOLIDATION_ADMISSION_UNAVAILABLE")
        host_identity = self._host_identity()
        domain = (
            f"{host_identity}\0{os.path.normcase(str(self._state_root))}\0"
            f"{state_identity.st_dev}\0{state_identity.st_ino}"
        )
        self._state_domain_digest = hashlib.sha256(domain.encode("utf-8")).hexdigest()

    @staticmethod
    def _host_identity() -> str:
        """Return a stable host discriminator without persisting host details."""

        try:
            machine_id = Path("/etc/machine-id").read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            machine_id = ""
        return machine_id or platform.node() or f"node-{uuid.getnode():012x}"

    def _coordinator(
        self,
        namespace: str,
        identity: str,
        *,
        timeout: float,
        state_root: Path | None = None,
    ) -> VaultMutationCoordinator:
        boundary = f"consolidation-admission:{self.vault_binding_digest}:{namespace}:{identity}"
        return VaultMutationCoordinator(
            self._state_root if state_root is None else state_root,
            boundary,
            timeout_seconds=timeout,
        )

    def _gate(self, *, timeout: float = 5.0) -> VaultMutationCoordinator:
        return self._coordinator("gate", "shared", timeout=timeout)

    def _participant_lock(
        self,
        participant_id: str,
        *,
        timeout: float,
    ) -> VaultMutationCoordinator:
        return self._coordinator("participant", participant_id, timeout=timeout)

    @staticmethod
    def _participant_bytes(participant: _Participant) -> bytes:
        if participant.kind in _PARTICIPANT_KINDS:
            value: dict[str, object] = {
                "schema": _PARTICIPANT_SCHEMA,
                "participant_id": participant.participant_id,
                "kind": participant.kind,
                "state_domain_digest": participant.state_domain_digest,
            }
        elif participant.kind == "control":
            value = {
                "schema": _CONTROL_SCHEMA,
                "participant_id": participant.participant_id,
                "kind": participant.kind,
                "state_domain_digest": participant.state_domain_digest,
                "run_id": participant.run_id,
                "operation_id": participant.operation_id,
                "journal_digest": participant.journal_digest,
                "request_digest": participant.request_digest,
                "phase": participant.phase,
                "action": participant.action,
            }
        else:
            _fail("CONSOLIDATION_ADMISSION_UNAVAILABLE")
        return consolidation_plan.canonical_closed_jcs(value)

    @staticmethod
    def _parse_participant(raw: bytes, *, expected_id: str) -> _Participant:
        try:
            parsed = consolidation_plan._parse_canonical_mapping(  # noqa: SLF001
                raw,
                maximum=_MAX_PARTICIPANT_BYTES,
            )
            if consolidation_plan.canonical_closed_jcs(parsed) != raw:
                raise ValueError
            schema = parsed["schema"]
            participant_id = parsed["participant_id"]
            kind = parsed["kind"]
            state_domain_digest = parsed["state_domain_digest"]
            if participant_id != expected_id:
                raise ValueError
            if (
                not isinstance(state_domain_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", state_domain_digest) is None
            ):
                raise ValueError
            if schema == _PARTICIPANT_SCHEMA:
                if frozenset(parsed) != _PARTICIPANT_FIELDS or kind not in _PARTICIPANT_KINDS:
                    raise ValueError
                return _Participant(participant_id, kind, state_domain_digest)
            if (
                schema != _CONTROL_SCHEMA
                or frozenset(parsed) != _CONTROL_FIELDS
                or kind != "control"
            ):
                raise ValueError
            run_id = parsed["run_id"]
            operation_id = parsed["operation_id"]
            journal_digest = parsed["journal_digest"]
            request_digest = parsed["request_digest"]
            phase = parsed["phase"]
            action = parsed["action"]
            if (
                not isinstance(run_id, str)
                or _UUID4.fullmatch(run_id) is None
                or not isinstance(operation_id, str)
                or _UUID4.fullmatch(operation_id) is None
                or not isinstance(journal_digest, str)
                or _DIGEST.fullmatch(journal_digest) is None
                or not isinstance(request_digest, str)
                or _DIGEST.fullmatch(request_digest) is None
                or phase != "sealing"
                or action != "apply"
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError, consolidation_plan.ConsolidationPlanUnavailable):
            _fail("CONSOLIDATION_ADMISSION_UNAVAILABLE")
        return _Participant(
            participant_id,
            kind,
            state_domain_digest,
            run_id,
            operation_id,
            journal_digest,
            request_digest,
            phase,
            action,
        )

    def _participant_path(self, participant_id: str) -> Path:
        return self._participants / f"{participant_id}.json"

    def _publish_participant(
        self,
        participant: _Participant,
        *,
        expected_raw: bytes | None = None,
    ) -> None:
        try:
            with _authority(self.vault_root, mutation=True):
                reserved_paths._publish_owner_bytes(  # noqa: SLF001
                    self.vault_root,
                    self._participant_path(participant.participant_id),
                    _DESCRIPTOR_ID,
                    self._participant_bytes(participant),
                    expected_sha256=(
                        hashlib.sha256(expected_raw).hexdigest()
                        if expected_raw is not None
                        else None
                    ),
                    require_missing=expected_raw is None,
                )
        except ConsolidationAdmissionUnavailable:
            raise
        except (OSError, RuntimeError, ValueError):
            _fail("CONSOLIDATION_ADMISSION_UNAVAILABLE")

    def _remove_participant(self, participant_id: str) -> None:
        try:
            with _authority(self.vault_root, mutation=True):
                reserved_paths._remove_owner_file(  # noqa: SLF001
                    self.vault_root,
                    self._participant_path(participant_id),
                    _DESCRIPTOR_ID,
                    missing_ok=True,
                )
        except ConsolidationAdmissionUnavailable:
            raise
        except (OSError, RuntimeError, ValueError):
            _fail("CONSOLIDATION_ADMISSION_UNAVAILABLE")

    def _participant_names(self) -> tuple[str, ...]:
        try:
            with _authority(self.vault_root, mutation=False):
                acquired = held_fs.acquire(self.vault_root)
                if not acquired.ok:
                    _fail("CONSOLIDATION_ADMISSION_UNAVAILABLE")
                with acquired.require() as filesystem:
                    relative = self._participants.relative_to(self.vault_root).as_posix()
                    parent_result = filesystem.parent(relative)
                    if not parent_result.ok:
                        if (
                            parent_result.error is not None
                            and parent_result.error.code == "MISSING"
                        ):
                            return ()
                        _fail("CONSOLIDATION_ADMISSION_UNAVAILABLE")
                    with parent_result.require() as parent:
                        children = filesystem.children(parent)
                        if not children.ok:
                            _fail("CONSOLIDATION_ADMISSION_UNAVAILABLE")
                        names: list[str] = []
                        for child in children.require():
                            match = _PARTICIPANT_NAME.fullmatch(child.relative_path)
                            if match is None or child.identity.kind != "file":
                                _fail("CONSOLIDATION_ADMISSION_UNAVAILABLE")
                            names.append(match.group(1))
                        return tuple(sorted(names))
        except ConsolidationAdmissionUnavailable:
            raise
        except (OSError, RuntimeError, ValueError, held_fs.HeldFsError):
            _fail("CONSOLIDATION_ADMISSION_UNAVAILABLE")

    def _load_participant(self, participant_id: str) -> _Participant | None:
        try:
            with _authority(self.vault_root, mutation=False):
                raw = reserved_paths._read_owner_bytes(  # noqa: SLF001
                    self.vault_root,
                    self._participant_path(participant_id),
                    _DESCRIPTOR_ID,
                    limit=_MAX_PARTICIPANT_BYTES,
                )
        except FileNotFoundError:
            return None
        except ConsolidationAdmissionUnavailable:
            raise
        except (OSError, RuntimeError, ValueError):
            _fail("CONSOLIDATION_ADMISSION_UNAVAILABLE")
        return self._parse_participant(raw, expected_id=participant_id)

    def _load_participants(self) -> tuple[_Participant, ...]:
        participants: list[_Participant] = []
        for participant_id in self._participant_names():
            participant = self._load_participant(participant_id)
            if participant is not None:
                participants.append(participant)
        return tuple(participants)

    def _load_state(self) -> consolidation_seal.ConsolidationSealState:
        try:
            return self._store.load(vault_binding_digest=self.vault_binding_digest)
        except consolidation_seal.ConsolidationSealUnavailable:
            _fail("CONSOLIDATION_SEAL_UNAVAILABLE")

    @staticmethod
    def _snapshot_from(
        state: consolidation_seal.ConsolidationSealState,
        participants: Sequence[_Participant],
    ) -> ConsolidationAdmissionSnapshot:
        counts = {kind: 0 for kind in _PARTICIPANT_KINDS}
        for participant in participants:
            if participant.kind in counts:
                counts[participant.kind] += 1
        return ConsolidationAdmissionSnapshot(
            state=state,
            active_reads=counts["read"],
            active_mutations=counts["mutation"],
            active_transfers=counts["transfer"],
            active_background=counts["background"],
            draining=state.kind == "consolidation-sealed" and state.phase == "sealing",
        )

    def snapshot(self) -> ConsolidationAdmissionSnapshot:
        try:
            with self._gate().hold(
                operation="admission_snapshot",
                holder_kind="reserved-state",
                publish_holder_metadata=False,
            ):
                return self._snapshot_from(self._load_state(), self._load_participants())
        except OpError:
            _fail("CONSOLIDATION_ADMISSION_UNAVAILABLE")

    def reload(self) -> ConsolidationAdmissionSnapshot:
        """Reconcile durable seal and participants before restart readiness."""

        return self.snapshot()

    @contextmanager
    def _admit(self, kind: str) -> Iterator[ConsolidationMutationAdmission | None]:
        if kind not in _PARTICIPANT_KINDS:
            raise ValueError("unknown consolidation admission participant")
        participant_id = uuid.uuid4().hex
        participant = _Participant(participant_id, kind, self._state_domain_digest)
        lease = _HandleLease()
        mutation = (
            ConsolidationMutationAdmission(self, participant_id, lease, _HANDLE_SEAL)
            if kind == "mutation"
            else None
        )
        lock = self._participant_lock(participant_id, timeout=5.0)
        published = False
        try:
            with lock.hold(
                request_id=participant_id,
                operation=f"admit_{kind}",
                holder_kind="reserved-state",
                publish_holder_metadata=False,
            ):
                try:
                    with self._gate().hold(
                        request_id=participant_id,
                        operation=f"admit_{kind}",
                        holder_kind="reserved-state",
                        publish_holder_metadata=False,
                    ):
                        if self._load_state().kind != "open":
                            _fail("CONSOLIDATION_SEALED")
                        if any(current.kind == "control" for current in self._load_participants()):
                            _fail("CONSOLIDATION_CONTROL_PENDING")
                        self._publish_participant(participant)
                        published = True
                    # Another configured coordination root cannot share this
                    # gate. Rechecking the seal closes that publication race.
                    if self._load_state().kind != "open":
                        self._remove_participant(participant_id)
                        published = False
                        _fail("CONSOLIDATION_SEALED")
                    yield mutation
                finally:
                    if published and (mutation is None or not mutation._is_converted()):
                        self._remove_participant(participant_id)
                    if mutation is not None:
                        mutation._finish()
        except OpError:
            _fail("CONSOLIDATION_ADMISSION_UNAVAILABLE")

    def admit_read(self) -> AbstractContextManager[None]:
        return cast(AbstractContextManager[None], self._admit("read"))

    def admit_mutation(self) -> AbstractContextManager[ConsolidationMutationAdmission]:
        return cast(
            AbstractContextManager[ConsolidationMutationAdmission],
            self._admit("mutation"),
        )

    def admit_transfer(self) -> AbstractContextManager[None]:
        return cast(AbstractContextManager[None], self._admit("transfer"))

    def admit_background(self) -> AbstractContextManager[None]:
        return cast(AbstractContextManager[None], self._admit("background"))

    @staticmethod
    def _control_participant(
        participant_id: str,
        state_domain_digest: str,
        *,
        run_id: str,
        operation_id: str,
        journal_digest: str,
        request_digest: str,
        phase: str,
        action: str,
    ) -> _Participant:
        if not isinstance(request_digest, str) or _DIGEST.fullmatch(request_digest) is None:
            _fail("CONSOLIDATION_CONTROL_UNAVAILABLE")
        if phase != "sealing" or action != "apply":
            _fail("CONSOLIDATION_CONTROL_UNAVAILABLE")
        return _Participant(
            participant_id,
            "control",
            state_domain_digest,
            run_id,
            operation_id,
            journal_digest,
            request_digest,
            phase,
            action,
        )

    @staticmethod
    def _require_control_authority(
        authority: object,
        *,
        vault_binding_digest: str,
        run_id: str,
        operation_id: str,
        journal_digest: str,
        phase: str,
        action: str,
    ) -> None:
        try:
            consolidation_authority.require_authority(
                authority,
                vault_binding_digest=vault_binding_digest,
                run_id=run_id,
                operation_id=operation_id,
                journal_digest=journal_digest,
                phase=phase,
                action=action,
            )
        except consolidation_authority.ConsolidationAuthorityUnavailable:
            _fail("CONSOLIDATION_AUTHORITY_UNAVAILABLE")

    def convert_control_mutation(
        self,
        mutation: object,
        *,
        authority: object,
        run_id: str,
        operation_id: str,
        journal_digest: str,
        request_digest: str,
        phase: str,
        action: str,
    ) -> ConsolidationControlAdmission:
        """Atomically replace this operation's ordinary count with durable control."""

        self._require_control_authority(
            authority,
            vault_binding_digest=self.vault_binding_digest,
            run_id=run_id,
            operation_id=operation_id,
            journal_digest=journal_digest,
            phase=phase,
            action=action,
        )
        if type(mutation) is not ConsolidationMutationAdmission:
            _fail("CONSOLIDATION_CONTROL_UNAVAILABLE")
        participant_id = mutation._participant(self)
        ordinary = _Participant(participant_id, "mutation", self._state_domain_digest)
        control = self._control_participant(
            participant_id,
            self._state_domain_digest,
            run_id=run_id,
            operation_id=operation_id,
            journal_digest=journal_digest,
            request_digest=request_digest,
            phase=phase,
            action=action,
        )
        try:
            with self._gate().hold(
                request_id=participant_id,
                operation="convert_control_mutation",
                holder_kind="reserved-state",
                publish_holder_metadata=False,
            ):
                if self._load_state().kind != "open":
                    _fail("CONSOLIDATION_SEALED")
                participants = self._load_participants()
                if ordinary not in participants:
                    _fail("CONSOLIDATION_CONTROL_UNAVAILABLE")
                if any(current.kind == "control" for current in participants):
                    _fail("CONSOLIDATION_CONTROL_CONFLICT")
                try:
                    self._publish_participant(
                        control,
                        expected_raw=self._participant_bytes(ordinary),
                    )
                except ConsolidationAdmissionUnavailable:
                    # Publication is durable before its catalogue bookkeeping.
                    # Adopt an exact committed control after a lost acknowledgement;
                    # otherwise retain ambiguous evidence rather than deleting a
                    # possibly committed control in the admission context cleanup.
                    try:
                        current = self._load_participant(participant_id)
                    except ConsolidationAdmissionUnavailable:
                        mutation._convert()
                        raise
                    if current == control:
                        mutation._convert()
                    elif current == ordinary:
                        raise
                    else:
                        mutation._convert()
                        _fail("CONSOLIDATION_CONTROL_CONFLICT")
                else:
                    mutation._convert()
        except OpError:
            _fail("CONSOLIDATION_ADMISSION_UNAVAILABLE")
        return ConsolidationControlAdmission(
            self,
            control,
            mutation._lease(),
            _HANDLE_SEAL,
        )

    def _matching_control(
        self,
        *,
        run_id: str,
        operation_id: str,
        journal_digest: str,
        request_digest: str,
        phase: str,
        action: str,
    ) -> _Participant:
        controls = tuple(
            participant
            for participant in self._load_participants()
            if participant.kind == "control"
        )
        if len(controls) != 1:
            _fail("CONSOLIDATION_CONTROL_UNAVAILABLE")
        current = controls[0]
        if current.state_domain_digest != self._state_domain_digest:
            _fail("CONSOLIDATION_ADMISSION_DOMAIN_CONFLICT")
        expected = self._control_participant(
            current.participant_id,
            current.state_domain_digest,
            run_id=run_id,
            operation_id=operation_id,
            journal_digest=journal_digest,
            request_digest=request_digest,
            phase=phase,
            action=action,
        )
        if current != expected:
            _fail("CONSOLIDATION_CONTROL_CONFLICT")
        return current

    def _require_durable_control(self, expected: _Participant) -> None:
        controls = tuple(
            participant
            for participant in self._load_participants()
            if participant.kind == "control"
        )
        if not controls:
            _fail("CONSOLIDATION_CONTROL_UNAVAILABLE")
        if len(controls) != 1 or controls[0] != expected:
            _fail("CONSOLIDATION_CONTROL_CONFLICT")
        if controls[0].state_domain_digest != self._state_domain_digest:
            _fail("CONSOLIDATION_ADMISSION_DOMAIN_CONFLICT")

    @contextmanager
    def resume_control_mutation(
        self,
        *,
        authority: object,
        run_id: str,
        operation_id: str,
        journal_digest: str,
        request_digest: str,
        phase: str,
        action: str,
    ) -> Iterator[ConsolidationControlAdmission]:
        """Recover one exact converted operation without reopening ordinary work."""

        self._require_control_authority(
            authority,
            vault_binding_digest=self.vault_binding_digest,
            run_id=run_id,
            operation_id=operation_id,
            journal_digest=journal_digest,
            phase=phase,
            action=action,
        )
        try:
            with self._gate().hold(
                operation="resume_control_lookup",
                holder_kind="reserved-state",
                publish_holder_metadata=False,
            ):
                current = self._matching_control(
                    run_id=run_id,
                    operation_id=operation_id,
                    journal_digest=journal_digest,
                    request_digest=request_digest,
                    phase=phase,
                    action=action,
                )
                state = self._load_state()
                if state.kind == "deletion-sealed":
                    _fail("CONSOLIDATION_CONTROL_UNAVAILABLE")
                if state.kind == "consolidation-sealed" and (
                    state.run_id != run_id
                    or state.operation_id != operation_id
                    or state.journal_digest != journal_digest
                ):
                    _fail("CONSOLIDATION_CONTROL_CONFLICT")
            with self._participant_lock(current.participant_id, timeout=5.0).hold(
                request_id=current.participant_id,
                operation="resume_control_mutation",
                holder_kind="reserved-state",
                publish_holder_metadata=False,
            ):
                with self._gate().hold(
                    request_id=current.participant_id,
                    operation="resume_control_recheck",
                    holder_kind="reserved-state",
                    publish_holder_metadata=False,
                ):
                    current = self._matching_control(
                        run_id=run_id,
                        operation_id=operation_id,
                        journal_digest=journal_digest,
                        request_digest=request_digest,
                        phase=phase,
                        action=action,
                    )
                lease = _HandleLease()
                control = ConsolidationControlAdmission(
                    self,
                    current,
                    lease,
                    _HANDLE_SEAL,
                )
                try:
                    yield control
                finally:
                    control._finish()
        except OpError:
            _fail("CONSOLIDATION_CONTROL_BUSY")

    def _require_control(
        self,
        control: object,
        *,
        run_id: str,
        operation_id: str,
        journal_digest: str,
    ) -> _Participant:
        if type(control) is not ConsolidationControlAdmission:
            _fail("CONSOLIDATION_CONTROL_UNAVAILABLE")
        bound = control._participant_for(self)
        if bound.request_digest is None:
            _fail("CONSOLIDATION_CONTROL_UNAVAILABLE")
        current = self._matching_control(
            run_id=run_id,
            operation_id=operation_id,
            journal_digest=journal_digest,
            request_digest=bound.request_digest,
            phase="sealing",
            action="apply",
        )
        if not control._matches(self, current):
            _fail("CONSOLIDATION_CONTROL_UNAVAILABLE")
        return current

    @staticmethod
    def _remaining(deadline: float) -> float:
        return max(0.0, deadline - time.monotonic())

    def _stop_background(
        self,
        stopper: Callable[[], object],
        *,
        deadline: float,
    ) -> None:
        remaining = self._remaining(deadline)
        if remaining <= 0:
            _fail("CONSOLIDATION_DRAIN_TIMEOUT")
        stopped = Event()
        failure: list[BaseException] = []

        def run() -> None:
            try:
                stopper()
            except BaseException as error:  # noqa: BLE001 - retain no private detail
                failure.append(error)
            finally:
                stopped.set()

        Thread(
            target=run,
            name="exomem-consolidation-background-stop",
            daemon=True,
        ).start()
        if not stopped.wait(remaining):
            _fail("CONSOLIDATION_DRAIN_TIMEOUT")
        if failure:
            _fail("CONSOLIDATION_DRAIN_FAILED")

    def _wait_participant(self, participant: _Participant, *, deadline: float) -> None:
        if participant.state_domain_digest == self._state_domain_digest:
            try:
                with self._participant_lock(
                    participant.participant_id,
                    timeout=self._remaining(deadline),
                ).hold(
                    request_id=participant.participant_id,
                    operation="drain_participant",
                    holder_kind="reserved-state",
                    publish_holder_metadata=False,
                ):
                    self._remove_participant(participant.participant_id)
            except OpError:
                _fail("CONSOLIDATION_DRAIN_TIMEOUT")
            return

        # Another external coordination domain cannot prove this participant's
        # liveness or released lock. Preserve its evidence and require the later
        # cross-replica lifecycle/fencing layer to resolve the configuration.
        _fail("CONSOLIDATION_ADMISSION_DOMAIN_CONFLICT")

    def begin_seal(
        self,
        *,
        control: object,
        authority: object,
        run_id: str,
        operation_id: str,
        journal_digest: str,
        sealed_at: str,
        expected_revision: int,
        timeout: float = 5.0,
    ) -> ConsolidationAdmissionSnapshot:
        """Persist the exact seal intent before any participant is drained."""

        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(float(timeout))
            or timeout < 0
        ):
            raise ValueError("consolidation seal timeout must be finite and non-negative")

        try:
            consolidation_authority.require_authority(
                authority,
                vault_binding_digest=self.vault_binding_digest,
                run_id=run_id,
                operation_id=operation_id,
                journal_digest=journal_digest,
                phase="sealing",
                action="apply",
            )
        except consolidation_authority.ConsolidationAuthorityUnavailable:
            _fail("CONSOLIDATION_AUTHORITY_UNAVAILABLE")
        control_participant = self._require_control(
            control,
            run_id=run_id,
            operation_id=operation_id,
            journal_digest=journal_digest,
        )
        if (
            type(expected_revision) is not int
            or expected_revision < 0
            or expected_revision > _MAX_SAFE_INTEGER
        ):
            _fail("CONSOLIDATION_SEAL_UNAVAILABLE")
        deadline = time.monotonic() + float(timeout)
        control_id = operation_id.replace("-", "")
        try:
            with self._coordinator(
                "control",
                control_id,
                timeout=self._remaining(deadline),
            ).hold(
                request_id=control_id,
                operation="seal_intent",
                holder_kind="reserved-state",
                publish_holder_metadata=False,
            ):
                with self._gate(timeout=self._remaining(deadline)).hold(
                    request_id=control_id,
                    operation="seal_intent",
                    holder_kind="reserved-state",
                    publish_holder_metadata=False,
                ):
                    self._require_durable_control(control_participant)
                    current = self._load_state()
                    if current.kind == "consolidation-sealed":
                        if (
                            current.phase == "sealing"
                            and current.run_id == run_id
                            and current.operation_id == operation_id
                            and current.journal_digest == journal_digest
                            and current.sealed_at == sealed_at
                            and current.revision == expected_revision + 1
                        ):
                            return self._snapshot_from(
                                current,
                                self._load_participants(),
                            )
                        _fail("CONSOLIDATION_SEAL_UNAVAILABLE")
                    try:
                        state = self._store.begin_consolidation(
                            vault_binding_digest=self.vault_binding_digest,
                            run_id=run_id,
                            operation_id=operation_id,
                            journal_digest=journal_digest,
                            sealed_at=sealed_at,
                            expected_revision=expected_revision,
                        )
                    except consolidation_seal.ConsolidationSealUnavailable:
                        _fail("CONSOLIDATION_SEAL_UNAVAILABLE")
                    return self._snapshot_from(state, self._load_participants())
        except OpError:
            _fail("CONSOLIDATION_DRAIN_BUSY")

    def drain_and_seal(
        self,
        *,
        control: object,
        authority: object,
        run_id: str,
        operation_id: str,
        journal_digest: str,
        sealed_at: str,
        completed_at: str,
        expected_revision: int,
        timeout: float,
        stoppers: Sequence[Callable[[], object]] = (),
    ) -> ConsolidationAdmissionSnapshot:
        """Drain every non-control participant, then persist the sealed terminal."""

        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(float(timeout))
            or timeout < 0
        ):
            raise ValueError("consolidation drain timeout must be finite and non-negative")
        if not all(callable(stopper) for stopper in stoppers):
            raise TypeError("consolidation background stoppers must be callable")
        try:
            consolidation_authority.require_authority(
                authority,
                vault_binding_digest=self.vault_binding_digest,
                run_id=run_id,
                operation_id=operation_id,
                journal_digest=journal_digest,
                phase="sealing",
                action="apply",
            )
        except consolidation_authority.ConsolidationAuthorityUnavailable:
            _fail("CONSOLIDATION_AUTHORITY_UNAVAILABLE")
        control_participant = self._require_control(
            control,
            run_id=run_id,
            operation_id=operation_id,
            journal_digest=journal_digest,
        )
        if (
            type(expected_revision) is not int
            or expected_revision < 0
            or expected_revision > _MAX_SAFE_INTEGER
        ):
            _fail("CONSOLIDATION_SEAL_UNAVAILABLE")
        deadline = time.monotonic() + float(timeout)
        control_id = operation_id.replace("-", "")
        try:
            with self._coordinator(
                "control",
                control_id,
                timeout=self._remaining(deadline),
            ).hold(
                request_id=control_id,
                operation="seal_drain",
                holder_kind="reserved-state",
                publish_holder_metadata=False,
            ):
                current = self._load_state()
                if current.kind == "consolidation-sealed" and current.phase == "sealed":
                    if (
                        current.run_id == run_id
                        and current.operation_id == operation_id
                        and current.journal_digest == journal_digest
                        and current.sealed_at == sealed_at
                        and current.recorded_at == completed_at
                        and current.revision == expected_revision + 1
                    ):
                        return self._snapshot_from(current, ())
                    _fail("CONSOLIDATION_SEAL_UNAVAILABLE")
                if (
                    current.kind != "consolidation-sealed"
                    or current.phase != "sealing"
                    or current.run_id != run_id
                    or current.operation_id != operation_id
                    or current.journal_digest != journal_digest
                    or current.sealed_at != sealed_at
                    or current.revision != expected_revision
                ):
                    _fail("CONSOLIDATION_SEAL_UNAVAILABLE")

                for stopper in tuple(stoppers):
                    self._stop_background(stopper, deadline=deadline)
                while True:
                    participants = tuple(
                        participant
                        for participant in self._load_participants()
                        if participant != control_participant
                    )
                    if not participants:
                        break
                    for participant in participants:
                        self._wait_participant(participant, deadline=deadline)

                with self._gate(timeout=self._remaining(deadline)).hold(
                    request_id=control_id,
                    operation="seal_terminal",
                    holder_kind="reserved-state",
                    publish_holder_metadata=False,
                ):
                    self._require_durable_control(control_participant)
                    if any(
                        participant.kind != "control"
                        for participant in self._load_participants()
                    ):
                        _fail("CONSOLIDATION_DRAIN_BUSY")
                    try:
                        state = self._store.advance_consolidation(
                            authority,
                            vault_binding_digest=self.vault_binding_digest,
                            action="apply",
                            target_phase="sealed",
                            recorded_at=completed_at,
                            expected_revision=expected_revision,
                        )
                    except consolidation_seal.ConsolidationSealUnavailable:
                        _fail("CONSOLIDATION_SEAL_UNAVAILABLE")
        except OpError:
            if self._load_state().kind == "consolidation-sealed":
                _fail("CONSOLIDATION_DRAIN_TIMEOUT")
            _fail("CONSOLIDATION_DRAIN_BUSY")
        return self._snapshot_from(state, ())

    def seal_and_drain(
        self,
        *,
        control: object,
        authority: object,
        run_id: str,
        operation_id: str,
        journal_digest: str,
        sealed_at: str,
        completed_at: str,
        expected_revision: int,
        timeout: float,
        stoppers: Sequence[Callable[[], object]] = (),
    ) -> ConsolidationAdmissionSnapshot:
        """Persist intent, close admission, drain every process, then seal."""
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(float(timeout))
            or timeout < 0
        ):
            raise ValueError("consolidation drain timeout must be finite and non-negative")
        if not all(callable(stopper) for stopper in stoppers):
            raise TypeError("consolidation background stoppers must be callable")
        if (
            type(expected_revision) is not int
            or expected_revision < 0
            or expected_revision > _MAX_SAFE_INTEGER
        ):
            _fail("CONSOLIDATION_SEAL_UNAVAILABLE")
        deadline = time.monotonic() + float(timeout)
        current = self._load_state()
        if current.kind == "consolidation-sealed" and current.phase == "sealed":
            return self.drain_and_seal(
                control=control,
                authority=authority,
                run_id=run_id,
                operation_id=operation_id,
                journal_digest=journal_digest,
                sealed_at=sealed_at,
                completed_at=completed_at,
                expected_revision=expected_revision + 1,
                timeout=self._remaining(deadline),
                stoppers=stoppers,
            )
        sealing = self.begin_seal(
            control=control,
            authority=authority,
            run_id=run_id,
            operation_id=operation_id,
            journal_digest=journal_digest,
            sealed_at=sealed_at,
            expected_revision=expected_revision,
            timeout=self._remaining(deadline),
        )
        return self.drain_and_seal(
            control=control,
            authority=authority,
            run_id=run_id,
            operation_id=operation_id,
            journal_digest=journal_digest,
            sealed_at=sealed_at,
            completed_at=completed_at,
            expected_revision=sealing.state.revision,
            timeout=self._remaining(deadline),
            stoppers=stoppers,
        )


__all__ = [
    "ConsolidationAdmission",
    "ConsolidationAdmissionSnapshot",
    "ConsolidationAdmissionUnavailable",
    "ConsolidationControlAdmission",
    "ConsolidationMutationAdmission",
]
