"""Process-local authority for one exact sealed consolidation control action."""

from __future__ import annotations

import re
from typing import NoReturn, SupportsIndex

_AUTHORITY_SEAL = object()
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
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


class ConsolidationAuthorityUnavailable(RuntimeError):
    """Content-free refusal for missing, forged, or cross-bound authority."""

    code = "CONSOLIDATION_AUTHORITY_UNAVAILABLE"

    def __init__(self) -> None:
        super().__init__("consolidation authority is unavailable")


def _fail() -> NoReturn:
    raise ConsolidationAuthorityUnavailable from None


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        _fail()
    return value


def _uuid4(value: object) -> str:
    if not isinstance(value, str) or _UUID4.fullmatch(value) is None:
        _fail()
    return value


def _phase(value: object) -> str:
    if not isinstance(value, str) or value not in _PHASES:
        _fail()
    return value


def _action(value: object) -> str:
    if not isinstance(value, str) or value not in _ACTIONS:
        _fail()
    return value


class ConsolidationAuthority:
    """Opaque process-local capability; only the coordinator owns its seal."""

    __slots__ = (
        "__action",
        "__journal_digest",
        "__operation_id",
        "__phase",
        "__run_id",
        "__seal",
        "__vault_binding_digest",
        "__weakref__",
    )
    __vault_binding_digest: str
    __run_id: str
    __operation_id: str
    __journal_digest: str
    __phase: str
    __action: str
    __seal: object

    def __init__(
        self,
        vault_binding_digest: str,
        run_id: str,
        operation_id: str,
        journal_digest: str,
        phase: str,
        action: str,
        seal: object,
    ) -> None:
        object.__setattr__(
            self, "_ConsolidationAuthority__vault_binding_digest", vault_binding_digest
        )
        object.__setattr__(self, "_ConsolidationAuthority__run_id", run_id)
        object.__setattr__(self, "_ConsolidationAuthority__operation_id", operation_id)
        object.__setattr__(self, "_ConsolidationAuthority__journal_digest", journal_digest)
        object.__setattr__(self, "_ConsolidationAuthority__phase", phase)
        object.__setattr__(self, "_ConsolidationAuthority__action", action)
        object.__setattr__(self, "_ConsolidationAuthority__seal", seal)

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("consolidation authority is immutable")

    def __repr__(self) -> str:
        return "<ConsolidationAuthority process-local>"

    def __reduce__(self) -> NoReturn:
        raise TypeError("consolidation authority is process-local")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("consolidation authority is process-local")

    def __copy__(self) -> NoReturn:
        raise TypeError("consolidation authority is process-local")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("consolidation authority is process-local")

    def _matches(self, expected: tuple[str, str, str, str, str, str]) -> bool:
        actual = (
            self.__vault_binding_digest,
            self.__run_id,
            self.__operation_id,
            self.__journal_digest,
            self.__phase,
            self.__action,
        )
        return self.__seal is _AUTHORITY_SEAL and actual == expected


def issue_authority(
    *,
    vault_binding_digest: str,
    run_id: str,
    operation_id: str,
    journal_digest: str,
    phase: str,
    action: str,
) -> ConsolidationAuthority:
    """Issue one opaque authority for an already-authenticated sealed action."""

    checked_phase = _phase(phase)
    checked_action = _action(action)
    if checked_action == "probe" and checked_phase != "transport-verifying":
        _fail()
    return ConsolidationAuthority(
        _digest(vault_binding_digest),
        _uuid4(run_id),
        _uuid4(operation_id),
        _digest(journal_digest),
        checked_phase,
        checked_action,
        _AUTHORITY_SEAL,
    )


def require_authority(
    authority: object,
    *,
    vault_binding_digest: str,
    run_id: str,
    operation_id: str,
    journal_digest: str,
    phase: str,
    action: str,
) -> None:
    """Require the exact process-local capability before bypassing the outer seal."""

    expected = (
        _digest(vault_binding_digest),
        _uuid4(run_id),
        _uuid4(operation_id),
        _digest(journal_digest),
        _phase(phase),
        _action(action),
    )
    if type(authority) is not ConsolidationAuthority:
        _fail()
    if not authority._matches(expected):
        _fail()


__all__ = [
    "ConsolidationAuthority",
    "ConsolidationAuthorityUnavailable",
    "issue_authority",
    "require_authority",
]
