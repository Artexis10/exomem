"""Deterministic transparent hybrid-RAG control, intentionally without a reranker."""

from __future__ import annotations

import hashlib
import math
import os
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass

import snowballstemmer
from membench.adapters.base import Profile
from protocol.models import CaseHandle, LaneReadiness, ProtocolEvent

from .base import ProviderHit, require_neutral

_TOKEN = re.compile(r"[A-Za-z0-9']+")


@dataclass(frozen=True)
class HybridRagConfig:
    chunk_tokens: int = 512
    chunk_overlap: int = 64
    bm25_k1: float = 1.2
    bm25_b: float = 0.75
    embedding_model: str = "BAAI/bge-base-en-v1.5"
    rrf_k: int = 60
    fixture_dimensions: int = 32

    def sha256(self) -> str:
        import json
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class _Chunk:
    chunk_id: str
    text: str
    tokens: tuple[str, ...]


def _tokens(text: str) -> list[str]:
    stem = snowballstemmer.stemmer("english")
    return stem.stemWords([token.lower() for token in _TOKEN.findall(text)])


def _fixture_vector(text: str, dimensions: int) -> tuple[float, ...]:
    values = [0.0] * dimensions
    for token in _tokens(text):
        digest = hashlib.sha256(token.encode()).digest()
        index = int.from_bytes(digest[:2], "big") % dimensions
        values[index] += -1.0 if digest[2] & 1 else 1.0
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return tuple(value / norm for value in values)


class HybridRagDirectProvider:
    """Single-threaded exact BM25/vector fusion over per-session chunks."""

    def __init__(self, config: HybridRagConfig = HybridRagConfig()) -> None:
        self.config = config
        self._chunks: list[_Chunk] = []

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
            words = "\n".join(parts).split()
            step = self.config.chunk_tokens - self.config.chunk_overlap
            for offset in range(0, len(words), step):
                window = words[offset : offset + self.config.chunk_tokens]
                if not window:
                    continue
                text = " ".join(window)
                digest = hashlib.sha256(f"{handle.case_id}:{ordinal}:{offset}:{text}".encode()).hexdigest()
                self._chunks.append(_Chunk(digest, text, tuple(_tokens(text))))
                inserted.append(digest)
                if offset + self.config.chunk_tokens >= len(words):
                    break
        return tuple(inserted)

    def _bm25(self, query: str) -> list[str]:
        terms = _tokens(query)
        count = len(self._chunks)
        avg_length = sum(len(chunk.tokens) for chunk in self._chunks) / count if count else 0.0
        docs = [Counter(chunk.tokens) for chunk in self._chunks]
        scores: list[tuple[float, str]] = []
        for chunk, doc in zip(self._chunks, docs, strict=True):
            score = 0.0
            for term in terms:
                df = sum(term in candidate for candidate in docs)
                if not df:
                    continue
                tf = doc[term]
                idf = math.log((count - df + 0.5) / (df + 0.5) + 1.0)
                score += idf * (tf * (self.config.bm25_k1 + 1.0)) / (tf + self.config.bm25_k1 * (1.0 - self.config.bm25_b + self.config.bm25_b * len(chunk.tokens) / (avg_length or 1.0)))
            scores.append((score, chunk.chunk_id))
        return [item[1] for item in sorted(scores, key=lambda item: (-item[0], item[1]))]

    def _semantic(self, query: str) -> list[str]:
        q = _fixture_vector(query, self.config.fixture_dimensions)
        scored = [(sum(a * b for a, b in zip(q, _fixture_vector(chunk.text, self.config.fixture_dimensions), strict=True)), chunk.chunk_id) for chunk in self._chunks]
        return [item[1] for item in sorted(scored, key=lambda item: (-item[0], item[1]))]

    def retrieve(self, question_text: str, top_k: int) -> list[ProviderHit]:
        if top_k < 0:
            raise ValueError("top_k must be non-negative")
        ranks: dict[str, float] = {}
        for ranking in (self._bm25(question_text), self._semantic(question_text)):
            for rank, chunk_id in enumerate(ranking, 1):
                ranks[chunk_id] = ranks.get(chunk_id, 0.0) + 1.0 / (self.config.rrf_k + rank)
        texts = {chunk.chunk_id: chunk.text for chunk in self._chunks}
        return [ProviderHit(chunk_id, texts[chunk_id], score) for chunk_id, score in sorted(ranks.items(), key=lambda item: (-item[1], item[0]))[:top_k]]

    def export_state(self) -> tuple[_Chunk, ...]:
        return tuple(self._chunks)

    def cleanup(self) -> None:
        self._chunks.clear()

    def variant_id(self) -> str:
        return "hybrid-rag-control"

    def readiness(self) -> list[LaneReadiness]:
        fixture = os.environ.get("PROTOCOL_FIXTURE_EMBEDDER") == "1"
        return [LaneReadiness(lane=self.variant_id(), requested=True, verified=fixture, method="config-state", evidence="deterministic fixture embedder" if fixture else "fixture embedder disabled")]
