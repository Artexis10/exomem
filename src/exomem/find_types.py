"""Data contracts shared by the find pipeline and callers."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import date
from functools import cached_property
from pathlib import Path
from typing import Any

from . import temporal


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
    # Exact raw-byte identity captured by the corpus parser. Private retrieval
    # state: never serialized, but lets the post-cache release gate detect a
    # retrieval-to-projection swap even when size/mtime are preserved.
    snapshot_hash: str | None = None
    # Whether an authored frontmatter block parsed as a YAML mapping. This is
    # private classification state, not part of any public result shape.
    frontmatter_valid: bool = True

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
        """Canonical ISO rendering of the recorded day or instant.

        Not `str(value)`: PyYAML hands back a `datetime` for an unquoted
        timestamp, and `str()` on that yields a space-separated form
        (`2026-08-05 09:12:33+00:00`) that neither round-trips as ISO nor
        matches what `semantic_unit_read` renders for the same field. Going
        through `temporal` keeps one spelling across every result surface, and
        keeps the lexicographic sorts in `find` and `lexstore` monotonic when
        date-only and timestamped pages are interleaved.
        """
        u = self.frontmatter.get("updated") or self.frontmatter.get("captured") or ""
        if isinstance(u, date):
            return temporal.stamp(u)
        moment = temporal.parse(u)
        if moment is None:
            return str(u)
        return temporal.stamp(moment.instant or moment.day)

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
    rerank_raw_score: float | None = None
    rerank_input_rank: int | None = None
    rerank_multiplier_chain: list[dict[str, float | str]] = field(default_factory=list)
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
    # Bounds this hit matched without the ordering actually being decidable —
    # a page recorded only to the day, against a bound carrying a time inside
    # it. Empty for every hit whose order was genuinely determined.
    order_indeterminate: list[str] = field(default_factory=list)
    relation_match: dict[str, Any] | None = None
    matched_units: list[dict[str, Any]] | None = None
    matched_units_truncated: int = 0
    result_type: str | None = None
    mixed_units_truncated: int = 0
    snapshot_hash: str | None = None
    # Release-plane annotation, attached by `governance.egress.annotate_hits`
    # strictly AFTER `find()` returns (design D2). It is per-principal, so it
    # must never be present on a candidate stored in the shared `_FIND_CACHE`
    # — `find()` deep-copies into the cache before annotation ever runs, and
    # `test_find_hot_cache_stays_principal_free` pins that.
    decision: Any = None

    def as_dict(self) -> dict:
        out: dict = {
            "path": self.path,
            "type": self.type,
            "scope": self.scope,
            "title": self.title,
            "updated": self.updated,
            "excerpt": self.excerpt,
        }
        if self.order_indeterminate:
            out["order_indeterminate"] = list(self.order_indeterminate)
        if self.graph_provenance is not None:
            out["graph"] = {
                "relation_type": self.graph_provenance.relation_type,
                "direction": self.graph_provenance.direction,
                "seed": self.graph_provenance.seed,
            }
        if self.relation_match is not None:
            out["relation_match"] = self.relation_match
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
        if self.matched_units is not None:
            out["matched_units"] = self.matched_units
            if self.matched_units_truncated:
                out["matched_units_truncated"] = self.matched_units_truncated
        if self.result_type is not None:
            out["result_type"] = self.result_type
        if self.mixed_units_truncated:
            out["mixed_units_truncated"] = self.mixed_units_truncated
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
        if self.graph_provenance is not None:
            out["graph"] = {
                "relation_type": self.graph_provenance.relation_type,
                "direction": self.graph_provenance.direction,
                "seed": self.graph_provenance.seed,
            }
        if self.relation_match is not None:
            out["relation_match"] = self.relation_match
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
        if self.matched_units is not None:
            out["matched_units"] = self.matched_units
            if self.matched_units_truncated:
                out["matched_units_truncated"] = self.matched_units_truncated
        if self.result_type is not None:
            out["result_type"] = self.result_type
        if self.mixed_units_truncated:
            out["mixed_units_truncated"] = self.mixed_units_truncated
        return out


@dataclass
class SemanticUnitHit:
    """One independently ranked, parent-citable semantic-unit result."""

    unit_ref: str
    form: str
    category_raw: str
    category_key: str
    category: str
    kind: str
    content: str
    excerpt: str
    tags: list[str]
    context: str | None
    source_anchor: str | None
    source_span: dict[str, int]
    source_hash: str
    parent_path: str
    parent_ref: str | None
    parent_title: str
    parent_type: str | None
    parent_status: str | None
    parent_updated: str
    parent_superseded_by: list[str] = field(default_factory=list)
    relations: list[dict[str, Any]] = field(default_factory=list)
    # Governed unit metadata. Absent means "not judged" / "no check date", which
    # is a different statement from any value, so both stay `None` and are
    # omitted from every serializer rather than emitted as null.
    verdict: str | None = None
    check_by: str | None = None
    relation_match: dict[str, Any] | None = None
    bm25_rank: int | None = None
    bm25_score: float | None = None
    vector_rank: int | None = None
    vector_score: float | None = None
    mixed_units_truncated: int = 0
    # Private retrieval snapshot identity.  Governance consumes this before
    # projection so a result cannot be swapped between ranking and release;
    # it is deliberately absent from every public serializer below.
    snapshot_hash: str | None = None
    # See `Hit.decision` — same per-principal, post-`find()` contract.
    decision: Any = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "result_type": "semantic_unit",
            "unit_ref": self.unit_ref,
            "form": self.form,
            "category_raw": self.category_raw,
            "category_key": self.category_key,
            "category": self.category,
            "kind": self.kind,
            "content": self.content,
            "excerpt": self.excerpt,
            "tags": self.tags,
            "context": self.context,
            "relations": self.relations,
            "source_anchor": self.source_anchor,
            "source_span": self.source_span,
            "source_hash": self.source_hash,
            "parent_path": self.parent_path,
            "parent_ref": self.parent_ref,
            "parent_title": self.parent_title,
            "parent_type": self.parent_type,
            "parent_status": self.parent_status,
            "parent_updated": self.parent_updated,
        }
        if self.verdict is not None:
            out["verdict"] = self.verdict
        if self.check_by is not None:
            out["check_by"] = self.check_by
        if self.parent_superseded_by:
            out["parent_superseded_by"] = self.parent_superseded_by
        if self.relation_match is not None:
            out["relation_match"] = self.relation_match
        if self.mixed_units_truncated:
            out["mixed_units_truncated"] = self.mixed_units_truncated
        signals: dict[str, Any] = {}
        if self.bm25_rank is not None:
            signals["bm25_rank"] = self.bm25_rank
        if self.bm25_score is not None:
            signals["bm25_score"] = round(self.bm25_score, 6)
        if self.vector_rank is not None:
            signals["vector_rank"] = self.vector_rank
        if self.vector_score is not None:
            signals["vector_score"] = round(self.vector_score, 6)
        if signals:
            out["signals"] = signals
        return out

    def as_compact_dict(self) -> dict[str, Any]:
        out = {
            "result_type": "semantic_unit",
            "unit_ref": self.unit_ref,
            "category": self.category,
            "kind": self.kind,
            "excerpt": self.excerpt,
            "source_anchor": self.source_anchor,
            "parent_path": self.parent_path,
            "parent_ref": self.parent_ref,
            "parent_title": self.parent_title,
            "parent_type": self.parent_type,
            "parent_status": self.parent_status,
            "parent_updated": self.parent_updated,
        }
        # A refuted unit ranks like any other, so the only way a compact reader
        # can tell it apart from an unexamined one is this field. It costs a
        # single short word and is the point of the whole primitive.
        if self.verdict is not None:
            out["verdict"] = self.verdict
        if self.mixed_units_truncated:
            out["mixed_units_truncated"] = self.mixed_units_truncated
        return out


#: Where a stage got its answer. `index` is a maintained index or catalogue,
#: `cache` an in-process or published cache, `declined` a stage that returned a
#: warming outcome or did not run, `computed` everything else — including a
#: corpus walk. The vocabulary is what makes a walk visible in the diagnostics
#: without a benchmark: a stage that starts scanning the scope stops being able
#: to call itself `index`.
SOURCE_INDEX = "index"
SOURCE_CACHE = "cache"
SOURCE_DECLINED = "declined"
SOURCE_COMPUTED = "computed"

#: Reported source when one stage runs more than once with different sources
#: (`filter_eligibility` runs once per lane when `result_level` is "mixed").
#: The widest wins, so a stage that computed once can never be reported as
#: index-backed — the direction that would hide a walk.
_SOURCE_RANK = {
    SOURCE_CACHE: 0,
    SOURCE_INDEX: 1,
    SOURCE_DECLINED: 2,
    SOURCE_COMPUTED: 3,
}

STAGE_SOURCES = frozenset(_SOURCE_RANK)


def _validated_source(source: str) -> str:
    if source not in _SOURCE_RANK:
        raise ValueError(
            f"unknown find timing source {source!r}; expected one of {sorted(STAGE_SOURCES)}"
        )
    return source


def _widest_source(existing: str | None, incoming: str) -> str:
    if existing is None:
        return incoming
    return max(existing, incoming, key=lambda value: _SOURCE_RANK.get(value, 0))


class StagesTable(Mapping[str, dict[str, Any]]):
    """The per-stage table, write-through from `FindTimings.span` only.

    #983 found three stages whose duration was written straight into this table
    without registering an interval. `unattributed_ms` merges intervals, so
    every one of them was counted twice — once as its own stage, once in the
    remainder — and nothing but review stood between the fourth and the same
    defect. Refusing the write is what stops it: a stage that cannot be written
    by hand cannot report time that no interval covered.
    """

    def __init__(self) -> None:
        self._entries: dict[str, dict[str, Any]] = {}

    def __getitem__(self, name: str) -> dict[str, Any]:
        return self._entries[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._entries!r})"

    def __setitem__(self, name: str, value: Any) -> None:
        raise TypeError(
            "find timing stages are write-through from FindTimings.span(); "
            f"register an interval for {name!r} rather than writing the table"
        )

    def __delitem__(self, name: str) -> None:
        raise TypeError("find timing stages cannot be deleted")

    def _entry(self, name: str) -> dict[str, Any]:
        """Internal writer seam. Only `FindTimings` may call this."""
        return self._entries.setdefault(name, {})


class FindTimings:
    """Opt-in per-stage timing collector for one find call.

    The stage table is only as honest as its coverage. A read whose measured
    stages summed to 4.4 s of a 6.1 s call (#283) had no defect in any stage —
    the missing 28 % was work that no `span` wrapped, and nothing in the output
    said so. `unattributed_ms` closes that: it is wall time inside the call
    that no span claimed, so an uninstrumented region announces itself instead
    of waiting to be found by subtracting a table by hand.

    Two properties are structural rather than reviewed: the table is
    write-through from `span` (see `StagesTable`), and every entry carries the
    `source` it was answered from.
    """

    def __init__(self) -> None:
        self._t0 = time.perf_counter()
        self.stages = StagesTable()
        self.cache: dict[str, Any] = {"enabled": False, "hit": False}
        self.profile: dict[str, Any] = {}
        # Every span's (start, end). `unattributed_ms` merges these rather than
        # summing stage times, so a nested span is not double-counted and a
        # lane that ever moves to its own thread still accounts correctly.
        self._intervals: list[tuple[float, float]] = []
        self._intervals_lock = threading.Lock()
        # Sources declared from inside an open span, consumed by its exit so
        # the exit stays the table's single writer.
        self._pending_sources: dict[str, str] = {}

    @contextmanager
    def span(self, name: str, *, source: str = SOURCE_COMPUTED, **fields: Any):
        """Time one stage and record where its answer came from.

        `source` is the stage's static answer. A stage that only learns it
        inside the span calls `mark_source` instead; this exit still performs
        the write.
        """
        _validated_source(source)
        t0 = time.perf_counter()
        try:
            yield
        finally:
            t1 = time.perf_counter()
            with self._intervals_lock:
                self._intervals.append((t0, t1))
                declared = self._pending_sources.pop(name, source)
            entry = self.stages._entry(name)
            elapsed = round((t1 - t0) * 1000.0, 3)
            if "ms" in entry:
                # A stage can run twice in one call — `filter_eligibility` does,
                # once for the unit lane and once for the page lane, whenever
                # result_level is "mixed". Assigning would report whichever ran
                # last and silently drop the other, so accumulate and say how
                # many calls the number covers.
                entry["ms"] = round(entry["ms"] + elapsed, 3)
                entry["calls"] = entry.get("calls", 1) + 1
            else:
                entry["ms"] = elapsed
            entry["source"] = _widest_source(entry.get("source"), declared)
            entry.update(fields)

    def mark_source(self, name: str, source: str) -> None:
        """Declare, from inside an open span, where that stage answered from.

        `filter_eligibility` seeds from the semantic-unit sidecar or falls
        through to the scan oracle; `bm25` answers from the maintained
        catalogue or rebuilds a Python corpus; `recall_projection` reads a
        cache, a published projection or a cold scope snapshot. None of them
        knows which before it runs, and all of them must say so afterwards.
        """
        _validated_source(source)
        with self._intervals_lock:
            self._pending_sources[name] = source

    def skipped(self, name: str) -> None:
        entry = self.stages._entry(name)
        entry["skipped"] = True
        entry["source"] = _widest_source(entry.get("source"), SOURCE_DECLINED)

    def error(self, name: str, exc: BaseException) -> None:
        entry = self.stages._entry(name)
        entry["error"] = type(exc).__name__
        # A lane that raised did the work it failed at, so it does not get to
        # claim an index; a span that already spoke keeps its own answer.
        entry.setdefault("source", SOURCE_COMPUTED)

    def _covered_seconds(self) -> float:
        """Wall seconds covered by at least one span, counting overlap once."""
        with self._intervals_lock:
            intervals = sorted(self._intervals)
        covered = 0.0
        reach: float | None = None  # end of the run being merged
        for start, end in intervals:
            if reach is None or start > reach:
                covered += end - start
                reach = end
            elif end > reach:
                covered += end - reach
                reach = end
        return covered

    def as_dict(self) -> dict[str, Any]:
        total = time.perf_counter() - self._t0
        return {
            "total_ms": round(total * 1000.0, 3),
            # Clamped: `as_dict` reads the clock after the last span closes, so
            # this cannot legitimately go negative, and a float epsilon that
            # made it -0.0 would read as a defect rather than as zero.
            "unattributed_ms": round(
                max(0.0, total - self._covered_seconds()) * 1000.0, 3
            ),
            "cache": dict(self.cache),
            "profile": dict(self.profile),
            "stages": {k: dict(v) for k, v in self.stages.items()},
        }


def timing_span(
    timings: FindTimings | None,
    name: str,
    *,
    source: str = SOURCE_COMPUTED,
    **fields: Any,
):
    """A timing span when a collector is present, else a no-op context."""
    if timings is None:
        return nullcontext()
    return timings.span(name, source=source, **fields)


def timing_mark_source(timings: FindTimings | None, name: str, source: str) -> None:
    """`FindTimings.mark_source` for the many call sites that may have none."""
    if timings is not None:
        timings.mark_source(name, source)
