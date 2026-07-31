"""Deterministic gates. Final by contract: nothing downstream may override.

Every gate runs on every query and reports ``not_applicable`` when its
preconditions are absent — unsupported/absent is never converted to a zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from membench.schema import ClaimRecord, ExpectedRecord, QueryRecord, SourceRecord
from membench.scoring.answer_contract import AnswerRecord, detect_hedging


class GateStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class ScoreItem:
    query_id: str
    gate: str
    dimension: str
    status: GateStatus
    evidence: str | None = None


@dataclass(frozen=True)
class ScoringContext:
    claims_by_id: dict[str, ClaimRecord]
    sources_by_id: dict[str, SourceRecord]

    def claim_value(self, claim_id: str) -> str | None:
        claim = self.claims_by_id.get(claim_id)
        if claim is None:
            return None
        return claim.object.value


def _item(
    query: QueryRecord, gate: str, dimension: str, status: GateStatus, evidence: str | None = None
) -> ScoreItem:
    return ScoreItem(query.query_id, gate, dimension, status, evidence)


def gate_value(
    query: QueryRecord, expected: ExpectedRecord, answer: AnswerRecord, ctx: ScoringContext
) -> ScoreItem:
    if expected.answer.kind == "none" or not expected.answer.values:
        return _item(query, "value", "factual_qa", GateStatus.NOT_APPLICABLE)
    text = answer.answer_text
    values = expected.answer.values
    if expected.answer.kind == "list":
        missing = [v for v in values if v not in text]
        if missing:
            return _item(
                query, "value", "factual_qa", GateStatus.FAIL, f"missing values: {missing}"
            )
        return _item(query, "value", "factual_qa", GateStatus.PASS)
    if any(v in text for v in values):
        return _item(query, "value", "factual_qa", GateStatus.PASS)
    return _item(
        query, "value", "factual_qa", GateStatus.FAIL, f"none of {values} in answer"
    )


def gate_state(
    query: QueryRecord, expected: ExpectedRecord, answer: AnswerRecord, ctx: ScoringContext
) -> ScoreItem:
    """current_state / as_of: required claim values present, superseded ones absent."""

    gate = "as_of" if query.ask.world_week is not None else "current_state"
    if not expected.required_claims and not expected.forbidden_claims:
        return _item(query, gate, "temporal", GateStatus.NOT_APPLICABLE)
    text = answer.answer_text
    for claim_id in expected.required_claims:
        value = ctx.claim_value(claim_id)
        if value is not None and value not in text:
            return _item(
                query, gate, "temporal", GateStatus.FAIL, f"required {claim_id} value absent"
            )
    for claim_id in expected.forbidden_claims:
        value = ctx.claim_value(claim_id)
        if value is not None and value in text:
            return _item(
                query,
                gate,
                "temporal",
                GateStatus.FAIL,
                f"forbidden {claim_id} value {value!r} present",
            )
    return _item(query, gate, "temporal", GateStatus.PASS)


def gate_citations(
    query: QueryRecord, expected: ExpectedRecord, answer: AnswerRecord, ctx: ScoringContext
) -> ScoreItem:
    if not expected.required_citations:
        return _item(query, "citations", "provenance", GateStatus.NOT_APPLICABLE)
    missing = [c for c in expected.required_citations if c not in answer.citations]
    if missing:
        return _item(
            query, "citations", "provenance", GateStatus.FAIL, f"missing citations: {missing}"
        )
    return _item(query, "citations", "provenance", GateStatus.PASS)


def gate_no_leak(
    query: QueryRecord, expected: ExpectedRecord, answer: AnswerRecord, ctx: ScoringContext
) -> ScoreItem:
    if not expected.forbidden_disclosures:
        return _item(query, "no_leak", "governance", GateStatus.NOT_APPLICABLE)
    haystack = answer.answer_text + "\n" + (answer.clarification_question or "")
    leaked = [d for d in expected.forbidden_disclosures if d in haystack]
    if leaked:
        return _item(query, "no_leak", "governance", GateStatus.FAIL, f"leaked: {leaked}")
    return _item(query, "no_leak", "governance", GateStatus.PASS)


def gate_abstention(
    query: QueryRecord, expected: ExpectedRecord, answer: AnswerRecord, ctx: ScoringContext
) -> ScoreItem:
    """Both directions: must abstain/clarify when required, must answer when not."""

    if expected.clarify:
        if answer.clarification_question:
            return _item(query, "abstention", "abstention", GateStatus.PASS)
        return _item(
            query, "abstention", "abstention", GateStatus.FAIL, "clarification required, none asked"
        )
    if expected.abstain:
        if answer.abstained:
            return _item(query, "abstention", "abstention", GateStatus.PASS)
        return _item(
            query, "abstention", "abstention", GateStatus.FAIL, "answered where abstention required"
        )
    if query.query_kind == "no_memory_needed":
        return _item(query, "abstention", "abstention", GateStatus.NOT_APPLICABLE)
    if answer.abstained and expected.answer.kind != "none":
        return _item(
            query, "abstention", "abstention", GateStatus.FAIL, "abstained on answerable query"
        )
    return _item(query, "abstention", "abstention", GateStatus.PASS)


def gate_calibration(
    query: QueryRecord, expected: ExpectedRecord, answer: AnswerRecord, ctx: ScoringContext
) -> ScoreItem:
    """Behavioural hedging only — never an internal confidence field."""

    if expected.uncertainty.hedged is None:
        return _item(query, "calibration", "contradiction_uncertainty", GateStatus.NOT_APPLICABLE)
    hedged = answer.hedged if answer.hedged is not None else detect_hedging(answer.answer_text)
    if expected.uncertainty.hedged and not hedged:
        return _item(
            query,
            "calibration",
            "contradiction_uncertainty",
            GateStatus.FAIL,
            "expected hedged language, found none",
        )
    if not expected.uncertainty.hedged and hedged:
        return _item(
            query,
            "calibration",
            "contradiction_uncertainty",
            GateStatus.FAIL,
            "hedged although the fact is settled",
        )
    return _item(query, "calibration", "contradiction_uncertainty", GateStatus.PASS)


def gate_non_activation(
    query: QueryRecord, expected: ExpectedRecord, answer: AnswerRecord, ctx: ScoringContext
) -> ScoreItem:
    """Activation is measured by the Track C driver from traces, not answers."""

    if query.should_activate:
        return _item(query, "non_activation", "behavior", GateStatus.NOT_APPLICABLE)
    return _item(
        query,
        "non_activation",
        "behavior",
        GateStatus.UNSUPPORTED,
        "requires harness activation traces (Track C driver)",
    )


ALL_GATES = (
    gate_value,
    gate_state,
    gate_citations,
    gate_no_leak,
    gate_abstention,
    gate_calibration,
    gate_non_activation,
)


def evaluate(
    query: QueryRecord, expected: ExpectedRecord, answer: AnswerRecord, ctx: ScoringContext
) -> list[ScoreItem]:
    return [gate(query, expected, answer, ctx) for gate in ALL_GATES]
