"""Runtime admission for the activated vocabulary-authority contract.

The authority store remains the source of truth.  This module only translates
its startup-safe status into the existing mutation and offline-replacement
boundaries; it never creates authority or relaxes v1 behaviour.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .governance import authorization_custody
from .vocabulary_authority import AuthorityStatus, VocabularyAuthority


class VocabularyAdmissionError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class RuntimeAdmission:
    status: AuthorityStatus

    @property
    def mutations_allowed(self) -> bool:
        return self.status.mode in {"v1", "v2"}


def runtime_admission(
    vault_root: Path,
    *,
    authority_factory: Callable[[Path], VocabularyAuthority] = VocabularyAuthority,
    now: int | None = None,
) -> RuntimeAdmission:
    """Read the authority's startup status without opening request authority."""
    authority = authority_factory(Path(vault_root))
    status = authority.runtime_status() if now is None else authority.runtime_status(now=now)
    return RuntimeAdmission(status)


def require_mutation_admission(
    vault_root: Path,
    *,
    authority_factory: Callable[[Path], VocabularyAuthority] = VocabularyAuthority,
    now: int | None = None,
) -> RuntimeAdmission:
    admission = runtime_admission(Path(vault_root), authority_factory=authority_factory, now=now)
    if not admission.mutations_allowed:
        raise VocabularyAdmissionError("VOCABULARY_AUTHORITY_UNAVAILABLE")
    return admission


def require_restore_admission(
    vault_root: Path,
    *,
    authority_factory: Callable[[Path], VocabularyAuthority] = VocabularyAuthority,
) -> RuntimeAdmission:
    """Refuse offline replacement of an activated v2 custody generation.

    There is no authority-preserving offline migration in this release.  A
    destination already under activated custody therefore cannot be replaced.
    """
    admission = runtime_admission(Path(vault_root), authority_factory=authority_factory)
    if admission.status.mode == "unavailable":
        raise VocabularyAdmissionError("VOCABULARY_AUTHORITY_UNAVAILABLE")
    if admission.status.mode == "v2":
        raise VocabularyAdmissionError("VOCABULARY_RESTORE_REQUIRES_AUTHORITY")
    return admission


def require_session_open_admission(vault_root: Path, *, now: int) -> None:
    admission = runtime_admission(Path(vault_root), now=now)
    if admission.mutations_allowed:
        return
    _require_floor_two(vault_root, now=now)

def require_activation_admission(vault_root: Path, *, now: int) -> None:
    _require_floor_two(vault_root, now=now)

def _require_floor_two(vault_root: Path, *, now: int) -> None:
    try:
        custody = authorization_custody.load_authorization_custody(Path(vault_root), now=now)
        if custody.control.version != 2 or custody.control.vocabulary_authority_floor != 2:
            raise ValueError
    except (authorization_custody.AuthorizationCustodyUnavailable, OSError, ValueError):
        raise VocabularyAdmissionError("VOCABULARY_AUTHORITY_UNAVAILABLE") from None
