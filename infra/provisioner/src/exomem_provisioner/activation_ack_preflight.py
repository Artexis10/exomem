"""Classify a cell before acknowledgement work begins, and refuse a doomed start.

Three things decide whether work may start against a target cell.

Whether the acknowledgement is outstanding, which is a direction-blind
comparison of the activation tuple custody holds against the tuple the store
reports. Never an ordering: a tuple that merely differs is not evidence that
either side is ahead.

Whether the deployment carries the capability. With it, an outstanding
acknowledgement is recoverable by the protocol. Without it, the outcome of the
committed mutation is genuinely unknown, and it stays unknown -- a preflight
that restores activation parity has not learned what the missing receipt said
and must never write one.

Whether the attestation window has room. A cell that cannot acknowledge cannot
sign its readiness attestation, so the window stops being renewed; once it
lapses on a cell that has served, minting is fenced off and the cell is lost.
An outstanding acknowledgement is therefore a countdown, and starting work that
cannot finish inside it converts a recoverable stall into a stranded cell.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

CLEAN: Final = "clean"
PROTOCOL_QUALIFIED: Final = "protocol-qualified"
LEGACY_UNCERTAIN: Final = "legacy-uncertain"
STRANDED: Final = "stranded"

# The closed vocabulary an inspected serving-membership attestation can carry;
# `authorization_membership` refuses any other value when it reads the bundle.
# A cell that has served cannot re-mint a lapsed attestation, and both of these
# mean it has served. `None` is the only way to say "no attestation yet", so an
# unrecognised string is bad input rather than a quiet "never served" -- which
# would discard the STRANDED verdict on exactly the cells that have it.
_SERVED_STATES: Final = frozenset({"SERVING", "DRAINING"})


@dataclass(frozen=True, slots=True)
class ActivationAckPreflight:
    status: str
    may_begin: bool
    remaining_seconds: int
    acknowledgement_outstanding: bool
    reason: str


def _tuples_match(
    left: tuple[str | None, int | None, str | None],
    right: tuple[str | None, int | None, str | None],
) -> bool:
    """Compare two activation tuples without implying an order between them."""

    left_store, left_epoch, left_digest = left
    right_store, right_epoch, right_digest = right
    if left_store != right_store or left_epoch != right_epoch:
        return False
    # Plain equality on purpose. The store id and epoch above are compared the
    # same way, an activation state digest is not a secret, and
    # `hmac.compare_digest` raises on a non-ASCII string rather than refusing.
    return left_digest == right_digest


def classify_activation_ack_target(
    *,
    custody_activation: tuple[str | None, int | None, str | None],
    store_activation: tuple[str | None, int | None, str | None],
    capability_bound: bool,
    release_compatible: bool,
    replica_state: str | None,
    attestation_expires_at: int,
    now: int,
    required_seconds: int,
) -> ActivationAckPreflight:
    """Name what this cell is and whether acknowledgement work may start on it."""

    if required_seconds <= 0:
        # A zero budget is a green light that promises nothing finishes.
        raise ValueError("required work budget must be positive")
    if replica_state is not None and replica_state not in _SERVED_STATES:
        raise ValueError("replica state is not a known serving-membership state")
    remaining = attestation_expires_at - now
    outstanding = not _tuples_match(custody_activation, store_activation)
    served = replica_state is not None

    if remaining <= 0 and served:
        return ActivationAckPreflight(
            status=STRANDED,
            may_begin=False,
            remaining_seconds=remaining,
            acknowledgement_outstanding=outstanding,
            reason="the attestation window lapsed on a cell that has served",
        )

    if not outstanding:
        status = CLEAN
    elif capability_bound:
        status = PROTOCOL_QUALIFIED
    else:
        status = LEGACY_UNCERTAIN

    if status == LEGACY_UNCERTAIN:
        # Restoring activation parity would make this cell look clean without
        # having recovered what the missing receipt said. Refuse rather than
        # reconcile: the identity and the unknown outcome must both survive.
        return ActivationAckPreflight(
            status=status,
            may_begin=False,
            remaining_seconds=remaining,
            acknowledgement_outstanding=True,
            reason="a committed mutation has no acknowledgement and no protocol to recover it",
        )

    if capability_bound and not release_compatible:
        return ActivationAckPreflight(
            status=status,
            may_begin=False,
            remaining_seconds=remaining,
            acknowledgement_outstanding=outstanding,
            reason="the capability is not bound to compatible runtime, provisioner and chart releases",
        )

    if remaining < required_seconds:
        return ActivationAckPreflight(
            status=status,
            may_begin=False,
            remaining_seconds=remaining,
            acknowledgement_outstanding=outstanding,
            reason="the work cannot complete inside the remaining attestation window",
        )

    return ActivationAckPreflight(
        status=status,
        may_begin=True,
        remaining_seconds=remaining,
        acknowledgement_outstanding=outstanding,
        reason=(
            "an outstanding acknowledgement is recoverable inside the remaining window"
            if outstanding
            else "the activation tuple is acknowledged and the window has room"
        ),
    )
