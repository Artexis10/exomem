"""Deterministic gates. Final by contract: nothing downstream may override.

Every gate runs on every query and reports ``not_applicable`` when its
preconditions are absent — unsupported/absent is never converted to a zero.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from functools import lru_cache

from membench import oracle, quant
from membench.schema import (
    ClaimRecord,
    EntityRecord,
    ExpectedRecord,
    QueryRecord,
    SourceRecord,
)
from membench.scoring.answer_contract import AnswerRecord, detect_hedging

# A leading ``-`` counts as a sign only when the token starts cleanly. Without
# the lookbehind, "Ref SRC-3.5" yields -3.5 and "a 2x-3.5x increase" yields
# -3.5, so a correct answer fails against an expected 3.5.
_NUMERIC_TOKEN = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?")

# Cap on how many offending citations a single gate evidence string lists.
_MAX_LISTED_CITATIONS = 8

# A value the gates can compare as a number rather than as a string.
_PLAIN_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")

# Numbers that stand on their own. Stricter than ``_NUMERIC_TOKEN``, which
# deliberately reads a magnitude out of the middle of a token ("SRC-3.5" ->
# 3.5) so a *tolerance* comparison has something to work with. Presence is a
# different question from tolerance: "SRC-10" does not state the value 10, and
# "2025-03-14" does not state 3.
#
# Both boundaries are asymmetric on purpose, and the asymmetry is load-bearing:
#
# - A leading ``.``, ``,`` or ``-`` always means the digits continue a larger
#   token — the ``5`` of "SRC-3.5", the ``000`` of "84,000", the ``03`` of a
#   hyphenated date.
# - A *trailing* ``.`` does not: the group is greedy, so a match can only end
#   before a dot that is not followed by a digit. That dot ends a sentence, and
#   "measured 197." states 197. Excluding it lost every value at the end of a
#   sentence, which is most of them.
# - A trailing ``,`` only continues the token when a digit follows it
#   (thousands separator); "197, 209 and 217" is a list, not one number.
_STANDALONE_NUMBER = re.compile(r"(?<![\w.,-])-?\d+(?:\.\d+)?(?![\w-])(?!,\d)")


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
    # Entity records resolve names to entities, which is what lets the citation
    # gate tell a reference-resolving claim from an unrelated measurement about
    # the same subject. Optional so every existing two-argument construction
    # keeps working; when absent the citation gate reports precision
    # unverifiable rather than guessing (see oracle.permitted_citations).
    entities_by_id: dict[str, EntityRecord] = field(default_factory=dict)

    def claim_value(self, claim_id: str) -> str | None:
        claim = self.claims_by_id.get(claim_id)
        if claim is None:
            return None
        return claim.object.value


def _item(
    query: QueryRecord, gate: str, dimension: str, status: GateStatus, evidence: str | None = None
) -> ScoreItem:
    return ScoreItem(query.query_id, gate, dimension, status, evidence)


def _numeric_candidate_within_tolerance(
    expected_value: str, unit: str | None, tolerance: float, text: str
) -> bool | None:
    """True iff some numeric token in ``text`` is within ``tolerance`` of
    ``expected_value``, per :func:`quant.within_tolerance` -- the same
    Decimal matching rule the oracle used to compute the tolerance, so there
    is one matching rule rather than two.

    ``None`` means the tolerance rule does not apply because the expected
    value is not a number. :func:`quant.within_tolerance` guards the candidate
    side but parses the expected side unguarded, so a record such as
    ``ExpectedAnswer(kind="value", values=["2.5 kg"], tolerance=0.05)`` raises.
    expected.jsonl is consumed by third parties, and a gate must degrade rather
    than crash the whole run on one malformed record, so the caller falls back
    to the literal rule instead.
    """

    try:
        derived = quant.DerivedQuantity(
            value=expected_value, unit=unit, tolerance=Decimal(str(tolerance))
        )
    except (ArithmeticError, ValueError, TypeError):
        return None
    try:
        return any(
            quant.within_tolerance(derived, candidate)
            for candidate in _NUMERIC_TOKEN.findall(text)
        )
    except (ArithmeticError, ValueError, TypeError):
        return None


@lru_cache(maxsize=4096)
def _literal_pattern(value: str) -> re.Pattern[str]:
    """``value`` anchored so it cannot match inside a longer word.

    The boundary is asserted only on the sides where the value itself ends in a
    word character, so a value with punctuation at either end still matches.
    """

    pattern = re.escape(value)
    if value[:1].isalnum() or value[:1] == "_":
        pattern = r"(?<![0-9A-Za-z_])" + pattern
    if value[-1:].isalnum() or value[-1:] == "_":
        pattern = pattern + r"(?![0-9A-Za-z_])"
    return re.compile(pattern)


def states_value(value: str, text: str) -> bool:
    """True iff ``text`` states ``value`` as a value in its own right.

    The single matching rule for both :func:`gate_value` and :func:`gate_state`.
    Bare substring containment — what both used — is wrong in both directions
    at once: a forbidden ``10`` fires on ``105``, ``2010`` and ``SRC-10``, and
    an expected ``10`` passes on ``105``. So it fails correct answers *and*
    passes wrong ones, and the corpus has already been bent around it (t20 caps
    a generated count at two digits purely so no forbidden value can be a
    substring of a legitimate one).

    Numbers go through the same ``Decimal`` path :func:`quant.within_tolerance`
    gives the tolerance branch, at tolerance zero, so there is one numeric
    matching rule rather than two: ``105`` is not ``10`` by arithmetic, not by
    string luck. Everything else — names, dates, free text — matches on word
    boundaries.
    """

    if not value:
        return False
    if _PLAIN_NUMBER.match(value):
        derived = quant.DerivedQuantity(value=value, unit=None, tolerance=Decimal(0))
        return any(
            quant.within_tolerance(derived, candidate)
            for candidate in _STANDALONE_NUMBER.findall(text)
        )
    return _literal_pattern(value).search(text) is not None


def gate_value(
    query: QueryRecord, expected: ExpectedRecord, answer: AnswerRecord, ctx: ScoringContext
) -> ScoreItem:
    if expected.answer.kind == "none" or not expected.answer.values:
        return _item(query, "value", "factual_qa", GateStatus.NOT_APPLICABLE)
    text = answer.answer_text
    values = expected.answer.values
    if expected.answer.kind == "list":
        missing = [v for v in values if not states_value(v, text)]
        if missing:
            return _item(
                query, "value", "factual_qa", GateStatus.FAIL, f"missing values: {missing}"
            )
        return _item(query, "value", "factual_qa", GateStatus.PASS)
    if expected.answer.kind == "value" and expected.answer.tolerance and len(values) == 1:
        within = _numeric_candidate_within_tolerance(
            values[0], expected.answer.unit, expected.answer.tolerance, text
        )
        if within is True:
            return _item(query, "value", "factual_qa", GateStatus.PASS)
        if within is False:
            return _item(
                query,
                "value",
                "factual_qa",
                GateStatus.FAIL,
                f"no candidate within tolerance {expected.answer.tolerance} of {values[0]}",
            )
        # within is None: the expected value is not numeric, so the tolerance
        # rule cannot decide. Fall through to the literal rule below.
    if any(states_value(v, text) for v in values):
        return _item(query, "value", "factual_qa", GateStatus.PASS)
    return _item(
        query, "value", "factual_qa", GateStatus.FAIL, f"none of {values} in answer"
    )


def _ask_instant(query: QueryRecord) -> date:
    """World-time instant this ask is scored at — the same one the gate's own
    name is chosen from: a pinned world week for ``as_of``, the knowledge
    cutoff for ``current_state``."""

    week = query.ask.world_week if query.ask.world_week is not None else query.ask.knowledge_week
    return oracle.world_cutoff(week)


def gate_state(
    query: QueryRecord, expected: ExpectedRecord, answer: AnswerRecord, ctx: ScoringContext
) -> ScoreItem:
    """current_state / as_of: state the required claim values, and do not put
    forward a retired one the oracle can prove should have been dropped.

    The bare rule this replaces — every required value present by substring,
    every forbidden value absent by substring — scored correct product
    behaviour as failure three separate ways. Each is fixed on its own terms:

    - **Matching is boundary-aware** (:func:`states_value`). Substring
      containment fires a forbidden ``10`` on ``105``, ``2010`` and ``SRC-10``,
      and it fired a forbidden ``3`` on the ``03`` inside a date and on
      whatever digits the provider's own ingest ids happened to contain, which
      made the verdict depend on a UUID.

    - **A forbidden value the query itself supplies is not measurable.** Four
      identity asks name the retired project name in the prompt ("the project
      once called X"), so *every possible* answer echoes it — including one
      produced with no memory at all. Its presence carries no information about
      the system under test, so it is excluded rather than scored. Without this
      those four asks are unwinnable, which is a defect in the harness, not a
      result about a contender.

    - **A retired value that arrives together with its own supersession is not
      a decidable verdict.** The absence rule silently assumed an assertive QA
      answer. Return a record *and* the memo that reversed it — reasonable
      retrieval behaviour, arguably better than hiding the history — and both
      values are in the text. Whether the answer *asserts* the retired value or
      merely reports it as history is a fact about rhetoric, and no
      deterministic rule can read it. Guessing PASS would let a provider that
      has the reversal backwards ("it is X, previously Y") bank a pass;
      guessing FAIL is the defect above. So the gate reports ``UNSUPPORTED``:
      unsupported-never-zero cuts both ways, and an answer that cannot be
      scored must not be banked as a pass either.

    The gate is not thereby weakened. A missing required value still fails; a
    retired value stated with no oracle-visible supersession toward a required
    claim still fails (including every record whose whole expectation is
    absence — the not-yet-knowable and retracted facts, which have no required
    claim for a supersession to point at). ``UNSUPPORTED`` is never a pass, so
    a contender that answers by dumping documents earns no temporal credit at
    all rather than earning a free one.
    """

    gate = "as_of" if query.ask.world_week is not None else "current_state"
    if not expected.required_claims and not expected.forbidden_claims:
        return _item(query, gate, "temporal", GateStatus.NOT_APPLICABLE)
    text = answer.answer_text

    stated_required: list[str] = []
    for claim_id in expected.required_claims:
        value = ctx.claim_value(claim_id)
        if value is None:
            continue
        if not states_value(value, text):
            return _item(
                query,
                gate,
                "temporal",
                GateStatus.FAIL,
                f"required {claim_id} value {value!r} absent",
            )
        stated_required.append(claim_id)

    echoed: list[str] = []
    superseded: list[str] = []
    asserted: list[str] = []
    for claim_id in expected.forbidden_claims:
        value = ctx.claim_value(claim_id)
        if value is None:
            continue
        if states_value(value, query.prompt_text):
            echoed.append(f"{claim_id} {value!r}")
            continue
        if not states_value(value, text):
            continue
        chain = oracle.superseded_toward(
            claim_id,
            stated_required,
            claims_by_id=ctx.claims_by_id,
            world_t=_ask_instant(query),
            knowledge_week=query.ask.knowledge_week,
        )
        if chain:
            superseded.append(f"{claim_id} {value!r} -> {' -> '.join(chain)}")
        else:
            asserted.append(f"{claim_id} {value!r}")

    if asserted:
        return _item(
            query,
            gate,
            "temporal",
            GateStatus.FAIL,
            f"forbidden value present with no supersession toward the required claims: "
            f"{asserted}",
        )
    if superseded:
        return _item(
            query,
            gate,
            "temporal",
            GateStatus.UNSUPPORTED,
            f"every required value is stated, and so is a value the oracle shows it "
            f"superseded ({superseded}); which of the two the answer asserts is not "
            f"derivable from the text",
        )
    evidence = (
        f"forbidden value supplied by the query prompt, not measurable: {echoed}"
        if echoed
        else None
    )
    return _item(query, gate, "temporal", GateStatus.PASS, evidence)


def gate_citations(
    query: QueryRecord, expected: ExpectedRecord, answer: AnswerRecord, ctx: ScoringContext
) -> ScoreItem:
    """Provenance is precision AND recall — shotgunning is not attribution.

    Recall alone lets a contender cite every source it retrieved (or the whole
    corpus) and pass every citation query, which flatters exactly the weak
    provenance this benchmark exists to expose. So an answer also fails when it
    names a source outside :func:`oracle.permitted_citations` — the evidence
    neighbourhood of the claims under discussion, closed over derivation in
    both directions, supersession, and reference-resolving claims about an
    entity in the closure. A source outside it asserts nothing visible about
    anything the answer is about.

    Precision is measured wherever a claim basis exists, including records that
    required no citations at all; scoring only the citation-requiring records
    left a quarter of the suite where a shotgun cost nothing. Recall is
    reported as ``n/a`` where nothing was required.

    Two disciplines constrain the verdict:

    - a citation the oracle merely did not *require* is never punished, so an
      answer with better provenance than the record demands still passes;
    - when the record gives no resolvable claim basis the precision half is
      unmeasurable, and the gate reports ``UNSUPPORTED``. Unsupported-never-zero
      cuts both ways: an unverifiable provenance verdict must not be banked as
      a PASS either, or a contender that shotguns those records shows a clean
      provenance sheet.

    Both ratios go into the evidence on every verdict so a reader can audit
    without rerunning.
    """

    required = list(dict.fromkeys(expected.required_citations))
    cited = list(dict.fromkeys(answer.citations))
    permitted, unverifiable = oracle.permitted_citations(
        expected,
        claims_by_id=ctx.claims_by_id,
        knowledge_week=query.ask.knowledge_week,
        entities_by_id=ctx.entities_by_id,
        sources_by_id=ctx.sources_by_id,
    )
    if not required and (unverifiable is not None or not cited):
        # Genuinely nothing to decide: no citation was required and either the
        # oracle has no claim basis or the answer cited nothing at all.
        return _item(
            query,
            "citations",
            "provenance",
            GateStatus.NOT_APPLICABLE,
            f"no required citations; {len(cited)} cited",
        )
    missing = [c for c in required if c not in cited]
    unsupported = [c for c in cited if c not in permitted] if unverifiable is None else []
    # Recall is only meaningful where citations were required; precision is
    # measurable wherever a claim basis exists, including records that required
    # none. Skipping those made a shotgun free on a quarter of the suite.
    recall = (
        f"recall {len(required) - len(missing)}/{len(required)}" if required else "recall n/a"
    )
    precision = (
        f"precision {len(cited) - len(unsupported)}/{len(cited)}"
        if unverifiable is None
        else f"precision unverifiable ({unverifiable})"
    )
    evidence = f"{recall}; {precision}"
    if missing:
        # Recall is provable whatever precision could be established, so a
        # record with no claim basis still fails here rather than hiding.
        return _item(
            query,
            "citations",
            "provenance",
            GateStatus.FAIL,
            f"{evidence}; missing citations: {missing}",
        )
    if unverifiable is not None:
        return _item(query, "citations", "provenance", GateStatus.UNSUPPORTED, evidence)
    if unsupported:
        # A shotgun can name hundreds of sources; the precision ratio above
        # already carries the full count, so the listing is capped to keep
        # deterministic-scores.json readable.
        shown = unsupported[:_MAX_LISTED_CITATIONS]
        elided = len(unsupported) - len(shown)
        listing = f"{shown}{f' (+{elided} more)' if elided else ''}"
        return _item(
            query,
            "citations",
            "provenance",
            GateStatus.FAIL,
            f"{evidence}; unsupported citations: {listing}",
        )
    return _item(query, "citations", "provenance", GateStatus.PASS, evidence)


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
