"""Native renderers: express the neutral corpus in each product's grammar.

Every renderer returns a :class:`FactParityReport` classifying every corpus
fact as represented / degraded / unsupported — nothing is silently dropped.
Track B v0.1 ingests *sources only* for every product (altitude parity), so
typed-structure facts are honestly ``degraded`` wherever they ride along as
raw text rather than as the product's typed primitive.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path

from membench.schema import ClaimRecord, EntityRecord, SourceRecord, load_jsonl


class ParityStatus(str, enum.Enum):
    REPRESENTED = "represented"
    DEGRADED = "degraded"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class ParityEntry:
    fact_id: str
    status: ParityStatus
    reason: str | None = None


@dataclass
class FactParityReport:
    renderer: str
    entries: dict[str, ParityEntry] = field(default_factory=dict)

    def record(self, fact_id: str, status: ParityStatus, reason: str | None = None) -> None:
        if fact_id in self.entries:
            raise ValueError(f"{self.renderer}: duplicate parity entry {fact_id}")
        if status is not ParityStatus.REPRESENTED and not reason:
            raise ValueError(f"{self.renderer}: {fact_id} needs a reason for {status.value}")
        self.entries[fact_id] = ParityEntry(fact_id, status, reason)

    def missing(self, fact_ids: list[str]) -> list[str]:
        return [fact_id for fact_id in fact_ids if fact_id not in self.entries]


@dataclass(frozen=True)
class CorpusView:
    """Everything a native renderer needs, loaded from a generated corpus."""

    root: Path
    entities: list[EntityRecord]
    sources: list[SourceRecord]
    claims: list[ClaimRecord]

    def source_text(self, source: SourceRecord) -> str:
        return (self.root / source.path).read_text(encoding="utf-8")

    def entities_by_id(self) -> dict[str, EntityRecord]:
        return {e.entity_id: e for e in self.entities}


def load_corpus_view(corpus_dir: Path) -> CorpusView:
    corpus_dir = Path(corpus_dir)
    return CorpusView(
        root=corpus_dir,
        entities=load_jsonl(EntityRecord, corpus_dir / "entities.jsonl"),
        sources=load_jsonl(SourceRecord, corpus_dir / "sources.jsonl"),
        claims=load_jsonl(ClaimRecord, corpus_dir / "claims.jsonl"),
    )


def corpus_facts(view: CorpusView) -> list[str]:
    """The complete fact inventory a renderer must account for."""

    facts: list[str] = []
    for claim in view.claims:
        for assertion in claim.assertions:
            facts.append(f"assert:{claim.claim_id}:{assertion.source_id}")
        if claim.supersedes:
            facts.append(f"supersedes:{claim.claim_id}:{claim.supersedes}")
    for entity in view.entities:
        for alias in entity.aliases:
            facts.append(f"alias:{entity.entity_id}:{alias}")
    return facts
