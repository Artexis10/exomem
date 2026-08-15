"""Which pre-registered families a pending amendment still withholds.

A family can be *pre-registered* without being *released*. PREREGISTRATION §1
lists f01-f19, but f15-f19 arrived through the 2026-08 §7 amendment, and that
amendment's receipt says plainly what it withholds until the founder
acknowledges it: those families "cannot support comparative runs or claims".

Before they were registered, the scenario loader refused them for an unrelated
reason — §1 simply did not know the ids. That was incidental protection, and
registering them removes it. This module is what replaces it, at the same
load-time choke point, governed by the receipt instead of by an accident of
sequencing.

**No Git.** The question here is only "is amendment N acknowledged?", which the
working receipt bytes answer on their own. Reconstructing the full pinned
identity needs Git history, and making scenario loading depend on that would
make an ordinary fixture load fail in any checkout without it. The families each
amendment introduced are mirrored in
:data:`epistemic.registry.AMENDMENT_INTRODUCED_FAMILIES` and drift-tested
against the Git-derived chain, so the cheap path cannot silently disagree with
the authoritative one.

**Fail closed.** If the receipts cannot be read at all, every family an
amendment introduced is treated as withheld. An unreadable receipt is not
evidence of release.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from protocol.contracts import (
    AmendmentAcknowledgmentPendingError,
    ContractIdentityError,
    working_amendment_receipts,
)

from .registry import AMENDMENT_INTRODUCED_FAMILIES

#: Repository root, three levels up from ``benchmarks/epistemic/amendments.py``.
REPO_ROOT = Path(__file__).resolve().parents[2]


def _pending_sequences(repo_root: Path) -> frozenset[int]:
    """Amendment sequences whose receipts are not yet acknowledged."""

    try:
        receipts = working_amendment_receipts(repo_root)
    except (ContractIdentityError, OSError):
        # Unreadable receipts prove nothing about release; withhold everything.
        return frozenset(AMENDMENT_INTRODUCED_FAMILIES.values())
    acknowledged = {
        receipt.sequence
        for receipt in receipts
        if receipt.acknowledgment_status == "acknowledged"
    }
    return frozenset(
        sequence
        for sequence in set(AMENDMENT_INTRODUCED_FAMILIES.values())
        if sequence not in acknowledged
    )


@lru_cache(maxsize=4)
def _withheld_cached(repo_root: str) -> frozenset[str]:
    pending = _pending_sequences(Path(repo_root))
    return frozenset(
        family_id
        for family_id, sequence in AMENDMENT_INTRODUCED_FAMILIES.items()
        if sequence in pending
    )


def withheld_family_ids(repo_root: Path | str | None = None) -> frozenset[str]:
    """Families an unacknowledged amendment introduced, and therefore withholds.

    Cached per repository root: the receipts do not change under a running
    process, and scenario loading must not pay file I/O per scenario.
    """

    root = Path(repo_root).resolve() if repo_root is not None else REPO_ROOT
    return _withheld_cached(str(root))


def amendment_sequence_for(family_id: str) -> int | None:
    """The amendment that introduced ``family_id``; ``None`` for a ratified-base family."""

    return AMENDMENT_INTRODUCED_FAMILIES.get(family_id)


def require_family_released(
    family_id: str, *, repo_root: Path | str | None = None
) -> None:
    """Refuse a family whose amendment has not been acknowledged.

    Raises :class:`~protocol.contracts.AmendmentAcknowledgmentPendingError`, the
    same typed refusal the manifest gate raises, so a caller can distinguish
    "this family is withheld pending acknowledgment" from "this family does not
    exist" without parsing a message.
    """

    if family_id not in withheld_family_ids(repo_root):
        return
    sequence = amendment_sequence_for(family_id)
    raise AmendmentAcknowledgmentPendingError(
        f"amendment sequence {sequence} founder acknowledgment is pending; "
        f"{family_id} may not back a comparative run, score or claim"
    )


def reset_cache() -> None:
    """Drop the memoized receipt read. For tests that mutate a fixture repo."""

    _withheld_cached.cache_clear()


__all__ = [
    "REPO_ROOT",
    "amendment_sequence_for",
    "require_family_released",
    "reset_cache",
    "withheld_family_ids",
]
