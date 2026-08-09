"""Answer envelope + add-only normalization.

The extractor can only ADD structure (sentinels found in text, a hedged flag
that was previously unset). It can never flip gate-relevant fields: an
explicit ``abstained``/``clarification_question``/``hedged`` value and
existing citations are preserved verbatim.
"""

from __future__ import annotations

from pydantic import Field

from membench.ids import sentinels_in
from membench.schema import StrictModel

_HEDGE_MARKERS = (
    "may ",
    "might ",
    "uncertain",
    "not confirmed",
    "tentative",
    "disputed",
    "conflicting",
    "unclear",
    "provisional",
    "appears to",
    "reportedly",
)


class AnswerRecord(StrictModel):
    query_id: str
    provider_token: str | None = None  # blinded provider identity
    answer_text: str = ""
    citations: list[str] = Field(default_factory=list)  # sentinel source ids
    abstained: bool = False
    clarification_question: str | None = None
    hedged: bool | None = None
    latency_ms: float | None = None
    raw: dict | None = None
    #: The provider supplied these citations itself, so they are a CLOSED
    #: claim about what it used. Sentinel harvesting is suppressed: a native
    #: answer that quotes a document must not silently acquire that document
    #: as a citation, or provenance goes back to measuring text content rather
    #: than the system's own attribution. Recorded in the artifacts so a reader
    #: can tell which regime produced any citation set.
    citations_are_native: bool = False


def detect_hedging(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _HEDGE_MARKERS)


def extract_structure(record: AnswerRecord) -> AnswerRecord:
    """Return a copy with derivable structure added; nothing gate-relevant
    is ever removed or overwritten.

    Citation harvesting is skipped for native answers. Everything else —
    hedging inference above all — still applies, because those are add-only
    derivations of what the provider actually emitted, whereas harvesting
    would *manufacture* an attribution the provider never made.
    """

    citations = list(record.citations)
    if not record.citations_are_native:
        for sentinel in sentinels_in(record.answer_text):
            if sentinel not in citations:
                citations.append(sentinel)
    hedged = record.hedged
    if hedged is None and record.answer_text:
        hedged = detect_hedging(record.answer_text)
    return record.model_copy(update={"citations": citations, "hedged": hedged})
