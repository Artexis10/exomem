"""Data contracts shared by the find pipeline and callers."""

from __future__ import annotations

import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GraphProvenance:
    """Why a hit entered results through the typed graph lane.

    Records the FIRST typed edge that surfaced a graph-expanded target: the
    relation type, the edge direction relative to the seed
    ("outbound"/"inbound"), and the seed page it hopped from. Populated only in
    typed mode for targets not already in the vector/BM25 primary set.
    """

    relation_type: str | None
    direction: str
    seed: str


@dataclass
class ParsedPage:
    path: Path
    rel_path: str
    frontmatter: dict[str, Any]
    body: str
    title: str
    mtime: float

    @property
    def page_type(self) -> str | None:
        t = self.frontmatter.get("type")
        return str(t) if t else None

    @property
    def scope(self) -> str | None:
        """Per-type scope used by the public search result shape."""
        fm = self.frontmatter
        t = self.page_type

        def _project_or_projects() -> str | None:
            if proj := fm.get("project"):
                return str(proj)
            if projects := fm.get("projects"):
                if isinstance(projects, list) and projects:
                    return ",".join(str(p) for p in projects)
                return str(projects)
            return None

        if t == "production-log":
            return str(fm["medium"]) if fm.get("medium") else None
        if t == "experiment":
            return str(fm["domain"]) if fm.get("domain") else None
        if t == "entity":
            return str(fm["entity_type"]) if fm.get("entity_type") else None
        if t == "source":
            return str(fm["source_type"]) if fm.get("source_type") else None
        if t in ("research-note", "pattern", "insight", "failure"):
            return _project_or_projects()

        return (
            _project_or_projects()
            or (str(fm["domain"]) if fm.get("domain") else None)
            or (str(fm["medium"]) if fm.get("medium") else None)
            or (str(fm["entity_type"]) if fm.get("entity_type") else None)
        )

    @property
    def updated(self) -> str:
        u = self.frontmatter.get("updated") or self.frontmatter.get("captured") or ""
        return str(u)

    @property
    def tags(self) -> list[str]:
        t = self.frontmatter.get("tags") or []
        return [str(x).lower() for x in t] if isinstance(t, list) else []

    @property
    def speakers(self) -> list[str]:
        s = self.frontmatter.get("speakers") or []
        return [str(x) for x in s] if isinstance(s, list) else []

    @property
    def media_type(self) -> str | None:
        mt = self.frontmatter.get("media_type")
        return str(mt) if mt else None

    @property
    def media_file(self) -> str | None:
        ef = self.frontmatter.get("evidence_file")
        return str(ef) if ef else None

    @property
    def parent_media(self) -> str | None:
        pm = self.frontmatter.get("parent_media")
        return str(pm) if pm else None

    @property
    def frame_ts(self) -> float | None:
        v = self.frontmatter.get("frame_ts")
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    @property
    def file_kind(self) -> str:
        if self.page_type == "dataset":
            fmt = self.frontmatter.get("format")
            return str(fmt).lower() if fmt else "dataset"
        if self.media_type:
            return self.media_type.lower()
        return "note"

    @property
    def status(self) -> str | None:
        s = self.frontmatter.get("status")
        return str(s) if s else None

    @property
    def superseded_by(self) -> list[str]:
        sb = self.frontmatter.get("superseded_by")
        if not sb:
            return []
        return [str(x) for x in sb] if isinstance(sb, list) else [str(sb)]

    @property
    def supersedes(self) -> list[str]:
        sv = self.frontmatter.get("supersedes")
        if not sv:
            return []
        return [str(x) for x in sv] if isinstance(sv, list) else [str(sv)]

    @cached_property
    def body_stripped(self) -> str:
        return self.body.strip()

    @cached_property
    def body_norm(self) -> str:
        return self.body_stripped.lower()

    @cached_property
    def title_norm(self) -> str:
        return self.title.lower()

    @cached_property
    def stem_set(self) -> frozenset[str]:
        from . import bm25

        return frozenset(bm25.tokenize(self.title + " " + self.body))


def _format_timestamp(seconds: float) -> str:
    """Seconds to mm:ss, or h:mm:ss past an hour."""
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


@dataclass
class Hit:
    path: str
    type: str | None
    scope: str | None
    title: str
    updated: str
    excerpt: str
    bm25_rank: int | None = None
    vector_rank: int | None = None
    vector_score: float | None = None
    clip_rank: int | None = None
    clip_score: float | None = None
    graph_hop: bool = False
    graph_in_degree: int = 0
    keyword_rank: int | None = None
    rerank_score: float | None = None
    outside_kb: bool = False
    media_type: str | None = None
    media_file: str | None = None
    clip_frame_ts: float | None = None
    scene_frame: str | None = None
    scene_frame_ts: float | None = None
    transcript_ts: float | None = None
    status: str | None = None
    superseded_by: list[str] = field(default_factory=list)
    activation: float | None = None
    usage_boost_applied: float | None = None
    graph_provenance: GraphProvenance | None = None

    def as_dict(self) -> dict:
        out: dict = {
            "path": self.path,
            "type": self.type,
            "scope": self.scope,
            "title": self.title,
            "updated": self.updated,
            "excerpt": self.excerpt,
        }
        if self.graph_provenance is not None:
            out["graph"] = {
                "relation_type": self.graph_provenance.relation_type,
                "direction": self.graph_provenance.direction,
                "seed": self.graph_provenance.seed,
            }
        if self.media_type:
            out["media_type"] = self.media_type
        if self.media_file:
            out["media_file"] = self.media_file
        if self.clip_frame_ts is not None:
            out["clip_match_at"] = _format_timestamp(self.clip_frame_ts)
        if self.scene_frame:
            out["scene_frame"] = self.scene_frame
            if self.scene_frame_ts is not None:
                out["scene_match_at"] = _format_timestamp(self.scene_frame_ts)
        if self.transcript_ts is not None:
            out["transcript_match_at"] = _format_timestamp(self.transcript_ts)
        if self.outside_kb:
            out["outside_kb"] = True
        if self.status and self.status != "active":
            out["status"] = self.status
        if self.superseded_by:
            out["superseded_by"] = self.superseded_by
        signals: dict = {}
        if self.bm25_rank is not None:
            signals["bm25_rank"] = self.bm25_rank
        if self.vector_rank is not None:
            signals["vector_rank"] = self.vector_rank
        if self.vector_score is not None:
            signals["vector_score"] = round(self.vector_score, 4)
        if self.clip_rank is not None:
            signals["clip_rank"] = self.clip_rank
        if self.clip_score is not None:
            signals["clip_score"] = round(self.clip_score, 4)
        if self.clip_frame_ts is not None:
            signals["clip_frame_ts"] = round(self.clip_frame_ts, 2)
        if self.graph_hop:
            signals["graph_hop"] = True
        if self.graph_in_degree:
            signals["graph_in_degree"] = self.graph_in_degree
        if self.keyword_rank is not None:
            signals["keyword_rank"] = self.keyword_rank
        if self.rerank_score is not None:
            signals["rerank_score"] = round(self.rerank_score, 4)
        if self.activation is not None:
            signals["activation"] = round(self.activation, 4)
        if self.usage_boost_applied is not None:
            signals["usage_boost"] = round(self.usage_boost_applied, 4)
        if signals:
            out["signals"] = signals
        return out

    def as_compact_dict(self) -> dict:
        out: dict = {
            "path": self.path,
            "type": self.type,
            "scope": self.scope,
            "title": self.title,
            "updated": self.updated,
        }
        if self.media_type:
            out["media_type"] = self.media_type
        if self.media_file:
            out["media_file"] = self.media_file
        if self.clip_frame_ts is not None:
            out["clip_match_at"] = _format_timestamp(self.clip_frame_ts)
        if self.scene_frame:
            out["scene_frame"] = self.scene_frame
            if self.scene_frame_ts is not None:
                out["scene_match_at"] = _format_timestamp(self.scene_frame_ts)
        if self.transcript_ts is not None:
            out["transcript_match_at"] = _format_timestamp(self.transcript_ts)
        if self.outside_kb:
            out["outside_kb"] = True
        if self.status and self.status != "active":
            out["status"] = self.status
        if self.superseded_by:
            out["superseded_by"] = self.superseded_by
        return out


class FindTimings:
    """Opt-in per-stage timing collector for one find call."""

    def __init__(self) -> None:
        self._t0 = time.perf_counter()
        self.stages: dict[str, dict[str, Any]] = {}
        self.cache: dict[str, Any] = {"enabled": False, "hit": False}
        self.profile: dict[str, Any] = {}

    @contextmanager
    def span(self, name: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            entry = self.stages.setdefault(name, {})
            entry["ms"] = round((time.perf_counter() - t0) * 1000.0, 3)

    def skipped(self, name: str) -> None:
        self.stages.setdefault(name, {})["skipped"] = True

    def error(self, name: str, exc: BaseException) -> None:
        self.stages.setdefault(name, {})["error"] = type(exc).__name__

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_ms": round((time.perf_counter() - self._t0) * 1000.0, 3),
            "cache": dict(self.cache),
            "profile": dict(self.profile),
            "stages": {k: dict(v) for k, v in self.stages.items()},
        }


def timing_span(timings: FindTimings | None, name: str):
    """A timing span when a collector is present, else a no-op context."""
    return timings.span(name) if timings is not None else nullcontext()
