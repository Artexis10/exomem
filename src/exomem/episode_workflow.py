"""Typed episode candidate operations over existing curation leaves.

The active agent decides every candidate, destination and disposition; this
module validates, records and (only when enabled) executes them. A candidate
names a typed destination for an existing writer -- one closed curation step
(`curation.STEP_KINDS`, fields checked by `curation.validate_forward_plan`) --
never a free-form effect.

Authority boundary, kept small for review (close-memory-loop task 5.5):

* `inspect` reads the caller's own episode ledger. It writes nothing.
* `prepare` and `disposition` write only the caller's audience-bound episode
  journal and inert sealed single-step curation plans. Preparation runs the
  same read-only leaf preparation `maintain_memory mode=curation` propose runs;
  no canonical page is written.
* `resume` is the only path to a writer. Unless the service environment sets
  `EXOMEM_EPISODE_WORKFLOW`, it refuses with `episode_workflow_disabled` before
  reading or writing anything. When enabled it runs only a leaf that is routed,
  bound to its own sealed plan, covered by a current precommit attestation and
  not already attempted, through `curation.apply` -- the existing executor,
  under the command's writer lease and each writer's own validation. It grants
  nothing `maintain_memory mode=curation apply` does not already grant the same
  caller, mints no vocabulary or edge authority, and never retries an
  uncertain attempt: reconciliation reads existing receipts only.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import curation, episode_capture
from . import episode_model as model
from .episode_recovery import EpisodeInputOwner
from .episode_store import EpisodeStore

ENABLE_ENV = "EXOMEM_EPISODE_WORKFLOW"
DISABLED_CODE = "episode_workflow_disabled"
DEFAULT_MAX_LEAVES = 8
MAX_LEAVES = 16
_EXECUTABLE = frozenset({"pending", "proven_uncommitted"})


def _error(code: str, reason: str) -> model.EpisodeError:
    return model.EpisodeError(code, reason)


def enabled() -> bool:
    """Whether this service may execute episode leaves. Default off.

    An environment setting only: the service operator enables it with `1`,
    `true`, `yes` or `on`, anything else leaves it off, and no tool call --
    `configure_memory` included -- can.
    """
    value = os.environ.get(ENABLE_ENV)
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


def _key(episode: Any) -> str:
    if not isinstance(episode, str) or not episode_capture.EPISODE_KEY_RE.fullmatch(episode):
        raise _error("EPISODE_KEY_INVALID", "episode must be an ep- key of 32 lowercase hex")
    return episode


class _Session:
    """One caller's episode, reloaded after every accepted transition."""

    def __init__(self, vault_root: Path, episode: Any):
        self.vault_root = Path(vault_root)
        self.key = _key(episode)
        # The owner is resolved before anything is read or written.
        self.store: EpisodeStore = EpisodeInputOwner(self.vault_root)._store()  # noqa: SLF001
        self.identity = model.episode_id(self.key)
        try:
            self.current = self.store.read(self.identity)
        except curation.CurationError as error:
            if error.code == "CURATION_RUN_NOT_FOUND":
                raise _error("EPISODE_NOT_FOUND", "episode is unavailable") from error
            raise

    @property
    def state(self) -> dict[str, Any]:
        return self.current["state"]

    def transition(self, action: str, **args: Any) -> dict[str, Any]:
        self.current = self.store.transition(
            self.identity,
            expected_revision=self.current["revision"],
            expected_digest=self.current["journal_digest"],
            action=action,
            args=args,
        )
        return self.current

    def transitions(self, commands: Sequence[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
        """Accept every command in one journal write, or none of them."""
        self.current = self.store.transitions(
            self.identity,
            expected_revision=self.current["revision"],
            expected_digest=self.current["journal_digest"],
            commands=commands,
        )
        return self.current


def _candidate_by_key(state: Mapping[str, Any], key: str) -> dict[str, Any] | None:
    return next((item for item in state["candidates"] if item["candidate_key"] == key), None)


def _projection(session: _Session) -> dict[str, Any]:
    """Identities, routes and outcomes only: never leaf args or proposal text."""
    state = session.state
    pending = set(model._pending(state))  # noqa: SLF001
    candidates = []
    for candidate in state["candidates"]:
        proposal = candidate["proposal"] or {}
        disposition = candidate["disposition"]
        leaves = []
        for leaf in candidate["leaves"]:
            binding = leaf["binding"] or {}
            proof = leaf["outcome_proof"] or {}
            leaves.append(
                {
                    "leaf_key": leaf["leaf_key"],
                    "leaf_id": leaf["leaf_id"],
                    "kind": leaf["kind"],
                    "effect_revision": leaf["effect_revision"],
                    "effect_digest": leaf["effect_digest"],
                    "bound": bool(binding),
                    "run_id": binding.get("run_id"),
                    "operation_id": binding.get("operation_id"),
                    "outcome": leaf["outcome"],
                    "attempts": leaf["attempts"],
                    "receipt_digest": proof.get("receipt_digest"),
                }
            )
        candidates.append(
            {
                "candidate_key": candidate["candidate_key"],
                "candidate_id": candidate["candidate_id"],
                "proposal_revision": candidate["proposal_revision"],
                "route": proposal.get("route"),
                "disposition": disposition["value"] if disposition else None,
                "disposition_input_revision": (
                    disposition["input_revision"] if disposition else None
                ),
                "pending": candidate["candidate_id"] in pending
                or any(leaf["leaf_id"] in pending for leaf in candidate["leaves"]),
                "leaves": leaves,
            }
        )
    precommit = state["current_precommit"]
    return {
        "operation": "episode_workflow",
        "episode": session.key,
        "episode_id": state["episode_id"],
        "revision": session.current["revision"],
        "journal_digest": session.current["journal_digest"],
        "input_revision": state["input_revisions"][-1]["revision"],
        "recovery": state["input_revisions"][-1]["recovery"],
        "candidates": candidates,
        "precommit_input_revision": precommit["input_revision"] if precommit else None,
        "reviewed_through_input_revision": state["reviewed_through_input_revision"],
        "covered_through_input_revision": state["covered_through_input_revision"],
        "complete": state["complete"],
        "coverage_current": "unchecked",
        "execution": "enabled" if enabled() else "disabled",
    }


def inspect(vault_root: Path, *, episode: Any) -> dict[str, Any]:
    return _projection(_Session(vault_root, episode))


def _seal(vault_root: Path, leaf: Mapping[str, Any]) -> dict[str, Any]:
    """Seal one leaf into its own single-step curation plan.

    `curation.propose` runs the leaf's existing read-only preparation against
    the current vault, so a new or revised effect is validated now, and the
    sealed plan is inert until `resume` executes it.
    """
    return curation.propose(
        vault_root,
        {
            "version": 1,
            "title": f"Episode leaf {leaf['leaf_key']}",
            "steps": [{"step_id": "leaf", "kind": leaf["kind"], "args": leaf["args"]}],
        },
    )


def prepare(vault_root: Path, *, episode: Any, candidate: Any, proposal: Any) -> dict[str, Any]:
    """Declare or revise one candidate's typed proposal and bind its leaves.

    The proposal is validated by the pure model, and every new or revised leaf
    passes current preparation, before the journal accepts anything: a refused
    proposal or leaf leaves the episode unchanged. The declaration, revision
    and bindings land in one journal write, so a refused bind leaves no
    half-bound candidate, and a journal without room for all of them refuses
    before any plan is sealed.
    """
    session = _Session(vault_root, episode)
    key = model._string(candidate, "candidate_key", 160)  # noqa: SLF001
    existing = _candidate_by_key(session.state, key)
    identity = (
        existing["candidate_id"]
        if existing
        else model.candidate_id(session.state["episode_id"], key)
    )
    trial = session.state if existing else model.declare_candidate(session.state, key)
    revised = True
    try:
        trial = model.revise_proposal(trial, identity, proposal)
    except model.EpisodeError as error:
        if error.code != "EPISODE_PROPOSAL_UNCHANGED":
            raise
        revised = False
    unsealed = [
        leaf
        for leaf in model._candidate(trial, identity)["leaves"]  # noqa: SLF001
        if leaf["binding"] is None and not leaf["attempts"]
    ]
    commands: list[tuple[str, dict[str, Any]]] = []
    if existing is None:
        commands.append(("declare_candidate", {"key": key}))
    if revised:
        commands.append(("revise_proposal", {"candidate": identity, "proposal": proposal}))
    if len(commands) + len(unsealed) > session.store.headroom(session.current):
        raise _error("EPISODE_TOO_LARGE", "episode transition cap reached")
    for leaf in unsealed:
        proposed = _seal(session.vault_root, leaf)
        commands.append(
            (
                "bind_curation_leaf",
                {
                    "candidate": identity,
                    "leaf": leaf["leaf_id"],
                    "run_id": proposed["run_id"],
                    "plan_id": proposed["plan_id"],
                    "plan_fingerprint": proposed["plan_fingerprint"],
                    "ordinal": 0,
                },
            )
        )
    if commands:
        session.transitions(commands)
    return _projection(session)


def disposition(
    vault_root: Path, *, episode: Any, candidate: Any, disposition: Any, reason: Any
) -> dict[str, Any]:
    session = _Session(vault_root, episode)
    key = model._string(candidate, "candidate_key", 160)  # noqa: SLF001
    existing = _candidate_by_key(session.state, key)
    if existing is None:
        raise _error("EPISODE_CANDIDATE_UNKNOWN", "candidate is not part of this episode")
    session.transition(
        "set_disposition",
        candidate=existing["candidate_id"],
        disposition=disposition,
        reason=reason,
    )
    return _projection(session)


def _refused(key: str) -> dict[str, Any]:
    return {
        "operation": "episode_workflow",
        "action": "resume",
        "episode": key,
        "status": "refused",
        "code": DISABLED_CODE,
        "reason": (
            "episode leaf execution is disabled on this service; its operator enables "
            f"it with {ENABLE_ENV}. Inspect, prepare and disposition remain available."
        ),
        "executed": [],
        "reconciled": [],
        "blocked": [],
    }


def _leaves(state: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (candidate["candidate_id"], leaf)
        for candidate in state["candidates"]
        for leaf in candidate["leaves"]
    ]


def _runnable(state: Mapping[str, Any]) -> list[tuple[str, str]]:
    result = []
    for candidate in state["candidates"]:
        disposition = candidate["disposition"]
        if disposition is None or disposition["value"] != "routed":
            continue
        for leaf in candidate["leaves"]:
            if leaf["outcome"] in _EXECUTABLE and leaf["binding"] is not None:
                result.append((candidate["candidate_id"], leaf["leaf_id"]))
    return result


def _order(state: Mapping[str, Any], order: Sequence[str] | None) -> list[tuple[str, str]]:
    """The runnable leaves, in the caller's order when it gives one.

    `order` may name any leaf of this episode; a committed, uncertain or
    unrouted one is simply not run again.
    """
    runnable = _runnable(state)
    if order is None:
        return runnable
    known = {leaf["leaf_id"] for _candidate_id, leaf in _leaves(state)}
    if (
        isinstance(order, (str, bytes))
        or not isinstance(order, Sequence)
        or len(order) > MAX_LEAVES
        or len(set(order)) != len(order)
        or any(item not in known for item in order)
    ):
        raise _error("EPISODE_ORDER_INVALID", "order must list distinct leaf ids of this episode")
    by_leaf = {leaf: (candidate, leaf) for candidate, leaf in runnable}
    return [by_leaf[item] for item in order if item in by_leaf]


def _reconcile_uncertain(session: _Session) -> tuple[list[dict], list[dict]]:
    """Reconcile every uncertain leaf from the existing curation receipts only."""
    reconciled, blocked = [], []
    for candidate_id, leaf in _leaves(session.state):
        if leaf["outcome"] != "uncertain":
            continue
        try:
            session.transition(
                "reconcile_curation_leaf", candidate=candidate_id, leaf=leaf["leaf_id"]
            )
        except model.EpisodeError as error:
            if error.code != "EPISODE_OUTCOME_UNCERTAIN":
                raise
            blocked.append({"leaf_id": leaf["leaf_id"], "code": error.code})
            continue
        reconciled.append({"leaf_id": leaf["leaf_id"], "outcome": "committed"})
    return reconciled, blocked


def _execute(session: _Session, candidate_id: str, leaf_id: str) -> dict[str, Any]:
    leaf = model._owned(session.state, candidate_id, leaf_id)  # noqa: SLF001
    binding = leaf["binding"]
    snapshot = session.state["current_precommit"]["snapshot"]
    # Durably uncertain before the writer runs: a crash from here on can only
    # be reconciled from receipts, never retried under a fresh identity.
    session.transition("mark_attempt_started", candidate=candidate_id, leaf=leaf_id)
    store = curation.CurationStore(session.vault_root)
    if store.approval_path(binding["run_id"]).exists():
        result = curation.resume(
            session.vault_root, run_id=binding["run_id"], plan_id=binding["plan_id"]
        )
    else:
        result = curation.apply(
            session.vault_root,
            run_id=binding["run_id"],
            plan_id=binding["plan_id"],
            expected_plan_fingerprint=binding["plan_fingerprint"],
            why=(
                f"Episode {session.state['episode_id'][:12]} leaf {leaf_id[:12]} under "
                f"precommit {snapshot[:12]}."
            ),
        )
    session.transition("reconcile_curation_leaf", candidate=candidate_id, leaf=leaf_id)
    step = result.get("step") if isinstance(result, Mapping) else None
    return {
        "leaf_id": leaf_id,
        "operation_id": binding["operation_id"],
        "outcome": "committed",
        "path": step.get("path") if isinstance(step, Mapping) else None,
    }


def resume(
    vault_root: Path,
    *,
    episode: Any,
    journal_digest: Any,
    input_revision: Any,
    order: Sequence[str] | None = None,
    max_leaves: Any = None,
    postcommit: bool = False,
) -> dict[str, Any]:
    """Reconcile, attest and execute this episode's remaining routed leaves.

    Refused with `episode_workflow_disabled`, before anything is read or
    written, unless the service enables execution. Otherwise it acts only on
    the journal state the caller last reviewed: a `journal_digest` other than
    the current one is refused before any reconcile or attestation.
    """
    key = _key(episode)
    if not enabled():
        EpisodeInputOwner(Path(vault_root))._owner()  # noqa: SLF001 - fail closed first
        return _refused(key)
    if type(input_revision) is not int:
        raise _error("EPISODE_WORKFLOW_INVALID", "resume needs the reviewed input_revision")
    if not isinstance(journal_digest, str):
        raise _error("EPISODE_WORKFLOW_INVALID", "resume needs the reviewed journal_digest")
    limit = DEFAULT_MAX_LEAVES if max_leaves is None else max_leaves
    if type(limit) is not int or not 1 <= limit <= MAX_LEAVES:
        raise _error("EPISODE_WORKFLOW_INVALID", f"max_leaves must be 1 through {MAX_LEAVES}")
    session = _Session(vault_root, key)
    if journal_digest != session.current["journal_digest"]:
        raise _error(
            "EPISODE_REVISION_CONFLICT", "the episode changed after the caller's last review"
        )
    if input_revision != session.state["input_revisions"][-1]["revision"]:
        raise _error("EPISODE_INPUT_REVISION_STALE", "resume must review the current input")

    reconciled, blocked = _reconcile_uncertain(session)
    executed: list[dict[str, Any]] = []
    if postcommit:
        committed = [
            leaf["leaf_id"] for _c, leaf in _leaves(session.state) if leaf["outcome"] == "committed"
        ]
        session.transition(
            "attest_postcommit", input_revision=input_revision, leaf_ids=sorted(committed)
        )
        deferred = 0
    else:
        planned = _order(session.state, order)
        if planned or session.state["current_precommit"] is None:
            if (session.state["current_precommit"] or {}).get("input_revision") != input_revision:
                session.transition("attest_precommit", input_revision=input_revision)
        frozen = {item["leaf_id"] for item in blocked}
        deferred = max(0, len(planned) - limit)
        for candidate_id, leaf_id in planned[:limit]:
            owner = model._candidate(session.state, candidate_id)  # noqa: SLF001
            if any(leaf["leaf_id"] in frozen for leaf in owner["leaves"]):
                continue
            try:
                executed.append(_execute(session, candidate_id, leaf_id))
            except (curation.CurationError, model.EpisodeError) as error:
                # A leaf past its attempt mark stays uncertain: a failure is
                # not proof of non-commit.
                session.current = session.store.read(session.identity)
                blocked.append({"leaf_id": leaf_id, "code": error.code})
                break
    projection = _projection(session)
    return {
        **projection,
        "action": "resume",
        "status": "ok" if not blocked else "blocked",
        "executed": executed,
        "reconciled": reconciled,
        "blocked": blocked,
        "deferred": deferred,
        # Writers schedule their own projections; this call never claims a
        # published graph or index is current.
        "publication": "pending" if executed else "unchanged",
    }
