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
import re
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
Outcome = Literal["pass", "fail", "unmeasured", "not_applicable", "void"]

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
    #: Digest of the prompt as delivered, from :func:`prompt_sha256` (dynamic
    #: slots normalised) or, for a hook whose output is live, the hook
    #: script's :func:`hook_script_sha256`. It must be one the installed
    #: product ships; anything else is a harness instruction wearing a
    #: product label.
    sha256: str = Field(pattern=_SHA256)


class StandingInstruction(StrictModel):
    """Instructions the agent carried into the session before any turn:
    project or user instruction files, custom instructions, native memory,
    installed skills. ``asks_for_memory`` records whether the text asks to
    save, recall, route or create pages (see :func:`standing_instruction`)."""

    kind: Literal[
        "project_instructions", "user_instructions", "custom_instructions", "native_memory", "installed_skill"
    ]
    sha256: str = Field(pattern=_SHA256)
    asks_for_memory: bool


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
    standing_instructions: tuple[StandingInstruction, ...] = ()

    @model_validator(mode="after")
    def _hooks_need_a_lifecycle(self) -> HostInitiation:
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
    #: Paths of the pages the agent inspected before choosing this
    #: destination (the proposal's bounded ``alternatives``).
    alternatives: tuple[str, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def _phase_fields(self) -> AgentDecision:
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
        elif (
            self.route is not None
            or self.target is not None
            or self.title is not None
            or self.alternatives
        ):
            raise ValueError("route, target, title and alternatives belong to the destination phase")
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
    def _observed_has_seq(self) -> Publication:
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
    def _grade_has_grader(self) -> LaterResponse:
        if (self.usefulness_grade == "ungraded") != (self.grader == "none"):
            raise ValueError("a usefulness grade needs a grader; ungraded has none")
        return self


class PrivateBinding(StrictModel):
    original_input_sha256: str = Field(pattern=_SHA256)
    snapshot_sha256: str = Field(pattern=_SHA256)


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
    #: For an exact private replay only: the original input and snapshot the
    #: run bound before any effect. Never present on a public fixture run.
    private_binding: PrivateBinding | None = None

    @model_validator(mode="after")
    def _ordering(self) -> NoNudgeObservation:
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


@dataclass(frozen=True)
class Frozen:
    """What a fixture pinned before any run, and what a record must bind to.

    ``shipped_prompt_sha256`` holds the digests of the prompt texts the
    installed product ships (see :func:`shipped_prompt_sha256`). A public
    synthetic fixture leaves the private digests unset; an exact private
    replay, run and kept locally, sets both.
    """

    fixture_id: str
    actor_sha256: str
    pre_capture_sha256: str
    evaluator_sha256: str
    turns_sha256: str
    later_turn_sha256: str | None = None
    shipped_prompt_sha256: frozenset[str] = frozenset()
    private_input_sha256: str | None = None
    private_snapshot_sha256: str | None = None

    @property
    def private(self) -> bool:
        return self.private_input_sha256 is not None


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


#: The dynamic slots product prompt templates carry, and the only values a
#: delivered prompt may put in them. A slot is normalised back to its
#: placeholder before digesting, so every genuine delivery of a template
#: shares the template's digest and any other edit does not.
PROMPT_SLOTS: dict[str, str] = {"key": r"ep-[0-9a-f]{32}"}
#: The hook scripts a lifecycle client runs. Their output is live (an
#: activation packet, a checkpoint), so a record binds the script that
#: produced it, never the packet text.
HOOK_SCRIPTS: tuple[str, ...] = (
    "exomem_retrieve_nudge.py",
    "exomem_continuation_checkpoint.py",
    "exomem_capture_nudge.py",
)


def product_prompt_templates() -> tuple[str, ...]:
    """The fixed and templated prompt texts this checkout ships: the MCP
    server instructions, the Stop hook's capture reminder and episode ask,
    the activation packet's episode_due sentence and the scaffold skill."""

    from importlib.resources import files

    from exomem import episode_nudge, server
    from exomem._hooks import exomem_capture_nudge as nudge

    skill = files("exomem._scaffold").joinpath("_Schema", "SKILL.md").read_text(encoding="utf-8")
    return (server.SERVER_INSTRUCTIONS, nudge.REMINDER, nudge.EPISODE_ASK, episode_nudge.EPISODE_RULE, skill)


def _template_pattern(template: str) -> re.Pattern[str] | None:
    parts = re.split(r"\{(" + "|".join(PROMPT_SLOTS) + r")\}", template)
    if len(parts) == 1:
        return None
    pattern = "".join(re.escape(part) if index % 2 == 0 else f"(?:{PROMPT_SLOTS[part]})" for index, part in enumerate(parts))
    return re.compile(pattern)


def prompt_sha256(text: str) -> str:
    """Digest of a product prompt as delivered, with its dynamic slots
    normalised: a delivery that fills a shipped template's slots with
    well-formed values digests to the template; anything else digests to
    its own bytes."""

    for template in product_prompt_templates():
        pattern = _template_pattern(template)
        if pattern is not None and pattern.fullmatch(text):
            return text_sha256(template)
    return text_sha256(text)


def hook_script_sha256(name: str) -> str:
    """Digest of one shipped hook script's bytes."""

    if name not in HOOK_SCRIPTS:
        raise ValueError(f"{name!r} is not a shipped hook script")
    from importlib.resources import files

    return hashlib.sha256(files("exomem._hooks").joinpath(name).read_bytes()).hexdigest()


def shipped_prompt_sha256(extra_texts: Iterable[str] = ()) -> frozenset[str]:
    """Digests of every product-shipped prompt this checkout carries: each
    template in :func:`product_prompt_templates` and each hook script, plus
    any texts a run harness reads from the installed version it exercises."""

    return frozenset(
        {
            *(text_sha256(text) for text in product_prompt_templates()),
            *(hook_script_sha256(name) for name in HOOK_SCRIPTS),
            *(text_sha256(text) for text in extra_texts),
        }
    )


def standing_instruction(kind: str, text: str) -> dict[str, Any]:
    """A standing-instruction entry for a record, from its text."""

    from .contract import find_nudges

    return {"kind": kind, "sha256": text_sha256(text), "asks_for_memory": bool(find_nudges(text))}


def void_reasons(record: NoNudgeObservation, frozen: Frozen) -> tuple[str, ...]:
    """Why a record cannot be scored against a fixture's frozen digests.

    A record bound to another fixture, actor input, pre-capture state,
    evaluator revision or later turn is void: an expectation edited after a
    run never rescores that run. A public synthetic fixture scores only
    synthetic-fixture runs; an exact private replay binds its own original
    input and snapshot and is never scored against a public fixture.
    """

    reasons = []
    if record.fixture_id != frozen.fixture_id:
        reasons.append("fixture_id differs from the fixture being scored")
    for name in ("actor_sha256", "pre_capture_sha256", "evaluator_sha256"):
        if getattr(record, name) != getattr(frozen, name):
            reasons.append(f"{name} differs from the fixture's frozen digest")
    later = record.later_response
    if later is not None and later.turn_sha256 != frozen.later_turn_sha256:
        reasons.append("the later turn differs from the fixture's frozen later turn")
    origin = record.host_initiation.input_origin
    if not frozen.private and origin != "synthetic_fixture":
        reasons.append(
            f"a {origin} run is not scored against a public synthetic fixture; an exact private "
            "replay binds its own original input and snapshot and stays local"
        )
    if frozen.private and origin != "original_private":
        reasons.append("an exact private replay needs the original private input")
    return tuple(reasons)


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


def _initiation(record: NoNudgeObservation, frozen: Frozen) -> Verdict:
    host = record.host_initiation
    reasons: list[str] = []
    if host.delivered_turns_sha256 != frozen.turns_sha256:
        reasons.append("delivered turns differ from the fixture's declared actor input")
    reasons.extend(f"user reminder: {turn!r}" for turn in host.user_reminders)
    reasons.extend(
        f"harness intervention {item.kind}: {item.detail}" for item in host.harness_interventions
    )
    reasons.extend(
        f"product prompt {prompt.kind} is not a shipped product prompt; it counts as a harness instruction"
        for prompt in host.product_prompts
        if prompt.sha256 not in frozen.shipped_prompt_sha256
    )
    reasons.extend(
        f"standing instruction {item.kind} asks for memory use and is not product-shipped"
        for item in host.standing_instructions
        if item.asks_for_memory and item.sha256 not in frozen.shipped_prompt_sha256
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
    """Transport: every observed effect committed or replayed from its receipt,
    and every candidate the agent routed has one.

    With no effect at all, a record whose agent dispositions are all
    non-routed is a correct abstention and passes; a routed candidate without
    an effect fails; a record with no disposition is unmeasured.
    """

    dispositions = [
        decision
        for decision in record.agent_decisions
        if decision.phase == "disposition" and decision.initiator == "agent"
    ]
    routed = {decision.candidate_key for decision in dispositions if decision.disposition == "routed"}
    if not record.leaf_effects:
        if routed:
            return Verdict("fail", tuple(f"routed candidate {key!r} has no effect" for key in sorted(routed)))
        if dispositions:
            return Verdict("pass", ("every candidate was disposed of without an effect",))
        return Verdict("unmeasured", ("no leaf effect and no disposition were observed",))
    landed = {
        effect.candidate_key for effect in record.leaf_effects if effect.outcome in {"committed", "replayed"}
    }
    reasons = [f"routed candidate {key!r} has no committed effect" for key in sorted(routed - landed)]
    reasons += [
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


def _exact_replay(record: NoNudgeObservation, frozen: Frozen) -> Verdict:
    origin = record.host_initiation.input_origin
    if origin == "synthetic_fixture":
        return Verdict("not_applicable", ("a synthetic fixture is not a private replay",))
    if origin == "reconstructed":
        return Verdict(
            "unmeasured",
            ("a reconstructed input cannot establish exact replay of the original episode",),
        )
    binding = record.private_binding
    if (
        binding is None
        or not frozen.private
        or binding.original_input_sha256 != frozen.private_input_sha256
        or binding.snapshot_sha256 != frozen.private_snapshot_sha256
    ):
        return Verdict(
            "unmeasured",
            ("an exact replay needs the original private input and snapshot digests bound before the run",),
        )
    return Verdict("pass")


def evaluate(record: NoNudgeObservation, frozen: Frozen) -> ObservationReport:
    """One verdict per part of the record, and the conjunctive acceptance gate
    over the record alone. :func:`accept` adds the vault check and binding."""

    initiation = _initiation(record, frozen)
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
        exact_private_replay=_exact_replay(record, frozen),
        ordinary_agent_acceptance=acceptance,
    )


def accept(record: NoNudgeObservation, capture_check: Any, frozen: Frozen) -> Verdict:
    """Ordinary-agent acceptance of one run of one fixture.

    Void when the record is bound to anything but the fixture's frozen
    digests; otherwise it passes only when the vault the capture left passes
    every frozen expectation (``capture_check.accepted``) and the record's own
    acceptance gate passes. Nothing else can stand in for either.
    """

    void = void_reasons(record, frozen)
    if void:
        return Verdict("void", void)
    report = evaluate(record, frozen)
    reasons = []
    if not getattr(capture_check, "accepted", False):
        failed = capture_check.failed() if hasattr(capture_check, "failed") else ()
        reasons.append(f"the vault check did not pass: {list(failed)}")
    gate = report.ordinary_agent_acceptance
    if reasons or gate.outcome == "fail":
        return Verdict("fail", tuple(reasons) + gate.reasons)
    if gate.outcome != "pass":
        return Verdict("unmeasured", gate.reasons)
    return Verdict("pass")


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
