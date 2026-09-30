"""Immutable inputs and fixture contracts for the sequence-four replay journeys."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from ..corpora.lifecycle_replay import STORE_BEARING_RE, StoreBearingUtterance

_ADDED_STORE_WORDS = re.compile(
    r"\b(?:collections?|ledgers?|schemas?|claims?|track(?:s|ed|ing)?)\b", re.I
)


@dataclass(frozen=True)
class ReplayAttachment:
    name: str
    media_type: str
    content: str


@dataclass(frozen=True)
class ReplayTurn:
    turn_id: str
    text: str
    observed_on: str
    event: tuple[tuple[str, str | tuple[str, ...]], ...] | None = None
    confirm: bool = False
    attachments: tuple[ReplayAttachment, ...] = ()

    def client_input(self) -> str:
        """Render complete text artifacts into the native client's user channel."""
        return self.text + "".join(
            f'\n\n<attachment filename="{attachment.name}" media_type="{attachment.media_type}">\n'
            + attachment.content
            + "\n</attachment>"
            for attachment in self.attachments
        )


@dataclass(frozen=True)
class ReplayCorpus:
    corpus_id: str
    family_id: str
    domain: str
    turns: tuple[ReplayTurn, ...]
    natural_key: tuple[str, ...]
    expect_candidate: bool = False
    candidate_turn: int | None = None
    seeded_collection: str | None = None

    def expected_records(self) -> list[dict[str, Any]]:
        return [
            {key: list(value) if isinstance(value, tuple) else value for key, value in turn.event}
            for turn in self.turns
            if turn.event is not None
        ]

    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(
                asdict(self), sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode()
        ).hexdigest()


def assert_no_store_bearing_utterance(turns: tuple[tuple[str, str], ...]) -> None:
    for turn_id, text in turns:
        match = STORE_BEARING_RE.search(text) or _ADDED_STORE_WORDS.search(text)
        if not text.strip() or match is not None:
            token = match.group(0) if match else "<empty>"
            raise StoreBearingUtterance(f"turn {turn_id!r} contains store-bearing token {token!r}")


def build_corpus(corpus: ReplayCorpus) -> ReplayCorpus:
    assert_no_store_bearing_utterance(
        tuple((turn.turn_id, turn.client_input()) for turn in corpus.turns)
    )
    if not corpus.turns or len({turn.turn_id for turn in corpus.turns}) != len(corpus.turns):
        raise ValueError("replay requires unique, nonempty turns")
    if corpus.family_id not in {"f28", "f29"}:
        raise ValueError("sequence four contains f28 and f29 only")
    if corpus.candidate_turn is not None and not 0 < corpus.candidate_turn < len(corpus.turns):
        raise ValueError("candidate observation must precede the confirmation phase")
    return corpus


def corpus_for(subject: str | None) -> ReplayCorpus:
    from .f28_promotion_replay import promotion_corpus
    from .f29_claimed_routing_replay import routing_corpus

    for corpus in (promotion_corpus(), promotion_corpus(twin=True), routing_corpus()):
        if subject == corpus.corpus_id:
            return corpus
    raise ValueError(f"unknown collection replay corpus: {subject!r}")


def fixture_payload(corpus: ReplayCorpus) -> dict[str, Any]:
    """The checked-in trajectory; no operation or user prompt is inferred at run time."""
    phases: list[dict[str, Any]] = []
    for arm, prominence in (("hookless", "maximal"), ("hooked", "balanced")):
        chunks = (
            (
                ("candidate", corpus.turns[: corpus.candidate_turn]),
                ("confirmed", corpus.turns[corpus.candidate_turn :]),
            )
            if corpus.candidate_turn is not None
            else (("observed", corpus.turns),)
        )
        for index, (stage, turns) in enumerate(chunks):
            phase = f"{arm}-{stage}"
            ops: list[dict[str, str]] = []
            if index == 0:
                ops.append({"op": "configure", "ref": f"{arm}-{prominence}"})
            ops.append({"op": "snapshot", "ref": f"s-{phase}-seed"})
            ops.extend(
                {
                    "op": "agent_turn",
                    "ref": turn.turn_id,
                    "at": turn.observed_on,
                    "detail": turn.client_input(),
                }
                for turn in turns
            )
            ops.append({"op": "snapshot", "ref": f"s-{phase}"})
            names = (
                ("collection_candidate_surfaced_within_budget",)
                if stage == "candidate"
                else ("ledger_state_matches_expectation",)
                if corpus.family_id == "f28"
                else ("claimed_observation_reflected", "no_structured_write_beyond_expectation")
            )
            phases.append(
                {
                    "phase_id": phase,
                    "ops": ops,
                    "expect": [{"assert": name, "subject": corpus.corpus_id} for name in names],
                }
            )
    return {
        "scenario_id": corpus.corpus_id,
        "family_id": corpus.family_id,
        "kind": "operational",
        "public_coverage": "none",
        "phases": phases,
        "fairness": {
            "why_neutral": "Ordinary observed events must reach durable state without naming its storage; tentative events must not.",
            "public_coverage_subtraction": "No public coverage is claimed for these operational journeys.",
            "mechanisms": [
                {
                    "provider_role": "subject",
                    "mechanism": "Documented collection and review surfaces projected into neutral snapshots.",
                    "verdict": "satisfiable",
                    "evidence": "benchmarks/epistemic/PREREGISTRATION.md:1",
                }
            ],
            "privileged_endpoint_matrix": [
                {
                    "driver_surface_id": "state.read",
                    "provider": "exomem",
                    "variant": "native",
                    "disposition": "equivalent",
                    "audit_scope": "Read-only collection and candidate projection",
                    "evidence": "benchmarks/epistemic/PREREGISTRATION.md:1",
                    "reason": "Reads the same state the client can inspect.",
                    "competitor_surface": "Documented collection and review read endpoint",
                }
            ],
            "acceptance_predicate": "PREREGISTRATION.md section 4, sequence-four replay predicates.",
        },
    }
