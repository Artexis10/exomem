"""Receipt-owned deferred write advisory work and exact result retrieval.

Lane 4 owns two leaves and no scheduler:

* an executor that a bounded server-owned drain hands one already-claimed
  ``write_advisory`` component, and
* an exact-only projection for one opaque
  ``exomem://write-advisory-result/<id>`` reference.

Everything durable belongs to the frozen Lane 1 receipt protocol -- claim,
generation proof, publication CAS, retention and completion. This module adds
no store, no queue, no second protocol, and no worker of its own: the caller
decides when a pass runs.

The advisory itself is unchanged measurement. It reuses the exact vectors the
embedding pass published for one generation, feeds them to the existing
deterministic duplicate/overlap thresholds, and surfaces the same
fingerprint-bound review references the synchronous write path would have
returned. What moved is only *when* that happens and *how* the answer is
addressed afterwards.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import corpus_aware, derived_receipts, embeddings
from .derived_receipts import (
    DerivedAdvisoryCandidate,
    DerivedBatchReceipt,
    DerivedComponent,
    DerivedComponentStatus,
)
from .governance import decisions, egress, membership, scrubber
from .governance import policy as policy_module
from .governance.principal import effective_principal
from .vault import content_hash

log = logging.getLogger(__name__)

#: The read-only `review_memory` mode this lane answers.
MODE = "write-advisory-result"
#: The result namespace, deliberately distinct from the triageable candidate
#: namespace `exomem://review/write-advisory/<id>`.
RESULT_REF_PREFIX = "exomem://write-advisory-result/"
MAX_RESULT_CANDIDATES = 8
DUPLICATE_TOP_N = 3
OVERLAP_TOP_N = 3
CLAIM_LEASE_SECONDS = 60.0

#: One outcome for malformed, unknown, unauthorized and expired references, so
#: a caller cannot separate "there is no such result" from "not for you". It
#: echoes nothing back, so the four cases are byte-identical.
RESULT_NOT_FOUND = (
    "REVIEW_ITEM_NOT_FOUND: no current write-advisory result for that reference"
)
MISSING_RESULT_REF = "INVALID_REVIEW: write-advisory-result mode requires `ref`"

CanonicalGenerationObserver = Callable[[Path], "str | None"]


class _UnaddressableAdvisory(RuntimeError):
    """A surfaced advisory that carries no review identity to address later."""


@dataclass(frozen=True, slots=True)
class AdvisoryCustody:
    """This receipt's advisory component status and stable result reference.

    Both values are the frozen Lane 1 objects, carried verbatim rather than
    reshaped, so a terminal wiring them cannot drift from the protocol.
    """

    status: DerivedComponentStatus
    result_ref: str | None


@dataclass(frozen=True, slots=True)
class AdvisoryExecution:
    """Bounded, content-free outcome of one advisory component attempt.

    It names the batch, the closed publication outcome, and the closed state
    that was published. No path, title, excerpt, exception text, authorization
    fact, or queue internal appears here.

    `reused_vectors` reports that this attempt invoked no encoder -- either
    because the published generation vectors were reused, or because an
    already-published result was replayed without recomputation at all.
    """

    batch_id: str
    outcome: str
    state: str | None = None
    failure_code: str | None = None
    candidate_count: int = 0
    reused_vectors: bool = False
    completed: bool = False


@dataclass
class _CandidateHit:
    """The minimal shape `egress.annotate_hits` decides: a path and its bytes.

    `snapshot_hash` is read by the release plane as the expected content hash,
    so a counterpart whose bytes moved since publication cannot be released
    under a decision that was made about different content.
    """

    path: str
    snapshot_hash: str
    decision: Any = None


# ---------------------------------------------------------------------------
# Frozen Lane 1 handoff
# ---------------------------------------------------------------------------


def advisory_custody(
    vault_root: Path,
    receipt: DerivedBatchReceipt,
    *,
    handoff: Any = None,
) -> AdvisoryCustody:
    """Read one receipt's advisory custody through the frozen Lane 1 seams.

    `handoff` accepts Lane 1's own committed protocol fake so a terminal
    boundary can be exercised without a store. Whatever it returns is passed
    straight through; this lane never translates the producer's shapes.
    """
    source = derived_receipts if handoff is None else handoff
    status = source.component_status(vault_root, receipt, DerivedComponent.WRITE_ADVISORY)
    result_ref = source.advisory_result_ref(vault_root, receipt)
    return AdvisoryCustody(status=status, result_ref=result_ref)


# ---------------------------------------------------------------------------
# Exact current-generation observation
# ---------------------------------------------------------------------------


def _observe_fingerprint(vault_root: Path, rel_path: str) -> str | None:
    """`vault.content_hash` over one path's current canonical bytes, or None.

    This is the cross-lane fingerprint definition: the same value the batch
    recorded as that path's `after_hash`. A tombstoned, absent, symlinked or
    non-UTF-8 target has no current generation to observe and returns None
    rather than a value that could accidentally compare equal.
    """
    target = Path(vault_root).joinpath(*str(rel_path).split("/"))
    try:
        if target.is_symlink() or not target.is_file():
            return None
        return content_hash(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Component execution
# ---------------------------------------------------------------------------


def _candidates_for(
    vault_root: Path,
    generation: embeddings.GenerationVectors,
    *,
    result_ref: str,
) -> tuple[tuple[DerivedAdvisoryCandidate, ...], list[Any]]:
    """The bounded candidate set for one proven generation, without re-encoding.

    Returns the candidates and the emitted advisories behind them, so the
    caller can commit the once-only first-surfaced ledger after -- and only
    after -- the store accepts the result they belong to.
    """
    scores = corpus_aware.best_cosine_per_file_for_vectors(
        vault_root,
        generation.vectors,
        self_path=generation.rel_path,
        k=max(DUPLICATE_TOP_N, OVERLAP_TOP_N) * 5,
    )
    duplicates = corpus_aware.detect_duplicates(
        vault_root,
        title=generation.title,
        body=generation.body,
        self_path=generation.rel_path,
        precomputed=scores,
        top_n=DUPLICATE_TOP_N,
    )
    overlaps = corpus_aware.detect_contradictions(
        vault_root,
        title=generation.title,
        body=generation.body,
        self_path=generation.rel_path,
        precomputed=scores,
        top_n=OVERLAP_TOP_N,
    )
    emitted = corpus_aware.emitted_write_advisory_groups(
        vault_root,
        self_path=generation.rel_path,
        groups=[
            ("near-duplicate", duplicates),
            *corpus_aware.detected_overlap_advisory_groups(overlaps),
        ],
        apply_declared_pair_filter=True,
        # This set may still be refused by the store, and a refused set reached
        # nobody. The stamp is committed after publication succeeds.
        record_surfacing=False,
    )

    candidates: dict[tuple[str, str], DerivedAdvisoryCandidate] = {}
    for item in emitted:
        if item.identity is None or item.counterpart_rel_path is None:
            # The synchronous path can print an unidentified advisory; a
            # deferred result cannot be addressed later without its review
            # identity, and dropping it silently would hide a real signal.
            raise _UnaddressableAdvisory(item.kind)
        fingerprint = _observe_fingerprint(vault_root, item.counterpart_rel_path)
        if fingerprint is None:
            continue
        key = (item.counterpart_rel_path, item.identity.ref)
        if key in candidates:
            continue
        candidates[key] = DerivedAdvisoryCandidate(
            counterpart_rel_path=item.counterpart_rel_path,
            counterpart_fingerprint=fingerprint,
            warning=item.warning,
            advisory_ref=result_ref,
            review_ref=item.identity.ref,
            triage_fingerprint=item.identity.fingerprint,
        )
        if len(candidates) >= MAX_RESULT_CANDIDATES:
            break
    return tuple(candidates.values()), emitted


def _publish(
    vault_root: Path,
    claimed_status: DerivedComponentStatus,
    *,
    state: str,
    observed: str,
    candidates: Sequence[DerivedAdvisoryCandidate] = (),
    failure_code: str | None = None,
    now: float | None = None,
    reused_vectors: bool = False,
) -> AdvisoryExecution:
    """CAS-publish through Lane 1 and report only its closed outcome."""
    normalized = tuple(candidates)
    publication = derived_receipts.publish_advisory_result(
        vault_root,
        claimed_status,
        state=state,
        candidates=normalized,
        failure_code=failure_code,
        observed_target_fingerprint=observed,
        now=now,
    )
    accepted = publication.outcome in {"published", "already_published"}
    return AdvisoryExecution(
        batch_id=claimed_status.batch_id,
        outcome=publication.outcome,
        state=state if accepted else None,
        failure_code=failure_code if accepted else None,
        candidate_count=len(normalized) if accepted else 0,
        reused_vectors=reused_vectors,
    )


def execute_write_advisory(
    vault_root: Path,
    claimed_status: DerivedComponentStatus,
    *,
    now: float | None = None,
) -> AdvisoryExecution:
    """Run one claimed `write_advisory` component against its exact target.

    The claim is the only authority: no foreground writer has to be present.
    What is re-proved before the target is read, encoded, scored or published
    is the target's own content fingerprint, and a mismatch publishes
    `failed`/`generation_changed` so the stale result is superseded rather
    than silently dropped. An older worker may still be running, but it cannot
    publish current output.

    This deliberately does not also compare the batch's recorded canonical
    generation against the vault's current one (orchestrator rulings R1, R3).
    That value is a single vault-global graph checkpoint which advances on
    every write to any page, so as a per-page freshness test it fails closed on
    writes that were entirely sound: in a burst, every batch but the
    last-written one refused with `generation_changed` and rotated until its
    attempts ran out. The fingerprint below is the substantive guard, and it
    asks the question the generation could not -- is *this* target still the
    bytes this result was computed for.
    """
    if claimed_status.component is not DerivedComponent.WRITE_ADVISORY:
        raise ValueError("this executor owns only the write_advisory component")

    batch_id = claimed_status.batch_id
    ref = claimed_status.advisory_result_ref
    stored = (
        None
        if ref is None
        else derived_receipts.read_advisory_result(vault_root, ref, now=now)
    )
    if stored is None or stored.target_rel_path is None:
        # No addressable result row, or a pre-extension row with no target
        # identity: it stays exactly resolvable and fails closed in the store.
        return AdvisoryExecution(
            batch_id=batch_id, outcome="stale_claim", failure_code="target_unreadable"
        )

    if stored.state != "pending":
        # A crash after publication reuses the stored result and completes the
        # component without recomputation (Design Decision 6). Recomputing here
        # could not converge: ordinary corpus drift between the crash and this
        # replay changes the candidate tuple, the store refuses the mismatched
        # replay as a stale claim, and the component would rotate for ever
        # while its attempt count climbed. Nothing is encoded, emitted or
        # published on this path; completion is proved by the caller.
        return AdvisoryExecution(
            batch_id=batch_id,
            outcome="already_published",
            state=stored.state,
            failure_code=stored.failure_code,
            candidate_count=len(stored.candidates),
            reused_vectors=True,
        )

    observed = _observe_fingerprint(vault_root, stored.target_rel_path)
    if observed is None:
        # Nothing current to observe, so nothing may be published against it.
        # Proof and retirement own what happens to the batch from here.
        return AdvisoryExecution(
            batch_id=batch_id, outcome="unprovable", failure_code="target_unreadable"
        )
    if observed != stored.target_fingerprint:
        # Publishing the true observation is what supersedes the whole result.
        return _publish(
            vault_root,
            claimed_status,
            state="failed",
            failure_code="generation_changed",
            observed=observed,
            now=now,
        )

    generation = embeddings.prepare_generation_vectors(
        vault_root, stored.target_rel_path, expected_fingerprint=observed
    )
    if generation is None:
        return _publish(
            vault_root,
            claimed_status,
            state="failed",
            failure_code="embedding_unavailable",
            observed=observed,
            now=now,
        )

    try:
        candidates, emitted = _candidates_for(vault_root, generation, result_ref=ref)
    except _UnaddressableAdvisory:
        log.debug("advisory batch=%s surfaced an unaddressable advisory", batch_id)
        return _publish(
            vault_root,
            claimed_status,
            state="failed",
            failure_code="advisory_failed",
            observed=observed,
            now=now,
            reused_vectors=generation.reused,
        )
    except Exception:  # noqa: BLE001 - optional advisory work fails closed and soft
        log.warning("advisory computation failed batch=%s", batch_id, exc_info=True)
        return _publish(
            vault_root,
            claimed_status,
            state="failed",
            failure_code="advisory_failed",
            observed=observed,
            now=now,
            reused_vectors=generation.reused,
        )

    execution = _publish(
        vault_root,
        claimed_status,
        state="ready",
        candidates=candidates,
        observed=observed,
        now=now,
        reused_vectors=generation.reused,
    )
    if execution.outcome == "published":
        # Durable now, so the signal has genuinely reached its result. A refused
        # publication deliberately leaves the once-only ledger untouched.
        corpus_aware.record_write_advisory_surfacing(vault_root, emitted)
    return execution


def _release(
    vault_root: Path,
    status: DerivedComponentStatus,
    *,
    failure_code: str,
    now: float | None,
) -> None:
    """Rotate one claim back to the store, or accept that it is no longer ours."""
    try:
        derived_receipts.retry_component(
            vault_root, status, failure_code=failure_code, now=now
        )
    except (RuntimeError, ValueError):
        log.debug("advisory claim was no longer current batch=%s", status.batch_id)


def run_pending_write_advisories(
    vault_root: Path,
    *,
    observe_current_generation: CanonicalGenerationObserver,
    owner: str | None = None,
    limit: int = 8,
    lease_seconds: float = CLAIM_LEASE_SECONDS,
    now: float | None = None,
) -> tuple[AdvisoryExecution, ...]:
    """One bounded pass: claim due advisory custody, execute it, complete it.

    Deliberately a single pass with no loop and no timer of its own. Lane 1's
    bounded drain, a restart pass, or an operator command decides when this
    runs; a component this lane does not own is handed straight back so it
    keeps rotating behind untouched work.
    """
    claim_owner = owner or f"write-advisory-{secrets.token_hex(8)}"
    claims = derived_receipts.claim_ready_components(
        vault_root,
        owner=claim_owner,
        limit=int(limit),
        lease_seconds=float(lease_seconds),
        now=now,
    )
    executions: list[AdvisoryExecution] = []
    for status in claims:
        if status.component is not DerivedComponent.WRITE_ADVISORY:
            _release(vault_root, status, failure_code="component_unhandled", now=now)
            continue
        try:
            execution = execute_write_advisory(vault_root, status, now=now)
        except Exception:  # noqa: BLE001 - exact custody stays retryable
            log.warning(
                "advisory dispatch failed batch=%s", status.batch_id, exc_info=True
            )
            _release(vault_root, status, failure_code="dispatch_failed", now=now)
            continue

        completed = False
        if execution.outcome in {"published", "already_published", "superseded"}:
            try:
                completed = derived_receipts.complete_component(
                    vault_root,
                    status,
                    observe_current_generation=observe_current_generation,
                    now=now,
                )
            except Exception:  # noqa: BLE001 - completion proof stays retryable
                log.warning(
                    "advisory completion proof failed batch=%s",
                    status.batch_id,
                    exc_info=True,
                )
                completed = False
        if not completed:
            _release(
                vault_root,
                status,
                failure_code=(
                    "component_unhandled"
                    if execution.outcome == "stale_claim"
                    else "generation_changed"
                ),
                now=now,
            )
        executions.append(replace(execution, completed=completed))
    return tuple(executions)


# ---------------------------------------------------------------------------
# Exact-only result retrieval
# ---------------------------------------------------------------------------


def _released_paths(vault_root: Path, pairs: Sequence[tuple[str, str]]) -> set[str]:
    """Current-authority release decision for exact `(path, fingerprint)` pairs.

    The whole decision -- principal, purpose, authorization session, grants,
    tombstones, scope ceilings and the expected content hash -- belongs to the
    existing release plane. Only the released set is used; notices and withheld
    counts are deliberately discarded so a withheld item leaves no trace.
    """
    hits = [
        _CandidateHit(path=rel_path, snapshot_hash=fingerprint)
        for rel_path, fingerprint in pairs
    ]
    if not hits:
        return set()
    annotated = egress.annotate_hits(vault_root, hits, limit=len(hits))
    return {str(getattr(hit, "path", "")) for hit in annotated.hits}


def _path_authority_allows(vault_root: Path, rel_path: str) -> bool:
    """Decide current authority for a path with no bytes left to read.

    A deleted target still has to be authorized before its result may report
    any status, but there is nothing to read and no content hash to bind a
    decision to. So the decision comes from the caller's current policy against
    the path's own scope membership through `membership.evaluate_path_only` --
    the seam the release plane itself uses when it must not read an item --
    with no standing grants applied, because a content-bound grant cannot be
    proven against content that no longer exists. Omitting them can only refuse
    more than the full decision would, never less.

    Every failure to establish the answer is a refusal: an unresolved
    membership, an unavailable authorization session, or an unreadable policy
    all fail closed rather than guess.
    """
    root = Path(vault_root)
    try:
        policy = policy_module.load(root)
    except Exception:  # noqa: BLE001 - an undecidable policy refuses
        return False
    if policy.empty:
        # No governance configured: the same open fast path `annotate_hits`
        # takes, so a deleted target behaves exactly as a present one would.
        return True
    who = effective_principal()
    if policy.blocked or not who.resolved:
        return False
    try:
        purpose = egress._declared_purpose(root, who, None)
    except Exception:  # noqa: BLE001 - an undecidable session refuses
        return False
    try:
        scope_ids = membership.evaluate_path_only(
            root, rel_path, policy
        ).require_classified()
    except Exception:  # noqa: BLE001 - unresolved membership refuses
        return False
    decision = decisions.decide(
        scope_ids,
        audience=who.audience_id,
        purpose=purpose,
        policy=policy,
        active_grants=(),
    )
    return decision.level >= egress.RELEASE_FLOOR


def _target_is_releasable(
    vault_root: Path, rel_path: str, *, fingerprint: str | None
) -> bool:
    """Whether this caller may currently receive anything about the target."""
    if fingerprint is None:
        return _path_authority_allows(vault_root, rel_path)
    return bool(_released_paths(vault_root, ((rel_path, fingerprint),)))


def _released_candidates(
    vault_root: Path, candidates: Sequence[DerivedAdvisoryCandidate]
) -> list[dict[str, str]]:
    """Project only candidates this caller may currently receive.

    Filtering runs before the bound is applied, so a withheld candidate is
    observationally identical to one a job never found: same payload, same
    length, same absence of a code or diagnostic.
    """
    current = [
        candidate
        for candidate in candidates
        if _observe_fingerprint(vault_root, candidate.counterpart_rel_path)
        == candidate.counterpart_fingerprint
    ]
    released = _released_paths(
        vault_root,
        tuple(
            (candidate.counterpart_rel_path, candidate.counterpart_fingerprint)
            for candidate in current
        ),
    )
    return [
        {
            "warning": candidate.warning,
            "ref": candidate.review_ref,
            "fingerprint": candidate.triage_fingerprint,
        }
        for candidate in current
        if candidate.counterpart_rel_path in released
    ][:MAX_RESULT_CANDIDATES]


def _projected(
    ref: str,
    status: str,
    *,
    code: str | None = None,
    advisories: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """The closed wire shape, through the repository's terminal scrubber."""
    payload: dict[str, Any] = {"mode": MODE, "ref": ref, "status": status}
    if status == "failed":
        payload["code"] = code or "advisory_failed"
    elif status == "ready":
        payload["advisories"] = advisories or []
    cleaned, blocked = scrubber.scrub_value(payload)
    if blocked:
        log.debug("write advisory result projection was scrubbed")
    return cleaned


def resolve_result(
    vault_root: Path, ref: str | None, *, now: float | None = None
) -> dict[str, Any]:
    """Resolve exactly one opaque advisory result under current authority.

    There is no list, browse, search, rank, count, continuation or
    implicit-current form: the reference is the only way in, and it addresses
    operational state without authorizing anything. Every lookup re-observes
    the target generation and each counterpart generation, and re-runs the
    release plane, rather than replaying the writer's earlier decision.
    """
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError(MISSING_RESULT_REF)
    stored = derived_receipts.read_advisory_result(vault_root, ref, now=now)
    if stored is None:
        raise ValueError(RESULT_NOT_FOUND)
    if stored.target_rel_path is None:
        # A pre-extension row stays exactly resolvable and fails closed with
        # the store's own compatibility code rather than raising.
        return _projected(stored.ref, "failed", code=stored.failure_code)

    # Authority first, and before any status can be derived from the target.
    # Deciding supersession first would leak: an unauthorized caller would get
    # `superseded` for a real reference and not-found for an unknown one, which
    # separates the two. The observation below stays server-internal until this
    # gate has passed.
    observed = _observe_fingerprint(vault_root, stored.target_rel_path)
    if not _target_is_releasable(
        vault_root, stored.target_rel_path, fingerprint=observed
    ):
        # The caller cannot currently receive the written page, so it cannot
        # receive a result about it either -- and cannot tell that apart from
        # a reference that never existed.
        raise ValueError(RESULT_NOT_FOUND)
    if observed is None or observed != stored.target_fingerprint:
        return _projected(stored.ref, "superseded")

    if stored.state in {"superseded", "pending"}:
        return _projected(stored.ref, stored.state)
    if stored.state == "failed":
        return _projected(stored.ref, "failed", code=stored.failure_code)
    return _projected(
        stored.ref,
        "ready",
        advisories=_released_candidates(vault_root, stored.candidates),
    )
