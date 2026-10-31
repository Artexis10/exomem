"""Single-writer lease coordination for replicated Exomem vaults.

The coordinator carries identity and timing metadata only. Vault content never
leaves the replica. Coordination is opt-in through ``EXOMEM_WRITER_LEASE_URL``;
without it the invocation path is the legacy standalone path.
"""

from __future__ import annotations

import atexit
import ctypes
import hashlib
import ipaddress
import json
import logging
import math
import os
import pickle
import re
import secrets
import sqlite3
import stat
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, nullcontext
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from functools import partial, wraps
from pathlib import Path
from typing import Any, get_args

from filelock import ReadWriteLock, Timeout

from . import call_spans, durable_write, mutation_journal, mutation_lock, request_budget
from . import capabilities as capabilities_module
from . import curation as curation_module
from .cli_ops import OpError, leaf_contract_code
from .mutation_lock import (
    VaultMutationCoordinator,
    _mutation_busy,
    canonical_mutation_identity,
    last_mutation_timing,
    process_local_mutation_boundary,
)
from .mutation_lock import _release_os_lock as _release_owner_lock
from .mutation_lock import _try_os_lock as _try_owner_lock
from .mutation_terminal import (
    _DERIVED_COMPONENT_NAMES,
    ResponseDetail,
    _CanonicalRequestReplay,
    committed_terminal,
    needs_review_terminal,
    project_terminal,
    replayed_terminal,
    settled_media_terminal,
    split_response_detail,
    valid_collection_receipt,
    valid_structured_files_receipt,
    with_fast_acknowledgement,
)
from .privacy_log import content_private_logging_enabled

_COORDINATOR_USER_AGENT = (
    "Mozilla/5.0 (compatible; Exomem-Coordinator/1.0; +https://github.com/Artexis10/exomem)"
)
# The implicit replay window is the acknowledgement-loss recovery budget: when
# the edge abandons a slow write that the origin then commits, retrying the
# byte-identical call within this window replays the stored result (with the
# written slug) instead of double-writing or failing on the existing page.
# 60s was shorter than one abandoned-write investigation; 10 minutes covers a
# human noticing the timeout, checking state, and retrying.
_IMPLICIT_RETRY_TTL_SECONDS = 600.0

_REQUEST_BOUND_REMOTE_SURFACES = frozenset({"mcp", "rest"})
# Maintenance modes whose writes remote surfaces admit. Each one is governed by
# an exact reviewed plan or preview identity, so remote callers get the same
# capability as the CLI. Closed set: the maintain_memory mode literal fixes it.
REMOTE_WRITE_MAINTENANCE_MODES = frozenset({  # nosemgrep: ep-word-set -- closed maintain_memory mode literals
    "structured-files", "tag-variants", "curation", "maintenance-job-cancel", "collections-store-adopt-local",
})


def remote_admitted_maintenance_modes() -> frozenset[str]:
    """Maintenance write modes a request-bound surface admits: the direct ones and the plan-bound job modes."""
    from .maintenance_jobs import JOB_MODES

    return REMOTE_WRITE_MAINTENANCE_MODES | JOB_MODES


REMOTE_MAINTENANCE_MESSAGE = (
    "write-mode maintenance is unavailable through request-bound remote commands"
)


# Only a mode with no remote form reaches this refusal, and today that is `collections-store-export`
# apply, which runs on the host with the service stopped. A mode that joins it needs its own text.
REMOTE_MAINTENANCE_REMEDIATION = (
    "Stop the service on the host, then run `exomem collections export --plan-id ID --why REASON` there. "
    "Preview the export remotely without apply."
)

# Commands whose `invoke()` boundary is narrowed to `writer_authority_guard`
# (fence-only, no shared vault lock) instead of the full `mutation_guard`.
# The command's own commit seam (`semantic_writes.commit_creation` or
# `commit_existing`) is responsible for acquiring the vault mutation boundary
# itself, around only its commit — not the corpus validation and model
# loading that precede it. Every name here ends in `commit_creation` or
# `commit_existing`, both self-guarding: `remember`/`replace_memory` ->
# `commit_creation`; `edit_memory` (all four of its edit/multi_edit/
# set_take/set_frontmatter_field sub-modes) and `observe_memory` ->
# `commit_existing`. `EXOMEM_WIDE_MUTATION_BOUNDARY` restores today's
# wide-boundary behavior for every command in this set.
_NARROW_BOUNDARY_COMMANDS = frozenset(
    {
        "remember",
        "replace_memory",
        "edit_memory",
        "observe_memory",
        "record_memory",
        "plan_memory",
        "preserve_artifacts",
    }
)
_EXPLICIT_RETRY_TTL_SECONDS = 24 * 60 * 60.0
_IDEMPOTENCY_WAIT_SECONDS = 5.0
_IDEMPOTENCY_POLL_INTERVAL_SECONDS = 0.025
_IDEMPOTENCY_SQLITE_TIMEOUT_SECONDS = 10.0
_COMMITTED_FAILURE_CODE = "BATCH_CLEANUP_INCOMPLETE"
_COMMITTED_FAILURE_MESSAGE = "The batch workspace cleanup is incomplete."
_COMMITTED_FAILURE_REMEDIATION = (
    "Do not retry the write; committed destinations are preserved. Reconcile retained "
    "workspace state."
)
_COMMITTED_FAILURE_TOP_KEYS = frozenset({"code", "message", "remediation", "outcome"})
_COMMITTED_FAILURE_OUTCOME_KEYS = frozenset(
    {
        "kind",
        "committed",
        "incomplete",
        "affected_count",
        "targets",
        "omitted_target_count",
    }
)
_RECEIPT_RESULT_SUMMARY_MAX_DEPTH = 8
_RECEIPT_RESULT_SUMMARY_MAX_ITEMS = 128
_RETRY_IDEMPOTENCY_CLAIM = object()
# Retained for compatibility with callers that advance their injected clocks
# before asserting a dead attempt remains fail-closed.
_IDEMPOTENCY_ABANDONED_RETRY_AFTER_SECONDS = 60.0
# A `pending` row written before this feature shipped has no `owner` to
# probe; it is honored under the pre-existing any-pending-blocks-forever
# rule for this long before it is classified as an unknown outcome.
_IDEMPOTENCY_LEGACY_OWNER_GRACE_SECONDS = 600.0
#: How many non-terminal receipt rows one process start may examine.
#:
#: `_prune_expired` removes only terminal rows and `_abandon_if_dead` runs only
#: when the identical identity is retried, so a `pending`/`reserved` row left
#: by a process that died had no resolver at all. The sweep below is that
#: resolver, and this is its per-start budget: it runs once at the end of
#: `IdempotencyStore.__init__` under its own `BEGIN IMMEDIATE`, and each
#: examined row costs one owner-lock probe (open, try-lock, close) plus at most
#: one UPDATE while that write transaction is open -- so an unbounded scan
#: would hold the store's write lock, on the critical path of every process
#: start, for as long as a pathological store is large. 64 clears the
#: thirteen-row backlog measured on the personal cell with five times the
#: headroom; anything past it is drained by the next start and stays visible in
#: `oldest_pending_age_seconds`, which this never resets.
_IDEMPOTENCY_START_SWEEP_LIMIT = 64
_OUTCOME_UNKNOWN_TERMINAL = ("exomem.outcome-unknown", 1)
_OUTCOME_UNKNOWN_PAYLOAD = pickle.dumps(_OUTCOME_UNKNOWN_TERMINAL)
_WINDOWS_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_WINDOWS_SECRET_ENVELOPE_MAGIC = b"EXID"
_WINDOWS_SECRET_ENVELOPE_VERSION = 1
_WINDOWS_SECRET_ENVELOPE_MAX_BYTES = 4096
_WINDOWS_SECRET_ENVELOPE_HEADER_BYTES = 10
_WINDOWS_SECRET_ENVELOPE_MAX_CIPHERTEXT = (
    _WINDOWS_SECRET_ENVELOPE_MAX_BYTES - _WINDOWS_SECRET_ENVELOPE_HEADER_BYTES
)
_WINDOWS_SECRET_ENTROPY_DOMAIN = b"exomem-graph-commit-receipt-dpapi:v1\0"
logger = logging.getLogger(__name__)
_mutation_logger = logging.getLogger("exomem.calls")
_ACTIVE_WRITE_FENCE: ContextVar[tuple[Any, int] | None] = ContextVar(
    "exomem_active_write_fence", default=None
)
_ACTIVE_COLLECTION_FILE_WRITE: ContextVar[tuple[Any, Any, bytes | None] | None] = ContextVar(
    "exomem_active_collection_file_write", default=None
)
_ACTIVE_MUTATION_TRACE: ContextVar[tuple[str, str, str] | None] = ContextVar(
    "exomem_active_mutation_trace", default=None
)
_ACTIVE_MUTATION_COMMITTED: ContextVar[bool] = ContextVar(
    "exomem_active_mutation_committed", default=False
)
_ACTIVE_MUTATION_PROTOCOL: ContextVar[tuple[str | None, str | None] | None] = ContextVar(
    "exomem_active_mutation_protocol", default=None
)
_ACTIVE_LEASE_MANAGER: ContextVar[Any | None] = ContextVar(
    "exomem_active_lease_manager", default=None
)
_ACTIVE_DIRECT_MUTATION_GUARDS: ContextVar[tuple[tuple[str, Path], ...]] = ContextVar(
    "exomem_active_direct_mutation_guards", default=()
)

_FAST_ACK_DEADLINE_SECONDS = 2.0

#: PROVISIONAL. The single-origin request budget the write-path resilience
#: contract assumes (60s), and the slice reserved for shaping and delivering
#: the terminal once the acknowledgement is done. The derived acknowledgement
#: now runs *after* the terminal is durable, so the only thing it can still
#: cost the caller is the response: bound it by what is left of the request
#: rather than letting a slow index or graph fan-out spend the whole budget on
#: work whose outcome is already recorded as `pending`. One definition, in one
#: module, so the pair cannot drift apart. Revisited by the latency change.
_MUTATION_REQUEST_BUDGET_SECONDS = 60.0
_TERMINAL_DELIVERY_RESERVE_SECONDS = 5.0


def acknowledgement_budget_deadline(entry: float | None = None) -> float:
    """When the derived acknowledgement must stop waiting, on the monotonic clock.

    Two bounds, and the earlier one wins. The entry-based value is the lease's
    own: measured from the moment this mutation entered, it keeps a mutation
    that arrived on a long-lived connection from waiting forever. The request
    budget's is the connector's: the origin has to have delivered a terminal
    before the client stops listening, so the wait ends a fixed delivery
    reserve before the request deadline itself.

    This bounds only the *wait*. A canonical commit already under way is never
    interrupted by either bound; what expiry changes is that the terminal is
    reported with `derived_sync` still pending, rather than held back until
    everything derived has been proven.
    """
    started = _fast_ack_monotonic() if entry is None else float(entry)
    deadline = (
        started + _MUTATION_REQUEST_BUDGET_SECONDS - _TERMINAL_DELIVERY_RESERVE_SECONDS
    )
    budget = request_budget.current()
    if budget is None:
        return deadline
    return min(deadline, budget.deadline - request_budget.DELIVERY_RESERVE_SECONDS)


def _publish_pending_visibility(vault_root: Path, receipt: Any) -> bool:
    """The production callee behind the frozen pending-visibility seam.

    Lane 2 owns the projection; Lane 1 owns the durable custody transition;
    this is the one place the two are joined. Imported lazily so the writer
    module keeps its current import graph -- ``pending_recall`` reaches recall
    and the lexical store, neither of which may be pulled in at writer import.
    """
    from . import pending_recall

    return pending_recall.publish(vault_root, receipt)


#: Overridable for tests that exercise the terminal boundary without a store.
#: In production it is always Lane 2's publisher: a ``None`` here makes every
#: governed fast-ack write fail closed into committed-uncertain.
_PENDING_VISIBILITY_PUBLISHER: Callable[[Path, Any], bool] | None = (
    _publish_pending_visibility
)
_fast_ack_monotonic = time.monotonic


@dataclass(slots=True)
class _FastAcknowledgementBatch:
    receipt: Any
    canonical_commit_monotonic: float
    advisory_target: str | None = None
    #: The route's own sweep inputs, when the leaf declared them for this
    #: batch's advisory target (`declare_write_advisory`).
    advisory_inputs: Any | None = None
    handoff_outcome: str | None = None
    handoff_error: _PostCommitOutcomeUncertain | None = None


@dataclass(slots=True)
class _FastAcknowledgementSession:
    vault_root: Path
    request_id: str
    mutation_attempt_digest: str
    terminal_replay_until: float
    advisory_required: bool
    sequence: int = 0
    #: One acknowledgement carries one stable advisory result ref, so the first
    #: canonical batch that *commits* with a named target claims the session's
    #: advisory job and later batches in the same mutation carry none. The
    #: claim is taken at the committed handoff, never at preparation: a
    #: prepared batch that rolls back must leave the claim for its successor.
    advisory_claimed: bool = False
    advisory_prepared_target: str | None = None
    advisory_prepared_inputs: Any | None = None
    batches: list[_FastAcknowledgementBatch] = field(default_factory=list)
    handoff_in_progress: bool = False


_ACTIVE_FAST_ACK_SESSION: ContextVar[_FastAcknowledgementSession | None] = ContextVar(
    "exomem_active_fast_ack_session", default=None
)

#: Derived work a leaf moved out of its own critical section, to be run once
#: this mutation's terminal is durable. The queue is a plain list owned by
#: `invoke` and bound for the leaf through this variable, the same way the
#: fast-acknowledgement session is: the leaf appends, `invoke` drains at the
#: position the acknowledgement occupies, and a leaf running outside any
#: mutation sees no queue at all and keeps doing the work itself.
_ACTIVE_POST_TERMINAL_FANOUT: ContextVar[list[Any] | None] = ContextVar(
    "exomem_active_post_terminal_fanout", default=None
)


def defer_until_terminal_persisted(work: Callable[[], list[object]]) -> bool:
    """Queue `work` to run after this mutation's terminal is persisted.

    Returns False when there is no mutation to defer to — a CLI caller, a
    watcher, a direct test — so the caller keeps doing the work inline rather
    than dropping it. Silence is the one answer this must never give.
    """
    queue = _ACTIVE_POST_TERMINAL_FANOUT.get()
    if queue is None:
        return False
    queue.append(work)
    return True


def _drain_post_terminal_fanout(queue: list[Any]) -> tuple[list[Any], bool]:
    """Run every deferred item once, in order, swallowing failures.

    The canonical write is already durable and its terminal already persisted
    by the time this runs. Derived work that fails here leaves the index behind
    the vault, which the deferred-refresh record and `derived_sync` exist to
    report — it must never turn a committed mutation into an error the caller
    would retry.
    """
    reports: list[Any] = []
    failed = False
    while queue:
        work = queue.pop(0)
        try:
            outcome = work()
            if isinstance(outcome, list):
                reports.extend(outcome)
        except Exception:  # noqa: BLE001 - the canonical terminal is durable
            failed = True
            logger.warning(
                "post-terminal derived fan-out failed; index refresh is behind",
                exc_info=True,
            )
    return reports, failed


#: Housekeeping a leaf moved out of its own critical section, to be run once
#: this mutation's terminal is durable -- the same deferral `_ACTIVE_
#: POST_TERMINAL_FANOUT` gives derived-index work, but for a registrant that
#: reports nothing about derived custody. Kept as a SEPARATE queue on purpose:
#: `post_terminal_fanout`'s mere non-emptiness is read in several places as
#: "real derived-index work is outstanding for this write" (whether to mark
#: the terminal `derived_sync: "pending"`, whether an acknowledgement is
#: owed). A housekeeping registrant sharing that queue would make every one
#: of those reads wrong for every write it touches, not just its own
#: response (review: merge fallout -- heat's commit-seam append did exactly
#: this by reusing `defer_until_terminal_persisted`).
_ACTIVE_POST_TERMINAL_HOUSEKEEPING: ContextVar[list[Callable[[], None]] | None] = ContextVar(
    "exomem_active_post_terminal_housekeeping", default=None
)


def defer_housekeeping_until_terminal_persisted(work: Callable[[], None]) -> bool:
    """Queue `work` to run after this mutation's terminal is persisted.

    For housekeeping with no derived-custody implications -- nothing here
    ever marks a terminal `derived_sync: "pending"` or wraps it in derived-
    acknowledgement diagnostics, unlike `defer_until_terminal_persisted`.
    Returns False exactly as that function does: no mutation to defer to (a
    CLI caller, a watcher, a direct test), so the caller does the work inline.
    """
    queue = _ACTIVE_POST_TERMINAL_HOUSEKEEPING.get()
    if queue is None:
        return False
    queue.append(work)
    return True


def _drain_post_terminal_housekeeping(queue: list[Callable[[], None]]) -> None:
    """Run every deferred housekeeping item once, in order, swallowing failures."""
    while queue:
        work = queue.pop(0)
        try:
            work()
        except Exception:  # noqa: BLE001 - the canonical terminal is already durable
            logger.warning("post-terminal housekeeping failed", exc_info=True)


def _with_post_terminal_fanout_acknowledgement(
    result: Any, reports: list[Any], *, drain_failed: bool
) -> Any:
    """Project observed non-graph media fan-out into the durable terminal.

    A no-op when nothing was actually drained. `post_terminal_fanout` is a
    shared queue of deferred CALLABLES, not derived-index reports: a
    registrant that has nothing to report about derived custody -- heat's own
    commit-seam append (`working_set_heat.persist_commit`) queues one on
    every governed write and its callable always returns `[]` -- must never
    dress an ordinary write up with `derived_sync`/`advisory_sync`/nested
    `diagnostics` fields that promise custody tracking nothing here actually
    performed. Only media's real fan-out ever returns a non-empty report list
    (review: merge fallout -- heat's registrant made this run unconditionally
    on every write once it started sharing the queue)."""
    if not isinstance(result, Mapping) or result.get("state") != "committed":
        return result
    if not reports and not drain_failed:
        return result

    from . import index_sync

    component_outcomes: dict[str, tuple[str, str | None]] = {}
    diagnostics: list[dict[str, str | None]] = []
    for report in reports:
        if not isinstance(report, index_sync.IndexSyncReport):
            drain_failed = True
            continue
        for component in report.components:
            diagnostics.append(
                {
                    "component": component.component,
                    "state": component.outcome,
                    "code": component.code,
                }
            )
            if component.component not in _DERIVED_COMPONENT_NAMES:
                continue
            if component.outcome in {"failed", "degraded"}:
                outcome = "failed"
            elif component.outcome in {"registered", "deferred"}:
                outcome = "pending"
            elif component.outcome == "accepted" and component.code not in {
                "embeddings_disabled",
                "no_eligible_paths",
            }:
                outcome = "pending"
            else:
                outcome = "completed"
            previous = component_outcomes.get(component.component)
            if previous is None or {"completed": 0, "pending": 1, "failed": 2}[
                outcome
            ] > {"completed": 0, "pending": 1, "failed": 2}[previous[0]]:
                component_outcomes[component.component] = (outcome, component.code)

    states = {outcome for outcome, _code in component_outcomes.values()}
    derived_sync = (
        "failed"
        if drain_failed or "failed" in states
        else "pending"
        if "pending" in states
        else "completed"
    )
    unfinished = tuple(
        component
        for component, (outcome, _code) in component_outcomes.items()
        if outcome != "completed"
    )
    return with_fast_acknowledgement(
        result,
        derived_sync=derived_sync,
        derived_sync_components=unfinished,
        component_diagnostics=diagnostics,
        advisory_sync="not_required",
    )


def _fast_ack_enabled(command: str | None = None, action: str | None = None) -> bool:
    flag = os.environ.get("EXOMEM_FAST_DURABLE_ACK")
    if flag is not None:
        return flag == "1"
    # Fixed command/action protocol: bulk apply uses existing durable custody by
    # default; an explicit flag still provides the synchronous rollback.
    return command == "adoption_studio" and action == "apply"


def fast_durable_ack_active() -> bool:
    """Whether the fast durable acknowledgement capability is currently active.

    ``EXOMEM_FAST_DURABLE_ACK=1`` enables the fast path globally. An absent
    value enables it only for bulk import apply. This global predicate remains
    false for that operation-specific default. Every explicit value other than
    ``1`` restores synchronous fan-out. The flag does not widen the fast path's
    fixed 2.0-second acknowledgement budget; bulk apply retains its own request
    budget for canonical writes.
    """
    return _fast_ack_enabled()


def _fast_ack_root_key(value: os.PathLike[str] | str) -> Path:
    """Normalize both sides of every fast-ack root comparison identically.

    The session root is produced with ``resolve`` while the batch seam passes
    an ``abspath``. Comparing those two forms directly would silently disable
    fast acknowledgement for a vault reached through a symlink, so both sides
    go through one normalization instead of two nearly-equal ones.
    """
    return Path(os.path.realpath(value))


def active_derived_batch_custody(vault_root: Path) -> bool:
    """Whether this exact vault currently owns a governed fast-ack session."""
    session = _ACTIVE_FAST_ACK_SESSION.get()
    return session is not None and _fast_ack_root_key(vault_root) == _fast_ack_root_key(
        session.vault_root
    )


#: Seconds one committed batch took to hand off its custody, per vault: the last few, kept in
#: memory only, so a new process starts unmeasured. A bulk operation reads them to size the
#: time it leaves for its own handoff.
_HANDOFF_SAMPLES_KEPT = 8
_handoff_samples: dict[Path, deque[float]] = {}
_handoff_samples_lock = threading.Lock()


def _pending_handoff_batches(session: _FastAcknowledgementSession) -> int:
    return sum(1 for b in session.batches if b.handoff_outcome is None and b.handoff_error is None)


def _timed_handoff(session: _FastAcknowledgementSession) -> None:
    """Hand off the session's pending batches and keep the seconds each batch it proved took."""
    began = _fast_ack_monotonic()
    proved = _handoff_derived_batches(session)
    if proved:
        per_batch = max(0.0, _fast_ack_monotonic() - began) / proved
        with _handoff_samples_lock:
            _handoff_samples.setdefault(
                _fast_ack_root_key(session.vault_root), deque(maxlen=_HANDOFF_SAMPLES_KEPT)
            ).append(per_batch)


def owed_custody_handoff_seconds(vault_root: Path, *, more_batches: int) -> float | None:
    """Seconds the custody handoff will take once `more_batches` more batches have committed.

    The dearest recent time per proved batch, times every batch of the active session plus
    `more_batches`, or nothing when no batch is owed. None when no handoff has been measured for
    this vault.
    """
    with _handoff_samples_lock:
        samples = _handoff_samples.get(_fast_ack_root_key(vault_root))
        dearest = max(samples) if samples else None
    if dearest is None:
        return None
    session = _ACTIVE_FAST_ACK_SESSION.get()
    if session is None or not active_derived_batch_custody(vault_root):
        batches = pending = 0
    else:
        batches, pending = len(session.batches), _pending_handoff_batches(session)
    if not pending and not more_batches:
        return 0.0
    # A handoff re-proves each earlier batch that shares a path with a new one, and a batch's
    # paths are known only once it commits; an import's batches all share its index pages.
    return dearest * (batches + more_batches)


#: What the running leaf declared about its default duplicate/overlap sweep:
#: unset (the leaf declared nothing), ``None`` (the leaf's write has no sweep),
#: or the route's exact `corpus_aware.WriteAdvisoryInputs`.
_UNDECLARED: Any = object()
_DECLARED_WRITE_ADVISORY: ContextVar[Any] = ContextVar(
    "exomem_declared_write_advisory", default=_UNDECLARED
)


@contextmanager
def declare_write_advisory(inputs: Any | None) -> Iterator[None]:
    """Declare, around a leaf's canonical commit, what its default sweep reads.

    A route that sweeps inline passes its exact inputs; under fast
    acknowledgement the committed batch then hands them to its `write_advisory`
    component, which runs the same sweep over them, and the leaf skips its own.
    ``None`` declares that this write has no default sweep, so the batch takes
    no advisory custody at all. A leaf that declares nothing keeps the
    component's generic behaviour.
    """
    token = _DECLARED_WRITE_ADVISORY.set(inputs)
    try:
        yield
    finally:
        _DECLARED_WRITE_ADVISORY.reset(token)


def write_advisory_deferred(vault_root: Path, inputs: Any) -> bool:
    """Whether a committed batch of this session carries exactly these inputs.

    True only under fast acknowledgement, for this exact vault, when a batch
    this mutation committed holds the session's advisory custody together with
    the very inputs the leaf declared. The leaf then skips its inline sweep:
    the component runs it and the terminal carries its result reference.
    Anything else -- no session, a claim released to a later batch, other
    inputs -- keeps the inline sweep.
    """
    session = _ACTIVE_FAST_ACK_SESSION.get()
    if session is None or not session.advisory_claimed:
        return False
    if _fast_ack_root_key(vault_root) != _fast_ack_root_key(session.vault_root):
        return False
    return any(
        batch.advisory_target is not None and batch.advisory_inputs is inputs
        for batch in session.batches
    )


def current_canonical_generation(vault_root: Path) -> str | None:
    """Public reader for the vault's current canonical generation.

    The component drain needs exactly the observation the acknowledgement
    makes, and a second implementation of it would be a second answer to the
    question every proof is bound to.
    """
    return _current_canonical_generation(vault_root)


def _current_canonical_generation(vault_root: Path) -> str | None:
    """The vault's current canonical generation, or None when unreadable.

    Proof must compare a receipt against what the vault holds *now*. Passing
    the receipt's own generation makes that comparison a tautology and searches
    for a superseding batch at the generation being superseded, so a real
    supersession can never be recognized. This reads the same graph-checkpoint
    family ``_batch_atomic_write_locked`` selects a receipt's generation from.
    """
    try:
        from . import graph_sync

        checkpoint = graph_sync.read_checkpoint(Path(vault_root))
    except Exception as error:  # noqa: BLE001 - fall back to the receipt
        # Name the class only. Silence here would reinstate the tautology this
        # function exists to remove, with nothing to show it happened.
        logger.debug(
            "canonical generation probe failed; proof falls back to the receipt (%s)",
            type(error).__name__,
        )
        return None
    return None if checkpoint is None else str(checkpoint.generation)


def _release_superseded_advisory_claim(
    session: _FastAcknowledgementSession,
    paths: tuple[Any, ...],
) -> None:
    """Hand the session's one advisory job back when its holder is superseded.

    A batch that took the claim and is then covered by a later batch of the
    same mutation publishes nothing, carries no result reference, and has its
    advisory work superseded in the store. Keeping the claim there would leave
    the batch that actually survives with no advisory custody at all -- and
    custody cannot be added after the receipt is durable, so the decision has
    to be made here, while this batch is still being prepared.

    The cover test is the store's own: the holder's paths must all appear in
    this batch. Anything narrower would release a claim whose holder is going
    to survive.
    """
    holder = next(
        (batch for batch in reversed(session.batches) if batch.advisory_target),
        None,
    )
    if holder is None:
        return
    covered = {path.rel_path for path in paths}
    if {path.rel_path for path in holder.receipt.paths} <= covered:
        session.advisory_claimed = False


def prepare_active_derived_batch(
    vault_root: Path,
    paths: tuple[Any, ...],
    *,
    canonical_generation: str | None = None,
    checkpoint_id: str | None = None,
    advisory_target_rel_path: str | None = None,
) -> Any | None:
    """Prepare exact receipt custody for the active governed mutation batch.

    ``advisory_target_rel_path`` is the one governed note this canonical batch
    is about, as named by the batch's own semantic-page declaration. Advisory
    custody applies only when that target is present in this batch with a
    surviving after-hash, and its fingerprint is exactly that after-hash -- the
    sha256 the advisory worker will later be asked to re-observe. When no
    single target exists the component is not required for this batch.
    """
    session = _ACTIVE_FAST_ACK_SESSION.get()
    root = Path(vault_root)
    if (
        session is None
        or _fast_ack_root_key(root) != _fast_ack_root_key(session.vault_root)
        or not paths
    ):
        return None
    from . import derived_receipts

    session.sequence += 1
    identity_material = json.dumps(
        {
            "request_id": session.request_id,
            "sequence": session.sequence,
            "mutation_attempt_digest": session.mutation_attempt_digest,
            "paths": [
                [path.rel_path, path.before_hash, path.after_hash] for path in paths
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    batch_id = hashlib.sha256(identity_material.encode("utf-8")).hexdigest()
    canonical_generation = canonical_generation or batch_id
    checkpoint_id = checkpoint_id or batch_id
    if session.advisory_claimed:
        _release_superseded_advisory_claim(session, paths)
    advisory_target: str | None = None
    advisory_fingerprint: str | None = None
    declared = _DECLARED_WRITE_ADVISORY.get()
    advisory_inputs = None
    if declared is None:
        # The leaf's write has no default sweep: nothing to defer.
        advisory_target_rel_path = None
    elif declared is not _UNDECLARED:
        if getattr(declared, "target_rel_path", None) == advisory_target_rel_path:
            advisory_inputs = declared
        else:
            # The batch is not about the page the leaf's sweep reads.
            advisory_target_rel_path = None
    if (
        session.advisory_required
        and not session.advisory_claimed
        and advisory_target_rel_path is not None
    ):
        after_hash = next(
            (
                path.after_hash
                for path in paths
                if path.rel_path == advisory_target_rel_path
            ),
            None,
        )
        if after_hash is not None:
            advisory_target = advisory_target_rel_path
            advisory_fingerprint = after_hash
    session.advisory_prepared_target = advisory_target
    session.advisory_prepared_inputs = (
        advisory_inputs if advisory_target is not None else None
    )
    required_components = set(derived_receipts.DerivedComponent)
    if advisory_target is None:
        required_components.remove(derived_receipts.DerivedComponent.WRITE_ADVISORY)
    return derived_receipts.prepare_batch(
        root,
        batch_id=batch_id,
        mutation_attempt_digest=session.mutation_attempt_digest,
        canonical_generation=canonical_generation,
        checkpoint_id=checkpoint_id,
        paths=paths,
        required_components=required_components,
        advisory_target_rel_path=advisory_target,
        advisory_target_fingerprint=advisory_fingerprint,
        terminal_replay_until=(
            session.terminal_replay_until if advisory_target is not None else None
        ),
        advisory_retention_until=(
            session.terminal_replay_until if advisory_target is not None else None
        ),
    )


def complete_active_derived_batch(
    receipt: Any | None,
    *,
    canonical_commit_monotonic: float,
) -> None:
    """Retain a committed receipt until the outer vault hold hands it off."""
    if receipt is None:
        return
    session = _ACTIVE_FAST_ACK_SESSION.get()
    if session is None:
        raise RuntimeError("derived receipt committed outside its acknowledgement session")
    advisory_target = session.advisory_prepared_target
    advisory_inputs = session.advisory_prepared_inputs
    session.advisory_prepared_target = None
    session.advisory_prepared_inputs = None
    if advisory_target is not None:
        # The canonical batch is known committed, so this is the point the
        # session's one advisory job becomes real. A batch that prepared a
        # target and then rolled back never reaches here and leaves the claim
        # available to the next governed batch in the same mutation.
        session.advisory_claimed = True
        if advisory_inputs is not None:
            from . import advisory_handoff

            advisory_handoff.register_route_inputs(
                session.vault_root, receipt.batch_id, advisory_inputs
            )
    session.batches.append(
        _FastAcknowledgementBatch(
            receipt,
            float(canonical_commit_monotonic),
            advisory_target=advisory_target,
            advisory_inputs=advisory_inputs if advisory_target is not None else None,
        )
    )


def _wait_for_derived_component(
    vault_root: Path,
    receipt: Any,
    status: Any,
    *,
    deadline_monotonic: float,
) -> Any:
    """Observe one already-running component without ever extending the deadline."""
    from . import derived_receipts

    current = status
    while current.state == "claimed" and _fast_ack_monotonic() < deadline_monotonic:
        time.sleep(min(0.025, max(0.0, deadline_monotonic - _fast_ack_monotonic())))
        current = derived_receipts.component_status(
            vault_root, receipt, current.component
        )
    return current


def _fast_ack_outcome(values: Iterable[str], *, not_required: bool = False) -> str:
    outcomes = tuple(values)
    if not_required and outcomes and all(value == "not_required" for value in outcomes):
        return "not_required"
    if "failed" in outcomes:
        return "failed"
    if "pending" in outcomes:
        return "pending"
    return "completed"


def _acknowledge_derived_batches(
    result: Any,
    session: _FastAcknowledgementSession,
    *,
    budget_deadline: float | None = None,
) -> Any:
    """Prove, publish, signal and freeze one post-canonical status snapshot."""
    if not session.batches:
        return result
    with call_spans.span("derived.acknowledgement"):
        try:
            return _acknowledge_derived_batches_timed(
                result, session, budget_deadline=budget_deadline
            )
        finally:
            call_spans.record_span(
                "derived.post_canonical",
                (_fast_ack_monotonic() - min(batch.canonical_commit_monotonic for batch in session.batches)) * 1000.0,
            )


_CLOSED_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_CLOSED_TOKEN = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _content_free_cause(error: BaseException, **fields: str | None) -> str:
    """One log fragment naming a failure without any vault content.

    The exception class and, when it carries one, a closed upper-case code; then
    each named field, kept only when it is a closed lower-case token. Messages,
    paths and argument values never appear.
    """
    code = getattr(error, "code", None)
    parts = [f"class={type(error).__name__}"]
    if isinstance(code, str) and _CLOSED_CODE.fullmatch(code):
        parts.append(f"code={code}")
    for name, value in fields.items():
        if isinstance(value, str) and _CLOSED_TOKEN.fullmatch(value):
            parts.append(f"{name}={value}")
    return " ".join(parts)


def _acknowledgement_component_names() -> tuple[str, ...]:
    """The derived components one acknowledgement is answerable for.

    `graph` and `write_advisory` carry their own terminal fields and are not
    part of `derived_sync_components`; the compact projection validates against
    the same closed set, so a name outside it would simply be dropped.
    """
    from . import derived_receipts

    return tuple(
        sorted(
            component.value
            for component in derived_receipts.DerivedComponent
            if component.value in _DERIVED_COMPONENT_NAMES
        )
    )


def _handoff_derived_batches(session: _FastAcknowledgementSession) -> int:
    """Cache exact custody before releasing canonical writers, without waits.

    A newer hold can move a path whose earlier receipt was already handed off.
    Re-prove only those overlapping receipts after publishing the newer custody.
    A failed handoff stays pending and cannot escape into canonical rollback.
    Returns how many receipts it proved, for the handoff's per-batch time.
    """
    proved = 0
    if session.handoff_in_progress:
        return proved
    pending = [
        batch
        for batch in session.batches
        if batch.handoff_outcome is None and batch.handoff_error is None
    ]
    if not pending:
        return proved
    changed_paths = {path.rel_path for batch in pending for path in batch.receipt.paths}
    affected = [
        batch
        for batch in reversed(session.batches)
        if any(path.rel_path in changed_paths for path in batch.receipt.paths)
    ]
    where: dict[str, str | None] = {"stage": "setup", "component": None, "outcome": None}
    # Public proof and publication guards reenter consistency_guard, including
    # on the direct leaf fallback where no enclosing canonical hold exists.
    session.handoff_in_progress = True
    try:
        from . import derived_receipts

        boundary_held = (
            active_manager()
            ._mutation_coordinator_for(session.vault_root)
            .current_thread_holds_boundary()
        )
        for batch in affected:
            batch.handoff_outcome = None
            batch.handoff_error = None
        # Newer live custody must exist before an older receipt can hand on its
        # moved paths. Component projection still uses commit order below.
        for batch in affected:
            receipt = batch.receipt
            observed_generation = (
                _current_canonical_generation(session.vault_root)
                or receipt.canonical_generation
            )
            where.update(stage="receipt_proof", component=None, outcome=None)
            with call_spans.span("derived.receipt_proof"):
                proof = derived_receipts.prove_committed(
                    session.vault_root,
                    receipt,
                    current_generation=observed_generation,
                )
            proved += 1
            where["outcome"] = proof.outcome
            if proof.batch_id != receipt.batch_id or proof.canonical_replay_authorized:
                raise RuntimeError("derived receipt proof does not match this batch")
            if proof.outcome != "superseded":
                if proof.outcome != "ready":
                    raise RuntimeError(
                        "derived receipt did not prove the committed generation"
                    )
                # Fresh proof and existing custody share the outer hold. The
                # direct fallback still publishes because its proof released it.
                where["stage"] = "pending_visibility"
                if not boundary_held or not derived_receipts.pending_visibility_complete(
                    session.vault_root, receipt
                ):
                    with call_spans.span("derived.pending_visibility"):
                        published = derived_receipts.publish_pending_visibility(
                            session.vault_root,
                            receipt,
                            publisher=_PENDING_VISIBILITY_PUBLISHER,
                        )
                    if not published:
                        raise RuntimeError("pending visibility publication was not proven")
                where["stage"] = "signal"
                derived_receipts.signal_components(session.vault_root, receipt)
            batch.handoff_outcome = proof.outcome
    except Exception as error:
        logger.warning(
            "fast acknowledgement failed; the persisted terminal degrades to "
            "derived_sync=pending (%s)",
            _content_free_cause(error, **where),
        )
        uncertain = _PostCommitOutcomeUncertain()
        uncertain.advisory_result_ref = _claimed_advisory_ref(session)
        for batch in affected:
            if batch.handoff_outcome is None:
                batch.handoff_error = uncertain
    finally:
        session.handoff_in_progress = False
    return proved


def _acknowledge_derived_batches_timed(
    result: Any,
    session: _FastAcknowledgementSession,
    *,
    budget_deadline: float | None = None,
) -> Any:
    # What this acknowledgement still owes, per batch: a component that batch 1
    # completed says nothing about batch 2's, so one shared set would under-report
    # a multi-batch mutation. A failure before any status is read owes the whole
    # set, which is the honest answer -- nothing about them was proven.
    owed: dict[str, set[str]] = {}
    # Where the acknowledgement was when it failed, for a content-free log:
    # closed stage names, a component name and a proof outcome, never a path.
    where: dict[str, str | None] = {"stage": "setup", "component": None, "outcome": None}

    def still_owed() -> tuple[str, ...] | None:
        """What is owed, or None when no batch was examined at all.

        `None` and `()` are different answers and the caller treats them so:
        nothing examined means nothing is known, and the honest report is the
        whole applicable set; an examined batch that owes nothing reports
        exactly that.
        """
        if not owed:
            return None
        return tuple(sorted(set().union(*owed.values())))

    try:
        from . import derived_receipts, index_sync

        snapshots = []
        advisory_refs: list[str] = []
        deadline = (
            min(batch.canonical_commit_monotonic for batch in session.batches)
            + _FAST_ACK_DEADLINE_SECONDS
        )
        if budget_deadline is not None:
            deadline = min(deadline, budget_deadline)
        # Direct leaves without a matching vault hold keep the guarded fallback.
        # Held writes reuse their exact handoff, including a cached failure.
        _timed_handoff(session)
        for batch in session.batches:
            if batch.handoff_error is not None:
                raise batch.handoff_error

        for batch in session.batches:
            receipt = batch.receipt
            superseded = batch.handoff_outcome == "superseded"
            # A superseded batch owes nothing: the newer batch that covers it
            # owns its convergence.
            batch_owed = set() if superseded else set(_acknowledgement_component_names())
            owed[receipt.batch_id] = batch_owed
            statuses = []
            for component in derived_receipts.DerivedComponent:
                if _fast_ack_monotonic() >= deadline:
                    break
                where.update(stage="component_status", component=component.value)
                status = derived_receipts.component_status(
                    session.vault_root, receipt, component
                )
                if superseded:
                    # Read the store, never the as-prepared memory state: the
                    # store has already closed these components as superseded,
                    # and a terminal built from memory would advertise pending
                    # work that no longer belongs to this batch.
                    statuses.append(status)
                    continue
                if status.state == "claimed" and component not in derived_receipts.UNACKNOWLEDGED_COMPONENTS:
                    where["stage"] = "component_wait"
                    status = _wait_for_derived_component(
                        session.vault_root,
                        receipt,
                        status,
                        deadline_monotonic=deadline,
                    )
                if status.state in {"completed", "not_required"}:
                    batch_owed.discard(str(component.value))
                statuses.append(status)
            if superseded:
                # The newer batch that superseded this one owns both the
                # convergence and the applicable advisory job, so no advisory
                # ref is resolved or carried here.
                snapshots.append(
                    index_sync.superseded_acknowledgement_snapshot(statuses)
                )
                continue
            snapshot = index_sync.derived_acknowledgement_snapshot(
                statuses,
                required=(
                    status.component for status in receipt.components
                    if status.state != "not_required"
                ),
            )
            snapshots.append(snapshot)
            if snapshot.advisory_sync != "not_required":
                # Prepared custody already carries this stable ref, even if its read was skipped.
                result_ref = next(
                    (status.advisory_result_ref for status in (*statuses, *receipt.components)
                     if status.component is derived_receipts.DerivedComponent.WRITE_ADVISORY
                     and status.advisory_result_ref),
                    None,
                )
                if result_ref is None:
                    result_ref = derived_receipts.advisory_result_ref(
                        session.vault_root, receipt
                    )
                if not result_ref:
                    raise RuntimeError("applicable advisory custody has no stable result ref")
                advisory_refs.append(result_ref)

        if len(advisory_refs) > 1:  # pragma: no cover - one job per session
            raise RuntimeError(
                "one acknowledgement cannot carry several advisory result refs"
            )
        derived_sync = _fast_ack_outcome(
            snapshot.derived_sync for snapshot in snapshots
        )
        advisory_sync = _fast_ack_outcome(
            (snapshot.advisory_sync for snapshot in snapshots),
            not_required=True,
        )
        diagnostics = [
            {"component": component, "state": state, "code": code}
            for snapshot in snapshots
            for component, state, code in snapshot.diagnostics
        ]
        return with_fast_acknowledgement(
            result,
            derived_sync=derived_sync,
            derived_sync_components=tuple(
                sorted(
                    {
                        component
                        for snapshot in snapshots
                        for component in snapshot.derived_sync_components
                    }
                )
            ),
            component_diagnostics=diagnostics,
            advisory_sync=advisory_sync,
            advisory_result_ref=advisory_refs[0] if advisory_refs else None,
        )
    except _PostCommitOutcomeUncertain:
        raise
    except Exception as error:
        # Content-free cause (owner ruling R4): the exception class, a closed
        # code, the stage, the component and the proof outcome -- never the
        # message, which can carry a path. The caller's terminal names the
        # derived components owed.
        logger.warning(
            "fast acknowledgement failed; the persisted terminal degrades to "
            "derived_sync=pending (%s)",
            _content_free_cause(error, **where),
        )
        uncertain = _PostCommitOutcomeUncertain()
        uncertain.derived_components = still_owed()
        uncertain.advisory_result_ref = _claimed_advisory_ref(session)
        raise uncertain from error


def _claimed_advisory_ref(session: _FastAcknowledgementSession) -> str | None:
    """The stable result ref for an advisory job this session already claimed.

    The claim is taken at the committed handoff and retained, so a failed
    acknowledgement leaves a real job behind. Reporting the advisory as pending
    without its ref would leave the caller holding a job it cannot ask about.
    """
    if not session.advisory_claimed:
        return None
    try:
        from . import derived_receipts

        for batch in reversed(session.batches):
            if batch.advisory_target is None:
                continue
            ref = derived_receipts.advisory_result_ref(session.vault_root, batch.receipt)
            if isinstance(ref, str) and ref:
                return ref
    except Exception:  # noqa: BLE001 - a committed terminal never depends on this probe
        return None
    return None


#: The one warning a lost acknowledgement adds to an already-persisted success
#: terminal. Names the derived components and nothing else: a failure message
#: can carry a vault path, and this string reaches the client.
_DERIVED_ACKNOWLEDGEMENT_PENDING_WARNING = (
    "derived state acknowledgement did not complete ({components}); "
    "derived state reconciles behind this committed write"
)


def _awaiting_derived_acknowledgement(result: Any) -> Any:
    """Mark a terminal that is durable but whose acknowledgement has not run.

    No components and no warning: nothing has failed yet, and the only claim
    being made is that derived state is not proven current at this instant.
    """
    if not isinstance(result, Mapping) or result.get("state") != "committed":
        return result
    if result.get("derived_sync") is not None:
        return result
    return {**result, "derived_sync": "pending"}


def _derived_acknowledgement_pending(result: Any, error: BaseException) -> Any:
    """Project a lost derived acknowledgement into a persisted success terminal.

    The commit is proven and the terminal is durable, so the honest report is a
    success whose derived custody has not caught up -- not the committed-uncertain
    error, which the contract reserves for a terminal that could not be persisted
    at all.

    `derived_sync_components` is how this reaches a compact client: compact
    carries the envelope fields and deliberately drops free-text warnings for
    artifact receipts, so the components have to travel in the field, not in
    the sentence.
    """
    if not isinstance(result, Mapping) or result.get("state") != "committed":
        return result
    reported = getattr(error, "derived_components", None)
    # `None` is "nothing known", which owes the whole set; an empty tuple is an
    # examined acknowledgement that owes nothing, and is left empty.
    components = _acknowledgement_component_names() if reported is None else tuple(reported)
    components = tuple(sorted({name for name in components if name in _DERIVED_COMPONENT_NAMES}))
    warning = _DERIVED_ACKNOWLEDGEMENT_PENDING_WARNING.format(
        components=", ".join(components) if components else "derived state"
    )
    terminal = dict(result)
    terminal["derived_sync"] = "pending"
    if components:
        terminal["derived_sync_components"] = list(components)
    terminal.pop("derived_sync_code", None)
    terminal.pop("derived_sync_next_action", None)
    # The advisory job was claimed with retention before the acknowledgement
    # ran, so it outlives the failure. Say so, with the ref that can be asked
    # about; say nothing at all rather than assert `not_required` for a job
    # whose ref could not be resolved.
    advisory_ref = getattr(error, "advisory_result_ref", None)
    if isinstance(advisory_ref, str) and advisory_ref.startswith(
        "exomem://write-advisory-result/"
    ):
        terminal["advisory_sync"] = "pending"
        terminal["advisory_result_ref"] = advisory_ref
        terminal.pop("advisory_sync_code", None)
        terminal.pop("advisory_sync_next_action", None)
    leaf = terminal.get("leaf_result")
    if isinstance(leaf, Mapping):
        existing = leaf.get("warnings")
        if isinstance(existing, (list, tuple)):
            warnings = [*existing, warning]
        elif existing:
            warnings = [existing, warning]
        else:
            warnings = [warning]
        terminal["leaf_result"] = {**leaf, "warnings": warnings}
    count = terminal.get("warnings_count")
    terminal["warnings_count"] = (
        count if isinstance(count, int) and not isinstance(count, bool) else 0
    ) + 1
    return terminal


def _direct_mutation_boundary(
    vault_root: os.PathLike[str] | str, state_root: Path
) -> tuple[str, Path]:
    root = (
        Path(vault_root)
        if isinstance(vault_root, str) and Path(vault_root).is_absolute()
        else vault_root
    )
    return (
        canonical_mutation_identity(root),
        state_root.expanduser().resolve(strict=False),
    )


def _carries_graph_rebuild_handoff(result: Any) -> bool:
    """Whether this result still holds an unfinalized graph-rebuild handoff."""
    if not isinstance(result, Mapping):
        return False
    leaf = result.get("leaf_result", result)
    return isinstance(leaf, Mapping) and isinstance(leaf.get("_graph_rebuild_handoff"), Mapping)


def _durable_graph_outcome(vault_root: Path) -> Any:
    """The terminal graph fragment provable from durable state alone, or None.

    A repair the durable queue owns has no registration to observe, so the
    terminal proves what it can from what is on disk. Two facts are provable and
    they are *different answers*:

    * the sidecar acknowledges the committed generation -- the graph is current
      for this write, which is exactly the `completed` this field has always
      meant;
    * it does not, and work is still queued against it -- `pending`.

    Reporting nothing for the first is what a queue-only probe does, and it grows
    **more** wrong as the drain gets healthier: a write whose repair landed before
    its response was shaped then says nothing about the graph at all, and a caller
    cannot tell a converged graph from an unreported one. Measured at 16 of 43
    writes in one 400-page run once the drain stopped being refused.

    Both terms still matter for `pending`: an unacknowledged checkpoint with an
    empty queue is a rebuild's business, and a queue holding later work says
    nothing about whether *this* generation converged.

    Never raises. This runs while shaping a terminal for canonical bytes that are
    already durable; a probe that failed loudly here would turn a successful
    write into an error report.
    """
    from . import deferred_index, graph_sync

    try:
        committed = graph_sync.read_checkpoint(vault_root)
        if committed is None:
            return None
        acknowledged = graph_sync.acknowledged_checkpoint(vault_root)
        if acknowledged is not None and acknowledged.covers(committed):
            return {"graph_sync": "completed"}
        if deferred_index.list_graph_paths(vault_root):
            return graph_sync.committed_graph_queued(committed)
        return None
    except Exception:  # noqa: BLE001 - a report must not fail a durable write
        logger.warning("durable graph outcome probe failed", exc_info=True)
        return None


def _windows_library(ctypes_module: Any, name: str) -> Any:
    return ctypes_module.WinDLL(name, use_last_error=True)


def _windows_last_error(ctypes_module: Any) -> int:
    return int(ctypes_module.get_last_error())


def _log_mutation_event(phase: str, *, level: int = logging.INFO, **fields: Any) -> None:
    prefix = (
        "event=hosted_call kind=mutation" if content_private_logging_enabled() else "event=mutation"
    )
    suffix = " ".join(f"{name}={value}" for name, value in fields.items())
    _mutation_logger.log(level, f"{prefix} phase={phase} {suffix}".rstrip())


def log_active_mutation_phase(phase: str, **fields: Any) -> None:
    """Log a canonical-writer phase against the active privacy-safe trace."""
    active = _ACTIVE_MUTATION_TRACE.get()
    if active is None:
        return
    request_id, command, receipt = active
    _log_mutation_event(
        phase,
        request_id=request_id,
        command=command,
        receipt=receipt,
        **fields,
    )


def mark_active_mutation_committed() -> None:
    """Mark that the current canonical writer crossed its durable commit boundary."""
    if _ACTIVE_MUTATION_TRACE.get() is not None:
        _ACTIVE_MUTATION_COMMITTED.set(True)


@contextmanager
def active_mutation_claim_context(
    *, claim_token: str | None, command_digest: str | None
) -> Iterator[None]:
    """Bind opaque per-claim protocol identity for canonical helper calls.

    The idempotency state-machine lane supplies the durable claim token.  The
    existing invocation path sets only its already-computed command digest, so
    existing callers retain their standalone random checkpoint identity.
    """
    if claim_token is not None and re.fullmatch(r"[0-9a-f]{24}", claim_token) is None:
        raise ValueError("claim token must be lowercase 24-hex")
    if command_digest is not None and re.fullmatch(r"[0-9a-f]{64}", command_digest) is None:
        raise ValueError("command digest must be lowercase SHA-256")
    token = _ACTIVE_MUTATION_PROTOCOL.set((claim_token, command_digest))
    try:
        yield
    finally:
        _ACTIVE_MUTATION_PROTOCOL.reset(token)


def active_mutation_claim_token() -> str | None:
    """Return the current opaque attempt claim token, if the caller has one."""
    active = _ACTIVE_MUTATION_PROTOCOL.get()
    return active[0] if active is not None else None


def active_mutation_command_digest() -> str | None:
    """Return the current normalized command digest without exposing arguments."""
    active = _ACTIVE_MUTATION_PROTOCOL.get()
    return active[1] if active is not None else None


class _PostCommitOutcomeUncertain(OpError):
    """Sanitized terminal state for an unexpected exception after canonical commit."""

    committed = True
    #: Derived components this acknowledgement could not prove, when it is the
    #: acknowledgement that failed. `None` means nothing is known about them --
    #: a terminal-persistence failure, or a failure before any batch was read --
    #: while an empty tuple means a batch was read and owes nothing.
    derived_components: tuple[str, ...] | None = None
    #: The claimed advisory job that outlives a failed acknowledgement, if one
    #: was claimed and its stable result ref could be resolved.
    advisory_result_ref: str | None = None

    def __init__(self) -> None:
        super().__init__(
            "MUTATION_COMMITTED_ACKNOWLEDGEMENT_UNCERTAIN",
            "the mutation completed but its exact terminal result could not be persisted",
            "Do not rerun with a new identity; reconcile and retry only with the same identity.",
            details={"status": "committed", "committed": True},
        )


def post_leaf_refusal(error: BaseException) -> BaseException:
    """Report a refusal raised after a write-capable leaf returned as an unknown outcome.

    A filter over the result, such as a store read that misses its deadline, runs after the
    leaf; its own refusal must not read as "nothing committed", and the filter cannot tell
    whether the leaf committed, prepared or rejected. A retry with the same identity replays
    the stored terminal; without one, the caller reconciles first. The inner code stays
    in the details. A fault without a public code is returned unchanged.
    """
    from .cli_ops import public_refusal

    refusal = public_refusal(error) if isinstance(error, Exception) else None
    if refusal is None:
        return error
    return OpError(
        "MUTATION_ACKNOWLEDGEMENT_PENDING",
        "the write returned, but a check of its result was refused; its outcome is not reported",
        details={"status": "uncertain", "committed": None,
                 "post_leaf_refusal": str(refusal.get("code") or "OP_ERROR")},
    )


@dataclass
class _ExecutionAttempt:
    """One bounded owner claim for a single canonical leaf execution."""

    attempt_id: str
    commit_token: str
    commit_secret: bytes
    handle: Any | None


class _WindowsDPAPISecretProtector:
    """Current-user DPAPI envelope for one local receipt-authentication key."""

    provider = 1

    def _crypt(self, secret: bytes, entropy: bytes, *, protect: bool) -> bytes:
        if protect and len(secret) != 32:
            raise ValueError("idempotency commit secret must be 32 bytes")
        from ctypes import wintypes

        class _DataBlob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

        def blob(value: bytes) -> tuple[_DataBlob, Any]:
            buffer = ctypes.create_string_buffer(value)
            return _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer

        source, _source_buffer = blob(secret)
        entropy_blob, _entropy_buffer = blob(entropy)
        output = _DataBlob()
        crypt32 = _windows_library(ctypes, "crypt32")
        if protect:
            routine = crypt32.CryptProtectData
            routine.argtypes = [
                ctypes.POINTER(_DataBlob),
                wintypes.LPCWSTR,
                ctypes.POINTER(_DataBlob),
                wintypes.LPVOID,
                wintypes.LPVOID,
                wintypes.DWORD,
                ctypes.POINTER(_DataBlob),
            ]
            ok = routine(
                ctypes.byref(source),
                None,
                ctypes.byref(entropy_blob),
                None,
                None,
                0x1,
                ctypes.byref(output),
            )
        else:
            routine = crypt32.CryptUnprotectData
            description = wintypes.LPWSTR()
            routine.argtypes = [
                ctypes.POINTER(_DataBlob),
                ctypes.POINTER(wintypes.LPWSTR),
                ctypes.POINTER(_DataBlob),
                wintypes.LPVOID,
                wintypes.LPVOID,
                wintypes.DWORD,
                ctypes.POINTER(_DataBlob),
            ]
            ok = routine(
                ctypes.byref(source),
                ctypes.byref(description),
                ctypes.byref(entropy_blob),
                None,
                None,
                0x1,
                ctypes.byref(output),
            )
            if description:
                _windows_library(ctypes, "kernel32").LocalFree(description)
        if not ok:
            raise OSError(_windows_last_error(ctypes), "Windows DPAPI operation failed")
        try:
            if output.cbData > _WINDOWS_SECRET_ENVELOPE_MAX_CIPHERTEXT:
                raise ValueError("Windows DPAPI output exceeds the idempotency envelope limit")
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            if not protect and output.pbData and output.cbData:
                try:
                    ctypes.memset(output.pbData, 0, output.cbData)
                except Exception:  # noqa: BLE001 - native cleanup must not mask DPAPI failure
                    pass
            if output.pbData:
                _windows_library(ctypes, "kernel32").LocalFree(output.pbData)

    def protect(self, secret: bytes, entropy: bytes) -> bytes:
        return self._crypt(secret, entropy, protect=True)

    def unprotect(self, ciphertext: bytes, entropy: bytes) -> bytes:
        result = self._crypt(ciphertext, entropy, protect=False)
        if len(result) != 32:
            raise ValueError("Windows DPAPI idempotency secret has invalid length")
        return result


@dataclass(frozen=True)
class _CanonicalResume:
    """A missing SQLite terminal backed by exact external commit evidence."""

    result: Any
    evidence: Any


@dataclass(frozen=True)
class _CanonicalCommittedFailure:
    """Canonical graph handoff retained before its stable public failure."""

    result: Any
    payload: dict[str, Any]


def _new_attempt() -> _ExecutionAttempt:
    # UUID hex is portable lowercase hexadecimal; trimming preserves the
    # protocol's opaque 96-bit identifier shape.
    attempt_id = uuid.uuid4().hex[:24]
    return _ExecutionAttempt(
        attempt_id=attempt_id,
        commit_token=uuid.uuid4().hex[:24],
        commit_secret=secrets.token_bytes(32),
        handle=None,
    )


def _call_after_canonical(
    callback: Any, result: Any, attempt: _ExecutionAttempt, canonical_disposition: str
) -> Any:
    """Support the pre-redesign one-argument hook during rolling upgrades."""
    try:
        return callback(result, attempt, canonical_disposition)
    except TypeError as error:
        try:
            return callback(result, attempt)
        except TypeError:
            try:
                return callback(result)
            except TypeError:
                raise error from None


def _invalid_committed_failure_payload() -> ValueError:
    return ValueError("invalid committed failure payload")


def _validate_committed_failure_payload(payload: Any) -> dict[str, Any]:
    """Return an owned copy of the one public committed-failure shape."""
    if type(payload) is not dict or set(payload) != _COMMITTED_FAILURE_TOP_KEYS:
        raise _invalid_committed_failure_payload()
    if (
        payload.get("code") != _COMMITTED_FAILURE_CODE
        or payload.get("message") != _COMMITTED_FAILURE_MESSAGE
        or payload.get("remediation") != _COMMITTED_FAILURE_REMEDIATION
    ):
        raise _invalid_committed_failure_payload()
    outcome = payload.get("outcome")
    if type(outcome) is not dict or set(outcome) != _COMMITTED_FAILURE_OUTCOME_KEYS:
        raise _invalid_committed_failure_payload()
    affected_count = outcome.get("affected_count")
    omitted_target_count = outcome.get("omitted_target_count")
    targets = outcome.get("targets")
    if (
        outcome.get("kind") != "cleanup_incomplete"
        or outcome.get("committed") is not True
        or outcome.get("incomplete") is not True
        or type(affected_count) is not int
        or affected_count < 0
        or type(omitted_target_count) is not int
        or omitted_target_count < 0
        or type(targets) is not list
        or len(targets) > 16
        or omitted_target_count != affected_count - len(targets)
    ):
        raise _invalid_committed_failure_payload()
    for target in targets:
        if type(target) is not str:
            raise _invalid_committed_failure_payload()
        try:
            encoded = target.encode("utf-8")
        except UnicodeEncodeError:
            raise _invalid_committed_failure_payload() from None
        parts = target.split("/")
        if (
            not target
            or target.startswith("/")
            or "\\" in target
            or "\0" in target
            or len(encoded) > 1024
            or _WINDOWS_DRIVE_PREFIX.match(target) is not None
            or any(part in {"", ".", ".."} for part in parts)
            or any(part.startswith(".exomem-batch-") for part in parts)
        ):
            raise _invalid_committed_failure_payload()
    return {
        "code": _COMMITTED_FAILURE_CODE,
        "message": _COMMITTED_FAILURE_MESSAGE,
        "remediation": _COMMITTED_FAILURE_REMEDIATION,
        "outcome": {
            "kind": "cleanup_incomplete",
            "committed": True,
            "incomplete": True,
            "affected_count": affected_count,
            "targets": list(targets),
            "omitted_target_count": omitted_target_count,
        },
    }


def _serialize_committed_failure_payload(payload: Any) -> bytes:
    validated = _validate_committed_failure_payload(payload)
    return json.dumps(
        validated,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _receipt_result_summary(value: Any, *, depth: int = 0) -> dict[str, Any]:
    """Build a bounded, content-free canonical summary of an ordinary result."""
    if value is None:
        return {"type": "none"}
    if type(value) is bool:
        return {"type": "bool", "value": value}
    if type(value) is int:
        magnitude = abs(value)
        width = max(1, (magnitude.bit_length() + 7) // 8)
        return {
            "type": "int",
            "negative": value < 0,
            "bits": magnitude.bit_length(),
            "sha256": hashlib.sha256(magnitude.to_bytes(width, "big")).hexdigest(),
        }
    if type(value) is float:
        if math.isnan(value):
            encoded = b"nan"
        elif math.isinf(value):
            encoded = b"-inf" if value < 0 else b"inf"
        else:
            encoded = value.hex().encode("ascii")
        return {"type": "float", "sha256": hashlib.sha256(encoded).hexdigest()}
    if type(value) is str:
        encoded = value.encode("utf-8")
        return {
            "type": "str",
            "bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }
    if type(value) is bytes:
        return {
            "type": "bytes",
            "bytes": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }
    if depth >= _RECEIPT_RESULT_SUMMARY_MAX_DEPTH:
        return {"type": "truncated"}
    if isinstance(value, Mapping):
        try:
            size = len(value)
        except Exception:  # noqa: BLE001 - an arbitrary mapping may not report a length
            size = None
        if size is not None and size > _RECEIPT_RESULT_SUMMARY_MAX_ITEMS:
            return {"type": "mapping", "size": size, "truncated": True}
        try:
            items = []
            for index, (key, item) in enumerate(value.items()):
                if index >= _RECEIPT_RESULT_SUMMARY_MAX_ITEMS:
                    return {"type": "mapping", "truncated": True}
                items.append(
                    [
                        _receipt_result_summary(key, depth=depth + 1),
                        _receipt_result_summary(item, depth=depth + 1),
                    ]
                )
            items.sort(
                key=lambda item: json.dumps(
                    item[0],
                    allow_nan=False,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        except Exception:  # noqa: BLE001 - an arbitrary mapping is summarized closed
            return {"type": "opaque"}
        return {
            "type": "mapping",
            "items": items,
            "truncated": False,
        }
    if type(value) in {list, tuple}:
        sequence_items = [
            _receipt_result_summary(item, depth=depth + 1)
            for item in value[:_RECEIPT_RESULT_SUMMARY_MAX_ITEMS]
        ]
        return {
            "type": "sequence",
            "items": sequence_items,
            "truncated": len(value) > len(sequence_items),
        }
    return {"type": "opaque"}


def _receipt_result_sha256(result: Any) -> str:
    """Hash a deterministic bounded summary without retaining leaf content."""
    summary = _receipt_result_summary(result)
    return hashlib.sha256(
        json.dumps(
            summary, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _invalid_committed_failure_payload()
        result[key] = value
    return result


def _deserialize_committed_failure_payload(payload: Any) -> dict[str, Any]:
    if type(payload) is not bytes:
        raise _invalid_committed_failure_payload()
    try:
        decoded = payload.decode("utf-8")
        parsed = json.loads(decoded, object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _invalid_committed_failure_payload() from None
    return _validate_committed_failure_payload(parsed)


def _committed_failure_payload(error: Exception) -> dict[str, Any] | None:
    if getattr(error, "committed", None) is not True:
        return None
    public_dict = getattr(error, "as_public_dict", None)
    if not callable(public_dict):
        return None
    try:
        return _validate_committed_failure_payload(public_dict())
    except Exception:  # noqa: BLE001 - arbitrary exception payloads are not cacheable
        return None


class _CachedCommittedFailure(ValueError):
    """Reconstructed public failure containing no original exception state."""

    committed = True

    def __init__(self, payload: Any):
        self._payload = _validate_committed_failure_payload(payload)
        self.code = _COMMITTED_FAILURE_CODE
        ValueError.__init__(self, self.__str__())

    def as_public_dict(self) -> dict[str, Any]:
        return _validate_committed_failure_payload(self._payload)

    def __str__(self) -> str:
        return self._payload_json()

    def _payload_json(self) -> str:
        return _serialize_committed_failure_payload(self._payload).decode("utf-8")


def _default_state_dir() -> Path:
    """Where lease state lives when the caller names nowhere else.

    A `default_factory`, not a plain default, because a plain one is
    evaluated when the class body runs -- at import. `Path.home()` raises
    `RuntimeError` outright when the environment names no home, which is
    the ordinary state of a service account, a container started without
    `HOME`, and any child handed a minimal environment. That made
    `import exomem.commands` fail there, so the whole command surface was
    unreachable for want of a directory nothing had asked for yet.

    Resolving it here moves the failure to the caller that actually wants
    the default path, and leaves every caller that passes its own
    `state_dir` -- the hosted cell and the service both do -- working with
    no home at all.
    """
    return Path.home() / ".cache" / "exomem"


@dataclass(frozen=True)
class LeaseConfig:
    url: str | None = None
    vault_id: str | None = None
    replica_id: str | None = None
    token: str | None = None
    ttl_seconds: float = 30.0
    timeout_seconds: float = 3.0
    preferred_writer: bool = False
    state_dir: Path = field(default_factory=_default_state_dir)
    # How long a writer waits for the vault mutation boundary before giving up
    # with MUTATION_BUSY.
    #
    # This is a *share of the edge budget*, not a free parameter. The HA edge
    # worker abandons a mutation-capable request at MCP_TOOL_TIMEOUT_MS
    # (historically 60s at the retired HA edge worker) and deliberately does
    # not replay it, because the origin may commit after the edge stops
    # waiting. Queueing here spends that same budget: time spent waiting is
    # unavailable to the write itself. A value at or near the edge timeout
    # guarantees the caller sees a 504 while the write commits anyway — the
    # exact acknowledgement loss this system works hardest to avoid.
    #
    # 5s leaves the large majority of the 60s budget for the write, whose own
    # cost is dominated by full-corpus contract validation (measured 12-45s
    # warm at 2.4k pages, 2026-07). Raise this only in tandem with
    # MCP_TOOL_TIMEOUT_MS, and never to meet or exceed it. The real headroom
    # win is shortening the critical section — the per-write corpus parse is
    # uncached and the corpus-aware embedding pass runs inside the boundary —
    # not widening the wait.
    mutation_timeout_seconds: float = 5.0
    # A non-preferred holder that has issued no mutation for this long hands
    # writer authority back on its own, so a laptop that is powered on but
    # not in use stops blocking the desktop indefinitely (design.md GAP A).
    # `0` disables idle release entirely. A preferred replica is exempt —
    # see writer_lease.py's `_maybe_idle_release` docstring for why.
    idle_release_seconds: float = 60.0
    # This is the binary's writer-side schema contract, not a caller-selected
    # view of the vault. Pre-v4 clients omit the field entirely; the external
    # coordinator interprets that released wire shape as schema 3.
    schema_version: int = 4
    # These deployment modes are a closed protocol contract, not vault vocabulary.
    topology: str = "multi-host"

    def __post_init__(self) -> None:
        if self.topology not in {"multi-host", "single-host"}:  # nosemgrep: ep-word-set -- Closed deployment modes select implemented auth boundaries.
            raise ValueError(
                "WRITER_LEASE_CONFIG: EXOMEM_WRITER_LEASE_TOPOLOGY must be multi-host or single-host"
            )
        if self.topology == "single-host":
            # Local session identities are safe only while authority stays on this host.
            try:
                origin = urllib.parse.urlsplit(self.url or "")
                address = ipaddress.ip_address(origin.hostname or "")
                origin.port  # Validate the URL parser's port field before using the origin.
            except ValueError:
                origin = None
            if (
                origin is None
                or origin.scheme not in ("http", "https")
                or not address.is_loopback
                or getattr(address, "scope_id", None) is not None
                or origin.username is not None
                or origin.password is not None
                or origin.path not in ("", "/")
                or origin.query or origin.fragment
                or self.url not in (f"{origin.scheme}://{origin.netloc}", f"{origin.scheme}://{origin.netloc}/")
            ):
                raise ValueError(
                    "WRITER_LEASE_CONFIG: single-host requires EXOMEM_WRITER_LEASE_URL "
                    "to be a credential-free literal loopback HTTP origin"
                )
            object.__setattr__(self, "url", f"{origin.scheme}://{origin.netloc}")

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LeaseConfig:
        values = os.environ if env is None else env
        url = values.get("EXOMEM_WRITER_LEASE_URL", "").strip() or None
        topology = values.get("EXOMEM_WRITER_LEASE_TOPOLOGY", "").strip() or "multi-host"
        state_raw = values.get("EXOMEM_WRITER_LEASE_STATE_DIR", "").strip()
        ttl_seconds = _positive_float(values, "EXOMEM_WRITER_LEASE_TTL", 30.0)
        idle_raw = values.get("EXOMEM_WRITER_LEASE_IDLE_SECONDS", "").strip()
        if idle_raw:
            idle_release_seconds = _non_negative_float(
                values, "EXOMEM_WRITER_LEASE_IDLE_SECONDS", 60.0
            )
            if 0 < idle_release_seconds < ttl_seconds:
                raise ValueError(
                    "WRITER_LEASE_CONFIG: EXOMEM_WRITER_LEASE_IDLE_SECONDS must be 0 (disabled) "
                    "or >= EXOMEM_WRITER_LEASE_TTL"
                )
        else:
            # The default tracks the TTL so raising EXOMEM_WRITER_LEASE_TTL
            # alone can never trip the idle>=ttl validation and brick startup.
            idle_release_seconds = max(60.0, ttl_seconds)
        config = cls(
            url=url if topology == "single-host" else (url.rstrip("/") if url else None),
            vault_id=values.get("EXOMEM_WRITER_LEASE_VAULT_ID", "").strip() or None,
            replica_id=values.get("EXOMEM_WRITER_LEASE_REPLICA_ID", "").strip() or None,
            token=values.get("EXOMEM_WRITER_LEASE_TOKEN", "").strip() or None,
            ttl_seconds=ttl_seconds,
            timeout_seconds=_positive_float(values, "EXOMEM_WRITER_LEASE_TIMEOUT", 3.0),
            preferred_writer=_truthy(values.get("EXOMEM_WRITER_LEASE_PREFERRED", "")),
            state_dir=Path(state_raw).expanduser() if state_raw else _default_state_dir(),
            mutation_timeout_seconds=_positive_float(values, "EXOMEM_MUTATION_TIMEOUT", 5.0),
            idle_release_seconds=idle_release_seconds,
            topology=topology,
        )
        if config.enabled and (not config.vault_id or not config.replica_id):
            raise ValueError(
                "WRITER_LEASE_CONFIG: EXOMEM_WRITER_LEASE_VAULT_ID and "
                "EXOMEM_WRITER_LEASE_REPLICA_ID are required when coordination is enabled"
            )
        return config


def _positive_float(values: Mapping[str, str], name: str, default: float) -> float:
    raw = values.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        raise ValueError(f"WRITER_LEASE_CONFIG: {name} must be a number") from None
    if value <= 0:
        raise ValueError(f"WRITER_LEASE_CONFIG: {name} must be positive")
    return value


def _non_negative_float(values: Mapping[str, str], name: str, default: float) -> float:
    raw = values.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        raise ValueError(f"WRITER_LEASE_CONFIG: {name} must be a number") from None
    if value < 0:
        raise ValueError(f"WRITER_LEASE_CONFIG: {name} must be non-negative")
    return value


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


COLLECTION_STORE_CAPABILITY = "collections-store-v1"
#: An offline owner step (``collection_store.admission``) holds the lease as its replica id
#: plus this suffix, so the coordinator arbitrates it against that replica's own service.
OFFLINE_OWNER_SUFFIX = "-owner"


def holder_text(holder: str | None, expires_at: float | None) -> str:
    """How a refusal names a lease holder; an offline owner step says so and when its lease expires."""
    if holder is None:
        return "unassigned"
    if not holder.endswith(OFFLINE_OWNER_SUFFIX):
        return holder
    if expires_at is None:
        return f"an offline owner step ({holder})"
    expiry = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires_at))
    return f"an offline owner step ({holder}, whose lease expires at {expiry})"


def _collection_store_uuid(value: object) -> str:
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("collection store identity must be a canonical UUID")
    return value


@dataclass(frozen=True)
class CollectionStoreHead:
    store_id: str
    instance_id: str
    commit_seq: int
    head_hash: str | None

    def __post_init__(self) -> None:
        _collection_store_uuid(self.store_id)
        _collection_store_uuid(self.instance_id)
        if type(self.commit_seq) is not int or not 0 <= self.commit_seq <= 2**63 - 1:
            raise ValueError("collection store sequence must be a nonnegative SQLite integer")
        if self.commit_seq == 0:
            if self.head_hash is not None:
                raise ValueError("collection store genesis hash must be null")
        elif (
            not isinstance(self.head_hash, str)
            or re.fullmatch("[0-9a-f]{64}", self.head_hash) is None
        ):
            raise ValueError("collection store head hash must be lowercase 64-hex")

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> CollectionStoreHead:
        if not isinstance(data, Mapping) or set(data) != {
            "store_id",
            "instance_id",
            "commit_seq",
            "head_hash",
        }:
            raise ValueError("collection store head fields are invalid")
        return cls(**data)

    def as_dict(self) -> dict[str, Any]:
        return {
            "store_id": self.store_id,
            "instance_id": self.instance_id,
            "commit_seq": self.commit_seq,
            "head_hash": self.head_hash,
        }


@dataclass(frozen=True)
class CollectionStoreFenceState:
    capability: str
    enrolled: bool
    store_id: str | None
    generation: int

    def __post_init__(self) -> None:
        if self.capability != COLLECTION_STORE_CAPABILITY or type(self.enrolled) is not bool:
            raise ValueError("collection store fence capability or enrollment is invalid")
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("collection store fence generation is invalid")
        if self.enrolled:
            _collection_store_uuid(self.store_id)
            if self.generation < 1:
                raise ValueError("collection store fence generation is invalid")
        # An unenrolled fence reports its newest retired generation, or 0 if never enrolled.
        elif self.store_id is not None:
            raise ValueError("collection store fence metadata is inconsistent")

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> CollectionStoreFenceState:
        if not isinstance(data, Mapping) or set(data) != {
            "capability",
            "enrolled",
            "store_id",
            "generation",
        }:
            raise ValueError("collection store fence response fields are invalid")
        return cls(**data)

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "enrolled": self.enrolled,
            "store_id": self.store_id,
            "generation": self.generation,
        }


@dataclass(frozen=True)
class RetiredCollectionStoreFence:
    """The newest retired store fence and the head the coordinator recorded for it.

    Generation 0, with no store and no head, means the vault's fence was never retired.
    """

    store_id: str | None
    generation: int
    collection_store_head: CollectionStoreHead | None

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> RetiredCollectionStoreFence:
        # nosemgrep: ep-word-set -- the closed field names of the coordinator's retired-fence response.
        if not isinstance(data, Mapping) or set(data) != {"store_id", "generation", "collection_store_head"}:
            raise ValueError("retired collection store fence fields are invalid")
        generation, head = data["generation"], data["collection_store_head"]
        if type(generation) is not int or generation < 0:
            raise ValueError("retired collection store fence generation is invalid")
        if generation == 0:
            if data["store_id"] is not None or head is not None:
                raise ValueError("a never-retired fence names no store")
            return cls(None, 0, None)
        store_id = _collection_store_uuid(data["store_id"])
        head = None if head is None else CollectionStoreHead.from_json(head)
        if head is not None and head.store_id != store_id:
            raise ValueError("retired collection store head names another store")
        return cls(store_id, generation, head)


@dataclass(frozen=True)
class LeaseRecord:
    holder: str | None
    expires_at: float | None
    fencing_token: int
    granted: bool = False
    required_schema_version: int | None = None
    schema_fence_generation: int | None = None
    governance_enrolled: bool = False
    required_collection_store_capability: str | None = None
    collection_store_fence_generation: int | None = None
    collection_store_head: CollectionStoreHead | None = None

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> LeaseRecord:
        holder = data.get("holder")
        expires = data.get("expires_at")
        token = data.get("fencing_token", 0)
        granted = data.get("granted", False)
        enrolled = data.get("governance_enrolled", False)
        if holder is not None and not isinstance(holder, str):
            raise ValueError("holder must be a string or null")
        if expires is not None and not isinstance(expires, (int, float)):
            raise ValueError("expires_at must be a number or null")
        if isinstance(token, bool) or not isinstance(token, int):
            raise ValueError("fencing_token must be an integer")
        if not isinstance(granted, bool) or not isinstance(enrolled, bool):
            raise ValueError("lease booleans are invalid")
        required_schema = data.get("required_schema_version")
        fence_generation = data.get("schema_fence_generation")
        if required_schema is not None and (
            isinstance(required_schema, bool)
            or not isinstance(required_schema, int)
            or required_schema not in {3, 4}
        ):
            raise ValueError("required_schema_version must be 3, 4, or null")
        if fence_generation is not None and (
            isinstance(fence_generation, bool)
            or not isinstance(fence_generation, int)
            or fence_generation < 1
        ):
            raise ValueError("schema_fence_generation must be a positive integer or null")
        has_fence = required_schema is not None and fence_generation is not None
        if enrolled != has_fence:
            raise ValueError("schema fence metadata is inconsistent")
        store_fields = {
            "required_collection_store_capability",
            "collection_store_fence_generation",
            "collection_store_head",
        }
        store_capability = data.get("required_collection_store_capability")
        store_generation = data.get("collection_store_fence_generation")
        store_head = data.get("collection_store_head")
        if store_fields.intersection(data):
            if (
                not store_fields.issubset(data)
                or store_capability != COLLECTION_STORE_CAPABILITY
                or type(store_generation) is not int
                or store_generation < 1
            ):
                raise ValueError("collection store fence metadata is inconsistent")
            if store_head is not None:
                store_head = CollectionStoreHead.from_json(store_head)
        return cls(
            holder,
            float(expires) if expires is not None else None,
            token,
            granted,
            required_schema,
            fence_generation,
            enrolled,
            store_capability,
            store_generation,
            store_head,
        )


@dataclass(frozen=True)
class SchemaAdmission:
    admitted: bool
    governance_enrolled: bool
    required_schema_version: int | None
    schema_fence_generation: int | None

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> SchemaAdmission:
        expected_fields = {
            "admitted",
            "governance_enrolled",
            "required_schema_version",
            "schema_fence_generation",
        }
        if set(data) != expected_fields:
            raise ValueError("schema admission response fields are invalid")
        admitted = data.get("admitted")
        enrolled = data.get("governance_enrolled")
        required = data.get("required_schema_version")
        generation = data.get("schema_fence_generation")
        if not isinstance(admitted, bool) or not isinstance(enrolled, bool):
            raise ValueError("schema admission booleans are invalid")
        if required is not None and (
            isinstance(required, bool) or not isinstance(required, int) or required not in {3, 4}
        ):
            raise ValueError("required_schema_version is invalid")
        if generation is not None and (
            isinstance(generation, bool) or not isinstance(generation, int) or generation < 1
        ):
            raise ValueError("schema_fence_generation is invalid")
        has_fence = required is not None and generation is not None
        if enrolled != has_fence or (admitted and not enrolled):
            raise ValueError("schema admission response has inconsistent authority")
        return cls(admitted, enrolled, required, generation)

    def as_dict(self) -> dict[str, Any]:
        return {
            "admitted": self.admitted,
            "governance_enrolled": self.governance_enrolled,
            "required_schema_version": self.required_schema_version,
            "schema_fence_generation": self.schema_fence_generation,
        }


@dataclass(frozen=True)
class SchemaFenceState:
    governance_enrolled: bool
    schema_version: int
    generation: int

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> SchemaFenceState:
        if set(data) != {"governance_enrolled", "schema_version", "generation"}:
            raise ValueError("schema fence response fields are invalid")
        enrolled = data.get("governance_enrolled")
        schema_version = data.get("schema_version")
        generation = data.get("generation")
        if enrolled is not True:
            raise ValueError("schema fence enrollment is invalid")
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version not in {3, 4}
        ):
            raise ValueError("schema fence version is invalid")
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
        ):
            raise ValueError("schema fence generation is invalid")
        return cls(enrolled, schema_version, generation)


#: Statuses that mean the URL answered and does not implement the lease
#: contract at this route: 404 (no such route or operation), 405 (route exists,
#: wrong method), 501 (the server does not implement it). A real coordinator
#: 404s only for an unknown operation and this client sends none, so a 404 here
#: is the wrong URL or an incompatible coordinator -- never one that is merely
#: busy. That distinction is the whole point: an unavailable coordinator is
#: worth asking again, and one that does not serve the contract will return the
#: same answer forever. Collapsing them made a misconfigured lease URL look
#: exactly like a transient outage, and the 5s availability probe polled it
#: 1,200 times in 25 minutes from seven server processes, none of which ever
#: said why (#550).
_CONTRACT_ABSENT_STATUSES = frozenset({404, 405, 501})


def _redacted_coordinator_url(url: str) -> str:
    """The coordinator URL without any embedded credential.

    `https://user:secret@host/` is a legal coordinator URL, and this string
    reaches logs and, through the error, MCP clients.
    """
    try:
        parts = urllib.parse.urlsplit(str(url))
    except ValueError:
        return "<unparsable coordinator url>"
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urllib.parse.urlunsplit((parts.scheme, host, parts.path, "", ""))


def _coordinator_unavailable_error(exc: BaseException) -> OpError:
    return OpError(
        "WRITER_COORDINATOR_UNAVAILABLE",
        f"writer coordinator could not confirm authority: {exc}",
        "Check the coordinator URL, credentials, and service health; reads remain available.",
    )


def _contract_absent_error(url: str, route: str, status: int) -> OpError:
    """A URL that answers but does not serve the writer-lease contract.

    Fail-closed exactly like an unavailable coordinator -- authority is still
    unproven, so writes still refuse. What changes is that the refusal names a
    configuration fault instead of an outage, and callers that poll can slow
    down instead of asking a 404 the same question every five seconds.
    """
    safe_url = _redacted_coordinator_url(url)
    logger.warning(
        "writer-lease coordinator %s answered %d for %s; "
        "this URL does not serve the writer-lease contract",
        safe_url,
        status,
        route,
    )
    return OpError(
        "WRITER_COORDINATOR_CONTRACT_ABSENT",
        f"the configured writer-lease coordinator answered {status} for {route}",
        "Point the writer-lease URL at a service that implements "
        "/v1/vaults/<id>/lease, or disable coordination. Retrying cannot "
        "change this answer; reads remain available and writes stay "
        "fail-closed until it is corrected.",
        details={"status": status, "coordinator_url": safe_url},
    )


class _SingleHostRedirectHandler(urllib.request.HTTPRedirectHandler):
    """A local authority cannot forward its bearer to another coordinator origin."""

    def redirect_request(self, request, response, code, message, headers, new_url):
        previous = urllib.parse.urlsplit(request.full_url)
        target = urllib.parse.urlsplit(new_url)
        if (target.scheme, target.netloc) != (previous.scheme, previous.netloc):
            raise urllib.error.URLError("single-host coordinator redirected outside its origin")
        return super().redirect_request(request, response, code, message, headers, new_url)


class LeaseCoordinatorClient:
    """Small stdlib HTTP client for the provider-neutral lease contract."""

    def __init__(self, config: LeaseConfig):
        if not config.enabled:
            raise ValueError("coordinator client requires enabled configuration")
        self.config = config

    def acquire(self, *, collection_store_capability: str | None = None) -> LeaseRecord:
        body = {
            "replica_id": self.config.replica_id,
            "ttl_seconds": self.config.ttl_seconds,
            "schema_version": self.config.schema_version,
        }
        if collection_store_capability is not None:
            body["collection_store_capability"] = collection_store_capability
        return self._request("POST", "acquire", body)

    def renew(
        self,
        fencing_token: int,
        *,
        collection_store_capability: str | None = None,
        collection_store_head: CollectionStoreHead | None = None,
    ) -> LeaseRecord:
        body = {
            "replica_id": self.config.replica_id,
            "fencing_token": fencing_token,
            "ttl_seconds": self.config.ttl_seconds,
            "schema_version": self.config.schema_version,
        }
        if collection_store_capability is not None:
            body["collection_store_capability"] = collection_store_capability
        if collection_store_head is not None:
            body["collection_store_head"] = collection_store_head.as_dict()
        return self._request("POST", "renew", body)

    def release(
        self,
        fencing_token: int,
        *,
        collection_store_head: CollectionStoreHead | None = None,
    ) -> LeaseRecord:
        replica_id = self.config.replica_id
        assert replica_id is not None
        if collection_store_head is None:
            return self.release_holder(replica_id, fencing_token)
        return self._request(
            "POST",
            "release",
            {
                "replica_id": replica_id,
                "fencing_token": fencing_token,
                "collection_store_head": collection_store_head.as_dict(),
            },
        )

    def release_holder(self, holder_replica_id: str, fencing_token: int) -> LeaseRecord:
        """Release on behalf of ANY holder, not just this client's own
        configured `replica_id` — the ops-only `exomem lease release`
        cross-device path (R6). No coordinator change needed: the server
        already keys `/release` on the body's `replica_id` + `fencing_token`
        rather than the caller's own identity (the bearer token is a single
        shared HA-cell secret, not a per-replica credential)."""
        return self._request(
            "POST",
            "release",
            {"replica_id": holder_replica_id, "fencing_token": fencing_token},
        )

    def status(self) -> LeaseRecord:
        return self._request("GET", "", None)

    def anonymous_status_code(self) -> int:
        """Return the native HTTP result without sending the writer credential."""
        request = urllib.request.Request(f"{self.config.url}{self._lease_route()}")
        try:
            with self._open_request(request) as response:
                return response.status
        except urllib.error.HTTPError as error:
            with error:
                return error.code
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise _coordinator_unavailable_error(error) from None

    def collection_store_fence(self) -> CollectionStoreFenceState:
        vault = urllib.parse.quote(str(self.config.vault_id), safe="")
        payload = self._request_json(
            "GET",
            f"/v1/vaults/{vault}/collection-store-fence",
            None,
            contract_route="/v1/vaults/<id>/collection-store-fence",
        )
        try:
            return CollectionStoreFenceState.from_json(payload)
        except ValueError as exc:
            raise _coordinator_unavailable_error(exc) from None

    def transition_collection_store_fence(
        self,
        *,
        expected_generation: int,
        store_id: str,
    ) -> CollectionStoreFenceState:
        if type(expected_generation) is not int or expected_generation < 0:
            raise ValueError("expected_generation must be a nonnegative integer")
        _collection_store_uuid(store_id)
        vault = urllib.parse.quote(str(self.config.vault_id), safe="")
        payload = self._request_json(
            "PUT",
            f"/v1/vaults/{vault}/collection-store-fence",
            {
                "expected_generation": expected_generation,
                "store_id": store_id,
                "capability": COLLECTION_STORE_CAPABILITY,
            },
            contract_route="/v1/vaults/<id>/collection-store-fence",
        )
        try:
            state = CollectionStoreFenceState.from_json(payload)
            if (
                not state.enrolled
                or state.store_id != store_id
                or state.generation not in {expected_generation, expected_generation + 1}
            ):
                raise ValueError("collection store fence transition result is inconsistent")
            return state
        except ValueError as exc:
            raise _coordinator_unavailable_error(exc) from None

    def retire_collection_store_fence(
        self,
        *,
        expected_generation: int,
        store_id: str,
    ) -> CollectionStoreFenceState:
        """Retire the enrolled store fence; the operator credential only, on a vault exported to files."""
        if type(expected_generation) is not int or expected_generation < 1:
            raise ValueError("expected_generation must be a positive integer")
        _collection_store_uuid(store_id)
        vault = urllib.parse.quote(str(self.config.vault_id), safe="")
        payload = self._request_json(
            "POST",
            f"/v1/vaults/{vault}/collection-store-fence/retire",
            {
                "expected_generation": expected_generation,
                "store_id": store_id,
                "capability": COLLECTION_STORE_CAPABILITY,
            },
            contract_route="/v1/vaults/<id>/collection-store-fence/retire",
        )
        try:
            state = CollectionStoreFenceState.from_json(payload)
            if state.enrolled or state.generation != expected_generation:
                raise ValueError("collection store fence retire result is inconsistent")
            return state
        except ValueError as exc:
            raise _coordinator_unavailable_error(exc) from None

    def retired_collection_store_fence(self) -> RetiredCollectionStoreFence:
        """The newest retired store fence with its recorded head, on a route only this release calls."""
        vault = urllib.parse.quote(str(self.config.vault_id), safe="")
        payload = self._request_json(
            "GET",
            f"/v1/vaults/{vault}/collection-store-fence/retired",
            None,
            contract_route="/v1/vaults/<id>/collection-store-fence/retired",
        )
        try:
            return RetiredCollectionStoreFence.from_json(payload)
        except ValueError as exc:
            raise _coordinator_unavailable_error(exc) from None

    def probe_collection_store_fence_retire(self) -> None:
        """Prove that the coordinator serves the fence retire and accepts this operator credential.

        The probe sends no field: the coordinator checks the operator credential, then
        answers 400 before it reads any fence, so the probe changes nothing. Only that
        400 proves both; any other answer refuses.
        """
        contract_route = "/v1/vaults/<id>/collection-store-fence/retire"
        vault = urllib.parse.quote(str(self.config.vault_id), safe="")
        request = self._json_request("POST", f"/v1/vaults/{vault}/collection-store-fence/retire", {})
        try:
            with self._open_request(request):
                pass
        except urllib.error.HTTPError as exc:
            if exc.code == 400:
                return
            if exc.code in _CONTRACT_ABSENT_STATUSES:
                raise _contract_absent_error(self.config.url, contract_route, exc.code) from None
            if exc.code == 401:
                raise OpError(
                    "SCHEMA_FENCE_OPERATOR_UNAVAILABLE",
                    "the coordinator rejected the operator credential",
                    "Set EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN to the coordinator's operator token.",
                ) from None
            raise _coordinator_unavailable_error(exc) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise _coordinator_unavailable_error(exc) from None
        raise _coordinator_unavailable_error(
            ValueError("the coordinator accepted a fence retire that names no fence")
        )

    def schema_admission(self, schema_version: int) -> SchemaAdmission:
        if isinstance(schema_version, bool) or schema_version not in {3, 4}:
            raise ValueError("schema_version must be 3 or 4")
        vault = urllib.parse.quote(str(self.config.vault_id), safe="")
        payload = self._request_json(
            "POST",
            f"/v1/vaults/{vault}/schema-fence/admit",
            {
                "replica_id": self.config.replica_id,
                "schema_version": schema_version,
            },
            contract_route="/v1/vaults/<id>/schema-fence/admit",
        )
        try:
            admission = SchemaAdmission.from_json(payload)
            expected_admission = bool(
                admission.governance_enrolled
                and admission.required_schema_version == schema_version
            )
            if admission.admitted != expected_admission:
                raise ValueError("schema admission decision disagrees with its fence")
            return admission
        except ValueError as exc:
            raise _coordinator_unavailable_error(exc) from None

    def schema_fence(self) -> SchemaFenceState:
        vault = urllib.parse.quote(str(self.config.vault_id), safe="")
        payload = self._request_json(
            "GET",
            f"/v1/vaults/{vault}/schema-fence",
            None,
            contract_route="/v1/vaults/<id>/schema-fence",
        )
        try:
            return SchemaFenceState.from_json(payload)
        except ValueError as exc:
            raise _coordinator_unavailable_error(exc) from None

    def transition_schema_fence(
        self,
        *,
        expected_generation: int,
        schema_version: int,
    ) -> SchemaFenceState:
        if (
            isinstance(expected_generation, bool)
            or not isinstance(expected_generation, int)
            or expected_generation < 1
        ):
            raise ValueError("expected_generation must be a positive integer")
        if isinstance(schema_version, bool) or schema_version not in {3, 4}:
            raise ValueError("schema_version must be 3 or 4")
        vault = urllib.parse.quote(str(self.config.vault_id), safe="")
        payload = self._request_json(
            "PUT",
            f"/v1/vaults/{vault}/schema-fence",
            {
                "expected_generation": expected_generation,
                "schema_version": schema_version,
            },
            contract_route="/v1/vaults/<id>/schema-fence",
        )
        try:
            state = SchemaFenceState.from_json(payload)
            if (
                state.schema_version != schema_version
                or state.generation != expected_generation + 1
            ):
                raise ValueError("schema fence transition result is inconsistent")
            return state
        except ValueError as exc:
            raise _coordinator_unavailable_error(exc) from None

    def _lease_route(self, operation: str = "") -> str:
        vault = urllib.parse.quote(str(self.config.vault_id), safe="")
        suffix = f"/{operation}" if operation else ""
        return f"/v1/vaults/{vault}/lease{suffix}"

    def _request(self, method: str, operation: str, body: dict | None) -> LeaseRecord:
        payload = self._request_json(
            method,
            self._lease_route(operation),
            body,
            contract_route="/v1/vaults/<id>/lease" + (f"/{operation}" if operation else ""),
        )
        try:
            return LeaseRecord.from_json(payload)
        except ValueError as exc:
            raise _coordinator_unavailable_error(exc) from None

    def _open_request(self, request: urllib.request.Request):
        open_request = urllib.request.urlopen
        if self.config.topology == "single-host":
            # Proxies and cross-origin redirects must not move local authority off-host.
            open_request = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), _SingleHostRedirectHandler()
            ).open
        return open_request(request, timeout=self.config.timeout_seconds)

    def _json_request(self, method: str, route: str, body: dict | None) -> urllib.request.Request:
        url = f"{self.config.url}{route}"
        headers = {"Accept": "application/json", "User-Agent": _COORDINATOR_USER_AGENT}
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.config.token:
            headers["Authorization"] = f"Bearer {self.config.token}"
        return urllib.request.Request(url, data=data, headers=headers, method=method)

    def _request_json(
        self,
        method: str,
        route: str,
        body: dict | None,
        *,
        contract_route: str,
    ) -> dict[str, Any]:
        request = self._json_request(method, route, body)
        try:
            with self._open_request(request) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("response is not an object")
            return payload
        except urllib.error.HTTPError as exc:
            # HTTPError subclasses URLError, so it has to be caught first for
            # the status to be visible at all.
            if exc.code in _CONTRACT_ABSENT_STATUSES:
                raise _contract_absent_error(
                    self.config.url, contract_route, exc.code
                ) from None
            raise _coordinator_unavailable_error(exc) from None
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            raise _coordinator_unavailable_error(exc) from None


def configured_schema_fence_operator_client(
    env: Mapping[str, str] | None = None,
) -> LeaseCoordinatorClient | None:
    """Return the protected rollout client when writer coordination is configured."""

    values = os.environ if env is None else env
    config = LeaseConfig.from_env(values)
    if not config.enabled:
        return None
    operator_token = values.get(
        "EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN", ""
    ).strip()
    if not operator_token:
        raise OpError(
            "SCHEMA_FENCE_OPERATOR_UNAVAILABLE",
            "schema fence operator credential is unavailable",
            "Set EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN only in the protected "
            "offline rollout environment.",
        )
    if operator_token == config.token:
        raise OpError(
            "SCHEMA_FENCE_OPERATOR_UNAVAILABLE",
            "schema fence operator credential must be distinct from the writer lease credential",
            "Provision a separate operator credential before changing the schema fence.",
        )
    return LeaseCoordinatorClient(replace(config, token=operator_token))


def require_configured_schema_fence(
    schema_version: int,
) -> SchemaFenceState | None:
    """Verify an optional external rollout fence without widening its state."""

    if isinstance(schema_version, bool) or schema_version not in {3, 4}:
        raise ValueError("schema_version must be 3 or 4")
    client = configured_schema_fence_operator_client()
    if client is None:
        return None
    state = client.schema_fence()
    if state.schema_version != schema_version:
        raise OpError(
            "SCHEMA_FENCE_CONFLICT",
            f"external schema fence requires v{state.schema_version}, not v{schema_version}",
            "Reconcile the durable store and schema-fence generation before retrying.",
            details={
                "required_schema_version": state.schema_version,
                "schema_fence_generation": state.generation,
            },
        )
    return state


def advance_configured_schema_fence(
    *,
    source_schema_version: int,
    target_schema_version: int,
) -> SchemaFenceState | None:
    """CAS an optional rollout fence, accepting an exact lost-ack replay."""

    if (
        isinstance(source_schema_version, bool)
        or isinstance(target_schema_version, bool)
        or source_schema_version not in {3, 4}
        or target_schema_version not in {3, 4}
        or source_schema_version == target_schema_version
    ):
        raise ValueError("schema fence transition must be between v3 and v4")
    client = configured_schema_fence_operator_client()
    if client is None:
        return None
    current = client.schema_fence()
    if current.schema_version == target_schema_version:
        return current
    if current.schema_version != source_schema_version:
        raise OpError(
            "SCHEMA_FENCE_CONFLICT",
            "external schema fence does not match the transition predecessor",
            "Reconcile the durable store and schema-fence generation before retrying.",
            details={
                "required_schema_version": current.schema_version,
                "schema_fence_generation": current.generation,
            },
        )
    advanced = client.transition_schema_fence(
        expected_generation=current.generation,
        schema_version=target_schema_version,
    )
    if (
        advanced.schema_version != target_schema_version
        or advanced.generation != current.generation + 1
    ):
        raise OpError(
            "SCHEMA_FENCE_CONFLICT",
            "external schema fence returned an inconsistent transition terminal",
            "Reconcile the durable store and schema-fence generation before retrying.",
        )
    return advanced


def _mutation_outcome_unknown_error() -> OpError:
    return OpError(
        "MUTATION_OUTCOME_UNKNOWN",
        "the executing process terminated before recording this mutation's outcome",
        "Verify whether the mutation landed and reconcile before submitting any new mutation; "
        "retries with this identity will remain fail-closed.",
        details={"status": "uncertain", "committed": None, "abandoned": True},
    )


def _generate_owner_id() -> str:
    """`pid:nonce` — immune to PID reuse because the nonce is random per
    process, never reused across a reboot the way a bare PID can be."""
    return f"{os.getpid()}:{uuid.uuid4().hex[:16]}"


def _owner_lock_path(state_dir: Path, owner: str) -> Path:
    return Path(state_dir) / "idempotency-owners" / f"{owner.replace(':', '-')}.lock"


def _acquire_own_owner_lock(state_dir: Path, owner: str) -> Any | None:
    """Open and lock this process's own owner file, held for the store's
    lifetime (closed only at process exit). A leftover locked-then-abandoned
    file is harmless: `_probe_owner_liveness` either finds it still locked
    (this process, still alive) or, once genuinely gone, lockable — and
    cleans it up itself."""
    path = _owner_lock_path(state_dir, owner)
    handle = None
    try:
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        existed = path.exists()
        if os.name == "nt":
            handle = path.open("a+b")
        else:
            # Create privately without changing configured ancestor-path semantics.
            handle = open(path, "a+b", opener=lambda name, flags: os.open(name, flags, 0o600))
        if not existed and os.name != "nt":
            os.chmod(path, 0o600)
    except OSError:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        return None
    try:
        if _try_owner_lock(handle):
            return handle
    except OSError:
        pass
    handle.close()
    return None


def _probe_owner_liveness(state_dir: Path, owner: str) -> bool:
    """Fail-closed liveness probe for `owner` (a `pid:nonce` string).

    Missing file -> dead (the owner released and cleaned up, or never
    existed). Lockable -> dead, and this probe cleans the file up. Refused
    (someone holds it) -> alive. ANY probe error -> alive: an inconclusive
    probe must never cause a live mutation to be declared abandoned.
    """
    path = _owner_lock_path(state_dir, owner)
    try:
        handle = path.open("r+b")
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        lockable = _try_owner_lock(handle)
    except OSError:
        handle.close()
        return True
    if not lockable:
        handle.close()
        return True
    try:
        _release_owner_lock(handle)
    except OSError:
        pass
    handle.close()
    try:
        path.unlink()
    except OSError:
        pass
    return False


@contextmanager
def _idempotency_initialization_guard(path: Path) -> Iterator[None]:
    """Serialize private runtime preparation and SQLite schema initialization."""
    configured_root = path.parent.expanduser().absolute()
    if os.name == "nt":
        mutation_lock.prepare_windows_private_state_root(configured_root)
        root = configured_root
    else:
        configured_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = configured_root.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise RuntimeError("idempotency runtime state path cannot be upgraded safely")
        # Resolve ancestor aliases without hiding an untrusted leaf symlink.
        root = configured_root.parent.resolve() / configured_root.name
    directory = mutation_lock._acquire_trusted_runtime_root(root)
    try:
        if os.name != "nt":
            assert directory.fd is not None
            os.fchmod(directory.fd, 0o700)

        def namespace_unchanged() -> bool:
            if not mutation_lock._same_directory_path(directory):
                return False
            if os.name == "nt":
                return True
            assert directory.fd is not None
            return os.path.samestat(configured_root.lstat(), os.fstat(directory.fd))

        if not namespace_unchanged():
            raise OSError("idempotency initialization directory changed")
        # This persistent protocol lock covers SQLite creation, privacy checks, and migration together.
        lock = path.with_name(f"{path.name}.init.lock")
        descriptor = mutation_lock._open_owned_runtime_lock_file(directory, lock.name)
        try:
            handle = os.fdopen(descriptor, "a+b")
        except BaseException:
            os.close(descriptor)
            raise
        with handle:
            if os.name == "nt":
                import msvcrt

                mutation_lock._validate_windows_runtime_entry(
                    directory.path / lock.name,
                    directory=False,
                    sid=mutation_lock._windows_current_user_sid(),
                    handle=msvcrt.get_osfhandle(handle.fileno()),
                )
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if not namespace_unchanged() or not mutation_lock._same_file_entry(
                directory, lock.name, handle.fileno()
            ):
                raise OSError("idempotency initialization namespace changed before acquisition")
            locked = mutation_lock._acquire_os_lock_until(
                handle,
                time.monotonic() + _IDEMPOTENCY_SQLITE_TIMEOUT_SECONDS,
                _IDEMPOTENCY_POLL_INTERVAL_SECONDS,
            )
            if not locked:
                raise TimeoutError("idempotency initialization lock timed out")
            try:
                if not namespace_unchanged() or not mutation_lock._same_file_entry(
                    directory, lock.name, handle.fileno()
                ):
                    raise OSError("idempotency initialization namespace changed during acquisition")
                yield
            finally:
                mutation_lock._release_os_lock(handle)
    finally:
        directory.close()


class _ClosingConnection(sqlite3.Connection):
    """A connection whose `with` block commits or rolls back, then closes.

    The stock `with conn:` only ends the transaction and leaves the handle to
    the collector. A leaked handle keeps the WAL sidecars alive until some
    later GC pass closes it, so the state directory changes at an arbitrary
    moment after the store was last used.
    """

    def __exit__(self, exc_type, exc, traceback):  # noqa: ANN001
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


class IdempotencyStore:
    """Durable per-replica retry cache, deliberately outside the synced vault."""

    def __init__(
        self,
        path: Path,
        *,
        clock=time.time,  # noqa: ANN001
        monotonic=time.monotonic,  # noqa: ANN001
        wait_seconds: float = _IDEMPOTENCY_WAIT_SECONDS,
        poll_interval_seconds: float = _IDEMPOTENCY_POLL_INTERVAL_SECONDS,
        after_terminal_persisted=None,  # noqa: ANN001
        secret_protector=None,  # noqa: ANN001
    ):
        if wait_seconds < 0:
            raise ValueError("idempotency wait must be non-negative")
        if poll_interval_seconds <= 0:
            raise ValueError("idempotency poll interval must be positive")
        self.path = path
        self.state_dir = path.parent
        self.clock = clock
        self.monotonic = monotonic
        self.wait_seconds = wait_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.after_terminal_persisted = after_terminal_persisted
        self._runtime_state_error: RuntimeError | None = None
        self._secret_protector = (
            secret_protector
            if secret_protector is not None
            else _WindowsDPAPISecretProtector()
            if os.name == "nt"
            else None
        )
        self._condition = threading.Condition()
        self._attempts: dict[str, _ExecutionAttempt] = {}
        # Replaced at the end of a successful construction. An inert store --
        # one whose runtime root failed validation -- never sweeps, and says so.
        self.start_sweep: dict[str, Any] = {
            "ran": False,
            "examined": 0,
            "resolved": 0,
            "retained": 0,
            "limit_reached": False,
        }
        self.owner_id: str | None = None
        self._owner_lock_handle = None
        if os.name == "nt":
            mutation_lock.prepare_windows_idempotency_runtime_paths(
                path.parent, _owner_lock_path(path.parent, "bootstrap").parent
            )
        try:
            with _idempotency_initialization_guard(path):
                owners_dir = _owner_lock_path(path.parent, "bootstrap").parent
                owners_dir_existed = owners_dir.exists()
                private_paths = (
                    path,
                    path.with_name(f"{path.name}-wal"),
                    path.with_name(f"{path.name}-shm"),
                )
                preexisting_private_paths = {item for item in private_paths if item.exists()}
                if os.name != "nt":
                    owners_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
                    if not owners_dir_existed:
                        os.chmod(owners_dir, 0o700)
                self._harden_legacy_runtime_paths()
                self._validate_existing_runtime_paths()
                # Kept only to recognize rows written by the preceding store-lifetime
                # owner protocol. New executions use an attempt lock below and release
                # it on every exit.
                owner_id = _generate_owner_id()
                self.owner_id = owner_id
                # Held for this store's lifetime (process lifetime, in practice —
                # stores are cached singletons keyed by vault+replica); never
                # explicitly released. See `_probe_owner_liveness`.
                self._owner_lock_handle = _acquire_own_owner_lock(self.state_dir, owner_id)
                if self._owner_lock_handle is None:
                    # Fail closed on the REGISTRATION side too: advertising an owner id
                    # whose lock file could not be created/held would let any peer
                    # probe it as dead and abandon a LIVE mutation. A NULL owner rides
                    # the legacy grace path instead (abandoned only after
                    # _IMPLICIT_RETRY_TTL_SECONDS), which cannot abandon a running
                    # write inside its window.
                    logger.warning(
                        "idempotency owner lock unavailable; falling back to the "
                        "legacy ownerless grace period for this store"
                    )
                    self.owner_id = None
                with self._connect() as conn:
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS mutations ("
                        "key TEXT PRIMARY KEY, digest TEXT NOT NULL, state TEXT NOT NULL, "
                        "result BLOB, updated_at REAL NOT NULL)"
                    )
                    columns = {row[1] for row in conn.execute("PRAGMA table_info(mutations)").fetchall()}
                    if "owner" not in columns:
                        conn.execute("ALTER TABLE mutations ADD COLUMN owner TEXT")
                    if "attempt_id" not in columns:
                        conn.execute("ALTER TABLE mutations ADD COLUMN attempt_id TEXT")
                    if "commit_token" not in columns:
                        conn.execute("ALTER TABLE mutations ADD COLUMN commit_token TEXT")
                    if "commit_secret" not in columns:
                        conn.execute("ALTER TABLE mutations ADD COLUMN commit_secret BLOB")
                if not path.exists():  # pragma: no cover - sqlite always creates the database
                    raise RuntimeError("idempotency runtime database was not created")
                if os.name != "nt":
                    for item in private_paths:
                        if item.exists() and item not in preexisting_private_paths:
                            os.chmod(item, 0o600)
                self._ensure_private_runtime_state()
        except RuntimeError as error:
            # Construction is also used by read/graph readiness paths. Keep
            # the unsafe runtime root inert there; every SQLite/claim use
            # below still fails closed before untrusted state is opened.
            self._runtime_state_error = error
            self.owner_id = None
            if self._owner_lock_handle is not None:
                self._owner_lock_handle.close()
                self._owner_lock_handle = None
            return
        self.start_sweep = self._sweep_dead_non_terminal_rows()

    def _sweep_dead_non_terminal_rows(self) -> dict[str, Any]:
        """Resolve `pending`/`reserved` rows whose owner is provably dead.

        Every other resolver for a non-terminal row needs the identical
        identity to be retried. A client that never retries therefore leaves
        its row behind forever, which is how a live cell reached thirteen
        pending receipts with the oldest fourteen days old.

        `executing` is DELIBERATELY EXCLUDED. Only the retry path can supply
        `commit_evidence`, and only with it can `_abandon_if_dead` promote a
        dead-owner `executing` row to `canonically_committed` and replay its
        terminal. A sweep has no such evidence to offer, so it would resolve
        exactly those rows to `completed` + `_OUTCOME_UNKNOWN_PAYLOAD` --
        destroying recoverable proof of a commit whose process died before
        persisting its terminal, and then telling the caller to resend under a
        new key, duplicating a write that already landed. The cost of leaving
        one is a row that waits for its retry, which is what it did before;
        the cost of reaping one is a duplicated committed mutation.

        What remains cannot carry that evidence. A `reserved` row provably
        predates any leaf, so `_abandon_if_dead` reclaims it by deletion. A
        `pending` row is the legacy/ownerless class, whose branch never
        consults evidence at all. For both states the sweep's outcome is
        therefore identical to an on-demand abandonment's, evidence or not --
        it only stops requiring a retry to trigger it.

        The liveness probe stays fail-closed either way: an owner whose lock is
        held, or whose liveness cannot be determined, counts as ALIVE and its
        row is left exactly as it was.

        Returns content-free counts for `coordination_status`/`doctor`: how
        many rows were examined, resolved and retained, and whether the budget
        was exhausted (so more may remain). Never a key, digest, or result.
        """
        examined = 0
        resolved = 0
        retained = 0
        limit_reached = False
        try:
            now = self.clock()
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    "SELECT key, digest, state, result, updated_at, owner, attempt_id, "
                    "commit_token, commit_secret FROM mutations "
                    "WHERE state IN ('pending', 'reserved') "
                    "ORDER BY updated_at ASC LIMIT ?",
                    (_IDEMPOTENCY_START_SWEEP_LIMIT,),
                ).fetchall()
                limit_reached = len(rows) >= _IDEMPOTENCY_START_SWEEP_LIMIT
                for row in rows:
                    examined += 1
                    disposition = self._abandon_if_dead(conn, row[0], row[1:], now)
                    if disposition is None or disposition[1] not in {
                        "pending",
                        "reserved",
                    }:
                        resolved += 1
                    else:
                        retained += 1
        except Exception:  # noqa: BLE001 - a store that cannot be swept still opens
            logger.warning("idempotency start sweep did not complete", exc_info=True)
            return {
                "ran": False,
                "examined": examined,
                "resolved": resolved,
                "retained": retained,
                "limit_reached": limit_reached,
            }
        if resolved:
            _log_mutation_event(
                "start_sweep",
                level=logging.WARNING,
                examined=examined,
                resolved=resolved,
                retained=retained,
            )
        return {
            "ran": True,
            "examined": examined,
            "resolved": resolved,
            "retained": retained,
            "limit_reached": limit_reached,
        }

    def _ensure_private_runtime_state(self) -> None:
        """Secrets require a local owner-only state directory; never repair one."""
        if os.name == "nt":
            from .mutation_lock import validate_windows_idempotency_runtime_paths

            validate_windows_idempotency_runtime_paths(
                self.state_dir,
                _owner_lock_path(self.state_dir, "bootstrap").parent,
                (
                    self.path,
                    self.path.with_name(f"{self.path.name}-wal"),
                    self.path.with_name(f"{self.path.name}-shm"),
                ),
            )
            return
        mode = stat.S_IMODE(self.state_dir.stat().st_mode)
        if mode & 0o077:
            raise RuntimeError("idempotency runtime state directory is not owner-only")
        owners_dir = _owner_lock_path(self.state_dir, "bootstrap").parent
        if stat.S_IMODE(owners_dir.stat().st_mode) & 0o077:
            raise RuntimeError("idempotency owner directory is not owner-only")
        # The private directory isolates unrelated lock files; claims must not scan them.
        for item in (
            self.path,
            self.path.with_name(f"{self.path.name}-wal"),
            self.path.with_name(f"{self.path.name}-shm"),
        ):
            if item.exists() and stat.S_IMODE(item.stat().st_mode) & 0o077:
                raise RuntimeError("idempotency runtime database is not owner-only")

    def completed_terminal(self, key: str | None, digest: str) -> Mapping[str, Any] | None:
        """Return an exact trusted replay terminal without taking an execution claim."""
        if not key:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state, result FROM mutations WHERE key = ? AND digest = ?", (key, digest)
            ).fetchone()
        if row is None or row[0] != "completed" or not isinstance(row[1], bytes):
            return None
        try:
            terminal = pickle.loads(row[1])  # noqa: S301 - private idempotency cache
        except (AttributeError, EOFError, ImportError, IndexError, pickle.UnpicklingError, TypeError):
            return None
        return terminal if isinstance(terminal, Mapping) else None

    def _validate_existing_runtime_paths(self) -> None:
        """Reject attacker-controlled SQLite/pickle paths before opening them."""
        if os.name == "nt":
            self._ensure_private_runtime_state()
            return
        owners_dir = _owner_lock_path(self.state_dir, "bootstrap").parent
        paths = (
            (self.state_dir, True),
            (owners_dir, True),
            (self.path, False),
            (self.path.with_name(f"{self.path.name}-wal"), False),
            (self.path.with_name(f"{self.path.name}-shm"), False),
        )
        for path, directory in paths:
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or (
                not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode)
            ):
                raise RuntimeError("idempotency runtime state path is not a trusted regular path")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise RuntimeError("idempotency runtime state path is not owner-only")

    def _harden_legacy_runtime_paths(self) -> None:
        """Upgrade prior owner-owned 0755/0644 runtime artifacts before open."""
        if os.name == "nt":
            return
        owners_dir = _owner_lock_path(self.state_dir, "bootstrap").parent
        paths = (
            (self.state_dir, True),
            (owners_dir, True),
            (self.path, False),
            (self.path.with_name(f"{self.path.name}-wal"), False),
            (self.path.with_name(f"{self.path.name}-shm"), False),
        )
        owner_uid = os.geteuid()
        for path, directory in paths:
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if (
                stat.S_ISLNK(info.st_mode)
                or (not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode))
                or info.st_uid != owner_uid
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                raise RuntimeError("idempotency runtime state path cannot be upgraded safely")
            desired_mode = 0o700 if directory else 0o600
            if stat.S_IMODE(info.st_mode) != desired_mode:
                os.chmod(path, desired_mode)
        for owner_lock in owners_dir.glob("*.lock"):
            # Another process releases its owner lock at any time; one that vanished
            # after the listing has nothing left to harden.
            try:
                info = owner_lock.lstat()
            except FileNotFoundError:
                continue
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or info.st_uid != owner_uid
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                raise RuntimeError("idempotency owner lock cannot be upgraded safely")
            if stat.S_IMODE(info.st_mode) != 0o600:
                try:
                    os.chmod(owner_lock, 0o600)
                except FileNotFoundError:
                    continue

    def _connect(self) -> sqlite3.Connection:
        if self._runtime_state_error is not None:
            raise self._runtime_state_error
        if os.name == "nt":
            self._ensure_private_runtime_state()
        private_paths = (
            self.path,
            self.path.with_name(f"{self.path.name}-wal"),
            self.path.with_name(f"{self.path.name}-shm"),
        )
        existed = {item for item in private_paths if item.exists()}
        conn = sqlite3.connect(
            self.path, timeout=_IDEMPOTENCY_SQLITE_TIMEOUT_SECONDS, factory=_ClosingConnection
        )
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            if os.name == "nt":
                self._ensure_private_runtime_state()
        except BaseException:
            conn.close()
            raise
        if os.name != "nt":
            for item in private_paths:
                if item.exists() and item not in existed:
                    os.chmod(item, 0o600)
        return conn

    def _commit_secret_entropy(self, digest: str, attempt: _ExecutionAttempt) -> bytes:
        del digest
        return (
            _WINDOWS_SECRET_ENTROPY_DOMAIN
            + attempt.attempt_id.encode("utf-8")
            + b"\0"
            + attempt.commit_token.encode("utf-8")
        )

    def _stored_commit_secret(self, digest: str, attempt: _ExecutionAttempt) -> bytes:
        protector = self._secret_protector
        if protector is None:
            return attempt.commit_secret
        ciphertext = protector.protect(
            attempt.commit_secret, self._commit_secret_entropy(digest, attempt)
        )
        if (
            not isinstance(ciphertext, bytes)
            or not 1 <= len(ciphertext) <= _WINDOWS_SECRET_ENVELOPE_MAX_CIPHERTEXT
        ):
            raise RuntimeError("idempotency secret protector returned an invalid ciphertext")
        provider = getattr(protector, "provider", None)
        if type(provider) is not int or not 1 <= provider <= 255:
            raise RuntimeError("idempotency secret protector has no valid provider")
        return (
            _WINDOWS_SECRET_ENVELOPE_MAGIC
            + bytes((_WINDOWS_SECRET_ENVELOPE_VERSION, provider))
            + len(ciphertext).to_bytes(4, "big")
            + ciphertext
        )

    def _unprotected_commit_secret(self, digest: str, attempt: _ExecutionAttempt) -> bytes | None:
        stored = attempt.commit_secret
        protector = self._secret_protector
        if protector is None:
            return stored if len(stored) == 32 else None
        if any(active is attempt for active in self._attempts.values()):
            return stored if len(stored) == 32 else None
        header = _WINDOWS_SECRET_ENVELOPE_HEADER_BYTES
        if (
            len(stored) < header
            or len(stored) > _WINDOWS_SECRET_ENVELOPE_MAX_BYTES
            or not stored.startswith(_WINDOWS_SECRET_ENVELOPE_MAGIC)
            or stored[4] != _WINDOWS_SECRET_ENVELOPE_VERSION
            or stored[5] != getattr(protector, "provider", None)
        ):
            return None
        length = int.from_bytes(stored[6:10], "big")
        ciphertext = stored[10:]
        if length != len(ciphertext) or not 1 <= length <= _WINDOWS_SECRET_ENVELOPE_MAX_CIPHERTEXT:
            return None
        try:
            secret = protector.unprotect(ciphertext, self._commit_secret_entropy(digest, attempt))
        except Exception:  # noqa: BLE001 - unreadable local authority fails closed
            return None
        return secret if isinstance(secret, bytes) and len(secret) == 32 else None

    def run(
        self,
        key: str | None,
        digest: str,
        operation,  # noqa: ANN001
        *,
        expires_after: float | None = None,
        on_replay=None,  # noqa: ANN001
        operation_guard=None,  # noqa: ANN001
        commit_observed=None,  # noqa: ANN001
        after_canonical_persisted=None,  # noqa: ANN001
        after_operation_guard=None,  # noqa: ANN001
        after_terminal_acknowledgement=None,  # noqa: ANN001
        acknowledgement_expected=None,  # noqa: ANN001
        resume_canonically_committed=None,  # noqa: ANN001
        commit_evidence=None,  # noqa: ANN001
        legacy_graph_pending_proof=None,  # noqa: ANN001
    ) -> Any:
        if not key:
            with operation_guard() if operation_guard is not None else nullcontext():
                result = operation()
            if after_operation_guard is not None:
                result = after_operation_guard(result)
            if after_terminal_acknowledgement is None:
                return result
            # No replay identity, so no terminal to persist -- but a committed
            # write must not be reported as uncertain because derived work
            # failed, so the same degradation applies.
            try:
                return after_terminal_acknowledgement(result)
            except _PostCommitOutcomeUncertain as acknowledgement_error:
                return _derived_acknowledgement_pending(result, acknowledgement_error)

        while True:
            with call_spans.span("mutation.idempotency_claim"):
                disposition, stored = self._claim_or_inspect(
                    key,
                    digest,
                    expires_after,
                    commit_evidence=commit_evidence,
                    legacy_graph_pending_proof=legacy_graph_pending_proof,
                )
            if disposition == "owner":
                break
            if disposition == "pending":
                waited = self._wait_for_terminal(
                    key,
                    digest,
                    on_replay=on_replay,
                    commit_evidence=commit_evidence,
                    legacy_graph_pending_proof=legacy_graph_pending_proof,
                )
                if waited is _RETRY_IDEMPOTENCY_CLAIM:
                    continue
                disposition, stored = waited
            if disposition == "canonical_resume":
                if resume_canonically_committed is None:
                    raise _mutation_outcome_unknown_error()
                terminal_result = resume_canonically_committed(stored)
                if terminal_result == _OUTCOME_UNKNOWN_TERMINAL:
                    # A v2 receipt can prove the canonical write happened,
                    # yet deliberately retains no cleanup target paths.  Do
                    # not turn that signed committed-failure cut into a
                    # fabricated success: retain the stable fail-closed
                    # terminal for every exact replay.
                    try:
                        self._persist_completed_from_canonical(key, digest, terminal_result)
                    except Exception as storage_error:
                        raise _PostCommitOutcomeUncertain() from storage_error
                    self._notify_waiters()
                    self._after_terminal_persisted()
                    raise _mutation_outcome_unknown_error()
                if isinstance(terminal_result, _CanonicalCommittedFailure):
                    try:
                        self._persist_committed_failure(key, digest, terminal_result.payload)
                    except Exception as storage_error:
                        raise _PostCommitOutcomeUncertain() from storage_error
                    self._notify_waiters()
                    self._after_terminal_persisted()
                    raise _CachedCommittedFailure(terminal_result.payload)
                try:
                    terminal_result = self._persist_completed_from_canonical(
                        key, digest, terminal_result
                    )
                except Exception as storage_error:
                    # The canonical row remains the exact retry anchor. Never
                    # let a SQLite hiccup send an already committed leaf back
                    # through the execution path.
                    raise _PostCommitOutcomeUncertain() from storage_error
                self._notify_waiters()
                self._after_terminal_persisted()
                return terminal_result
            if disposition == "outcome_unknown":
                raise _mutation_outcome_unknown_error()
            return self._replay(disposition, stored, on_replay)

        attempt = self._attempts.get(key)
        if attempt is None:  # pragma: no cover - defensive against an in-process map loss
            raise self._reconciliation_error("executing mutation attempt")
        guard = operation_guard() if operation_guard is not None else nullcontext()
        leaf_started = False
        leaf_returned = False
        canonical_persisted = False
        committed_failure: dict[str, Any] | None = None
        committed_error: Exception | None = None
        committed_handoff = False
        try:
            with guard:
                with active_mutation_claim_context(
                    claim_token=attempt.commit_token,
                    command_digest=digest if re.fullmatch(r"[0-9a-f]{64}", digest) else None,
                ):
                    try:
                        leaf_started = True
                        result = operation()
                    except Exception as operation_error:
                        if isinstance(operation_error, _PostCommitOutcomeUncertain):
                            leaf_returned = True
                            raise
                        committed_failure = _committed_failure_payload(operation_error)
                        if committed_failure is None:
                            raise
                        leaf_returned = True
                        if commit_observed is None or not commit_observed():
                            try:
                                self._persist_committed_failure(key, digest, committed_failure)
                            except Exception as storage_error:
                                raise operation_error from storage_error
                            self._notify_waiters()
                            self._after_terminal_persisted()
                            raise
                        committed_error = operation_error
                        committed_handoff = True
                        # The outer signed receipt disposition, rather than
                        # any portable terminal field, records this cut.
                        result = {"status": "committed", "mutated": True}
                    leaf_returned = True
                    if after_canonical_persisted is not None:
                        prepared = _call_after_canonical(
                            after_canonical_persisted,
                            result,
                            attempt,
                            "committed_failure" if committed_handoff else "success",
                        )
                        result = prepared
                    canonical_result = result
                    if committed_handoff:
                        assert committed_failure is not None
                        canonical_result = _CanonicalCommittedFailure(result, committed_failure)
                    try:
                        self._persist_canonically_committed(
                            key,
                            digest,
                            canonical_result,
                            attempt,
                        )
                    except Exception as storage_error:
                        if self._exact_evidence(commit_evidence, digest, attempt):
                            raise _PostCommitOutcomeUncertain() from storage_error
                        raise _PostCommitOutcomeUncertain() from storage_error
                    canonical_persisted = True
                    self._notify_waiters()
            # The attempt lock is released only after the canonical handoff;
            # derived graph work is deliberately not owned by it.
            self._release_attempt(key, attempt)
            # Work whose terminal *is* the derived state -- a graph-rebuild
            # handoff -- stays here, before persistence: a `completed` row must
            # never hold unfinished operation-completion work, or a crash in
            # the window replays a terminal that says the rebuild is cleared
            # while the graph is still quarantined. The composition site binds
            # a command's guard to this hook or to the post-persistence one.
            if after_operation_guard is None:
                terminal_result = result
            else:
                terminal_result = after_operation_guard(result)
            if committed_handoff:
                # A committed *failure* has no success terminal to protect, so
                # its derived work still runs before the failure terminal
                # replaces the canonical row.
                if after_terminal_acknowledgement is not None:
                    after_terminal_acknowledgement(terminal_result)
                assert committed_failure is not None
                try:
                    self._persist_committed_failure(key, digest, committed_failure)
                except Exception as storage_error:
                    assert committed_error is not None
                    raise committed_error from storage_error
                self._notify_waiters()
                self._after_terminal_persisted()
                assert committed_error is not None
                raise committed_error
            # A terminal persisted before its acknowledgement has not proven
            # any derived state, and the contract says such a terminal reads
            # `pending`. Writing that first means the crash window and a failed
            # `_advance_completed_terminal` both leave a row that says pending
            # -- honest, and self-correcting through reconcile -- instead of a
            # row that says nothing about derived state at all.
            #
            # Read after the leaf returned, like `commit_observed`: whether any
            # derived work is owed is only known once the leaf has had its
            # chance to register a batch. A commit that registers none owes
            # nothing and must not be stamped pending forever.
            if acknowledgement_expected is not None and acknowledgement_expected():
                terminal_result = _awaiting_derived_acknowledgement(terminal_result)
            try:
                terminal_result = self._persist_completed_from_canonical(
                    key, digest, terminal_result
                )
            except Exception as storage_error:
                raise _PostCommitOutcomeUncertain() from storage_error
            self._notify_waiters()
            self._after_terminal_persisted()
            if after_terminal_acknowledgement is None:
                return terminal_result
            # The canonical terminal is durable BEFORE the derived-state
            # acknowledgement runs. The ledger's `preserve_artifacts` rows are
            # the other order: commit proven, acknowledgement lost, no terminal
            # for the identity, and the client's same-identity retry resolved
            # to the fail-closed outcome-unknown. Committed-uncertain is now
            # reached only when persisting the terminal itself fails.
            try:
                acknowledged = after_terminal_acknowledgement(terminal_result)
            except _PostCommitOutcomeUncertain as acknowledgement_error:
                acknowledged = _derived_acknowledgement_pending(
                    terminal_result, acknowledgement_error
                )
            self._advance_completed_terminal(key, digest, acknowledged)
            return acknowledged
        except BaseException as error:
            # Ordinary, explicitly uncommitted leaf failures are safe to retry.
            # Abrupt exits and anything after the canonical commit point are
            # intentionally retained for exact evidence/outcome-unknown
            # classification instead.
            if (
                not leaf_returned
                and isinstance(error, Exception)
                and (commit_observed is None or not commit_observed())
            ):
                self._delete_executing(key, digest, attempt)
            elif not leaf_started:
                self._delete_executing(key, digest, attempt)
            elif not canonical_persisted:
                # A returned leaf without a durable exact receipt is the
                # unavoidable multi-store cut. Its released attempt makes the
                # next exact retry classify outcome_unknown, never replay it.
                self._notify_waiters()
            raise
        finally:
            self._release_attempt(key, attempt)

    def _claim_or_inspect(
        self,
        key: str,
        digest: str,
        expires_after: float | None,
        *,
        commit_evidence=None,
        legacy_graph_pending_proof=None,
    ) -> tuple[str, Any]:
        now = self.clock()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._prune_expired(conn, now, expires_after, key)
            row = conn.execute(
                "SELECT digest, state, result, updated_at, owner, attempt_id, commit_token, commit_secret "
                "FROM mutations WHERE key = ?",
                (key,),
            ).fetchone()
            if row and self._expired_row(row, now, expires_after):
                conn.execute("DELETE FROM mutations WHERE key = ?", (key,))
                row = None
            if row is not None:
                row = self._abandon_if_dead(
                    conn,
                    key,
                    row,
                    now,
                    commit_evidence=commit_evidence,
                    legacy_graph_pending_proof=legacy_graph_pending_proof,
                )
                if row is not None:
                    return self._decode_disposition(row, digest, commit_evidence=commit_evidence)
            self._ensure_private_runtime_state()
            attempt = _new_attempt()
            try:
                stored_commit_secret = self._stored_commit_secret(digest, attempt)
            except Exception as error:
                raise OpError(
                    "IDEMPOTENCY_SECRET_PROTECTION_UNAVAILABLE",
                    "could not protect the local idempotency receipt secret",
                    "Resolve local Windows credential protection before retrying this mutation.",
                ) from error
            attempt.handle = _acquire_own_owner_lock(self.state_dir, attempt.attempt_id)
            if attempt.handle is None:
                raise OpError(
                    "IDEMPOTENCY_OWNER_UNAVAILABLE",
                    "could not establish a bounded idempotency execution claim",
                    "Retry the same mutation identity after local coordination recovers.",
                )
            try:
                self._ensure_private_runtime_state()
                conn.execute(
                    "INSERT INTO mutations(key, digest, state, updated_at, owner, attempt_id, commit_token, commit_secret) "
                    "VALUES (?, ?, 'reserved', ?, ?, ?, ?, ?)",
                    (
                        key,
                        digest,
                        now,
                        attempt.attempt_id,
                        attempt.attempt_id,
                        attempt.commit_token,
                        stored_commit_secret,
                    ),
                )
                cursor = conn.execute(
                    "UPDATE mutations SET state = 'executing' WHERE key = ? AND state = 'reserved' "
                    "AND attempt_id = ? AND commit_token = ?",
                    (key, attempt.attempt_id, attempt.commit_token),
                )
                if cursor.rowcount != 1:
                    raise self._reconciliation_error("reserved mutation claim")
            except BaseException:
                _release_owner_lock(attempt.handle)
                attempt.handle.close()
                raise
            self._attempts[key] = attempt
        _log_mutation_event("executing", receipt=_receipt_tag(key))
        return "owner", None

    def _abandon_if_dead(
        self,
        conn: sqlite3.Connection,
        key: str,
        row: tuple[Any, ...],
        now: float,
        *,
        commit_evidence=None,  # noqa: ANN001
        legacy_graph_pending_proof=None,  # noqa: ANN001
    ) -> tuple[Any, ...] | None:
        """Resolve proven-dead idempotency ownership without replaying a leaf.
        Fail-closed: an inconclusive liveness probe leaves the row `pending`.
        """
        if row[1] == "graph_pending":
            # Retired graph-pending rows predate the authenticated commit
            # receipt. Migrate only an exact checkpoint binding that still
            # names the current coherent durable epoch; every other legacy cut
            # remains outcome-unknown and can never replay its leaf.
            try:
                legacy_result = pickle.loads(row[2])  # noqa: S301 - local runtime state
                checkpoint_payload = (
                    legacy_result.get("_graph_sync_checkpoint")
                    if type(legacy_result) is dict
                    else None
                )
                if type(checkpoint_payload) is not dict:
                    raise ValueError("legacy graph checkpoint binding is absent")
                from . import graph_sync

                checkpoint = graph_sync.GraphSyncCheckpoint.parse(
                    json.dumps(checkpoint_payload, sort_keys=True, separators=(",", ":")).encode(
                        "utf-8"
                    )
                )
                if checkpoint is None or legacy_graph_pending_proof is None:
                    raise ValueError("legacy graph checkpoint binding is invalid")
                if legacy_graph_pending_proof(checkpoint) is not True:
                    raise ValueError("legacy graph checkpoint is not durable")
            except Exception:  # noqa: BLE001 - legacy state is fail-closed
                try:
                    cursor = conn.execute(
                        "UPDATE mutations SET state = 'completed', result = ?, owner = NULL, updated_at = ? "
                        "WHERE key = ? AND state = 'graph_pending'",
                        (_OUTCOME_UNKNOWN_PAYLOAD, now, key),
                    )
                except sqlite3.Error:
                    return (
                        row[0],
                        "completed",
                        _OUTCOME_UNKNOWN_PAYLOAD,
                        now,
                        None,
                        row[5],
                        row[6],
                        row[7],
                    )
                if cursor.rowcount == 1:
                    self._notify_waiters()
                    return (
                        row[0],
                        "completed",
                        _OUTCOME_UNKNOWN_PAYLOAD,
                        now,
                        None,
                        row[5],
                        row[6],
                        row[7],
                    )
                refreshed = conn.execute(
                    "SELECT digest, state, result, updated_at, owner, attempt_id, commit_token, commit_secret "
                    "FROM mutations WHERE key = ?",
                    (key,),
                ).fetchone()
                return refreshed if refreshed is not None else row
            cursor = conn.execute(
                "UPDATE mutations SET state = 'canonically_committed', owner = NULL, "
                "updated_at = ? WHERE key = ? AND state = 'graph_pending'",
                (now, key),
            )
            if cursor.rowcount == 1:
                return (row[0], "canonically_committed", row[2], now, None, row[5], row[6], row[7])
            return row
        if row[1] not in {"pending", "reserved", "executing"}:
            return row
        owner = row[4]
        if owner is None:
            dead = (now - row[3]) >= _IDEMPOTENCY_LEGACY_OWNER_GRACE_SECONDS
        elif any(
            active.attempt_id == owner and active.handle is not None
            for active in self._attempts.values()
        ):
            dead = False
        elif owner == self.owner_id and row[5] is None:
            # A row from the retired store-lifetime owner scheme cannot name
            # an active execution in this process. Treat it as an unprovable
            # legacy cut instead of letting this idle store object hold it
            # forever merely because its compatibility lock remains open.
            dead = True
        else:
            dead = not _probe_owner_liveness(self.state_dir, owner)
        if not dead:
            return row
        if row[1] == "reserved":
            # No leaf can have started before the executing transition. A
            # proven-dead reservation is therefore safe to reclaim with a
            # fresh attempt secret and must not consult vault evidence.
            cursor = conn.execute(
                "DELETE FROM mutations WHERE key = ? AND state = 'reserved'",
                (key,),
            )
            return None if cursor.rowcount == 1 else row
        attempt = _ExecutionAttempt(
            attempt_id=row[5] if isinstance(row[5], str) else owner or "",
            commit_token=row[6] if isinstance(row[6], str) else "",
            commit_secret=row[7] if isinstance(row[7], bytes) else b"",
            handle=None,
        )
        if row[1] == "executing" and self._exact_evidence(commit_evidence, row[0], attempt):
            cursor = conn.execute(
                "UPDATE mutations SET state = 'canonically_committed', owner = NULL, updated_at = ? "
                "WHERE key = ? AND state IN ('pending', 'reserved', 'executing')",
                (now, key),
            )
            if cursor.rowcount == 1:
                self._notify_waiters()
                return (row[0], "canonically_committed", row[2], now, None, row[5], row[6], row[7])
        else:
            try:
                cursor = conn.execute(
                    "UPDATE mutations SET state = 'completed', result = ?, owner = NULL, updated_at = ? "
                    "WHERE key = ? AND state IN ('pending', 'reserved', 'executing')",
                    (_OUTCOME_UNKNOWN_PAYLOAD, now, key),
                )
            except sqlite3.Error:
                # A broken retry store cannot authorize a replay. Classify this
                # observation fail-closed even when its durable marker cannot advance.
                return (
                    row[0],
                    "completed",
                    _OUTCOME_UNKNOWN_PAYLOAD,
                    now,
                    None,
                    row[5],
                    row[6],
                    row[7],
                )
        if cursor.rowcount != 1:
            # Raced with another abandon/terminal transition; re-read rather
            # than assume which one won.
            refreshed = conn.execute(
                "SELECT digest, state, result, updated_at, owner, attempt_id, commit_token, commit_secret "
                "FROM mutations WHERE key = ?",
                (key,),
            ).fetchone()
            return refreshed if refreshed is not None else row
        _log_mutation_event("abandoned", level=logging.WARNING, receipt=_receipt_tag(key))
        self._notify_waiters()
        return (row[0], "completed", _OUTCOME_UNKNOWN_PAYLOAD, now, None, row[5], row[6], row[7])

    def _prune_expired(
        self,
        conn: sqlite3.Connection,
        now: float,
        expires_after: float | None,
        key: str,
    ) -> None:
        key_pattern = f"{key.partition(':')[0]}:%"
        if expires_after is not None:
            cutoff = now - expires_after
            expired_completed = conn.execute(
                "SELECT key, result FROM mutations WHERE key LIKE ? "
                "AND state = 'completed' "
                "AND typeof(updated_at) IN ('integer', 'real') "
                "AND updated_at >= 0 AND updated_at <= ?",
                (key_pattern, cutoff),
            ).fetchall()
            for expired_key, expired_payload in expired_completed:
                try:
                    completed = pickle.loads(expired_payload)  # noqa: S301 - trusted runtime state
                except Exception:  # noqa: BLE001 - probe the alternate fail-closed encoding
                    try:
                        _deserialize_committed_failure_payload(expired_payload)
                    except Exception:  # noqa: BLE001 - corrupt terminals remain fail-closed
                        continue
                else:
                    if completed == _OUTCOME_UNKNOWN_TERMINAL:
                        continue
                conn.execute(
                    "DELETE FROM mutations WHERE key = ? AND state = 'completed'", (expired_key,)
                )
            expired_failures = conn.execute(
                "SELECT key, result FROM mutations WHERE key LIKE ? "
                "AND state = 'committed_failure' "
                "AND typeof(updated_at) IN ('integer', 'real') "
                "AND updated_at >= 0 AND updated_at <= ?",
                (key_pattern, cutoff),
            ).fetchall()
            for expired_key, expired_payload in expired_failures:
                try:
                    _deserialize_committed_failure_payload(expired_payload)
                except Exception:  # noqa: BLE001 - corrupt markers remain fail-closed
                    continue
                conn.execute(
                    "DELETE FROM mutations WHERE key = ? AND state = 'committed_failure'",
                    (expired_key,),
                )
        # `outcome_unknown` deliberately remains retained: expiry would turn
        # an unprovable canonical cut into permission to replay the leaf.

    def _expired_row(self, row: tuple[Any, ...], now: float, expires_after: float | None) -> bool:
        if expires_after is None or row[1] not in {"completed", "committed_failure"}:
            return False
        updated_at = row[3]
        if row[1] == "completed":
            try:
                completed = pickle.loads(row[2])  # noqa: S301 - trusted runtime state
            except Exception:  # noqa: BLE001 - probe the alternate fail-closed encoding
                try:
                    _deserialize_committed_failure_payload(row[2])
                except Exception:  # noqa: BLE001 - corrupt terminals remain fail-closed
                    raise self._reconciliation_error("cached completed mutation state") from None
            else:
                if completed == _OUTCOME_UNKNOWN_TERMINAL:
                    return False
        elif row[1] == "committed_failure":
            try:
                _deserialize_committed_failure_payload(row[2])
            except Exception:  # noqa: BLE001 - corrupt state blocks mutation
                raise self._reconciliation_error("cached committed mutation state") from None
        if type(updated_at) not in {int, float} or not math.isfinite(updated_at) or updated_at < 0:
            raise self._reconciliation_error("cached mutation state")
        return updated_at <= now - expires_after

    def _decode_disposition(
        self,
        row: tuple[Any, ...],
        digest: str,
        *,
        commit_evidence=None,  # noqa: ANN001
    ) -> tuple[str, Any]:
        if row[0] != digest:
            raise OpError(
                "IDEMPOTENCY_KEY_REUSED",
                "idempotency key was already used for different input",
            )
        state = row[1]
        if state == "completed":
            try:
                completed = pickle.loads(row[2])  # noqa: S301 - trusted runtime state
            except Exception:  # noqa: BLE001 - probe the alternate fail-closed encoding
                try:
                    failure = _CachedCommittedFailure(
                        _deserialize_committed_failure_payload(row[2])
                    )
                except Exception:  # noqa: BLE001 - corrupt state blocks mutation
                    raise self._reconciliation_error("cached completed mutation state") from None
                return "committed_failure", failure
            if completed == _OUTCOME_UNKNOWN_TERMINAL:
                return "outcome_unknown", None
            return "completed", completed
        if state == "committed_failure":
            try:
                failure = _CachedCommittedFailure(_deserialize_committed_failure_payload(row[2]))
            except Exception:  # noqa: BLE001 - corrupt state blocks mutation
                raise self._reconciliation_error("cached committed mutation state") from None
            return "committed_failure", failure
        if state == "committed_uncertain":
            if row[2] is not None:
                raise self._reconciliation_error("cached committed mutation state")
            return "committed_uncertain", _PostCommitOutcomeUncertain()
        if state == "graph_pending":
            # A migration race must never turn an unvalidated legacy row into
            # permission to deserialize and resume derived work.
            return "outcome_unknown", None
        if state == "canonically_committed":
            if row[2] is not None:
                try:
                    return "canonical_resume", pickle.loads(row[2])  # noqa: S301
                except Exception:  # noqa: BLE001 - corrupt runtime state is not a terminal
                    raise self._reconciliation_error("cached canonical mutation state") from None
            attempt = _ExecutionAttempt(
                attempt_id=row[5] if isinstance(row[5], str) else "",
                commit_token=row[6] if isinstance(row[6], str) else "",
                commit_secret=row[7] if isinstance(row[7], bytes) else b"",
                handle=None,
            )
            return "canonical_resume", _CanonicalResume(
                None, self._read_exact_evidence(commit_evidence, row[0], attempt)
            )
        if state in {"pending", "reserved", "executing"}:
            return "pending", None
        if state in {"abandoned", "outcome_unknown"}:
            return "outcome_unknown", None
        raise self._reconciliation_error("cached mutation state")

    def _wait_for_terminal(
        self,
        key: str,
        digest: str,
        *,
        on_replay=None,  # noqa: ANN001
        commit_evidence=None,  # noqa: ANN001
        legacy_graph_pending_proof=None,  # noqa: ANN001
    ) -> Any:
        _log_mutation_event("pending", receipt=_receipt_tag(key))
        deadline = self.monotonic() + self.wait_seconds
        while True:
            now = self.clock()
            del now
            disposition, stored = self._claim_or_inspect(
                key,
                digest,
                None,
                commit_evidence=commit_evidence,
                legacy_graph_pending_proof=legacy_graph_pending_proof,
            )
            if disposition == "owner":
                return _RETRY_IDEMPOTENCY_CLAIM
            if disposition == "outcome_unknown":
                return disposition, stored
            if disposition != "pending":
                return disposition, stored
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise OpError(
                    "MUTATION_ACKNOWLEDGEMENT_PENDING",
                    "an identical mutation is still executing; its commit outcome is not yet known",
                )
            with self._condition:
                self._condition.wait(timeout=min(self.poll_interval_seconds, remaining))

    def _replay(self, disposition: str, stored: Any, on_replay) -> Any:  # noqa: ANN001
        if on_replay is not None:
            on_replay()
        if disposition == "completed":
            if isinstance(stored, Mapping):
                leaf = stored.get("leaf_result")
                if (
                    isinstance(leaf, Mapping)
                    and leaf.get("receipt_version") == 2
                    and valid_collection_receipt(leaf)
                    and leaf.get("outcome") == "committed"
                ):
                    stored_request_id = stored.get("request_id")
                    return replayed_terminal(
                        leaf,
                        request_id=(
                            stored_request_id
                            if isinstance(stored_request_id, str)
                            else str(uuid.uuid4())
                        ),
                        receipt_id=stored.get("receipt_id")
                        if isinstance(stored.get("receipt_id"), str)
                        else None,
                        idempotency_key=stored.get("idempotency_key")
                        if isinstance(stored.get("idempotency_key"), str)
                        else None,
                    )
            return stored
        if disposition == "committed_failure":
            raise stored
        if disposition == "committed_uncertain":
            raise stored
        raise self._reconciliation_error("cached mutation state")

    def _persist_completed(self, key: str, digest: str, result: Any) -> None:
        payload = pickle.dumps(result)
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE mutations SET state = 'completed', result = ?, updated_at = ? "
                "WHERE key = ? AND digest = ? AND state IN ('pending', 'executing', 'canonically_committed')",
                (payload, self.clock(), key, digest),
            )
            if cursor.rowcount != 1:
                raise self._reconciliation_error("completed mutation state")
        _log_mutation_event("terminal", receipt=_receipt_tag(key))

    def _persist_canonically_committed(
        self, key: str, digest: str, result: Any, attempt: _ExecutionAttempt
    ) -> None:
        # Closes the span opened when canonical bytes landed. Everything between
        # the two is derived fan-out and terminal bookkeeping, and the 0.83.1
        # deploy could not attribute ~32 s of it per write.
        call_spans.record_span_since(
            "derived.canonical_to_committed", "canonical_files_committed"
        )
        # The other half of that umbrella. `derived.fanout` is stamped in
        # `vault` when the derived work returns; what is left is the terminal
        # bookkeeping between the leaf returning and this row transitioning --
        # the `after_canonical_persisted` hooks. Both spans close before the
        # UPDATE below, so neither includes this row's own write. Measured at
        # 0.4 s per write on 0.84.1, against 46 s inside the umbrella that had
        # no span at all, so the split is what says which half to look in.
        call_spans.record_span_since("derived.terminal_persist", "derived_fanout_complete")
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE mutations SET state = 'canonically_committed', result = ?, owner = NULL, "
                "updated_at = ? WHERE key = ? AND digest = ? AND state = 'executing' "
                "AND attempt_id = ? AND commit_token = ?",
                (
                    pickle.dumps(result),
                    self.clock(),
                    key,
                    digest,
                    attempt.attempt_id,
                    attempt.commit_token,
                ),
            )
            if cursor.rowcount != 1:
                raise self._reconciliation_error("canonical mutation state")
        _log_mutation_event("canonically_committed", receipt=_receipt_tag(key))

    def _persist_completed_from_canonical(self, key: str, digest: str, result: Any) -> Any:
        payload = pickle.dumps(result)
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE mutations SET state = 'completed', result = ?, updated_at = ? "
                "WHERE key = ? AND digest = ? AND state = 'canonically_committed'",
                (payload, self.clock(), key, digest),
            )
            if cursor.rowcount != 1:
                row = conn.execute(
                    "SELECT result FROM mutations WHERE key = ? AND digest = ? AND state = 'completed'",
                    (key, digest),
                ).fetchone()
                if row is None:
                    raise self._reconciliation_error("canonical mutation completion")
                return pickle.loads(row[0])  # noqa: S301 - trusted runtime state
        _log_mutation_event("terminal", receipt=_receipt_tag(key))
        return result

    def _advance_completed_terminal(self, key: str, digest: str, result: Any) -> None:
        """Refresh the persisted terminal with what the acknowledgement learned.

        Best effort by construction. The terminal is already durable and already
        correct; failing to advance it costs a reader one stale `derived_sync`
        field, while raising here would put back the exact failure this ordering
        removes -- a committed write reported as uncertain because a *derived*
        step did not land.
        """
        try:
            self._replace_completed(key, digest, result)
        except Exception as error:  # noqa: BLE001 - the terminal is already durable
            logger.warning(
                "committed terminal kept its pre-acknowledgement projection (%s)",
                type(error).__name__,
            )

    def _replace_completed(self, key: str, digest: str, result: Any) -> None:
        """Advance a durably committed canonical terminal with derived progress."""
        payload = pickle.dumps(result)
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE mutations SET result = ?, updated_at = ? "
                "WHERE key = ? AND digest = ? AND state = 'completed'",
                (payload, self.clock(), key, digest),
            )
            if cursor.rowcount != 1:
                raise self._reconciliation_error("completed mutation state")
        _log_mutation_event("terminal", receipt=_receipt_tag(key))

    def _persist_committed_failure(
        self, key: str, digest: str, committed_failure: dict[str, Any]
    ) -> None:
        payload = _serialize_committed_failure_payload(committed_failure)
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE mutations SET state = 'completed', result = ?, "
                "updated_at = ? WHERE key = ? AND digest = ? "
                "AND state IN ('executing', 'canonically_committed')",
                (payload, self.clock(), key, digest),
            )
            if cursor.rowcount != 1:
                raise sqlite3.OperationalError(
                    "pending idempotency marker changed before committed failure update"
                )
        _log_mutation_event("terminal", receipt=_receipt_tag(key))

    def _delete_pending(self, key: str, digest: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM mutations WHERE key = ? AND digest = ? "
                "AND state IN ('pending', 'reserved', 'executing')",
                (key, digest),
            )
        self._notify_waiters()

    def _delete_executing(self, key: str, digest: str, attempt: _ExecutionAttempt) -> None:
        del attempt
        self._delete_pending(key, digest)

    def _release_attempt(self, key: str, attempt: _ExecutionAttempt) -> None:
        current = self._attempts.get(key)
        if current is attempt:
            self._attempts.pop(key, None)
        handle = attempt.handle
        attempt.handle = None
        if handle is None:
            return
        try:
            _release_owner_lock(handle)
        except OSError:
            pass
        try:
            handle.close()
        except OSError:
            pass
        try:
            _owner_lock_path(self.state_dir, attempt.attempt_id).unlink()
        except OSError:
            pass

    def _read_exact_evidence(
        self, commit_evidence: Any, digest: str, attempt: _ExecutionAttempt
    ) -> Any:
        if commit_evidence is None or not attempt.attempt_id or not attempt.commit_token:
            return None
        self._ensure_private_runtime_state()
        secret = self._unprotected_commit_secret(digest, attempt)
        if secret is None:
            return None
        try:
            return commit_evidence(digest, attempt.attempt_id, attempt.commit_token, secret)
        except TypeError:
            try:
                # Generic IdempotencyStore users may still provide their own
                # local evidence hook while they migrate. LeaseManager never
                # uses this compatibility route for portable receipts.
                return commit_evidence(digest, attempt.attempt_id, attempt.commit_token)
            except TypeError:
                return commit_evidence()

    def _exact_evidence(
        self, commit_evidence: Any, digest: str, attempt: _ExecutionAttempt
    ) -> bool:
        return bool(self._read_exact_evidence(commit_evidence, digest, attempt))

    def _notify_waiters(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def validate_runtime_state(self) -> None:
        """Fail closed before diagnostics summarize private receipt state."""
        if self._runtime_state_error is not None:
            raise self._runtime_state_error
        self._ensure_private_runtime_state()

    def _after_terminal_persisted(self) -> None:
        if self.after_terminal_persisted is not None:
            self.after_terminal_persisted()

    def status_summary(self) -> dict[str, Any]:
        """Content-free counts for `coordination_status`/`doctor`: never a
        key, digest, or result — just how many rows are pending/abandoned
        and how stale the oldest pending row is. Call `validate_runtime_state`
        first when an unsafe store must be surfaced rather than softened."""
        try:
            now = self.clock()
            with self._connect() as conn:
                pending_updated_at = [
                    row[0]
                    for row in conn.execute(
                        "SELECT updated_at FROM mutations "
                        "WHERE state IN ('pending', 'reserved', 'executing')"
                    ).fetchall()
                ]
                abandoned = conn.execute(
                    "SELECT COUNT(*) FROM mutations "
                    "WHERE state IN ('abandoned', 'outcome_unknown') "
                    "OR (state = 'completed' AND result = ?)",
                    (_OUTCOME_UNKNOWN_PAYLOAD,),
                ).fetchone()[0]
            oldest_pending_age_seconds = (
                round(max(0.0, now - min(pending_updated_at)), 3) if pending_updated_at else None
            )
            return {
                "pending": len(pending_updated_at),
                "abandoned": int(abandoned),
                "oldest_pending_age_seconds": oldest_pending_age_seconds,
                # What this process start already reaped, so a stale oldest-age
                # can be read against it rather than mistaken for stalled work.
                "start_sweep": dict(self.start_sweep),
            }
        except Exception:  # noqa: BLE001 - status must never break the caller
            return {
                "pending": None,
                "abandoned": None,
                "oldest_pending_age_seconds": None,
                "start_sweep": dict(self.start_sweep),
            }

    @staticmethod
    def _reconciliation_error(subject: str) -> OpError:
        return OpError(
            "IDEMPOTENCY_IN_PROGRESS",
            f"{subject} requires reconciliation",
            "Reconcile the local idempotency store before retrying this mutation.",
        )


def _namespaced_idempotency_key(kind: str, identity: str, public_key: str) -> str:
    digest = hashlib.sha256(f"{identity}\0{public_key}".encode()).hexdigest()
    return f"{kind}:{digest}"


def _receipt_tag(key: str) -> str:
    """Return a short privacy-safe correlation tag for an internal receipt key."""
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _command_digest(command: Any, kwargs: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            {"command": command.name, "kwargs": kwargs},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _canonicalize_command_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    if kwargs.get("relation_disposition") == "reviewed-none":
        return {**kwargs, "relation_disposition": "reviewed_none"}
    return kwargs


_PUBLIC_IDEMPOTENCY_KEY_UNSET = object()


def _effective_idempotency_key(
    manager: LeaseManager,
    *,
    command: Any,
    mutation_subject: os.PathLike[str] | str,
    digest: str,
    idempotency_key: str | None,
    principal_scope: str | None,
    implicit_idempotency_scope: str | None = None,
) -> tuple[str | None, float | None, Any]:
    from .governance.principal import effective_principal

    identity = canonical_mutation_identity(mutation_subject)
    namespace = f"cell:{manager.config.vault_id}" if manager.config.vault_id else identity
    binding = effective_principal().client_binding
    if binding is not None:
        # Verified clients cannot share owner terminals. This versioned namespace
        # leaves old terminals unadopted; reauthentication keeps successful retries.
        client = hashlib.sha256(json.dumps(
            [binding.issuer, binding.client_id], separators=(",", ":"),
        ).encode()).hexdigest()
        namespace = f"{namespace}\0client:v1:{client}"
    if idempotency_key:
        explicit_namespace = (
            f"{namespace}\0principal:{principal_scope}" if principal_scope else namespace
        )
        return (
            _namespaced_idempotency_key("explicit", explicit_namespace, idempotency_key),
            _EXPLICIT_RETRY_TTL_SECONDS,
            None,
        )
    if implicit_idempotency_scope:
        key = _namespaced_idempotency_key(
            "implicit",
            namespace,
            f"{implicit_idempotency_scope}\0{digest}",
        )

        def log_replay() -> None:
            _log_mutation_event(
                "replayed",
                command=command.name,
                receipt=_receipt_tag(key),
            )
            try:
                from . import metrics

                metrics.inc_counter("exomem_idempotency_replays_total", {})
            except Exception:  # noqa: BLE001 - observability must never break a replay
                pass

        return key, _IMPLICIT_RETRY_TTL_SECONDS, log_replay
    return None, None, None


def _is_receipt_vault_root(root: Path) -> bool:
    """Receipt storage is allowed only inside an existing vault scaffold."""
    from .kbdir import kb_dirname

    return (root / kb_dirname()).is_dir()


# Every keyed write: a busy boundary is the server's problem to wait out, not
# the client's to retry. A client cannot act on ordinary contention; the
# contention_absorbed and contention_refused events keep it visible to operators.
_CAPTURE_WAIT_SECONDS = 40.0
_CAPTURE_WAIT_CEILING_SECONDS = 600.0
_CAPTURE_RETRY_FLOOR_SECONDS = 0.05
_CAPTURE_WAITERS_FULL_REMEDIATION = (
    "The write did not commit. Other writes are already waiting on the boundary and "
    "the server keeps workers free for reads: retry with the same idempotency key "
    "after retry_after_ms."
)
_CAPTURE_HOLDER_OVERDUE_REMEDIATION = (
    "The write did not commit. The mutation boundary's holder has run past its "
    "allowance and looks stuck: inspect coordination_status or cell health, then "
    "retry with the same idempotency key once it clears."
)
_CAPTURE_WAIT_EXHAUSTED_REMEDIATION = (
    "The write did not commit. The boundary stayed busy for the whole bounded wait: "
    "retry with the same idempotency key after retry_after_ms."
)


def _writes_absorb_contention() -> bool:
    return os.environ.get("EXOMEM_CAPTURE_CONTENTION_ABSORB", "1").strip().lower() not in {
        "0",
        "false",
    }


# A waiting write parks one of the process's sync workers (anyio's limiter,
# shared with search and activation). Only half may be parked, so a long
# holder cannot turn every worker into a sleeping write and starve reads.
# The `capture` in these names, the env knobs and the refusal causes is
# historical: they now cover every keyed write.
_CAPTURE_WAITERS_LOCK = threading.Lock()
_capture_waiters = 0


def _capture_waiter_cap() -> int:
    from . import runtime_resources

    return max(1, runtime_resources.resolve_policy().sync_workers // 2)


def _take_capture_waiter_slot() -> bool:
    global _capture_waiters
    with _CAPTURE_WAITERS_LOCK:
        if _capture_waiters >= _capture_waiter_cap():
            return False
        _capture_waiters += 1
        return True


def _release_capture_waiter_slot() -> None:
    global _capture_waiters
    with _CAPTURE_WAITERS_LOCK:
        _capture_waiters = max(0, _capture_waiters - 1)


def _capture_wait_seconds(guard_seconds: float = 0.0) -> float:
    """The bounded server-side wait, capped by the caller's own deadline.

    An MCP call that would outlive its request budget gets the typed refusal
    while the client is still listening, never a wait that ends in a timeout.
    The last attempt may start at the returned bound and still block for one
    guard timeout, so the bound leaves room for the delivery reserve plus that.
    """
    try:
        wait = float(os.environ.get("EXOMEM_CAPTURE_WAIT_SECONDS", _CAPTURE_WAIT_SECONDS))
    except ValueError:
        wait = _CAPTURE_WAIT_SECONDS
    if not math.isfinite(wait):
        wait = _CAPTURE_WAIT_SECONDS
    wait = min(max(0.0, wait), _CAPTURE_WAIT_CEILING_SECONDS)
    budget = request_budget.current()
    if budget is not None:
        wait = min(
            wait,
            max(0.0, budget.remaining() - request_budget.DELIVERY_RESERVE_SECONDS - guard_seconds),
        )
    return wait


_WARMUP_POLL_SECONDS = 0.25


def _wait_out_write_warmup(
    *, refuse_cause: str | None, deadline: float
) -> tuple[str | None, str | None, float]:
    """The component still warming when the wait ends, why it was not waited out, and seconds waited.

    Warm-up ends by itself within seconds (5-20 s per restart on a personal vault,
    2026-10-25..30), so a keyed write waits it out until `deadline`, the same
    deadline its busy-boundary wait uses, and on the same parked-worker cap,
    instead of handing the client a refusal it can only retry. An unkeyed write
    keeps the immediate refusal: a client that gave up during the wait and resent
    it would run it twice, and so does an attempt on a collection store's thread,
    which never parks. `refuse_cause` names either case. The cause is None when
    nothing was warming, and the seconds are 0 unless this write actually parked.
    """
    from . import readiness

    def still_warming() -> str | None:
        # Readiness components, a closed set that readiness.COMPONENTS fixes.
        return next(
            (c for c in ("graph_handoff", "semantic_corpus") if readiness.should_defer(c)),
            None,
        )

    warming = still_warming()
    if warming is None:
        return None, None, 0.0
    if refuse_cause is not None:
        return warming, refuse_cause, 0.0
    if not _writes_absorb_contention():
        return warming, "absorb_disabled", 0.0
    if time.monotonic() >= deadline:
        return warming, "warm_wait_exhausted", 0.0
    if not _take_capture_waiter_slot():
        return warming, "capture_waiters_full", 0.0
    started = time.monotonic()
    try:
        while warming is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Polled, not one long wait: a warm that finishes without marking
            # this component also ends the gate (should_defer turns false).
            readiness.wait(warming, min(remaining, _WARMUP_POLL_SECONDS))
            warming = still_warming()
    finally:
        _release_capture_waiter_slot()
    waited = time.monotonic() - started
    return warming, None if warming is None else "warm_wait_exhausted", waited


@dataclass(frozen=True)
class _CapturePreparationRetry:
    """The invocation's existing allowance and admission for precommit retries."""

    deadline: float
    admit: Callable[[], bool]

    def available(self) -> bool:
        return time.monotonic() < self.deadline


_ACTIVE_CAPTURE_PREPARATION_RETRY: ContextVar[_CapturePreparationRetry | None] = ContextVar(
    "exomem_capture_preparation_retry", default=None
)


def _active_capture_preparation_retry() -> _CapturePreparationRetry | None:
    return _ACTIVE_CAPTURE_PREPARATION_RETRY.get()


@dataclass
class _StoreAttempt:
    """One attempt of a write whose request thread, not the thread running it, waits out contention.

    The store thread serves every store-routed request in turn, so a write running there never
    waits in line: it makes one attempt and hands back a refusal raised before anything committed
    (`_collection_store_thread`). The request thread sets `holds_ticket`, `resubmission` and
    `deadline`; the attempt sets the rest.
    """

    holds_ticket: bool
    resubmission: bool
    deadline: float
    received: bool = False
    preparation_retry_wanted: bool = False
    handed_off: OpError | None = None
    deferred_refusal: Callable[[], None] | None = None


_ACTIVE_STORE_ATTEMPT: ContextVar[_StoreAttempt | None] = ContextVar(
    "exomem_store_attempt", default=None
)


class _FairCaptureQueue:
    """First-come-first-served order, within one process, for keyed writes
    waiting on the boundary.

    A write that meets a busy boundary or invalidated preparation joins. A
    write that arrives while others wait joins behind them instead of racing
    for the lock, so the waiter who has waited longest attempts next.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._line: list[object] = []

    def waiting(self) -> bool:
        with self._condition:
            return bool(self._line)

    def enter_line(self) -> object:
        """Take a place at the back of the line; never blocks."""
        ticket = object()
        with self._condition:
            self._line.append(ticket)
        return ticket

    def wait_turn(self, ticket: object, deadline: float) -> bool:
        with self._condition:
            while self._line[0] is not ticket:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def leave(self, ticket: object) -> None:
        with self._condition:
            if ticket in self._line:
                self._line.remove(ticket)
            self._condition.notify_all()


class _ContentionWait:
    """One keyed write's bounded wait: its deadline, its place in line and its waiter slot.

    Only the thread that waits uses it; an attempt on the store thread reads a `_StoreAttempt`.
    """

    def __init__(
        self,
        queue: _FairCaptureQueue,
        wait_seconds: float,
        *,
        started: float | None = None,
        deadline: float | None = None,
    ) -> None:
        self.queue = queue
        # A write that already waited out warm-up keeps the clock `invoke` started,
        # so its busy wait neither restarts the bound nor under-reports waited_ms.
        self.started = time.monotonic() if started is None else started
        self.deadline = self.started + wait_seconds if deadline is None else deadline
        self.ticket: object | None = None
        self.holds_slot = False
        self.absorbed = 0
        self.last_attempt_seconds = 0.0

    def take_slot(self) -> bool:
        if not self.holds_slot:
            self.holds_slot = _take_capture_waiter_slot()
        return self.holds_slot

    def join_line(self) -> None:
        if self.ticket is None:
            self.ticket = self.queue.enter_line()

    def wait_turn(self) -> bool:
        # A head ticket can return immediately even after its deadline.
        return (
            time.monotonic() < self.deadline
            and self.queue.wait_turn(self.ticket, self.deadline)
            and time.monotonic() < self.deadline
        )

    def fits_another_attempt(self) -> bool:
        # The deadline leaves room for one guard timeout, not for a leaf whose
        # preparation is slow; never start an attempt the budget cannot finish.
        return request_budget.can_afford(
            request_budget.DELIVERY_RESERVE_SECONDS + self.last_attempt_seconds
        )

    def admit_preparation_retry(self) -> bool:
        if time.monotonic() >= self.deadline or not self.take_slot():
            return False
        self.join_line()
        return self.wait_turn()

    def waited_ms(self) -> int:
        return round((time.monotonic() - self.started) * 1000)

    def close(self) -> None:
        if self.holds_slot:
            _release_capture_waiter_slot()
            self.holds_slot = False
        if self.ticket is not None:
            self.queue.leave(self.ticket)
            self.ticket = None


class LeaseManager:
    def __init__(
        self,
        config: LeaseConfig,
        *,
        client: LeaseCoordinatorClient | None = None,
        clock=time.time,  # noqa: ANN001
        mutation_timeout_seconds: float | None = None,
        mutation_poll_interval_seconds: float = 0.025,
        idempotency_wait_seconds: float = _IDEMPOTENCY_WAIT_SECONDS,
        after_terminal_persisted=None,  # noqa: ANN001
    ):
        self.config = config
        self.client = (
            client
            if client is not None
            else (LeaseCoordinatorClient(config) if config.enabled else None)
        )
        replica = config.replica_id or "standalone"
        vault = config.vault_id or "standalone"
        safe_name = hashlib.sha256(f"{vault}\0{replica}".encode()).hexdigest()[:20]
        self.idempotency = IdempotencyStore(
            config.state_dir / f"idempotency-{safe_name}.sqlite",
            clock=clock,
            wait_seconds=idempotency_wait_seconds,
            after_terminal_persisted=after_terminal_persisted,
        )
        self._fencing_token: int | None = None
        self._expires_at: float | None = None
        # When the last acquire or renew reported `_expires_at`, on this host's monotonic clock.
        self._granted_monotonic: float | None = None
        self._capture_queue = _FairCaptureQueue()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._renewer: threading.Thread | None = None
        self._mutation_timeout_seconds = (
            config.mutation_timeout_seconds
            if mutation_timeout_seconds is None
            else mutation_timeout_seconds
        )
        self._mutation_poll_interval_seconds = mutation_poll_interval_seconds
        self._last_renew_monotonic: float | None = None
        # (code, monotonic-timestamp) for the most recent coordinator RPC
        # failure, so `status()` can surface a fault instead of only ever
        # flipping `coordinator_healthy` to False with no further detail.
        self._last_coordinator_error: tuple[str, float] | None = None
        # Idle-release accounting (R5), guarded by `self._lock`.
        # `_active_mutations` is incremented/decremented ONLY by
        # `writer_authority_guard()`, which sets every command fence, so
        # `count == 0` means no command holds a fence from that guard. The
        # collection-store fence setters are not counted (see that guard).
        # Shared grant claims also protect commands in other local processes.
        self._active_mutations = 0
        # Whole guard lifetimes include claim acquisition and release, beyond captured fence work.
        self._active_authority_guards = 0
        self._last_activity_monotonic = time.monotonic()
        self._collection_store = None
        # Set by start_server_lifecycle: generic file writes must protect the store's ownership.
        self._serves_collection_store = False
        self._file_store_acquisition = None
        self._file_store_marker = None
        self._store_bound = False
        self._store_borrowers: dict[int, int] = {}
        self._store_handoff = False
        # A coalesced replica publication holds the handoff; arrivals wait for it instead.
        self._store_publishing = False
        self._store_closed = False
        self._store_released = False
        self._store_condition = threading.Condition(self._lock)
        self._report_lock = threading.Lock()

    def _record_coordinator_error(self, code: str) -> None:
        self._last_coordinator_error = (code, time.monotonic())
        try:
            from . import metrics

            metrics.inc_counter("exomem_coordinator_errors_total", {"code": code})
        except Exception:  # noqa: BLE001 - observability must never break the caller
            pass

    def _record_lease_op(self, op: str, outcome: str) -> None:
        try:
            from . import metrics

            metrics.inc_counter("exomem_lease_ops_total", {"op": op, "outcome": outcome})
        except Exception:  # noqa: BLE001 - observability must never break the caller
            pass

    def _mutation_coordinator_for(
        self, vault_root: os.PathLike[str] | str
    ) -> VaultMutationCoordinator:
        """Construct this manager's canonical boundary for one vault."""
        return VaultMutationCoordinator(
            self.config.state_dir,
            vault_root,
            timeout_seconds=self._mutation_timeout_seconds,
            poll_interval_seconds=self._mutation_poll_interval_seconds,
        )

    @contextmanager
    def _replica_grant_claim(self, *, exclusive: bool = False) -> Iterator[bool]:
        """Keep the replica's grant while any local process holds a command fence.

        A busy exclusive claim defers voluntary release, keeping writer routing
        until the in-flight command exits. Each context owns its connection.
        """
        if not self.config.enabled:
            yield True
            return
        origin = urllib.parse.urlsplit(self.config.url)
        identity = (
            origin.scheme, origin.hostname,
            origin.port if origin.port is not None else (443 if origin.scheme == "https" else 80),
            self.config.vault_id, self.config.replica_id,
        )
        key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        lock = ReadWriteLock(
            self.config.state_dir / f"writer-grant-{key}.db", is_singleton=False,
        )
        try:
            try:
                if exclusive:
                    lock.acquire_write(blocking=False)
                else:
                    lock.acquire_read(timeout=self._mutation_timeout_seconds)
            except Timeout:
                if not exclusive:
                    raise _mutation_busy() from None
                yield False
                return
            try:
                yield True
            finally:
                lock.release()
        finally:
            lock.close()

    def holds_mutation_boundary(self, vault_root: os.PathLike[str] | str) -> bool:
        """Whether this thread already holds this vault's mutation boundary.

        A nested `mutation_guard` re-enters the lock but still advances the commit
        generation on exit, so a seam that is reachable under an outer boundary
        asks first and takes no second guard.
        """
        return self._mutation_coordinator_for(vault_root).current_thread_holds_boundary()

    def _log_lease_event(
        self, event: str, *, level: int = logging.INFO, **fields: Any
    ) -> None:
        try:
            from .log_events import log_event

            log_event(logger, level, event, fields=fields)
        except Exception:  # noqa: BLE001 - observability must never break the caller
            pass

    def _offers_collection_store_capability(self) -> bool:
        """Offer the store fence capability for a producer or proved unrelated file writes."""
        if self._collection_store is not None:
            return True
        if self._serves_collection_store:
            return self._collection_file_marker() is not None
        try:
            import sqlite3
            from contextlib import closing

            from .collection_store import authority, connection
            from .vault import resolve_vault

            root = resolve_vault()
            if authority.read_marker(root) is None:
                return False
            # Preserve the media child's lightweight admission; it does not serve general file writes.
            uri = f"{connection.store_path(root).resolve().as_uri()}?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as store:
                pending = store.execute(
                    "SELECT 1 FROM store_meta WHERE key=?", (authority.PENDING_CREATE,)
                ).fetchone()
            return pending is None
        except Exception:  # noqa: BLE001 - unknown routing or an unreadable store keeps the process fenced
            return False

    def _renew_claims_store_capability(self) -> bool:
        """Claim the store capability on renew by the rule this process acquired with."""
        if self._file_store_acquisition is not None:
            # A server renews only for the publication it proved at acquisition.
            marker = self._collection_file_marker(self._file_store_acquisition)
            return marker is not None and marker == self._file_store_marker
        # A standalone writer re-proves the acquire predicate on every renew, so a
        # marker removed, or a create still pending, mid-guard is refused by the
        # coordinator, not assumed.
        return not self._serves_collection_store and self._offers_collection_store_capability()

    def _collection_file_marker(self, acquired=None, *, vault_root=None):
        try:
            from .collection_store import replica
            from .vault import resolve_vault

            return replica.file_writer_marker(resolve_vault() if vault_root is None else Path(vault_root), acquired)
        except Exception:  # noqa: BLE001 - unknown routing or an unreadable store keeps the process fenced
            return None

    def _store_refused(self) -> bool:
        """With the lock held: wait out a coalesced publication, then refuse closure or handoff.

        A thread holding a mutation boundary or a store checkout passes a publication
        (which needs both free), so the publication defers itself and never deadlocks.
        """
        thread = threading.get_ident()
        exempt = self._store_publishing and (
            thread in self._store_borrowers
            or self._mutation_coordinator_for(self._collection_store.root).current_thread_holds_boundary()
        )
        if self._store_publishing and not exempt:
            deadline = time.monotonic() + self._mutation_timeout_seconds
            while self._store_publishing and (remaining := deadline - time.monotonic()) > 0:
                self._store_condition.wait(min(0.05, remaining))
        return self._store_closed or (
            self._store_handoff and thread not in self._store_borrowers
            and not (self._store_publishing and exempt)
        )

    def await_store_admission(self, deadline: float) -> None:
        """Wait, holding nothing, for a store handoff to end before a request joins the store thread's queue.

        A handoff drains store borrowers, authority guards and the vault mutation boundary before it
        flushes, so a thread holding any of them must not wait on it, and returns at once to meet the
        deep refusals as before. A wait that reaches ``deadline`` also returns: the deep refusals then
        answer. Both ends of a handoff notify, so the poll below only bounds a missed wakeup.
        """
        thread = threading.get_ident()
        with self._store_condition:
            if not self._store_handoff:
                return
            fence = _ACTIVE_WRITE_FENCE.get()
            if (thread in self._store_borrowers or (fence is not None and fence[0] is self)
                    or (self._collection_store is not None
                        and self._mutation_coordinator_for(
                            self._collection_store.root).current_thread_holds_boundary())):
                return
            while (self._store_handoff and not self._store_closed
                   and (remaining := deadline - time.monotonic()) > 0):
                self._store_condition.wait(min(0.05, remaining))

    def _require_store_admitted(self) -> None:
        """With the lock held: raise COLLECTION_STORE_BUSY while a closure or handoff excludes this thread."""
        if self._store_refused():
            raise OpError("COLLECTION_STORE_BUSY", "collection store handoff is in progress")

    def ensure_writer(self, *, cause: str = "mutation") -> LeaseRecord:
        with self._lock:
            self._require_store_admitted()
        return self._acquire_writer(cause=cause)

    def _acquire_writer(self, *, cause: str = "mutation") -> LeaseRecord:
        """Ask the coordinator for the grant; the caller has passed `_require_store_admitted`."""
        if not self.config.enabled:
            return LeaseRecord(self.config.replica_id, None, 0, True)
        assert self.client is not None
        with self._lock:
            if self._store_handoff and not self._store_publishing:
                if threading.get_ident() not in self._store_borrowers:
                    raise OpError("COLLECTION_STORE_BUSY", "collection store handoff is in progress")
                if self._fencing_token is None:
                    self._raise_fenced(0)
                return LeaseRecord(
                    self.config.replica_id, self._expires_at, self._fencing_token, True
                )
            try:
                record = self.client.acquire(**(
                    {"collection_store_capability": COLLECTION_STORE_CAPABILITY}
                    if self._offers_collection_store_capability() else {}
                ))
            except OpError as error:
                self._record_coordinator_error(error.code)
                self._record_lease_op("acquire", "error")
                raise
            if (
                record.governance_enrolled
                and record.required_schema_version is not None
                and record.required_schema_version != self.config.schema_version
            ):
                self._record_lease_op("acquire", "schema_refused")
                raise OpError(
                    "WRITER_SCHEMA_FENCE_MISMATCH",
                    "this replica does not match the enrolled writer schema",
                    "Drain this replica and deploy the schema version selected by the "
                    "external lease fence.",
                    details={
                        "required_schema_version": record.required_schema_version,
                        "schema_fence_generation": record.schema_fence_generation,
                    },
                )
            if not record.granted or record.holder != self.config.replica_id:
                self._record_lease_op("acquire", "refused")
                raise OpError(
                    "WRITER_LEASE_REQUIRED",
                    f"replica is read-only; current writer is {holder_text(record.holder, record.expires_at)}",
                    "Send the mutation to the current writer or retry after its lease expires.",
                )
            if (self._serves_collection_store and self._collection_store is None
                    and record.required_collection_store_capability is not None):
                marker = self._collection_file_marker(record)
                if marker is None:
                    raise OpError("WRITER_LEASE_REQUIRED", "current local store publication is unverified")
                self._file_store_acquisition, self._file_store_marker = record, marker
            else:
                self._file_store_acquisition = self._file_store_marker = None
            self._record_lease_op("acquire", "granted")
            transition = self._fencing_token != record.fencing_token
            self._fencing_token = record.fencing_token
            self._note_grant(record.expires_at)
            self._store_released = False
            if self._collection_store is not None:
                self._collection_store.record_acquisition(record)
            if cause == "mutation":
                # Only a mutation-driven grant counts as write activity for the
                # idle-release timer (it closes the window between this grant
                # and the writer_authority_guard count increment). Probe and
                # startup grants must NOT refresh the timer, or a polling
                # caller — e.g. the media worker's 5s availability probe —
                # would suppress idle release forever without writing a byte.
                self._last_activity_monotonic = time.monotonic()
            # A re-grant under the same token is routine (thousands an hour); a grant that
            # changes the token is a role transition (first grant, takeover, re-grant after idle release).
            self._log_lease_event(
                "lease_acquired", level=logging.INFO if transition else logging.DEBUG, cause=cause
            )
            return record

    def _renew_interval_seconds(self) -> float:
        """How often the lease renews, and so the margin a grant needs to outlast the next renewal."""
        return max(1.0, self.config.ttl_seconds / 3)

    def _note_grant(self, expires_at: float | None) -> None:
        """With the lock held: record the expiry that an acquire or renew reported, and when it arrived."""
        self._expires_at = expires_at
        self._granted_monotonic = time.monotonic()

    def _reusable_operation_grant(self) -> LeaseRecord | None:
        """With the lock held: the grant this operation's outer guard holds, or None when the guard must acquire.

        Only a collection-store write asks for this: its transaction revalidates the token with the
        coordinator before it commits, and the coordinator revokes the holder and advances the token in
        the same transaction as every fence change. A reuse never re-grants. When a condition fails,
        the guard acquires as before, and that acquire can re-grant.
        """
        active = _ACTIVE_WRITE_FENCE.get()
        if active is None or active[0] is not self:
            return None
        token = active[1]
        if (
            self._fencing_token is None
            or token != self._fencing_token
            or self._active_mutations <= 0
            # Only a real acquire derives the file-store marker from the acquisition record.
            or (self._serves_collection_store and self._collection_store is None)
            or self._expires_at is None
            or self._granted_monotonic is None
            # Local grant age, not the coordinator's wall-clock expiry, so a skewed clock cannot widen the reuse.
            or time.monotonic() - self._granted_monotonic
            >= self.config.ttl_seconds - self._renew_interval_seconds()
        ):
            return None
        self._record_lease_op("acquire", "reused")
        return LeaseRecord(self.config.replica_id, self._expires_at, token, True)

    @contextmanager
    def consistency_guard(
        self,
        vault_root: os.PathLike[str] | str,
        *,
        request_id: str | None = None,
        operation: str | None = None,
        holder_kind: str = "unknown",
        may_borrow: bool = False,
    ) -> Iterator[VaultMutationCoordinator]:
        """Serialize hosted reads with mutations without requiring writer authority.

        `may_borrow` is for a brief derived hold that writes no canonical content: it may
        also take the boundary that a long mutation lends between its units (`lend_boundary`).
        """
        mutation = self._mutation_coordinator_for(vault_root)
        outermost = not mutation.current_thread_holds_boundary()
        with mutation.hold(
            request_id=request_id,
            operation=operation,
            holder_kind=holder_kind,
            may_borrow=may_borrow,
        ):
            try:
                yield mutation
            finally:
                session = _ACTIVE_FAST_ACK_SESSION.get()
                if outermost and session is not None and active_derived_batch_custody(vault_root):
                    # _mutation_boundary has bumped the generation. Keep exact
                    # receipt custody visible before any waiting writer enters.
                    _timed_handoff(session)

    def lend_boundary(
        self,
        vault_root: os.PathLike[str] | str,
        *,
        next_unit_seconds: float,
        next_unit_batches: int,
        room_seconds: float,
    ) -> int:
        """Let waiting borrowers hold the boundary between two committed units of a long mutation.

        A loan first publishes what a release would: the custody of the fast-acknowledged
        batches so far, then the commit generation. The holder lends only when that serves a
        borrower the next lend could not: the earliest borrower gives up before the next unit
        and the handoff after it end, yet still waits once this handoff ends, and the handoff
        fits in `room_seconds`, the time the next unit may take. No offer outlives that room.
        Returns how many borrowers held the boundary.
        """
        mutation = self._mutation_coordinator_for(vault_root)
        due = mutation.next_borrower_deadline()
        if due is None or not mutation.current_thread_holds_boundary():
            return 0
        root = Path(vault_root)
        session = _ACTIVE_FAST_ACK_SESSION.get()
        custody = session is not None and active_derived_batch_custody(root)
        handoff, later = 0.0, 0.0
        if custody:
            if any(batch.handoff_error is not None for batch in session.batches):
                # A failed handoff stays owed to the terminal: a borrower would read
                # custody that was never published.
                return 0
            handoff = owed_custody_handoff_seconds(root, more_batches=0)
            later = owed_custody_handoff_seconds(root, more_batches=next_unit_batches)
            if handoff is None or later is None:
                # Unmeasured, the handoff could take any part of the room.
                return 0
        now = time.monotonic()
        if not now + handoff < due <= now + next_unit_seconds + later or handoff >= room_seconds:
            return 0
        if custody:
            _timed_handoff(session)
            if any(batch.handoff_error is not None for batch in session.batches):
                return 0
        if mutation.next_borrower_deadline() is None:
            # Every borrower gave up during the handoff, so no reader needs a new generation.
            return 0
        _bump_commit_generation(self.config.state_dir, vault_root)
        return mutation.lend(until=now + room_seconds)

    @contextmanager
    def reserved_identity_guard(
        self,
        vault_root: os.PathLike[str] | str,
        *,
        domains: Iterable[str],
        exclusive: bool,
        advance_generation: bool = True,
        request_id: str | None = None,
        operation: str | None = None,
        holder_kind: str = "reserved-state",
        deadline: float | None = None,
    ) -> Iterator[str]:
        """Coordinate one owner's identities without serializing other owners."""

        ordered_domains = tuple(sorted(set(domains)))
        if not ordered_domains or any(
            type(domain) is not str or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", domain) is None
            for domain in ordered_domains
        ):
            raise ValueError("reserved identity domains must be registered labels")
        vault_identity = canonical_mutation_identity(vault_root)
        manager_deadline = time.monotonic() + self._mutation_timeout_seconds
        deadline = manager_deadline if deadline is None else min(deadline, manager_deadline)
        gate = self._mutation_coordinator_for(f"reserved-identity-v1:gate:{vault_identity}")

        def enter(
            stack: ExitStack,
            mutation: VaultMutationCoordinator,
            *,
            operation_label: str | None = operation,
        ) -> None:
            stack.enter_context(
                mutation.hold(
                    timeout_seconds=max(0.0, deadline - time.monotonic()),
                    request_id=request_id,
                    operation=operation_label,
                    holder_kind=holder_kind,
                    publish_holder_metadata=False,
                )
            )

        def enter_domains(stack: ExitStack) -> None:
            for domain in ordered_domains:
                identity = f"reserved-identity-v1:{domain}:{vault_identity}"
                mutation = self._mutation_coordinator_for(identity)
                label = f"{operation}:{domain}" if operation is not None else domain
                enter(stack, mutation, operation_label=label)

        if exclusive:
            with ExitStack() as gate_stack:
                gate_label = f"{operation}:gate" if operation is not None else "gate"
                enter(gate_stack, gate, operation_label=gate_label)
                with ExitStack() as domains_stack:
                    enter_domains(domains_stack)
                    yield _read_reserved_identity_generation(self.config.state_dir, vault_root)
            return

        with ExitStack() as domains_stack:
            with ExitStack() as gate_stack:
                gate_label = f"{operation}:gate" if operation is not None else "gate"
                enter(gate_stack, gate, operation_label=gate_label)
                # Admission order matches the exclusive scanner: gate first,
                # then domains.  The domain stack outlives the gate so exact
                # owners still release admission before doing their work,
                # without the gate/domain inversion that can deadlock a scan.
                enter_domains(domains_stack)
                generation = (
                    _bump_reserved_identity_generation(self.config.state_dir, vault_root)
                    if advance_generation
                    else _read_reserved_identity_generation(self.config.state_dir, vault_root)
                )
            yield generation

    @contextmanager
    def mutation_guard(
        self,
        vault_root: os.PathLike[str] | str,
        *,
        request_id: str | None = None,
        operation: str | None = None,
        holder_kind: str = "command",
        attachment_control: bool = False,
        attachment_now: int | None = None,
        session_open_admission: bool = False,
        activation_admission: bool = False,
        reuse_operation_grant: bool = False,
    ) -> Iterator[VaultMutationCoordinator]:
        """Hold the shared vault mutation boundary and revalidate writer authority.

        `reuse_operation_grant` lets a collection-store write keep the grant that its operation's
        outer guard holds. Every other guard acquires.
        """
        with self._mutation_boundary(
            vault_root,
            request_id=request_id,
            operation=operation,
            holder_kind=holder_kind,
            attachment_control=attachment_control,
            attachment_now=attachment_now,
            session_open_admission=session_open_admission,
            activation_admission=activation_admission,
            reuse_operation_grant=reuse_operation_grant,
            advance_generation=True,
        ) as mutation:
            yield mutation

    @contextmanager
    def projection_guard(
        self, vault_root: os.PathLike[str] | str, *, operation: str
    ) -> Iterator[VaultMutationCoordinator]:
        """Hold the vault mutation boundary, with writer authority, to write a projection file.

        Only for a file that a canonical source rebuilds and no canonical read depends
        on, such as the collection store's derived rows. The holder is background work,
        and the commit generation does not move, so every captured validity stamp stays
        current. A writer of canonical content takes `mutation_guard`.
        """
        with self._mutation_boundary(
            vault_root, operation=operation, holder_kind="background", advance_generation=False
        ) as mutation:
            yield mutation

    @contextmanager
    def _mutation_boundary(
        self,
        vault_root: os.PathLike[str] | str,
        *,
        request_id: str | None = None,
        operation: str | None = None,
        holder_kind: str,
        attachment_control: bool = False,
        attachment_now: int | None = None,
        session_open_admission: bool = False,
        activation_admission: bool = False,
        reuse_operation_grant: bool = False,
        advance_generation: bool,
    ) -> Iterator[VaultMutationCoordinator]:
        """`mutation_guard`, which advances the commit generation, and `projection_guard`, which does not."""
        if self._mutation_coordinator_for(vault_root).current_thread_borrows_boundary():
            # A loan falls between two units of another mutation, whose reads must stay valid.
            raise RuntimeError("a canonical write cannot run on a borrowed mutation boundary")
        direct_boundary: tuple[str, Path] | None = None
        direct_token: Token[tuple[tuple[str, Path], ...]] | None = None
        outer_direct_boundary = False
        inherited_manager = _ACTIVE_LEASE_MANAGER.get()
        if (
            inherited_manager is None
            and request_id is None
            and active_mutation_request_id() is None
        ):
            direct_boundary = _direct_mutation_boundary(vault_root, self.config.state_dir)
            active_direct = _ACTIVE_DIRECT_MUTATION_GUARDS.get()
            outer_direct_boundary = direct_boundary not in active_direct
            direct_token = _ACTIVE_DIRECT_MUTATION_GUARDS.set((*active_direct, direct_boundary))
        manager_token = _ACTIVE_LEASE_MANAGER.set(self)
        try:
            with self.consistency_guard(
                vault_root,
                request_id=request_id,
                operation=operation,
                holder_kind=holder_kind,
            ) as mutation:
                try:
                    with self.writer_authority_guard(
                        vault_root=vault_root,
                        attachment_control=attachment_control,
                        attachment_now=attachment_now,
                        session_open_admission=session_open_admission,
                        activation_admission=activation_admission,
                        reuse_operation_grant=reuse_operation_grant,
                    ):
                        yield mutation
                finally:
                    # Bump while the boundary is still held, so the counter is
                    # serialized with the mutations it fingerprints. Every
                    # governed writer of canonical content exits through here
                    # (wide or narrow), which is what lets the validity-stamp
                    # comparison skip the in-boundary corpus stat-walk.
                    if advance_generation:
                        _bump_commit_generation(self.config.state_dir, vault_root)
        finally:
            _ACTIVE_LEASE_MANAGER.reset(manager_token)
            if direct_token is not None:
                _ACTIVE_DIRECT_MUTATION_GUARDS.reset(direct_token)
        if outer_direct_boundary and (
            isinstance(vault_root, os.PathLike)
            or (isinstance(vault_root, str) and Path(vault_root).is_absolute())
        ):
            from . import graph_sync

            root = Path(vault_root)
            try:
                graph_sync.start_registered(root, state_root=self.config.state_dir)
                # The second unbounded join (#576), and the one that produced
                # the worst case measured in production: a 300 s single-unit
                # `observe_memory` append, against 14.3 s for the `remember`
                # immediately before it. This guard is held while serving a
                # request exactly like `after_operation_guard` is, so it takes
                # the same seam. There is no terminal envelope here to carry
                # `pending`, so the honest report is the log line below -- the
                # leaf's own bounded index/readiness paths already tell its
                # caller the derived graph is not current.
                if not graph_sync.join_registered_if_settled(
                    root, state_root=self.config.state_dir
                ):
                    logger.info("direct mutation left its graph rebuild running")
            except graph_sync.GraphRebuildRegistrationError:
                # Direct leaf APIs have no terminal-envelope seam for a graph
                # failure. Canonical bytes and the exact durable failure handle
                # remain authoritative; their bounded index/readiness paths
                # expose the recovery requirement.
                logger.warning(
                    "direct mutation graph join failed after canonical release",
                    exc_info=True,
                )

    @contextmanager
    def writer_authority_guard(
        self,
        *,
        vault_root: os.PathLike[str] | str | None = None,
        attachment_control: bool = False,
        attachment_now: int | None = None,
        session_open_admission: bool = False,
        activation_admission: bool = False,
        reuse_operation_grant: bool = False,
    ) -> Iterator[None]:
        """Revalidate writer authority without holding the vault mutation lock.

        The choke point for idle-release accounting (R5): every command fence is
        set and counted here, so `count == 0` means no command holds a fence
        from this guard. Two collection-store helpers also set
        `_ACTIVE_WRITE_FENCE` without counting: the admission session's `writer`
        and the offline export's `_files_as`. The count does not cover them.
        """
        fence_context: Token[tuple[Any, int] | None] | None = None
        file_context = None
        counted = False
        renewal_stop = None
        renewer = None
        admission_now = int(time.time()) if attachment_now is None else attachment_now
        if session_open_admission and activation_admission:
            raise ValueError("writer admission must name one restricted transition")
        if vault_root is not None and not attachment_control:
            from .vocabulary_admission import (
                VocabularyAdmissionError,
                require_activation_admission,
                require_mutation_admission,
                require_session_open_admission,
            )

            try:
                if session_open_admission:
                    require_session_open_admission(Path(vault_root), now=admission_now)
                elif activation_admission:
                    require_activation_admission(Path(vault_root), now=admission_now)
                else:
                    require_mutation_admission(Path(vault_root), now=admission_now)
            except VocabularyAdmissionError as exc:
                raise OpError(exc.code, "activated vocabulary authority is unavailable") from None
        if vault_root is not None and not attachment_control:
            from .governance import authorization_custody

            try:
                authorization_custody.require_standalone_mutation_admission(
                    Path(vault_root),
                    now=admission_now,
                )
            except authorization_custody.AuthorizationCustodyUnavailable:
                raise OpError(
                    "ATTACHMENT_DRAINING",
                    "the registered vault attachment is not serving mutations",
                    "Complete or recover the authenticated attachment transition before retrying.",
                ) from None
        registered = False
        try:
            if self.config.enabled or self._collection_store is not None:
                with self._lock:
                    self._require_store_admitted()
                    self._active_authority_guards += 1
                    registered = True
            with self._replica_grant_claim():
                if self.config.enabled or self._collection_store is not None:
                    with self._lock:
                        self._require_store_admitted()
                        lease = self._reusable_operation_grant() if reuse_operation_grant else None
                        if lease is None:
                            inherited = _ACTIVE_WRITE_FENCE.get()
                            # A narrowed command whose own grant was lost during unguarded
                            # preparation is fenced, whether this replica or only the
                            # coordinator has noticed; it never commits under a new grant.
                            held = inherited[1] if inherited is not None and inherited[0] is self else None
                            if held is not None and self._fencing_token != held:
                                self._raise_fenced(held)
                            try:
                                lease = self._acquire_writer()
                            except OpError as error:
                                if held is not None and error.code == "WRITER_LEASE_REQUIRED":
                                    self.validate_fencing_token(held)
                                raise
                            if held is not None and lease.fencing_token != held:
                                self._raise_fenced(held)
                        if self.config.enabled:
                            fence_context = _ACTIVE_WRITE_FENCE.set((self, lease.fencing_token))
                        if self._serves_collection_store and self._collection_store is None:
                            # Keep this mutation's file classification if a producer attaches during staging.
                            file_context = _ACTIVE_COLLECTION_FILE_WRITE.set((
                                self, self._file_store_acquisition, self._file_store_marker,
                            ))
                        self._active_mutations += 1
                        self._last_activity_monotonic = time.monotonic()
                    counted = True
                try:
                    if (fence_context is not None and self._collection_store is None
                            and (self._renewer is None or not self._renewer.is_alive())):
                        # Standalone file mutations lack the server's renewal lifecycle.
                        # Each guard owns only its captured token, never a replacement grant.
                        renewal_stop = threading.Event()
                        renewer = threading.Thread(
                            target=self._renew_loop,
                            kwargs={"stop": renewal_stop, "fencing_token": lease.fencing_token},
                            name="exomem-mutation-lease",
                            daemon=True,
                        )
                        renewer.start()
                    yield
                finally:
                    if renewal_stop is not None:
                        renewal_stop.set()
                        # The renewal needs the manager lock; keep this join outside it.
                        # Finish in-flight coordinator I/O before ending this guard's ownership.
                        if renewer is not None and renewer.ident is not None:
                            renewer.join()
                    if file_context is not None:
                        _ACTIVE_COLLECTION_FILE_WRITE.reset(file_context)
                    if fence_context is not None:
                        _ACTIVE_WRITE_FENCE.reset(fence_context)
                    if counted:
                        with self._lock:
                            self._active_mutations -= 1
                            self._last_activity_monotonic = time.monotonic()
                            self._store_condition.notify_all()
        finally:
            if registered:
                with self._lock:
                    self._active_authority_guards -= 1
                    self._store_condition.notify_all()

    def invoke(
        self,
        command: Any,
        injected: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        read_only: bool | None = None,
        idempotency_key: str | None = None,
        public_idempotency_key: str | None | object = _PUBLIC_IDEMPOTENCY_KEY_UNSET,
        idempotency_principal_scope: str | None = None,
        implicit_idempotency_scope: str | None = None,
        mutation_request_id: str | None = None,
    ) -> Any:
        t_start = time.perf_counter()
        # Same instant on the clock the acknowledgement's own deadlines use, so
        # the two bounds are comparable without converting between them, and
        # the earlier of the lease's own budget and the connector's.
        acknowledgement_deadline = acknowledgement_budget_deadline()
        kwargs = _canonicalize_command_kwargs(kwargs)
        configured_response_detail = getattr(command, "response_detail", None)
        response_detail_default: ResponseDetail = (
            configured_response_detail
            if configured_response_detail in {"compact", "full", "legacy"}
            else "compact"
        )
        kwargs, response_detail = split_response_detail(kwargs, default=response_detail_default)
        if public_idempotency_key is _PUBLIC_IDEMPOTENCY_KEY_UNSET:
            effective_public_idempotency_key = idempotency_key
        else:
            assert public_idempotency_key is None or isinstance(public_idempotency_key, str)
            effective_public_idempotency_key = public_idempotency_key
        authorization_issuance = (
            command.name == "govern_memory"
            and kwargs.get("operation") == "session"
            and kwargs.get("session_action") in {"open", "rotate"}
        ) or (
            # Both closed transfer operations issue bearer capabilities, not
            # durable mutations whose result may be replayed to another session.
            command.name == "transfer_artifact"
            and kwargs.get("operation", "upload") in {"upload", "download"}
        )
        session_open_admission = (
            command.name == "govern_memory"
            and kwargs.get("operation") == "session"
            and kwargs.get("session_action") == "open"
        )
        if authorization_issuance:
            # The generic mutation store pickles its terminal for replay. An
            # credential issuance terminal contains a raw bearer, so it must never enter
            # that durable cache (or be emitted a second time by replay).
            idempotency_key = None
            implicit_idempotency_scope = None
            effective_public_idempotency_key = None
        invocation_read_only = command.read_only if read_only is None else read_only
        if invocation_read_only:
            # Reads never take the mutation boundary, hosted or local: every
            # canonical write lands via atomic staging (write-to-temp then
            # `os.replace`), which already makes a concurrent whole-file read
            # torn-free without any lock. This supersedes the archived
            # decision that reserved the bypass to `mode="audit"`/
            # `validate_only` reads while other hosted reads held the guard
            # (openspec/changes/archive/2026-07-20-make-mcp-acknowledgement-replay-safe/design.md:64).
            manager_token = _ACTIVE_LEASE_MANAGER.set(self)
            try:
                return command.leaf(*injected, **kwargs)
            finally:
                _ACTIVE_LEASE_MANAGER.reset(manager_token)
        mutation_subject = self._mutation_subject(injected)
        receipt_vault_root = self._receipt_vault_root(injected)
        digest = _command_digest(command, kwargs)
        key, expires_after, on_replay = _effective_idempotency_key(
            self,
            command=command,
            mutation_subject=mutation_subject,
            digest=digest,
            idempotency_key=idempotency_key,
            principal_scope=idempotency_principal_scope,
            implicit_idempotency_scope=implicit_idempotency_scope,
        )
        # The receipt hooks below are reached only through the idempotent
        # state-machine path; unkeyed writes bypass `IdempotencyStore.run`'s
        # durable receipt protocol.
        if key is not None:
            receipt_key_digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        else:
            receipt_key_digest = None
        request_id = mutation_request_id or str(uuid.uuid4())
        receipt = _receipt_tag(key) if key else None
        vocabulary_binding = None
        vocabulary_replay_terminal = self.idempotency.completed_terminal(key, digest)
        # The private-identity inventory is a whole-vault walk. Build it here,
        # after the replay check (a durable terminal replays without paying for
        # it) and before any boundary is taken, so a write that meets it cold
        # does not hold every other writer stopped while it walks.
        if receipt_vault_root is not None and vocabulary_replay_terminal is None:
            from . import reserved_paths

            reserved_paths.warm_identity_catalogue_before_boundary(receipt_vault_root)
        if kwargs.get("vocabulary_ref") is not None or kwargs.get("vocabulary_fingerprint") is not None:
            # These fields only correlate an explicit canonical writer call
            # with an already reviewed choice.  They never carry a receipt,
            # resulting version, or permission supplied by the caller.
            if receipt_vault_root is None or key is None:
                raise OpError(
                    "VOCABULARY_APPLICATION_INVALID",
                    "a vocabulary application requires a canonical idempotency identity",
                    "Retry the same reviewed canonical write with an idempotency key.",
                )
            from . import vocabulary_application
            from .governance.principal import effective_principal

            principal = effective_principal()
            if not principal.resolved:
                raise OpError(
                    "VOCABULARY_APPLICATION_INVALID",
                    "a vocabulary application requires a resolved principal",
                    "Retry through an authenticated or local-owner writer surface.",
                )
            vocabulary_binding = vocabulary_application.bind(
                receipt_vault_root,
                command=command.name,
                kwargs=kwargs,
                idempotency_key=key,
                command_digest=digest,
                principal=principal.audience_id,
                request_id=request_id,
                canonical_terminal=vocabulary_replay_terminal,
            )
        commit_state = {"observed": False, "leaf_started": False}
        # Derived work the leaf hands back rather than running inside its own
        # guard. Drained by `idempotency.run`'s post-persistence acknowledgement
        # hook, strictly after the terminal is durable.
        post_terminal_fanout: list[Any] = []
        # Housekeeping with no derived-custody implications, drained the same
        # way but never read as a sign that acknowledgement is owed.
        post_terminal_housekeeping: list[Callable[[], None]] = []
        fast_ack_session = (
            _FastAcknowledgementSession(
                vault_root=receipt_vault_root,
                request_id=request_id,
                mutation_attempt_digest=digest,
                terminal_replay_until=self.idempotency.clock()
                + (expires_after or _IMPLICIT_RETRY_TTL_SECONDS),
                advisory_required=True,
            )
            if _fast_ack_enabled(command.name, kwargs.get("action"))
            and not kwargs.get("suggestions")
            and receipt_vault_root is not None
            and _is_receipt_vault_root(receipt_vault_root)
            else None
        )
        store_attempt = _ACTIVE_STORE_ATTEMPT.get()
        if store_attempt is None or not store_attempt.resubmission:
            _log_mutation_event(
                "received",
                request_id=request_id,
                command=command.name,
                receipt=receipt or "none",
            )
        if store_attempt is not None:
            store_attempt.received = True
        # The graph handoff joins this gate for the reason the corpus is on it:
        # a write admitted before its delta origin exists does not merely run
        # slower, it registers a whole-vault rebuild for a lineage it could
        # have adopted in the first seconds of the warm. Both refusals are the
        # same retryable shape, and reads are untouched either way.
        # One clock for every wait this write makes before it runs: warm-up here,
        # then a busy boundary, so neither restarts the bound or under-reports it.
        wait_started = time.monotonic()
        wait_deadline = wait_started + _capture_wait_seconds(self._mutation_timeout_seconds)
        warming, warm_cause, warm_waited = _wait_out_write_warmup(
            refuse_cause=(
                "unkeyed" if key is None else "store_thread" if store_attempt is not None else None
            ),
            deadline=wait_deadline,
        )
        warm_waited_ms = round(warm_waited * 1000)
        if warm_waited_ms > 0:
            _log_mutation_event(
                "warmup_waited",
                request_id=request_id,
                command=command.name,
                receipt=receipt or "none",
                waited_ms=warm_waited_ms,
                outcome="admitted" if warming is None else warm_cause,
            )
        if warming is not None:
            details: dict[str, Any] = {
                "status": "retryable",
                "committed": False,
                "retry_after_ms": 750,
                "request_id": request_id,
                "receipt_id": receipt,
                "cause": warm_cause,
                "waited_ms": warm_waited_ms,
            }
            if effective_public_idempotency_key is not None:
                details["idempotency_key"] = effective_public_idempotency_key
            _log_mutation_event(
                "interrupted",
                level=logging.INFO,
                request_id=request_id,
                command=command.name,
                receipt=receipt or "none",
                error="MUTATION_WARMING",
            )
            details["warming_component"] = warming
            # This gate runs ahead of the journaled block below, so the refusal records itself.
            self._record_mutation_journal(
                request_id=request_id,
                command=command.name,
                receipt=receipt,
                outcome="failed",
                error_code="MUTATION_WARMING",
                duration_ms=round((time.perf_counter() - t_start) * 1000, 2),
                scope=implicit_idempotency_scope or idempotency_principal_scope,
                targets=[str(mutation_subject)],
                boundary_timing=(None, None),
            )
            warming_refusal = OpError(
                "MUTATION_WARMING",
                (
                    "graph handoff warm-up is still in progress"
                    if warming == "graph_handoff"
                    else "semantic corpus warm-up is still in progress"
                ),
                "Retry the same mutation after warm-up completes.",
                details=details,
            )
            # Claimed after its row, so a commit that wraps this refusal does not journal it again.
            mutation_journal.claim_refusal(warming_refusal)
            raise warming_refusal

        def invoke_leaf() -> Any:
            commit_state["leaf_started"] = True
            trace_token = _ACTIVE_MUTATION_TRACE.set((request_id, command.name, receipt or "none"))
            commit_token = _ACTIVE_MUTATION_COMMITTED.set(False)
            manager_token = _ACTIVE_LEASE_MANAGER.set(self)
            from .governance.principal import effective_principal
            from .vocabulary_gate import attach_evidence, operation_context

            gate_context = None
            post_terminal_token = _ACTIVE_POST_TERMINAL_FANOUT.set(post_terminal_fanout)
            housekeeping_token = _ACTIVE_POST_TERMINAL_HOUSEKEEPING.set(
                post_terminal_housekeeping
            )
            try:
                with operation_context(
                    receipt_vault_root,
                    idempotency_key=key,
                    command_digest=digest,
                    receipt_id=receipt,
                    principal=effective_principal(),
                ) as gate_context:
                    leaf_result = command.leaf(*injected, **kwargs)
                    if vocabulary_binding is not None and not _ACTIVE_MUTATION_COMMITTED.get():
                        from . import vocabulary_application

                        vocabulary_application.refuse_identity_preparation(leaf_result)
                    if _ACTIVE_MUTATION_COMMITTED.get():
                        terminal = committed_terminal(
                            leaf_result,
                            request_id=request_id,
                            receipt_id=receipt,
                            idempotency_key=effective_public_idempotency_key,
                        )
                        _note_committed_terminal_targets(terminal)
                        return attach_evidence(terminal, gate_context)
                    if isinstance(leaf_result, _CanonicalRequestReplay) or ((
                        command.name in {"record_memory", "plan_memory"}
                        and valid_collection_receipt(leaf_result)
                        or command.name == "maintain_memory"
                        and kwargs.get("mode") == "structured-files"
                        and valid_structured_files_receipt(leaf_result)
                        or command.name == "maintain_memory"
                        and kwargs.get("mode") == "curation"
                        and curation_module.valid_replay_result(leaf_result)
                    ) and leaf_result.get("outcome") == "replayed"):
                        return attach_evidence(
                            replayed_terminal(
                                leaf_result,
                                request_id=request_id,
                                receipt_id=receipt,
                                idempotency_key=effective_public_idempotency_key,
                            ),
                            gate_context,
                        )
                    if command.name == "process_media" and kwargs.get("paths") is not None:
                        terminal = settled_media_terminal(
                            leaf_result,
                            request_id=request_id,
                            receipt_id=receipt,
                            idempotency_key=effective_public_idempotency_key,
                        )
                        if terminal is not None:
                            return attach_evidence(terminal, gate_context)
                    # A guarded write that validated but did not commit is mid-flight, not
                    # failed. Give it the same envelope shape as its eventual success so a
                    # client can correlate the pair on `operation_id` and see `terminal`
                    # is false, instead of reading the first response as the outcome.
                    return attach_evidence(needs_review_terminal(leaf_result), gate_context)
            except Exception as error:
                if (
                    _ACTIVE_MUTATION_COMMITTED.get()
                    and getattr(error, "committed", None) is not True
                ):
                    logger.warning(
                        "committed mutation raised after its canonical commit; the "
                        "terminal is committed-uncertain (%s)",
                        _content_free_cause(error, stage="post_commit_leaf"),
                    )
                    raise _PostCommitOutcomeUncertain() from error
                raise
            finally:
                commit_state["observed"] = _ACTIVE_MUTATION_COMMITTED.get()
                _ACTIVE_POST_TERMINAL_FANOUT.reset(post_terminal_token)
                _ACTIVE_POST_TERMINAL_HOUSEKEEPING.reset(housekeeping_token)
                _ACTIVE_LEASE_MANAGER.reset(manager_token)
                _ACTIVE_MUTATION_COMMITTED.reset(commit_token)
                _ACTIVE_MUTATION_TRACE.reset(trace_token)

        def with_graph_outcome(result: Any, outcome: Mapping[str, Any]) -> Any:
            """Keep derived graph health in the durable terminal payload."""
            if not isinstance(result, Mapping):
                return result
            terminal = {**result, **outcome}
            leaf = result.get("leaf_result")
            if isinstance(leaf, Mapping) and not valid_collection_receipt(leaf):
                terminal["leaf_result"] = {**leaf, **outcome}
            return terminal

        def graph_failure(checkpoint: Any, error: BaseException | None = None) -> dict[str, str]:
            from . import graph_sync

            if isinstance(error, graph_sync.GraphRepairQueued):
                return graph_sync.committed_graph_queued(checkpoint)
            if isinstance(error, graph_sync.GraphRebuildInProgress):
                return graph_sync.committed_graph_pending(checkpoint)
            if isinstance(error, graph_sync.GraphRebuildRegistrationError):
                return graph_sync.committed_graph_failure(
                    checkpoint, code=error.code, remediation=error.remediation
                )
            return graph_sync.committed_graph_failure(checkpoint)

        def joins_unbounded_graph(result: Any) -> bool:
            """Whether this command's terminal *is* the derived graph state.

            One predicate, two readers that must agree. `wait_for_graph_sync`
            uses it to decide whether to join the rebuild flight unbounded, and
            the composition below uses it to decide whether this command's
            guard runs before or after terminal persistence -- because a
            command that joins unbounded is exactly the one whose terminal
            cannot be made durable until that join has finalized its handoff.
            """
            return (
                command.name == "reconcile"
                or (command.name == "maintain_memory" and kwargs.get("mode") == "reconcile")
                or _carries_graph_rebuild_handoff(result)
            )

        def wait_for_graph_sync(result: Any) -> Any:
            """Join derived graph work only after the canonical guard released."""
            terminal_result = (
                {key: value for key, value in result.items() if key != "_graph_sync_checkpoint"}
                if isinstance(result, Mapping)
                else result
            )
            has_reconcile_handoff = _carries_graph_rebuild_handoff(terminal_result)

            def finish_reconcile_graph_status(value: Any, *, current: bool) -> Any:
                if not isinstance(value, Mapping):
                    return value
                final = dict(value)
                refreshed = final.pop("_graph_reconcile_registered", None)
                leaf = final.get("leaf_result")
                if refreshed is None and isinstance(leaf, Mapping):
                    refreshed = leaf.get("_graph_reconcile_registered")
                if refreshed is None:
                    return final
                if isinstance(leaf, Mapping):
                    reported_leaf = dict(leaf)
                    reported_leaf.pop("_graph_reconcile_registered", None)
                    final["leaf_result"] = {
                        **reported_leaf,
                        "graph_status": "refreshed" if current else "unavailable",
                        "graph_refreshed": refreshed if current else 0,
                    }
                else:
                    final["graph_status"] = "refreshed" if current else "unavailable"
                    final["graph_refreshed"] = refreshed if current else 0
                return final

            required = None
            try:
                from . import graph_sync

                root = receipt_vault_root
                if root is None:
                    return terminal_result
                required = graph_sync.registered_checkpoint(root, state_root=self.config.state_dir)
                if required is None and not has_reconcile_handoff:
                    durable = _durable_graph_outcome(root)
                    if durable is not None:
                        return with_graph_outcome(terminal_result, durable)
                    return terminal_result
                if required is not None:
                    # An interactive write takes a derived-graph outcome only if
                    # one is already there (#576/#588); it never waits for one.
                    # Maintenance whose whole purpose is to make the graph
                    # current opts back in to the unbounded join rather than
                    # every write paying for it: `reconcile` proves readability
                    # in its own terminal below, and a rebuild handoff has
                    # nothing to hand off until the flight lands.
                    if joins_unbounded_graph(terminal_result):
                        # graph-join: unbounded by design (reconcile proves the
                        # graph is readable in its own terminal).
                        graph_sync.wait_for_registered(root, state_root=self.config.state_dir)
                    elif not graph_sync.join_registered_if_settled(
                        root, state_root=self.config.state_dir
                    ):
                        # The flight keeps running on its own daemon thread and
                        # publishes behind this response; the caller is told the
                        # derived graph has not caught up yet rather than left
                        # to infer that it has.
                        return with_graph_outcome(
                            terminal_result, graph_sync.committed_graph_pending(required)
                        )
            except Exception as error:  # noqa: BLE001 - canonical commit remains terminal
                terminal_result = finish_reconcile_graph_status(terminal_result, current=False)
                if has_reconcile_handoff and isinstance(terminal_result, Mapping):
                    if root is None:
                        return terminal_result
                    from . import reconcile as reconcile_module

                    leaf = terminal_result.get("leaf_result")
                    if isinstance(leaf, Mapping):
                        return {
                            **terminal_result,
                            "leaf_result": reconcile_module.finalize_graph_rebuild_handoff(
                                root, leaf, state_root=self.config.state_dir
                            ),
                        }
                    return reconcile_module.finalize_graph_rebuild_handoff(
                        root, terminal_result, state_root=self.config.state_dir
                    )
                if isinstance(result, Mapping) and required is not None:
                    return with_graph_outcome(terminal_result, graph_failure(required, error))
                return terminal_result
            if command.name == "reconcile" or (
                command.name == "maintain_memory" and kwargs.get("mode") == "reconcile"
            ):
                from . import audit as audit_module
                from .epistemic_graph import EpistemicGraphIndex

                def graph_is_current() -> bool:
                    return (
                        graph_sync.status(root)["state"] == "current"
                        and EpistemicGraphIndex(
                            root, mutation_coordinator=self._mutation_coordinator_for(root)
                        ).available()
                        and not audit_module._check_graph_drift(root)
                    )

                try:
                    graph_current = graph_is_current()
                    if not graph_current and not has_reconcile_handoff:
                        # A startup/background mutation can supersede the exact
                        # checkpoint reconcile joined while its canonical guard
                        # is still held. Reconcile is the one command whose
                        # terminal promises graph currency, so close that final
                        # post-guard drift here instead of returning
                        # ``unavailable`` while the drain rebuilds moments later.
                        EpistemicGraphIndex(
                            root, mutation_coordinator=self._mutation_coordinator_for(root)
                        ).rebuild_all()
                        graph_current = graph_is_current()
                except Exception:  # noqa: BLE001 - preserve a failed graph terminal
                    graph_current = False
                terminal_result = finish_reconcile_graph_status(
                    terminal_result, current=graph_current
                )
                if has_reconcile_handoff and isinstance(terminal_result, Mapping):
                    from . import reconcile as reconcile_module

                    leaf = terminal_result.get("leaf_result")
                    if isinstance(leaf, Mapping):
                        terminal_result = {
                            **terminal_result,
                            "leaf_result": reconcile_module.finalize_graph_rebuild_handoff(
                                root, leaf, state_root=self.config.state_dir
                            ),
                        }
                    else:
                        terminal_result = reconcile_module.finalize_graph_rebuild_handoff(
                            root, terminal_result, state_root=self.config.state_dir
                        )
            if isinstance(terminal_result, Mapping):
                return with_graph_outcome(terminal_result, {"graph_sync": "completed"})
            return terminal_result

        def finish_fast_ack_and_graph(result: Any) -> Any:
            acknowledged = (
                _acknowledge_derived_batches(
                    result,
                    fast_ack_session,
                    budget_deadline=acknowledgement_deadline,
                )
                if fast_ack_session is not None
                else result
            )
            terminal = wait_for_graph_sync(acknowledged)
            if receipt_vault_root is not None:
                from .vocabulary_delivery import after_commit

                terminal = after_commit(receipt_vault_root, terminal)
            if vocabulary_binding is not None and isinstance(terminal, Mapping):
                # This hook is reached only after IdempotencyStore persisted
                # the canonical mutation. A correlation write must never turn
                # that committed result into an error or a fabricated applied
                # state when its own durable record cannot be refreshed.
                try:
                    if terminal.get("state") == "committed":
                        from . import vocabulary_application

                        vocabulary_application.commit(
                            receipt_vault_root,
                            vocabulary_binding,
                            vocabulary_application._writer_terminal(terminal),
                        )
                except Exception:  # noqa: BLE001 - canonical replay must remain successful
                    try:
                        from .vocabulary_state import VocabularyState

                        VocabularyState(receipt_vault_root).record_application_uncertain(
                            vocabulary_binding.item,
                            operation_id=vocabulary_binding.operation_id,
                        )
                    except Exception:  # noqa: BLE001 - correlation recovery is advisory
                        # The canonical terminal is already durable. A second
                        # local state failure stays recoverable rather than
                        # changing that completed mutation into a failure.
                        pass
            return terminal

        def persist_graph_sync_progress(
            result: Any, attempt: _ExecutionAttempt, canonical_disposition: str = "success"
        ) -> Any:
            """Persist exact canonical evidence while canonical authority is held."""
            if post_terminal_fanout:
                result = _awaiting_derived_acknowledgement(result)
            if not commit_state["observed"]:
                return result
            try:
                from . import graph_sync

                root = receipt_vault_root
                if root is None or not _is_receipt_vault_root(root):
                    raise _PostCommitOutcomeUncertain()
                required = graph_sync.registered_checkpoint(root, state_root=self.config.state_dir)
                current = graph_sync.read_checkpoint(root)
                if current is not None and current.mutation_id == attempt.commit_token:
                    required = current
                projection = {
                    name: result.get(name)
                    for name in (
                        "_terminal",
                        "version",
                        "ok",
                        "state",
                        "status",
                        "committed",
                        "mutated",
                        "terminal",
                        "request_id",
                        "receipt_id",
                        "operation_id",
                        "warnings_count",
                        "additive_authority",
                        "derived_sync",
                    )
                    if isinstance(result, Mapping) and name in result
                }
                if isinstance(result, Mapping):
                    resolution = result.get("vocabulary_resolution")
                    leaf = result.get("leaf_result")
                    if resolution is None and isinstance(leaf, Mapping):
                        resolution = leaf.get("vocabulary_resolution")
                    if resolution is not None:
                        from .vocabulary_resolution import valid_public_resolution

                        if valid_public_resolution(resolution):
                            projection["vocabulary_resolution"] = resolution
                projection["result_sha256"] = _receipt_result_sha256(result)
                if not projection:
                    projection = {"status": "committed", "mutated": True}
                evidence = graph_sync.GraphCommitReceipt.create(
                    idempotency_key_digest=receipt_key_digest or "0" * 64,
                    command_digest=digest,
                    attempt_id=attempt.attempt_id,
                    commit_token=attempt.commit_token,
                    canonical_disposition=canonical_disposition,
                    terminal_projection=projection,
                    checkpoint_generation=(required.generation if required is not None else None),
                    checkpoint_sha256=(
                        required.checkpoint_sha256 if required is not None else None
                    ),
                    commit_secret=attempt.commit_secret,
                )
                graph_sync.write_graph_commit_receipt(root, evidence)
                if isinstance(result, Mapping) and required is not None:
                    return {
                        **result,
                        "_graph_sync_checkpoint": required.as_dict(),
                    }
                return result
            except Exception as error:
                logger.warning(
                    "graph commit evidence could not be persisted; the terminal is "
                    "committed-uncertain (%s)",
                    _content_free_cause(error, stage="graph_commit_evidence"),
                )
                raise _PostCommitOutcomeUncertain() from error

        def exact_commit_evidence(
            expected_digest: str, attempt_id: str, claim_token: str, commit_secret: bytes
        ) -> Any:
            try:
                from . import graph_sync

                root = receipt_vault_root
                if root is None or not _is_receipt_vault_root(root):
                    return False
                evidence = graph_sync.read_graph_commit_receipt(root, claim_token)
                if not (
                    evidence is not None
                    and evidence.verify(
                        commit_secret,
                        idempotency_key_digest=receipt_key_digest or "0" * 64,
                        command_digest=expected_digest,
                        attempt_id=attempt_id,
                        commit_token=claim_token,
                    )
                ):
                    return None
                return evidence
            except Exception:  # noqa: BLE001 - absence is fail-closed
                return None

        def legacy_graph_pending_proof(candidate: Any) -> bool:
            """Authorize only the retired row's exact coherent graph epoch."""
            try:
                from . import graph_sync

                root = receipt_vault_root
                if root is None or not _is_receipt_vault_root(root):
                    return False
                epoch = graph_sync.classify_epoch(root)
                return (
                    epoch.kind == "coherent"
                    and epoch.checkpoint is not None
                    and epoch.checkpoint == candidate
                    and graph_sync.read_checkpoint(root) == candidate
                )
            except Exception:  # noqa: BLE001 - legacy migration is fail-closed
                return False

        def resume_graph_sync(result: Any) -> Any:
            """Resume only a durable derived checkpoint after owner death."""
            required = None
            evidence: Any = None
            terminal: dict[str, Any] | None = None
            committed_failure: dict[str, Any] | None = None

            def retain_failure(value: Any) -> Any:
                return (
                    _CanonicalCommittedFailure(value, committed_failure)
                    if committed_failure is not None
                    else value
                )

            def finalize_graph_rebuild_handoff(value: Any) -> Any:
                if not isinstance(value, Mapping) or root is None:
                    return value
                from . import reconcile as reconcile_module

                leaf = value.get("leaf_result", value)
                if not (
                    isinstance(leaf, Mapping)
                    and isinstance(leaf.get("_graph_rebuild_handoff"), Mapping)
                ):
                    return value
                finalized = reconcile_module.finalize_graph_rebuild_handoff(
                    root, leaf, state_root=self.config.state_dir
                )
                return {**value, "leaf_result": finalized} if "leaf_result" in value else finalized

            try:
                from . import graph_sync
                from .epistemic_graph import EpistemicGraphIndex

                root = receipt_vault_root
                if isinstance(result, _CanonicalResume):
                    evidence = result.evidence
                    result = result.result
                    # A canonical row without its retained terminal needs
                    # the exact receipt *and* its matching local secret to
                    # establish what crossed the multi-store cut.  A cleanup
                    # crash may leave the row but lose that trusted material;
                    # derived recovery cannot fabricate a success from it.
                    if result is None and not isinstance(evidence, graph_sync.GraphCommitReceipt):
                        return _OUTCOME_UNKNOWN_TERMINAL
                if isinstance(result, _CanonicalCommittedFailure):
                    committed_failure = result.payload
                    result = result.result
                if result is None:
                    if not isinstance(evidence, graph_sync.GraphCommitReceipt):
                        raise ValueError("canonical receipt terminal projection is unavailable")
                    terminal = dict(evidence.terminal_projection)
                    # The portable receipt deliberately cannot retain leaf
                    # paths/content. Keep the public terminal envelope valid
                    # with an empty local diagnostics projection.
                    terminal["leaf_result"] = {}
                    receipt_only_failure = evidence.canonical_disposition == "committed_failure"
                    if terminal.get("_terminal") == "exomem.mutation-terminal":
                        if effective_public_idempotency_key is not None:
                            terminal["idempotency_key"] = effective_public_idempotency_key
                    if root is None or not _is_receipt_vault_root(root):
                        if receipt_only_failure:
                            return _OUTCOME_UNKNOWN_TERMINAL
                        return retain_failure(
                            with_graph_outcome(
                                terminal,
                                {
                            "graph_sync": "failed",
                            "graph_sync_code": "GRAPH_SYNC_VAULT_AUTHORITY_MISSING",
                            "graph_sync_remediation": (
                                "Configure the vault mount and run reconcile to recover the derived graph."
                            ),
                                },
                            )
                        )
                    required = graph_sync.read_checkpoint(root)
                    if (evidence.checkpoint_generation, evidence.checkpoint_sha256) != (
                        required.generation if required is not None else None,
                        required.checkpoint_sha256 if required is not None else None,
                    ):
                        if receipt_only_failure:
                            return _OUTCOME_UNKNOWN_TERMINAL
                        if required is not None:
                            return retain_failure(
                                with_graph_outcome(terminal, graph_failure(required))
                            )
                        return retain_failure(
                            with_graph_outcome(
                                terminal,
                                {
                            "graph_sync": "failed",
                            "graph_sync_code": "GRAPH_SYNC_CHECKPOINT_MISSING",
                            "graph_sync_remediation": "Run reconcile to recover the derived graph.",
                                },
                            )
                        )
                    if required is None:
                        if receipt_only_failure:
                            return _OUTCOME_UNKNOWN_TERMINAL
                        return retain_failure(
                            with_graph_outcome(terminal, {"graph_sync": "completed"})
                        )
                    EpistemicGraphIndex(
                        root, mutation_coordinator=self._mutation_coordinator_for(root)
                    ).rebuild_all()
                    if graph_sync.status(root)["state"] == "current":
                        if receipt_only_failure:
                            return _OUTCOME_UNKNOWN_TERMINAL
                        return retain_failure(
                            with_graph_outcome(terminal, {"graph_sync": "completed"})
                        )
                    if receipt_only_failure:
                        return _OUTCOME_UNKNOWN_TERMINAL
                    return retain_failure(with_graph_outcome(terminal, graph_failure(required)))
                if not isinstance(result, Mapping):
                    return retain_failure(result)
                if root is None or not _is_receipt_vault_root(root):
                    terminal = {
                        key: value
                        for key, value in result.items()
                        if key != "_graph_sync_checkpoint"
                    }
                    return retain_failure(
                        with_graph_outcome(
                            terminal,
                            {
                        "graph_sync": "failed",
                        "graph_sync_code": "GRAPH_SYNC_VAULT_AUTHORITY_MISSING",
                        "graph_sync_remediation": (
                            "Configure the vault mount and run reconcile to recover the derived graph."
                        ),
                            },
                        )
                    )
                payload = result.get("_graph_sync_checkpoint")
                if payload is None:
                    return retain_failure(result)
                if not isinstance(payload, dict):
                    raise ValueError("canonical checkpoint payload is invalid")
                stored = graph_sync.GraphSyncCheckpoint.parse(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
                )
                if stored is None:
                    raise ValueError("graph-pending checkpoint payload is invalid")
                required = graph_sync.read_checkpoint(root)
                if required is None or stored != required:
                    terminal = {
                        key: value
                        for key, value in result.items()
                        if key != "_graph_sync_checkpoint"
                    }
                    if required is not None:
                        return retain_failure(
                            with_graph_outcome(
                            terminal, graph_sync.committed_graph_failure(required)
                            )
                        )
                    return retain_failure(
                        with_graph_outcome(
                            terminal,
                            {
                        "graph_sync": "failed",
                        "graph_sync_code": "GRAPH_SYNC_CHECKPOINT_MISSING",
                        "graph_sync_remediation": "Run reconcile to recover the derived graph.",
                            },
                        )
                    )
                EpistemicGraphIndex(
                    root, mutation_coordinator=self._mutation_coordinator_for(root)
                ).rebuild_all()
                terminal = {
                    key: value for key, value in result.items() if key != "_graph_sync_checkpoint"
                }
                if graph_sync.status(root)["state"] == "current":
                    terminal = finalize_graph_rebuild_handoff(terminal)
                    return retain_failure(with_graph_outcome(terminal, {"graph_sync": "completed"}))
                return retain_failure(
                    with_graph_outcome(terminal, graph_sync.committed_graph_failure(required))
                )
            except Exception as error:  # noqa: BLE001 - canonical commit remains terminal
                if terminal is None:
                    if isinstance(result, Mapping):
                        terminal = {
                            key: value
                            for key, value in result.items()
                            if key != "_graph_sync_checkpoint"
                        }
                    elif isinstance(getattr(evidence, "terminal_projection", None), Mapping):
                        terminal = dict(evidence.terminal_projection)
                    else:
                        terminal = {}
                if required is not None:
                    return retain_failure(
                        with_graph_outcome(terminal, graph_failure(required, error))
                    )
                return retain_failure(
                    with_graph_outcome(
                        terminal,
                        {
                    "graph_sync": "failed",
                    "graph_sync_code": "GRAPH_SYNC_RESUME_FAILED",
                    "graph_sync_remediation": "Run reconcile to recover the derived graph.",
                        },
                    )
                )

        from .commands import ProcessMediaOperation

        media_operation = kwargs.get("operation", "process")
        narrow_media_commit = (
            command.name == "process_media"
            and media_operation in get_args(ProcessMediaOperation)
            and media_operation != "status"
        )
        # `manage_memory_file` is the Tier-2 escape hatch's single consolidated
        # command; only its `create`/`append` operations narrow. Their leaves
        # are self-guarding on EVERY routing: governed Markdown goes through
        # `commit_creation`/`commit_existing`, and the non-semantic branches
        # (non-`.md` targets, Evidence appends, `kind="dir"`) acquire the
        # mutation boundary in the leaf itself (`create_file.py`,
        # `append_to_file.py`, `create_directory.py`).
        # `move`/`delete`/`recover`/`list`/`trash-list` still rely entirely on
        # this outer boundary (`commit_move`/`commit_recovery` are out of
        # scope for this change), so only the file-write operations narrow —
        # never the whole command.
        narrow_tier2_file_commit = (
            command.name == "manage_memory_file"
            and kwargs.get("operation", "list") in {"create", "append"}
            and not os.environ.get("EXOMEM_WIDE_MUTATION_BOUNDARY")
        )
        # `capture_source` carries two lanes under one name. Its file lane
        # stages client handles first -- up to eight network fetches against a
        # batch deadline -- and its leaf takes the mutation boundary itself,
        # once per committed artifact (`client_artifacts.capture_source_artifacts`),
        # exactly as `preserve_artifacts` does. Its text lane routes to `add`,
        # which guards nothing and depends on this outer boundary. So the name
        # cannot join `_NARROW_BOUNDARY_COMMANDS`: narrowing it wholesale would
        # leave a text capture unguarded, and leaving it out holds the vault
        # lock across every fetch and blocks all other writers for the
        # duration. The boundary follows the invocation, not the command.
        narrow_source_artifact_commit = (
            command.name == "capture_source"
            and bool(kwargs.get("files"))
            and not os.environ.get("EXOMEM_WIDE_MUTATION_BOUNDARY")
        )
        narrow_relation_registry_commit = (
            command.name == "schema_memory"
            and kwargs.get("subject", "contract") == "relations"
            and kwargs.get("operation") == "save-relations"
            and not os.environ.get("EXOMEM_WIDE_MUTATION_BOUNDARY")
        )
        # `maintain_memory(mode="tag-variants")` plans over the tag catalogue
        # and every candidate page before it writes; its leaf takes the
        # mutation boundary itself around only the batch re-verification and
        # commit (`tag_variants.apply`). Every other maintenance mode still
        # relies on this outer boundary.
        narrow_tag_variant_commit = (
            command.name == "maintain_memory"
            and kwargs.get("mode") == "tag-variants"
            and not os.environ.get("EXOMEM_WIDE_MUTATION_BOUNDARY")
        )
        from . import maintenance_jobs

        # A maintenance job's start only claims a row and launches a thread, and its
        # cancel only flags one; neither touches the vault. A chunked job's own dispatch
        # (`plan_id` and `dry_run=False`) holds the boundary per chunk in its leaf, so
        # a job can start while another writer holds the boundary. A whole-run job's
        # dispatch keeps the wide boundary (`maintenance_jobs.WHOLE_RUN_MODES`).
        # `EXOMEM_WIDE_MUTATION_BOUNDARY` widens a job start like every other narrow row.
        # It never widens a cancel: that must stay able to stop a job that holds the
        # wide boundary, so it needs no switch to be safe.
        narrow_maintenance_job_cancel = (
            command.name == "maintain_memory" and kwargs.get("mode") == maintenance_jobs.MODE_CANCEL
        )
        narrow_maintenance_job = (
            command.name == "maintain_memory"
            and maintenance_jobs.narrows_boundary(kwargs, getattr(capabilities_module.current_active_surface(), "surface", None))
            and not os.environ.get("EXOMEM_WIDE_MUTATION_BOUNDARY")
        )
        # A curation proposal prepares read-only, then seals; `seal_proposal` takes
        # the mutation boundary itself and re-checks every binding under it, so
        # preparation can run without the lock. Apply, resume and compensation
        # write step by step and keep the wide boundary.
        narrow_curation_proposal = (
            command.name == "maintain_memory"
            and kwargs.get("mode") == "curation"
            and kwargs.get("curation_action") == "propose"
            and not os.environ.get("EXOMEM_WIDE_MUTATION_BOUNDARY")
        )
        # Only record owns a short guard around fresh admission, revision
        # selection and ledger binding; the other episode actions still rely
        # on this command boundary. The action is a closed protocol enum.
        narrow_episode_record = (
            command.name == "episode_memory"
            and kwargs.get("action") == "record"
            and not os.environ.get("EXOMEM_WIDE_MUTATION_BOUNDARY")
        )
        narrow_boundary = (
            narrow_media_commit
            or narrow_tier2_file_commit
            or narrow_source_artifact_commit
            or narrow_relation_registry_commit
            or narrow_tag_variant_commit
            or narrow_maintenance_job_cancel
            or narrow_maintenance_job
            or narrow_curation_proposal
            or narrow_episode_record
            or (
                command.name in _NARROW_BOUNDARY_COMMANDS
                and not os.environ.get("EXOMEM_WIDE_MUTATION_BOUNDARY")
            )
        )
        # One guard, two possible seams. The handoff clause of the predicate is
        # a property of the *result*, so the choice is made when the guard is
        # reached rather than when it is bound -- and recorded, so a guard that
        # already ran before persistence (finalizing and thereby stripping the
        # handoff it was selected by) is not run a second time after it.
        guard_ran_before_persistence = False

        def finish_before_terminal_persistence(result: Any) -> Any:
            nonlocal guard_ran_before_persistence
            if not joins_unbounded_graph(result):
                # The guard used to strip the canonical checkpoint binding here;
                # it now runs after persistence, so strip it at this seam rather
                # than persisting an internal key into the completed row. The
                # *canonical* row keeps it, which is what the resume path reads.
                if isinstance(result, Mapping) and "_graph_sync_checkpoint" in result:
                    return {
                        key: value
                        for key, value in result.items()
                        if key != "_graph_sync_checkpoint"
                    }
                return result
            guard_ran_before_persistence = True
            return finish_fast_ack_and_graph(result)

        def finish_after_terminal_persistence(result: Any) -> Any:
            if post_terminal_housekeeping:
                _drain_post_terminal_housekeeping(post_terminal_housekeeping)
            if post_terminal_fanout:
                # The leaf's manager context has ended, but these callbacks
                # still belong to this invocation. Graph registrations must
                # use the same state root as the finalizer below.
                manager_token = _ACTIVE_LEASE_MANAGER.set(self)
                try:
                    reports, drain_failed = _drain_post_terminal_fanout(
                        post_terminal_fanout
                    )
                finally:
                    _ACTIVE_LEASE_MANAGER.reset(manager_token)
                result = _with_post_terminal_fanout_acknowledgement(
                    result, reports, drain_failed=drain_failed
                )
            if guard_ran_before_persistence:
                return result
            return finish_fast_ack_and_graph(result)

        wide_hold: dict[str, Any] = {}

        try:
            @contextmanager
            def operation_guard() -> Iterator[None]:
                # Wide guards outlive invoke_leaf; retain the session until
                # their outer hold has handed off its committed receipts.
                token = _ACTIVE_FAST_ACK_SESSION.set(fast_ack_session)
                try:
                    guard = (
                        self.writer_authority_guard(
                            vault_root=mutation_subject,
                            session_open_admission=session_open_admission,
                        )
                        if narrow_boundary
                        else self.mutation_guard(
                            mutation_subject,
                            request_id=request_id,
                            operation=command.name,
                            # A whole-run job's dispatch: a blocked writer learns that a job holds the boundary.
                            holder_kind="maintenance-job" if maintenance_jobs.current_run_id() else "command",
                            session_open_admission=session_open_admission,
                        )
                    )
                    try:
                        with guard:
                            yield
                    finally:
                        if not narrow_boundary:
                            # Post-terminal housekeeping takes later, shorter holds that
                            # would overwrite this one before the journal row is written.
                            wide_hold["timing"] = last_mutation_timing()
                finally:
                    _ACTIVE_FAST_ACK_SESSION.reset(token)

            def run_idempotent() -> Any:
                return self.idempotency.run(
                    key,
                    digest,
                    invoke_leaf,
                    expires_after=expires_after,
                    on_replay=on_replay,
                    operation_guard=operation_guard,
                    commit_observed=lambda: commit_state["observed"],
                    after_canonical_persisted=persist_graph_sync_progress,
                    after_operation_guard=finish_before_terminal_persistence,
                    after_terminal_acknowledgement=finish_after_terminal_persistence,
                    # Whether derived work is *owed*, not whether a hook is bound
                    # and not whether a session exists: the session is built before
                    # the leaf runs, from a feature flag and a vault root, so it
                    # cannot know whether a batch will be registered. A commit that
                    # registers none owes no component and must not be stamped
                    # pending. Evaluated after the leaf, where the batches are known.
                    acknowledgement_expected=lambda: (
                        bool(post_terminal_fanout)
                        or fast_ack_session is not None
                        and bool(fast_ack_session.batches)
                    ),
                    resume_canonically_committed=resume_graph_sync,
                    commit_evidence=exact_commit_evidence,
                    legacy_graph_pending_proof=legacy_graph_pending_proof,
                )

            from .command_surface import OPEN_WORLD_OPS

            result = self._run_absorbing_capture_contention(
                command,
                run_idempotent,
                key=key,
                commit_state=commit_state,
                request_id=request_id,
                wait_started=wait_started,
                wait_deadline=wait_deadline,
                # A narrow leaf meets the busy only after its own preparation. When
                # that preparation fetched caller-supplied temporary handles, the
                # server cannot fetch them again; only the caller can retry.
                rerun_safe=not (narrow_boundary and command.name in OPEN_WORLD_OPS),
            )
            if (
                vocabulary_replay_terminal is not None
                and vocabulary_binding is not None
                and isinstance(result, Mapping)
                and result.get("state") == "committed"
            ):
                try:
                    from . import vocabulary_application

                    vocabulary_application.commit(
                        receipt_vault_root,
                        vocabulary_binding,
                        vocabulary_application._writer_terminal(result),
                    )
                except Exception:  # noqa: BLE001 - canonical replay must remain successful
                    try:
                        from .vocabulary_state import VocabularyState

                        VocabularyState(receipt_vault_root).record_application_uncertain(
                            vocabulary_binding.item, operation_id=vocabulary_binding.operation_id
                        )
                    except Exception:  # noqa: BLE001 - correlation recovery is advisory
                        pass
        except BaseException as error:
            if isinstance(error, OpError):
                if error.code == "MUTATION_BUSY":
                    error.details.update(status="retryable", committed=False)
                    # `_mutation_busy()` (mutation_lock.py) already computed a
                    # load-aware hint; only fill in the static floor when
                    # nothing more specific was set.
                    error.details.setdefault("retry_after_ms", 750)
                elif error.code == "MUTATION_ACKNOWLEDGEMENT_PENDING":
                    error.details.update(status="uncertain", committed=None)
                error.details.update(request_id=request_id, receipt_id=receipt)
                error.details.pop("idempotency_key", None)
                if effective_public_idempotency_key is not None:
                    error.details["idempotency_key"] = effective_public_idempotency_key
            # The request thread attempts a handed-back write again and journals only its final refusal.
            handed_off = store_attempt is not None and error is store_attempt.handed_off
            _log_mutation_event(
                "interrupted",
                level=logging.DEBUG if handed_off else logging.WARNING,
                request_id=request_id,
                command=command.name,
                receipt=receipt or "none",
                error=type(error).__name__,
            )
            journal_refusal = partial(
                self._record_mutation_journal,
                request_id=request_id,
                command=command.name,
                receipt=receipt,
                outcome="failed",
                # `OpError` carries a real code directly; a leaf-contract
                # `ValueError` ("CODE: message") encodes one in its string
                # form (issue #553 — the journal was recording the Python
                # exception class name for these, losing the refusal
                # classification entirely). Anything that doesn't match
                # either structured shape keeps its class name so a
                # genuinely unexpected exception stays visible as a bug
                # rather than being laundered into a plausible-looking code.
                error_code=leaf_contract_code(error) or type(error).__name__,
                duration_ms=round((time.perf_counter() - t_start) * 1000, 2),
                scope=implicit_idempotency_scope or idempotency_principal_scope,
                targets=[str(mutation_subject)],
                wide_hold=wide_hold,
            )
            if handed_off:
                store_attempt.deferred_refusal = journal_refusal
            else:
                journal_refusal()
                # The refusal leaves invoke here, so this is where a busy becomes visible to the caller.
                if isinstance(error, OpError) and error.code == "MUTATION_BUSY":
                    _count_busy_once(error)
                else:
                    mutation_journal.claim_refusal(error)
            raise
        _log_mutation_event(
            "returned",
            request_id=request_id,
            command=command.name,
            receipt=receipt or "none",
        )
        self._record_mutation_journal(
            request_id=request_id,
            command=command.name,
            receipt=receipt,
            outcome=(
                "replayed"
                if isinstance(result, Mapping)
                and result.get("status") == "replayed"
                and result.get("mutated") is False
                else "committed"
            ),
            error_code=None,
            duration_ms=round((time.perf_counter() - t_start) * 1000, 2),
            scope=implicit_idempotency_scope or idempotency_principal_scope,
            targets=[str(mutation_subject)],
            wide_hold=wide_hold,
        )
        # Reclassify and its revert relocate through `move_file`, so they rewrite
        # the same withheld linking pages a move does.
        if receipt_vault_root is not None and (
            command.name == "move_file"
            or (
                command.name == "manage_memory_file"
                and kwargs.get("operation")  # nosemgrep: ep-word-membership -- closed manage_memory_file operation selectors that relocate pages.
                in {"move", "reclassify", "revert-reclassification"}
            )
        ):
            from . import move_file as move_file_module

            result = move_file_module.restricted_mover_terminal(
                receipt_vault_root,
                result,
                committed=lambda leaf: committed_terminal(
                    leaf,
                    request_id=request_id,
                    receipt_id=receipt,
                    idempotency_key=effective_public_idempotency_key,
                ),
            )
        projected = project_terminal(result, response_detail)
        from .governance import scrubber as governance_scrubber

        return governance_scrubber._trusted_issuance_projection(
            command.name,
            kwargs,
            result,
            projected,
        )

    def _record_mutation_journal(
        self,
        *,
        request_id: str,
        command: str,
        receipt: str | None,
        outcome: str,
        error_code: str | None,
        duration_ms: float | None,
        scope: str | None,
        targets: list[str],
        wide_hold: Mapping[str, Any] | None = None,
        boundary_timing: tuple[float | None, float | None] | None = None,
    ) -> None:
        """Best-effort mutation-journal write. Never raises.

        `boundary_timing` is the `(wait_ms, hold_ms)` the row reports. A refusal that
        never held the boundary passes its own, so the row cannot borrow an earlier
        hold from this context; `None` reads the hold this context just finished.
        """
        # A retry of a parked upload session that refuses for the park's own reason adds no row.
        if outcome == "failed" and mutation_journal.repeats_parked_refusal(error_code):
            return
        try:
            from .mutation_journal import record_mutation

            if boundary_timing is None:
                timing = (
                    wide_hold["timing"]
                    if wide_hold is not None and "timing" in wide_hold
                    else last_mutation_timing()
                )
                boundary_timing = (
                    (timing.get("wait_ms"), timing.get("hold_ms")) if timing else (None, None)
                )
            lease_role = (
                "standalone"
                if not self.config.enabled
                else ("writer" if self._fencing_token is not None else "follower")
            )
            scope_kind = scope.split(":", 1)[0] if scope else None
            record_mutation(
                request_id=request_id,
                tool=command,
                command=command,
                receipt_id=receipt,
                outcome=outcome,
                error_code=error_code,
                duration_ms=duration_ms,
                boundary_wait_ms=boundary_timing[0],
                boundary_hold_ms=boundary_timing[1],
                lease_role=lease_role,
                fencing_token=self._fencing_token,
                replica_id=self.config.replica_id,
                scope=scope_kind,
                targets=targets,
            )
        except Exception:  # noqa: BLE001 - the journal must never break a mutation
            pass

    def _run_absorbing_capture_contention(
        self,
        command: Any,
        run: Callable[[], Any],
        *,
        key: str | None,
        commit_state: Mapping[str, bool],
        request_id: str,
        wait_started: float | None = None,
        wait_deadline: float | None = None,
        rerun_safe: bool = True,
    ) -> Any:
        """Run one keyed write, waiting out an ordinary busy boundary.

        Ordinary contention (a holder still inside its allowance) never reaches
        the client: the same keyed write is attempted again, in arrival order,
        until it commits or the bounded wait ends. Only a holder already past
        its allowance, or a wait that ran out, is refused -- with a cause the
        caller can act on. A retry is safe because a busy refusal is raised
        before the write commits and releases its idempotency claim, so the
        key commits exactly once; anything that observed a commit is never
        re-run. A write is attempted again only while the request budget still
        fits another attempt as long as the last one.
        """
        eligible = key is not None and rerun_safe and _writes_absorb_contention()
        store_attempt = _ACTIVE_STORE_ATTEMPT.get()
        if store_attempt is not None:
            return self._run_store_attempt(
                store_attempt, run, eligible=eligible, commit_state=commit_state
            )
        if not eligible:
            # An ineligible nested invocation must not borrow its parent's
            # allowance or admission callback.
            token = _ACTIVE_CAPTURE_PREPARATION_RETRY.set(None)
            try:
                return run()
            finally:
                _ACTIVE_CAPTURE_PREPARATION_RETRY.reset(token)
        # The request's wait clock when `invoke` started it before a warm-up wait.
        wait = _ContentionWait(
            self._capture_queue,
            _capture_wait_seconds(self._mutation_timeout_seconds),
            started=wait_started,
            deadline=wait_deadline,
        )
        preparation_retry = _CapturePreparationRetry(wait.deadline, wait.admit_preparation_retry)

        def attempt() -> Any:
            token = _ACTIVE_CAPTURE_PREPARATION_RETRY.set(preparation_retry)
            try:
                with mutation_lock.absorbing_busy():
                    return run()
            finally:
                _ACTIVE_CAPTURE_PREPARATION_RETRY.reset(token)

        def absorbable(error: OpError) -> bool:
            if error.code != "MUTATION_BUSY":
                return False
            if commit_state["observed"] or error.details.get("committed") is not False:
                _count_busy_once(error)
                return False
            return True

        return self._absorb_contention(
            command, wait, attempt, absorbable=absorbable, request_id=request_id, join_on_arrival=True
        )

    def _absorb_contention(
        self,
        command: Any,
        wait: _ContentionWait,
        attempt: Callable[[], Any],
        *,
        absorbable: Callable[[OpError], bool],
        request_id: str,
        join_on_arrival: bool,
        on_refuse: Callable[[OpError], None] | None = None,
    ) -> Any:
        """Attempt a write until it returns, waiting in line after each refusal `absorbable` admits.

        Only the calling thread waits. `absorbable` admits a refusal raised before anything
        committed; every other error propagates on first sight.
        """
        # The last refusal this write absorbed: a wait that runs out in line
        # still names the holder that kept it waiting.
        last_busy: OpError | None = None

        def holderless_busy() -> OpError:
            return _mutation_busy(None, wait_ms=wait.waited_ms())

        def refuse(error: OpError, cause: str, remediation: str) -> OpError:
            if on_refuse is not None:
                on_refuse(error)
            if error.code != "MUTATION_BUSY":
                # A handed-back store or preparation refusal keeps its own shape and cause.
                mutation_journal.claim_refusal(error)
                return error
            # The one place a busy from this loop becomes client-visible.
            _count_busy_once(error)
            # waited_ms covers the whole request; one attempt's wait_ms would contradict it.
            error.details.pop("wait_ms", None)
            error.details.update(cause=cause, waited_ms=wait.waited_ms(), absorbed_attempts=wait.absorbed)
            error.remediation = remediation
            _log_mutation_event(
                "contention_refused",
                level=logging.WARNING,
                request_id=request_id,
                command=command.name,
                cause=cause,
                waited_ms=error.details["waited_ms"],
            )
            return error

        try:
            if join_on_arrival and wait.queue.waiting():
                # Arriving behind others means parking this thread as they do.
                if not wait.take_slot():
                    raise refuse(
                        holderless_busy(),
                        "capture_waiters_full",
                        _CAPTURE_WAITERS_FULL_REMEDIATION,
                    )
                wait.join_line()
            while True:
                if wait.ticket is not None and not wait.wait_turn():
                    raise refuse(
                        last_busy or holderless_busy(),
                        "capture_wait_exhausted",
                        _CAPTURE_WAIT_EXHAUSTED_REMEDIATION,
                    )
                if last_busy is not None and not wait.fits_another_attempt():
                    # Waiting in line can spend the budget the check below saw.
                    raise refuse(
                        last_busy, "capture_wait_exhausted", _CAPTURE_WAIT_EXHAUSTED_REMEDIATION
                    )
                attempt_started = time.monotonic()
                try:
                    result = attempt()
                except OpError as error:
                    if not absorbable(error):
                        raise
                    holder = error.details.get("holder")
                    if isinstance(holder, Mapping) and holder.get("overdue") is True:
                        raise refuse(
                            error, "holder_overdue", _CAPTURE_HOLDER_OVERDUE_REMEDIATION
                        ) from None
                    remaining = wait.deadline - time.monotonic()
                    wait.last_attempt_seconds = time.monotonic() - attempt_started
                    if remaining <= 0 or not wait.fits_another_attempt():
                        raise refuse(
                            error,
                            "capture_wait_exhausted",
                            _CAPTURE_WAIT_EXHAUSTED_REMEDIATION,
                        ) from None
                    if not wait.take_slot():
                        raise refuse(
                            error,
                            "capture_waiters_full",
                            _CAPTURE_WAITERS_FULL_REMEDIATION,
                        ) from None
                    wait.absorbed += 1
                    last_busy = error
                    wait.join_line()
                    time.sleep(min(_CAPTURE_RETRY_FLOOR_SECONDS, remaining))
                    continue
                if wait.absorbed:
                    _log_mutation_event(
                        "contention_absorbed",
                        request_id=request_id,
                        command=command.name,
                        attempts=wait.absorbed + 1,
                        waited_ms=wait.waited_ms(),
                    )
                return result
        finally:
            wait.close()

    def _run_store_attempt(
        self,
        attempt: _StoreAttempt,
        run: Callable[[], Any],
        *,
        eligible: bool,
        commit_state: Mapping[str, bool],
    ) -> Any:
        """Make one attempt for a request thread that waits out contention itself.

        It never waits in line or between attempts, so the store thread serves other requests
        meanwhile. A refusal raised before anything committed is handed back for another attempt.
        """
        queue = self._capture_queue

        def admit_preparation_retry() -> bool:
            # Retry in place only while no other write waits; otherwise wait in line on the request thread.
            if time.monotonic() >= attempt.deadline:
                return False
            if attempt.holds_ticket or not queue.waiting():
                return True
            attempt.preparation_retry_wanted = True
            return False

        if eligible and not attempt.holds_ticket and queue.waiting():
            # Arriving behind waiting writes: join their line on the request thread without running.
            with mutation_lock.absorbing_busy():
                attempt.handed_off = _mutation_busy(None, wait_ms=0)
            raise attempt.handed_off
        # A nested invocation inside this one is not the request thread's attempt.
        nested_token = _ACTIVE_STORE_ATTEMPT.set(None)
        retry_token = _ACTIVE_CAPTURE_PREPARATION_RETRY.set(
            _CapturePreparationRetry(attempt.deadline, admit_preparation_retry) if eligible else None
        )
        try:
            with mutation_lock.absorbing_busy() if eligible else nullcontext():
                return run()
        except OpError as error:
            before_commit = not commit_state["observed"]
            if error.code == "MUTATION_BUSY":
                if before_commit and eligible and error.details.get("committed") is False:
                    attempt.handed_off = error
                elif eligible:
                    _count_busy_once(error)
            elif error.code == "COLLECTION_STORE_BUSY":
                # A store handoff refused the guard before the leaf ran, so nothing ran, keyed or not.
                if not commit_state["leaf_started"]:
                    attempt.handed_off = error
            elif error.code == "REFERENCE_PREPARATION_UNAVAILABLE":
                if before_commit and attempt.preparation_retry_wanted:
                    attempt.handed_off = error
            raise
        finally:
            _ACTIVE_CAPTURE_PREPARATION_RETRY.reset(retry_token)
            _ACTIVE_STORE_ATTEMPT.reset(nested_token)

    def _attempt_store_write(
        self,
        func: Callable[..., Any],
        command: Any,
        injected: tuple[Any, ...],
        kwargs: dict[str, Any],
        routed: Any,
    ) -> Any:
        """Run a write that may route to a collection store thread, waiting out contention here.

        The store thread makes each attempt once and hands back a refusal raised before anything
        committed (`_run_store_attempt`), so it never parks while this write waits its turn.
        Routing proves ownership under the boundary and changes nothing, so its busy is waited out too.
        `routed` is the first routing's store server, or the busy refusal it raised.
        """
        from .collection_store import runtime as store_runtime

        # Every attempt is one request: one id in the logs and in the one journal row.
        request_id = kwargs.get("mutation_request_id") or str(uuid.uuid4())
        call_kwargs = {**kwargs, "mutation_request_id": request_id}
        wait = _ContentionWait(
            self._capture_queue, _capture_wait_seconds(self._mutation_timeout_seconds)
        )
        routing = received = False
        current: _StoreAttempt | None = None
        refusal_journal: list[Callable[[], None]] = []

        def extend(completion_deadline: float) -> float | None:
            # The store has not taken the write, so it never ran: it keeps its place while its wait allows.
            if not wait.take_slot():
                return None
            return min(
                wait.deadline,
                completion_deadline - max(wait.last_attempt_seconds, self._mutation_timeout_seconds),
            )

        def run() -> Any:
            return func(command, *injected, **call_kwargs)

        def attempt() -> Any:
            nonlocal routing, received, current
            current = _StoreAttempt(
                holds_ticket=wait.ticket is not None,
                resubmission=received,
                deadline=wait.deadline,
            )
            routing = True
            if wait.absorbed:
                with mutation_lock.absorbing_busy():
                    server = store_runtime.route(command.name, injected[0], kwargs)
            elif isinstance(routed, OpError):
                raise routed
            else:
                server = routed
            routing = False
            token = _ACTIVE_STORE_ATTEMPT.set(current)
            try:
                if server is None:
                    return run()
                return server.call(
                    run,
                    wake_maintenance=True,
                    read_only=False,
                    extend=extend,
                    # The queue refuses a write it never took, or still holds, before `invoke` can journal it.
                    on_refusal=partial(_record_store_queue_refusal, command, call_kwargs, server.root),
                )
            finally:
                _ACTIVE_STORE_ATTEMPT.reset(token)
                received = received or current.received

        def absorbable(error: OpError) -> bool:
            if routing:
                if error.code != "MUTATION_BUSY":
                    return False
                # Routing refuses before `invoke` runs, so this seam journals the refusal the caller finally sees.
                refusal_journal[:] = [lambda: _record_store_queue_refusal(
                    command, call_kwargs, injected[0], error, wait.waited_ms() / 1000)]
                return True
            if current is None or error is not current.handed_off:
                return False
            # The journal row, if any, of the refusal the caller would see now.
            refusal_journal[:] = [] if current.deferred_refusal is None else [current.deferred_refusal]
            return True

        def journal_refusal(_error: OpError) -> None:
            for journal in refusal_journal:
                journal()

        return self._absorb_contention(
            command,
            wait,
            attempt,
            absorbable=absorbable,
            request_id=request_id,
            join_on_arrival=False,
            on_refuse=journal_refusal,
        )

    def _mutation_subject(self, injected: tuple[Any, ...]) -> os.PathLike[str] | str:
        if injected and isinstance(injected[0], os.PathLike):
            return injected[0]
        if self.config.vault_id:
            return self.config.vault_id
        return "standalone"

    def _receipt_vault_root(self, injected: tuple[Any, ...]) -> Path | None:
        """Resolve receipt storage only from an explicit vault authority."""
        candidate = injected[0] if injected else None
        if isinstance(candidate, (str, os.PathLike)):
            root = Path(candidate)
            if root.is_absolute():
                return root.resolve(strict=False)
        configured = os.environ.get("EXOMEM_VAULT_PATH", "").strip()
        if configured:
            root = Path(configured)
            if root.is_absolute():
                return root.resolve(strict=False)
        return None

    def validate_fencing_token(self, fencing_token: int) -> None:
        """Fail closed unless the command's token is still locally and remotely current."""
        with self._lock:
            if self._fencing_token != fencing_token:
                self._raise_fenced(fencing_token)
        assert self.client is not None
        record = self.client.status()
        with self._lock:
            still_current = self._fencing_token == fencing_token
            coordinator_current = (
                record.holder == self.config.replica_id and record.fencing_token == fencing_token
            )
            if still_current and coordinator_current:
                return
            if self._fencing_token == fencing_token:
                self._fencing_token = None
                self._expires_at = None
        self._raise_fenced(fencing_token)

    @staticmethod
    def _raise_fenced(fencing_token: int) -> None:
        raise OpError(
            "WRITER_FENCED",
            f"writer lease fencing token {fencing_token} is no longer current",
        )

    def status(self, vault_or_cell: os.PathLike[str] | str | None = None) -> dict[str, Any]:
        # Without a boundary identity there is nothing to probe: the answer can
        # only cover this process, so it is reported as `unknown`, never as a
        # verified `free`.
        mutation_boundary = (
            VaultMutationCoordinator(
                self.config.state_dir,
                vault_or_cell,
                timeout_seconds=self._mutation_timeout_seconds,
                poll_interval_seconds=self._mutation_poll_interval_seconds,
            ).snapshot()
            if vault_or_cell is not None
            else process_local_mutation_boundary()
        )
        renewer_alive = self._renewer is not None and self._renewer.is_alive()
        last_renew_age_seconds = (
            round(time.monotonic() - self._last_renew_monotonic, 3)
            if self._last_renew_monotonic is not None
            else None
        )
        base: dict[str, Any] = {
            "enabled": self.config.enabled,
            "role": "standalone" if not self.config.enabled else "unknown",
            "replica_id": self.config.replica_id,
            "holder": None,
            "expires_at": None,
            "fencing_token": None,
            "coordinator_healthy": True if not self.config.enabled else False,
            "mutation_boundary": mutation_boundary,
            "ttl_remaining_seconds": None,
            "renewer_alive": renewer_alive,
            "last_renew_age_seconds": last_renew_age_seconds,
            "last_coordinator_error": None,
            "idempotency": self.idempotency.status_summary(),
        }
        if vault_or_cell is not None:
            try:
                from . import epistemic_graph, graph_sync

                vault_root = Path(vault_or_cell)
                graph_status = graph_sync.status(vault_root)
                if graph_status["state"] == "current":
                    # A status probe reports; it never pays the O(corpus)
                    # source-bytes proof. It reads the remembered verdict, or
                    # says that nothing has proved the sidecar yet (#1454).
                    availability = epistemic_graph.EpistemicGraphIndex(
                        vault_root,
                        mutation_coordinator=self._mutation_coordinator_for(vault_root),
                    ).availability_state()
                    if availability != "available":
                        graph_status = {
                            "state": availability,
                            "generation": graph_status["generation"],
                        }
                base["graph_sync"] = graph_status
            except Exception:  # noqa: BLE001 - coordination diagnostics stay bounded
                base["graph_sync"] = {"state": "unavailable", "generation": 0}
        if self._collection_store is not None:
            try:
                base["collection_store"] = self._collection_store.status()
            except Exception:  # noqa: BLE001 - a status probe reports, never raises
                base["collection_store"] = {"status": "unavailable"}
        if not self.config.enabled:
            return base
        assert self.client is not None
        try:
            record = self.client.status()
        except OpError as error:
            # Record the fault instead of only ever reporting
            # `coordinator_healthy: False` with no further detail.
            self._record_coordinator_error(error.code)
            current_error = self._last_coordinator_error
            assert current_error is not None
            code, at = current_error
            base["last_coordinator_error"] = {
                "code": code,
                "age_seconds": round(time.monotonic() - at, 3),
            }
            return base
        ttl_remaining_seconds = (
            round(record.expires_at - time.time(), 3) if record.expires_at is not None else None
        )
        base.update(
            role="writer" if record.holder == self.config.replica_id else "follower",
            holder=record.holder,
            expires_at=record.expires_at,
            fencing_token=record.fencing_token,
            coordinator_healthy=True,
            ttl_remaining_seconds=ttl_remaining_seconds,
        )
        if self._last_coordinator_error is not None:
            code, at = self._last_coordinator_error
            base["last_coordinator_error"] = {
                "code": code,
                "age_seconds": round(time.monotonic() - at, 3),
            }
        return base

    def start_renewer(self) -> None:
        if not self.config.enabled or self._renewer is not None:
            return
        self._renewer = threading.Thread(
            target=self._renew_loop, name="exomem-writer-lease", daemon=True
        )
        self._renewer.start()

    @contextmanager
    def _collection_store_checkout(self):
        thread = threading.get_ident()
        with self._lock:
            if self._stop.is_set() or self._store_refused():
                from .collection_store import connection as store_connection

                error = store_connection.busy("collection store handoff is in progress")
                attempt = _ACTIVE_STORE_ATTEMPT.get()
                if attempt is not None and not (self._stop.is_set() or self._store_closed):
                    # A handoff refuses only a thread that is not already a store borrower, and the
                    # store thread borrows for the whole request once `_serve` checks out. So a
                    # refusal that sees an attempt came before the request's work ran.
                    attempt.handed_off = error
                raise error
            self._store_bound = True
            self._store_borrowers[thread] = self._store_borrowers.get(thread, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                remaining = self._store_borrowers[thread] - 1
                if remaining:
                    self._store_borrowers[thread] = remaining
                else:
                    del self._store_borrowers[thread]
                self._store_condition.notify_all()

    def _renew_collection_store(self, token: int, *, due_only=False) -> None:
        if not self.config.enabled:
            return
        with self._report_lock:
            with self._lock:
                if self._fencing_token != token:
                    self._raise_fenced(token)
                if (due_only and self._last_renew_monotonic is not None
                        and time.monotonic() - self._last_renew_monotonic
                        < self._renew_interval_seconds()):
                    return
            head = self._collection_store.report_head(token)
            record = self.client.renew(
                token, collection_store_capability=COLLECTION_STORE_CAPABILITY,
                collection_store_head=head,
            )
            with self._lock:
                if self._fencing_token != token:
                    self._raise_fenced(token)
                if (not record.granted or record.holder != self.config.replica_id
                        or record.fencing_token != token):
                    self._fencing_token = None
                    self._expires_at = None
                    self._raise_fenced(token)
                self._note_grant(record.expires_at)
                self._last_renew_monotonic = time.monotonic()
                self._record_lease_op("renew", "granted")

    def _release_unadmitted_store(self, token, *, closing):
        """A token with no store commits to publish has no head to flush and must not hold the lease.

        Releasing without a head keeps the coordinator's recorded head, so the host
        that owns those commits can take the lease back for every write.
        """
        runtime = self._collection_store
        with ExitStack() as boundary:
            try:
                boundary.enter_context(self._mutation_coordinator_for(runtime.root).hold(
                    timeout_seconds=0, operation="collection_store_release",
                ))
            except OpError as error:
                if error.code != "MUTATION_BUSY":
                    raise
                return False
            if not boundary.enter_context(self._replica_grant_claim(exclusive=True)):
                return False
            boundary.enter_context(self._report_lock)
            with self._lock:
                if (self._fencing_token != token or self._store_handoff or self._store_borrowers
                        or self._active_mutations or not runtime._retire_handle()):
                    return False
                self._fencing_token = None
                self._expires_at = None
                self._store_closed = closing
                self._store_released = True
            self.client.release(token)
        self._record_lease_op("release", "ok")
        return True

    def _release_collection_store(self, token, *, deadline, cancelled=None, closing=False,
                                  release=True, drain_views=True):
        """Quiesce, flush the replica and hand the lease back with the flushed head.

        ``release=False`` is the synchronous portability-export flush: the same
        quiescence and publication, keeping the lease and returning the flushed head.
        """
        runtime = self._collection_store
        if not runtime.reporting_ready(token):
            if release and self.config.enabled and runtime.releases_without_head(token):
                return self._release_unadmitted_store(token, closing=closing)
            return False
        started = time.monotonic()
        with self._store_condition:
            # A coalesced publication finishes within its own deadline.
            while self._store_publishing and (remaining := deadline - time.monotonic()) > 0:
                self._store_condition.wait(min(0.05, remaining))
            if self._store_closed:
                return True
            if self._store_handoff:
                return False
            self._store_handoff = True
        try:
            # No boundary or manager lock is held while borrowers unwind.
            self._renew_collection_store(token)
            while True:
                if (time.monotonic() >= deadline or (cancelled is not None and cancelled())):
                    return False
                with self._store_condition:
                    if (not self._store_borrowers and not self._active_mutations
                            and not self._active_authority_guards):
                        break
                    self._store_condition.wait(min(0.05, max(0, deadline - time.monotonic())))
                self._renew_collection_store(token, due_only=True)
            mutation = self._mutation_coordinator_for(runtime.root)
            boundary_deadline = min(deadline, time.monotonic() + self._mutation_timeout_seconds)
            with ExitStack() as boundary:
                while True:
                    if (time.monotonic() >= boundary_deadline
                            or (cancelled is not None and cancelled())):
                        return False
                    self._renew_collection_store(token, due_only=True)
                    try:
                        boundary.enter_context(mutation.hold(
                            timeout_seconds=max(0, min(0.25, boundary_deadline - time.monotonic())),
                            operation="collection_store_release",
                        ))
                        break
                    except OpError as error:
                        if error.code != "MUTATION_BUSY":
                            raise
                if time.monotonic() >= deadline or (cancelled is not None and cancelled()):
                    return False
                # Mutation guards take the canonical boundary before their shared claim.
                # Try only: a peer preparing outside that boundary must be able to seal.
                if release and not boundary.enter_context(self._replica_grant_claim(exclusive=True)):
                    return False
                if self.config.enabled:
                    self.validate_fencing_token(token)
                head = runtime.flush(token, deadline=deadline, cancelled=cancelled,
                                     **({} if drain_views else {"drain_views": False}))
                if not release:
                    return head
                with self._report_lock:
                    if time.monotonic() >= deadline or (cancelled is not None and cancelled()):
                        return False
                    if self.config.enabled:
                        self.validate_fencing_token(token)
                    if runtime.sample_head() != head:
                        return False
                    if self.config.enabled:
                        record = self.client.release(token, collection_store_head=head)
                        if record.holder is not None or record.fencing_token != token:
                            return False
                    with self._lock:
                        self._fencing_token = None
                        self._expires_at = None
                        self._store_closed = closing
                        self._store_released = True
                        runtime._handed_off = True
                self._record_lease_op("release", "ok")
                # `closing` is only set by `close()`; every other release is the idle handoff.
                self._log_lease_event(
                    "lease_released", cause="shutdown" if closing else "idle",
                    commit_seq=head.commit_seq, duration_ms=round((time.monotonic() - started) * 1000))
                return True
        finally:
            with self._store_condition:
                self._store_handoff = False
                self._store_condition.notify_all()

    def _publish_collection_store(self, token, *, deadline):
        """A9 coalesced replica publication outside the write path, keeping the lease.

        The snapshot copy runs outside the vault-wide boundary while writers proceed;
        only its two brief steps (record the intent, then verify and swap) hold it,
        each taken at an idle instant. A write that arrives during a step waits for
        it. Returns the published head, or None when the store was busy and the
        publication should be retried.
        """
        with self._lock:
            if (self._fencing_token != token or self._store_handoff or self._store_closed
                    or self._store_borrowers or self._active_mutations):
                return None
        return self._collection_store.publish(token, deadline=deadline)

    @contextmanager
    def _store_publication_window(self, token, *, patience, cancelled=None):
        """One brief publication step under the vault-wide boundary, taken at an idle instant.

        It waits up to ``patience`` seconds for an instant with no store borrower or
        mutation in flight and a free boundary, never waiting on a writer. Arrivals then
        wait for the step (bounded) and a thread already holding a boundary passes it.
        Raises COLLECTION_STORE_BUSY when no idle instant came or ``cancelled`` fired,
        and COLLECTION_STORE_LEASE_REQUIRED once ``token`` no longer holds the lease.
        """
        from .collection_store.connection import CollectionStoreError

        mutation = self._mutation_coordinator_for(self._collection_store.root)
        until = time.monotonic() + patience
        with ExitStack() as held:
            while True:
                with self._store_condition:
                    if self._fencing_token != token or self._store_closed:
                        raise CollectionStoreError(
                            "COLLECTION_STORE_LEASE_REQUIRED", "publication authority was lost")
                    idle = not (self._store_handoff or self._store_borrowers or self._active_mutations)
                    if idle:
                        self._store_handoff = self._store_publishing = True
                    elif (remaining := until - time.monotonic()) > 0 and not (cancelled and cancelled()):
                        self._store_condition.wait(min(0.05, remaining))
                        continue
                    else:
                        raise CollectionStoreError("COLLECTION_STORE_BUSY", "no idle instant for publication")
                # Unwinding releases the boundary first, then lets arrivals through.
                held.callback(self._end_store_publication)
                try:
                    held.enter_context(mutation.hold(timeout_seconds=0.01, operation="collection_store_publish"))
                    break
                except BaseException as error:
                    held.pop_all().close()
                    if not isinstance(error, OpError) or error.code != "MUTATION_BUSY":
                        raise
                    if time.monotonic() >= until or (cancelled and cancelled()):
                        raise CollectionStoreError(
                            "COLLECTION_STORE_BUSY", "no idle instant for publication") from None
                    time.sleep(0.01)
            with self._store_condition:
                # A boundary holder that checked out before this step is unwinding.
                unwound = time.monotonic() + 1.0
                while self._store_borrowers:
                    if (remaining := unwound - time.monotonic()) <= 0:
                        raise CollectionStoreError("COLLECTION_STORE_BUSY", "a store borrower is unwinding")
                    self._store_condition.wait(min(0.05, remaining))
            yield

    def _end_store_publication(self):
        with self._store_condition:
            self._store_handoff = self._store_publishing = False
            self._store_condition.notify_all()

    def _attempt_preferred_reclaim(self) -> None:
        """Retry writer acquisition while this preferred replica is a follower.

        `start_server_lifecycle()` attempts acquisition once at startup and
        swallows the failure, reasoning that "mutations will retry
        authoritatively". Under the HA edge that is false: the edge routes
        mutation-capable requests to the current lease holder, so a follower is
        never sent the mutation that would trigger a retry. Without this the
        preferred replica loses one startup race and stays a follower for the
        entire process lifetime — observed as a 15-hour outage on 2026-07-20
        while reporting healthy and `takeover_eligible: true`.

        This cannot displace a live holder. The coordinator grants acquisition
        only when the existing lease is absent or expired, so a refused attempt
        is the normal steady state for a follower, not a fault worth logging.
        """
        if not self.config.preferred_writer:
            return
        try:
            self.ensure_writer(cause="reclaim")
        except OpError:
            return
        self._log_lease_event("lease_reclaimed")

    def _maybe_idle_release(self, token: int) -> bool:
        """Hand writer authority back when idle. Returns True if it released
        (the caller should skip renewing this tick).

        A preferred replica is exempt — without the exemption it would
        acquire, sit idle, release, and reclaim on the very next renew tick
        (via `_attempt_preferred_reclaim`), churning edge routing every
        interval for no benefit. Idle release exists to let a non-preferred
        holder (e.g. a laptop that is powered on but unused) hand back to the
        preferred replica on its own.
        """
        idle_seconds = self.config.idle_release_seconds
        if idle_seconds <= 0 or self.config.preferred_writer:
            return False
        assert self.client is not None
        with self._lock:
            if self._fencing_token != token or self._store_handoff or self._store_closed:
                return False
            if self._active_mutations != 0:
                return False
            if time.monotonic() - self._last_activity_monotonic < idle_seconds:
                return False
            runtime = self._collection_store
            if runtime is None and self._store_borrowers:
                return False
        if runtime is not None:
            try:
                return self._release_collection_store(
                    token, deadline=time.monotonic() + self._mutation_timeout_seconds
                )
            except Exception:  # noqa: BLE001 - idle handoff defers; never claims a flush
                logger.warning("collection store idle release remains pending", exc_info=True)
                return False
        with self._replica_grant_claim(exclusive=True) as available:
            if not available:
                return False
            with self._lock:
                if (self._fencing_token != token or self._store_handoff or self._store_closed
                        or self._active_mutations or self._store_borrowers
                        or time.monotonic() - self._last_activity_monotonic < idle_seconds):
                    return False
                # Clear local state BEFORE (and during) the release RPC, still
                # holding the lock: an `ensure_writer` arriving concurrently
                # blocks on this same lock and then acquires a fresh token — it
                # can never observe us as still the writer mid-release.
                self._fencing_token = None
                self._expires_at = None
                try:
                    self.client.release(token)
                    self._record_lease_op("idle_release", "ok")
                except OpError as error:
                    # Swallowed: the local token is already cleared, and the
                    # coordinator's own TTL expiry closes the gap within one TTL
                    # — a degraded handover, never split-brain.
                    self._record_coordinator_error(error.code)
                    self._record_lease_op("idle_release", "error")
                self._log_lease_event("lease_idle_released")
                return True

    def _renew_loop(
        self, *, stop: threading.Event | None = None, fencing_token: int | None = None
    ) -> None:
        interval = self._renew_interval_seconds()
        stop = self._stop if stop is None else stop
        while not stop.wait(interval):
            with self._lock:
                token = self._fencing_token
                if fencing_token is not None and (
                    token != fencing_token or self._collection_store is not None or self._stop.is_set()
                ):
                    return
            if self.client is None:
                continue
            if token is None:
                self._attempt_preferred_reclaim()
                continue
            if fencing_token is None and self._maybe_idle_release(token):
                continue
            try:
                if fencing_token is None and self._collection_store is not None:
                    self._renew_collection_store(token)
                    continue
                record = self.client.renew(token, **(
                    {"collection_store_capability": COLLECTION_STORE_CAPABILITY}
                    if self._renew_claims_store_capability() else {}
                ))
                with self._lock:
                    if self._fencing_token != token:
                        continue
                    if (record.granted and record.holder == self.config.replica_id
                            and record.fencing_token == token):
                        self._note_grant(record.expires_at)
                        self._last_renew_monotonic = time.monotonic()
                        self._record_lease_op("renew", "granted")
                    else:
                        self._fencing_token = None
                        self._expires_at = None
                        self._record_lease_op("renew", "rejected")
                        self._log_lease_event("lease_renew_rejected")
            except OpError as error:
                # Mutations still revalidate synchronously and fail closed.
                self._record_coordinator_error(error.code)
                self._record_lease_op("renew", "error")
                continue
            except Exception:  # noqa: BLE001 - unavailable store samples must not kill renewal
                if self._collection_store is None:
                    raise
                logger.warning("collection store head report unavailable", exc_info=True)
                self._record_lease_op("renew", "error")

    def close(self, *, deadline=None, cancelled=None, drain_views=True) -> None:
        if self._collection_store is not None:
            with self._lock:
                if self._store_closed:
                    return
                if self._store_released:
                    self._store_closed = True
                    self._stop.set()
                    return
                token = self._fencing_token if self.config.enabled else 0
            try:
                handed_off = token is not None and self._release_collection_store(
                    token, deadline=(time.monotonic() + self._mutation_timeout_seconds
                                     if deadline is None else deadline),
                    cancelled=cancelled, closing=True,
                    **({} if drain_views else {"drain_views": False}),
                )
            finally:
                # Keep custody until flush finishes, then let even a failed handoff's
                # lease expire. A surviving process must not fence the next writer forever.
                self._stop.set()
            if not handed_off:
                raise OpError("COLLECTION_STORE_FLUSH_PENDING", "replica handoff remains pending")
            return
        handoff = False
        try:
            with self._store_condition:
                if self._store_bound:
                    if self._store_closed:
                        return
                    if self._store_handoff or threading.get_ident() in self._store_borrowers:
                        self._stop.set()
                        raise OpError("COLLECTION_STORE_FLUSH_PENDING", "preview borrowers remain active")
                    self._store_handoff = handoff = True
                    deadline = (time.monotonic() + self._mutation_timeout_seconds
                                if deadline is None else deadline)
                    while True:
                        if time.monotonic() >= deadline or (cancelled is not None and cancelled()):
                            self._stop.set()
                            raise OpError("COLLECTION_STORE_FLUSH_PENDING", "preview borrowers remain active")
                        if not self._store_borrowers:
                            break
                        self._store_condition.wait(min(0.05, max(0, deadline - time.monotonic())))
                self._stop.set()
                # No grant or preview handoff remains, so shutdown needs no shared state.
                if self._fencing_token is None and not handoff:
                    return
            # Borrowers may need a command claim to finish; drain them before exclusion.
            with self._replica_grant_claim(exclusive=True) as available:
                if not available:
                    if self._store_bound:
                        raise OpError("COLLECTION_STORE_FLUSH_PENDING", "preview borrowers remain active")
                    return
                with self._lock:
                    if handoff:
                        self._store_closed = True
                    token = self._fencing_token
                    self._fencing_token = None
                if token is not None and self.client is not None:
                    try:
                        self.client.release(token)
                        self._record_lease_op("release", "ok")
                        self._log_lease_event("lease_released", cause="shutdown")
                    except OpError as error:
                        self._record_coordinator_error(error.code)
                        self._record_lease_op("release", "error")
        finally:
            if handoff:
                with self._store_condition:
                    self._store_handoff = False
                    self._store_condition.notify_all()


def validate_active_write_fence() -> None:
    """Revalidate the active command's lease token at a vault commit boundary."""
    active = _ACTIVE_WRITE_FENCE.get()
    if active is None:
        return
    manager, fencing_token = active
    manager.validate_fencing_token(fencing_token)


def validate_active_collection_file_paths(vault_root: Path, paths: Iterable[Path]) -> None:
    """Keep generic file effects outside store ownership while its producer is unavailable."""
    active = _ACTIVE_COLLECTION_FILE_WRITE.get()
    if active is None:
        return
    from .collection_store import authority

    manager, acquired, marker = active
    root = Path(vault_root).resolve()
    if acquired is not None:
        current = manager._collection_file_marker(acquired, vault_root=root)
        if current is None or marker is None or current != marker:
            raise OpError("WRITER_LEASE_REQUIRED", "current local store publication is unverified")
    authority.refuse_view_paths(
        root, (path.resolve().relative_to(root).as_posix() for path in paths
               if path.resolve().is_relative_to(root)), OpError, expected_marker=marker,
    )


_MANAGERS: dict[LeaseConfig, LeaseManager] = {}
_MANAGERS_LOCK = threading.Lock()


def get_manager() -> LeaseManager:
    config = LeaseConfig.from_env()
    with _MANAGERS_LOCK:
        manager = _MANAGERS.get(config)
        if manager is None:
            manager = LeaseManager(config)
            _MANAGERS[config] = manager
        return manager


def _commit_generation_path(state_dir: Path, vault_or_cell: os.PathLike[str] | str) -> Path:
    from .mutation_lock import canonical_mutation_identity

    digest = hashlib.sha256(canonical_mutation_identity(vault_or_cell).encode("utf-8")).hexdigest()[
        :20
    ]
    return Path(state_dir) / "commit-generations" / f"{digest}.txt"


def _reserved_identity_generation_path(
    state_dir: Path, vault_or_cell: os.PathLike[str] | str
) -> Path:
    digest = hashlib.sha256(canonical_mutation_identity(vault_or_cell).encode("utf-8")).hexdigest()[
        :20
    ]
    return Path(state_dir) / "reserved-identity-generations" / f"{digest}.txt"


def _read_reserved_identity_generation(
    state_dir: Path, vault_or_cell: os.PathLike[str] | str
) -> str:
    """Read the cross-process private-identity seqlock token.

    The caller holds the identity gate. Missing means no owner has entered yet;
    malformed or unreadable state fails closed instead of blessing a stale
    catalogue.
    """

    path = _reserved_identity_generation_path(state_dir, vault_or_cell)
    try:
        value = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return "0" * 32
    except (OSError, UnicodeError) as error:
        raise RuntimeError("reserved identity generation is unreadable") from error
    if re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise RuntimeError("reserved identity generation is malformed")
    return value


def _bump_reserved_identity_generation(
    state_dir: Path, vault_or_cell: os.PathLike[str] | str
) -> str:
    """Advance the token before an exact owner may change private identities."""

    path = _reserved_identity_generation_path(state_dir, vault_or_cell)
    token = secrets.token_hex(16)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(token, encoding="ascii")
        os.replace(temporary, path)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError("reserved identity generation could not advance") from error
    return token


def read_commit_generation(vault_or_cell: os.PathLike[str] | str) -> int | None:
    """Monotonic count of boundary-held mutations for this vault.

    Read outside the boundary at preflight and compared inside it to admit
    validity-stamp reuse without a corpus stat-walk. ``0`` when never bumped;
    ``None`` on any read error other than the file simply not existing —
    fail closed: an unreadable counter must disable reuse, never admit it.
    """
    try:
        path = _commit_generation_path(active_manager().config.state_dir, vault_or_cell)
    except Exception:  # noqa: BLE001 - unresolvable identity disables reuse
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip() or "0")
    except FileNotFoundError:
        return 0
    except Exception:  # noqa: BLE001 - unreadable counter disables reuse
        return None


def _bump_commit_generation(state_dir: Path, vault_or_cell: os.PathLike[str] | str) -> None:
    """Advance the counter; called while the mutation boundary is held."""
    try:
        path = _commit_generation_path(state_dir, vault_or_cell)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            current = int(path.read_text(encoding="utf-8").strip() or "0")
        except Exception:  # noqa: BLE001 - a fresh/corrupt counter restarts
            current = 0
        # Replaced atomically but not synced. A power loss may leave the counter older or empty
        # (read as 0); that is safe because every validity stamp lives within one invocation, and
        # the boundary serializes this bump with every in-boundary comparison.
        try:
            durable_write.replace_text(path, str(current + 1), sync=False)
        except OSError:
            # A reader that holds the file past the rename's retry window
            # (Windows) must not stop the bump: a counter that stays put would
            # admit a stale stamp. Write in place instead; a torn read of it
            # differs from the stamp's generation, which disables reuse.
            path.write_text(str(current + 1), encoding="utf-8")
    except Exception:  # noqa: BLE001 - the counter must never break a commit
        pass


def active_manager() -> LeaseManager:
    """Return the manager owning this invocation, or the configured default."""
    manager = _ACTIVE_LEASE_MANAGER.get()
    return manager if manager is not None else get_manager()


def _count_busy_once(error: BaseException) -> None:
    """Count a `MUTATION_BUSY` as it reaches a caller, once, unless it repeats a recorded park."""
    if mutation_journal.claim_refusal(error) and not mutation_journal.repeats_parked_refusal("MUTATION_BUSY"):
        mutation_lock.record_client_visible_busy()


def record_caller_refusal(
    error: BaseException,
    *,
    tool: str,
    code: str | None = None,
    request_id: str | None = None,
    targets: Sequence[str] = (),
    duration_ms: float | None = None,
) -> None:
    """Journal and count a refusal that is leaving for a caller without passing `invoke`.

    `invoke` records its own failures. The store queue, the `/upload` route and an
    upload session's retryable park hand a refusal to a caller from outside it, so
    those seams call this with the tool or route that was refused. A refusal that
    already has its row (`invoke` journaled it, or an earlier seam did) records
    nothing, so each refusal appears once. The boundary wait is the one the refusal
    carries; a refusal that never reached the boundary reports none. Never raises.
    """
    try:
        if not mutation_journal.claim_refusal(error):
            return
        code = code or leaf_contract_code(error) or type(error).__name__
        active_manager()._record_mutation_journal(
            request_id=request_id or str(uuid.uuid4()),
            command=tool,
            receipt=None,
            outcome="failed",
            error_code=code,
            duration_ms=duration_ms,
            scope=None,
            targets=list(targets),
            boundary_timing=(mutation_journal.boundary_wait_ms(error), None),
        )
        if code == "MUTATION_BUSY" and not mutation_journal.repeats_parked_refusal(code):
            mutation_lock.record_client_visible_busy()
    except Exception:  # noqa: BLE001 - observability must never change the refusal a caller gets
        pass


def active_mutation_request_id() -> str | None:
    """Return the current content-free request identity for commit attribution."""
    trace = _ACTIVE_MUTATION_TRACE.get()
    return trace[0] if trace is not None else None


def active_mutation_trace() -> tuple[str, str, str] | None:
    """`(request_id, command, receipt)` of the mutation this code runs inside,
    or `None` outside one (a CLI helper, the watcher, a direct call). The heat
    projection reads the command name as the origin of a governed commit."""
    return _ACTIVE_MUTATION_TRACE.get()


def active_mutation_committed() -> bool:
    """Whether this invocation's canonical writer already crossed its commit boundary.

    The read side of `mark_active_mutation_committed`, and the one honest signal
    for "this invocation will have a committed terminal to carry an advisory on".
    False outside an invocation, on a verified replay (which writes nothing), and
    on any pass that found nothing to commit.
    """
    return _ACTIVE_MUTATION_COMMITTED.get()


def active_direct_mutation_guard(vault_root: os.PathLike[str] | str, *, state_root: Path) -> bool:
    """Return whether this thread owns this manager-root boundary directly."""
    boundary = _direct_mutation_boundary(vault_root, state_root)
    return boundary in _ACTIVE_DIRECT_MUTATION_GUARDS.get()


def _invocation_provenance(
    function: Callable[..., Any],
) -> Callable[..., Any]:
    """Give each dispatched command one record of what it served, for its own guard passes only.

    Outermost, so a store-routed call fills the same record from the store's thread. Under the
    MCP surface this joins the record that the surface opened for its second pass.
    """
    @wraps(function)
    def wrapped(command: Any, *injected: Any, **kwargs: Any) -> Any:
        from .governance import principal as principal_module

        with principal_module.invocation_scope():
            return function(command, *injected, **kwargs)

    return wrapped


def _foreground_command_activity(
    function: Callable[..., Any],
) -> Callable[..., Any]:
    """Cover the dispatcher and its projected completion with foreground activity."""
    @wraps(function)
    def wrapped(command: Any, *injected: Any, **kwargs: Any) -> Any:
        from .governance.egress import is_vault_root

        if not injected or not is_vault_root(injected[0]):
            return function(command, *injected, **kwargs)
        from .foreground_activity import foreground_scope

        with foreground_scope(injected[0]):
            return function(command, *injected, **kwargs)

    return wrapped


def _fixed_projected_command_completion(
    function: Callable[..., Any],
) -> Callable[..., Any]:
    @wraps(function)
    def wrapped(
        command: Any,
        *injected: Any,
        idempotency_key: str | None = None,
        public_idempotency_key: str | None | object = _PUBLIC_IDEMPOTENCY_KEY_UNSET,
        idempotency_principal_scope: str | None = None,
        implicit_idempotency_scope: str | None = None,
        mutation_request_id: str | None = None,
        **kwargs: Any,
    ) -> Any:
        from .governance import projection_runtime, projection_timing

        def invoke() -> Any:
            return function(
                command,
                *injected,
                idempotency_key=idempotency_key,
                public_idempotency_key=public_idempotency_key,
                idempotency_principal_scope=idempotency_principal_scope,
                implicit_idempotency_scope=implicit_idempotency_scope,
                mutation_request_id=mutation_request_id,
                **kwargs,
            )

        if (
            getattr(command, "name", None) not in {"ask_memory", "find"}
            or not injected
            or not isinstance(injected[0], (str, os.PathLike))
        ):
            return invoke()
        started_at = time.perf_counter()
        vault_root = Path(injected[0])
        preactivated = projection_runtime.has_preactivated_projection_runtime(vault_root)
        completion_required = (
            preactivated
            or projection_runtime.requires_fixed_projected_completion(vault_root)
            or projection_runtime.classify_projected_completion_boundary(vault_root)
        )
        if not completion_required:
            return invoke()
        request_class = projection_timing.request_class_for_command(
            command,
            injected,
            kwargs,
        )
        if request_class is None:
            return invoke()
        completion = (
            projection_timing.fixed_public_completion(request_class)
            if preactivated
            else projection_timing.fixed_public_completion(
                request_class,
                started_at=started_at,
            )
        )
        with completion:
            return invoke()

    return wrapped


def _restricted_refusal_floor(
    function: Callable[..., Any],
) -> Callable[..., Any]:
    """Hold a restricted caller's direct-read refusal to the disclosure floor.

    A missing page refuses before any page is read, while a withheld one is
    read and decided first. Padding every refusal that a restricted caller
    receives to one floor removes that difference. A success returns at once,
    notices included. The restriction is asked only after a refusal, so a
    success never pays for the question. Behind an MCP or REST surface the
    surface waits, after its own handling of the refusal; elsewhere this
    thread waits.
    """

    @wraps(function)
    def wrapped(command: Any, *injected: Any, **kwargs: Any) -> Any:
        if (
            not getattr(command, "restricted_refusal_floor", False)
            or not injected
            or not isinstance(injected[0], (str, os.PathLike))
        ):
            return function(command, *injected, **kwargs)
        from .governance import policy

        started_at = time.perf_counter()
        # A direct read changes no governance, so it decides under one policy load.
        with policy.request_policy_snapshot():
            try:
                return function(command, *injected, **kwargs)
            except Exception as error:
                _complete_restricted_refusal(Path(injected[0]), started_at, error)
                raise

    return wrapped


def _complete_restricted_refusal(
    vault_root: Path, started_at: float, error: BaseException
) -> None:
    from .governance import egress, projection_timing

    try:
        restricted = egress.caller_restricted(vault_root)
    except Exception:  # noqa: BLE001 - an undecided caller costs only the floor
        restricted = True
    if not restricted:
        return
    # The finished frames of a withheld read still hold the page it decided.
    # Freed after the wait, a large page would make its refusal measurably
    # later than a missing one, so free them inside the floor.
    seen: set[int] = set()
    chained: BaseException | None = error
    while chained is not None and id(chained) not in seen:
        seen.add(id(chained))
        traceback.clear_frames(chained.__traceback__)
        chained = chained.__cause__ or chained.__context__
    padding_started = time.perf_counter()
    work_ms = (padding_started - started_at) * 1000.0
    deferred = projection_timing.defer_refusal_floor(started_at)
    if not deferred:
        projection_timing.pad_to_refusal_floor(started_at)
    # Owner-only ledger span of fixed labels and counts, never caller or vault data.
    call_spans.record_span(
        "command.refusal_floor",
        (time.perf_counter() - padding_started) * 1000.0,
        {
            "work_ms": math.ceil(work_ms),
            "overrun": int(work_ms > projection_timing.RESTRICTED_REFUSAL_FLOOR_MS),
            "deferred": int(deferred),
        },
    )


def _note_committed_terminal_targets(terminal: Mapping[str, Any]) -> None:
    """Hand the call ledger the pages this write committed, from the terminal's
    own path projection, so a write whose destination the server derived still
    reports what it saved. Paths only; the ledger keeps a count where paths are
    content."""
    from . import call_ledger

    path = terminal.get("path")
    paths = terminal.get("paths")
    call_ledger.note_committed_targets(
        [path] if isinstance(path, str) else paths if isinstance(paths, list) else []
    )


def _request_prominence_context(func):
    @wraps(func)
    def wrapped(command, *injected, **kwargs):
        from . import prominence

        if injected and isinstance(injected[0], Path):
            with prominence.request_scope(injected[0]):
                return func(command, *injected, **kwargs)
        return func(command, *injected, **kwargs)

    return wrapped


def _store_request_is_read_only(command, kwargs):
    """Whether a store-routed request changes nothing, as `invoke_command` will classify it."""
    from .commands import invocation_is_read_only
    from .governance.egress import SelectorCoverageError

    try:
        return invocation_is_read_only(command, kwargs)
    except SelectorCoverageError:
        # `invoke_command` sends this selector down the writer path and refuses it there.
        return False


def _record_store_queue_refusal(command, kwargs, root, error, waited_seconds):
    record_caller_refusal(
        error,
        tool=command.name,
        request_id=kwargs.get("mutation_request_id"),
        targets=[str(root)],
        duration_ms=round(waited_seconds * 1000, 2),
    )


def _collection_store_thread(func):
    """Run a store-routed collection request on the store thread of the service serving its
    vault, in the caller's context (``collection_store.runtime.route``)."""

    @wraps(func)
    def wrapped(command, *injected, **kwargs):
        if injected and isinstance(injected[0], (str, os.PathLike)):
            from . import foreground_priority
            from .collection_store import runtime as store_runtime

            import_request = kwargs.get("import_request")
            # Import status is a read-only protocol mode; polling must preserve maintenance backoff.
            import_status = (command.name == "record_memory" and kwargs.get("action") == "import"
                             and isinstance(import_request, Mapping) and import_request.get("mode") == "status")
            read_only = _store_request_is_read_only(command, kwargs)
            # Keep demand visible between the routing proof and queue insertion.
            with foreground_priority.foreground():
                if not (read_only or import_status) and _writes_absorb_contention():
                    try:
                        with mutation_lock.absorbing_busy():
                            routed = store_runtime.route(command.name, injected[0], kwargs)
                    except OpError as error:
                        # Routing proves ownership under the boundary and changes nothing.
                        if error.code != "MUTATION_BUSY":
                            raise
                        routed = error
                    if routed is not None:
                        return get_manager()._attempt_store_write(func, command, injected, kwargs, routed)
                    return func(command, *injected, **kwargs)
                server = store_runtime.route(command.name, injected[0], kwargs)
                if server is not None:
                    return server.call(
                        lambda: func(command, *injected, **kwargs),
                        # A read cannot start a job, revise a declaration or leave views pending; waking
                        # every tick after it kept the store thread busy beside the next request.
                        wake_maintenance=not (read_only or import_status),
                        read_only=read_only,
                        # The queue refuses before `invoke` runs, so a refused write records itself here.
                        on_refusal=None if read_only else partial(
                            _record_store_queue_refusal, command, kwargs, server.root
                        ),
                    )
        return func(command, *injected, **kwargs)

    return wrapped


def _foreground_priority_request(func):
    """Run every dispatched command, read or write, ahead of in-process bulk work.

    The one mark for commands (`foreground_priority`). Innermost, so it marks
    the thread that runs the command -- the collection store's thread for a
    routed call -- and leaves out the fixed public completion that pads a
    projected read to its class deadline.
    """

    @wraps(func)
    def wrapped(command, *injected, **kwargs):
        from . import foreground_priority

        with foreground_priority.request_foreground():
            return func(command, *injected, **kwargs)

    return wrapped


@_invocation_provenance
@_foreground_command_activity
@_fixed_projected_command_completion
@_restricted_refusal_floor
@_request_prominence_context
@_collection_store_thread
@_foreground_priority_request
def invoke_command(
    command: Any,
    *injected: Any,
    idempotency_key: str | None = None,
    public_idempotency_key: str | None | object = _PUBLIC_IDEMPOTENCY_KEY_UNSET,
    idempotency_principal_scope: str | None = None,
    implicit_idempotency_scope: str | None = None,
    mutation_request_id: str | None = None,
    **kwargs: Any,
) -> Any:
    from .commands import (
        invocation_is_read_only,
        validate_process_media_operation,
        writes_through_superseded_maintenance_leaf,
    )
    from .reserved_paths import (
        _owner_authority_scope,
        mutation_remediation,
        reserved_preflight,
    )

    reserved_hit = reserved_preflight(command, kwargs)

    if command.name == "edit_memory":
        from .edit_operations import normalize_edit_surface_arguments

        kwargs = normalize_edit_surface_arguments(kwargs)

    # Terminal egress filter (design D1). THIS is the one dispatcher shared by
    # MCP, REST, hosted, and CLI — `command_surface.bind_vault` covers MCP
    # only, and the `EXOMEM_RETRIEVE_INJECT` hook deliberately reaches memory
    # over REST-then-CLI, the two paths that skip it. Putting the scrubber and
    # the withheld cross-check here removes that whole bypass class.
    from .governance.egress import (
        SelectorCoverageError,
        disclosure_boundary,
        emit_boundary_receipt,
        is_vault_root,
        postfilter,
        postfilter_error,
    )

    selector_error: SelectorCoverageError | None = None
    try:
        read_only = invocation_is_read_only(command, kwargs)
    except SelectorCoverageError as error:
        # Unknown selectors must first take the conservative writer/admission
        # path, but must never execute a leaf that could return an unreceipted
        # future read representation.
        selector_error = error
        read_only = False

    if not read_only:
        from . import cloud_cell

        if cloud_cell.cloud_read_only_enabled():
            raise OpError(
                "CLOUD_CELL_READ_ONLY",
                "this Exomem Cloud cell is serving read-only",
                "wait for the cell to leave read-only mode, or read instead of write",
            )

    active_surface = capabilities_module.current_active_surface()
    job_start = False
    if (
        command.name == "maintain_memory"
        and not read_only
        and selector_error is None
        and active_surface is not None
        and active_surface.surface in _REQUEST_BOUND_REMOTE_SURFACES
    ):
        from . import maintenance_jobs

        mode = kwargs.get("mode")
        if mode in maintenance_jobs.JOB_MODES:
            # Before the writer manager and before any job row: a remote write has no
            # run without the plan the owner previewed and a reason.
            maintenance_jobs.require_plan(mode, kwargs)
            maintenance_jobs.require_owner(injected[0])
            job_start = True
            # Reconcile writes by default, so a start may omit `dry_run`: the job's dispatch is explicit.
            kwargs = {**kwargs, "dry_run": False}
        if not job_start and mode not in remote_admitted_maintenance_modes():
            raise OpError(
                "MAINTENANCE_REQUIRES_CLI",
                REMOTE_MAINTENANCE_MESSAGE,
                REMOTE_MAINTENANCE_REMEDIATION,
                details={"status": "terminal", "committed": False},
            )

    if (
        not read_only
        and selector_error is None
        and active_surface is not None
        and active_surface.surface in _REQUEST_BOUND_REMOTE_SURFACES
        and writes_through_superseded_maintenance_leaf(command, kwargs)
    ):
        # The legacy leaf commands still write through the wide boundary with no plan. Refuse before
        # the writer manager: a remote write is a `maintain_memory` job with a reviewed plan.
        raise OpError(
            "MAINTENANCE_PLAN_REQUIRED",
            f"a remote {command.name} write runs as a maintenance job through maintain_memory",
            f"Use {capabilities_module.active_tools_text('maintain_memory')}: preview with dry_run=true, "
            "then repeat it with dry_run=false, the preview's plan_id and a why.",
            details={"status": "terminal", "committed": False},
        )

    if reserved_hit is not None:
        if read_only:
            raise OpError("NOT_FOUND", "path does not exist")
        raise OpError(
            "RESERVED_PATH",
            "path is reserved for its owning subsystem",
            mutation_remediation(reserved_hit.classification.descriptor_id),
        )

    # process_media already owns a stable public input error. Preserve it once
    # reserved-path routing has had its required first refusal opportunity.
    if selector_error is not None and command.name == "process_media":
        validate_process_media_operation(kwargs.get("operation", "process"))

    dispatch_command = command
    if job_start:
        # A job start returns a handle and a later poll reads the live state: replaying a
        # stored handle for 600 s after a cancel would hand back a dead job.
        implicit_idempotency_scope = None
        dispatch_command = replace(command, leaf=maintenance_jobs.start_leaf(command, injected, kwargs))
    if selector_error is not None:

        def reject_uncovered_selector(*_args: Any, **_kwargs: Any) -> Any:
            raise OpError(
                "RECEIPT_OUTCOME_MISSING",
                "command selector is not release-covered",
            )

        dispatch_command = replace(command, leaf=reject_uncovered_selector)

    def _invoke() -> Any:
        with _owner_authority_scope(command.name):
            # This fixed capture field gates cached terminals and key mismatch, too.
            if command.name == "capture_source" and kwargs.get("source_identity") is not None:
                from .source_capture import require_authority

                require_authority(injected[0])
            return get_manager().invoke(
                dispatch_command,
                injected,
                kwargs,
                read_only=read_only,
                idempotency_key=idempotency_key,
                public_idempotency_key=public_idempotency_key,
                idempotency_principal_scope=idempotency_principal_scope,
                implicit_idempotency_scope=implicit_idempotency_scope,
                mutation_request_id=mutation_request_id,
            )

    if not injected or not is_vault_root(injected[0]):
        return _invoke()
    # An error is a payload. `_invoke()` is evaluated as an ARGUMENT to
    # `postfilter`, so a raising command never reached the filter and its
    # message crossed this boundary untouched — `AMBIGUOUS_REFERENCE` embedded
    # the colliding vault paths and made that a path oracle. Both the
    # read-only and the mutation return are inside one try, so a future path
    # through this function cannot open a fresh bypass either.
    #
    # `kwargs` goes to the filter so it can tell a reference the CALLER sent
    # from one the vault volunteered. Only the latter may be redacted; see
    # `postfilter_error`.
    #
    # `BaseException`, not `Exception`: a "terminal" filter that a Cancelled or
    # a SystemExit walks straight past is not terminal.
    # Command names are fixed by the command schema; other commands keep their meter.
    body_meter = (
        call_spans.meter(call_spans.MOVE_PREFLIGHT_MS, call_spans.MOVE_PREFLIGHTS)
        if command.name == "manage_memory_file" else nullcontext()
    )
    if not read_only:
        try:
            # The same fixed phase labels as the read-only path below.
            with body_meter as measured, call_spans.span("command.body", measured):
                result = _invoke()
            with call_spans.span("command.terminal_filter"):
                try:
                    return postfilter(command.name, result, injected[0], request_kwargs=kwargs)
                except Exception as error:
                    converted = post_leaf_refusal(error)
                    if converted is error:
                        raise
                    raise converted from error
        except BaseException as error:
            with call_spans.span("command.terminal_filter"):
                postfilter_error(command.name, error, injected[0], request_kwargs=kwargs)
            raise
    # The try lives INSIDE the boundary so `collector` is still bound when the
    # filter runs: `disclosure_boundary`'s `finally` resets the contextvar on
    # the way out, so an except-block outside it records credential blocks into
    # a collector that is already gone, and emits no receipt for a governed read
    # that touched withheld items before failing.
    from .collection_store import ownership

    with ownership.read_scope(injected[0]) as ownership_scope, \
            disclosure_boundary(injected[0], command.name) as collector:
        # The dispatcher's own checks before the body, which the leaf span alone does not separate.
        call_spans.record_span_since("command.leaf_pre", "command.leaf_start")
        try:
            # Fixed phase labels carry timing only, never caller or vault data.
            with body_meter as measured, call_spans.span("command.body", measured):
                result = _invoke()
            with call_spans.span("command.terminal_filter"):
                result = postfilter(command.name, result, injected[0], request_kwargs=kwargs)
        except BaseException as error:
            try:
                with call_spans.span("command.terminal_filter"):
                    postfilter_error(command.name, error, injected[0], request_kwargs=kwargs)
            finally:
                try:
                    ownership_scope.finish()
                finally:
                    with call_spans.span("command.disclosure_receipt"):
                        emit_boundary_receipt(collector)
            raise
        try:
            with call_spans.span("command.ownership_finish"):
                ownership_scope.finish()
        finally:
            with call_spans.span("command.disclosure_receipt"):
                emit_boundary_receipt(collector)
        from . import call_ledger
        from .governance.egress import released_item_count

        call_ledger.note_released_items(released_item_count(collector, result))
        return result


def coordination_status(
    vault_or_cell: os.PathLike[str] | str | None = None,
) -> dict[str, Any]:
    return get_manager().status(vault_or_cell)


def start_server_lifecycle() -> LeaseManager:
    manager = get_manager()
    manager._serves_collection_store = True
    if manager.config.enabled and manager.config.preferred_writer:
        try:
            manager.ensure_writer()
        except OpError:
            # Startup remains readable. Mutations will retry authoritatively.
            pass
    manager.start_renewer()
    atexit.register(manager.close)
    try:
        from .collection_store import runtime as store_runtime
        from .vault import resolve_vault

        # The vault's collection store, if its marker routes any collection to one (S1).
        store_runtime.serve(resolve_vault(), manager)
    except Exception:  # noqa: BLE001 - file collections serve regardless; store routes refuse
        logger.warning("collection store serving did not start", exc_info=True)
    return manager


def stop_server_lifecycle() -> None:
    """Hand the writer lease back at a clean service stop, so the next process takes it at once.

    uvicorn re-raises the stop signal after its graceful shutdown, so the `atexit` close
    never runs on that path; the lifespan end calls this instead. A flush that cannot finish
    inside the mutation timeout leaves the lease to expire on its TTL, as a crash would.
    """
    with _MANAGERS_LOCK:
        manager = _MANAGERS.get(LeaseConfig.from_env())
    if manager is None or not manager._serves_collection_store:
        return
    try:
        manager.close()
    except OpError as error:
        if error.code != "COLLECTION_STORE_FLUSH_PENDING":
            raise
        logger.warning("lease handback at stop is pending; the lease expires on its TTL")
    finally:
        # A stopped manager is not reusable; the next lifecycle builds a fresh one.
        with _MANAGERS_LOCK:
            if _MANAGERS.get(manager.config) is manager:
                del _MANAGERS[manager.config]


def reset_managers_for_tests() -> None:
    """Close and clear process globals; intentionally public for deterministic tests."""
    with _MANAGERS_LOCK:
        managers = list(_MANAGERS.values())
        _MANAGERS.clear()
    for manager in managers:
        manager.close()
