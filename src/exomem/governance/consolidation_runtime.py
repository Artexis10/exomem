"""Production admission and readiness wiring for durable consolidation seals."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from ..cli_ops import OpError
from . import (
    consolidation_admission,
    consolidation_authority,
    consolidation_identity,
    consolidation_seal,
)

_BOUND_ADMISSION: ContextVar[consolidation_admission.ConsolidationAdmission | None] = (
    ContextVar("exomem_consolidation_admission", default=None)
)
_BOUND_RESOLVER: ContextVar[
    tuple[Path, Callable[[], consolidation_admission.ConsolidationAdmission | None]] | None
] = ContextVar("exomem_consolidation_admission_resolver", default=None)
_BOUND_VERIFICATION: ContextVar[
    tuple[consolidation_admission.ConsolidationAdmission, object] | None
] = ContextVar("exomem_consolidation_verification_admission", default=None)


class ConsolidationRuntimeUnavailable(RuntimeError):
    """Internal content-free refusal to construct the production boundary."""


def _public_refusal() -> OpError:
    return OpError("VAULT_UNAVAILABLE", "vault is unavailable")


def _local_admission(vault_root: Path) -> consolidation_admission.ConsolidationAdmission | None:
    store = consolidation_seal.ConsolidationSealStore(vault_root)
    try:
        state = store.load_optional()
        if state is None:
            return None
        identity = consolidation_identity.load_local_identity(
            vault_root,
            now=int(time.time()),
        )
        if identity.record_digest != state.vault_binding_digest:
            raise ConsolidationRuntimeUnavailable
        return consolidation_admission.ConsolidationAdmission(
            vault_root,
            vault_binding_digest=identity.record_digest,
            store=store,
        )
    except (
        consolidation_admission.ConsolidationAdmissionUnavailable,
        consolidation_identity.ConsolidationIdentityUnavailable,
        consolidation_seal.ConsolidationSealUnavailable,
        OSError,
        RuntimeError,
        ValueError,
    ) as error:
        raise ConsolidationRuntimeUnavailable from error


def load_hosted_admission(
    binding: Any,
    *,
    custody: Any | None = None,
    now: int | None = None,
) -> consolidation_admission.ConsolidationAdmission | None:
    """Construct the exact Hosted admission only when a seal store exists."""

    vault_root = Path(binding.vault_root).absolute()
    store = consolidation_seal.ConsolidationSealStore(vault_root)
    try:
        state = store.load_optional()
        if state is None:
            return None
        current_time = int(time.time()) if now is None else now
        if custody is None:
            from . import authorization_custody

            custody = authorization_custody.load_authorization_custody(
                vault_root,
                now=current_time,
            )
        identity = consolidation_identity.load_hosted_identity(
            binding,
            custody=custody,
            now=current_time,
        )
        if identity.record_digest != state.vault_binding_digest:
            raise ConsolidationRuntimeUnavailable
        return consolidation_admission.ConsolidationAdmission(
            vault_root,
            vault_binding_digest=identity.record_digest,
            store=store,
        )
    except (
        consolidation_admission.ConsolidationAdmissionUnavailable,
        consolidation_identity.ConsolidationIdentityUnavailable,
        consolidation_seal.ConsolidationSealUnavailable,
        OSError,
        RuntimeError,
        ValueError,
    ) as error:
        raise ConsolidationRuntimeUnavailable from error


@contextmanager
def bind_admission(
    admission: consolidation_admission.ConsolidationAdmission,
) -> Iterator[None]:
    """Bind a trusted Hosted/supervisor-owned admission to this invocation."""

    if not isinstance(admission, consolidation_admission.ConsolidationAdmission):
        raise TypeError("admission must be a ConsolidationAdmission")
    token = _BOUND_ADMISSION.set(admission)
    try:
        yield
    finally:
        _BOUND_ADMISSION.reset(token)


@contextmanager
def bind_hosted_admission(binding: Any) -> Iterator[None]:
    """Bind a trusted Hosted identity resolver without request-supplied facts."""

    vault_root = Path(binding.vault_root).absolute()
    token = _BOUND_RESOLVER.set(
        (vault_root, lambda: load_hosted_admission(binding))
    )
    try:
        yield
    finally:
        _BOUND_RESOLVER.reset(token)


def _require_verification(
    admission: consolidation_admission.ConsolidationAdmission,
    authority: object,
) -> None:
    try:
        state = admission.reload().state
        if state.kind != "consolidation-sealed" or state.phase != "verifying":
            raise ConsolidationRuntimeUnavailable
        consolidation_authority.require_authority(
            authority,
            vault_binding_digest=state.vault_binding_digest,
            run_id=state.run_id,
            operation_id=state.operation_id,
            journal_digest=state.journal_digest,
            phase="verifying",
            action="verify",
        )
    except (
        consolidation_admission.ConsolidationAdmissionUnavailable,
        consolidation_authority.ConsolidationAuthorityUnavailable,
        ConsolidationRuntimeUnavailable,
    ) as error:
        raise ConsolidationRuntimeUnavailable from error


@contextmanager
def bind_verification_admission(
    admission: consolidation_admission.ConsolidationAdmission,
    authority: object,
) -> Iterator[None]:
    """Admit exact in-process verification without widening ordinary traffic."""

    if not isinstance(admission, consolidation_admission.ConsolidationAdmission):
        raise TypeError("admission must be a ConsolidationAdmission")
    _require_verification(admission, authority)
    admission_token = _BOUND_ADMISSION.set(admission)
    verification_token = _BOUND_VERIFICATION.set((admission, authority))
    try:
        yield
    finally:
        _BOUND_VERIFICATION.reset(verification_token)
        _BOUND_ADMISSION.reset(admission_token)


def _resolve_admission(
    vault_root: Path,
) -> consolidation_admission.ConsolidationAdmission | None:
    bound = _BOUND_ADMISSION.get()
    if bound is not None:
        if bound.vault_root != vault_root.absolute():
            raise ConsolidationRuntimeUnavailable
        return bound
    resolver = _BOUND_RESOLVER.get()
    if resolver is not None:
        expected_root, load = resolver
        if expected_root != vault_root.absolute():
            raise ConsolidationRuntimeUnavailable
        return load()
    return _local_admission(vault_root)


@contextmanager
def _admit(vault_root: Path, *, kind: str) -> Iterator[None]:
    try:
        admission = _resolve_admission(Path(vault_root).absolute())
    except ConsolidationRuntimeUnavailable:
        raise _public_refusal() from None
    if admission is None:
        yield
        return
    verification = _BOUND_VERIFICATION.get()
    if verification is not None:
        expected_admission, authority = verification
        try:
            if admission is not expected_admission or kind != "read":
                raise ConsolidationRuntimeUnavailable
            _require_verification(admission, authority)
        except ConsolidationRuntimeUnavailable:
            raise _public_refusal() from None
        yield
        return
    try:
        if kind == "read":
            boundary = admission.admit_read()
        elif kind == "mutation":
            boundary = admission.admit_mutation()
        elif kind == "transfer":
            boundary = admission.admit_transfer()
        elif kind == "background":
            boundary = admission.admit_background()
        else:  # pragma: no cover - only closed wrappers below call this helper
            raise AssertionError(f"unknown consolidation admission kind: {kind}")
        with boundary:
            yield
    except consolidation_admission.ConsolidationAdmissionUnavailable:
        raise _public_refusal() from None


@contextmanager
def admit_command(vault_root: Path, *, read_only: bool) -> Iterator[None]:
    """Apply one content-free outer boundary before command dispatch."""

    with _admit(vault_root, kind="read" if read_only else "mutation"):
        yield


@contextmanager
def admit_mutation(vault_root: Path) -> Iterator[None]:
    """Apply the outer boundary to a non-command mutation surface."""

    with _admit(vault_root, kind="mutation"):
        yield


@contextmanager
def admit_transfer(vault_root: Path) -> Iterator[None]:
    """Apply the outer boundary to an upload or download lifetime."""

    with _admit(vault_root, kind="transfer"):
        yield


@contextmanager
def admit_upload(vault_root: Path) -> Iterator[None]:
    """Hold both transfer and mutation admission for an upload lifetime."""

    with admit_transfer(vault_root):
        with admit_mutation(vault_root):
            yield


@contextmanager
def admit_background(vault_root: Path) -> Iterator[None]:
    """Apply the outer boundary to one background operation."""

    with _admit(vault_root, kind="background"):
        yield


def readiness(
    vault_root: Path,
    *,
    admission: consolidation_admission.ConsolidationAdmission | None = None,
) -> dict[str, bool]:
    """Return phase-free public admission readiness from durable state."""

    try:
        resolved = admission if admission is not None else _resolve_admission(vault_root)
        admitted = resolved is None or resolved.reload().state.kind == "open"
    except (
        ConsolidationRuntimeUnavailable,
        consolidation_admission.ConsolidationAdmissionUnavailable,
        OSError,
        RuntimeError,
        ValueError,
    ):
        admitted = False
    return {"admitted": admitted}


__all__ = [
    "ConsolidationRuntimeUnavailable",
    "admit_background",
    "admit_command",
    "admit_mutation",
    "admit_transfer",
    "admit_upload",
    "bind_admission",
    "bind_hosted_admission",
    "bind_verification_admission",
    "load_hosted_admission",
    "readiness",
]
