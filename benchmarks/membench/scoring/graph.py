"""Graph-facts scorer: deterministic multi-hop verdicts.

What t14 actually emits (read before writing this scorer): its multi-hop
query carries NO dedicated graph gate string — the expectation is encoded as
``query_kind="multi_hop"`` plus a required claim whose ``derived_from`` chain
names the intermediate hops (``expect_value(c_lead)`` where ``c_lead`` is
derived from the ownership claim), and ``expected.answer.values`` holds the
chain endpoint. This scorer keys on exactly that encoding: the endpoint value
must appear in the answer, and answering with an intermediate hop's value
instead is a distinct ``wrong hop`` failure.
"""

from __future__ import annotations

from membench.schema import ExpectedRecord, QueryRecord
from membench.scoring.answer_contract import AnswerRecord
from membench.scoring.gates import GateStatus, ScoreItem, ScoringContext

GRAPH_DIMENSION = "graph"
GRAPH_GATE = "graph_multi_hop"

#: Query kinds that encode a relation/multi-hop expectation.
MULTI_HOP_QUERY_KINDS = frozenset({"multi_hop"})


def hop_chain_claim_ids(expected: ExpectedRecord, ctx: ScoringContext) -> list[str]:
    """Required claims plus their transitive ``derived_from`` claim chain.

    ``derived_from`` may also carry source ids; anything not resolvable as a
    claim is skipped (sources are citation evidence, not hops).
    """

    chain: list[str] = []
    seen: set[str] = set()
    frontier = list(expected.required_claims)
    while frontier:
        claim_id = frontier.pop(0)
        if claim_id in seen:
            continue
        seen.add(claim_id)
        claim = ctx.claims_by_id.get(claim_id)
        if claim is None:
            continue  # not a claim (e.g. a source id ridden along)
        chain.append(claim_id)
        frontier.extend(claim.derived_from)
    return chain


def score_graph(
    query: QueryRecord,
    expected: ExpectedRecord,
    answer: AnswerRecord,
    ctx: ScoringContext,
) -> list[ScoreItem]:
    """One deterministic verdict for the encoded multi-hop expectation."""

    def item(status: GateStatus, evidence: str | None = None) -> list[ScoreItem]:
        return [
            ScoreItem(query.query_id, GRAPH_GATE, GRAPH_DIMENSION, status, evidence)
        ]

    if query.query_kind not in MULTI_HOP_QUERY_KINDS or not expected.answer.values:
        return item(GateStatus.NOT_APPLICABLE)

    text = answer.answer_text
    endpoint_values = expected.answer.values
    if any(value in text for value in endpoint_values):
        return item(GateStatus.PASS)

    chain = hop_chain_claim_ids(expected, ctx)
    endpoint_claims = set(expected.required_claims)
    intermediate_values = [
        value
        for claim_id in chain
        if claim_id not in endpoint_claims
        and (value := ctx.claim_value(claim_id)) is not None
    ]
    named_hops = [value for value in intermediate_values if value in text]
    if named_hops:
        return item(
            GateStatus.FAIL,
            f"wrong hop: answered intermediate {named_hops[0]!r} instead of "
            f"chain endpoint {endpoint_values}",
        )
    return item(
        GateStatus.FAIL, f"chain endpoint absent: none of {endpoint_values} in answer"
    )
