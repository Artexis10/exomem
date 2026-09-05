"""Explicit offline enrollment into the durable consolidation seal lifecycle."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from .. import writer_lease
from ..cli_ops import OpError
from ..mutation_lock import VaultMutationCoordinator, canonical_mutation_identity
from . import consolidation_identity, consolidation_seal
from .principal import OWNER_AUDIENCE, RequestPrincipal

_DEFAULT_SLOTS = 64
_MAX_SLOTS = 1024
_PROCESS_STATE_LOCK = threading.Lock()
_PROCESS_ACTIVE_SLOTS: dict[str, set[int]] = {}
_PROCESS_OFFLINE: set[str] = set()
_LOCAL_ENROLLMENT_ISSUERS = frozenset({"cli-local-owner", "library-local-owner"})


@dataclass
class _CliRuntimeScope:
    owner_thread_id: int
    stack: ExitStack = field(default_factory=ExitStack)
    vaults: set[str] = field(default_factory=set)


_CLI_RUNTIME_SCOPE: ContextVar[_CliRuntimeScope | None] = ContextVar(
    "exomem_consolidation_cli_runtime_scope",
    default=None,
)
_CLI_PROCESS_SCOPE_LOCK = threading.Lock()
_CLI_PROCESS_SCOPE: _CliRuntimeScope | None = None


class ConsolidationEnrollmentUnavailable(RuntimeError):
    """Stable content-free refusal to enroll a live or invalid vault."""

    def __init__(self, code: str = "CONSOLIDATION_ENROLLMENT_UNAVAILABLE") -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str = "CONSOLIDATION_ENROLLMENT_UNAVAILABLE") -> NoReturn:
    raise ConsolidationEnrollmentUnavailable(code) from None


def _lock_failure(error: OpError) -> NoReturn:
    if error.code == "MUTATION_BUSY":
        _fail("CONSOLIDATION_ENROLLMENT_BUSY")
    _fail()


def _recorded_at(now: int) -> str:
    if type(now) is not int or now < 1:
        _fail()
    try:
        return (
            datetime.fromtimestamp(now, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):
        _fail()


class LocalRuntimePresenceRegistry:
    """Cross-process runtime slots plus one exclusive offline enrollment gate."""

    def __init__(
        self,
        vault_root: Path | str,
        *,
        state_root: Path | str | None = None,
        slots: int = _DEFAULT_SLOTS,
    ) -> None:
        if type(slots) is not int or not 1 <= slots <= _MAX_SLOTS:
            raise ValueError("runtime presence slots must be between 1 and 1024")
        self.vault_root = Path(vault_root).absolute()
        self.state_root = (
            Path(state_root)
            if state_root is not None
            else writer_lease.active_manager().config.state_dir
        )
        canonical = canonical_mutation_identity(self.vault_root)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self._key = digest
        self._slots = slots
        self._gate = self._coordinator("gate")

    def _coordinator(self, suffix: str) -> VaultMutationCoordinator:
        return VaultMutationCoordinator(
            self.state_root,
            f"consolidation-runtime-presence:{self._key}:{suffix}",
            long_holder_seconds=10 * 365 * 24 * 60 * 60,
        )

    @staticmethod
    def _hold(coordinator: VaultMutationCoordinator, *, timeout_seconds: float):
        return coordinator.hold(
            timeout_seconds=timeout_seconds,
            operation="consolidation-presence",
            holder_kind="runtime-presence",
            publish_holder_metadata=False,
        )

    @contextmanager
    def runtime_presence(self, *, timeout_seconds: float = 5.0) -> Iterator[None]:
        """Register one server/direct invocation without holding the gate."""

        slot_index: int | None = None
        slot_hold = None
        try:
            with _PROCESS_STATE_LOCK:
                if self._key in _PROCESS_OFFLINE:
                    _fail("CONSOLIDATION_ENROLLMENT_BUSY")
            try:
                gate = self._hold(self._gate, timeout_seconds=timeout_seconds)
                gate.__enter__()
            except OpError as error:
                _lock_failure(error)
            try:
                with _PROCESS_STATE_LOCK:
                    if self._key in _PROCESS_OFFLINE:
                        _fail("CONSOLIDATION_ENROLLMENT_BUSY")
                    local_slots = _PROCESS_ACTIVE_SLOTS.setdefault(self._key, set())
                    candidates = tuple(
                        index for index in range(self._slots) if index not in local_slots
                    )
                for index in candidates:
                    candidate = self._hold(
                        self._coordinator(f"slot:{index}"),
                        timeout_seconds=0,
                    )
                    try:
                        candidate.__enter__()
                    except OpError as error:
                        if error.code == "MUTATION_BUSY":
                            continue
                        _lock_failure(error)
                    slot_index = index
                    slot_hold = candidate
                    with _PROCESS_STATE_LOCK:
                        _PROCESS_ACTIVE_SLOTS.setdefault(self._key, set()).add(index)
                    break
                if slot_hold is None:
                    _fail("CONSOLIDATION_ENROLLMENT_BUSY")
            finally:
                gate.__exit__(None, None, None)
            yield
        finally:
            if slot_hold is not None:
                slot_hold.__exit__(None, None, None)
            if slot_index is not None:
                with _PROCESS_STATE_LOCK:
                    active = _PROCESS_ACTIVE_SLOTS.get(self._key)
                    if active is not None:
                        active.discard(slot_index)
                        if not active:
                            _PROCESS_ACTIVE_SLOTS.pop(self._key, None)

    @contextmanager
    def offline_enrollment(self, *, timeout_seconds: float = 0.0) -> Iterator[None]:
        """Exclude new registrations and prove every prior slot has drained."""

        try:
            gate = self._hold(self._gate, timeout_seconds=timeout_seconds)
            gate.__enter__()
        except OpError as error:
            _lock_failure(error)
        offline_marked = False
        try:
            with _PROCESS_STATE_LOCK:
                if self._key in _PROCESS_OFFLINE or _PROCESS_ACTIVE_SLOTS.get(self._key):
                    _fail("CONSOLIDATION_ENROLLMENT_BUSY")
                _PROCESS_OFFLINE.add(self._key)
                offline_marked = True
            for index in range(self._slots):
                probe = self._hold(
                    self._coordinator(f"slot:{index}"),
                    timeout_seconds=0,
                )
                try:
                    probe.__enter__()
                except OpError as error:
                    _lock_failure(error)
                else:
                    probe.__exit__(None, None, None)
            yield
        finally:
            if offline_marked:
                with _PROCESS_STATE_LOCK:
                    _PROCESS_OFFLINE.discard(self._key)
            gate.__exit__(None, None, None)


@contextmanager
def local_runtime_presence(vault_root: Path | str) -> Iterator[None]:
    """Hold the default local presence slot for one runtime interval."""

    with LocalRuntimePresenceRegistry(vault_root).runtime_presence():
        yield


@contextmanager
def cli_runtime_scope() -> Iterator[None]:
    """Retain direct-CLI presence until all process-bound work has drained."""

    global _CLI_PROCESS_SCOPE

    current = _CLI_RUNTIME_SCOPE.get()
    if current is not None:
        yield
        return
    scope = _CliRuntimeScope(owner_thread_id=threading.get_ident())
    with _CLI_PROCESS_SCOPE_LOCK:
        if _CLI_PROCESS_SCOPE is not None:
            raise RuntimeError("CLI runtime scope is already active")
        _CLI_PROCESS_SCOPE = scope
    token = _CLI_RUNTIME_SCOPE.set(scope)
    try:
        yield
    finally:
        try:
            with _CLI_PROCESS_SCOPE_LOCK:
                if _CLI_PROCESS_SCOPE is scope:
                    _CLI_PROCESS_SCOPE = None
            scope.stack.close()
        finally:
            _CLI_RUNTIME_SCOPE.reset(token)


def ensure_cli_runtime_presence(vault_root: Path | str) -> bool:
    """Enter one vault slot in the active direct-CLI lifetime, if present."""

    scope = _CLI_RUNTIME_SCOPE.get()
    if scope is None:
        with _CLI_PROCESS_SCOPE_LOCK:
            process_scope = _CLI_PROCESS_SCOPE
        if (
            process_scope is not None
            and process_scope.owner_thread_id == threading.get_ident()
        ):
            scope = process_scope
    if scope is None:
        return False
    root = Path(vault_root).absolute()
    key = canonical_mutation_identity(root)
    if key not in scope.vaults:
        scope.stack.enter_context(local_runtime_presence(root))
        scope.vaults.add(key)
    return True


@contextmanager
def invocation_runtime_presence(vault_root: Path | str) -> Iterator[None]:
    """Reuse direct-CLI lifetime presence or hold one invocation-local slot."""

    if ensure_cli_runtime_presence(vault_root):
        yield
        return
    with local_runtime_presence(vault_root):
        yield


def _require_local_owner(who: RequestPrincipal) -> None:
    if (
        not isinstance(who, RequestPrincipal)
        or not who.resolved
        or who.audience_id != OWNER_AUDIENCE
        or who.issuer_family not in _LOCAL_ENROLLMENT_ISSUERS
    ):
        _fail()


def _initialize_exact(
    vault_root: Path,
    *,
    identity: Any,
    now: int,
) -> consolidation_seal.ConsolidationSealState:
    store = consolidation_seal.ConsolidationSealStore(vault_root)
    try:
        existing = store.load_optional()
    except consolidation_seal.ConsolidationSealUnavailable as error:
        if error.code != "SEAL_STORE_CORRUPT":
            raise
        existing = None
    if existing is None:
        state = store.initialize_open(
            vault_binding_digest=identity.record_digest,
            recorded_at=_recorded_at(now),
        )
    else:
        if (
            existing.kind != "open"
            or existing.vault_binding_digest != identity.record_digest
        ):
            _fail()
        state = existing
    loaded = store.load(vault_binding_digest=identity.record_digest)
    if loaded != state:
        _fail()
    return loaded


def enroll_local(
    vault_root: Path | str,
    *,
    principal: RequestPrincipal,
    now: int,
    state_root: Path | str | None = None,
) -> consolidation_seal.ConsolidationSealState:
    """Enroll an already authenticated local identity while all runtimes are absent."""

    _require_local_owner(principal)
    root = Path(vault_root).absolute()
    registry = LocalRuntimePresenceRegistry(root, state_root=state_root)
    try:
        with registry.offline_enrollment():
            identity = consolidation_identity.load_local_identity(root, now=now)
            state = _initialize_exact(root, identity=identity, now=now)
            reloaded = consolidation_identity.load_local_identity(root, now=now)
            if reloaded.record_digest != identity.record_digest:
                _fail()
            return state
    except ConsolidationEnrollmentUnavailable:
        raise
    except (
        consolidation_identity.ConsolidationIdentityUnavailable,
        consolidation_seal.ConsolidationSealUnavailable,
        OSError,
        RuntimeError,
        ValueError,
    ):
        _fail()


def enroll_hosted_locked(
    binding: Any,
    *,
    custody: Any,
    now: int,
) -> consolidation_seal.ConsolidationSealState:
    """Enroll under the Hosted lifetime lock already held by provisioning."""

    root = Path(binding.vault_root).absolute()
    try:
        identity = consolidation_identity.load_hosted_identity(
            binding,
            custody=custody,
            now=now,
        )
        state = _initialize_exact(root, identity=identity, now=now)
        reloaded = consolidation_identity.load_hosted_identity(
            binding,
            custody=custody,
            now=now,
        )
        if reloaded.record_digest != identity.record_digest:
            _fail()
        return state
    except ConsolidationEnrollmentUnavailable:
        raise
    except (
        consolidation_identity.ConsolidationIdentityUnavailable,
        consolidation_seal.ConsolidationSealUnavailable,
        OSError,
        RuntimeError,
        ValueError,
    ):
        _fail()


def enroll_hosted(
    binding: Any,
    *,
    custody: Any,
    now: int,
) -> consolidation_seal.ConsolidationSealState:
    """Acquire Hosted lifetime exclusion and enroll an authenticated identity."""

    from ..hosted_operator import OperatorFailure
    from ..hosted_restore import acquire_hosted_lifetime_lock

    try:
        with acquire_hosted_lifetime_lock(binding.state_root, binding=binding):
            return enroll_hosted_locked(binding, custody=custody, now=now)
    except ConsolidationEnrollmentUnavailable:
        raise
    except OperatorFailure as error:
        if error.code == "HOSTED_RESTORE_BUSY":
            _fail("CONSOLIDATION_ENROLLMENT_BUSY")
        _fail()
