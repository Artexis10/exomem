"""The closed outcome table of the write-intent applier (design D5).

One dispatch that does not return a committed result ends in one of these rows. The table is a
closed protocol set, the error codes the writer and the leaves raise, so it is not vocabulary.
A new code that is in no row reaches the last row, `apply_failed`, and an owner decides it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from . import cli_ops

WAIT = "wait"
COMMITTED = "committed"
CONFLICTED = "conflicted"
REJECTED = "rejected"

CAUSE_WRITER_LEASE = "writer_lease"
CAUSE_ACKNOWLEDGEMENT_PENDING = "acknowledgement_pending"
CAUSE_BASIS_CHANGED = "basis_changed"
CAUSE_VOCABULARY_CHANGED = "vocabulary_changed"
CAUSE_TARGET_MISSING = "target_missing"
CAUSE_REVIEW_REQUIRED = "review_required"
CAUSE_CONTRACT_CHANGED = "contract_changed"
CAUSE_IDENTITY_CONFLICT = "identity_conflict"
CAUSE_OUTCOME_UNKNOWN = "outcome_unknown"
CAUSE_APPLY_FAILED = "apply_failed"
CAUSE_STALLED = "stalled"
CAUSE_BOUNDARY_HELD = "boundary_held"

CAUSE_REVOKED = "authorization_revoked"
CAUSE_EXPIRED = "authorization_expired"
#: Causes that never retry: the caller's authority is gone, so only a new request under a live login may write.
AUTHORIZATION_CAUSES = (
    CAUSE_REVOKED,
    CAUSE_EXPIRED,
)  # nosemgrep: ep-word-set -- the closed causes the applier writes

NOT_FOUND = "NOT_FOUND"
ACKNOWLEDGEMENT_UNCERTAIN = "MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN"


@dataclass(frozen=True)
class Outcome:
    """What the applier does with one dispatch that did not commit through the ordinary result."""

    kind: str
    cause: str | None = None
    #: A wait that belongs to this one intent. It counts toward the per-intent bound; the others do not.
    counts: bool = False
    #: The error is the terminal of a write that did commit.
    uncertain: bool = False
    #: The refusal comes before anything commits, so accept may journal the write instead of answering it.
    precommit: bool = False
    #: For a precommit refusal: why the write was journaled, in the cause vocabulary of the `accepted` row.
    accept_cause: str | None = None


# Wait and re-run. `None` as the cause means the code names itself. Lost authority blocks every intent alike, so it
# never counts. A cause that belongs to one intent counts toward the per-intent bound (design D4).
_WAITS: dict[
    str, Outcome
] = {  # nosemgrep: ep-word-set -- the writer's closed error codes, not vocabulary
    "WRITER_FENCED": Outcome(WAIT, CAUSE_WRITER_LEASE),
    "WRITER_LEASE_REQUIRED": Outcome(WAIT, CAUSE_WRITER_LEASE),
    "WRITER_COORDINATOR_UNAVAILABLE": Outcome(WAIT, CAUSE_WRITER_LEASE),
    "MUTATION_WARMING": Outcome(WAIT, counts=True, precommit=True, accept_cause="warming"),
    "REFERENCE_PREPARATION_UNAVAILABLE": Outcome(WAIT, counts=True, precommit=True, accept_cause="reference_unprepared"),
    "CLOUD_CELL_READ_ONLY": Outcome(WAIT, counts=True),
    "MUTATION_ACKNOWLEDGEMENT_PENDING": Outcome(WAIT, CAUSE_ACKNOWLEDGEMENT_PENDING, counts=True),
}

# The caller's own basis no longer holds, or a rule now blocks the write: an owner decides.
_CONFLICTS: dict[
    str, Outcome
] = {  # nosemgrep: ep-word-set -- the leaves' closed error codes, not vocabulary
    "STALE_EDIT": Outcome(CONFLICTED, CAUSE_BASIS_CHANGED),
    "STALE_SEMANTIC_WRITE": Outcome(CONFLICTED, CAUSE_BASIS_CHANGED),
    "PATH_GUARD_CHANGED": Outcome(CONFLICTED, CAUSE_BASIS_CHANGED),
    "STALE_PARENT_HASH": Outcome(CONFLICTED, CAUSE_BASIS_CHANGED),
    "STALE_UNIT_REFERENCE": Outcome(CONFLICTED, CAUSE_BASIS_CHANGED),
    "CONTINUATION_STALE": Outcome(CONFLICTED, CAUSE_BASIS_CHANGED),
    "STALE_RECORD": Outcome(CONFLICTED, CAUSE_BASIS_CHANGED),
    "STALE_RECORD_SNAPSHOT": Outcome(CONFLICTED, CAUSE_BASIS_CHANGED),
    "STALE_VOCABULARY_BINDING": Outcome(CONFLICTED, CAUSE_VOCABULARY_CHANGED),
    "IDEMPOTENCY_KEY_REUSED": Outcome(CONFLICTED, CAUSE_IDENTITY_CONFLICT),
    "MUTATION_OUTCOME_UNKNOWN": Outcome(CONFLICTED, CAUSE_OUTCOME_UNKNOWN),
}


def error_code(error: BaseException) -> str | None:
    """The code the caller would have seen. A leaf may wrap the writer's refusal, so the public form counts too."""
    raw = getattr(error, "code", None)
    if isinstance(raw, str) and raw:
        return raw
    public = cli_ops.error_dict(error) if isinstance(error, Exception) else {}
    code = public.get("code")
    return code if isinstance(code, str) and code else None


def is_precommit_code(code: str | None) -> bool:
    """Whether the outcome table marks this error code as one the writer raises before any effect."""
    outcome = _WAITS.get(code or "")
    return outcome is not None and outcome.precommit


def accept_cause(code: str | None) -> str | None:
    """Why a precommit refusal made the write journal, as a cause and never as the error code."""
    if code == "MUTATION_BUSY":  # the one closed protocol code for a plain held boundary; a cause on it wins upstream
        return CAUSE_BOUNDARY_HELD
    outcome = _WAITS.get(code or "")
    return None if outcome is None else outcome.accept_cause


def classify_error(error: BaseException, *, validation: str) -> Outcome:
    """The row of the outcome table for one raised error. `validation` is the intent's: `complete` or `deferred`."""
    code = error_code(error)
    if getattr(error, "committed", False) is True:
        # The canonical commit is proven. Never retried; its recorded failure or uncertainty is the terminal.
        return Outcome(COMMITTED, uncertain=code == ACKNOWLEDGEMENT_UNCERTAIN)
    if code in _WAITS:
        row = _WAITS[code]
        return row if row.cause is not None else replace(row, cause=code)
    # Before the boundary or the leaf: nothing committed, so wait for the holder and run again, without a bound.
    details: Any = getattr(error, "details", None)
    if code == "MUTATION_BUSY" and isinstance(details, dict) and details.get("committed") is False:
        return Outcome(WAIT, str(details.get("cause") or "busy"))
    if code in _CONFLICTS:
        return _CONFLICTS[code]
    if cli_ops.leaf_contract_code(error) is not None:
        # The leaf refused this write by its own contract. After complete validation the contract changed
        # since accept, which an owner decides; after deferred validation it is the synchronous error.
        return (
            Outcome(REJECTED)
            if validation == "deferred"
            else Outcome(CONFLICTED, CAUSE_CONTRACT_CHANGED)
        )
    return Outcome(CONFLICTED, CAUSE_APPLY_FAILED)


def classify_result(result: Any) -> Outcome:
    """The row for a dispatch that returned without raising and without a committed state."""
    if isinstance(result, dict) and result.get("state") == "needs_review":
        return Outcome(CONFLICTED, CAUSE_REVIEW_REQUIRED)
    return Outcome(CONFLICTED, CAUSE_APPLY_FAILED)
