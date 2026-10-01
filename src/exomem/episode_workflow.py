"""Typed episode candidate operations over existing curation leaves.

The active agent decides every candidate, destination and disposition; this
module validates, records and (only when enabled) executes them. A candidate
names a typed destination for an existing writer -- one closed curation step
of a kind its route owns (fields checked by `curation.validate_forward_plan`)
-- never a free-form effect. The `records` route's `append-record` leaf is the
one step that reaches the Records tree, through the Records writer; only this
module's seal admits it.

`resume` `postcommit` attests current coverage as a chain per path (see
`episode_reconciliation.current_coverage`): each earlier leaf's recorded
result must be the next leaf's recorded start, and only the last leaf on a
path answers to the live page.

What each action writes (close-memory-loop task 5.5):

* `inspect` reads the caller's own episode ledger. It writes nothing. Its
  `coverage` block says what was attempted, what is pending and what comes
  next, for a host checkpoint that must not treat a write as completion.
* `coverage` reads the final pass's evidence -- the input ref and each
  committed leaf's receipt and current readback -- and writes nothing.
* `prepare` and `disposition` write only the caller's audience-bound episode
  journal and inert sealed single-step curation plans. Preparation runs the
  same read-only leaf preparation `maintain_memory mode=curation` propose runs;
  no canonical page is written. Revising a proposal withdraws its disposition.
  A proposal's destination decision -- route, home, the alternatives the agent
  inspected with the version it read of each, and its reason -- is checked for
  structure only (task 3.8): each alternative is a page this caller can read at
  that version, and an existing-page home is the one page its leaves write.
* `resume` is the episode's executor. It acts only on the journal digest its
  caller last reviewed, and runs only a leaf that is routed, bound to a current
  sealed plan, covered by a current precommit attestation and not already
  attempted, through `curation.apply` -- the existing executor, under the
  command's writer lease and each writer's own validation. It never retries an
  uncertain attempt: reconciliation reads existing receipts only. A candidate
  whose inspected pages changed after its decision is reported stale, with no
  attempt, until the agent reconsiders it (task 3.9).

`EXOMEM_EPISODE_WORKFLOW` is a feature switch for that executor, not an
authority boundary. Unless the service environment sets it, `resume` refuses
with `episode_workflow_disabled` before reading or writing anything. A sealed
leaf plan is an ordinary curation run, which the same caller can apply through
`maintain_memory mode=curation apply` whatever the switch says; the episode
operations grant nothing that apply does not already grant that caller and
mint no vocabulary or edge authority.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import curation, episode_capture, memory_refs
from . import episode_model as model
from .episode_reconciliation import current_coverage, reconcile_curation_leaf
from .episode_recovery import EpisodeInputOwner
from .episode_store import EpisodeStore
from .governance import egress
from .governance.principal import effective_principal
from .vault import content_hash, parse_frontmatter

ENABLE_ENV = "EXOMEM_EPISODE_WORKFLOW"
DISABLED_CODE = "episode_workflow_disabled"
DEFAULT_MAX_LEAVES = 8
MAX_LEAVES = 16
_EXECUTABLE = frozenset({"pending", "proven_uncommitted"})
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
#: Routes whose home is one existing page, and the arg each owned leaf kind
#: names that page by.
_BOUND_ROUTES = frozenset({"existing_page", "semantic_unit"})
_LEAF_TARGET = {"edit": "path", "supersede": "old_path"}
DESTINATION_STALE = "EPISODE_DESTINATION_STALE"
_UNAVAILABLE = (
    "EPISODE_DESTINATION_UNAVAILABLE",
    "a destination or inspected alternative is not a page this caller can read",
)
_STALE = (
    DESTINATION_STALE,
    "a page this decision inspected changed after it was read; read it again and revise",
)
_MISMATCH = (
    "EPISODE_DESTINATION_MISMATCH",
    "an existing-page destination's leaves must write the page it names",
)


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


def _coverage(state: Mapping[str, Any]) -> dict[str, Any]:
    """What this episode attempted, what is pending and what comes next (task 4.1).

    A host checkpoint reads this instead of treating a successful write as
    completion. `next` names the agent's next step: `decide` a candidate with
    no disposition at the current input revision, `resume` a routed leaf not
    yet committed (or still uncertain), `attest` committed results the last
    postcommit attestation did not review, else `none` -- which deferred or
    awaiting-authority work may still leave pending. Coverage rests on the
    agent's attestation against its input: the server never claims the
    candidates exhaust it.
    """
    current = state["input_revisions"][-1]["revision"]
    candidates = state["candidates"]
    leaves = [(candidate, leaf) for candidate in candidates for leaf in candidate["leaves"]]
    committed = {leaf["leaf_id"] for _candidate, leaf in leaves if leaf["outcome"] == "committed"}
    attestations = state["postcommit_attestations"]
    if not candidates:
        step = "none"
    elif any(
        item["disposition"] is None or item["disposition"]["input_revision"] != current
        for item in candidates
    ):
        step = "decide"
    elif any(
        candidate["disposition"]["value"] == "routed" and leaf["outcome"] != "committed"
        for candidate, leaf in leaves
    ):
        step = "resume"
    elif (
        state["reviewed_through_input_revision"] != current
        or not attestations
        or set(attestations[-1]["leaf_ids"]) != committed
    ):
        step = "attest"
    else:
        step = "none"
    return {
        "attempted": sum(
            1
            for _candidate, leaf in leaves
            if leaf["attempts"] or any(item["attempts"] for item in leaf["effect_history"])
        ),
        "pending": len(model._pending(state)),  # noqa: SLF001
        "covered_through_input_revision": state["covered_through_input_revision"],
        "historically_covered_through": max(
            (item["input_revision"] for item in attestations if not item["pending"]),
            default=None,
        ),
        "next": step,
        "basis": "agent_attestation",
    }


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
        "coverage": _coverage(state),
        "coverage_current": "unchecked",
        "execution": "enabled" if enabled() else "disabled",
    }


def inspect(vault_root: Path, *, episode: Any) -> dict[str, Any]:
    return _projection(_Session(vault_root, episode))


# --- the final coverage pass (close-memory-loop 4.2) --------------------------


def _written_path(vault_root: Path, binding: Mapping[str, Any]) -> str | None:
    """The page a committed leaf's sealed plan names as its postcondition."""
    try:
        plan = curation.CurationStore(vault_root).load_plan(binding["run_id"])
        item = plan["binding_manifest"][binding["ordinal"]]
    except (curation.CurationError, KeyError, IndexError, TypeError):
        return None
    post = item.get("postcondition") if isinstance(item, Mapping) else None
    path = post.get("path") if isinstance(post, Mapping) else None
    return path if isinstance(path, str) else None


#: Distinct candidates landing on one page before the pass reports it.
SINK_CLUSTERS = 3
#: A page of these kinds legitimately gathers many candidates: an entity, a
#: production log, or a page tagged as a hub.
SINK_EXEMPT_TYPES = frozenset({"entity", "production-log"})
SINK_EXEMPT_TAGS = frozenset({"hub"})
SINK_GUIDANCE = (
    "Each page in `sink` received effects from the distinct candidates listed "
    "with it. Keep them here when this is their canonical page. Otherwise give "
    "each candidate a disposition: route it to an existing canonical page, "
    "entity, Planning or Records item, or to a justified new page, or mark it "
    "no_capture when nothing durable remains. Guidance, not a block."
)


def _page_kind(vault_root: Path, path: str) -> tuple[str | None, bool]:
    """The page's `type` and whether it is exempt from sink flagging."""
    try:
        text = (vault_root / path).read_text(encoding="utf-8")
        frontmatter, _body, _raw = parse_frontmatter(text)
    except (OSError, UnicodeError, ValueError):
        return None, False
    page_type = frontmatter.get("type")
    page_type = page_type if isinstance(page_type, str) else None
    tags = frontmatter.get("tags")
    tagged = isinstance(tags, list) and any(t in SINK_EXEMPT_TAGS for t in tags)
    return page_type, page_type in SINK_EXEMPT_TYPES or tagged


def _sinks(vault_root: Path, receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pages that several distinct candidates' committed effects landed on.

    Structural only: it counts distinct candidates per page and names the page
    type; it never judges topics. Entity, production-log and hub pages, which
    are meant to gather many candidates, are not reported.
    """
    by_path: dict[str, set[str]] = {}
    for item in receipts:
        if item["path"] is not None:
            by_path.setdefault(item["path"], set()).add(item["candidate_key"])
    sinks = []
    for path, keys in sorted(by_path.items()):
        if len(keys) < SINK_CLUSTERS:
            continue
        page_type, exempt = _page_kind(vault_root, path)
        if not exempt:
            sinks.append(
                {
                    "path": path,
                    "type": page_type,
                    "distinct_candidates": len(keys),
                    "candidates": sorted(keys),
                }
            )
    return sinks


def coverage(vault_root: Path, *, episode: Any) -> dict[str, Any]:
    """The evidence for the agent's final coverage pass. Read-only.

    The pass is the active agent's, separate from the precommit destination
    review: it compares its dispositions with the episode's current input --
    read through `read_memory` at `input.ref`, under the ordinary release
    checks -- and with what each committed leaf left, a receipt and a readback
    reverified now against the live page. It attests with `resume`
    `postcommit=true`. A page the caller may no longer read reads back
    `unavailable`, exactly like a page that is gone. Nothing here judges
    whether the candidates exhaust the input. `sink` names any page that
    several distinct candidates landed on, with guidance to disposition each
    candidate; it never blocks attestation.
    """
    session = _Session(vault_root, episode)
    state = session.state
    latest = state["input_revisions"][-1]
    ref = latest["evidence"].get("reference")
    page = _page_ref(ref)
    if page is None or page not in _visible(session.vault_root, [page]):
        ref = None
    keep = egress.restricted_release_filter(session.vault_root, principal=effective_principal())
    receipts = []
    # One consistent view of every committed page, as an attestation takes.
    with session.store._guard():  # noqa: SLF001
        current = current_coverage(session.vault_root, state, keep=keep)
        for candidate in state["candidates"]:
            for leaf in candidate["leaves"]:
                if leaf["outcome"] != "committed":
                    continue
                path = _written_path(session.vault_root, leaf["binding"])
                if (
                    path is None
                    or not (session.vault_root / path).is_file()
                    or (keep is not None and not keep(path))
                ):
                    path, readback = None, "unavailable"
                elif current[leaf["leaf_id"]]:
                    readback = "verified"
                else:
                    readback = "changed"
                receipts.append(
                    {
                        "candidate_key": candidate["candidate_key"],
                        "leaf_id": leaf["leaf_id"],
                        "operation_id": leaf["binding"]["operation_id"],
                        "receipt_digest": leaf["outcome_proof"]["receipt_digest"],
                        "path": path,
                        "readback": readback,
                    }
                )
    sink = _sinks(session.vault_root, receipts)
    return {
        **_projection(session),
        "action": "coverage",
        "input": {
            "input_revision": latest["revision"],
            "ref": ref,
            "recovery": latest["recovery"] if ref is not None else "unavailable",
        },
        "receipts": receipts,
        "coverage_current": (
            "verified" if all(item["readback"] == "verified" for item in receipts) else "changed"
        ),
        "sink": sink,
        **({"sink_guidance": SINK_GUIDANCE} if sink else {}),
    }


def _prepare_seal(vault_root: Path, leaf: Mapping[str, Any]) -> curation.PreparedProposal:
    """Prepare one leaf's own single-step curation plan, sealing nothing.

    `curation.prepare_proposal` runs the leaf's existing read-only preparation
    against the current vault, so a new or revised effect is validated now;
    `prepare` seals only once every leaf has passed, and the sealed plan is
    inert until `resume` executes it.
    """
    return curation.prepare_proposal(
        vault_root,
        {
            "version": 1,
            "title": f"Episode leaf {leaf['leaf_key']}",
            "steps": [{"step_id": "leaf", "kind": leaf["kind"], "args": leaf["args"]}],
        },
        # The pure model has already held the leaf to its route's kinds, so
        # only a `records` candidate reaches here with a Records leaf.
        allow_records=True,
    )


def _committed(vault_root: Path, run_id: str) -> bool:
    """Whether this sealed plan's step already committed, here or elsewhere."""
    return bool(curation.CurationStore(vault_root).reconstruct(run_id)["committed_steps"])


def _blockers(vault_root: Path, run_id: str) -> list[str]:
    """Codes that would refuse an uncommitted sealed plan now, as `curation.preview`
    says for the current caller: a bound page it may not read is a missing one."""
    keep = egress.restricted_release_filter(vault_root, principal=effective_principal())
    return [
        item["code"]
        for item in curation.preview(vault_root, run_id=run_id, keep=keep)["blockers"]
    ]


# --- destination decisions (close-memory-loop 3.8) ---------------------------
#
# The home, the alternatives weighed and the reason are the active agent's
# judgment. These checks are structural only: every inspected alternative is a
# page this caller can read at the version the agent read, and an existing-page
# home is the one page its leaves write, so the leaf's own expected-hash guard
# protects the declared home. Nothing here scores, ranks or prefers a page, and
# a page the caller may not read is answered exactly as one that does not exist.


def _page_ref(value: Any) -> str | None:
    """The canonical page ref `value` spells, any unit fragment dropped, or None."""
    if not isinstance(value, str):
        return None
    parent = value.partition("#")[0]
    memory_id = memory_refs.parse_memory_ref(parent)
    if memory_id is None or memory_refs.memory_ref(memory_id) != parent:
        return None
    return parent


def _check_decision_shape(proposal: Mapping[str, Any]) -> None:
    """What a new decision must carry beyond what the pure model already checks."""
    for item in proposal["alternatives"]:
        if _page_ref(item["target"]) != item["target"] or not _HEX64.fullmatch(item["version"]):
            raise _error(
                "EPISODE_PROPOSAL_INVALID",
                "an alternative names a page by its memory ref and the content_hash read",
            )
    if proposal["route"] in _BOUND_ROUTES and "target" not in proposal:
        # Never inferred from the page the conversation has open.
        raise _error("EPISODE_PROPOSAL_INVALID", "an existing-page destination names its target")


def _destination_refs(proposal: Mapping[str, Any]) -> list[str]:
    refs = [item["target"] for item in proposal.get("alternatives", ())]
    if proposal["route"] in _BOUND_ROUTES and (ref := _page_ref(proposal.get("target"))):
        refs.append(ref)
    return refs


def _visible(vault_root: Path, refs: Sequence[str]) -> dict[str, str]:
    """One release-filtered lookup for every ref: `{ref: path}` for visible ones."""
    if not refs:
        return {}
    return egress.visible_memory_ref_paths(vault_root, refs, principal=effective_principal())


def _version(vault_root: Path, path: str) -> str | None:
    """The page's current content_hash, the one `read_memory` returns."""
    try:
        return content_hash((Path(vault_root) / path).read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError):
        return None


def _home(vault_root: Path, target: Any, visible: Mapping[str, str]) -> str | None:
    """The one readable page an existing-page destination names, else None."""
    ref = _page_ref(target)
    if ref is not None:
        return visible.get(ref)
    if not isinstance(target, str) or target.lower().startswith(memory_refs.REF_PREFIX):
        return None
    try:
        path = curation.normalize_target_path(target, field="target")
        exists = (Path(vault_root) / path).is_file()
    except (curation.CurationError, OSError):
        return None
    if not exists or egress.write_target_withheld(
        vault_root, path, principal=effective_principal()
    ):
        return None
    return path


def _destination_blocker(
    vault_root: Path,
    proposal: Mapping[str, Any],
    leaves: Sequence[Mapping[str, Any]],
    visible: Mapping[str, str],
) -> tuple[str, str] | None:
    """Why a recorded destination decision does not hold now, or None."""
    for item in proposal.get("alternatives", ()):
        path = visible.get(item["target"])
        if path is None:
            return _UNAVAILABLE
        if _version(vault_root, path) != item["version"]:
            return _STALE
    if proposal["route"] in _BOUND_ROUTES:
        home = _home(vault_root, proposal.get("target"), visible)
        if home is None:
            return _UNAVAILABLE
        if any(leaf["args"].get(_LEAF_TARGET.get(leaf["kind"], "")) != home for leaf in leaves):
            return _MISMATCH
    return None


def _stale_destinations(session: _Session, candidate_ids: set[str]) -> set[str]:
    """Candidates whose destination evidence changed since their decision."""
    owners = [model._candidate(session.state, item) for item in sorted(candidate_ids)]  # noqa: SLF001
    visible = _visible(
        session.vault_root,
        [ref for owner in owners for ref in _destination_refs(owner["proposal"])],
    )
    return {
        owner["candidate_id"]
        for owner in owners
        if _destination_blocker(session.vault_root, owner["proposal"], owner["leaves"], visible)
    }


def _unverifiable(session: _Session, candidate_id: str, leaf_id: str) -> str | None:
    """Why reconciling this leaf would fail now, from a read-only trial on a copy.

    The copy marks the attempt the pure model would mark and runs the same
    receipt and postimage verification its reconcile runs; nothing is recorded.
    """
    trial = model.mark_attempt_started(session.state, candidate_id, leaf_id)
    try:
        reconcile_curation_leaf(session.vault_root, trial, candidate_id, leaf_id)
    except model.EpisodeError as error:
        if error.code != "EPISODE_OUTCOME_UNCERTAIN":
            raise
        return error.code
    return None


def prepare(vault_root: Path, *, episode: Any, candidate: Any, proposal: Any) -> dict[str, Any]:
    """Declare or revise one candidate's typed proposal and bind its leaves.

    The proposal is validated by the pure model, and every new or revised leaf
    passes current preparation, before the journal accepts anything: a refused
    proposal or leaf leaves the episode unchanged. An unattempted leaf whose
    sealed plan has gone stale is sealed again against the current vault. The
    declaration, revision and bindings land in one journal write, so a refused
    bind leaves no half-bound candidate, and a journal without room for all of
    them refuses before any plan is sealed.
    """
    session = _Session(vault_root, episode)
    key = model._string(candidate, "candidate_key", 160)  # noqa: SLF001
    existing = _candidate_by_key(session.state, key)
    # Revision checks, then the decision's destination evidence, then sealing:
    # a refusal at any step leaves the journal unchanged.
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
    decided = model._candidate(trial, identity)  # noqa: SLF001
    _check_decision_shape(decided["proposal"])
    blocker = _destination_blocker(
        session.vault_root,
        decided["proposal"],
        decided["leaves"],
        _visible(session.vault_root, _destination_refs(decided["proposal"])),
    )
    if blocker is not None:
        raise _error(*blocker)
    unsealed = [
        leaf
        for leaf in decided["leaves"]
        if not leaf["attempts"]
        and (
            leaf["binding"] is None
            or (
                not _committed(session.vault_root, leaf["binding"]["run_id"])
                and _blockers(session.vault_root, leaf["binding"]["run_id"])
            )
        )
    ]
    commands: list[tuple[str, dict[str, Any]]] = []
    if existing is None:
        commands.append(("declare_candidate", {"key": key}))
    if revised:
        commands.append(("revise_proposal", {"candidate": identity, "proposal": proposal}))
    # A lower bound on what the journal takes: the commands plus each sealed
    # plan's step. Too little room refuses before any plan is sealed.
    transitions, room = session.store.room(session.identity)
    needed = len(model._json(commands).encode()) + sum(  # noqa: SLF001
        len(model._json(leaf["args"]).encode())  # noqa: SLF001
        for leaf in unsealed
    )
    if len(commands) + len(unsealed) > transitions or needed > room:
        raise _error("EPISODE_TOO_LARGE", "the episode journal has no room for this preparation")
    # Every leaf passes its preparation before any plan is sealed, so a refused
    # leaf leaves no sealed plan behind for its siblings.
    ready = [(leaf, _prepare_seal(session.vault_root, leaf)) for leaf in unsealed]
    for leaf, prepared in ready:
        proposed = curation.seal_proposal(session.vault_root, prepared, allow_records=True)
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
        "replayed": [],
        "stale": [],
        "diverged": [],
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
        if _refused_for_caller(session.vault_root, leaf["binding"]):
            # What a reconcile of a missing page gives: still uncertain.
            blocked.append({"leaf_id": leaf["leaf_id"], "code": "EPISODE_OUTCOME_UNCERTAIN"})
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


def _refused_for_caller(vault_root: Path, binding: Mapping[str, Any]) -> bool:
    """Whether this caller may no longer write what a sealed leaf writes.

    The leaf's own permission check, repeated for the current caller before
    any attempt: a Records leaf asks the Records owner for a released
    collection whose whole write set this caller can see; a page leaf asks the
    write doors' release check for every path its sealed binding touches.
    Unreadable evidence counts as refused. The caller answers a refusal exactly
    as it answers a missing target, so a target withheld after preparation is
    indistinguishable from one that is gone, and never costs an attempt.
    """
    from . import record_governance
    from . import structured_collections as collections
    from .vault import PathGuardError

    try:
        plan = curation.CurationStore(vault_root).load_plan(binding["run_id"])
        step = plan["steps"][binding["ordinal"]]
        item = plan["binding_manifest"][binding["ordinal"]]
        if step["kind"] == curation.RECORDS_STEP_KIND:
            manifest = record_governance.resolve_collection_for_mutation(
                vault_root, item["prepared"]["manifest_path"]
            )
            record_governance.require_mutation_visibility(vault_root, manifest)
            return False
        rows = [*(item.get("effect_before") or ()), *(item.get("effect_after") or ())]
        paths = {
            item.get("path"),
            (item.get("postcondition") or {}).get("path"),
            *(row.get("path") for row in rows if isinstance(row, Mapping)),
        }
    except (
        curation.CurationError,
        collections.CollectionError,
        PathGuardError,
        OSError,
        ValueError,
        KeyError,
        IndexError,
        TypeError,
        AttributeError,
    ):
        return True
    principal = effective_principal()
    return any(
        isinstance(path, str)
        and path
        and egress.write_target_withheld(vault_root, path, principal=principal)
        for path in paths
    )


def _execute(session: _Session, candidate_id: str, leaf_id: str) -> tuple[str, dict[str, Any]]:
    leaf = model._owned(session.state, candidate_id, leaf_id)  # noqa: SLF001
    binding = leaf["binding"]
    # Neither case records an attempt, so the agent can still re-prepare or
    # re-disposition the candidate. A plan committed elsewhere must still
    # reconcile, or its attempt would stay uncertain for good; any other plan
    # must still apply to the vault it would change. A target this caller may
    # no longer write answers exactly as a missing one does in each case: the
    # same trial or preview runs either way, with the same ordered blockers,
    # and the refusal only supplies the code a missing target would.
    refused = _refused_for_caller(session.vault_root, binding)
    if _committed(session.vault_root, binding["run_id"]):
        try:
            code = _unverifiable(session, candidate_id, leaf_id)
        except model.EpisodeError:
            if not refused:
                raise
            code = None
        if refused:
            code = "EPISODE_OUTCOME_UNCERTAIN"
        if code:
            return "diverged", {"leaf_id": leaf_id, "code": code}
    else:
        blockers = _blockers(session.vault_root, binding["run_id"])
        if refused or blockers:
            code = (blockers or ["CURATION_BINDING_STALE"])[0]
            return "stale", {"leaf_id": leaf_id, "code": code}
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
    # A plan already applied elsewhere (through `maintain_memory` curation
    # apply, say) is reconciled from its receipt; this pass wrote nothing.
    kind = "replayed" if curation.valid_replay_result(result) else "executed"
    return kind, {
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
    reported: dict[str, list[dict[str, Any]]] = {
        "executed": [],
        "replayed": [],
        "stale": [],
        "diverged": [],
    }
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
        held: set[str] = set()
        deferred = max(0, len(planned) - limit)
        # The precommit destination review: a decision whose inspected pages
        # changed since it was made waits for the agent's fresh consideration.
        reconsider = _stale_destinations(session, {candidate for candidate, _ in planned[:limit]})
        for candidate_id, leaf_id in planned[:limit]:
            owner = model._candidate(session.state, candidate_id)  # noqa: SLF001
            if candidate_id in held or any(leaf["leaf_id"] in frozen for leaf in owner["leaves"]):
                continue
            if candidate_id in reconsider:
                reported["stale"].append({"leaf_id": leaf_id, "code": DESTINATION_STALE})
                held.add(candidate_id)
                continue
            try:
                kind, item = _execute(session, candidate_id, leaf_id)
            except (curation.CurationError, model.EpisodeError) as error:
                # A leaf past its attempt mark stays uncertain: a failure is
                # not proof of non-commit.
                session.current = session.store.read(session.identity)
                blocked.append({"leaf_id": leaf_id, "code": error.code})
                break
            reported[kind].append(item)
            if kind in {"stale", "diverged"}:
                # The rest of its candidate waits for the owner's review.
                held.add(candidate_id)
    projection = _projection(session)
    status = "blocked" if blocked else "ok"
    if not blocked and (reported["stale"] or reported["diverged"]):
        status = "stale" if reported["stale"] else "diverged"
    return {
        **projection,
        "action": "resume",
        "status": status,
        **reported,
        "reconciled": reconciled,
        "blocked": blocked,
        "deferred": deferred,
        # Writers schedule their own projections; this call never claims a
        # published graph or index is current.
        "publication": "pending" if reported["executed"] else "unchanged",
    }
