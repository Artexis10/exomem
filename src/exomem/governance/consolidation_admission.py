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
from typing import NoReturn

from .. import held_fs, reserved_paths, writer_lease
from ..cli_ops import OpError
from ..mutation_lock import VaultMutationCoordinator
from . import consolidation_authority, consolidation_plan, consolidation_seal

_DESCRIPTOR_ID = "consolidation-tree"
_OWNER = "consolidation.run"
_PARTICIPANT_SCHEMA = "exomem.consolidation-admission-participant/v1"
_PARTICIPANT_KINDS = frozenset({"read", "mutation", "transfer", "background"})
_PARTICIPANT_FIELDS = frozenset(
    {"schema", "participant_id", "kind", "state_domain_digest"}
)
_PARTICIPANT_NAME = re.compile(r"([0-9a-f]{32})\.json\Z")
_MAX_PARTICIPANT_BYTES = 1024


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
            self.vault_root
            / "Knowledge Base"
            / "_Consolidation"
            / "admission"
            / "participants"
        )
        try:
            self._state_root = Path(
                writer_lease.active_manager().config.state_dir
            ).expanduser().resolve(strict=False)
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
        boundary = (
            "consolidation-admission:"
            f"{self.vault_binding_digest}:{namespace}:{identity}"
        )
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
        return consolidation_plan.canonical_closed_jcs(
            {
                "schema": _PARTICIPANT_SCHEMA,
                "participant_id": participant.participant_id,
                "kind": participant.kind,
                "state_domain_digest": participant.state_domain_digest,
            }
        )

    @staticmethod
    def _parse_participant(raw: bytes, *, expected_id: str) -> _Participant:
        try:
            parsed = consolidation_plan._parse_canonical_mapping(  # noqa: SLF001
                raw,
                maximum=_MAX_PARTICIPANT_BYTES,
            )
            if frozenset(parsed) != _PARTICIPANT_FIELDS:
                raise ValueError
            if consolidation_plan.canonical_closed_jcs(parsed) != raw:
                raise ValueError
            participant_id = parsed["participant_id"]
            kind = parsed["kind"]
            state_domain_digest = parsed["state_domain_digest"]
            if participant_id != expected_id:
                raise ValueError
            if not isinstance(kind, str) or kind not in _PARTICIPANT_KINDS:
                raise ValueError
            if (
                not isinstance(state_domain_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", state_domain_digest) is None
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError, consolidation_plan.ConsolidationPlanUnavailable):
            _fail("CONSOLIDATION_ADMISSION_UNAVAILABLE")
        return _Participant(participant_id, kind, state_domain_digest)

    def _participant_path(self, participant_id: str) -> Path:
        return self._participants / f"{participant_id}.json"

    def _publish_participant(self, participant: _Participant) -> None:
        try:
            with _authority(self.vault_root, mutation=True):
                reserved_paths._publish_owner_bytes(  # noqa: SLF001
                    self.vault_root,
                    self._participant_path(participant.participant_id),
                    _DESCRIPTOR_ID,
                    self._participant_bytes(participant),
                    require_missing=True,
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
    def _admit(self, kind: str) -> Iterator[None]:
        if kind not in _PARTICIPANT_KINDS:
            raise ValueError("unknown consolidation admission participant")
        participant_id = uuid.uuid4().hex
        participant = _Participant(participant_id, kind, self._state_domain_digest)
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
                        self._publish_participant(participant)
                        published = True
                    # Another configured coordination root cannot share this
                    # gate. Rechecking the seal closes that publication race.
                    if self._load_state().kind != "open":
                        self._remove_participant(participant_id)
                        published = False
                        _fail("CONSOLIDATION_SEALED")
                    yield
                finally:
                    if published:
                        self._remove_participant(participant_id)
        except OpError:
            _fail("CONSOLIDATION_ADMISSION_UNAVAILABLE")

    def admit_read(self) -> AbstractContextManager[None]:
        return self._admit("read")

    def admit_mutation(self) -> AbstractContextManager[None]:
        return self._admit("mutation")

    def admit_transfer(self) -> AbstractContextManager[None]:
        return self._admit("transfer")

    def admit_background(self) -> AbstractContextManager[None]:
        return self._admit("background")

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

    def seal_and_drain(
        self,
        *,
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

        deadline = time.monotonic() + float(timeout)
        control_id = operation_id.replace("-", "")
        try:
            with self._coordinator(
                "control",
                control_id,
                timeout=self._remaining(deadline),
            ).hold(
                request_id=control_id,
                operation="seal_and_drain",
                holder_kind="reserved-state",
                publish_holder_metadata=False,
            ):
                with self._gate(timeout=self._remaining(deadline)).hold(
                    request_id=control_id,
                    operation="seal_intent",
                    holder_kind="reserved-state",
                    publish_holder_metadata=False,
                ):
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

                for stopper in tuple(stoppers):
                    self._stop_background(stopper, deadline=deadline)

                while True:
                    participants = self._load_participants()
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
                    if self._load_participants():
                        _fail("CONSOLIDATION_DRAIN_BUSY")
                    try:
                        state = self._store.advance_consolidation(
                            authority,
                            vault_binding_digest=self.vault_binding_digest,
                            action="apply",
                            target_phase="sealed",
                            recorded_at=completed_at,
                            expected_revision=state.revision,
                        )
                    except consolidation_seal.ConsolidationSealUnavailable:
                        _fail("CONSOLIDATION_SEAL_UNAVAILABLE")
        except OpError:
            if self._load_state().kind == "consolidation-sealed":
                _fail("CONSOLIDATION_DRAIN_TIMEOUT")
            _fail("CONSOLIDATION_DRAIN_BUSY")
        return self._snapshot_from(state, ())


__all__ = [
    "ConsolidationAdmission",
    "ConsolidationAdmissionSnapshot",
    "ConsolidationAdmissionUnavailable",
]
