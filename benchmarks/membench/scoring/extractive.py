"""Deterministic extractive answerer — the model-free substrate baseline.

Pure assembly over retrieval hits: no model, no expected-record access. It
answers with bounded stored text from the top hits and cites their sentinels;
with no hits it abstains. Its failures are real product findings (e.g. it
cannot abstain on a lexically-matching distractor), which is the point.
"""

from __future__ import annotations

from membench.adapters.base import Hit
from membench.schema import QueryRecord
from membench.scoring.answer_contract import AnswerRecord, extract_structure

_TOP_HITS = 3
_CHARS_PER_HIT = 800


def build_answer(
    query: QueryRecord, hits: list[Hit], *, latency_ms: float | None = None
) -> AnswerRecord:
    if not hits:
        return AnswerRecord(
            query_id=query.query_id,
            answer_text="",
            abstained=True,
            latency_ms=latency_ms,
        )
    used = hits[:_TOP_HITS]
    chunks: list[str] = []
    citations: list[str] = []
    for hit in used:
        body = hit.text or hit.excerpt or hit.title or ""
        chunks.append(body[:_CHARS_PER_HIT])
        for sentinel in hit.sentinels:
            if sentinel not in citations:
                citations.append(sentinel)
    record = AnswerRecord(
        query_id=query.query_id,
        answer_text="\n\n".join(chunk for chunk in chunks if chunk),
        citations=citations,
        abstained=False,
        latency_ms=latency_ms,
    )
    return extract_structure(record)
