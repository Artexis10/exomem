"""The no-nudge observation record (close-memory-loop task 1.4).

A capture-to-activation run is judged on what an ordinary agent does without
being told, so the record keeps five things apart that a forced test would
blur together:

* **host initiation** -- the client and its observed lifecycle support, the
  exact turns the actor received, every product-owned ask (server
  instructions, an installed hook, the ``episode_due`` advisory), every
  harness intervention and every user reminder;
* **agent decisions** -- activation, decomposition (with the partition's four
  axes), destination, disposition and the pre/postcommit reviews, each with
  who initiated it;
* **leaf effects** -- what a writer actually committed, with its readback;
* **publication** -- whether derived state was current before the later
  session;
* **later response usefulness** -- a fresh session's answer, how its
  activation was initiated, any reminder the user had to send, and its grade.

:func:`evaluate` returns one verdict per part and a conjunctive
``ordinary_agent_acceptance`` gate. There is no score. Forced plumbing is
still reported -- a scripted run that commits the right effects passes
``leaf_effects`` -- but a harness intervention, a harness- or user-initiated
decision or effect, a user reminder, or a turn the fixture did not declare
fails ``ordinary_initiation``, and with it the gate. That separation is the
task's verify clause: forced-call tests cannot satisfy ordinary-agent
initiation acceptance.

A product hook is legitimate host initiation on a lifecycle-enforced client
(it may start activation), never a semantic decider: decomposition,
destination, disposition and review decisions must be the agent's own.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field, model_validator

from ..snapshot import StrictModel

ARTIFACT_TYPE = "memory-loop-observation.v1"
_SHA256 = r"^[0-9a-f]{64}$"

Initiator = Literal["agent", "product_hook", "harness", "user"]
Lifecycle = Literal["lifecycle_enforced", "best_effort"]
InputOrigin = Literal["synthetic_fixture", "original_private", "reconstructed"]
Phase = Literal[
    "activation",
    "decomposition",
    "destination",
    "disposition",
    "precommit_review",
    "postcommit_review",
]
Disposition = Literal[
    "routed", "no_capture", "uncertain", "rejected", "deferred", "awaiting_authority"
]
Outcome = Literal["pass", "fail", "unmeasured", "not_applicable"]

#: Every way a benchmark harness can reach a correct end state without the
#: agent choosing to: a tool call it mandated, a packet it injected, a write it
#: scripted, an instruction it added, or a turn it forced into the conversation.
HARNESS_INTERVENTION_KINDS: tuple[str, ...] = (
    "mandated_tool_call",
    "injected_packet",
    "scripted_write",
    "system_instruction",
    "forced_turn",
)
#: The product's own asks. They are host initiation, not harness forcing: the
#: server instructions and advisories reach every client, the hooks only a
#: client that actually runs them.
PRODUCT_PROMPT_KINDS: tuple[str, ...] = (
    "server_instructions",
    "bootstrap",
    "skill_guidance",
    "episode_due_advisory",
    "activation_hook",
    "stop_hook_checkpoint",
)
_HOOK_PROMPTS = frozenset({"activation_hook", "stop_hook_checkpoint"})
#: Phases only the agent may decide. A hook may start activation; it never
#: decomposes, routes, disposes or reviews.
_SEMANTIC_PHASES = frozenset(
    {"decomposition", "destination", "disposition", "precommit_review", "postcommit_review"}
)
EFFECT_KINDS: tuple[str, ...] = (
    "create_note",
    "create_entity",
    "edit_page",
    "add_unit",
    "accept_relation",
    "append_record",
    "create_source",
    "create_evidence",
    "register_type",
    "register_relation",
)


def turns_sha256(turns: Iterable[str]) -> str:
    """Digest of the exact ordered turns an actor received."""

    encoded = json.dumps(list(turns), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# --------------------------------------------------------------------------- #
# The record
# --------------------------------------------------------------------------- #


class ClientIdentity(StrictModel):
    client: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    #: Observed lifecycle support, never inferred from the client's brand.
    lifecycle: Lifecycle


class ProductPrompt(StrictModel):
    kind: Literal[
        "server_instructions",
        "bootstrap",
        "skill_guidance",
        "episode_due_advisory",
        "activation_hook",
        "stop_hook_checkpoint",
    ]
    detail: str = Field(min_length=1, max_length=500)


class HarnessIntervention(StrictModel):
    kind: Literal[
        "mandated_tool_call", "injected_packet", "scripted_write", "system_instruction", "forced_turn"
    ]
    detail: str = Field(min_length=1, max_length=500)


class HostInitiation(StrictModel):
    client: ClientIdentity
    input_origin: InputOrigin
    delivered_turns_sha256: str = Field(pattern=_SHA256)
    user_reminders: tuple[str, ...] = ()
    product_prompts: tuple[ProductPrompt, ...] = ()
    harness_interventions: tuple[HarnessIntervention, ...] = ()

    @model_validator(mode="after")
    def _hooks_need_a_lifecycle(self) -> "HostInitiation":
        if self.client.lifecycle != "lifecycle_enforced" and any(
            prompt.kind in _HOOK_PROMPTS for prompt in self.product_prompts
        ):
            raise ValueError("a hook prompt requires an observed lifecycle_enforced client")
        return self


class Partition(StrictModel):
    """The agent's own labels; the evaluator compares identities, not wording."""

    partition_id: str = Field(min_length=1, max_length=160)
    retrieval_question: str = Field(min_length=1, max_length=500)
    subject: str = Field(min_length=1, max_length=200)
    temporal_episode: str = Field(min_length=1, max_length=200)
    epistemic_role: str = Field(min_length=1, max_length=200)


class AgentDecision(StrictModel):
    seq: int = Field(ge=1)
    phase: Phase
    initiator: Initiator
    candidate_key: str | None = Field(default=None, min_length=1, max_length=160)
    partition: Partition | None = None
    route: str | None = None
    target: str | None = None
    title: str | None = None
    disposition: Disposition | None = None
    #: The candidate's statement as the agent authored it (title, reason and
    #: leaf content); what the evaluator's model-free matcher reads.
    text: str = Field(default="", max_length=8000)
    reason: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def _phase_fields(self) -> "AgentDecision":
        candidate_phases = {"decomposition", "destination", "disposition"}
        if self.phase in candidate_phases and self.candidate_key is None:
            raise ValueError(f"{self.phase} decision needs a candidate_key")
        if (self.phase == "decomposition") != (self.partition is not None):
            raise ValueError("a partition belongs to exactly the decomposition phase")
        if self.phase == "destination":
            if not self.route:
                raise ValueError("a destination decision needs a route")
            if (self.target is None) == (self.title is None):
                raise ValueError("a destination names exactly one existing target or new title")
        elif self.route is not None or self.target is not None or self.title is not None:
            raise ValueError("route, target and title belong to the destination phase")
        if (self.phase == "disposition") != (self.disposition is not None):
            raise ValueError("a disposition belongs to exactly the disposition phase")
        return self


class Unit(StrictModel):
    category: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=4000)


class Edge(StrictModel):
    relation: str = Field(min_length=1, max_length=120)
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    #: Refs of the Sources, Evidence or episode the edge was authored from.
    evidence: tuple[str, ...] = ()
    #: Whether the relation id is a published, active definition at readback.
    active: bool = True


class RecordItem(StrictModel):
    collection: str = Field(min_length=1)
    item_key: str = Field(min_length=1)
    fields: dict[str, str]


class Readback(StrictModel):
    """Canonical readback of the effect's page, as the writer left it."""

    page_type: str | None = None
    entity_type: str | None = None
    title: str | None = None
    aliases: tuple[str, ...] = ()
    created: bool = False
    units: tuple[Unit, ...] = ()
    edges: tuple[Edge, ...] = ()
    record_item: RecordItem | None = None
    sources: tuple[str, ...] = ()


class LeafEffect(StrictModel):
    seq: int = Field(ge=1)
    candidate_key: str = Field(min_length=1, max_length=160)
    initiator: Initiator
    kind: Literal[
        "create_note",
        "create_entity",
        "edit_page",
        "add_unit",
        "accept_relation",
        "append_record",
        "create_source",
        "create_evidence",
        "register_type",
        "register_relation",
    ]
    path: str = Field(min_length=1)
    outcome: Literal["committed", "replayed", "uncertain", "stale", "diverged", "refused"]
    receipt_sha256: str | None = Field(default=None, pattern=_SHA256)
    readback: Readback | None = None


class Publication(StrictModel):
    status: Literal["current", "pending", "warming", "unavailable", "not_observed"]
    seq: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _observed_has_seq(self) -> "Publication":
        if (self.status == "not_observed") != (self.seq is None):
            raise ValueError("an observed publication status carries its seq; not_observed has none")
        return self


class LaterResponse(StrictModel):
    seq: int = Field(ge=1)
    fresh_session: bool
    turn_sha256: str = Field(pattern=_SHA256)
    activation_initiator: Literal["agent", "product_hook", "harness", "none"]
    reminder_turns: tuple[str, ...] = ()
    response_text: str = Field(max_length=20000)
    usefulness_grade: Literal["useful", "partial", "not_useful", "ungraded"]
    grader: Literal["blind_rubric", "owner", "none"]

    @model_validator(mode="after")
    def _grade_has_grader(self) -> "LaterResponse":
        if (self.usefulness_grade == "ungraded") != (self.grader == "none"):
            raise ValueError("a usefulness grade needs a grader; ungraded has none")
        return self


class NoNudgeObservation(StrictModel):
    artifact_type: Literal["memory-loop-observation.v1"]
    schema_version: Literal[1]
    fixture_id: str = Field(min_length=1)
    #: Digests the fixture froze before the run; a mismatch voids the run.
    actor_sha256: str = Field(pattern=_SHA256)
    pre_capture_sha256: str = Field(pattern=_SHA256)
    evaluator_sha256: str = Field(pattern=_SHA256)
    host_initiation: HostInitiation
    agent_decisions: tuple[AgentDecision, ...]
    leaf_effects: tuple[LeafEffect, ...]
    publication: Publication
    later_response: LaterResponse | None = None

    @model_validator(mode="after")
    def _ordering(self) -> "NoNudgeObservation":
        seqs = [item.seq for item in self.agent_decisions] + [item.seq for item in self.leaf_effects]
        if self.publication.seq is not None:
            seqs.append(self.publication.seq)
        if self.later_response is not None:
            seqs.append(self.later_response.seq)
        if len(seqs) != len(set(seqs)):
            raise ValueError("seq values must be unique across the record")
        committed = [
            effect.seq for effect in self.leaf_effects if effect.outcome in {"committed", "replayed"}
        ]
        last_effect = max((effect.seq for effect in self.leaf_effects), default=0)
        if self.publication.seq is not None and committed and self.publication.seq < max(committed):
            raise ValueError("publication is observed after the last committed effect it covers")
        if self.later_response is not None:
            if self.later_response.seq <= last_effect:
                raise ValueError("the later response comes after every leaf effect")
            if self.publication.seq is not None and self.later_response.seq <= self.publication.seq:
                raise ValueError("the later response comes after the observed publication")
        return self


def load_observation(data: Mapping[str, Any]) -> NoNudgeObservation:
    return NoNudgeObservation.model_validate(dict(data), strict=False)


def dump_observation_json(record: NoNudgeObservation) -> str:
    return json.dumps(record.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def load_observation_json(text: str) -> NoNudgeObservation:
    return load_observation(json.loads(text))


# --------------------------------------------------------------------------- #
# Verdicts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Verdict:
    outcome: Outcome
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ObservationReport:
    initiation_class: Lifecycle
    ordinary_initiation: Verdict
    agent_decisions: Verdict
    leaf_effects: Verdict
    publication: Verdict
    later_usefulness: Verdict
    exact_private_replay: Verdict
    ordinary_agent_acceptance: Verdict


def _verdict(reasons: list[str]) -> Verdict:
    return Verdict("fail", tuple(reasons)) if reasons else Verdict("pass")


def _decision_trace(record: NoNudgeObservation) -> list[str]:
    """Every agent-executed effect follows the agent's own routed disposition."""

    reasons: list[str] = []
    routed_at: dict[str, int] = {}
    for decision in record.agent_decisions:
        if decision.phase == "disposition" and decision.disposition == "routed" and decision.initiator == "agent":
            routed_at.setdefault(decision.candidate_key or "", decision.seq)
    if not any(decision.initiator == "agent" for decision in record.agent_decisions):
        reasons.append("no decision was initiated by the agent")
    for effect in record.leaf_effects:
        seq = routed_at.get(effect.candidate_key)
        if seq is None or seq > effect.seq:
            reasons.append(
                f"effect {effect.seq} for candidate {effect.candidate_key!r} has no earlier "
                "agent-routed disposition"
            )
    return reasons


def _initiation(record: NoNudgeObservation, expected_turns_sha256: str) -> Verdict:
    host = record.host_initiation
    reasons: list[str] = []
    if host.delivered_turns_sha256 != expected_turns_sha256:
        reasons.append("delivered turns differ from the fixture's declared actor input")
    reasons.extend(f"user reminder: {turn!r}" for turn in host.user_reminders)
    reasons.extend(
        f"harness intervention {item.kind}: {item.detail}" for item in host.harness_interventions
    )
    for decision in record.agent_decisions:
        if decision.initiator in {"harness", "user"}:
            reasons.append(f"{decision.phase} decision {decision.seq} initiated by {decision.initiator}")
        elif decision.phase in _SEMANTIC_PHASES and decision.initiator != "agent":
            reasons.append(
                f"{decision.phase} decision {decision.seq} initiated by {decision.initiator}; "
                "only the agent decides meaning"
            )
    for effect in record.leaf_effects:
        if effect.initiator != "agent":
            reasons.append(f"effect {effect.seq} executed by {effect.initiator}")
    reasons.extend(_decision_trace(record))
    return _verdict(reasons)


def _effects(record: NoNudgeObservation) -> Verdict:
    """Transport: every observed effect committed or replayed from its receipt."""

    if not record.leaf_effects:
        return Verdict("unmeasured", ("no leaf effect was observed",))
    reasons = [
        f"effect {effect.seq} ({effect.candidate_key}) is {effect.outcome}"
        for effect in record.leaf_effects
        if effect.outcome not in {"committed", "replayed"}
    ]
    reasons.extend(
        f"effect {effect.seq} ({effect.candidate_key}) has no receipt"
        for effect in record.leaf_effects
        if effect.outcome in {"committed", "replayed"} and effect.receipt_sha256 is None
    )
    return _verdict(reasons)


def _publication(record: NoNudgeObservation) -> Verdict:
    status = record.publication.status
    if status == "not_observed":
        return Verdict("unmeasured", ("publication was not observed",))
    if status != "current":
        return Verdict("fail", (f"publication was {status} before the later session",))
    return Verdict("pass")


def _later(record: NoNudgeObservation) -> Verdict:
    later = record.later_response
    if later is None:
        return Verdict("unmeasured", ("no later fresh-session response was observed",))
    reasons: list[str] = []
    if not later.fresh_session:
        reasons.append("the later response was not in a fresh session")
    if later.activation_initiator == "harness":
        reasons.append("the later activation was forced by the harness")
    reasons.extend(f"later reminder: {turn!r}" for turn in later.reminder_turns)
    if reasons:
        return Verdict("fail", tuple(reasons))
    if later.usefulness_grade == "ungraded":
        return Verdict("unmeasured", ("the later response is ungraded",))
    if later.usefulness_grade != "useful":
        return Verdict("fail", (f"the later response was graded {later.usefulness_grade}",))
    return Verdict("pass")


def _exact_replay(record: NoNudgeObservation) -> Verdict:
    origin = record.host_initiation.input_origin
    if origin == "synthetic_fixture":
        return Verdict("not_applicable", ("a synthetic fixture is not a private replay",))
    if origin == "reconstructed":
        return Verdict(
            "unmeasured",
            ("a reconstructed input cannot establish exact replay of the original episode",),
        )
    return Verdict("pass")


def evaluate(record: NoNudgeObservation, *, expected_turns_sha256: str) -> ObservationReport:
    """One verdict per part of the record, and the conjunctive acceptance gate."""

    initiation = _initiation(record, expected_turns_sha256)
    decisions = _verdict(_decision_trace(record))
    effects = _effects(record)
    publication = _publication(record)
    later = _later(record)
    parts = {
        "ordinary_initiation": initiation,
        "agent_decisions": decisions,
        "leaf_effects": effects,
        "publication": publication,
        "later_usefulness": later,
    }
    failing = [name for name, verdict in parts.items() if verdict.outcome != "pass"]
    if not failing:
        acceptance = Verdict("pass")
    elif any(parts[name].outcome == "fail" for name in failing):
        acceptance = Verdict("fail", tuple(f"{name} did not pass" for name in failing))
    else:
        acceptance = Verdict("unmeasured", tuple(f"{name} is unmeasured" for name in failing))
    return ObservationReport(
        initiation_class=record.host_initiation.client.lifecycle,
        ordinary_initiation=initiation,
        agent_decisions=decisions,
        leaf_effects=effects,
        publication=publication,
        later_usefulness=later,
        exact_private_replay=_exact_replay(record),
        ordinary_agent_acceptance=acceptance,
    )


def report_to_dict(report: ObservationReport) -> dict[str, Any]:
    names = (
        "ordinary_initiation",
        "agent_decisions",
        "leaf_effects",
        "publication",
        "later_usefulness",
        "exact_private_replay",
        "ordinary_agent_acceptance",
    )
    return {
        "initiation_class": report.initiation_class,
        "verdicts": {
            name: {
                "outcome": getattr(report, name).outcome,
                "reasons": list(getattr(report, name).reasons),
            }
            for name in names
        },
    }
