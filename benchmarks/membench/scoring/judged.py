"""The judged lane: strictly additive, structurally unable to override a gate.

Deterministic gates are FINAL. That is not enforced here by convention or by
review discipline — it is enforced by types and by file boundaries, so a bug
in this module cannot break it:

- A judged verdict is a :class:`JudgedItem`, never a
  :class:`~membench.scoring.gates.ScoreItem`. Nothing in this module
  constructs, copies or mutates a ``ScoreItem``, so no route — including a
  buggy one — turns judge output into a deterministic verdict.
- :func:`candidate_for` refuses any gate item whose status is not
  ``UNSUPPORTED``, and raises rather than skipping. Upgrading "the harness
  cannot tell" into a verdict is the entire job; a decided gate is out of
  contract, and a caller that asks for one has a bug worth failing on.
- Judged verdicts are tallied into their own dimension
  (:data:`JUDGED_DIMENSION`) and written to their own file
  (``judged-scores.json``). ``deterministic-scores.json`` is written *before*
  the judge phase runs and is byte-identical whether or not a judge was
  configured, so a reader subtracts the judged contribution by ignoring one
  file — never by rerunning.

Scope (task 4b.20). The judge is not pointed at every UNSUPPORTED gate, only
at the ones whose UNSUPPORTED reason is a *semantic* question it was actually
measured on. See :data:`JUDGE_RESOLVABLE_GATES`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from membench.judge.backends import JUDGE_PROMPT_TEMPLATE, make_judge_item
from membench.judge.handshake import RequestItem
from membench.schema import ExpectedRecord, QueryRecord
from membench.scoring.answer_contract import AnswerRecord
from membench.scoring.gates import GateStatus, ScoreItem

#: The dimension judged verdicts are tallied into. Deliberately NOT one of the
#: deterministic dimension names: the two can then never collide in a table,
#: and no reader can add a judged count into a deterministic one by accident.
JUDGED_DIMENSION = "semantic_match"

#: Gates whose UNSUPPORTED verdict a judge is allowed to speak to.
#:
#: ``current_state`` / ``as_of`` report UNSUPPORTED for exactly one reason
#: (see :func:`~membench.scoring.gates.gate_state`): every required value is
#: stated AND so is a value the oracle proves it superseded, so *which of the
#: two the answer asserts as current* decides the verdict and is not derivable
#: from the text by any deterministic rule available here. That is the
#: question the 19/19 direction-discrimination probe measured, and it is the
#: only measured territory.
#:
#: Every other UNSUPPORTED verdict in this harness is deliberately excluded,
#: because a judge reading the answer text cannot resolve any of them:
#:
#: - ``non_activation`` needs harness activation traces (Track C driver) that
#:   are not in the answer at all.
#: - ``citations`` reports UNSUPPORTED when the oracle has no claim basis to
#:   establish which sources are permitted. That is a corpus-coverage fact,
#:   and the judge is shown blinded ``[ctx:N]`` tokens precisely so it cannot
#:   reason about source identity.
#: - ``no_leak`` / ``abstention`` go UNSUPPORTED under wired governance when
#:   the translation DROPPED the rule the expectation depends on. The
#:   measurement never happened; no amount of reading the answer recovers it.
#:
#: Widening this set is a measurement, not a code change.
JUDGE_RESOLVABLE_GATES = frozenset({"current_state", "as_of"})

#: Task 4b.23. The 19/19 discrimination result was measured on clean
#: one-sentence candidates where supersession direction is unambiguous; the
#: real rows are multi-document dumps. It is an UPPER BOUND, not settled
#: capability, and every surface that publishes a judged number carries this.
JUDGE_UPPER_BOUND_CAVEAT = (
    "UPPER BOUND, not settled capability (task 4b.23): judge discrimination "
    "was measured 19/19 on clean one-sentence candidates where supersession "
    "direction is unambiguous. Real responses are multi-document dumps. The "
    "same direction swap on actual response text has not been run."
)


def prompt_fingerprint() -> str:
    """Stable id for the exact judge prompt template a verdict was produced
    under, so a published figure can be tied back to the wording that made it."""

    digest = hashlib.sha256(JUDGE_PROMPT_TEMPLATE.encode("utf-8")).hexdigest()
    return f"judge-prompt:{digest[:12]}"


def expected_summary_for(expected: ExpectedRecord) -> str:
    """The ``expected_summary`` field of a judge prompt.

    Pinned, character for character, to the rendering used by the probe whose
    19/19 result justifies this dimension existing at all (the committed
    artifact ``benchmarks/judge-agreement/judge-vs-gates/
    direction-discrimination-items.json``). "Improving" this string silently
    invalidates the evidence the dimension is published on; changing it means
    re-running the probe first. ``tests/test_membench_judge_wiring.py`` pins it
    against that artifact.
    """

    return (
        f"answer kind {expected.answer.kind}; "
        f"acceptable values: {list(expected.answer.values)}; "
        f"abstain={expected.abstain}"
    )


@dataclass(frozen=True)
class JudgeProvenance:
    """What produced a judged verdict, recorded so a figure can be audited."""

    backend: str
    prompt_id: str
    model_ids: tuple[str, ...]
    samples_total: int
    samples_valid: int
    semantic_matches: int
    caveat: str = JUDGE_UPPER_BOUND_CAVEAT

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "prompt_id": self.prompt_id,
            "model_ids": list(self.model_ids),
            "samples_total": self.samples_total,
            "samples_valid": self.samples_valid,
            "semantic_matches": self.semantic_matches,
            "caveat": self.caveat,
        }


@dataclass(frozen=True)
class JudgedItem:
    """One judged verdict. Deliberately NOT a :class:`ScoreItem`.

    ``base_dimension`` names the deterministic dimension whose UNSUPPORTED row
    this speaks to. It exists so a reader can see *which* deterministic cell
    the judged contribution sits beside — never so the two can be summed.
    """

    query_id: str
    gate: str
    base_dimension: str
    status: GateStatus
    evidence: str
    provenance: JudgeProvenance | None = None
    dimension: str = JUDGED_DIMENSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "gate": self.gate,
            "base_dimension": self.base_dimension,
            "dimension": self.dimension,
            "status": self.status.value,
            "evidence": self.evidence,
            "source": "judge",
            "provenance": None if self.provenance is None else self.provenance.as_dict(),
        }


@dataclass(frozen=True)
class JudgeCandidate:
    """One row the judge may speak to, with its blinding inputs already chosen."""

    query_id: str
    question: str
    expected_summary: str
    candidate_answer: str
    gates: tuple[ScoreItem, ...]


def _resolvable(items: Sequence[ScoreItem]) -> tuple[ScoreItem, ...]:
    return tuple(
        item
        for item in items
        if item.gate in JUDGE_RESOLVABLE_GATES and item.status is GateStatus.UNSUPPORTED
    )


def candidate_for(
    query: QueryRecord,
    expected: ExpectedRecord,
    answer: AnswerRecord,
    items: Sequence[ScoreItem],
) -> JudgeCandidate | None:
    """The judge candidate for one query, or ``None`` when there is nothing
    a judge may or usefully could decide.

    ``None`` for three reasons, all of which leave the row UNSUPPORTED:

    - no judge-resolvable gate reported UNSUPPORTED (the overwhelming
      majority of rows — this is what keeps the dimension non-redundant and
      the judge bill small);
    - the answer has no content to grade (an abstention or empty response is
      already decided by ``gate_abstention``, a deterministic gate);
    - the record states no acceptable values, so the prompt would carry an
      empty expectation and any verdict would be unfounded.
    """

    gates = _resolvable(items)
    if not gates:
        return None
    if answer.abstained or not answer.answer_text.strip():
        return None
    if not expected.answer.values:
        return None
    return JudgeCandidate(
        query_id=query.query_id,
        question=query.prompt_text,
        expected_summary=expected_summary_for(expected),
        candidate_answer=answer.answer_text,
        gates=gates,
    )


def request_items(
    candidates: Sequence[JudgeCandidate], *, provider_token: str
) -> list[RequestItem]:
    """Blinded handshake requests for ``candidates`` (blinding is
    :func:`~membench.judge.backends.make_judge_item`'s job, not ours)."""

    return [
        make_judge_item(
            candidate.query_id,
            question=candidate.question,
            expected_summary=candidate.expected_summary,
            candidate_answer=candidate.candidate_answer,
            provider_token=provider_token,
        )
        for candidate in candidates
    ]


def _guard(candidate: JudgeCandidate) -> None:
    for item in candidate.gates:
        if item.status is not GateStatus.UNSUPPORTED:
            raise ValueError(
                f"judged lane asked to speak to {item.gate!r} on {item.query_id} "
                f"with deterministic status {item.status.value!r}; deterministic "
                "gates are final and only UNSUPPORTED may be resolved"
            )


def unresolved(
    candidates: Sequence[JudgeCandidate], *, cause: str
) -> list[JudgedItem]:
    """UNSUPPORTED judged items naming why no verdict exists.

    A judge failure — backend skipped, malformed JSON, refusal, timeout,
    leakage refusal — is recorded here and never becomes a guess. The run
    stays valid and the contender loses nothing.
    """

    out: list[JudgedItem] = []
    for candidate in candidates:
        _guard(candidate)
        for item in candidate.gates:
            out.append(
                JudgedItem(
                    query_id=candidate.query_id,
                    gate=item.gate,
                    base_dimension=item.dimension,
                    status=GateStatus.UNSUPPORTED,
                    evidence=f"no judged verdict: {cause}",
                )
            )
    return out


def _sample_models(samples: Sequence[dict]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for sample in samples:
        model_id = sample.get("model_id")
        if isinstance(model_id, str) and model_id:
            seen.setdefault(model_id)
    return tuple(seen)


def _sample_errors(samples: Sequence[dict]) -> str:
    errors = [str(sample.get("error")) for sample in samples if "error" in sample]
    return ", ".join(errors) if errors else "no samples recorded"


def _first_reason(samples: Sequence[dict]) -> str:
    for sample in samples:
        reason = sample.get("reason")
        if isinstance(reason, str) and reason.strip():
            return reason.strip()[:200]
    return ""


def resolve(
    candidates: Sequence[JudgeCandidate],
    judge_rows: dict[str, dict],
    *,
    backend: str,
) -> list[JudgedItem]:
    """Turn merged judge samples into judged verdicts.

    ``judge_rows`` is ``judge-scores.json``'s ``per_query``, keyed by query id.
    A row with no valid sample stays UNSUPPORTED with the cause named — an
    unparseable or missing verdict is never a match and never a guess.
    """

    prompt_id = prompt_fingerprint()
    out: list[JudgedItem] = []
    for candidate in candidates:
        _guard(candidate)
        row = judge_rows.get(candidate.query_id)
        samples: list[dict] = list(row.get("samples", [])) if row else []
        valid = int(row.get("samples_valid", 0)) if row else 0
        total = int(row.get("samples_total", len(samples))) if row else 0
        matches = int(row.get("semantic_matches", 0)) if row else 0
        provenance = JudgeProvenance(
            backend=backend,
            prompt_id=prompt_id,
            model_ids=_sample_models(samples),
            samples_total=total,
            samples_valid=valid,
            semantic_matches=matches,
        )
        if row is None or valid == 0:
            cause = "judge returned no verdict for this row" if row is None else (
                f"no usable sample ({_sample_errors(samples)})"
            )
            status = GateStatus.UNSUPPORTED
            evidence = f"no judged verdict: {cause}"
        else:
            matched = bool(row.get("majority"))
            status = GateStatus.PASS if matched else GateStatus.FAIL
            reason = _first_reason(samples)
            evidence = (
                f"judge {'match' if matched else 'no match'} "
                f"({matches}/{total} sample(s), {valid} valid; "
                f"models {', '.join(provenance.model_ids) or 'unknown'})"
                + (f"; reason: {reason}" if reason else "")
            )
        for item in candidate.gates:
            out.append(
                JudgedItem(
                    query_id=candidate.query_id,
                    gate=item.gate,
                    base_dimension=item.dimension,
                    status=status,
                    evidence=evidence,
                    provenance=provenance,
                )
            )
    return out


def _empty_counts() -> dict[str, int]:
    return {"pass": 0, "fail": 0, "not_applicable": 0, "unsupported": 0}


def summarize_judged(items: Sequence[JudgedItem]) -> dict[str, dict[str, dict[str, int]]]:
    """Judged tallies, sliced three ways.

    ``by_base_dimension`` is the one a reader needs to audit a published
    figure: it says how many verdicts in each deterministic dimension's row
    came from a judge rather than a gate. It is reported NEXT TO that row and
    never inside it.
    """

    by_dimension: dict[str, dict[str, int]] = {}
    by_base: dict[str, dict[str, int]] = {}
    by_gate: dict[str, dict[str, int]] = {}
    for item in items:
        by_dimension.setdefault(item.dimension, _empty_counts())[item.status.value] += 1
        by_base.setdefault(item.base_dimension, _empty_counts())[item.status.value] += 1
        by_gate.setdefault(item.gate, _empty_counts())[item.status.value] += 1
    return {
        "by_dimension": dict(sorted(by_dimension.items())),
        "by_base_dimension": dict(sorted(by_base.items())),
        "by_gate": dict(sorted(by_gate.items())),
    }
