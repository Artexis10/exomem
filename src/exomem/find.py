"""Read-only search across the Knowledge Base.

Scans every `.md` under `Knowledge Base/`, parses YAML frontmatter, filters by
structured fields, then does case-insensitive substring matching on
title + body. A typical vault is hundreds of pages — full-scan is fast enough.

Cached in-process between calls: keyed by file path, invalidated by mtime.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import logging
import math
import os
import re
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml

from . import freshness

log = logging.getLogger(__name__)

EXCLUDED_DIR_NAMES = frozenset({"_Schema", "_attachments", "_archive", "_trash"})
# Navigation files — auto-generated summaries / activity logs. Their bodies
# mention every recently-written page, so they false-positive on hybrid
# queries that touch any term recently introduced into the KB. Excluded
# from search results regardless of mode.
_NAVIGATION_BASENAMES = frozenset({"index.md", "log.md"})
FRONTMATTER_PATTERN = re.compile(r"^---\n(.*?)\n---\n(.*)", re.DOTALL)
H1_PATTERN = re.compile(r"^# (.+)$", re.MULTILINE)

EXCERPT_RADIUS = 100  # chars on each side of the match
EXCERPT_MAX_LEN = 220

# --- Silent-degradation counter (process-scoped observability) --------------
# Every semantic lane has a soft-fallback, and a POST-WARM failure historically
# dropped the request to a weaker ranking (vector→BM25, or every-lane-empty→
# keyword) emitting nothing but a log line — so a persistently broken sidecar or
# a flapping model was invisible in aggregate. These counters make the fallbacks
# countable: doctor (and any future health endpoint) can read them, and each
# find carries a per-request `degraded` envelope marker built from `failed_out`
# below (distinct from the `warming` marker, which means "lane deferred while a
# model preload is still in flight", not "lane failed"). Thread-safe — find runs
# on FastMCP/REST worker + file-watcher threads concurrently.
_DEGRADATION_LOCK = threading.Lock()
_DEGRADATION_COUNTS: dict[str, int] = {}


def _record_degradation(lane: str) -> None:
    """Increment the process-lifetime silent-degradation counter for `lane`."""
    with _DEGRADATION_LOCK:
        _DEGRADATION_COUNTS[lane] = _DEGRADATION_COUNTS.get(lane, 0) + 1


def degradation_counts() -> dict[str, int]:
    """Snapshot of per-lane post-warm silent-degradation counts (process-scoped).

    Keys: "vector" (vector lane failed post-warm → BM25-only ranking), "clip"
    (CLIP image lane failed → image search skipped), "no_candidates" (every lane
    produced nothing → keyword fallback). Empty when nothing degraded this
    process. These count genuine fallbacks, NOT warm-time deferrals.
    """
    with _DEGRADATION_LOCK:
        return dict(_DEGRADATION_COUNTS)


def reset_degradation_counts() -> None:
    """Test hook: zero the degradation counters (process-reset, like clear_cache)."""
    with _DEGRADATION_LOCK:
        _DEGRADATION_COUNTS.clear()


@dataclass(frozen=True)
class RankingConfig:
    """The tunable knobs of the hybrid ranker, in one place.

    Every value here was historically a hardcoded literal scattered through
    `_find_semantic`/`fusion`/`_type_multiplier`. Bundling them lets the
    offline eval harness (`scripts/eval_retrieval.py`) sweep them against a
    golden set and pick winners by NDCG/MRR instead of intuition. The
    field defaults reproduce the pre-refactor behaviour byte-for-byte — see
    `tests/test_ranking_config.py`, which guards that invariant.

    Intentionally NOT exposed on the MCP `find` tool signature: claude.ai
    needs no knobs API. It's an internal seam for measurement + tuning.
    """

    rrf_k: int = 60  # Cormack/Clarke/Buettcher 2009 default; fusion.py
    compiled_boost: float = 1.15  # must equal _COMPILED_BOOST
    source_penalty: float = 0.85  # must equal _SOURCE_PENALTY
    superseded_penalty: float = 0.5  # must equal _SUPERSEDED_PENALTY
    candidate_multiplier: int = 5  # candidate_k = max(limit*mult, floor)
    candidate_floor: int = 50
    graph_seed_cap: int = 20  # per-ranker fanout cap for 1-hop expansion

    # ---- Temporal lane (Gaussian recency) ----
    # `temporal_boost` is the peak multiplier a brand-new page gets on a
    # temporal query: 1.0 = OFF (the default), so recency NEVER perturbs a
    # non-temporal ranking. The post-RRF boost only fires when both
    # `_is_temporal_query(query)` is true AND `temporal_boost != 1.0`.
    temporal_boost: float = 1.0
    temporal_sigma_days: float = 60.0  # Gaussian width: ~halflife of "recent"

    # ---- Usage-activation boost (opt-in via find(prefer_used=true)) ----
    # Bounded multiplicative post-boost from ACT-R activation over the JSONL
    # access logs (see usage.py). `usage_boost` is the CEILING of the
    # multiplier — deliberately below `compiled_boost` so usage can break
    # ties but never override the epistemic hierarchy, and low enough that
    # a superseded tombstone at max usage still loses to its active
    # successor (0.5 × 1.10 < 1.0). `usage_w_surfaced` defaults to 0: being
    # surfaced by find is not a choice anyone made (rich-get-richer guard);
    # reads and citations are genuine selection acts.
    usage_boost: float = 1.10
    usage_decay: float = 0.5
    usage_horizon_days: float = 90.0
    usage_w_surfaced: float = 0.0
    usage_w_read: float = 1.0
    usage_w_cited: float = 2.0

    # ---- Intent-adaptive weighted RRF ----
    # One weight per fusion lane, aligned positionally to LANE_ORDER:
    #   (vector, bm25, keyword, clip, graph, temporal)
    # `conceptual` is fully neutral (all 1.0) so the common case reproduces the
    # pre-feature unweighted RRF byte-for-byte; only the non-conceptual intents
    # diverge. The adaptivity is the feature, not a global ranking change.
    intent_weights_conceptual: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0, 1.0, 1.0)
    # exact: literal lookups — favour the lexical lanes (bm25 + keyword), damp
    # the semantic/connectivity lanes that float topical-but-inexact matches.
    intent_weights_exact: tuple[float, ...] = (0.7, 1.5, 1.5, 1.0, 0.7, 1.0)
    # relationship: "what links/cites/relates to X" — favour the graph lane.
    intent_weights_relationship: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0, 1.8, 1.0)
    # temporal: up-weight the recency lane so newer matches surface first.
    intent_weights_temporal: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0, 1.0, 2.0)

    def intent_weights(self, intent: str) -> tuple[float, ...]:
        """Lane-weight tuple for a classified intent; conceptual (neutral) default."""
        return {
            "exact": self.intent_weights_exact,
            "temporal": self.intent_weights_temporal,
            "relationship": self.intent_weights_relationship,
            "conceptual": self.intent_weights_conceptual,
        }.get(intent, self.intent_weights_conceptual)


# Fusion lane order — the canonical alignment for the per-intent weight tuples.
# MUST match the order lanes are assembled into the weighted RRF in
# `_find_semantic` (see `lane_rankings`).
LANE_ORDER = ("vector", "bm25", "keyword", "clip", "graph", "temporal")

DEFAULT_RANKING = RankingConfig()


# --------------------------------------------------------------------------- #
# Adopted-config seam: the auto-tuner writes a reviewed `ranking_config.json`;
# `find()` loads it once per process when no explicit `config` is passed. Absent
# file (or any parse error) → DEFAULT_RANKING, byte-identical to the in-code
# baseline. The live `op_find` calls find() WITHOUT config (consults disk); the
# eval harnesses pass config= explicitly (hermetic, never touch disk).
# --------------------------------------------------------------------------- #
# src/exomem/find.py → parents[2] is the repo (or deploy-checkout) root.
_REPO_ROOT = Path(__file__).resolve().parents[2]

_ACTIVE_RANKING: RankingConfig | None = None
_ACTIVE_RANKING_LOADED = False


def ranking_config_to_jsonable(cfg: RankingConfig) -> dict[str, Any]:
    """Plain-dict form of a RankingConfig (tuples render as JSON arrays)."""
    return dataclasses.asdict(cfg)


def ranking_config_from_jsonable(data: dict[str, Any]) -> RankingConfig:
    """Build a RankingConfig from a parsed JSON dict, field by field.

    Unknown keys are ignored (schema-drift-safe) and missing keys fall back to
    the dataclass default. Scalar fields are coerced to int/float per their
    default; the four `intent_weights_*` fields are coerced to float tuples and
    MUST have exactly `len(LANE_ORDER)` entries. Raises on an uncoercible value
    or a wrong-length lane tuple so the caller can fail loud.
    """
    field_names = {f.name for f in dataclasses.fields(RankingConfig)}
    unknown = set(data) - field_names
    if unknown:
        log.warning(
            "ranking config: ignoring unknown knob(s): %s", ", ".join(sorted(unknown))
        )
    n_lanes = len(LANE_ORDER)
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(RankingConfig):
        if f.name not in data:
            continue
        value = data[f.name]
        if f.name.startswith("intent_weights_"):
            tup = tuple(float(x) for x in value)
            if len(tup) != n_lanes:
                raise ValueError(
                    f"{f.name}: expected {n_lanes} lane weights, got {len(tup)}"
                )
            kwargs[f.name] = tup
        elif isinstance(f.default, bool):
            kwargs[f.name] = bool(value)
        elif isinstance(f.default, int):
            kwargs[f.name] = int(value)
        else:
            kwargs[f.name] = float(value)
    return RankingConfig(**kwargs)


def _load_adopted_ranking() -> RankingConfig:
    """Resolve the active RankingConfig from disk; DEFAULT_RANKING on absence/error.

    Resolution order:
      1. ``EXOMEM_DISABLE_RANKING_CONFIG`` set → DEFAULT_RANKING (hermetic; the
         test suite sets this so a committed file never pollutes the suite).
      2. ``EXOMEM_RANKING_CONFIG=<path>`` → that path.
      3. ``<repo_root>/ranking_config.json`` if present.
      4. else DEFAULT_RANKING.

    A malformed / wrong-typed / bad-lane-length file fails LOUD (``log.error``)
    and falls back to DEFAULT_RANKING — it never crashes the server and never
    applies a partially-parsed config.
    """
    if os.environ.get("EXOMEM_DISABLE_RANKING_CONFIG"):
        return DEFAULT_RANKING
    override = os.environ.get("EXOMEM_RANKING_CONFIG")
    path = Path(override) if override else _REPO_ROOT / "ranking_config.json"
    if not path.exists():
        return DEFAULT_RANKING
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("ranking config must be a JSON object")
        return ranking_config_from_jsonable(data)
    except Exception as exc:  # noqa: BLE001 — degrade to known-good, loudly.
        log.error(
            "adopted ranking config %s invalid (%s); using DEFAULT_RANKING", path, exc
        )
        return DEFAULT_RANKING


def _active_ranking() -> RankingConfig:
    """The adopted RankingConfig, loaded once per process (memoized)."""
    global _ACTIVE_RANKING, _ACTIVE_RANKING_LOADED
    if not _ACTIVE_RANKING_LOADED:
        _ACTIVE_RANKING = _load_adopted_ranking()
        _ACTIVE_RANKING_LOADED = True
    return _ACTIVE_RANKING if _ACTIVE_RANKING is not None else DEFAULT_RANKING


def reset_active_ranking_cache() -> None:
    """Drop the memoized adopted config (tests; desk-side adopt happens out of band)."""
    global _ACTIVE_RANKING, _ACTIVE_RANKING_LOADED
    _ACTIVE_RANKING = None
    _ACTIVE_RANKING_LOADED = False


@dataclass
class ParsedPage:
    path: Path  # absolute
    rel_path: str  # vault-relative, e.g. "Knowledge Base/Notes/Insights/foo.md"
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
        """Per-type "scope" field as defined in the plan.

        - research-note → project
        - pattern / insight / failure → project (singular) or projects (joined)
        - experiment → domain
        - production-log → medium
        - entity → entity_type
        - source → source_type
        Fallback: project / projects / domain / medium / entity_type in that order.
        """
        fm = self.frontmatter
        t = self.page_type

        def _project_or_projects() -> str | None:
            if (proj := fm.get("project")):
                return str(proj)
            if (projects := fm.get("projects")):
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

        # Unknown type: fall back across all candidates
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
        """Named speakers from a diarized media sidecar's `speakers:` frontmatter
        (e.g. `[Alice, Speaker B]`). Empty when undiarized or not a media sidecar."""
        s = self.frontmatter.get("speakers") or []
        return [str(x) for x in s] if isinstance(s, list) else []

    @property
    def media_type(self) -> str | None:
        """audio/video/image/pdf on an Evidence media sidecar, else None."""
        mt = self.frontmatter.get("media_type")
        return str(mt) if mt else None

    @property
    def media_file(self) -> str | None:
        """Vault-relative pointer to the original binary this sidecar describes."""
        ef = self.frontmatter.get("evidence_file")
        return str(ef) if ef else None

    @property
    def parent_media(self) -> str | None:
        """Vault-relative path of the parent video for a scene-frame child sidecar
        (written by scene_frames). None for every other page."""
        pm = self.frontmatter.get("parent_media")
        return str(pm) if pm else None

    @property
    def frame_ts(self) -> float | None:
        """Seconds into the parent video for a scene-frame child (defensive parse)."""
        v = self.frontmatter.get("frame_ts")
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    @property
    def file_kind(self) -> str:
        """Coarse artifact kind for file-type filtering: a dataset's underlying
        format (csv/json/tsv), a binary companion's media_type (pdf/image/audio/
        video), else 'note' for a plain markdown page. The vocabulary `find`'s
        `file_types`/`exclude_file_types` scope on."""
        if self.page_type == "dataset":
            fmt = self.frontmatter.get("format")
            return str(fmt).lower() if fmt else "dataset"
        if self.media_type:
            return self.media_type.lower()
        return "note"

    @property
    def status(self) -> str | None:
        """Lifecycle status — draft / active / superseded / archived (None if unset)."""
        s = self.frontmatter.get("status")
        return str(s) if s else None

    @property
    def superseded_by(self) -> list[str]:
        """Wikilink(s) to the page(s) that replaced this one (empty when not superseded)."""
        sb = self.frontmatter.get("superseded_by")
        if not sb:
            return []
        return [str(x) for x in sb] if isinstance(sb, list) else [str(sb)]

    @property
    def supersedes(self) -> list[str]:
        """Wikilink(s) to the page(s) this one replaced — the backward supersession
        pointer `replace` writes on a new page (empty when it supersedes nothing)."""
        sv = self.frontmatter.get("supersedes")
        if not sv:
            return []
        return [str(x) for x in sv] if isinstance(sv, list) else [str(sv)]

    # ---- Derived-text memoization (computed at most once per page revision;
    # FrontmatterCache replaces the whole ParsedPage on mtime change, so these
    # never go stale). Nothing query-dependent is cached here. ----

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
        """Snowball stems of title+body — the corpus side of the stem gates."""
        from . import bm25

        return frozenset(bm25.tokenize(self.title + " " + self.body))


def _format_timestamp(seconds: float) -> str:
    """Seconds → `mm:ss` (or `h:mm:ss` past an hour) for human-readable video deeplinks."""
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
    # Per-mode ranking signals — populated by hybrid/vector modes only.
    # `None` means the ranker did not surface this path in its top-K. The
    # presence of these fields lets a caller introspect WHY a hit ranked
    # (vector_rank=1 vs bm25_rank=1 vs graph_hop=True) without re-running
    # the query in each mode. Always omitted from as_dict() when empty so
    # keyword-mode callers don't see noise.
    bm25_rank: int | None = None
    vector_rank: int | None = None
    vector_score: float | None = None
    clip_rank: int | None = None      # rank from CLIP text→image visual search
    clip_score: float | None = None   # CLIP cosine similarity (image vs the query)
    graph_hop: bool = False
    graph_in_degree: int = 0
    keyword_rank: int | None = None
    rerank_score: float | None = None
    # True when this hit came from OUTSIDE Knowledge Base/ via scope="kb"
    # auto-widening. Lets the caller (and SKILL.md guidance) see that the
    # search reached past the curated KB into the wider vault. Omitted from
    # as_dict() when False so KB-scoped callers don't see noise.
    outside_kb: bool = False
    # Set when the hit is an Evidence media sidecar — `media_type` is
    # audio/video/image/pdf and `media_file` points at the original binary, so the
    # caller surfaces the FILE as the result (and can mint_download_token it),
    # with the matched transcript/OCR text as the "why". Omitted when absent.
    media_type: str | None = None
    media_file: str | None = None
    # Seconds into a video where its best CLIP keyframe matched the query. Set only
    # on video visual hits (multi-vector index); None for images. Surfaced as a
    # human-readable `clip_match_at` ("14:32") so the caller can deep-link the moment.
    clip_frame_ts: float | None = None
    # Persisted scene frame backing this hit (vault-relative JPEG path) + its
    # timestamp. Set when the hit matched via a scene frame's OCR text (frame
    # children group under the parent video) or resolved as the nearest persisted
    # frame to a CLIP keyframe match. The JPEG is directly downloadable — the
    # visual "why" for the hit. Surfaced as `scene_frame` + `scene_match_at`.
    scene_frame: str | None = None
    scene_frame_ts: float | None = None
    # Seconds into a TIMED transcript where the text match localizes — the
    # matched semantic-segment chunk's leading [timestamp] (vector lane) or the
    # nearest marker preceding the query anchor (BM25/keyword). Surfaced as
    # `transcript_match_at`. Data-driven: flat sidecars never set it.
    transcript_ts: float | None = None
    # Lifecycle. `status` is set when a hit is NOT plain `active`, so a reader can
    # tell a superseded tombstone (or draft) from a live conclusion; `superseded_by`
    # carries the forward pointer(s) to the replacement(s). Surfaced in as_dict()
    # regardless of `prefer_active` — exposure is independent of the ranking
    # demotion. Omitted when status is active/unset so live hits stay noise-free.
    status: str | None = None
    superseded_by: list[str] = field(default_factory=list)
    # Usage-activation transparency (prefer_used=true only): the raw ACT-R
    # activation B and the multiplier it produced, so the caller sees exactly
    # why and by how much a hit was boosted. None (omitted) whenever the
    # boost didn't touch this hit — default responses are byte-identical.
    activation: float | None = None
    usage_boost_applied: float | None = None

    def as_dict(self) -> dict:
        out: dict = {
            "path": self.path,
            "type": self.type,
            "scope": self.scope,
            "title": self.title,
            "updated": self.updated,
            "excerpt": self.excerpt,
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
        """Token-cheap routing shape: enough metadata to pick a page for a
        follow-up `get`, with the token-heavy `excerpt` and `signals` omitted
        (rerun with detail="full" when you need the why)."""
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


@dataclass
class FrontmatterCache:
    """Per-process cache of parsed pages, invalidated by mtime."""

    entries: dict[Path, ParsedPage] = field(default_factory=dict)

    def get(self, path: Path, vault_root: Path) -> ParsedPage | None:
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            self.entries.pop(path, None)
            return None
        cached = self.entries.get(path)
        if cached and cached.mtime == mtime:
            return cached
        parsed = _parse_page(path, mtime, vault_root)
        if parsed is not None:
            self.entries[path] = parsed
        return parsed


_CACHE = FrontmatterCache()


class FindTimings:
    """Opt-in per-stage timing collector for one `find` call.

    Records milliseconds per named retrieval stage plus total elapsed time and
    hot-cache status. Carries NO note content — stage names, numbers, flags,
    and short exception class names only (the spec forbids bodies, excerpts,
    query-expanded text, and vectors in diagnostics).
    """

    def __init__(self) -> None:
        self._t0 = time.perf_counter()
        self.stages: dict[str, dict[str, Any]] = {}
        self.cache: dict[str, Any] = {"enabled": False, "hit": False}

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
            "stages": {k: dict(v) for k, v in self.stages.items()},
        }


def _span(timings: FindTimings | None, name: str):
    """A timing span when a collector is present, else a no-op context."""
    return timings.span(name) if timings is not None else nullcontext()


# --------------------------------------------------------------------------- #
# Hot find cache: bounded in-process LRU over base Hit lists (OpenSpec change
# improve-find-latency-token-cost). Keyed by the FULL recall request (every
# ranking/filtering knob + the resolved RankingConfig, which is frozen and
# hashable) plus a freshness key covering the markdown scope the request can
# see, the embedding/CLIP sidecars when a semantic lane could contribute, and
# today's date (temporal lanes and recency filters are date-relative). Cached
# values are deep-copied on the way in AND out so caller mutation can never
# poison a later response. `EXOMEM_FIND_CACHE_SIZE=0` disables it.
# --------------------------------------------------------------------------- #
_FIND_CACHE: OrderedDict[tuple, list[Hit]] = OrderedDict()
_FIND_CACHE_LOCK = threading.Lock()
_DEFAULT_FIND_CACHE_SIZE = 32


def _find_cache_size() -> int:
    raw = os.environ.get("EXOMEM_FIND_CACHE_SIZE")
    if raw is None or not raw.strip():
        return _DEFAULT_FIND_CACHE_SIZE
    try:
        return max(0, int(raw))
    except ValueError:
        log.warning("EXOMEM_FIND_CACHE_SIZE=%r is not an int; using default", raw)
        return _DEFAULT_FIND_CACHE_SIZE


def _walk_freshness_key(paths) -> tuple[int, int, str]:
    """(file count, max st_mtime_ns, digest of sorted path+mtime pairs).

    Digest-strength on purpose: count/max-mtime alone miss a delete paired
    with a create, a rename (mtime preserved), and a replacement carrying an
    older mtime. Every consumer that caches corpus-derived state (the hot
    find cache, BM25, the wikilink resolver, the inbound-link index) compares
    the whole triple, so those histories now invalidate correctly.
    """
    entries: list[tuple[str, int]] = []
    for p in paths:
        try:
            entries.append((str(p), p.stat().st_mtime_ns))
        except OSError:
            continue
    return freshness.triple_from_entries(entries)


class FreshnessSnapshot:
    """Per-request corpus freshness: each markdown scope is resolved at most
    once per `find()` call, and every consumer (hot-cache key, BM25 rebuild
    check, wikilink-resolver reuse, auto-widen's vault BM25) shares the result
    instead of recomputing. Lazy — a `scope="kb-only"` request never pays the
    vault cost.

    Reads the event-maintained `freshness` registry when it is live for the
    scope (sub-ms, syscall-free); otherwise falls back to a full stat-walk that
    yields a byte-identical triple."""

    def __init__(self, vault_root: Path) -> None:
        self._root = vault_root
        self._kb: tuple[int, int, str] | None = None
        self._vault: tuple[int, int, str] | None = None

    def kb(self) -> tuple[int, int, str]:
        if self._kb is None:
            live = freshness.triple(self._root, "kb")
            if live is not None:
                self._kb = live
            else:
                kb = self._root / "Knowledge Base"
                self._kb = _walk_freshness_key(_walk_md(kb) if kb.is_dir() else ())
        return self._kb

    def vault(self) -> tuple[int, int, str]:
        if self._vault is None:
            live = freshness.triple(self._root, "vault")
            if live is not None:
                self._vault = live
            else:
                from .vault import walk_vault_md

                self._vault = _walk_freshness_key(walk_vault_md(self._root))
        return self._vault

    def for_scope(self, scope: str) -> tuple[int, int, str]:
        return self.vault() if scope == "vault" else self.kb()


def _freshness_key(
    vault_root: Path,
    *,
    scope: str,
    query_norm: str,
    mode: str,
    snapshot: FreshnessSnapshot,
) -> tuple:
    """Freshness inputs that can change this request's answer.

    - scope="kb-only": KB walk key only.
    - scope="vault": full-vault walk key.
    - scope="kb" with a non-empty query: BOTH (auto-widen reserves out-of-KB
      slots on every non-empty query).
    - hybrid/vector modes: the embedding and CLIP sidecar mtimes (0 when
      absent), since sidecar refreshes change semantic results.
    """
    parts: list[Any] = [date.today().toordinal()]
    kb = vault_root / "Knowledge Base"
    if scope in ("kb", "kb-only"):
        parts.append(("kb", *snapshot.kb()))
    if scope == "vault" or (scope == "kb" and query_norm):
        parts.append(("vault", *snapshot.vault()))
    if mode in ("hybrid", "vector"):
        for name in (".embeddings.sqlite", ".clip.sqlite"):
            try:
                parts.append((name, (kb / name).stat().st_mtime_ns))
            except OSError:
                parts.append((name, 0))
    return tuple(parts)


def find(
    vault_root: Path,
    *,
    query: str,
    types: list[str] | None = None,
    projects: list[str] | None = None,
    tags: list[str] | None = None,
    speakers: list[str] | None = None,
    file_types: list[str] | None = None,
    exclude_file_types: list[str] | None = None,
    limit: int = 15,
    scope: str = "kb",
    mode: str = "hybrid",
    graph: bool = True,
    rerank: bool | None = None,
    auto_rerank: bool = False,
    temporal: bool = True,
    intent: str | None = None,
    updated_after: str | None = None,
    updated_before: str | None = None,
    recency_days: int | None = None,
    prefer_compiled: bool = True,
    prefer_active: bool = True,
    prefer_used: bool = False,
    config: RankingConfig | None = None,
    timings: FindTimings | None = None,
    degraded_out: list[str] | None = None,
    failed_out: list[str] | None = None,
) -> list[Hit]:
    """Search the vault. Returns up to `limit` hits.

    `degraded_out`: optional caller-owned list. While the background warm-up
    is in flight (see `readiness`), model-touching lanes (vector, CLIP,
    rerank) are skipped instead of blocking on a model load; each skipped
    lane appends its component name here so the caller can mark the response
    as warming. Empty after the call = full ranking ran. Degradation is
    tracked internally even when the caller passes None, so a lexical-only
    ranking produced mid-warm is never stored in the hot cache.

    `failed_out`: optional caller-owned list, the POST-WARM sibling of
    `degraded_out`. A lane that FAILED (vector/CLIP `except`, or the
    all-lanes-empty keyword fallback) — not merely deferred — appends its lane
    name here and bumps `degradation_counts()`. The caller surfaces this as a
    `degraded` envelope marker, distinct from `warming`. A failed result is also
    never cached (a transient sidecar/model failure must not stick).

    `scope` controls the walk root:
    - "kb" (default): only `Knowledge Base/`. Compiled material + sources.
    - "vault": full vault, including sibling folders outside
      `Knowledge Base/` (e.g. curated, read-only material kept in its own
      top-level folders). Use when you need to discover content outside the
      KB. Existing filters still apply — such pages typically lack structured
      frontmatter so `types`/`projects`/`tags` filters won't match many of
      them; free-text queries work fine.

    `mode` controls the ranker:
    - "hybrid" (default): BM25 + local vector embeddings fused via RRF.
      Best recall on natural-language queries. Empty query falls back to
      keyword behavior (filtered most-recent). Embedding sidecar is
      KB-scoped; with `scope="vault"`, vector results cover KB only
      while BM25 covers the full vault.
    - "keyword": case-insensitive substring matching across title + body,
      sorted most-recently-updated first. The original behavior, preserved
      for backward compatibility.
    - "vector": vector embeddings only, no BM25. Testing aid for
      isolating semantic recall.

    `graph`: when True (default for hybrid/vector), the outbound wikilinks
    of top-ranked BM25/vector candidates contribute a third ranking that
    surfaces 1-hop neighbours of strong matches. Set False for pure
    BM25+vector hybrid without graph expansion.

    `rerank`: True/False forces the BAAI/bge-reranker-base CrossEncoder pass
    on/off; `None` (default) defers to `auto_rerank`. When on, runs the top
    `3 * limit` fused candidates through the reranker and re-sorts by reranker
    score. Recovers ordering quality on ambiguous queries — the LLM-Wiki cases
    where vector floats a topically-off doc to the top. ~50ms / candidate on
    Blackwell. Off by default to keep the model out of the common path.

    `auto_rerank`: when True AND `rerank` is left unset (None), the reranker
    fires only when `should_rerank()` judges it worthwhile (top-3 vector/bm25
    disagreement >50% or a long query). An explicit `rerank=True/False` always
    wins over this. Default False so the suite never loads the model implicitly.

    `temporal`: when True (default), temporal queries (recent/latest/when/...)
    get a recency fusion lane and the optional Gaussian recency boost
    (`config.temporal_boost`). Both are strict no-ops on non-temporal queries,
    so this never perturbs the common case. Set False to disable recency logic.

    `intent`: force the intent label ("exact"/"temporal"/"relationship"/
    "conceptual") instead of classifying from the query text — a testing/override
    seam. None (default) auto-classifies. Drives the per-intent lane weights.

    `updated_after` / `updated_before` (ISO date strings) and `recency_days`
    (int) are an explicit post-filter: hits whose `updated` date falls outside
    the window are dropped (undated hits drop too). All None/off by default.

    `prefer_compiled`: when True (default), applies a small multiplicative
    boost to fused/rerank scores for COMPILED page types (insight, pattern,
    failure, research-note, entity) and a small penalty for raw `source`
    pages. Reflects the KB's epistemic hierarchy — compiled distillations
    are the intentional output, sources are inputs. Set False to retrieve
    raw source discussion verbatim (e.g. "what did I capture from Dr. X").

    `prefer_active`: when True (default), soft-demotes `status: superseded`
    pages so a replaced conclusion can't outrank the page that superseded it.
    The tombstone stays findable (never excluded) and its hit carries `status`
    + `superseded_by` either way, so the reader sees it's superseded and where
    it points. Set False to rank superseded pages on their content alone (e.g.
    "what did I used to think about X").

    `prefer_used`: when True (OFF by default — default ranking is usage-blind
    and byte-identical), applies a bounded, positive-only usage-activation
    boost from the JSONL access logs (see `usage.py`): pages you actually
    read and cite get up to `config.usage_boost` (≤ the compiled boost, so
    usage breaks ties but never overrides the epistemic hierarchy). Never a
    penalty, never creates candidates — it can only reorder pages the
    content lanes already surfaced. Boosted hits expose `signals.activation`
    and `signals.usage_boost`. Strict no-op on cold start, absent logs, or
    `EXOMEM_DISABLE_USAGE_BOOST`. Bypasses the hot find cache.
    """
    if scope not in ("kb", "vault", "kb-only"):
        raise ValueError(
            f"find: scope must be 'kb', 'vault', or 'kb-only', got {scope!r}"
        )
    if mode not in ("hybrid", "keyword", "vector"):
        raise ValueError(
            f"find: mode must be 'hybrid', 'keyword', or 'vector', got {mode!r}"
        )
    if limit < 1:
        limit = 1
    limit = min(limit, 100)
    query_norm = (query or "").lower().strip()

    # One freshness snapshot + one parsed-page memo per request: every
    # consumer below (hot cache, BM25, resolver, auto-widen, boost passes)
    # shares them instead of re-walking / re-stat'ing.
    snapshot = FreshnessSnapshot(vault_root)
    page_memo: dict[str, ParsedPage | None] = {}

    def _page_of(rel: str) -> ParsedPage | None:
        if rel not in page_memo:
            page_memo[rel] = _CACHE.get(vault_root / rel, vault_root)
        return page_memo[rel]

    # ---- Hot cache lookup (freshness-keyed; see _freshness_key above) ----
    # prefer_used bypasses the cache entirely — simplest correct interaction;
    # log freshness never has to enter the cache key.
    resolved_config = config if config is not None else _active_ranking()
    cache_size = 0 if prefer_used else _find_cache_size()
    cache_key: tuple | None = None
    if timings is not None:
        timings.cache["enabled"] = cache_size > 0
    if cache_size > 0:
        def _t(v: list | None) -> tuple | None:
            return tuple(v) if v is not None else None

        request_key = (
            str(vault_root.resolve()), query, _t(types), _t(projects), _t(tags),
            _t(speakers), _t(file_types), _t(exclude_file_types), limit, scope,
            mode, graph, rerank, auto_rerank, temporal, intent,
            updated_after, updated_before, recency_days,
            prefer_compiled, prefer_active, resolved_config,
        )
        with _span(timings, "freshness"):
            fresh = _freshness_key(
                vault_root, scope=scope, query_norm=query_norm, mode=mode,
                snapshot=snapshot,
            )
        cache_key = (request_key, fresh)
        with _span(timings, "cache_lookup"):
            with _FIND_CACHE_LOCK:
                cached = _FIND_CACHE.get(cache_key)
                if cached is not None:
                    _FIND_CACHE.move_to_end(cache_key)
        if cached is not None:
            if timings is not None:
                timings.cache["hit"] = True
            return copy.deepcopy(cached)

    # Track warm-window degradation even when the caller passed no list —
    # internal callers (suggest_links, evolution, note/add sweeps) must never
    # cache a lexical-only ranking that would outlive the warm.
    degraded = degraded_out if degraded_out is not None else []
    # Same rationale for POST-WARM lane failures: a BM25-only result produced
    # because the vector lane threw must not be cached and served after the
    # sidecar/model recovers. Tracked internally even when the caller passes None.
    failed = failed_out if failed_out is not None else []

    # "kb-only" is the strict opt-out (legacy KB-only behavior); "kb" walks the
    # same KB tree but auto-widens to the vault below when it underfills. Both
    # map to a KB-only walk in the underlying rankers.
    walk_scope = "vault" if scope == "vault" else "kb"

    # Empty queries always degrade to keyword behavior — there's no signal
    # to embed or score with, just "give me recent stuff that matches the
    # structured filters."
    if mode == "keyword" or not query_norm:
        with _span(timings, "keyword"):
            hits = _find_keyword(
                vault_root,
                query_norm=query_norm,
                types=types, projects=projects, tags=tags, speakers=speakers,
                file_types=file_types, exclude_file_types=exclude_file_types,
                limit=limit, scope=walk_scope,
            )
    else:
        hits = _find_semantic(
            vault_root,
            query=query, query_norm=query_norm,
            types=types, projects=projects, tags=tags, speakers=speakers,
            file_types=file_types, exclude_file_types=exclude_file_types,
            limit=limit, scope=walk_scope, mode=mode, graph=graph, rerank=rerank,
            auto_rerank=auto_rerank, temporal=temporal, intent=intent,
            prefer_compiled=prefer_compiled,
            prefer_active=prefer_active,
            prefer_used=prefer_used,
            config=resolved_config,
            timings=timings,
            snapshot=snapshot,
            page_memo=page_memo,
            degraded_out=degraded,
            failed_out=failed,
        )

    # Auto-widen: reach into the wider vault (sibling folders like Tracking/,
    # Reference/, plus curated trees) so content outside Knowledge Base/ isn't
    # silently invisible. Only for scope="kb" (not "kb-only"/"vault") and
    # non-empty queries (an empty query has no signal to widen on).
    #
    # We RESERVE a few result slots for out-of-KB hits rather than only
    # back-filling when the KB underfills. The reason is empirical: on a real
    # vault a bare query like "X3" finds 8+ KB files that literally mention the
    # term, which fills `limit` — so a count- or even quality-gated back-fill
    # never fires, and the actual out-of-KB target (e.g. `Tracking/X3 Full
    # Reps.md`, whose title IS the query) stays hidden. Reserving guarantees
    # such a match surfaces. The KB keeps the majority of slots (strong literal
    # hits first, then weak graph/recency filler); the reserve never starves
    # the KB (capped at limit-1) and is empty when nothing outside matches.
    if scope == "kb" and query_norm:
        with _span(timings, "outside_kb"):
            seen = {h.path for h in hits}
            outside = [
                h for h in _find_outside_kb(
                    vault_root,
                    query=query,
                    query_norm=query_norm,
                    types=types, projects=projects, tags=tags, speakers=speakers,
                    file_types=file_types, exclude_file_types=exclude_file_types,
                    limit=limit, snapshot=snapshot,
                )
                if h.path not in seen
            ]
            if outside:
                strong: list[Hit] = []
                weak: list[Hit] = []
                for h in hits:
                    page = _page_of(h.path)
                    # Word/stem-level, not substring: a bare "x3" query must not
                    # treat files that merely contain "x3" inside a longer token
                    # (a hash, "max3...", a log copy) as strong topical matches.
                    if page is not None and _stem_tokens_present(page, query_norm):
                        strong.append(h)
                    else:
                        weak.append(h)
                reserve = min(len(outside), max(1, limit // 5), max(0, limit - 1))
                kb_keep = limit - reserve
                hits = ((strong + weak)[:kb_keep] + outside)[:limit]

    # Explicit recency window (off by default) — drop out-of-window hits last,
    # after auto-widen, so it governs every mode uniformly.
    with _span(timings, "date_filter"):
        hits = _filter_by_date(
            hits,
            updated_after=updated_after,
            updated_before=updated_before,
            recency_days=recency_days,
        )

    # ---- Hot cache store (deep copies both ways; bounded LRU eviction) ----
    # A result produced with warm-deferred lanes is lexical-only — caching it
    # would keep serving the degraded ranking after the warm completes. A
    # post-warm lane FAILURE (`failed`) is skipped for the same reason: the
    # failure may be transient, so don't pin a BM25-only result in the cache.
    if cache_key is not None and not degraded and not failed:
        with _FIND_CACHE_LOCK:
            _FIND_CACHE[cache_key] = copy.deepcopy(hits)
            _FIND_CACHE.move_to_end(cache_key)
            while len(_FIND_CACHE) > cache_size:
                _FIND_CACHE.popitem(last=False)
    return hits


def _transcript_ts_for_hit(page: ParsedPage, chunk: str | None, query_norm: str) -> float | None:
    """Localize a text match inside a TIMED transcript → seconds, or None.

    Vector lane: the matched chunk is a semantic segment whose first timed line
    carries the segment start. BM25/keyword (no chunk, or a chunk without
    markers): anchor on the first query token's position in the body and take
    the nearest PRECEDING timestamp marker. Only timed audio/video sidecars
    ever return a value — flat pages cost one media_type check.
    """
    if page.media_type not in ("audio", "video"):
        return None
    from . import semantic_segments as ss

    if chunk:
        for line in chunk.splitlines():
            m = ss.TIMED_LINE_RE.match(line)
            if m and m.group(2) is not None:
                return ss.ts_from_match(m)
    tokens = query_norm.split() if query_norm else []
    if not tokens:
        return None
    body = page.body or ""
    if "[" not in body:
        return None
    pos = body.lower().find(tokens[0])
    if pos == -1:
        return None
    offset = 0
    best: float | None = None
    for line in body.splitlines(keepends=True):
        if offset > pos:
            break
        m = ss.TIMED_LINE_RE.match(line)
        if m and m.group(2) is not None:
            best = ss.ts_from_match(m)
        offset += len(line)
    return best


def _collapse_frame_children(
    ranking: list[str],
    vault_root: Path,
    attribution: dict[str, tuple[str, float | None]],
    *aux_maps: dict,
) -> list[str]:
    """Remap scene-frame sidecar candidates onto their parent video's sidecar
    (pre-fusion) so one video = one candidate.

    A persisted scene frame (frontmatter `parent_media`, written by scene_frames)
    must never compete with its parent in ranking — RRF would count the same
    content twice and a multi-scene video could flood a lane with near-duplicate
    frames. Each frame child is replaced by `<parent_media>.md` at the frame's
    lane position (keep-first = keep-best in a rank-ordered lane); the best-ranked
    frame per parent is recorded in `attribution[parent] = (jpg_rel, frame_ts)`
    for hit enrichment. Keys in `aux_maps` (scores/chunk texts) move to the
    parent keep-best. An orphan frame (parent sidecar gone) passes through and
    surfaces standalone. Non-frame candidates cost one cached frontmatter lookup.
    """
    if not ranking:
        return ranking
    out: list[str] = []
    seen: set[str] = set()
    for rel in ranking:
        page = _CACHE.get(vault_root / rel, vault_root)
        parent = page.parent_media if page is not None else None
        if parent:
            parent_sidecar = parent + ".md"
            if (vault_root / parent_sidecar).exists():
                attribution.setdefault(parent_sidecar, (page.media_file or rel, page.frame_ts))
                for m in aux_maps:
                    if rel in m:
                        v = m.pop(rel)
                        m.setdefault(parent_sidecar, v)
                rel = parent_sidecar
        if rel in seen:
            continue
        seen.add(rel)
        out.append(rel)
    return out


def _find_keyword(
    vault_root: Path,
    *,
    query_norm: str,
    types: list[str] | None,
    projects: list[str] | None,
    tags: list[str] | None,
    speakers: list[str] | None = None,
    file_types: list[str] | None = None,
    exclude_file_types: list[str] | None = None,
    limit: int,
    scope: str,
) -> list[Hit]:
    """Original keyword-mode find. Preserved for backward compat."""
    if scope == "kb":
        kb = vault_root / "Knowledge Base"
        if not kb.is_dir():
            log.error("KB directory missing: %s", kb)
            return []
        walk = _walk_md(kb)
    else:
        from .vault import walk_vault_md
        walk = walk_vault_md(vault_root)

    hits: list[tuple[str, Hit]] = []
    by_path: dict[str, Hit] = {}
    for path in walk:
        if path.name.lower() in _NAVIGATION_BASENAMES:
            continue
        page = _CACHE.get(path, vault_root)
        if page is None:
            continue
        excerpt = _make_excerpt(page, query_norm)
        if query_norm and excerpt is None:
            continue
        # A scene-frame child groups under its parent video: the parent becomes
        # the hit (carrying the matched frame + timestamp); an orphan frame
        # (parent gone) surfaces standalone. Filters apply to the EMITTED page.
        scene_frame: str | None = None
        scene_frame_ts: float | None = None
        if page.parent_media:
            parent_page = _CACHE.get(vault_root / (page.parent_media + ".md"), vault_root)
            if parent_page is not None:
                existing = by_path.get(parent_page.rel_path)
                if existing is not None:
                    if existing.scene_frame is None:
                        existing.scene_frame = page.media_file
                        existing.scene_frame_ts = page.frame_ts
                    continue
                scene_frame, scene_frame_ts = page.media_file, page.frame_ts
                page = parent_page
        if page.rel_path in by_path:
            continue
        if not _passes_filters(page, vault_root=vault_root, types=types, projects=projects, tags=tags, speakers=speakers,
                               file_types=file_types, exclude_file_types=exclude_file_types):
            continue
        hit = Hit(
            path=page.rel_path,
            type=page.page_type,
            scope=page.scope,
            title=page.title,
            updated=page.updated,
            excerpt=excerpt or "",
            media_type=page.media_type,
            media_file=page.media_file,
            status=page.status,
            superseded_by=page.superseded_by,
            scene_frame=scene_frame,
            scene_frame_ts=scene_frame_ts,
        )
        hit.transcript_ts = _transcript_ts_for_hit(page, None, query_norm)
        if hit.scene_frame is None and page.media_type == "video" and page.media_file and hit.transcript_ts is not None:
            from . import scene_frames  # lazy: keyword mode stays import-light
            nf = scene_frames.nearest_frame(vault_root, page.media_file, hit.transcript_ts)
            if nf is not None:
                hit.scene_frame, hit.scene_frame_ts = nf
        by_path[page.rel_path] = hit
        hits.append((page.updated or "0000-00-00", hit))

    hits.sort(key=lambda t: (t[0], t[1].path), reverse=True)
    return [h for _, h in hits[:limit]]


def _find_semantic(
    vault_root: Path,
    *,
    query: str,
    query_norm: str,
    types: list[str] | None,
    projects: list[str] | None,
    tags: list[str] | None,
    speakers: list[str] | None = None,
    file_types: list[str] | None = None,
    exclude_file_types: list[str] | None = None,
    limit: int,
    scope: str,
    mode: str,
    graph: bool = True,
    rerank: bool | None = False,
    auto_rerank: bool = False,
    temporal: bool = True,
    intent: str | None = None,
    prefer_compiled: bool = True,
    prefer_active: bool = True,
    prefer_used: bool = False,
    config: RankingConfig = DEFAULT_RANKING,
    timings: FindTimings | None = None,
    snapshot: FreshnessSnapshot | None = None,
    page_memo: dict[str, ParsedPage | None] | None = None,
    degraded_out: list[str] | None = None,
    failed_out: list[str] | None = None,
) -> list[Hit]:
    """Hybrid (BM25+vector) or vector-only mode.

    `failed_out` (distinct from `degraded_out`): a POST-WARM lane FAILURE — the
    vector or CLIP `except`, or the all-lanes-empty keyword fallback — appends
    the failed lane name here and bumps the process degradation counter. This is
    the "the lane broke and we silently served a weaker ranking" signal, versus
    `degraded_out`'s "the lane was deferred while a model preload is warming".
    """
    # Lazy imports — keep keyword-mode users out of the torch import path.
    from . import bm25, embeddings, fusion, readiness, scene_frames

    if snapshot is None:
        snapshot = FreshnessSnapshot(vault_root)
    if page_memo is None:
        page_memo = {}

    # Usage-activation map for the opt-in boost. Empty dict = strict no-op
    # (default, cold start, gated, or kill-switched).
    usage_map: dict[str, float] = {}
    if prefer_used:
        from . import usage as usage_module
        usage_map = usage_module.activation_map(config)

    def _page_of(rel: str) -> ParsedPage | None:
        if rel not in page_memo:
            page_memo[rel] = _CACHE.get(vault_root / rel, vault_root)
        return page_memo[rel]

    # Pull more than we need so post-filter losses don't starve the result.
    candidate_k = max(limit * config.candidate_multiplier, config.candidate_floor)

    # Scene-frame children collapse onto their parent video per lane (pre-fusion);
    # this records parent sidecar → (best matching frame jpg, frame_ts) for hits.
    frame_attribution: dict[str, tuple[str, float | None]] = {}

    # ---- Vector contribution ----
    vector_ranking: list[str] = []
    chunk_text_by_path: dict[str, str] = {}
    vector_score_by_path: dict[str, float] = {}
    if readiness.should_defer("embeddings"):
        # Background warm-up owns the model lock right now — calling
        # embed_texts here would BLOCK on it (minutes on a first-ever
        # download), and the except-fallback below would never fire. Skip
        # the lane; the caller marks the response as warming.
        if timings is not None:
            timings.skipped("vector")
        if degraded_out is not None:
            degraded_out.append("embeddings")
    else:
        try:
            with _span(timings, "vector"):
                idx = embeddings.get_embedding_index(vault_root)
                query_vec = embeddings.embed_texts([query], is_query=True)[0]
                chunk_hits = idx.search(query_vec, k=candidate_k * 3)  # over-fetch chunks
                # Collapse chunks → file-level: keep the best-scoring chunk per file.
                best_per_file: dict[str, tuple[float, str]] = {}
                for fp, _idx, ctext, score in chunk_hits:
                    existing = best_per_file.get(fp)
                    if existing is None or score > existing[0]:
                        best_per_file[fp] = (score, ctext)
                vector_ranking = sorted(
                    best_per_file.keys(), key=lambda p: -best_per_file[p][0]
                )[:candidate_k]
                chunk_text_by_path = {p: best_per_file[p][1] for p in vector_ranking}
                vector_score_by_path = {p: best_per_file[p][0] for p in vector_ranking}
        except ImportError as e:
            log.warning(
                "vector search unavailable (%s); falling back to BM25-only ranking",
                e,
            )
            _record_degradation("vector")
            if failed_out is not None:
                failed_out.append("vector")
            if timings is not None:
                timings.error("vector", e)
        except Exception as e:
            log.warning("vector search failed: %s; falling back to BM25-only", e)
            _record_degradation("vector")
            if failed_out is not None:
                failed_out.append("vector")
            if timings is not None:
                timings.error("vector", e)
    vector_ranking = _collapse_frame_children(
        vector_ranking, vault_root, frame_attribution, chunk_text_by_path, vector_score_by_path
    )

    # ---- CLIP contribution: text→image visual search ----
    # Lets a text query match a (possibly textless) Evidence photo by visual content.
    # Returns the image's *sidecar* path (what the corpus indexes); soft-fails when CLIP
    # isn't installed or the index is empty. Gated by EXOMEM_DISABLE_CLIP.
    clip_ranking: list[str] = []
    clip_score_by_path: dict[str, float] = {}
    clip_frame_ts_by_path: dict[str, float | None] = {}
    if embeddings.clip_enabled() and query.strip() and readiness.should_defer("clip"):
        # Same lock-blocking hazard as the vector lane: never touch the CLIP
        # getter while the background warm is loading it.
        if timings is not None:
            timings.skipped("clip")
        if degraded_out is not None:
            degraded_out.append("clip")
    elif embeddings.clip_enabled() and query.strip():
        try:
            with _span(timings, "clip"):
                clip_idx = embeddings.get_clip_index(vault_root)
                clip_qvec = embeddings.embed_clip_text(query)
                # A video contributes N keyframe rows; over-fetch so distinct videos
                # aren't crowded out, then dedup to best-per-file (rows are score-desc,
                # so the FIRST time a sidecar appears is its best frame). Stop at candidate_k
                # distinct sidecars; record that best frame's timestamp (None for images).
                for img_rel, frame_ts, score in clip_idx.search(clip_qvec, k=candidate_k * 8):
                    if len(clip_ranking) >= candidate_k:
                        break
                    sidecar_rel = img_rel + ".md"
                    if sidecar_rel not in clip_score_by_path and (vault_root / sidecar_rel).exists():
                        clip_ranking.append(sidecar_rel)
                        clip_score_by_path[sidecar_rel] = score
                        clip_frame_ts_by_path[sidecar_rel] = frame_ts
        except embeddings.ClipUnavailable as e:
            log.warning("CLIP search unavailable (%s); skipping image search", e)
            _record_degradation("clip")
            if failed_out is not None:
                failed_out.append("clip")
            if timings is not None:
                timings.error("clip", e)
        except Exception as e:  # noqa: BLE001 — image search is best-effort
            log.warning("CLIP search failed: %s; skipping image search", e)
            _record_degradation("clip")
            if failed_out is not None:
                failed_out.append("clip")
            if timings is not None:
                timings.error("clip", e)
    elif timings is not None:
        timings.skipped("clip")
    # Defensive: frame children get no ClipIndex rows, but a stray row (e.g. from a
    # pre-exclusion index) must still group under the parent.
    clip_ranking = _collapse_frame_children(
        clip_ranking, vault_root, frame_attribution, clip_score_by_path, clip_frame_ts_by_path
    )

    bm25_ranking: list[str] = []
    keyword_ranking: list[str] = []
    if mode == "vector":
        if timings is not None:
            timings.skipped("bm25")
            timings.skipped("keyword")
        rankings = [r for r in (vector_ranking, clip_ranking) if r]
    else:
        # ---- BM25 contribution ----
        try:
            with _span(timings, "bm25"):
                bm25_hits = bm25.search(
                    vault_root, query, k=candidate_k, scope=scope,
                    freshness=snapshot.for_scope(scope),
                )
                bm25_ranking = [p for p, _ in bm25_hits]
        except ImportError as e:
            log.warning("BM25 unavailable (%s); using vector-only", e)
            if timings is not None:
                timings.error("bm25", e)
        except Exception as e:
            log.warning("BM25 search failed: %s; using vector-only", e)
            if timings is not None:
                timings.error("bm25", e)
        bm25_ranking = _collapse_frame_children(bm25_ranking, vault_root, frame_attribution)

        # ---- Keyword contribution: literal all-tokens-present matches ----
        # Walking the KB for substring matches makes hybrid a strict superset
        # of keyword — any page keyword would surface lands in the candidate
        # pool regardless of where BM25/vector rank it. Closes the recall
        # hole where BM25 buries a target under thematically-noisy hits with
        # high TF on a shared common token (e.g. "Borough Market" buried
        # under Q marketing pages on the "market" stem).
        with _span(timings, "keyword"):
            keyword_ranking = _keyword_match_paths(vault_root, query_norm, scope)
        keyword_ranking = _collapse_frame_children(
            keyword_ranking, vault_root, frame_attribution
        )
        rankings = [
            r for r in (vector_ranking, bm25_ranking, keyword_ranking, clip_ranking) if r
        ]

    # ---- Graph expansion: 1-hop outbound wikilinks of STRONG candidates ----
    # Strong = ranked by vector (semantically gated by construction), or
    # BM25-ranked AND passing the stem-aware all-tokens-present check. Seeding
    # from raw BM25 alone leaks neighbours of weak matches into results
    # (e.g. queries like "skip-marker-abc" where every token is common).
    graph_ranking: list[str] = []
    graph_in_degree_by_path: dict[str, int] = {}
    if not graph and timings is not None:
        timings.skipped("graph")
    _graph_t0 = time.perf_counter()
    if graph:
        primary_set: set[str] = set(vector_ranking) | set(bm25_ranking)
        vector_set: set[str] = set(vector_ranking)
        graph_seeds: list[str] = []
        seen_seed: set[str] = set()
        for r in (vector_ranking, bm25_ranking):
            for p in r[:config.graph_seed_cap]:  # cap fanout
                if p in seen_seed:
                    continue
                seen_seed.add(p)
                if p in vector_set:
                    graph_seeds.append(p)
                    continue
                # BM25-only seed: gate via stem-aware tokens-present.
                page = _page_of(p)
                if page is None:
                    continue
                if (
                    _make_excerpt(page, query_norm) is not None
                    or _stem_tokens_present(page, query_norm)
                ):
                    graph_seeds.append(p)
        # One resolver for the whole request, freshness-checked against the
        # request's own vault snapshot (no extra walk).
        resolver = (
            _get_query_resolver(vault_root, freshness=snapshot.vault())
            if graph_seeds else None
        )
        seen_target: set[str] = set()
        for seed_rel in graph_seeds:
            page = _page_of(seed_rel)
            if page is None:
                continue
            for target_rel in _outbound_wikilink_paths(
                page, vault_root, resolver=resolver
            ):
                # Count in-degree for ALL targets — primary-ranked hubs benefit
                # too. graph_ranking still only carries non-primary targets so
                # RRF doesn't double-count them.
                graph_in_degree_by_path[target_rel] = (
                    graph_in_degree_by_path.get(target_rel, 0) + 1
                )
                if target_rel in primary_set or target_rel in seen_target:
                    continue
                seen_target.add(target_rel)
                graph_ranking.append(target_rel)
        if graph_ranking:
            rankings.append(graph_ranking)
        if timings is not None:
            timings.stages.setdefault("graph", {})["ms"] = round(
                (time.perf_counter() - _graph_t0) * 1000.0, 3
            )

    # Pre-compute per-mode rank lookups so we can tag each Hit's signals.
    vector_rank_by_path = {p: i + 1 for i, p in enumerate(vector_ranking)}
    bm25_rank_by_path = {p: i + 1 for i, p in enumerate(bm25_ranking)}
    keyword_rank_by_path = {p: i + 1 for i, p in enumerate(keyword_ranking)}
    clip_rank_by_path = {p: i + 1 for i, p in enumerate(clip_ranking)}
    keyword_set: set[str] = set(keyword_ranking)
    clip_set: set[str] = set(clip_ranking)
    graph_set = set(graph_ranking)

    if not rankings:
        # Both rankers failed or produced nothing. Degrade to keyword.
        log.info("semantic search produced no candidates; falling back to keyword")
        _record_degradation("no_candidates")
        if failed_out is not None:
            failed_out.append("keyword")
        return _find_keyword(
            vault_root,
            query_norm=query_norm,
            types=types, projects=projects, tags=tags, speakers=speakers,
            file_types=file_types, exclude_file_types=exclude_file_types,
            limit=limit, scope=scope,
        )

    # ---- Temporal lane: recency-ordered candidates (temporal queries only) ----
    # Built ONLY when the query is temporal, so it's empty (and thus a no-op in
    # fusion) for every other query — keeping the common path byte-identical.
    temporal_ranking: list[str] = []
    if temporal and _is_temporal_query(query):
        with _span(timings, "temporal"):
            pool: list[str] = []
            for lane in (vector_ranking, bm25_ranking, keyword_ranking, clip_ranking):
                pool.extend(lane)
            temporal_ranking = _recency_ranking(pool, vault_root, candidate_k)
    elif timings is not None:
        timings.skipped("temporal")

    # ---- Intent-adaptive weighted RRF ----
    # Classify the query, pick the per-intent lane weights, fuse. The
    # "conceptual" default is all-1.0, so the common case reproduces the
    # unweighted RRF exactly; only non-conceptual intents reweight the lanes.
    with _span(timings, "fusion"):
        intent_label = intent or _classify_intent(query)
        weights = config.intent_weights(intent_label)
        lane_rankings = [
            vector_ranking, bm25_ranking, keyword_ranking,
            clip_ranking, graph_ranking, temporal_ranking,
        ]
        active_lists: list[list[str]] = []
        active_weights: list[float] = []
        for lane, w in zip(lane_rankings, weights, strict=True):
            if lane:
                active_lists.append(lane)
                active_weights.append(w)
        fused = fusion.reciprocal_rank_fusion_weighted(
            active_lists, active_weights, k=config.rrf_k
        )
        # Post-RRF multiplicative boosts (type / status / Gaussian recency) in
        # ONE pass with ONE sort. Order-equivalent to the historical
        # sequential _apply_type_boost → _apply_status_demotion →
        # _apply_temporal_boost passes: each of those ended with the same
        # total-order sort key (-score, path), so only the final combined
        # multiplier ever determined the order (guarded by the equivalence
        # tests). For rerank, type/status/usage are also applied to
        # rerank_score below so they survive the final sort.
        fused = _apply_post_rrf_multipliers(
            fused, query, config,
            prefer_compiled=prefer_compiled,
            prefer_active=prefer_active,
            temporal=temporal,
            page_of=_page_of,
            usage_map=usage_map,
        )
    vector_paths: set[str] = set(vector_ranking)

    # Resolve fused paths back to ParsedPage, filter, build hits in fused order.
    # BM25-only candidates must still satisfy the keyword all-tokens-present
    # gate — without it, BM25's word-level tokenizer surfaces files that share
    # any single token with the query (false positives). Vector-ranked
    # candidates skip that gate by design: surfacing semantically-similar
    # files that don't contain the literal tokens is the whole point.
    # When reranking, we over-fetch then trim post-rerank. `rerank` may be
    # unset (None) with `auto_rerank` on — in that case we don't yet know
    # whether we'll rerank (should_rerank inspects the built hits), so over-fetch
    # whenever reranking is even possible.
    may_rerank = rerank is True or (rerank is None and auto_rerank)
    target_n = limit * 3 if may_rerank else limit
    hits: list[Hit] = []
    seen: set[str] = set()
    _filter_t0 = time.perf_counter()
    for rel_path, _score in fused:
        if rel_path in seen:
            continue
        seen.add(rel_path)
        if rel_path.rsplit("/", 1)[-1].lower() in _NAVIGATION_BASENAMES:
            continue
        page = _page_of(rel_path)
        if page is None:
            continue
        if not _passes_filters(page, vault_root=vault_root, types=types, projects=projects, tags=tags, speakers=speakers,
                               file_types=file_types, exclude_file_types=exclude_file_types):
            continue
        keyword_excerpt = _make_excerpt(page, query_norm)
        if (
            rel_path not in vector_paths
            and rel_path not in graph_set
            and rel_path not in keyword_set
            and rel_path not in clip_set
            and rel_path not in frame_attribution
            and keyword_excerpt is None
        ):
            # No literal match, not a graph hop, not vector-ranked, not in
            # the keyword scan. Try stem match before dropping — recovers
            # morphology ("regulation" matching a "regulator" page).
            if not _stem_tokens_present(page, query_norm):
                continue
            keyword_excerpt = _stem_anchored_excerpt(page, query_norm)
        elif (
            rel_path in graph_set
            or rel_path in clip_set
            or rel_path in frame_attribution
        ) and keyword_excerpt is None:
            # Graph-hop neighbour, CLIP visual match, or frame-collapsed parent:
            # no all-tokens-present requirement (the reason for surfacing is
            # connectivity / visual similarity / a child frame's text, not this
            # page's lexical overlap). Prefer the matched frame's OCR text as the
            # "why", else the sidecar's leading body.
            attr = frame_attribution.get(rel_path)
            if attr is not None:
                fpage = _CACHE.get(vault_root / (attr[0] + ".md"), vault_root)
                if fpage is not None:
                    keyword_excerpt = _make_excerpt(fpage, query_norm)
            if keyword_excerpt is None:
                body = page.body.strip()
                keyword_excerpt = _collapse(body[:EXCERPT_MAX_LEN]) if body else ""
        chunk = chunk_text_by_path.get(rel_path)
        excerpt = _semantic_excerpt(page, query_norm, chunk, keyword_excerpt)
        is_graph_only = (
            rel_path in graph_set
            and rel_path not in vector_rank_by_path
            and rel_path not in bm25_rank_by_path
        )
        hit_activation: float | None = None
        hit_usage_mult: float | None = None
        if usage_map:
            from . import usage as usage_module
            hit_activation = usage_map.get(usage_module.canon(rel_path))
            if hit_activation is not None:
                hit_usage_mult = usage_module.usage_multiplier(
                    hit_activation, config
                )
        attr = frame_attribution.get(rel_path)
        hit = Hit(
            path=page.rel_path,
            type=page.page_type,
            scope=page.scope,
            title=page.title,
            updated=page.updated,
            excerpt=excerpt or "",
            media_type=page.media_type,
            media_file=page.media_file,
            status=page.status,
            superseded_by=page.superseded_by,
            bm25_rank=bm25_rank_by_path.get(rel_path),
            vector_rank=vector_rank_by_path.get(rel_path),
            vector_score=vector_score_by_path.get(rel_path),
            clip_rank=clip_rank_by_path.get(rel_path),
            clip_score=clip_score_by_path.get(rel_path),
            clip_frame_ts=clip_frame_ts_by_path.get(rel_path),
            graph_hop=is_graph_only,
            graph_in_degree=graph_in_degree_by_path.get(rel_path, 0),
            keyword_rank=keyword_rank_by_path.get(rel_path),
            activation=hit_activation,
            usage_boost_applied=hit_usage_mult,
            scene_frame=attr[0] if attr else None,
            scene_frame_ts=attr[1] if attr else None,
        )
        hit.transcript_ts = _transcript_ts_for_hit(page, chunk, query_norm)
        if hit.scene_frame is None and page.media_type == "video" and page.media_file:
            # A localized match on a video — CLIP keyframe first (existing), else a
            # timed-transcript match — attaches the nearest PERSISTED frame so the
            # moment is viewable, not just timestamped.
            anchor_ts = hit.clip_frame_ts if hit.clip_frame_ts is not None else hit.transcript_ts
            if anchor_ts is not None:
                nf = scene_frames.nearest_frame(vault_root, page.media_file, anchor_ts)
                if nf is not None:
                    hit.scene_frame, hit.scene_frame_ts = nf
        hits.append(hit)
        if len(hits) >= target_n:
            break
    if timings is not None:
        timings.stages.setdefault("filter_hits", {})["ms"] = round(
            (time.perf_counter() - _filter_t0) * 1000.0, 3
        )

    # Resolve the rerank decision. An explicit rerank=True/False always wins;
    # otherwise (rerank is None) auto_rerank consults should_rerank on the built
    # hits. Keeps the reranker model out of the default/test path.
    if rerank is None:
        do_rerank = auto_rerank and should_rerank(hits, query, config)
    else:
        do_rerank = rerank

    if do_rerank and hits and readiness.should_defer("reranker"):
        # Background warm-up owns the reranker load right now — calling
        # rerank_pairs would block on the singleton lock. Skip; caller marks
        # the response as warming.
        if degraded_out is not None:
            degraded_out.append("reranker")
        do_rerank = False

    if timings is not None and not (do_rerank and hits):
        timings.skipped("rerank")
    if do_rerank and hits:
        _rerank_t0 = time.perf_counter()
        try:
            from . import embeddings as emb
            # Best passage for each hit: the matched chunk when we have one,
            # else the leading body slice.
            passages: list[str] = []
            for h in hits:
                ctext = chunk_text_by_path.get(h.path)
                if ctext:
                    passages.append(ctext)
                else:
                    pg = _page_of(h.path)
                    body = (pg.body if pg else "") or h.excerpt
                    passages.append(body[:1500])  # CrossEncoder caps at 512 tokens
            scores = emb.rerank_pairs(query, passages)
            for h, s in zip(hits, scores):
                h.rerank_score = float(s)
            # Re-apply the type boost to rerank scores so prefer_compiled
            # survives the post-rerank sort. This rescues compiled material
            # that bge-reranker-base demotes — e.g. a "thoughts on..." query
            # where the reranker preferred raw Source discussion over
            # compiled Insights.
            if prefer_compiled:
                for h in hits:
                    if h.rerank_score is not None:
                        h.rerank_score *= _type_multiplier(h.type, config)
            # Re-apply the supersession demotion to rerank scores too, so a
            # superseded tombstone the reranker liked can't float back above
            # its successor in the final sort.
            if prefer_active:
                for h in hits:
                    if h.rerank_score is not None:
                        h.rerank_score *= _status_multiplier(h.status, config)
            # Re-apply the usage boost too, mirroring type/status, so an
            # opted-in boost survives the post-rerank sort.
            if usage_map:
                for h in hits:
                    if h.rerank_score is not None and h.usage_boost_applied:
                        h.rerank_score *= h.usage_boost_applied
            hits.sort(key=lambda h: -(h.rerank_score if h.rerank_score is not None else float("-inf")))
        except ImportError as e:
            log.warning("rerank requested but reranker unavailable: %s", e)
            if timings is not None:
                timings.error("rerank", e)
        except Exception as e:
            log.warning("rerank failed: %s; returning fused order", e)
            if timings is not None:
                timings.error("rerank", e)
        finally:
            if timings is not None:
                timings.stages.setdefault("rerank", {})["ms"] = round(
                    (time.perf_counter() - _rerank_t0) * 1000.0, 3
                )

    return hits[:limit]


def _find_outside_kb(
    vault_root: Path,
    *,
    query: str,
    query_norm: str,
    types: list[str] | None,
    projects: list[str] | None,
    tags: list[str] | None,
    speakers: list[str] | None = None,
    file_types: list[str] | None = None,
    exclude_file_types: list[str] | None = None,
    limit: int,
    snapshot: FreshnessSnapshot | None = None,
) -> list[Hit]:
    """BM25/keyword recall over the vault, RESTRICTED to paths outside
    `Knowledge Base/`. Powers scope="kb" auto-widening.

    Recall is BM25-only by design (the vector sidecar is KB-scoped), with a
    RELAXED gate: a candidate survives when at least one query stem is present,
    not the strict all-tokens-present gate the KB path enforces. Terse,
    frontmatter-less files (e.g. a numbers-heavy workout tracker) would
    otherwise be filtered out by any natural-language query that includes a
    word they don't literally contain.
    """
    if not query_norm or limit < 1:
        return []
    from . import bm25

    # Over-fetch: KB files dominate the corpus, so pull a generous slice then
    # filter to out-of-KB paths. Auto-widen only fires when the KB underfilled
    # — i.e. the query was already rare in the KB — so the out-of-KB target
    # won't be buried under hundreds of KB matches.
    bm25_k = max(limit * 5, 100)
    candidates: list[str] = []
    try:
        for path, _score in bm25.search(
            vault_root, query, k=bm25_k, scope="vault",
            freshness=snapshot.vault() if snapshot is not None else None,
        ):
            if not path.startswith("Knowledge Base/"):
                candidates.append(path)
    except ImportError:
        candidates = _outside_kb_keyword_paths(vault_root, query_norm)
    except Exception as e:  # noqa: BLE001 — widening must never break find
        log.warning("auto-widen BM25 failed: %s; falling back to keyword", e)
        candidates = _outside_kb_keyword_paths(vault_root, query_norm)

    hits: list[Hit] = []
    seen: set[str] = set()
    for rel_path in candidates:
        if rel_path in seen:
            continue
        seen.add(rel_path)
        if rel_path.rsplit("/", 1)[-1].lower() in _NAVIGATION_BASENAMES:
            continue
        page = _CACHE.get(vault_root / rel_path, vault_root)
        if page is None:
            continue
        if not _passes_filters(page, vault_root=vault_root, types=types, projects=projects, tags=tags, speakers=speakers,
                               file_types=file_types, exclude_file_types=exclude_file_types):
            continue
        # Relaxed gate: BM25 score>0 already implies a token match, but the
        # keyword fallback path needs this explicit check.
        if not _any_stem_present(page, query_norm):
            continue
        excerpt = _stem_anchored_excerpt(page, query_norm)
        hits.append(Hit(
            path=page.rel_path,
            type=page.page_type,
            scope=page.scope,
            title=page.title,
            updated=page.updated,
            excerpt=excerpt or "",
            media_type=page.media_type,
            media_file=page.media_file,
            status=page.status,
            superseded_by=page.superseded_by,
            outside_kb=True,
        ))
        if len(hits) >= limit:
            break
    return hits


def _any_stem_present(page: ParsedPage, query_norm: str) -> bool:
    """True if at least ONE query stem appears in title+body.

    The relaxed counterpart to `_stem_tokens_present` (which requires ALL).
    Tokenizes the query the SAME way BM25 tokenizes text (split on `[a-z0-9]+`,
    then stem) so a hyphenated query like `cognitive-core-marker-xyz` matches a
    body that contains those words split on the hyphens.
    """
    if not query_norm:
        return False
    from . import bm25 as bm25_module
    return any(qs in page.stem_set for qs in bm25_module.tokenize(query_norm))


def _outside_kb_keyword_paths(vault_root: Path, query_norm: str) -> list[str]:
    """BM25-unavailable fallback: walk vault .md outside Knowledge Base/, keep
    files where >=1 query stem is present, ordered most-recent first."""
    from .vault import walk_vault_md
    vault_resolved = vault_root.resolve()
    matches: list[tuple[str, str]] = []
    for path in walk_vault_md(vault_root):
        try:
            rel = path.resolve().relative_to(vault_resolved).as_posix()
        except ValueError:
            continue
        if rel.startswith("Knowledge Base/"):
            continue
        page = _CACHE.get(path, vault_root)
        if page is None:
            continue
        if _any_stem_present(page, query_norm):
            matches.append((page.updated or "0000-00-00", rel))
    matches.sort(reverse=True)
    return [p for _, p in matches]


# KB epistemic hierarchy: compiled distillations are the intentional output,
# raw sources are inputs. Surfaced via prefer_compiled=True post-RRF boost.
# Multipliers are small — designed as tie-breakers between similar fused
# scores, not as dominators. Tune in one place if needed.
_COMPILED_TYPES = frozenset(
    {
        "insight", "pattern", "failure", "research-note", "entity",
        # Production-logs and experiments are also Notes/-tier compiled
        # outputs (creative-artifact knowledge / hypothesis-tested results
        # respectively), not raw inputs. Boost them alongside their peers.
        "production-log", "experiment",
    }
)
_SOURCE_TYPES = frozenset({"source"})
_COMPILED_BOOST = 1.15
_SOURCE_PENALTY = 0.85
# Supersession demotion: a `status: superseded` page stays in place per the
# supersession protocol (it is NOT moved), so without this it competes head-to-head
# with — and can outrank — the very page that replaced it. Soft-demote, never
# exclude: the tombstone must stay findable for "what did I used to think".
_SUPERSEDED_PENALTY = 0.5


def _type_multiplier(
    page_type: str | None, config: RankingConfig = DEFAULT_RANKING
) -> float:
    if page_type in _COMPILED_TYPES:
        return config.compiled_boost
    if page_type in _SOURCE_TYPES:
        return config.source_penalty
    return 1.0


def _status_multiplier(
    status: str | None, config: RankingConfig = DEFAULT_RANKING
) -> float:
    """Demote `superseded` tombstones; everything else is neutral.

    `archived` pages live in `_archive/` (already dir-excluded), so only
    `superseded` needs handling here. `active`/`draft`/unset → 1.0.
    """
    if status == "superseded":
        return config.superseded_penalty
    return 1.0


def _apply_type_boost(
    fused: list[tuple[str, float]],
    vault_root: Path,
    config: RankingConfig = DEFAULT_RANKING,
) -> list[tuple[str, float]]:
    """Re-sort fused `(path, score)` pairs after applying per-type multipliers.

    Paths whose ParsedPage can't be loaded keep their original score (no
    multiplier known). Stable sort by adjusted score desc, path asc.
    """
    adjusted: list[tuple[str, float]] = []
    for path, score in fused:
        page = _CACHE.get(vault_root / path, vault_root)
        if page is not None and page.media_type:
            # A media sidecar is `type: source`, but the binary it points at IS the
            # answer — exempt it from the source penalty so it ranks on its content.
            mult = 1.0
        else:
            mult = _type_multiplier(page.page_type if page else None, config)
        adjusted.append((path, score * mult))
    adjusted.sort(key=lambda t: (-t[1], t[0]))
    return adjusted


def _apply_status_demotion(
    fused: list[tuple[str, float]],
    vault_root: Path,
    config: RankingConfig = DEFAULT_RANKING,
) -> list[tuple[str, float]]:
    """Re-sort fused `(path, score)` pairs after demoting superseded pages.

    Mirrors `_apply_type_boost` but gated by `prefer_active` independently of
    `prefer_compiled`. Pages that can't be loaded keep their original score.
    """
    adjusted: list[tuple[str, float]] = []
    for path, score in fused:
        page = _CACHE.get(vault_root / path, vault_root)
        mult = _status_multiplier(page.status if page else None, config)
        adjusted.append((path, score * mult))
    adjusted.sort(key=lambda t: (-t[1], t[0]))
    return adjusted


def _apply_post_rrf_multipliers(
    fused: list[tuple[str, float]],
    query: str,
    config: RankingConfig,
    *,
    prefer_compiled: bool,
    prefer_active: bool,
    temporal: bool,
    page_of,
    usage_map: dict[str, float] | None = None,
) -> list[tuple[str, float]]:
    """All post-RRF multiplicative boosts in one pass with one final sort.

    Combines the type boost (`prefer_compiled`, with the media-sidecar
    source-penalty exemption), the supersession demotion (`prefer_active`),
    and the Gaussian recency multiplier (temporal query AND
    `config.temporal_boost != 1.0`). Order-equivalent to running the three
    single-purpose `_apply_*` passes sequentially — each ended with the same
    total-order sort key `(-score, path)`, so intermediate orders never
    mattered; those functions remain as the reference implementations the
    equivalence tests compare against. When no stage is active the input is
    returned unchanged (matching the historical all-off path, which never
    re-sorted). `page_of` is the per-request ParsedPage memo, so each
    candidate is stat'd/parsed at most once per request.
    """
    temporal_active = (
        temporal and config.temporal_boost != 1.0 and _is_temporal_query(query)
    )
    usage_active = bool(usage_map)
    if not (prefer_compiled or prefer_active or temporal_active or usage_active):
        return fused
    if usage_active:
        from . import usage as usage_module
    today = date.today() if temporal_active else None
    adjusted: list[tuple[str, float]] = []
    for path, score in fused:
        page = page_of(path)
        # Multiply the score progressively (never pre-combine multipliers) so
        # the float ops are bit-identical to the sequential reference passes.
        if prefer_compiled:
            if page is not None and page.media_type:
                # A media sidecar is `type: source`, but the binary it points
                # at IS the answer — exempt from the source penalty.
                pass
            else:
                score = score * _type_multiplier(
                    page.page_type if page else None, config
                )
        if prefer_active:
            score = score * _status_multiplier(
                page.status if page else None, config
            )
        if temporal_active:
            d = _parse_date(page.updated) if page else None
            if d is not None:
                score = score * _recency_multiplier(
                    max(0.0, float((today - d).days)), config
                )
        if usage_active:
            b = usage_map.get(usage_module.canon(path))
            if b is not None:
                score = score * usage_module.usage_multiplier(b, config)
        adjusted.append((path, score))
    adjusted.sort(key=lambda t: (-t[1], t[0]))
    return adjusted


# ---- Intent classification + temporal lane (deterministic, no LLM) ----
# Pure pattern-matching, per the pure-substrate constraint: NO model decides
# intent — only literal markers do. Word-boundaried so a marker can't fire from
# a substring of an unrelated token (e.g. "reference-marker-xyz" must NOT read
# as a relationship query).
_TEMPORAL_MARKERS = re.compile(
    r"\b(recent|recently|latest|newest|today|yesterday|tonight|"
    r"week|weeks|month|months|year|years|"
    r"when|before|after|since|until|ago|"
    r"20\d\d|\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE,
)
_RELATIONSHIP_MARKERS = re.compile(
    r"\b(links?|linked|relate[sd]?|related|relationship|"
    r"connect(?:s|ed|ion|ions)?|cite[sd]?|citations?|"
    r"mention(?:s|ed)?)\b",
    re.IGNORECASE,
)
_EXACT_LEADING = re.compile(r"^(who|whose|what|which)\b", re.IGNORECASE)


def _is_temporal_query(query: str) -> bool:
    """True when the query carries a recency/time marker (deterministic scan).

    Gates the temporal lane + Gaussian recency boost. Markers: recent/latest/
    today/yesterday/week/month/year/when/before/after/since/ago, a bare 4-digit
    year (20xx), or an ISO date. Word-boundaried to avoid substring false hits.
    """
    if not query:
        return False
    return _TEMPORAL_MARKERS.search(query) is not None


def _classify_intent(query: str) -> str:
    """Deterministic intent label: exact | temporal | relationship | conceptual.

    Precedence: a literal/lookup signal (quotes, a wikilink, or a leading
    who/what/which) wins as "exact"; then temporal markers; then relationship
    markers; else the common "conceptual" case (semantic recall). Used only to
    pick a lane-weight tuple — never changes WHICH candidates are considered,
    only how they're fused.
    """
    q = (query or "").strip()
    if not q:
        return "conceptual"
    if '"' in q or "[[" in q:
        return "exact"
    if _EXACT_LEADING.match(q):
        return "exact"
    if _is_temporal_query(q):
        return "temporal"
    if _RELATIONSHIP_MARKERS.search(q):
        return "relationship"
    return "conceptual"


def _parse_date(value: str | None) -> date | None:
    """Best-effort ISO date parse (YYYY-MM-DD prefix); None when unparseable."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _recency_multiplier(days_old: float, config: RankingConfig = DEFAULT_RANKING) -> float:
    """Gaussian recency weight: peaks at `temporal_boost` for a brand-new page,
    decaying to 1.0 over `temporal_sigma_days`. Returns 1.0 when boost is off."""
    if config.temporal_boost == 1.0:
        return 1.0
    sigma = config.temporal_sigma_days or 1.0
    return 1.0 + (config.temporal_boost - 1.0) * math.exp(
        -(days_old ** 2) / (2.0 * sigma ** 2)
    )


def _apply_temporal_boost(
    fused: list[tuple[str, float]],
    vault_root: Path,
    query: str,
    config: RankingConfig = DEFAULT_RANKING,
) -> list[tuple[str, float]]:
    """Re-sort fused `(path, score)` after a Gaussian recency multiplier.

    Mirrors `_apply_type_boost`/`_apply_status_demotion` but gated on BOTH a
    temporal query AND `temporal_boost != 1.0`, so it is a strict no-op for the
    default config and for every non-temporal query. Pages with no parseable
    `updated`/`captured` date keep their score (multiplier 1.0).
    """
    if not _is_temporal_query(query) or config.temporal_boost == 1.0:
        return fused
    today = date.today()
    adjusted: list[tuple[str, float]] = []
    for path, score in fused:
        page = _CACHE.get(vault_root / path, vault_root)
        d = _parse_date(page.updated) if page else None
        if d is None:
            mult = 1.0
        else:
            days_old = max(0.0, float((today - d).days))
            mult = _recency_multiplier(days_old, config)
        adjusted.append((path, score * mult))
    adjusted.sort(key=lambda t: (-t[1], t[0]))
    return adjusted


def _recency_ranking(
    candidate_paths: list[str], vault_root: Path, cap: int
) -> list[str]:
    """The temporal fusion lane: candidate paths ordered most-recently-updated
    first. Undated pages are dropped (no recency vote). Capped at `cap`."""
    dated: list[tuple[date, str]] = []
    seen: set[str] = set()
    for p in candidate_paths:
        if p in seen:
            continue
        seen.add(p)
        page = _CACHE.get(vault_root / p, vault_root)
        if page is None:
            continue
        d = _parse_date(page.updated)
        if d is not None:
            dated.append((d, p))
    dated.sort(key=lambda t: (-t[0].toordinal(), t[1]))
    return [p for _, p in dated][:cap]


def _filter_by_date(
    hits: list[Hit],
    *,
    updated_after: str | None = None,
    updated_before: str | None = None,
    recency_days: int | None = None,
) -> list[Hit]:
    """Drop hits whose `updated` date falls outside the requested window.

    All three knobs are optional and off by default. A hit with no parseable
    date is dropped when any window is active (it can't be confirmed in-range).
    """
    after = _parse_date(updated_after)
    before = _parse_date(updated_before)
    floor: date | None = None
    if recency_days is not None and recency_days >= 0:
        floor = date.today() - timedelta(days=recency_days)
    if after is None and before is None and floor is None:
        return hits
    out: list[Hit] = []
    for h in hits:
        d = _parse_date(h.updated)
        if d is None:
            continue
        if after is not None and d < after:
            continue
        if before is not None and d > before:
            continue
        if floor is not None and d < floor:
            continue
        out.append(h)
    return out


def should_rerank(
    hits: list[Hit], query: str, config: RankingConfig = DEFAULT_RANKING
) -> bool:
    """Heuristic: is this query worth the reranker's model-load cost?

    True when the top-3 vector and top-3 bm25 paths disagree by >50% (the
    rankers can't agree on the best matches, so a cross-encoder tiebreak pays
    off) OR the query is long (>=5 tokens, where lexical signal is diluted).
    Deterministic and torch-free — inspects only the ranks already on `hits`.
    """
    if len((query or "").split()) >= 5:
        return True
    vec = [
        h.path
        for h in sorted(
            (h for h in hits if h.vector_rank is not None),
            key=lambda h: h.vector_rank,  # type: ignore[arg-type,return-value]
        )
    ][:3]
    bm = [
        h.path
        for h in sorted(
            (h for h in hits if h.bm25_rank is not None),
            key=lambda h: h.bm25_rank,  # type: ignore[arg-type,return-value]
        )
    ][:3]
    if not vec or not bm:
        return False
    overlap = len(set(vec) & set(bm))
    disagreement = 1.0 - overlap / max(len(vec), len(bm))
    return disagreement > 0.5


def _keyword_match_paths(vault_root: Path, query_norm: str, scope: str) -> list[str]:
    """Return paths that satisfy keyword mode's all-tokens-present gate.

    Sorted by `updated:` desc to mirror keyword-mode's ordering, so RRF's
    rank reflects keyword's own preference. Walks the same tree the keyword
    flow would, honors the navigation-file filter, and skips pages that
    can't be parsed.
    """
    if not query_norm:
        return []
    if scope == "kb":
        kb = vault_root / "Knowledge Base"
        if not kb.is_dir():
            return []
        walk = _walk_md(kb)
    else:
        from .vault import walk_vault_md
        walk = walk_vault_md(vault_root)
    matches: list[tuple[str, str]] = []  # (updated, rel_path)
    for path in walk:
        if path.name.lower() in _NAVIGATION_BASENAMES:
            continue
        page = _CACHE.get(path, vault_root)
        if page is None:
            continue
        if _make_excerpt(page, query_norm) is None:
            continue
        matches.append((page.updated or "0000-00-00", page.rel_path))
    matches.sort(reverse=True)  # most-recent first
    return [p for _, p in matches]


def _outbound_wikilink_paths(
    page: ParsedPage, vault_root: Path, resolver=None
) -> list[str]:
    """Vault-relative POSIX paths (no .md) that this page's body links to.

    Skips matches inside fenced code blocks and inline code (delegates to
    vault.find_body_wikilinks). Targets are normalised through
    `normalize_wikilink` so bare / KB-stripped / aliased forms all resolve to
    the same canonical path. Unresolvable targets and folder-hub links
    (trailing `/`) are dropped. `#anchor` is stripped — anchors are intra-
    page jumps, not separate files. Pass `resolver` to reuse one across a
    request (the graph lane does); None builds/reuses the process cache.
    """
    from .vault import (
        find_body_wikilinks,
        normalize_wikilink,
    )
    if resolver is None:
        resolver = _get_query_resolver(vault_root)
    out: list[str] = []
    seen: set[str] = set()
    for m in find_body_wikilinks(page.body):
        inner = m.group(0)[2:-2]
        target = inner.split("|", 1)[0].strip()
        if not target or target.endswith("/"):
            continue
        try:
            canonical, warning = normalize_wikilink(
                target, vault_root, resolver=resolver, strict=False
            )
        except Exception:
            continue
        if warning:
            continue  # unresolved — don't pollute the ranking
        rel = canonical.split("#", 1)[0].strip()
        if not rel:
            continue
        rel_with_md = rel if rel.endswith(".md") else rel + ".md"
        # Sanity: only walk into the KB itself for graph expansion; curated
        # trees are intentional out-of-graph references.
        if not rel_with_md.startswith("Knowledge Base/"):
            continue
        if rel_with_md in seen:
            continue
        seen.add(rel_with_md)
        out.append(rel_with_md)
    return out


_RESOLVER_CACHE: dict[Path, tuple[tuple, "object"]] = {}
_RESOLVER_LOCK = threading.Lock()


def _get_query_resolver(vault_root: Path, freshness: tuple | None = None):
    """Per-process WikilinkResolver cache, invalidated when the vault changes.

    Freshness is the digest-strength `_walk_freshness_key` triple — the old
    (count, max-mtime) pair missed pure renames, which change the resolver's
    stem/title maps without touching count or any mtime. Pass `freshness`
    (from the request's FreshnessSnapshot) to skip the walk; None computes it
    here for out-of-request callers.

    The build is serialized by a double-checked lock so the background warm
    thread and a racing request build the resolver once, not twice.
    """
    from .vault import WikilinkResolver
    if freshness is None:
        freshness = FreshnessSnapshot(vault_root).vault()
    cached = _RESOLVER_CACHE.get(vault_root)
    if cached and cached[0] == freshness:
        return cached[1]
    with _RESOLVER_LOCK:
        cached = _RESOLVER_CACHE.get(vault_root)
        if cached and cached[0] == freshness:
            return cached[1]
        resolver = WikilinkResolver(vault_root)
        _RESOLVER_CACHE[vault_root] = (freshness, resolver)
    return resolver


def _stem_tokens_present(page: ParsedPage, query_norm: str) -> bool:
    """All-tokens-present check using Snowball stems on both sides.

    Recovers morphological matches that the literal substring gate
    misses — query "regulation" passes for a page that mentions
    "regulator", "compounding" passes for one that mentions "compound".
    Used only as a fallback in hybrid mode; keyword mode keeps the
    strict substring gate (precision is the feature there).
    """
    if not query_norm:
        return True
    from . import bm25 as bm25_module
    text_stems = page.stem_set
    for tok in query_norm.split():
        if not tok:
            continue
        if bm25_module.stem_word(tok) not in text_stems:
            return False
    return True


def _stem_anchored_excerpt(page: ParsedPage, query_norm: str) -> str:
    """Snippet anchored on the first body word whose stem matches the query.

    Falls back to the leading body snippet when nothing in the body matches
    a query stem (e.g. the match was title-only).
    """
    from . import bm25 as bm25_module
    body = page.body.strip()
    if not body:
        return ""
    query_stems = {bm25_module.stem_word(t) for t in query_norm.split() if t}
    if not query_stems:
        return _collapse(body[:EXCERPT_MAX_LEN])
    anchor_idx = -1
    anchor_len = 0
    # Walk body words in order; first one whose stem is in query_stems wins.
    for m in re.finditer(r"[A-Za-z0-9]+", body):
        word = m.group(0)
        if bm25_module.stem_word(word.lower()) in query_stems:
            anchor_idx = m.start()
            anchor_len = len(word)
            break
    if anchor_idx == -1:
        return _collapse(body[:EXCERPT_MAX_LEN])
    start = max(0, anchor_idx - EXCERPT_RADIUS)
    end = min(len(body), anchor_idx + anchor_len + EXCERPT_RADIUS)
    snippet = body[start:end]
    if start > 0:
        snippet = "…" + snippet.lstrip()
    if end < len(body):
        snippet = snippet.rstrip() + "…"
    return _collapse(snippet)


def _semantic_excerpt(
    page: ParsedPage,
    query_norm: str,
    best_chunk: str | None,
    keyword_excerpt: str | None,
) -> str:
    """Prefer the matching chunk text (trimmed); fall back to the keyword excerpt."""
    if best_chunk:
        # Strip the title prefix the chunker prepends — it's redundant with
        # the Hit.title field.
        body = best_chunk
        title_prefix = (page.title or "").strip()
        if title_prefix and body.startswith(title_prefix + "\n\n"):
            body = body[len(title_prefix) + 2:]
        snippet = body.strip()[:EXCERPT_MAX_LEN].strip()
        if len(body) > EXCERPT_MAX_LEN:
            snippet = snippet.rstrip() + "…"
        return _collapse(snippet)
    return keyword_excerpt or ""


def _walk_md(root: Path):
    """Yield every .md path under root, skipping excluded subtrees.

    Skips Obsidian `*.sync-conflict-*.md` files — transient conflict
    duplicates that would otherwise pollute the index and search results.
    """
    for child in root.iterdir():
        if child.is_dir():
            if child.name in EXCLUDED_DIR_NAMES:
                continue
            yield from _walk_md(child)
        elif (
            child.is_file()
            and child.suffix.lower() == ".md"
            and ".sync-conflict-" not in child.name
        ):
            yield child


def _parse_page(path: Path, mtime: float, vault_root: Path) -> ParsedPage | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        log.warning("could not read %s: %s", path, e)
        return None

    fm_match = FRONTMATTER_PATTERN.match(text)
    if fm_match:
        try:
            frontmatter = yaml.safe_load(fm_match.group(1)) or {}
            if not isinstance(frontmatter, dict):
                frontmatter = {}
        except yaml.YAMLError as e:
            log.warning("YAML parse error in %s: %s", path, e)
            frontmatter = {}
        body = fm_match.group(2)
        # The FRONTMATTER_PATTERN consumes the closing `\n---\n` but not the
        # blank line that conventionally follows. Strip a single leading `\n`
        # so callers (notably `get`) can feed `body` back into `edit` without
        # accumulating blanks across round-trips.
        if body.startswith("\n"):
            body = body[1:]
    else:
        frontmatter = {}
        body = text

    h1_match = H1_PATTERN.search(body)
    title = h1_match.group(1).strip() if h1_match else path.stem

    try:
        rel_path = path.resolve().relative_to(vault_root.resolve()).as_posix()
    except ValueError:
        rel_path = path.as_posix()

    return ParsedPage(
        path=path,
        rel_path=rel_path,
        frontmatter=frontmatter,
        body=body,
        title=title,
        mtime=mtime,
    )


def _passes_filters(
    page: ParsedPage,
    *,
    vault_root: Path | None = None,
    types: list[str] | None,
    projects: list[str] | None,
    tags: list[str] | None,
    speakers: list[str] | None = None,
    file_types: list[str] | None = None,
    exclude_file_types: list[str] | None = None,
) -> bool:
    # `excluded` tier (_access.yaml): never surfaced. Checked first — an excluded
    # page is invisible regardless of how well it matches. (vault_root omitted in
    # unit tests → skip; real find paths always pass it.)
    if vault_root is not None:
        from . import access
        if not access.is_indexable(vault_root, page.rel_path):
            return False
    if types and page.page_type not in types:
        return False
    if projects:
        page_projects = _all_projects(page.frontmatter)
        if not any(p in page_projects for p in projects):
            return False
    if tags:
        page_tags = set(page.tags)
        if not any(t.lower() in page_tags for t in tags):
            return False
    if speakers:
        page_speakers = {s.lower() for s in page.speakers}
        if not any(s.lower() in page_speakers for s in speakers):
            return False
    # File-type scoping (opt-in; default None/None lets every kind through — a
    # search must never hide an artifact type by default).
    if file_types or exclude_file_types:
        kind = page.file_kind
        if file_types and kind not in {ft.lower() for ft in file_types}:
            return False
        if exclude_file_types and kind in {ft.lower() for ft in exclude_file_types}:
            return False
    return True


def _all_projects(fm: dict) -> set[str]:
    out: set[str] = set()
    if (p := fm.get("project")):
        out.add(str(p))
    if (ps := fm.get("projects")):
        if isinstance(ps, list):
            out.update(str(x) for x in ps)
        else:
            out.add(str(ps))
    return out


def _make_excerpt(page: ParsedPage, query_norm: str) -> str | None:
    """Return ~200-char snippet anchored to the query; None if no match.

    Tokenizes the query on whitespace and requires every token to appear in
    title or body (case-insensitive, any order). So `contract employment`
    matches a page mentioning "employment contract" — natural-language
    queries don't have to guess exact phrasing.

    If query is empty, returns the first ~200 chars of body (no match required).
    """
    body = page.body_stripped
    if not query_norm:
        snippet = body[:EXCERPT_MAX_LEN]
        return _collapse(snippet)
    title_norm = page.title_norm
    body_norm = page.body_norm
    tokens = query_norm.split()
    if not tokens:
        snippet = body[:EXCERPT_MAX_LEN]
        return _collapse(snippet)
    # Every token must appear somewhere in title or body.
    for tok in tokens:
        if tok not in title_norm and tok not in body_norm:
            return None
    # Pick the anchor: first token's first body occurrence; if every token is
    # title-only, return a leading body snippet for context.
    anchor_idx = -1
    anchor_len = 0
    for tok in tokens:
        idx = body_norm.find(tok)
        if idx != -1:
            anchor_idx = idx
            anchor_len = len(tok)
            break
    if anchor_idx == -1:
        snippet = body[:EXCERPT_MAX_LEN]
        return _collapse(snippet)
    start = max(0, anchor_idx - EXCERPT_RADIUS)
    end = min(len(body), anchor_idx + anchor_len + EXCERPT_RADIUS)
    snippet = body[start:end]
    if start > 0:
        snippet = "…" + snippet.lstrip()
    if end < len(body):
        snippet = snippet.rstrip() + "…"
    return _collapse(snippet)


def _collapse(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def clear_cache() -> None:
    """Test hook: flush every in-process find cache between tests — parsed
    pages, the wikilink resolver, the hot find-result cache, and the vault
    inbound-link index."""
    _CACHE.entries.clear()
    _RESOLVER_CACHE.clear()
    with _FIND_CACHE_LOCK:
        _FIND_CACHE.clear()
    freshness.clear()
    from . import vault as vault_module
    vault_module.clear_inbound_index()
