"""Deterministic transparent hybrid-RAG control, intentionally without a reranker.

Chunking is sentence-boundary preferred: a session's text is split on sentence
ends and packed to at most ``chunk_tokens`` tokens of the *same* tokenization
BM25 indexes (regex word split plus the English Snowball stemmer), with a
sentence-aligned overlap of at most ``chunk_overlap`` tokens.  A chunk never
spans two sessions.  An oversized single sentence is hard-split on word windows
so one runaway sentence cannot silently exceed the declared window.

Both retrieval tables are built once per ingest: BM25 document frequencies live
in ``rank_bm25.BM25Okapi`` (the implementation the brief named) and every chunk
carries its embedding vector.  Queries reuse those tables, so retrieval cost is
linear in the chunk count rather than quadratic in corpus size.

The embedding seam is explicit: with ``PROTOCOL_FIXTURE_EMBEDDER=1`` a
deterministic 32-dimension hash embedder serves, and the config — and therefore
``control_config_sha256`` and ``variant_id()`` — say so.  No dead model field
is carried in the hash, because a hash must describe what actually ran.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace

import snowballstemmer
from membench.adapters.base import Profile
from protocol.models import CaseHandle, LaneReadiness, ProtocolEvent
from rank_bm25 import BM25Okapi

from .base import ProviderHit, require_neutral

_TOKEN = re.compile(r"[A-Za-z0-9']+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_STEMMER = snowballstemmer.stemmer("english")


def tokenize(text: str) -> list[str]:
    """The single tokenization used by chunk packing, BM25, and the embedder."""

    return _STEMMER.stemWords([token.lower() for token in _TOKEN.findall(text)])


@dataclass(frozen=True)
class HybridRagConfig:
    chunk_tokens: int = 512
    chunk_overlap: int = 64
    chunking: str = "sentence-preferred-packed"
    tokenization: str = "regex-word+snowball-english"
    bm25_implementation: str = "rank_bm25.BM25Okapi"
    bm25_k1: float = 1.2
    bm25_b: float = 0.75
    rrf_k: int = 60
    fixture_dimensions: int = 32
    embedding_backend: str = "bge-base-en-v1.5"

    def sha256(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class _Chunk:
    chunk_id: str
    text: str
    tokens: tuple[str, ...]
    session_ordinal: int
    vector: tuple[float, ...] = field(default=())


def _fixture_vector(text: str, dimensions: int) -> tuple[float, ...]:
    values = [0.0] * dimensions
    for token in tokenize(text):
        digest = hashlib.sha256(token.encode()).digest()
        index = int.from_bytes(digest[:2], "big") % dimensions
        values[index] += -1.0 if digest[2] & 1 else 1.0
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return tuple(value / norm for value in values)


def pack_sentences(text: str, *, chunk_tokens: int, chunk_overlap: int) -> list[str]:
    """Pack sentence units into <=chunk_tokens windows with sentence-aligned overlap."""

    if chunk_tokens < 1 or chunk_overlap < 0 or chunk_overlap >= chunk_tokens:
        raise ValueError("chunk_overlap must be smaller than a non-empty chunk_tokens window")
    units: list[tuple[str, int]] = []
    for sentence in (part.strip() for part in _SENTENCE_END.split(text)):
        if not sentence:
            continue
        length = len(tokenize(sentence))
        if length <= chunk_tokens:
            units.append((sentence, length))
            continue
        words = sentence.split()
        for start in range(0, len(words), chunk_tokens):
            window = " ".join(words[start : start + chunk_tokens])
            units.append((window, len(tokenize(window))))
    chunks: list[str] = []
    index = 0
    while index < len(units):
        end, total = index, 0
        while end < len(units) and (end == index or total + units[end][1] <= chunk_tokens):
            total += units[end][1]
            end += 1
        chunks.append(" ".join(unit[0] for unit in units[index:end]))
        if end >= len(units):
            break
        # Rewind whole sentences for the overlap; index always advances by >=1
        # unit, so an overlap can never stall the packer.
        back, carried = end, 0
        while back > index + 1 and carried + units[back - 1][1] <= chunk_overlap:
            back -= 1
            carried += units[back][1]
        index = back
    return chunks


class HybridRagDirectProvider:
    """Single-threaded exact BM25/vector fusion over per-session chunks."""

    def __init__(self, config: HybridRagConfig = HybridRagConfig()) -> None:
        self.config = replace(
            config,
            embedding_backend="fixture-hash-32" if os.environ.get("PROTOCOL_FIXTURE_EMBEDDER") == "1" else config.embedding_backend,
        )
        self._chunks: list[_Chunk] = []
        self._index: BM25Okapi | None = None

    def setup(self, profile: Profile | None) -> None:
        del profile
        if os.environ.get("PROTOCOL_FIXTURE_EMBEDDER") != "1":
            # The model seam is explicit: production wiring may supply it, while
            # offline benchmark runs never download a model accidentally.
            raise RuntimeError("hybrid RAG requires PROTOCOL_FIXTURE_EMBEDDER=1 in offline mode")

    def ingest_case(self, events: Sequence[ProtocolEvent], handle: CaseHandle) -> tuple[str, ...]:
        require_neutral(events, handle)
        grouped: dict[int, list[str]] = {}
        for event in events:
            grouped.setdefault(event.session_ordinal, []).append(event.content)
        inserted: list[str] = []
        for ordinal, parts in sorted(grouped.items()):
            packed = pack_sentences(
                "\n".join(parts), chunk_tokens=self.config.chunk_tokens, chunk_overlap=self.config.chunk_overlap
            )
            for offset, text in enumerate(packed):
                digest = hashlib.sha256(f"{handle.case_id}:{ordinal}:{offset}:{text}".encode()).hexdigest()
                self._chunks.append(
                    _Chunk(digest, text, tuple(tokenize(text)), ordinal, _fixture_vector(text, self.config.fixture_dimensions))
                )
                inserted.append(digest)
        # One pass at ingest builds the document-frequency table every later
        # query reuses; chunk embeddings were cached in the loop above.
        self._index = (
            BM25Okapi([list(chunk.tokens) for chunk in self._chunks], k1=self.config.bm25_k1, b=self.config.bm25_b)
            if self._chunks
            else None
        )
        return tuple(inserted)

    def lexical_ranking(self, query: str) -> list[str]:
        if self._index is None:
            return []
        scores = self._index.get_scores(tokenize(query))
        return [
            chunk_id
            for _score, chunk_id in sorted(
                (-float(score), chunk.chunk_id) for score, chunk in zip(scores, self._chunks, strict=True)
            )
        ]

    def semantic_ranking(self, query: str) -> list[str]:
        vector = _fixture_vector(query, self.config.fixture_dimensions)
        return [
            chunk_id
            for _score, chunk_id in sorted(
                (-sum(a * b for a, b in zip(vector, chunk.vector, strict=True)), chunk.chunk_id) for chunk in self._chunks
            )
        ]

    def retrieve(self, question_text: str, top_k: int) -> list[ProviderHit]:
        if top_k < 0:
            raise ValueError("top_k must be non-negative")
        ranks: dict[str, float] = {}
        for ranking in (self.lexical_ranking(question_text), self.semantic_ranking(question_text)):
            for rank, chunk_id in enumerate(ranking, 1):
                ranks[chunk_id] = ranks.get(chunk_id, 0.0) + 1.0 / (self.config.rrf_k + rank)
        texts = {chunk.chunk_id: chunk.text for chunk in self._chunks}
        # Equal fused scores resolve on the chunk id, so a tie is deterministic.
        ordered = sorted(ranks.items(), key=lambda item: (-item[1], item[0]))[:top_k]
        return [ProviderHit(chunk_id, texts[chunk_id], score) for chunk_id, score in ordered]

    def export_state(self) -> tuple[_Chunk, ...]:
        return tuple(self._chunks)

    def cleanup(self) -> None:
        self._chunks.clear()
        self._index = None

    def variant_id(self) -> str:
        return "hybrid-rag-fixture" if self.config.embedding_backend == "fixture-hash-32" else "hybrid-rag-control"

    def readiness(self) -> list[LaneReadiness]:
        fixture = self.config.embedding_backend == "fixture-hash-32"
        return [
            LaneReadiness(
                lane=self.variant_id(), requested=True, verified=fixture, method="config-state",
                evidence="deterministic fixture embedder" if fixture else "fixture embedder disabled",
            )
        ]
