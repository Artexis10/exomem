"""Text embedding vector sidecar store.

This module owns the `.embeddings.sqlite` lifecycle: chunk vectors, matrix
caching, and sqlite-vec fallback behavior. Model loading and encoding stay in
`embeddings.py`; this file is storage only.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from collections.abc import Set as AbstractSet
from itertools import chain
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple, NoReturn, cast

import numpy as np

from . import (
    call_spans,
    cloud_cell,
    foreground_activity,
    foreground_priority,
    index_paths,
    recall_space,
    reserved_paths,
    semantic_index,
    sidecar_store,
    vecstore,
)
from .vector_index_common import vec_gate

log = logging.getLogger(__name__)


def _sqlite_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
    with reserved_paths._subsystem_authority_scope("embedding_index"):
        return _sqlite_connect_owned(database, *args, **kwargs)


def _sqlite_connect_owned(
    database: Any, *args: Any, **kwargs: Any
) -> sqlite3.Connection:
    return sqlite3.connect(database, *args, **kwargs)

#: The width of the legacy English space, kept for callers that name it. A
#: sidecar's own width is `EmbeddingIndex.dim`, read from its vector-space record.
VECTOR_DIM = recall_space.LEGACY_DIM
#: 4: rows are selection-free structural occurrences, and each parent's
#: coverage record lives in `meta`. An older table is dropped, not migrated.
SEMANTIC_UNIT_SCHEMA_VERSION = 4
#: `meta` key prefix of one parent's neutral structural coverage record. A storage
#: key format this sidecar alone writes and reads; it decides no meaning.
COVERAGE_PREFIX = "occurrence_coverage:"
#: The first key past the prefix's range, so a range scan uses the key index.
_COVERAGE_END = COVERAGE_PREFIX[:-1] + chr(ord(COVERAGE_PREFIX[-1]) + 1)
#: The generation `semantic_unit_parent_states` reports for an absent record. A
#: sentinel, not a vocabulary value: no parse produces it.
MISSING_COVERAGE = "missing-coverage"
#: The occurrence columns, in table order, before `vector` and `file_mtime`. The
#: table's own schema, named once for every reader and writer of its rows.
OCCURRENCE_COLUMNS = (  # nosemgrep: ep-word-set -- The table's own column names.
    "unit_key", "record_type", "parent_path", "parent_ref", "parent_generation",
    "parent_source_hash", "parser_version", "form", "content", "unit_source_hash",
    "source_order",
)
_CONTENT_COLUMN = OCCURRENCE_COLUMNS.index("content")
#: The counter every unit write bumps inside its own transaction.
SEMANTIC_UNIT_GENERATION = "semantic_unit_generation"
#: The tables holding this sidecar's vectors; the first row read for a legacy width.
_VECTOR_TABLES = ("chunks", "semantic_unit_vectors")

# Per-path change log for the read-side bounded catch-up (see sidecar_store's
# "bounded catch-up" note). The table records the generation at which each
# `chunks.file_path` was last mutated; the two meta keys bound the contiguous run
# of generations that log fully covers, so a bump from any writer that does not
# maintain it — an older generation-AWARE binary sharing this vault, a manual
# repair, a future in-tree writer — forces the full reload instead of reading as
# "this write changed nothing".
CHUNK_PATH_LOG = sidecar_store.PathChangeLog(
    "chunk_path_log", "chunk_path_log_from", "chunk_path_log_upto"
)

# Catch-up bounds. PATHS is the real cost knob, not generations: one bump can
# retire a whole batch of paths (a purge) and many bumps can rewrite one path
# (repeated upserts), so the changed-path count — not the generation delta — is
# what the splice actually pays for. Per path it costs one `file_block` walk over
# the key list plus that path's rows; the whole delta then costs ONE concatenate.
# A full reload instead fetches and re-materializes every vector blob (a ~3 KB
# BLOB per row against a bare string compare per key), so the per-path unit is
# more than an order of magnitude cheaper than the per-row unit it replaces. 32
# is set an order of magnitude below where those two could plausibly meet, and
# comfortably above the 1-11 path deltas seen in production; it is a safety bound
# on a fallback, not a tuned break-even, and the fallback is always correct.
# GENERATIONS is only a cheap pre-filter: a cache that far behind belongs to an
# idle process rejoining a busy vault, where one clean reload is the simpler
# answer.
CATCHUP_MAX_PATHS = 32
CATCHUP_MAX_GENERATIONS = 64
# After a background corpus-matrix load fails, a managed recall reports the
# dense lane failed, not warming, and starts no load for this long. A load that
# fails late has read the whole corpus, so retrying on every recall would repeat
# that read; the wait starts short, so a transient failure (a busy lock) costs
# seconds of dense recall, and doubles with each consecutive failure up to 64x.
MATRIX_LOAD_RETRY_SECONDS = 5.0

# --------------------------------------------------------------- generation meta
#
# The matrix caches key on an in-band WRITE GENERATION, not the sidecar file's
# mtime. The sidecars are WAL sqlite: a commit does NOT move the main file's
# mtime — a CHECKPOINT does, and under concurrent connections the checkpoint
# fires whenever the last connection (often a pure reader) closes, at a moment no
# writer runs. So mtime-keyed invalidation BOTH spuriously misses (a checkpoint
# with no content change) AND goes stale (an uncheckpointed commit leaves the
# mtime unmoved). A `meta(key, value)` row bumped inside each write's own
# transaction changes iff the content did. Third occurrence of this class in the
# repo; precedent + rationale: lexstore.cache_token.
#
# One-way legacy fallback: once a sidecar's generation reaches >= 1, the cache
# trusts (epoch, generation, instance) EXCLUSIVELY and stops checking mtime. A
# write from a PRE-generation binary (one that predates this whole mechanism)
# past that point is invisible to invalidation — old and new binaries writing
# the SAME sidecar concurrently is unsupported. Fine for this single-user,
# single-machine-per-sidecar deployment; would need re-litigating for multi-writer.


class _EmbCache(NamedTuple):
    """EmbeddingIndex's in-memory matrix cache. `(epoch, generation, instance)` is
    the write token (F1-F3); `mtime` is retained only for the gen==0 legacy
    fallback. `metadata[i] = (file_path, chunk_idx)`; `matrix[i]` = its vector."""

    epoch: int
    generation: int
    instance: int
    mtime: float
    recall_policy_identity: tuple[str, str]
    metadata: list[tuple[str, int]]
    matrix: np.ndarray
    space: recall_space.SpaceIdentity | None = None


class ChunkProof(NamedTuple):
    """The actual vector snapshot that supplied a selected chunk's score."""

    index: EmbeddingIndex
    token: tuple[int, int, int]
    space: recall_space.SpaceIdentity | None


class ChunkRef(NamedTuple):
    """A request-local passage reference; it owns no connection or matrix."""

    proof: ChunkProof
    path: str
    chunk: int


def _cache_covers_publication(
    cached: _EmbCache | None, token: tuple[int, ...] | None,
    identity: recall_space.SpaceIdentity | None,
) -> bool:
    """A failed token probe must not serve rows from an obsolete publication."""
    # The first producer or a refined fingerprint can change space without a
    # chunk generation bump. Retire that cache through the existing reload path.
    if cached is None or cached.space != identity:
        return False
    if token is None:
        return True
    # Semantic-unit generation does not change the chunk matrix.
    epoch, generation, instance = token[:3]
    return cached.instance == instance and (
        cached.epoch, cached.generation
    ) >= (epoch, generation)


class _MaskCache(NamedTuple):
    """One memoized eligibility row mask for `search`'s allowed-paths filter.

    `metadata` is held by STRONG reference and matched with `is`, NOT by the
    write token `all_vectors()` keys on — deliberately the stronger of the two
    invariants. Every producer of the matrix cache is copy-on-write
    (`_load_all_rows`, `_splice_path_blocks` under `_patch_cache` /
    `_catch_up_cache`, and `_purge_cache_paths` all build fresh containers), so
    a different row set is ALWAYS a different list object; the converse does not
    hold, because an equal `(epoch, generation, instance)` triple still falls
    back to mtime on gen==0 legacy sidecars, where mtime both spuriously misses
    and goes stale (see the generation-meta note). Reading the token separately
    would also race: another thread may swap `self._cache` between
    `all_vectors()` returning and the token being read, keying a mask against a
    generation its rows never came from. Holding the reference additionally
    stops the list being freed and its `id()` reused by an unrelated one, so a
    mask can never outlive the matrix it was computed against.

    `allowed_paths` is matched by CONTENT (a frozenset), never by identity: the
    set belongs to the caller, which may mutate it in place between two queries
    at the same scope, and a stale mask would return rows the caller excluded —
    far worse than the slowness this cache exists to remove. `frozenset(x)`
    returns `x` itself when it is already a frozenset, so an immutable caller
    pays nothing for that safety.
    """

    metadata: list[tuple[str, int]]
    allowed_paths: frozenset[str]
    mask: np.ndarray
    eligible: int


def _splice_path_blocks(
    metadata: list[tuple[str, int]],
    matrix: np.ndarray,
    replacements: dict[str, tuple[list[tuple[str, int]], np.ndarray | None]],
) -> tuple[list[tuple[str, int]], np.ndarray]:
    """Replace whole per-path row blocks in a `(metadata, matrix)` pair.

    THE splice: the write-side single-path patch and the read-side bounded
    catch-up both go through here, so there is exactly one implementation of
    "swap a file's contiguous block for its current rows, keeping the pair sorted
    by file_path and the two halves the same length".

    `metadata` is sorted by file_path (both producers keep it that way), so
    `sidecar_store.file_block` locates each path's block — or, for a path with no
    rows yet, its insertion point. It scans from index 0 every time, so this is
    O(paths x rows), not a single pass; that is affordable only because the
    caller bounds the path count (CATCHUP_MAX_PATHS). The replacements are
    processed in sorted order and `cursor` only ever moves forward, so blocks
    that go backwards mean the caller handed in unsorted metadata and raise
    rather than silently scrambling the matrix. An empty replacement (`new_vecs`
    None or zero rows) deletes the block.

    Copy-on-write: builds fresh containers and never mutates the arrays a
    concurrent reader may be holding. Raises ValueError on any inconsistency;
    both callers treat that as "drop the cache and take the full reload".
    """
    keys = [m[0] for m in metadata]
    out_meta: list[tuple[str, int]] = []
    parts: list[np.ndarray] = []
    cursor = 0
    for path in sorted(replacements):
        lo, hi = sidecar_store.file_block(keys, path)
        if lo < cursor:
            raise ValueError(
                f"unsorted matrix metadata at {path}: block starts at {lo}, "
                f"behind cursor {cursor}"
            )
        out_meta.extend(metadata[cursor:lo])
        parts.append(matrix[cursor:lo])
        new_meta, new_vecs = replacements[path]
        out_meta.extend(new_meta)
        if new_vecs is not None and new_vecs.shape[0]:
            parts.append(new_vecs)
        cursor = hi
    out_meta.extend(metadata[cursor:])
    parts.append(matrix[cursor:])
    parts = [p for p in parts if p.shape[0]]
    new_matrix = (
        np.concatenate(parts, axis=0)
        if parts
        else np.zeros((0, matrix.shape[1]), dtype=np.float32)
    )
    if len(out_meta) != new_matrix.shape[0]:
        raise ValueError(
            f"splice invariant broken for {sorted(replacements)}: "
            f"{len(out_meta)} meta rows vs {new_matrix.shape[0]} vectors"
        )
    return out_meta, new_matrix


class SemanticUnitVectorHit(NamedTuple):
    """One current semantic-unit vector candidate with its raw cosine."""

    unit_ref: str
    parent_path: str
    parent_generation: str
    parent_source_hash: str
    parser_version: int
    cosine: float


class SemanticUnitVectorRow(NamedTuple):
    """One stored occurrence vector as the corpus-level read returns it.

    `parent_generation` travels with the geometry on purpose: a consumer that
    reads these vectors against a NEWER parse of the same page would be reading
    deleted text, and an occurrence key alone cannot detect that.
    """

    unit_key: str
    source_order: int
    vector: np.ndarray
    parent_generation: str


class _UnitBlock(NamedTuple):
    """Unit vectors with the stamps a hit carries and validation reads.

    `rows[i] = (unit_key, parent_path, parent_generation, parent_source_hash,
    parser_version)` and `matrix[i]` is its vector. `parent_of[i]` indexes
    `parents`, the block's distinct parent paths, so an allowed-parents filter
    costs one membership test per parent rather than one per unit. Unit content,
    form, category and kind are never held.
    """

    rows: list[tuple[str, str, str, str, int]]
    parents: list[str]
    parent_of: np.ndarray
    matrix: np.ndarray


class _UnitCache(NamedTuple):
    """Neutral resident vectors and compact proof at an exact snapshot token.

    Every unit write moves `generation`; a chunk-only write leaves it alone.
    `width` binds the matrix to its loaded vector space. No selected meaning
    or continuing admission authority lives here.
    """

    epoch: int
    generation: int
    instance: int
    width: int
    block: _UnitBlock
    coverage: dict[str, tuple[str, int]]
    unproved: frozenset[str]


_UNIT_COLUMNS = (
    "unit_key, parent_path, parent_generation, parent_source_hash, parser_version, vector"
)


def _unit_block(rows: Iterable[tuple], count: int, width: int) -> _UnitBlock:
    """Read at most `count` unit rows of `width`-wide vectors; skip other widths."""
    matrix = np.empty((count, width), dtype=np.float32)
    parent_of = np.empty(count, dtype=np.int32)
    kept: list[tuple[str, str, str, str, int]] = []
    parents: list[str] = []
    slots: dict[str, int] = {}
    # Share repeated strings within this load only: sys.intern never frees on 3.12.
    shared: dict[str, str] = {}
    for unit_key, parent_path, generation, source_hash, parser_version, blob in rows:
        if not isinstance(blob, bytes) or len(blob) != width * 4:
            continue
        try:
            version = int(parser_version)
        except (TypeError, ValueError):
            continue
        if len(kept) == count:
            raise ValueError("semantic-unit row count differs from snapshot")
        path = str(parent_path)
        slot = slots.get(path)
        if slot is None:
            slot = slots[path] = len(parents)
            parents.append(path)
        matrix[len(kept)] = np.frombuffer(blob, dtype=np.float32)
        parent_of[len(kept)] = slot
        generation, source_hash = str(generation), str(source_hash)
        kept.append(
            (
                str(unit_key),
                parents[slot],
                shared.setdefault(generation, generation),
                shared.setdefault(source_hash, source_hash),
                version,
            )
        )
    size = len(kept)
    if size < count:
        matrix, parent_of = matrix[:size].copy(), parent_of[:size].copy()
    return _UnitBlock(kept, parents, parent_of, matrix)


def _unit_mask(block: _UnitBlock, admitted: Callable[[str], bool]) -> np.ndarray:
    """Admit each parent before any score can spend a transport slot."""
    mask = np.fromiter(
        (admitted(path) for path in block.parents), dtype=bool, count=len(block.parents)
    )
    return mask[block.parent_of]


def _best_with_ties(scores: np.ndarray, window: int) -> np.ndarray:
    """Positions of the `window` highest scores and of every score tied with the
    lowest of them, so ranking this subset by the full key gives exactly the first
    `window` rows that ranking every row would. NaN ranks below every number."""
    if len(scores) <= window:
        return np.arange(len(scores))
    negated = -scores
    cut = negated[np.argpartition(negated, window - 1)[window - 1]]
    if np.isnan(cut):
        # Fewer than `window` scores are numbers: every row is needed.
        return np.arange(len(scores))
    return np.flatnonzero(negated <= cut)


def _unit_rank(candidate: tuple[float, int, tuple[str, str, str, str, int]]) -> tuple:
    """Best score first, then `unit_ref`: the order the unit lane publishes.

    NaN ranks last. The row's position in the scan breaks a tie on both, which
    makes the order total, so a wider window always starts with a narrower one.
    """
    score, position, row = candidate
    return (score != score, -score, row[0], position)


def _unit_top(
    blocks: Callable[[], Iterable[tuple[_UnitBlock, np.ndarray | None]]],
    query: np.ndarray,
    window: int,
    raw_window: int,
    parents: semantic_index.AdmittedParents,
    refs: frozenset[str] | None,
) -> tuple[list[tuple[float, tuple[str, str, str, str, int]]], float, bool]:
    """Bounded selected winners and a score bound on omitted transport rows.

    Every cutoff tie is proposed before selected mapping. Mapping can suppress
    or merge occurrences, so the caller widens until this bound proves its prefix.
    """
    best: dict[str, tuple[float, int, tuple[str, str, str, str, int]]] = {}
    omitted_bound = -np.inf
    complete = True
    offset = 0
    for block, mask in blocks():
        positions = np.arange(len(block.rows)) if mask is None else np.flatnonzero(mask)
        start, offset = offset, offset + len(block.rows)
        if not len(positions):
            continue
        if len(positions) == len(block.rows):
            scores = block.matrix @ query
        else:
            # Admission precedes scoring, without a second admitted corpus matrix.
            scores = np.empty(len(positions), dtype=np.float32)
            for first in range(0, len(positions), SEMANTIC_UNIT_READ_BATCH):
                batch = positions[first : first + SEMANTIC_UNIT_READ_BATCH]
                scores[first : first + len(batch)] = block.matrix[batch] @ query
        proposed = _best_with_ties(scores, raw_window)
        if len(proposed) < len(positions):
            complete = False
            omitted_bound = max(omitted_bound, float(scores[proposed].min()))
        for first in range(0, len(proposed), SEMANTIC_UNIT_READ_BATCH):
            batch = proposed[first : first + SEMANTIC_UNIT_READ_BATCH]
            parents.prefetch(block.rows[int(positions[n])][1] for n in batch)
            for n in batch:
                position = int(positions[n])
                key, path, generation, source_hash, version = block.rows[position]
                parent = parents.parent(path)
                if parent is None or (
                    parent.metadata.get("parent_generation"),
                    parent.metadata.get("parent_source_hash"),
                    parent.metadata.get("parser_version"),
                ) != (generation, source_hash, version):
                    continue
                unit = parent.structure.unit(key)
                if (
                    unit is None
                    or not unit.unit_ref
                    or (refs is not None and unit.unit_ref not in refs)
                ):
                    continue
                row = (unit.unit_ref, path, generation, source_hash, version)
                candidate = (float(scores[n]), start + position, row)
                previous = best.get(unit.unit_ref)
                if previous is None or _unit_rank(candidate) < _unit_rank(previous):
                    best[unit.unit_ref] = candidate
            # Ties can span the corpus. Keep only a window plus one transport batch.
            best = {item[2][0]: item for item in sorted(best.values(), key=_unit_rank)[:window]}
    ordered = sorted(best.values(), key=_unit_rank)
    return [(score, row) for score, _position, row in ordered], float(omitted_bound), complete


#: Paths per `file_mtimes` read: under SQLite's oldest default bound of 999
#: parameters per statement.
_PATHS_PER_READ = 900


#: Rows per batch in the corpus-level unit-vector read. Bounds the result set a
#: single SELECT materialises; it is not a limit on what the read returns.
SEMANTIC_UNIT_READ_BATCH = 2_000


#: Query rows `EmbeddingIndex.search_many` scores per matrix product. Scoring a
#: whole draft at once holds `queries x rows` float32 scores: 293 MiB for a
#: 1,000-chunk note against 76,000 rows, where one `search` per chunk peaked at
#: a single row. A block of 64 bounds that at one block (~19 MiB at 76,000 rows)
#: and keeps most of the batched speed: for 1,000 chunks against 58,000 rows under
#: a 2-CPU quota, 5.5 s at 17 MiB peak, against 4.5 s at 225 MiB for one product
#: and 100 s for one `search` per chunk.
SEARCH_MANY_BLOCK = 64
_UNSPECIFIED_SPACE = object()


def occurrence_rows(state: semantic_index.SemanticParentIndexState) -> list[tuple]:
    """One parent's occurrence rows, in `OCCURRENCE_COLUMNS` order, without vectors.

    Every occurrence some selected interpretation could emit is encoded once:
    the rich heading body or the compact content, never a selected meaning.
    """
    return [
        (
            occurrence.key, "structural_occurrence", state.path, state.parent_ref,
            state.parent_generation, state.parent_source_hash, state.parser_version,
            occurrence.form, occurrence.content, occurrence.source_hash, source_order,
        )
        for source_order, occurrence in enumerate(state.occurrences)
    ]


def coverage_record(state: semantic_index.SemanticParentIndexState) -> str:
    """The parent's neutral summary plus the occurrence keys its rows must hold.

    It binds parser generation, source hash and format, and lists every
    expected key, so an empty set still proves a parent with no occurrences.
    """
    return json.dumps(
        {
            **semantic_index.structural_metadata(state),
            "occurrences": sorted(occurrence.key for occurrence in state.occurrences),
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def expected_parent_state(
    state: semantic_index.SemanticParentIndexState,
) -> tuple[frozenset[str], frozenset[str]]:
    """What `semantic_unit_parent_states` reports for a current publication of `state`."""
    return (
        frozenset({state.parent_generation}),
        frozenset(occurrence.key for occurrence in state.occurrences),
    )


def stored_parent_states(
    conn: sqlite3.Connection, paths: list[str] | None = None
) -> dict[str, tuple[frozenset[str], frozenset[str]]]:
    """Stored generations and occurrence keys per parent, from rows and records.

    A parent appears when it has rows or a coverage record. Its generations
    hold every row's and the record's; `MISSING_COVERAGE` stands for an absent
    record. `expected_parent_state` is the value of a current publication.
    """
    rows = conn.execute(
        "SELECT parent_path, parent_generation, unit_key FROM semantic_unit_vectors"
        + ("" if paths is None else " WHERE parent_path IN (SELECT value FROM json_each(?))"),
        () if paths is None else (json.dumps(paths, ensure_ascii=False),),
    ).fetchall()
    grouped: dict[str, tuple[set[str], set[str]]] = {
        path: ({generation}, set()) for path, (generation, _count) in _coverage_index(conn, paths).items()
    }
    for parent_path, generation, unit_key in rows:
        generations, keys = grouped.setdefault(str(parent_path), ({MISSING_COVERAGE}, set()))
        generations.add(str(generation))
        keys.add(str(unit_key))
    return {
        parent_path: (frozenset(generations), frozenset(keys))
        for parent_path, (generations, keys) in grouped.items()
    }


def _write_coverage(conn: sqlite3.Connection, state: semantic_index.SemanticParentIndexState) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        (COVERAGE_PREFIX + state.path, coverage_record(state)),
    )


def _delete_coverage(conn: sqlite3.Connection, paths: list[str] | None = None) -> None:
    if paths is None:
        conn.execute(
            "DELETE FROM meta WHERE key >= ? AND key < ?",
            (COVERAGE_PREFIX, _COVERAGE_END),
        )
        return
    conn.executemany("DELETE FROM meta WHERE key = ?", ((COVERAGE_PREFIX + path,) for path in paths))


def indexed_parent_paths(conn: sqlite3.Connection) -> list[str]:
    """Parents this sidecar holds chunks or an occurrence coverage record for."""
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT file_path FROM chunks UNION "
            "SELECT substr(key, ?) FROM meta WHERE key >= ? AND key < ?",
            (len(COVERAGE_PREFIX) + 1, COVERAGE_PREFIX, _COVERAGE_END),
        )
    ]


def _coverage_index(
    conn: sqlite3.Connection, paths: list[str] | None = None
) -> dict[str, tuple[str, int]]:
    """`{path: (record generation, expected occurrence count)}`, parsed by SQLite."""
    select = (
        "SELECT substr(key, ?), json_extract(value, '$.parent_generation'), "
        "json_array_length(value, '$.occurrences') FROM meta "
    )
    if paths is None:
        rows = conn.execute(
            select + "WHERE key >= ? AND key < ? AND json_valid(value)",
            (len(COVERAGE_PREFIX) + 1, COVERAGE_PREFIX, _COVERAGE_END),
        )
    else:
        rows = conn.execute(
            select + "WHERE key IN (SELECT ? || value FROM json_each(?)) AND json_valid(value)",
            (len(COVERAGE_PREFIX) + 1, COVERAGE_PREFIX, json.dumps(paths, ensure_ascii=False)),
        )
    return {
        str(path): (str(generation), int(count))
        for path, generation, count in rows
        if generation is not None and count is not None
    }


def _coverage_records(conn: sqlite3.Connection, paths: list[str]) -> dict[str, dict[str, Any]]:
    """Stored coverage records by parent path; an unreadable record is absent."""
    rows = conn.execute(
        "SELECT key, value FROM meta WHERE key IN (SELECT ? || value FROM json_each(?))",
        (COVERAGE_PREFIX, json.dumps(sorted(paths), ensure_ascii=False)),
    )
    records: dict[str, dict[str, Any]] = {}
    for key, value in rows:
        try:
            record = json.loads(value)
        except (TypeError, ValueError):
            continue
        if isinstance(record, dict) and isinstance(record.get("occurrences"), list):
            records[str(key)[len(COVERAGE_PREFIX):]] = record
    return records


def _unit_coverage(
    conn: sqlite3.Connection, width: int,
) -> tuple[dict[str, tuple[str, int]], frozenset[str]]:
    """Compact neutral proof of exact occurrence keys, row stamps and usable vectors.

    SQLite checks JSON through its parser and compares both key directions. Counts
    alone cannot detect a substituted occurrence. Full summaries stay on disk.
    """
    from .semantic_units import STRUCTURAL_FORMAT

    coverage: dict[str, tuple[str, int]] = {}
    unproved: set[str] = set()
    rows = conn.execute(
        "WITH records AS ("
        "SELECT substr(key, ?) AS path, "
        "CASE WHEN json_valid(value) THEN value ELSE '{}' END AS doc FROM meta "
        "WHERE key >= ? AND key < ?) "
        "SELECT path, json_extract(doc, '$.parent_generation'), "
        "json_array_length(doc, '$.occurrences'), "
        "json_extract(doc, '$.structural_complete') = 1 "
        "AND json_extract(doc, '$.parser_version') = ? "
        "AND json_extract(doc, '$.structure.format') = ? "
        "AND json_type(doc, '$.parent_generation') = 'text' "
        "AND json_type(doc, '$.parent_source_hash') = 'text' "
        "AND json_type(doc, '$.occurrences') = 'array' "
        "AND (SELECT COUNT(*) FROM semantic_unit_vectors v WHERE v.parent_path = records.path) "
        "= json_array_length(doc, '$.occurrences') "
        "AND NOT EXISTS (SELECT 1 FROM json_each(doc, '$.occurrences') expected "
        "WHERE expected.type != 'text' OR NOT EXISTS ("
        "SELECT 1 FROM semantic_unit_vectors v WHERE v.parent_path = records.path "
        "AND v.unit_key = expected.value "
        "AND v.parent_generation = json_extract(doc, '$.parent_generation') "
        "AND v.parent_source_hash = json_extract(doc, '$.parent_source_hash') "
        "AND v.parser_version = json_extract(doc, '$.parser_version') "
        "AND typeof(v.vector) = 'blob' AND length(v.vector) = ?)) "
        "AND NOT EXISTS (SELECT 1 FROM semantic_unit_vectors v WHERE v.parent_path = records.path "
        "AND v.unit_key NOT IN (SELECT value FROM json_each(doc, '$.occurrences'))) "
        "FROM records",
        (len(COVERAGE_PREFIX) + 1, COVERAGE_PREFIX, _COVERAGE_END,
         semantic_index.PARSER_VERSION, STRUCTURAL_FORMAT, width * 4),
    )
    for path, generation, count, proved in rows:
        if generation is not None and count is not None:
            coverage[str(path)] = (str(generation), int(count))
        if not proved:
            unproved.add(str(path))
    unproved.update(
        str(path)
        for (path,) in conn.execute("SELECT DISTINCT parent_path FROM semantic_unit_vectors")
        if str(path) not in coverage
    )
    return coverage, frozenset(unproved)


def _top_admitted(
    scores: np.ndarray,
    k: int,
    total: int,
    admitted: Callable[[int], bool],
    metadata: list[tuple[str, int]],
) -> list[tuple[str, int, float]]:
    """One query's `k` best admitted rows from its full score row, best first.

    The candidate window holds the query's highest-scoring rows, so the first
    `k` admitted rows in window order are its top `k` admitted rows overall; the
    window widens until `k` are found or every row has been considered.
    """
    order = -scores
    window = min(total, max(4 * k, 64))
    while True:
        if window >= total:
            ranked = np.argsort(order, kind="stable")
        else:
            candidates = np.argpartition(order, window - 1)[:window]
            ranked = candidates[np.argsort(order[candidates], kind="stable")]
        picked: list[int] = []
        for row in ranked.tolist():
            if admitted(row):
                picked.append(row)
                if len(picked) == k:
                    break
        if len(picked) == k or window >= total:
            break
        window = min(total, window * 4)
    return [(metadata[row][0], metadata[row][1], float(scores[row])) for row in picked]


class EmbeddingIndex:
    """Per-vault sqlite sidecar holding chunk-level vectors.

    The matrix returned by `all_vectors()` is cached per-process and
    invalidated by an in-band WRITE GENERATION (a `meta` row bumped inside every
    write's own transaction), NOT the sidecar mtime — see the generation-meta
    note above for why WAL-checkpoint timing makes mtime keying both spuriously
    miss and go stale. When the vec0 backend is active (`vecstore`), `search()` is served by a
    SQL-native KNN over shadow tables in the same sidecar instead, and this
    matrix stays cold — `all_vectors()` remains for audit's all-pairs sweep
    and the numpy fallback.

    numpy-lite (2026-07-04): the cache holds ONLY `(file_path, chunk_idx)`
    metadata + the float32 matrix — chunk TEXT is never resident. Text was
    most of the numpy backend's memory bill at scale (~2GB of a ~3.5GB RSS at
    200k chunks); the top-k winners' texts are point-lookups on the
    `(file_path, chunk_idx)` PRIMARY KEY at search time, exactly how the vec0
    path already hydrates metadata by rowid.
    """

    DISK_BLOCK_ROWS = 2048
    PAIRWISE_BLOCK_ROWS = 256

    def __init__(self, vault_root: Path, *, path: Path | None = None):
        self.vault_root = vault_root
        #: The sidecar this instance reads and writes: the serving one unless
        #: `path` names another (a new vector space being built beside it).
        self.path = path if path is not None else index_paths.sidecar_path(vault_root)
        #: The vector space the sidecar holds, as last read from it; None while
        #: it holds no vectors and no record. Refreshed on every connection.
        self._identity: recall_space.SpaceIdentity | None = None
        self._identity_read = False
        self._identity_token: tuple[int, int, int, int] | None = None
        self._cache: _EmbCache | None = None
        # One-slot memo for search()'s allowed-paths row mask (see _MaskCache).
        self._mask_cache: _MaskCache | None = None
        # The resident semantic-unit matrix (see `_unit_vectors`).
        self._unit_cache: _UnitCache | None = None
        # Guards in-memory cache mutation only (never held across a sqlite write).
        # Reentrant so rebuild_all()-style nesting can't self-deadlock.
        self._lock = threading.RLock()
        # Serializes unit-matrix loads apart from `_lock`, so a unit reload never
        # stalls a chunk write's `_patch_cache`.
        self._unit_lock = threading.Lock()
        # Guards `_matrix_load`, the one background corpus-matrix load. A leaf:
        # it is taken under `_lock` but never waits on it.
        self._matrix_load_lock = threading.Lock()
        self._matrix_load: threading.Thread | None = None
        self._matrix_load_failed_at: float | None = None
        self._matrix_load_failures = 0
        #: Matrix served or loaded: the use signal the idle reaper watches.
        self._hits = 0
        # vec0 backend state (see vec_gate): sync memo + per-instance retirement.
        # The vec0 column is declared at the sidecar's own width; see `_vec_prepare`.
        self._vec = vecstore.SqliteVecStore("chunks", "vector", VECTOR_DIM, "vec_chunks")
        self._vec_ready: bool | None = None
        self._vec_quant_synced = False
        self._vec_failed = False

    def _connect(self, path: Path | None = None) -> sqlite3.Connection:
        with reserved_paths._subsystem_authority_scope("embedding_index"):
            with reserved_paths._identity_coordination_scope(
                self.vault_root,
                descriptor_ids=("embeddings-store",),
            ):
                return self._connect_owned(path)

    def _connect_owned(self, path: Path | None = None) -> sqlite3.Connection:
        target = path if path is not None else self.path
        if target == self.path:
            with reserved_paths._sqlite_owner_target_scope(
                self.vault_root,
                target,
                "embeddings-store",
                create=True,
            ) as retained_target:
                return self._connect_retained(retained_target)
        return self._connect_retained(target)

    def _connect_retained(self, target: Path) -> sqlite3.Connection:
        sidecar_store.ensure_sidecar_parent(target)
        conn = _sqlite_connect_owned(target)
        sidecar_store.apply_sidecar_pragmas(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                file_path TEXT NOT NULL,
                chunk_idx INTEGER NOT NULL,
                chunk_text TEXT NOT NULL,
                vector BLOB NOT NULL,
                file_mtime REAL NOT NULL,
                PRIMARY KEY (file_path, chunk_idx)
            )
            """
        )
        sidecar_store.ensure_meta_table(conn, "chunks", self.path.name)
        # Before any write on this connection: every writer must have the log, so
        # that a writer which bumps the generation without logging its paths is
        # DETECTED rather than silently caught up (see sidecar_store).
        sidecar_store.ensure_path_change_log(conn, CHUNK_PATH_LOG)
        stored_unit_schema = conn.execute(
            "SELECT value FROM meta WHERE key = 'semantic_unit_schema_version'"
        ).fetchone()
        current_unit_schema = stored_unit_schema == (SEMANTIC_UNIT_SCHEMA_VERSION,)
        with conn:
            if not current_unit_schema:
                # The row shape changed, so an older table and its staging copy
                # are replaced rather than emptied under their old constraints.
                conn.execute("DROP TABLE IF EXISTS semantic_unit_vectors")
                conn.execute("DROP TABLE IF EXISTS embedding_build_units")
                conn.execute("DROP TABLE IF EXISTS embedding_build_coverage")
                _delete_coverage(conn)
            # The record_type CHECK fixes this table's one row format token.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS semantic_unit_vectors (
                    unit_key TEXT NOT NULL,
                    record_type TEXT NOT NULL CHECK(record_type = 'structural_occurrence'),
                    parent_path TEXT NOT NULL,
                    parent_ref TEXT,
                    parent_generation TEXT NOT NULL,
                    parent_source_hash TEXT NOT NULL,
                    parser_version INTEGER NOT NULL,
                    form TEXT NOT NULL,
                    content TEXT NOT NULL,
                    unit_source_hash TEXT NOT NULL,
                    source_order INTEGER NOT NULL,
                    vector BLOB NOT NULL,
                    file_mtime REAL NOT NULL,
                    PRIMARY KEY (parent_path, unit_key)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS semantic_unit_vectors_parent "
                "ON semantic_unit_vectors(parent_path, parent_generation)"
            )
            if not current_unit_schema:
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                    ("semantic_unit_schema_version", SEMANTIC_UNIT_SCHEMA_VERSION),
                )
                sidecar_store.bump_meta(conn, SEMANTIC_UNIT_GENERATION)
        if target == self.path:
            conn.execute("BEGIN")
            try:
                identity = recall_space.read_identity(conn, tables=_VECTOR_TABLES)
                token = self._build_token(conn)
            finally:
                conn.rollback()
            self._observe_identity(identity, token)
            self._identity_read = True
        if target == self.path:
            try:
                reserved_paths._publish_sqlite_owner_family(
                    self.vault_root,
                    target,
                    "embeddings-store",
                    conn,
                )
            except BaseException:
                conn.close()
                raise
        return conn

    # ------------------------------------------------------------ vector space

    @property
    def identity(self) -> recall_space.SpaceIdentity | None:
        """The vector space this sidecar holds; None when it holds none.

        Read from the sidecar by this instance's first connection and again by
        every later one, so it is as current as the last operation. Never
        creates the sidecar.
        """
        if not self._identity_read and self.path.exists():
            conn = self._connect()
            conn.close()
        return self._identity

    @property
    def dim(self) -> int:
        """The width of this sidecar's vectors: its record's, or the encoder's in use."""
        identity = self.identity
        return identity.dim if identity is not None else recall_space.current_dim()

    def _set_identity(self, identity: recall_space.SpaceIdentity | None) -> None:
        previous, self._identity = self._identity, identity
        if identity is None:
            return
        if self._vec.dim != identity.dim:
            self._vec = vecstore.SqliteVecStore("chunks", "vector", identity.dim, "vec_chunks")
        if previous != identity:
            # A new record re-runs the vec0 sync check, which redeclares a
            # column of another width.
            self._vec_ready = None
            self._vec_quant_synced = False

    def _observe_identity(
        self, identity: recall_space.SpaceIdentity | None, token: tuple[int, int, int, int]
    ) -> bool:
        """Remember a serving snapshot unless a newer observation already won."""
        with self._lock:
            observed = self._identity_token
            if observed is not None and observed[2] == token[2] and (
                observed[0], observed[1], observed[3]
            ) > (token[0], token[1], token[3]):
                return False
            self._set_identity(identity)
            self._identity_token = token
            return True

    def _complete_publication(
        self, identity: recall_space.SpaceIdentity | None, token: tuple[int, int, int, int]
    ) -> None:
        """Fence identity and retained caches together after the SQL commit."""
        with self._lock:
            observed = self._identity_token
            if observed is None or observed[2] == token[2]:
                self._observe_identity(identity, token)
            self._invalidate_before((self._identity_token or token)[:3])

    def _vec_prepare(self, conn: sqlite3.Connection) -> bool:
        """Whether vec0 may be synced now: only once the sidecar has a width.

        A vec0 column is declared at a fixed width, so a sidecar holding no
        vectors yet must not declare one; its first write records the width
        and then syncs.
        """
        del conn
        return self._identity is not None

    def _admit(self, conn: sqlite3.Connection, dim: int) -> None:
        """Record or check the vector space before rows of width `dim` are written."""
        with conn:
            identity = recall_space.admit(conn, self._identity, dim)
            token = self._build_token(conn)
        self._observe_identity(identity, token)

    @contextlib.contextmanager
    def encoding(self, *, load: bool = False) -> Iterator[None]:
        """Encode for this sidecar inside the block, or refuse before encoding.

        Selects the encoder that serves this sidecar's vector space and checks
        it against the sidecar's record before anything is encoded, so a query
        vector or a row made inside the block belongs to this sidecar. A
        sidecar written by another model than the recall encoder's (one a
        re-embed has not replaced yet) is served by that model, which warm-up
        keeps resident and a request never loads: `recall_space.ServingEncoderCold`
        when it is not resident. `recall_space.VectorSpaceMismatch` when the
        encoder here is another build than the one the sidecar records. A
        sidecar holding no vectors takes whatever the recall encoder produces.
        """
        identity = self.identity
        if identity is None:
            yield
            return
        model = recall_space.recall_model()
        if identity.model != model:
            if recall_space.cell_mode():
                # A cell runs no second encoder and never re-embeds in place.
                raise recall_space.VectorSpaceMismatch(
                    f"the sidecar holds {identity.model} vectors; this cell encodes with {model}"
                )
            if load:
                recall_space.previous_encoder(identity.model)
            if recall_space.previous_resident(identity.model) is None:
                raise recall_space.ServingEncoderCold(
                    f"{identity.model}, which serves this sidecar, is not resident"
                )
            fingerprint = recall_space.resident_fingerprint(identity.model)
            if not identity.accepts(identity.model, fingerprint):
                raise recall_space.VectorSpaceMismatch(
                    f"the sidecar was written by another build of {identity.model}"
                )
            with recall_space.selecting(identity.model):
                yield
            return
        if identity.fingerprint is not None and recall_space.resident_fingerprint(model) is None:
            from . import embeddings

            embeddings.get_model()
        if not identity.accepts(model, recall_space.resident_fingerprint(model)):
            raise recall_space.VectorSpaceMismatch(
                f"the sidecar was written by another build of {model}"
            )
        with recall_space.selecting(None):
            yield

    def upsert_file(
        self,
        rel_path: str,
        chunks: list[str],
        vectors: np.ndarray,
        mtime: float,
    ) -> None:
        """Replace all rows for `rel_path` in a single transaction."""
        if len(chunks) != len(vectors):
            raise ValueError(
                f"chunks/vectors length mismatch for {rel_path}: {len(chunks)} vs {len(vectors)}"
            )
        conn = self._connect()
        try:
            if chunks:
                self._admit(conn, np.asarray(vectors).shape[1])
            vec_on = vec_gate(self, conn)
            with conn:
                if vec_on:
                    # BEFORE the blob delete — the subquery needs the old rowids.
                    self._vec.dual_delete(conn, "file_path = ?", (rel_path,))
                conn.execute("DELETE FROM chunks WHERE file_path = ?", (rel_path,))
                if chunks:
                    rows = [
                        (rel_path, i, chunks[i], vectors[i].astype(np.float32).tobytes(), mtime)
                        for i in range(len(chunks))
                    ]
                    conn.executemany(
                        "INSERT INTO chunks "
                        "(file_path, chunk_idx, chunk_text, vector, file_mtime) "
                        "VALUES (?, ?, ?, ?, ?)",
                        rows,
                    )
                    if vec_on:
                        self._vec.dual_insert(conn, "file_path = ?", (rel_path,))
                # Bump the write generation INSIDE this txn — declaring the path
                # it changed, so a reader one generation behind can catch up from
                # the log instead of full-loading — then read back the FULL
                # (epoch, generation, instance) token, stable under the write
                # lock. The cache keys on it, not the mtime.
                sidecar_store.bump_generation_for_paths(
                    conn, CHUNK_PATH_LOG, [rel_path]
                )
                own_epoch, own_gen, own_instance = sidecar_store.read_meta_token(conn)
        finally:
            conn.close()
        # Patch the shared in-memory matrix in place instead of nulling it, so a
        # concurrent find() doesn't pay a full O(vault) reload for this one write.
        # numpy-lite: metadata rows carry no chunk text (see class docstring).
        new_meta = [(rel_path, i) for i in range(len(chunks))]
        new_vecs = np.asarray(vectors, dtype=np.float32) if chunks else None
        self._patch_cache(rel_path, new_meta, new_vecs, own_epoch, own_gen, own_instance)

    def _admit_producer(
        self, conn: sqlite3.Connection, producer: recall_space.SpaceIdentity
    ) -> recall_space.SpaceIdentity:
        """Compare the actual producer with the serving space under the write lock."""
        stored = recall_space.read_identity(conn, tables=_VECTOR_TABLES)
        if stored is not None and (
            stored.dim != producer.dim or not stored.accepts(producer.model, producer.fingerprint)
        ):
            raise recall_space.VectorSpaceMismatch(
                f"vectors from {producer} cannot join the serving space {stored}"
            )
        identity = stored or producer
        if stored is not None and stored.fingerprint is None and producer.fingerprint:
            identity = producer
        recall_space.write_identity(conn, identity)
        return identity

    def _publication_vec(
        self, conn: sqlite3.Connection, identity: recall_space.SpaceIdentity | None
    ) -> vecstore.SqliteVecStore | None:
        """Prepare a local mirror without committing or publishing process state."""
        if identity is None or self._vec_failed or vecstore.backend() == "numpy":
            return None
        mirror = vecstore.SqliteVecStore("chunks", "vector", identity.dim, "vec_chunks")
        if not mirror.try_load(conn):
            return None
        mirror.ensure_synced_in_transaction(conn, quant=vecstore.quant_mode() == "binary")
        return mirror

    def _invalidate_before(self, token: tuple[int, int, int]) -> None:
        """Retain compatible immutable rows for verified catch-up after publication."""
        epoch, _generation, instance = token
        with self._lock:
            cached = self._cache
            current = _cache_covers_publication(cached, token, self._identity)
            # Same-epoch rows can catch up; a changed space or instance must reload.
            if not current and (
                cached is None or cached.instance != instance or cached.epoch != epoch
            ):
                self._cache = None
            mask = self._mask_cache
            if mask is not None and (not current or mask.metadata is not cached.metadata):
                self._mask_cache = None

    def upsert_batch(
        self,
        replacements: list[tuple[str, list[str], np.ndarray, float]],
        unit_replacements: list[tuple[semantic_index.SemanticParentIndexState, np.ndarray, float]]
        | None = None,
        *,
        identity: recall_space.SpaceIdentity,
        validate: Callable[[], bool] | None = None,
    ) -> tuple[int, int, int, int] | None:
        """Publish one bounded group of prepared parents, then release old caches."""
        unit_replacements = unit_replacements or []
        if not replacements and not unit_replacements:
            return
        for path, chunks, vectors, _mtime in replacements:
            if len(chunks) != len(vectors) or (
                len(vectors) and np.asarray(vectors).shape != (len(chunks), identity.dim)
            ):
                raise ValueError(f"chunk/vector shape mismatch for {path}")
        unit_rows = []
        for state, vectors, mtime in unit_replacements:
            if len(vectors) and np.asarray(vectors).shape[1] != identity.dim:
                raise ValueError(f"semantic-unit vector width mismatch for {state.path}")
            unit_rows.append((state, self._semantic_unit_rows(state, vectors, mtime)))
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if validate is not None and not validate():
                    raise ValueError("embedding input drifted before publication")
                stored = recall_space.read_identity(conn, tables=_VECTOR_TABLES)
                if any(chunks for _path, chunks, _vectors, _mtime in replacements) or any(
                    rows for _state, rows in unit_rows
                ):
                    stored = self._admit_producer(conn, identity)
                mirror = self._publication_vec(conn, stored)
                for path, chunks, vectors, mtime in replacements:
                    if mirror is not None:
                        mirror.dual_delete(conn, "file_path = ?", (path,))
                    conn.execute("DELETE FROM chunks WHERE file_path = ?", (path,))
                    conn.executemany(
                        "INSERT INTO chunks VALUES (?, ?, ?, ?, ?)",
                        (
                            (
                                path,
                                i,
                                chunk,
                                np.asarray(vectors[i], dtype=np.float32).tobytes(),
                                mtime,
                            )
                            for i, chunk in enumerate(chunks)
                        ),
                    )
                    if mirror is not None:
                        mirror.dual_insert(conn, "file_path = ?", (path,))
                for state, rows in unit_rows:
                    self._replace_occurrences(conn, state, rows)
                if replacements:
                    sidecar_store.bump_generation_for_paths(
                        conn, CHUNK_PATH_LOG, [path for path, *_rest in replacements]
                    )
                if unit_replacements:
                    sidecar_store.bump_meta(conn, SEMANTIC_UNIT_GENERATION)
                if validate is not None and not validate():
                    raise ValueError("embedding input drifted during publication")
                own_token = self._build_token(conn)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        finally:
            conn.close()
        self._complete_publication(stored, own_token)
        return own_token

    def parent_publication_current(
        self,
        rel_path: str,
        *,
        token: tuple[int, int, int, int],
        identity: recall_space.SpaceIdentity,
        chunk_count: int,
        unit_count: int,
        parent_generation: str,
        parent_source_hash: str,
    ) -> bool:
        """Check a known exact publication without reading text or vector blobs.

        The chunk owner's existing contiguous change log proves that this
        parent's chunks have not moved since its transaction. Unit rows carry
        their deterministic generation/source hash. An absent/gapped proof is
        refused and replayed; unrelated publications do not invalidate it.
        """
        if not self.path.exists() or self.path != index_paths.sidecar_path(self.vault_root):
            return False
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            stored = recall_space.read_identity(conn, tables=_VECTOR_TABLES)
            if stored != identity and (chunk_count or unit_count or stored is not None):
                return False
            if rel_path not in self._unchanged_chunk_paths(conn, token[:3], {rel_path}):
                return False
            width = identity.dim * 4
            chunks = conn.execute(
                "SELECT COUNT(*), MIN(chunk_idx), MAX(chunk_idx), "
                "SUM(length(vector) != ?) FROM chunks WHERE file_path = ?",
                (width, rel_path),
            ).fetchone()
            if chunks[0] != chunk_count or (chunks[3] or 0) != 0:
                return False
            if chunk_count and (chunks[1], chunks[2]) != (0, chunk_count - 1):
                return False
            units = conn.execute(
                "SELECT COUNT(*), SUM(parent_generation != ? OR parent_source_hash != ? "
                "OR length(vector) != ?) FROM semantic_unit_vectors WHERE parent_path = ?",
                (parent_generation, parent_source_hash, width, rel_path),
            ).fetchone()
            record = _coverage_records(conn, [rel_path]).get(rel_path)
            return (
                units[0] == unit_count and (units[1] or 0) == 0
                and record is not None
                and record.get("parent_generation") == parent_generation
                and len(record["occurrences"]) == unit_count
            )
        finally:
            conn.close()

    def _unchanged_chunk_paths(
        self, conn: sqlite3.Connection, token: tuple[int, int, int], paths: set[str],
    ) -> set[str]:
        """Reuse the owner's contiguous path log inside the caller's snapshot."""
        current = self._build_token(conn)
        if current[0] != token[0] or current[2] != token[2] or current[1] < token[1]:
            return set()
        if current[1] == token[1]:
            return paths
        start, upto = sidecar_store.read_logged_run(conn, CHUNK_PATH_LOG)
        if start is None or start > token[1] or upto != current[1]:
            return set()
        changed = set()
        for path in paths:
            row = conn.execute(
                "SELECT generation FROM chunk_path_log WHERE file_path = ?", (path,),
            ).fetchone()
            if row is not None and int(row[0]) > token[1]:
                changed.add(path)
        return paths - changed

    def parent_publication_from_source(
        self, chunks: list[str], state: semantic_index.SemanticParentIndexState,
        *, identity: recall_space.SpaceIdentity,
    ) -> tuple[int, int, int, int] | None:
        """Recover publication authority from exact rows in one read snapshot.

        Read only this bounded parent's text and metadata, never vector blobs.
        Counts and text sizes are checked before hydrating stored text, so a
        corrupt/older large projection cannot defeat the preparation allowance.
        """
        if not self.path.exists() or self.path != index_paths.sidecar_path(self.vault_root):
            return None
        units = {row[0]: row for row in occurrence_rows(state)}
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            stored = recall_space.read_identity(conn, tables=_VECTOR_TABLES)
            if stored != identity and (chunks or units or stored is not None):
                return None
            chunk_size = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(length(CAST(chunk_text AS BLOB))), 0), "
                "COALESCE(SUM(typeof(chunk_idx) != 'integer'), 0) "
                "FROM chunks WHERE file_path = ?", (state.path,),
            ).fetchone()
            if chunk_size != (len(chunks), sum(len(text.encode("utf-8")) for text in chunks), 0):
                return None
            width = identity.dim * 4
            for number, row in enumerate(conn.execute(
                "SELECT chunk_idx, chunk_text, length(vector) FROM chunks "
                "WHERE file_path = ? ORDER BY chunk_idx", (state.path,),
            )):
                if row != (number, chunks[number], width):
                    return None
            columns = OCCURRENCE_COLUMNS
            text_size = " + ".join(
                f"COALESCE(length(CAST({column} AS BLOB)), 0)"
                for column in columns if column not in {"parser_version", "source_order"}
            )
            unit_size = conn.execute(
                f"SELECT COUNT(*), COALESCE(SUM({text_size}), 0), "
                "COALESCE(SUM(typeof(parser_version) != 'integer' OR "
                "typeof(source_order) != 'integer'), 0) "
                "FROM semantic_unit_vectors WHERE parent_path = ?", (state.path,),
            ).fetchone()
            expected_size = sum(
                len(value.encode("utf-8")) for row in units.values()
                for value in row if isinstance(value, str)
            )
            if unit_size != (len(units), expected_size, 0):
                return None
            for row in conn.execute(
                f"SELECT {', '.join(columns)}, length(vector) "
                "FROM semantic_unit_vectors WHERE parent_path = ?", (state.path,),
            ):
                if units.get(row[0]) != row[:-1] or row[-1] != width:
                    return None
            stored = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (COVERAGE_PREFIX + state.path,),
            ).fetchone()
            if stored is None or stored[0] != coverage_record(state):
                return None
            return self._build_token(conn)
        finally:
            conn.close()

    def delete_file(self, rel_path: str) -> None:
        """Remove one parent's page and semantic-unit rows if the sidecar exists."""
        self.purge_paths_if_present([rel_path])

    def purge_paths_if_present(
        self, rel_paths: list[str], *, connection_path: Path | None = None
    ) -> int:
        """Model-free, idempotent removal for paths no longer admitted to recall.

        This intentionally never calls :meth:`_connect` for an absent sidecar:
        policy changes and disabled-model writes must be able to clean stale rows
        without creating derived state.  The chunk and semantic-unit tables (and
        vec0's chunk mirror) move together under one transaction.
        """
        paths = sorted({path for path in rel_paths if path})
        target = connection_path if connection_path is not None else self.path
        if not paths or not target.exists():
            return 0
        conn = self._connect(target)
        try:
            vec_on = vec_gate(self, conn)
            with conn:
                removed = 0
                removed_paths: list[str] = []
                for rel_path in paths:
                    chunk_count = conn.execute(
                        "SELECT COUNT(*) FROM chunks WHERE file_path = ?", (rel_path,)
                    ).fetchone()[0]
                    unit_count = conn.execute(
                        "SELECT COUNT(*) FROM semantic_unit_vectors WHERE parent_path = ?",
                        (rel_path,),
                    ).fetchone()[0]
                    covered = conn.execute(
                        "SELECT 1 FROM meta WHERE key = ?", (COVERAGE_PREFIX + rel_path,),
                    ).fetchone()
                    if not chunk_count and not unit_count and covered is None:
                        continue
                    if vec_on and chunk_count:
                        self._vec.dual_delete(conn, "file_path = ?", (rel_path,))
                    conn.execute("DELETE FROM chunks WHERE file_path = ?", (rel_path,))
                    conn.execute(
                        "DELETE FROM semantic_unit_vectors WHERE parent_path = ?",
                        (rel_path,),
                    )
                    _delete_coverage(conn, [rel_path])
                    removed += 1
                    removed_paths.append(rel_path)
                if removed:
                    # ONE bump for the whole batch — hence the explicit path list:
                    # the generation delta alone could never say how many, or
                    # which, paths this retired.
                    sidecar_store.bump_generation_for_paths(
                        conn, CHUNK_PATH_LOG, removed_paths
                    )
                    sidecar_store.bump_meta(conn, SEMANTIC_UNIT_GENERATION)
                    own_epoch, own_gen, own_instance = sidecar_store.read_meta_token(conn)
        finally:
            conn.close()
        if removed:
            self._purge_cache_paths(set(paths), own_epoch, own_gen, own_instance)
        return removed

    def purge_exact_persisted_rows(
        self, values: list[str], *, connection_path: Path | None = None
    ) -> int:
        """Remove quarantined stored identities without filesystem routing."""
        return self.purge_paths_if_present(values, connection_path=connection_path)

    def _purge_cache_paths(
        self,
        paths: set[str],
        own_epoch: int,
        own_gen: int,
        own_instance: int,
    ) -> None:
        """Drop a multi-path block from a warm matrix only when contiguous."""
        with self._lock:
            cached = self._cache
            if cached is None:
                return
            if (
                own_epoch != cached.epoch
                or own_instance != cached.instance
                or own_gen != cached.generation + 1
            ):
                log.info(
                    "embedding matrix purge refused: paths=%s "
                    "own=(epoch=%d, gen=%d, instance=%d) "
                    "cached=(epoch=%d, gen=%d, instance=%d) delta=%d",
                    sorted(paths),
                    own_epoch,
                    own_gen,
                    own_instance,
                    cached.epoch,
                    cached.generation,
                    cached.instance,
                    own_gen - cached.generation,
                )
                self._cache = None
                return
            keep = [i for i, (path, _chunk) in enumerate(cached.metadata) if path not in paths]
            self._cache = _EmbCache(
                cached.epoch,
                own_gen,
                cached.instance,
                cached.mtime,
                cached.recall_policy_identity,
                [cached.metadata[i] for i in keep],
                cached.matrix[keep]
                if keep
                else np.zeros((0, cached.matrix.shape[1]), dtype=np.float32),
                cached.space,
            )

    def upsert_semantic_units(
        self,
        state: semantic_index.SemanticParentIndexState,
        vectors: np.ndarray,
        mtime: float,
    ) -> None:
        """Replace one parent's occurrence vectors and coverage in one transaction.

        An empty `vectors` publishes a parent with no occurrences: its coverage
        record still proves the empty set.
        """
        rows = self._semantic_unit_rows(state, vectors, mtime)
        conn = self._connect()
        try:
            if rows:
                self._admit(conn, np.asarray(vectors).shape[1])
            with conn:
                self._replace_occurrences(conn, state, rows)
                sidecar_store.bump_meta(conn, SEMANTIC_UNIT_GENERATION)
        finally:
            conn.close()

    def delete_semantic_units(self, parent_path: str) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "DELETE FROM semantic_unit_vectors WHERE parent_path = ?", (parent_path,)
                )
                _delete_coverage(conn, [parent_path])
                sidecar_store.bump_meta(conn, SEMANTIC_UNIT_GENERATION)
        finally:
            conn.close()

    @staticmethod
    def _replace_occurrences(
        conn: sqlite3.Connection, state: semantic_index.SemanticParentIndexState, rows: list[tuple],
    ) -> None:
        conn.execute("DELETE FROM semantic_unit_vectors WHERE parent_path = ?", (state.path,))
        conn.executemany(
            f"INSERT INTO semantic_unit_vectors({', '.join(OCCURRENCE_COLUMNS)}, vector, file_mtime) "
            f"VALUES ({', '.join('?' for _ in range(len(OCCURRENCE_COLUMNS) + 2))})",
            rows,
        )
        _write_coverage(conn, state)

    @classmethod
    def _semantic_unit_rows(
        cls,
        state: semantic_index.SemanticParentIndexState,
        vectors: np.ndarray,
        mtime: float,
    ) -> list[tuple]:
        metadata = occurrence_rows(state)
        if len(metadata) != len(vectors):
            raise ValueError(
                f"semantic-unit/vector length mismatch for {state.path}: "
                f"{len(metadata)} vs {len(vectors)}"
            )
        return [
            (*row, vectors[number].astype(np.float32).tobytes(), mtime)
            for number, row in enumerate(metadata)
        ]

    def _patch_cache(
        self,
        rel_path: str,
        new_meta: list[tuple[str, int]],
        new_vecs: np.ndarray | None,
        own_epoch: int,
        own_gen: int,
        own_instance: int,
    ) -> None:
        """Splice one file's rows into the cached matrix (copy-on-write) — ONLY
        when this write is contiguous with the CURRENT cache: `own_epoch ==
        cached.epoch AND own_instance == cached.instance AND own_gen ==
        cached.generation + 1`. On ANY mismatch, the splice is skipped ENTIRELY —
        content is NOT spliced and the label does NOT advance — leaving the cache
        exactly as it was; the resulting token mismatch heals via a full reload on
        the next `all_vectors()` (cheap enough — Phase 1 semantics).

        This gates content and label TOGETHER because splicing content whose
        label can't (yet) advance is unsafe on its own (a corrected design point:
        an earlier version of this cache spliced content unconditionally and only
        gated the label, which does not prevent corruption). Proven trace: writer
        A upserts file F (capturing generation 5) then stalls before calling this;
        writer B upserts the SAME file F (generation 6) and patches immediately —
        contiguous, so B's rows land and the label advances to 6; A then resumes
        and calls this with its OWN (now stale) generation 5 and its OLDER rows —
        if content were spliced unconditionally (as before), A's stale rows would
        overwrite B's current ones while the label still reads a plausible value,
        risking B's genuinely-current rows being replaced by A's stale ones. Never
        use `max()` on the generation either, for the same reason: it would let
        the cache claim a generation whose rows it never received.

        Builds fresh `metadata`/`matrix` and atomically swaps `self._cache`; never
        mutates the arrays a concurrent reader may be holding. Best-effort: any
        inconsistency (post-gate) drops the cache to None so the next
        `all_vectors()` does a safe full reload. Leaves a cold (`None`) cache
        cold — the next read loads.
        """
        with self._lock:
            c = self._cache
            if c is None:
                return
            if own_epoch != c.epoch or own_instance != c.instance or own_gen != c.generation + 1:
                log.info(
                    "embedding matrix patch refused: rel_path=%s "
                    "own=(epoch=%d, gen=%d, instance=%d) "
                    "cached=(epoch=%d, gen=%d, instance=%d) delta=%d",
                    rel_path,
                    own_epoch,
                    own_gen,
                    own_instance,
                    c.epoch,
                    c.generation,
                    c.instance,
                    own_gen - c.generation,
                )
                return  # not contiguous with what THIS cache holds -> never splice
            try:
                new_metadata, new_matrix = _splice_path_blocks(
                    c.metadata, c.matrix, {rel_path: (list(new_meta), new_vecs)}
                )
                self._cache = _EmbCache(
                    c.epoch,
                    own_gen,
                    c.instance,
                    c.mtime,
                    c.recall_policy_identity,
                    new_metadata,
                    new_matrix,
                    c.space,
                )
            except Exception as e:  # noqa: BLE001 — self-heal, never break a write
                log.warning("embedding matrix splice failed (%s); dropping cache", e)
                self._cache = None

    def all_vectors(self) -> tuple[list[tuple[str, int]], np.ndarray]:
        """Return `(metadata, matrix)` cached until the sidecar's write generation
        (or epoch) advances — NOT its mtime (see the class + generation-meta notes).

        metadata[i] = (file_path, chunk_idx); matrix[i] = vector. Chunk text
        is deliberately NOT here (numpy-lite — see class docstring); fetch the
        winners' texts via `_texts_for` when needed.
        """
        served = self._all_vector_snapshot()
        if served is None:
            return [], np.zeros((0, self.dim), dtype=np.float32)
        return served.metadata, served.matrix

    def _all_vector_snapshot(self) -> _EmbCache | None:
        """Keep the scored cache's proof attached to its copy-on-write arrays."""
        if not self.path.exists():
            return None
        if cloud_cell.resource_policy() == "service-v1":
            # Explicit audit consumers may read all rows transiently, but they
            # cannot turn subsequent ordinary writes into matrix replacements.
            self.unload_cache()
            loaded = self._load_all_rows()
            return loaded
        from . import recall_policy

        policy_identity = recall_policy.recall_policy_identity(self.vault_root)
        served = self._serve_resident(policy_identity)
        if served is not None:
            return served
        while True:
            loading = self._matrix_loading()
            if loading is not None:
                # Wait off the lock for the load already running, never a second one.
                loading.join()
            with self._lock:
                served = self._serve_or_catch_up_locked(policy_identity)
                if served is not None:
                    return served
                if self._matrix_loading() is not None:
                    continue
                loaded = self._full_load_locked()
                return loaded

    def _serve_resident(
        self, policy_identity: tuple[str, str], *, nonblocking: bool = False
    ) -> _EmbCache | None:
        """The cache when it serves the current publication, read without the lock."""
        # Snapshot the cache tuple ONCE: another thread may swap or null it between
        # reads. This fast path takes no lock — the common case.
        c = self._cache
        served = (
            sidecar_store.try_serve_cached(c, self.path, nonblocking=nonblocking)
            if c is not None and c.recall_policy_identity == policy_identity
            else None
        )
        if served is not None and _cache_covers_publication(served, self._identity_token, self._identity):
            self._hits += 1
            return served
        return None

    def resident_matrix(self) -> _EmbCache | None:
        """The matrix cache when it already serves the current publication, else None.

        For a reader that must not wait and must not start work, such as
        activation: no lock, no load, no background load and no catch-up. A
        cold, stale or disk-scored matrix is None, and the caller reports
        that rather than asking for more.
        """
        if not self.path.exists() or cloud_cell.resource_policy() == "service-v1":
            return None
        from . import recall_policy

        return self._serve_resident(
            recall_policy.recall_policy_identity(self.vault_root), nonblocking=True
        )

    def require_resident_matrix(self) -> _EmbCache | None:
        """The matrix that serves now, or a background load and a raise.

        A matrix that serves, or catches up within the bound, is returned.
        Otherwise this starts the one background load and raises
        `recall_space.CorpusMatrixCold`, or a plain error while a failed load
        backs off. None means no resident matrix applies (no sidecar, or the
        disk-scoring profile). A long-running service's recall serves through
        this: a whole-corpus load took minutes on a request thread.
        """
        if not self.path.exists() or cloud_cell.resource_policy() == "service-v1":
            return None
        from . import recall_policy

        policy_identity = recall_policy.recall_policy_identity(self.vault_root)
        served = self._serve_resident(policy_identity)
        if served is not None:
            return served
        if self._matrix_loading() is not None:
            raise recall_space.CorpusMatrixCold("corpus matrix is loading")
        with self._lock:
            served = self._serve_or_catch_up_locked(policy_identity)
            if served is not None:
                return served
            self._defer_matrix_load()

    def _serve_or_catch_up_locked(self, policy_identity: tuple[str, str]) -> _EmbCache | None:
        """The cache that serves the current publication, else None. Hold `self._lock`."""
        # Re-check under the lock: another thread may have loaded while we
        # waited, or the fast-path token read may have failed transiently.
        c = self._cache
        served = (
            sidecar_store.try_serve_cached(c, self.path)
            if c is not None and c.recall_policy_identity == policy_identity
            else None
        )
        if served is not None and _cache_covers_publication(served, self._identity_token, self._identity):
            self._hits += 1
            return served
        # Bounded catch-up BEFORE the full reload: a cache a couple of
        # generations behind (the common case — another instance wrote, or a
        # patch was refused as non-contiguous) is patched from the changed
        # paths' rows alone, instead of paying the O(vault) SELECT + stack.
        if c is not None and c.recall_policy_identity == policy_identity:
            try:
                with call_spans.span("embeddings.matrix_catch_up"):
                    patched = self._catch_up_cache(c)
            except Exception as e:  # noqa: BLE001 — always fall back, never raise
                log.warning(
                    "embedding matrix catch-up failed (%s); taking the full load", e
                )
                patched = None
            if patched is not None:
                self._cache = patched
                self._hits += 1
                return patched
        return None

    def _full_load_locked(self) -> _EmbCache:
        """Replace the cache with every stored row. Hold `self._lock`."""
        c = self._cache
        # Keep this call zero-argument: cache tests and production probes
        # deliberately wrap the named full-reload seam.
        with call_spans.span("embeddings.matrix_load"):
            loaded = self._load_all_rows()
        log.info(
            "embedding matrix full load: reason=%s rows=%d gen=%d epoch=%d cached_gen=%d",
            sidecar_store.reload_reason(c, loaded.epoch, loaded.generation),
            len(loaded.metadata),
            loaded.generation,
            loaded.epoch,
            c.generation if c is not None else -1,
        )
        self._cache = loaded
        self._hits += 1
        self._matrix_load_failed_at = None
        self._matrix_load_failures = 0
        return loaded

    def start_corpus_matrix_load(self) -> threading.Thread:
        """Start the one background corpus-matrix load, or return the one running."""
        with self._matrix_load_lock:
            loading = self._matrix_load
            if loading is None or not loading.is_alive():
                loading = threading.Thread(
                    target=self._load_corpus_matrix, name="exomem-matrix-load", daemon=True
                )
                loading.start()
                self._matrix_load = loading
            return loading

    def _matrix_loading(self) -> threading.Thread | None:
        with self._matrix_load_lock:
            loading = self._matrix_load
            return loading if loading is not None and loading.is_alive() else None

    def _defer_matrix_load(self) -> NoReturn:
        """Start the background load, or report that the last one failed recently."""
        failed_at = self._matrix_load_failed_at
        backoff = MATRIX_LOAD_RETRY_SECONDS * 2 ** min(self._matrix_load_failures - 1, 6)
        if failed_at is not None and time.monotonic() - failed_at < backoff:
            raise RuntimeError("the background corpus matrix load failed; retrying after a backoff")
        self.start_corpus_matrix_load()
        raise recall_space.CorpusMatrixCold("corpus matrix is loading")

    def _load_corpus_matrix(self) -> None:
        """Read every row off `self._lock`, so writers and recall never wait on the read."""
        from . import recall_policy

        # Like every reader here, never create the sidecar it would read.
        if not self.path.exists() or cloud_cell.resource_policy() == "service-v1":
            return
        started = time.perf_counter()
        try:
            policy_identity = recall_policy.recall_policy_identity(self.vault_root)
            loaded = self._load_all_rows()
            with self._lock:
                reason = sidecar_store.reload_reason(self._cache, loaded.epoch, loaded.generation)
                served = self._serve_or_catch_up_locked(policy_identity)
                if served is None:
                    self._cache = loaded
                    served = self._serve_or_catch_up_locked(policy_identity)
                if served is None:
                    # Writes outran the read and the catch-up bound: read once
                    # more under the lock, which holds the writers' cache patches.
                    served = self._full_load_locked()
        except Exception:  # noqa: BLE001 — recall reports the failure until the backoff ends
            self._matrix_load_failures += 1
            self._matrix_load_failed_at = time.monotonic()
            log.warning("background embedding matrix load failed", exc_info=True)
            return
        self._matrix_load_failed_at = None
        self._matrix_load_failures = 0
        log.info(
            "embedding matrix background load: reason=%s rows=%d gen=%d in %.0f ms",
            reason,
            len(served.metadata),
            served.generation,
            (time.perf_counter() - started) * 1000.0,
        )

    def unload_cache(self) -> bool:
        """Drop the resident chunk and unit matrices without deleting sidecar rows."""
        with self._lock:
            loaded = self._cache is not None or self._unit_cache is not None
            self._cache = None
            self._unit_cache = None
            # The mask memo pins the metadata list by strong reference, so an
            # unload that left it behind would keep those rows resident after
            # the caller asked for the memory back. Correctness never depended
            # on this — a reload builds a new list and simply misses.
            self._mask_cache = None
            return loaded

    def cache_status(self) -> dict:
        """Best-effort residency status for this index's in-memory matrices.

        `rows` counts chunk rows and `unit_rows` unit rows; `bytes` sums both matrices.
        """
        c, units = self._cache, self._unit_cache
        if c is None and units is None:
            return {"loaded": False, "rows": 0, "bytes": 0, "hits": self._hits}
        status = {
            "loaded": True,
            "hits": self._hits,
            "rows": len(c.metadata) if c is not None else 0,
            "unit_rows": len(units.block.rows) if units is not None else 0,
            "bytes": (int(c.matrix.nbytes) if c is not None else 0)
            + (int(units.block.matrix.nbytes) if units is not None else 0),
        }
        if c is not None:
            status["epoch"] = c.epoch
            status["generation"] = c.generation
        return status

    def _catch_up_cache(self, c: _EmbCache) -> _EmbCache | None:
        """Patch a slightly-stale warm cache forward from the changed paths only.

        Returns the patched cache, or None when this delta is not eligible (the
        caller then takes the full reload). Call under `self._lock` with `c` the
        cache that was just found stale and already known to match the current
        recall-policy identity.

        Everything the decision rests on — the meta token, the change log, the
        replacement rows, and the row count that cross-checks them — is read
        inside ONE explicit `BEGIN`, for exactly the reason `_load_all_rows`
        does it: python sqlite3 in autocommit gives every bare SELECT its own
        snapshot, so a split read could pair one write's generation with another
        write's rows and then label the result as current.

        What keeps an unlogged generation bump from being read as "this write
        changed nothing" is `catchup_is_eligible`'s run check, NOT the `COUNT(*)`
        below: a count cannot see an in-place re-embed of an existing chunk,
        which preserves the row count exactly. The count is a cheap secondary
        cross-check for insert/delete-shaped divergence only.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            try:
                epoch, gen, instance = sidecar_store.read_meta_token(conn)
                if recall_space.read_identity(conn, tables=_VECTOR_TABLES) != c.space:
                    return None
                run = sidecar_store.read_logged_run(conn, CHUNK_PATH_LOG)
                if not sidecar_store.catchup_is_eligible(
                    c,
                    epoch,
                    gen,
                    instance,
                    run=run,
                    max_generations=CATCHUP_MAX_GENERATIONS,
                ):
                    return None
                changed = sidecar_store.changed_paths_since(
                    conn, CHUNK_PATH_LOG, c.generation, limit=CATCHUP_MAX_PATHS
                )
                if len(changed) > CATCHUP_MAX_PATHS:
                    return None  # wider than the bound — one clean reload is cheaper
                replacements: dict[
                    str, tuple[list[tuple[str, int]], np.ndarray | None]
                ] = {}
                for path in changed:
                    rows = conn.execute(
                        "SELECT chunk_idx, vector FROM chunks "
                        "WHERE file_path = ? ORDER BY chunk_idx",
                        (path,),
                    ).fetchall()
                    key = sys.intern(path)
                    replacements[key] = (
                        [(key, idx) for idx, _blob in rows],
                        np.stack(
                            [np.frombuffer(blob, dtype=np.float32) for _idx, blob in rows],
                            axis=0,
                        )
                        if rows
                        else None,
                    )
                total_rows = int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
            finally:
                conn.rollback()  # read-only txn — release the snapshot
        finally:
            conn.close()
        metadata, matrix = _splice_path_blocks(c.metadata, c.matrix, replacements)
        if len(metadata) != total_rows:
            raise ValueError(
                f"catch-up row count disagrees with the sidecar: {len(metadata)} "
                f"spliced vs {total_rows} stored (gen {c.generation} -> {gen})"
            )
        log.info(
            "embedding matrix catch-up: reason=%s paths=%d rows=%d gen=%d epoch=%d "
            "cached_gen=%d delta=%d",
            sidecar_store.CATCHUP_REASON,
            len(replacements),
            len(metadata),
            gen,
            epoch,
            c.generation,
            gen - c.generation,
        )
        return _EmbCache(
            epoch, gen, instance, c.mtime, c.recall_policy_identity, metadata, matrix, c.space
        )

    def _load_all_rows(self, policy_identity: tuple[str, str] | None = None) -> _EmbCache:
        """Load one serving snapshot directly into its final float32 matrix."""
        with call_spans.span("embeddings.matrix_load.policy"):
            if policy_identity is None:
                from . import recall_policy

                policy_identity = recall_policy.recall_policy_identity(self.vault_root)
        with call_spans.span("embeddings.matrix_load.connect"):
            conn = self._connect()
        try:
            with call_spans.span("embeddings.matrix_load.snapshot"):
                conn.execute("BEGIN")
            try:
                with call_spans.span("embeddings.matrix_load.snapshot"):
                    snapshot_token = self._build_token(conn)
                    epoch, gen, instance = snapshot_token[:3]
                    identity = recall_space.read_identity(conn, tables=_VECTOR_TABLES)
                    width = identity.dim if identity is not None else recall_space.current_dim()
                    count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                with call_spans.span("embeddings.matrix_load.allocate"):
                    matrix = np.empty((count, width), dtype=np.float32)
                    metadata: list[tuple[str, int]] = []
                fields = {"expected_rows": count, "width_sum": width}
                with call_spans.span("embeddings.matrix_load.rows", fields):
                    try:
                        cursor = conn.execute(
                            "SELECT file_path, chunk_idx, vector FROM chunks ORDER BY file_path, chunk_idx"
                        )
                        for row, (fp, idx, blob) in enumerate(cursor):
                            if len(blob) != width * np.dtype(np.float32).itemsize:
                                raise ValueError("embedding row width differs from stored space")
                            if row >= count:
                                raise ValueError("embedding row count differs from snapshot")
                            matrix[row] = np.frombuffer(blob, dtype=np.float32)
                            metadata.append((sys.intern(fp), idx))
                        if len(metadata) != count:
                            raise ValueError("embedding row count differs from snapshot")
                    finally:
                        fields["rows"] = len(metadata)
                        fields["vector_bytes"] = len(metadata) * width * np.dtype(np.float32).itemsize
            finally:
                with call_spans.span("embeddings.matrix_load.release"):
                    conn.rollback()
        finally:
            with call_spans.span("embeddings.matrix_load.release"):
                conn.close()
        with call_spans.span("embeddings.matrix_load.finalize"):
            self._observe_identity(identity, snapshot_token)
            try:
                mtime = self.path.stat().st_mtime
            except OSError:
                mtime = 0.0
            return _EmbCache(epoch, gen, instance, mtime, policy_identity, metadata, matrix, identity)

    def search(
        self,
        query_vec: np.ndarray,
        k: int,
        *,
        allowed_paths: set[str] | None = None,
        encoded_for: Any = _UNSPECIFIED_SPACE,
        defer_cold_load: bool = False,
    ) -> list[tuple[str, int, str, float]]:
        """Top-k chunks with eager text, preserving the ordinary search API."""
        return cast(list[tuple[str, int, str, float]], self._search(
            query_vec, k, allowed_paths=allowed_paths, encoded_for=encoded_for,
            defer_cold_load=defer_cold_load,
        ))

    def select_chunks(
        self, query_vec: np.ndarray, k: int, *, allowed_paths: set[str] | None = None,
        encoded_for: Any = _UNSPECIFIED_SPACE, defer_cold_load: bool = False,
    ) -> list[tuple[str, int, str | ChunkRef, float]]:
        """Keep modern passages as references until final evidence selection.

        Generation-zero snapshots retain eager text: a cached legacy matrix
        has no durable token, and a different scorer can change tied winners.
        """
        return self._search(
            query_vec, k, allowed_paths=allowed_paths, encoded_for=encoded_for,
            defer_cold_load=defer_cold_load, defer_text=True,
        )

    def _search(
        self, query_vec: np.ndarray, k: int, *, allowed_paths: set[str] | None = None,
        encoded_for: Any = _UNSPECIFIED_SPACE, defer_cold_load: bool = False,
        defer_text: bool = False,
    ) -> list[tuple[str, int, str | ChunkRef, float]]:
        """Top-k chunk hits: list of `(file_path, chunk_idx, chunk_text, score)`.

        Backend ladder: vec0 KNN in the sidecar when available (full-precision by
        default — exact, rank-identical to the scan below; binary+rescore when
        `EXOMEM_VEC_QUANT=binary`), otherwise the in-memory numpy scan. Every vec
        failure mode falls through to the scan — search never breaks on vec0.

        Under an allowed-paths filter the scan SCORES FIRST AND MASKS SECOND:
        one BLAS pass over the whole cached matrix, then `-inf` onto the
        ineligible rows' scores. Slicing the matrix instead (`matrix[keep]`)
        fancy-indexed a fresh copy of most of a ~200 MB matrix on every single
        recall, which the matmul then had to read back — the memcpy issue #951
        measured, and the reason a semantic recall cost ~10x a keyword one with
        the GPU idle. Masking is behaviour-identical rather than approximate: an
        ineligible row scores `-inf` and `k_eff` is clamped to the eligible
        count, so `argpartition` provably cannot reach a masked row.
        """
        if cloud_cell.resource_policy() == "service-v1":
            # SQLite row fetches release the GIL; cooperative bulk graph work
            # must yield during this read-only scan, which never waits on it.
            # Batched advisory scoring marks only its actual foreground caller.
            with foreground_priority.foreground():
                return self._disk_score(
                    [query_vec], k, admits=lambda path: allowed_paths is None or path in allowed_paths,
                    encoded_for=encoded_for, hydrate=True, defer_text=defer_text,
                )[0]
        if allowed_paths is None:
            vec_hits = (
                self._vec_search(query_vec, k, defer_text=True, encoded_for=encoded_for)
                if defer_text else self._vec_search(query_vec, k)
            )
            if vec_hits is not None:
                return vec_hits
        resident = self.require_resident_matrix() if defer_cold_load else None
        if defer_text:
            resident = resident if resident is not None else self._all_vector_snapshot()
            if resident is None:
                return []
            metadata, matrix = resident.metadata, resident.matrix
        else:
            # Ordinary search retains the public matrix-provider seam. Only
            # deferred passages need the immutable cache object as their proof.
            metadata, matrix = (
                (resident.metadata, resident.matrix) if resident is not None else self.all_vectors()
            )
        if not metadata:
            return []
        if defer_text and encoded_for is not _UNSPECIFIED_SPACE:
            recall_space.require_same_space(SimpleNamespace(identity=resident.space), encoded_for, query_vec)
        mask: np.ndarray | None = None
        # The ceiling on how many rows may be returned. Under a filter it is the
        # ELIGIBLE count, never the row count: `argpartition` over the full score
        # array will return `k` rows whatever the mask says, so an unclamped
        # `k_eff` pads the answer with masked `-inf` rows as soon as fewer than
        # `k` survive the filter — and returns a whole top-k when none do.
        k_ceiling = len(metadata)
        if allowed_paths is not None:
            mask, k_ceiling = self._eligibility_mask(metadata, allowed_paths)
        k_eff = min(k, k_ceiling)
        if k_eff <= 0:
            return []
        # query_vec is (768,) normalized; matrix is (N, 768) normalized.
        scores = matrix @ query_vec.astype(np.float32, copy=False)
        if mask is not None:
            scores = np.where(mask, scores, -np.inf)
        # argpartition is O(N), then sort the top-k slice.
        top_idx = np.argpartition(-scores, k_eff - 1)[:k_eff]
        if mask is not None and not bool(mask[top_idx].all()):
            # An ineligible row won a slot, which masking alone cannot prevent:
            # `-(-inf)` is `+inf`, and numpy orders NaN ABOVE `+inf`, so when the
            # query embeds to NaN (a zero-norm or broken vector) every eligible
            # score is NaN and the masked rows partition first. Reachable only
            # with non-finite scores, but eligibility is a governance boundary
            # rather than a ranking preference, so it must not depend on
            # arithmetic holding. Fall back to selecting among the eligible rows
            # only — the pre-#951 computation exactly, on the rows it would have
            # had — which restores both the row and its true score.
            eligible_idx = np.flatnonzero(mask)
            sub = scores[eligible_idx]
            top_idx = eligible_idx[np.argpartition(-sub, k_eff - 1)[:k_eff]]
        top_idx = top_idx[np.argsort(-scores[top_idx])]
        top = [(metadata[i][0], metadata[i][1], float(scores[i])) for i in top_idx]
        if defer_text and resident.generation > 0:
            proof = ChunkProof(self, (resident.epoch, resident.generation, resident.instance), resident.space)
            return [(fp, ci, ChunkRef(proof, fp, ci), score) for fp, ci, score in top]
        # numpy-lite: hydrate only the winners' texts (PK point-lookups).
        try:
            texts = self._texts_for([(fp, ci) for fp, ci, _ in top])
        except Exception as e:  # noqa: BLE001 — text hydration must never break search
            log.warning("chunk-text fetch failed (%s); returning hits without text", e)
            texts = {}
        return [(fp, ci, texts.get((fp, ci), ""), score) for fp, ci, score in top]

    def search_many(
        self,
        query_vecs: np.ndarray,
        k: int,
        *,
        admits: Callable[[str], bool],
        encoded_for: Any = _UNSPECIFIED_SPACE,
        defer_cold_load: bool = False,
    ) -> list[list[tuple[str, int, float]]]:
        """Top-k eligible chunk rows for each query row: `(file_path, chunk_idx, score)`.

        Each list is what `search(query, k, allowed_paths=A)` returns for that
        row, up to the choice and order among exactly tied scores, where
        `admits(file_path)` is membership in `A`: the `k` best rows whose file
        is admitted, best first, fewer only when fewer are admitted. Two things
        make it cheaper for a caller holding many queries, which is the write
        advisory scoring every chunk of a draft. The matrix is read once per
        block of `SEARCH_MANY_BLOCK` queries, in one product for the block,
        rather than once per query, and no chunk text is hydrated. And
        eligibility is asked only of files whose rows reach a query's candidate
        window, each file once, so the caller need not enumerate its whole
        eligible set to probe a few dozen of them.

        Exact, not approximate: a query's window holds its highest-scoring rows,
        so the first `k` admitted rows in window order are its top `k` admitted
        rows overall; the window widens until `k` are found or every row has
        been considered. Scores agree with `search` to the BLAS kernel wobble
        the #951 note measured, and a non-finite score sorts last exactly as
        `search`'s guarded selection leaves it.

        `defer_cold_load` behaves as in `search`: a cold matrix starts the one
        background load and raises `recall_space.CorpusMatrixCold`.
        """
        if cloud_cell.resource_policy() == "service-v1":
            # An advisory may be inline in a save or receipt-owned background
            # work. Prioritize only this vault's foreground caller, and only
            # the read-only scan: never a model wait or a component drain.
            with (
                foreground_priority.foreground()
                if foreground_activity.foreground_active_on_this_thread(self.vault_root)
                else contextlib.nullcontext()
            ):
                return [
                    [(path, chunk, score) for path, chunk, _text, score in hits]
                    for hits in self._disk_score(query_vecs, k, admits=admits, encoded_for=encoded_for)
                ]
        resident = self.require_resident_matrix() if defer_cold_load else None
        metadata, matrix = (
            (resident.metadata, resident.matrix) if resident is not None else self.all_vectors()
        )
        queries = np.asarray(query_vecs, dtype=np.float32).reshape(-1, matrix.shape[1])
        if not len(queries):
            return []
        if not metadata or k <= 0:
            return [[] for _ in range(len(queries))]
        verdicts: dict[str, bool] = {}

        def admitted(row: int) -> bool:
            file_path = metadata[row][0]
            verdict = verdicts.get(file_path)
            if verdict is None:
                verdict = verdicts[file_path] = bool(admits(file_path))
            return verdict

        answers: list[list[tuple[str, int, float]]] = []
        total = len(metadata)
        for start in range(0, len(queries), SEARCH_MANY_BLOCK):
            # `matrix @ q.T`, not `q @ matrix.T`: the same scores, but with the
            # tall matrix leading OpenBLAS ran a 64-query block ~1.8x faster
            # (measured at 58k rows under a 2-CPU quota). Rows of the transposed
            # view are strided; `_top_admitted` reads each one once.
            block = (matrix @ queries[start : start + SEARCH_MANY_BLOCK].T).T
            answers.extend(_top_admitted(row, k, total, admitted, metadata) for row in block)
            # Released before the next product, so two blocks are never alive at once.
            del block
        return answers

    def chunk_count(self) -> int:
        """Count stored chunk rows without warming their vectors."""
        if not self.path.exists():
            return 0
        conn = self._connect()
        try:
            return int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
        finally:
            conn.close()

    def _disk_score(
        self, query_vecs, k: int, *, admits: Callable[[str], bool],
        encoded_for: Any = _UNSPECIFIED_SPACE, hydrate: bool = False,
        defer_text: bool = False,
    ) -> list[list[tuple[str, int, str | ChunkRef, float]]]:
        """Exact eligible top-k with bounded vectors and one scoring snapshot."""
        if not self.path.exists():
            return [[] for _ in query_vecs]
        self.unload_cache()
        # Fixed phase names form the disk-scoring instrumentation protocol.
        with call_spans.span("embeddings.disk_score.connect"):
            conn = self._connect()
        try:
            with call_spans.span("embeddings.disk_score.snapshot"):
                conn.execute("BEGIN")
                token = self._build_token(conn)
                identity = recall_space.read_identity(conn, tables=_VECTOR_TABLES)
                width = identity.dim if identity is not None else recall_space.current_dim()
                queries = np.asarray(query_vecs, dtype=np.float32).reshape(-1, width)
                if encoded_for is not _UNSPECIFIED_SPACE and len(queries):
                    recall_space.require_same_space(SimpleNamespace(identity=identity), encoded_for, queries[0])
                self._observe_identity(identity, token)
                proof = ChunkProof(self, token[:3], identity) if defer_text and token[1] > 0 else None
            answers: list[list[tuple[str, int, float]]] = [[] for _ in queries]
            if k <= 0:
                return [[] for _ in queries]
            for start in range(0, len(queries), SEARCH_MANY_BLOCK):
                with call_spans.span("embeddings.disk_score.fetch"):
                    # Physical order avoids a table lookup per secondary-index row.
                    cursor = conn.execute(
                        "SELECT file_path, chunk_idx, vector FROM chunks ORDER BY rowid"
                    )
                while True:
                    with call_spans.span("embeddings.disk_score.fetch"):
                        rows = cursor.fetchmany(self.DISK_BLOCK_ROWS)
                    if not rows:
                        break
                    fields = {"rows": 0, "vector_bytes": 0}
                    with call_spans.span("embeddings.disk_score.decode", fields):
                        matrix = np.empty((len(rows), width), dtype=np.float32)
                        metadata = []
                        for n, (path, chunk, blob) in enumerate(rows):
                            if not isinstance(blob, bytes) or len(blob) != width * 4:
                                raise ValueError("embedding row width differs from stored space")
                            matrix[n] = np.frombuffer(blob, dtype=np.float32)
                            metadata.append((path, chunk))
                            fields["rows"] += 1
                            fields["vector_bytes"] += len(blob)
                    with call_spans.span("embeddings.disk_score.score"):
                        block = (matrix @ queries[start:start + SEARCH_MANY_BLOCK].T).T
                    with call_spans.span("embeddings.disk_score.select"):
                        # Only this block's candidate verdicts live across queries.
                        verdicts: dict[str, bool] = {}
                        def admitted(row: int, metadata=metadata, verdicts=verdicts) -> bool:
                            path = metadata[row][0]
                            if path not in verdicts:
                                verdicts[path] = bool(admits(path))
                            return verdicts[path]
                        for n, scores in enumerate(block, start):
                            winners = _top_admitted(scores, min(k, len(rows)), len(rows), admitted, metadata)
                            merged = answers[n] + winners
                            # Stable sort preserves the existing score/tie contract.
                            merged.sort(key=lambda hit: (np.isnan(hit[2]), -hit[2]))
                            answers[n] = merged[:k]
                    del matrix, block, rows
            with call_spans.span("embeddings.disk_score.hydrate"):
                result = []
                for hits in answers:
                    hydrated = []
                    for path, chunk, score in hits:
                        text: str | ChunkRef = ""
                        if proof is not None:
                            text = ChunkRef(proof, path, chunk)
                        elif hydrate:
                            text = conn.execute(
                                "SELECT chunk_text FROM chunks WHERE file_path=? AND chunk_idx=?",
                                (path, chunk),
                            ).fetchone()[0]
                        hydrated.append((path, chunk, text, score))
                    result.append(hydrated)
            return result
        finally:
            with call_spans.span("embeddings.disk_score.release"):
                conn.rollback()
                conn.close()

    def pairwise_current_chunks(
        self, wanted: dict[str, list[str]],
    ) -> tuple[dict[frozenset[str], float], set[str]]:
        """Exact selected-parent coverage and blocked cosine maxima, no corpus cache."""
        if not self.path.exists():
            return {}, set()
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            identity = recall_space.read_identity(conn, tables=_VECTOR_TABLES)
            width = identity.dim if identity is not None else recall_space.current_dim()
            covered: set[str] = set()
            for path, chunks in wanted.items():
                count = 0
                exact = True
                for chunk, text, size in conn.execute(
                    "SELECT chunk_idx, chunk_text, length(vector) FROM chunks "
                    "WHERE file_path=? ORDER BY chunk_idx", (path,),
                ):
                    if count >= len(chunks) or chunk != count or text != chunks[count] or size != width * 4:
                        exact = False
                        break
                    count += 1
                if exact and count == len(chunks) and count:
                    covered.add(path)

            def blocks(path: str):
                cursor = conn.execute(
                    "SELECT vector FROM chunks WHERE file_path=? ORDER BY chunk_idx", (path,),
                )
                while rows := cursor.fetchmany(self.PAIRWISE_BLOCK_ROWS):
                    matrix = np.empty((len(rows), width), dtype=np.float32)
                    for n, (blob,) in enumerate(rows):
                        matrix[n] = np.frombuffer(blob, dtype=np.float32)
                    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
                    matrix /= np.maximum(norms, 1e-12)
                    yield matrix

            best: dict[frozenset[str], float] = {}
            names = sorted(covered)
            for n, a in enumerate(names):
                for b in names[n + 1:]:
                    maximum = -np.inf
                    for first in blocks(a):
                        for second in blocks(b):
                            maximum = np.maximum(maximum, (first @ second.T).max())
                    best[frozenset((a, b))] = float(maximum)
            return best, covered
        finally:
            conn.rollback()
            conn.close()

    def _eligibility_mask(
        self, metadata: list[tuple[str, int]], allowed_paths: AbstractSet[str]
    ) -> tuple[np.ndarray, int]:
        """`(row_mask, eligible_count)` for `allowed_paths` over `metadata`'s rows.

        Memoized in a single slot (see `_MaskCache` for why the key is the
        metadata list's identity plus the allowed set's CONTENT). `allowed_paths`
        is stable across a session at a given scope, so only the first query
        after a matrix rebuild or a scope change pays the membership pass; the
        rest pay one frozenset compare instead of a `len(metadata)`-iteration
        Python loop that ran on every recall.

        Lock-free on purpose, like `all_vectors()`'s fast path. The slot holds
        one immutable NamedTuple, so a concurrent reader sees either the old
        entry or the new one and never a torn pair, and it re-validates whatever
        it read before trusting it. Two threads racing at different scopes both
        compute a correct mask and one simply wins the slot; the only loss is a
        recomputation. An `unload_cache()` that interleaves with the assignment
        below can leave one memo pinned — the next unload or rebuild clears it,
        and no answer is affected, which is not worth putting a lock on the hot
        path for.
        """
        key = frozenset(allowed_paths)
        cached = self._mask_cache
        if (
            cached is not None
            and cached.metadata is metadata
            and cached.allowed_paths == key
        ):
            return cached.mask, cached.eligible
        # Build from `key`, NOT from `allowed_paths`. The caller's set is mutable
        # and callers do mutate it, so reading it a second time here can cache a
        # mask under a key that does not describe it: snapshot {a}, another thread
        # adds b, the mask admits b, and the entry is filed under {a}. Restoring
        # the set to {a} then serves that stale mask for the rest of the session.
        # Membership on the frozenset costs the same and closes the window.
        mask = np.fromiter(
            (path in key for path, _chunk in metadata),
            dtype=bool,
            count=len(metadata),
        )
        eligible = int(np.count_nonzero(mask))
        self._mask_cache = _MaskCache(metadata, key, mask, eligible)
        return mask, eligible

    def search_semantic_units(
        self,
        query_vec: np.ndarray,
        k: int,
        *,
        allowed_unit_refs: set[str] | None = None,
        allowed_parent_paths: set[str] | None = None,
        validate: bool = True,
        incomplete_out: list[str] | None = None,
        encoded_for: recall_space.SpaceIdentity | None | object = _UNSPECIFIED_SPACE,
    ) -> list[SemanticUnitVectorHit]:
        """Rank selected units from neutral rows and one coverage snapshot.

        Admission precedes scoring; selected suppression, reference filtering and
        deduplication precede result and validation limits. Raw windows widen until
        their omitted-score bound proves the selected prefix, including cutoff ties.
        """
        if k <= 0 or not self.path.exists():
            return []
        if allowed_unit_refs is not None and not allowed_unit_refs:
            return []
        if allowed_parent_paths is not None and not allowed_parent_paths:
            return []
        from .governance import egress

        refs = None if allowed_unit_refs is None else frozenset(allowed_unit_refs)
        paths = None if allowed_parent_paths is None else frozenset(allowed_parent_paths)
        keep = egress.page_release_filter(self.vault_root)

        def allowed(path: str) -> bool:
            return (paths is None or path in paths) and (keep is None or bool(keep(path)))

        filtered = refs is not None or paths is not None
        query = query_vec.astype(np.float32, copy=False)
        window = max(k * 4, k + 32) if validate else k
        raw_window = window
        freshness_by_stamp: dict[tuple[str, str, str, int], bool] = {}
        ranked: list[SemanticUnitVectorHit] = []
        walked = 0
        with self._unit_blocks(allowed) as (blocks, conn, coverage, unproved, identity):
            if encoded_for is not _UNSPECIFIED_SPACE:
                recall_space.require_same_space(
                    SimpleNamespace(identity=identity), encoded_for, query
                )
            parents = semantic_index.AdmittedParents(
                lambda wanted: _coverage_records(conn, wanted),
                allowed=allowed,
                interpretations=semantic_index.Interpretations(self.vault_root),
            )
            while True:
                ordered, bound, complete = _unit_top(
                    blocks, query, window, raw_window, parents, refs
                )
                if not complete and (len(ordered) < window or not ordered[-1][0] >= bound):
                    raw_window *= 4
                    continue
                for score, row in ordered[walked:]:
                    unit_ref, parent_path, generation, source_hash, parser_version = row
                    if validate:
                        stamp = (parent_path, generation, source_hash, parser_version)
                        accepted = freshness_by_stamp.get(stamp)
                        if accepted is None:
                            accepted = semantic_index.validate_parent_record(
                                self.vault_root,
                                parent_path=parent_path,
                                parent_generation_value=generation,
                                parent_source_hash=source_hash,
                                parser_version=parser_version,
                            ).current
                            freshness_by_stamp[stamp] = accepted
                        if not accepted:
                            continue
                    ranked.append(
                        SemanticUnitVectorHit(
                            unit_ref, parent_path, generation, source_hash, parser_version, score
                        )
                    )
                    if len(ranked) == k:
                        break
                if (
                    len(ranked) == k
                    or not (validate and filtered)
                    or (complete and len(ordered) < window)
                ):
                    break
                walked = len(ordered)
                window *= 4
                raw_window = max(raw_window, window)
            if incomplete_out is not None:
                owed = self._coverage_owed()
                unpublished = (path for path in paths or () if path not in coverage and owed(path))
                witness = next(
                    chain(
                        sorted(parents.unavailable),
                        (
                            path
                            for path in chain(sorted(unproved), unpublished)
                            if parents.allowed(path)
                        ),
                    ),
                    None,
                )
                if witness is not None:
                    incomplete_out.append(witness)
        return ranked

    def _coverage_owed(self) -> Callable[[str], bool]:
        """Whether a publication writes a page a record: the rebuild walk's own rules."""
        from . import access

        walked = index_paths.index_markdown_admitter(self.vault_root)
        return lambda rel_path: (
            index_paths.is_embeddable_path(Path(rel_path))
            and (walked is None or walked(rel_path))
            and access.is_indexable(self.vault_root, rel_path)
        )

    @contextlib.contextmanager
    def _unit_blocks(self, allowed: Callable[[str], bool]) -> Iterator[tuple]:
        """Rows, compact coverage and lazy summaries share one SQLite read snapshot.

        Local queries reuse a neutral resident matrix only under the exact snapshot
        token. `service-v1` rescans ordered blocks and retains no corpus matrix.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            token = self._build_token(conn)
            identity = recall_space.read_identity(conn, tables=_VECTOR_TABLES)
            self._observe_identity(identity, token)
            width = identity.dim if identity is not None else recall_space.current_dim()
            if cloud_cell.resource_policy() != "service-v1":
                cached = self._unit_vectors(conn, token, width)
                source = ((cached.block, _unit_mask(cached.block, allowed)),)
                yield lambda: source, conn, cached.coverage, cached.unproved, identity
                return
            coverage, unproved = _unit_coverage(conn, width)

            def blocks() -> Iterator[tuple[_UnitBlock, np.ndarray]]:
                cursor = conn.execute(
                    f"SELECT {_UNIT_COLUMNS} FROM semantic_unit_vectors ORDER BY rowid"
                )
                while rows := cursor.fetchmany(self.DISK_BLOCK_ROWS):
                    block = _unit_block(rows, len(rows), width)
                    yield block, _unit_mask(block, allowed)

            yield blocks, conn, coverage, unproved, identity
        finally:
            conn.rollback()
            conn.close()

    def _unit_vectors(
        self, conn: sqlite3.Connection, token: tuple[int, int, int, int], width: int
    ) -> _UnitCache:
        """Reuse a neutral matrix and proof only at this read transaction's token."""
        epoch, _chunk_generation, instance, generation = token

        def current(cached: _UnitCache | None) -> bool:
            return cached is not None and (
                cached.epoch,
                cached.generation,
                cached.instance,
                cached.width,
            ) == (epoch, generation, instance, width)

        served = self._unit_cache
        if not current(served):
            with self._unit_lock:
                served = self._unit_cache
                if not current(served):
                    # Release this lane's stale matrix before allocating its replacement.
                    served = self._unit_cache = None
                    with call_spans.span("embeddings.unit_matrix_load"):
                        served = self._unit_cache = self._load_unit_rows(conn, token, width)
                    log.info(
                        "semantic-unit matrix full load: rows=%d gen=%d epoch=%d",
                        len(served.block.rows),
                        generation,
                        epoch,
                    )
        self._hits += 1
        return served

    def _load_unit_rows(
        self, conn: sqlite3.Connection, token: tuple[int, int, int, int], width: int
    ) -> _UnitCache:
        """Load neutral vectors and their compact structural proof from one snapshot."""
        count = conn.execute("SELECT COUNT(*) FROM semantic_unit_vectors").fetchone()[0]
        block = _unit_block(
            conn.execute(f"SELECT {_UNIT_COLUMNS} FROM semantic_unit_vectors ORDER BY rowid"),
            count,
            width,
        )
        coverage, unproved = _unit_coverage(conn, width)
        epoch, _chunk_generation, instance, generation = token
        return _UnitCache(epoch, generation, instance, width, block, coverage, unproved)

    def _texts_for(
        self, pairs: list[tuple[str, int]], *, proof: ChunkProof | None = None,
    ) -> dict[tuple[str, int], str]:
        """chunk_text for `(file_path, chunk_idx)` pairs — search's top-k only.

        The in-memory cache holds no chunk text (numpy-lite), so the numpy
        rung hydrates its winners here: point-lookups on the table's
        `(file_path, chunk_idx)` PRIMARY KEY, batched to stay far under
        SQLite's bound-variable cap.
        """
        out: dict[tuple[str, int], str] = {}
        if not pairs:
            return out
        conn = self._connect()
        try:
            if proof is not None:
                conn.execute("BEGIN")
                if (
                    proof.index is not self or proof.token[1] <= 0
                    or recall_space.read_identity(conn, tables=_VECTOR_TABLES) != proof.space
                ):
                    return {}
                unchanged = self._unchanged_chunk_paths(conn, proof.token, {fp for fp, _ci in pairs})
                pairs = [(fp, ci) for fp, ci in pairs if fp in unchanged]
            batch_size = 150  # 2 bound params per pair
            for s in range(0, len(pairs), batch_size):
                batch = pairs[s : s + batch_size]
                where = " OR ".join("(file_path = ? AND chunk_idx = ?)" for _ in batch)
                params: list = []
                for fp, ci in batch:
                    params.extend((fp, ci))
                rows = conn.execute(
                    f"SELECT file_path, chunk_idx, chunk_text FROM chunks WHERE {where}",
                    params,
                ).fetchall()
                for fp, ci, txt in rows:
                    out[(fp, ci)] = txt
        finally:
            conn.close()
        return out

    def stored_chunks_for(self, rel_path: str) -> tuple[list[str], float | None]:
        """One page's published chunk texts in index order, and the file mtime
        the embedding pass stamped on them.

        `([], None)` when the sidecar or the page's rows are absent, or the rows
        are not a contiguous `0..n-1` run (a partially replaced generation is not
        a chunking anyone cut). A reader that must not re-derive a page's
        chunking -- the context pack, for a media transcript whose chunking is
        an encode -- compares the mtime with the file it holds and takes these
        texts as the page's chunking when they match. One primary-key range
        read; never creates the sidecar.
        """
        if not self.path.exists():
            return [], None
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT chunk_idx, chunk_text, file_mtime FROM chunks "
                "WHERE file_path = ? ORDER BY chunk_idx",
                (rel_path,),
            ).fetchall()
        finally:
            conn.close()
        if not rows or [int(idx) for idx, _text, _mtime in rows] != list(range(len(rows))):
            return [], None
        mtimes = [float(mtime) for _idx, _text, mtime in rows if mtime is not None]
        if len(mtimes) != len(rows):
            return [], None
        return [str(text) for _idx, text, _mtime in rows], max(mtimes)

    def stored_text_vectors(
        self, rel_path: str, *, max_bytes: int | None = None
    ) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
        """One page's published chunk and unit vectors keyed by exact text."""
        chunks, units, _space = self.stored_text_vectors_with_space(rel_path, max_bytes=max_bytes)
        return chunks, units

    def stored_text_vectors_with_space(
        self, rel_path: str, *, max_bytes: int | None = None
    ) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], recall_space.SpaceIdentity | None]:
        """One page's published vectors keyed by the exact text each was encoded
        from, and their space identity, all from one read transaction.

        A write that changes one chunk of a long page takes the rest from here
        instead of encoding them again. A row is offered only when its blob is
        one full float32 vector at the sidecar's width; anything else is left
        out, so the caller encodes that text. Reuse assumes what every search
        over this sidecar already assumes -- one encoder wrote all of it, which
        the sidecar's vector-space record enforces. Two primary-key range reads
        on one connection; never creates the sidecar.
        """
        if not self.path.exists():
            return {}, {}, None
        conn = self._connect()
        try:
            conn.execute("BEGIN", ())
            space = recall_space.read_identity(conn, tables=_VECTOR_TABLES)
            if max_bytes is not None:
                # Blob + copied array, decoded strings and per-row containers.
                # Check both projections in the same snapshot before fetchall;
                # a formerly huge parent can now be a tiny replacement.
                reuse_bytes = conn.execute(
                    "SELECT COALESCE(SUM(2 * length(vector) + "
                    "4 * length(CAST(chunk_text AS BLOB)) + 512), 0) "
                    "FROM chunks WHERE file_path = ?",
                    (rel_path,),
                ).fetchone()[0]
                reuse_bytes += conn.execute(
                    "SELECT COALESCE(SUM(2 * length(vector) + "
                    "4 * length(CAST(content AS BLOB)) + 512), 0) "
                    "FROM semantic_unit_vectors WHERE parent_path = ?",
                    (rel_path,),
                ).fetchone()[0]
                if reuse_bytes > max_bytes:
                    return {}, {}, space
            chunk_rows = conn.execute(
                "SELECT chunk_text, vector FROM chunks WHERE file_path = ?",
                (rel_path,),
            ).fetchall()
            unit_rows = conn.execute(
                "SELECT content, vector FROM semantic_unit_vectors WHERE parent_path = ?",
                (rel_path,),
            ).fetchall()
        finally:
            conn.close()
        dim = space.dim if space is not None else recall_space.current_dim()
        width = dim * np.dtype(np.float32).itemsize

        def keyed(rows: list[tuple[Any, Any]]) -> dict[str, np.ndarray]:
            return {
                str(text): np.frombuffer(blob, dtype=np.float32).copy()
                for text, blob in rows
                if isinstance(blob, (bytes, memoryview)) and len(blob) == width
            }

        return keyed(chunk_rows), keyed(unit_rows), space

    def _vec_search(
        self, query_vec: np.ndarray, k: int, *, defer_text: bool = False,
        encoded_for: Any = _UNSPECIFIED_SPACE,
    ) -> list[tuple[str, int, str | ChunkRef, float]] | None:
        """vec0 KNN, or None when the backend can't serve (the scan takes over).

        Never creates the sidecar file on a read path (a missing sidecar keeps the
        historical `[]`-via-scan semantics), and never raises: a runtime vec
        failure logs, retires vec for this instance, and returns None.
        """
        if self._vec_failed or vecstore.backend() == "numpy" or vecstore.load_failed():
            return None
        if not self.path.exists():
            return None
        try:
            conn = self._connect()
            try:
                if not vec_gate(self, conn):
                    return None
                # The gate may commit while synchronizing the mirror. Score,
                # rescore and map rowids only after it, in one read snapshot.
                conn.execute("BEGIN")
                proof = None
                if defer_text:
                    token = self._build_token(conn)
                    identity = recall_space.read_identity(conn, tables=_VECTOR_TABLES)
                    if encoded_for is not _UNSPECIFIED_SPACE:
                        recall_space.require_same_space(SimpleNamespace(identity=identity), encoded_for, query_vec)
                    if token[1] > 0:
                        proof = ChunkProof(self, token[:3], identity)
                quant = vecstore.quant_mode() == "binary"
                pairs = self._vec.knn(conn, query_vec, k, quant=quant)
                if not pairs:
                    return []
                ids = [rid for rid, _ in pairs]
                placeholders = ",".join("?" * len(ids))
                text_column = "NULL" if proof is not None else "chunk_text"
                rows = conn.execute(
                    f"SELECT rowid, file_path, chunk_idx, {text_column} FROM chunks "
                    f"WHERE rowid IN ({placeholders})",
                    ids,
                ).fetchall()
                by_id = {r[0]: r for r in rows}
                return [
                    (by_id[rid][1], by_id[rid][2],
                     ChunkRef(proof, by_id[rid][1], by_id[rid][2]) if proof is not None else by_id[rid][3], score)
                    for rid, score in pairs
                    if rid in by_id
                ]
            finally:
                conn.close()
        except recall_space.VectorSpaceMismatch:
            raise
        except Exception as e:  # noqa: BLE001 — vec failure must never break search
            log.warning(
                "vec search failed for %s (%s); falling back to the in-memory scan",
                self.path,
                e,
            )
            self._vec_failed = True
            return None

    def file_mtimes(self, paths: AbstractSet[str] | None = None) -> dict[str, float]:
        """Map each indexed `file_path` → its max stored `file_mtime` (one query).

        The idempotency oracle for `index_incremental`: a file whose on-disk mtime
        does not exceed this value is already current in the sidecar and is skipped.
        `paths` limits the read to those files, for a caller that checks a few.
        Empty dict when the sidecar has not been created yet.
        """
        if not self.path.exists():
            return {}
        conn = self._connect()
        try:
            if paths is None:
                rows = conn.execute(
                    "SELECT file_path, MAX(file_mtime) FROM chunks GROUP BY file_path"
                ).fetchall()
            else:
                wanted = sorted(paths)
                rows = []
                for start in range(0, len(wanted), _PATHS_PER_READ):
                    batch = wanted[start : start + _PATHS_PER_READ]
                    rows += conn.execute(
                        "SELECT file_path, MAX(file_mtime) FROM chunks "
                        f"WHERE file_path IN ({','.join('?' * len(batch))}) GROUP BY file_path",
                        batch,
                    ).fetchall()
        finally:
            conn.close()
        return {r[0]: r[1] for r in rows if isinstance(r[0], str) and r[1] is not None}

    def semantic_unit_parent_states(
        self,
    ) -> dict[str, tuple[frozenset[str], frozenset[str]]]:
        """Stored generations and occurrence keys per parent, for incremental parity."""
        if not self.path.exists():
            return {}
        conn = self._connect()
        try:
            return stored_parent_states(conn)
        finally:
            conn.close()

    def all_semantic_unit_vectors(
        self, *, batch_size: int = SEMANTIC_UNIT_READ_BATCH
    ) -> dict[str, list[SemanticUnitVectorRow]]:
        """Every stored occurrence vector, grouped by parent path, in ONE corpus read.

        The audit's semantic scope-divergence sensor needs a page's unit geometry,
        and nothing here could supply it: `all_vectors()` is the CHUNK matrix
        (`metadata[i] = (file_path, chunk_idx)`), and `search_semantic_units` is a
        kNN query whose hit type carries a cosine but no vector. This is the
        missing bulk read, and it is deliberately the ONLY one — a per-page
        `WHERE parent_path = ?` across a sweep is the shape this exists to prevent.
        Rows carry occurrence keys; the caller maps them to the units of the
        parse it holds.

        Read-only, and NOT wired into `_cache`: that cache is keyed by
        `(file_path, chunk_idx)` and patched by chunk-path deltas, so admitting
        unit rows would either corrupt its splice arithmetic or silently serve a
        stale generation. A sweep pays one honest load instead.

        Paginated by the table's own primary key `(parent_path, unit_key)` so a
        large corpus never materialises one unbounded result set.

        A row whose blob is not a readable full-width vector is DROPPED, and that
        includes a blob whose length is not a whole number of float32s —
        `np.frombuffer` raises on those before any shape check could run, and an
        escaping raise would cost every OTHER page in the corpus its judgment
        rather than just the corrupt row. Rebuildable derived data never fails a
        sweep closed.
        """
        if not self.path.exists():
            return {}
        limit = max(1, int(batch_size))
        grouped: dict[str, list[SemanticUnitVectorRow]] = {}
        conn = self._connect()
        try:
            cursor = ("", "")
            while True:
                rows = conn.execute(
                    "SELECT parent_path, unit_key, source_order, vector, parent_generation "
                    "FROM semantic_unit_vectors WHERE (parent_path, unit_key) > (?, ?) "
                    "ORDER BY parent_path, unit_key LIMIT ?",
                    (*cursor, limit),
                ).fetchall()
                if not rows:
                    break
                for parent_path, unit_key, source_order, blob, generation in rows:
                    try:
                        vector = np.frombuffer(blob, dtype=np.float32)
                    except (ValueError, TypeError):
                        # Truncated or non-buffer blob: this row is unreadable, the
                        # rest of the corpus is not.
                        continue
                    if vector.shape != (self.dim,):
                        continue
                    grouped.setdefault(str(parent_path), []).append(
                        SemanticUnitVectorRow(
                            str(unit_key), int(source_order), vector, str(generation)
                        )
                    )
                cursor = (str(rows[-1][0]), str(rows[-1][1]))
                if len(rows) < limit:
                    break
        finally:
            conn.close()
        for unit_rows in grouped.values():
            unit_rows.sort(key=lambda row: (row.source_order, row.unit_key))
        return grouped

    def _projected_source_snapshot(
        self,
    ) -> tuple[tuple[str, str], tuple[tuple[str, tuple[int, int, int]], ...]]:
        """Current ordinary-recall source identity for a rebuild publication.

        Rebuilds are staged from human-owned files.  This compact snapshot binds
        the staged vectors to exactly the projected corpus and local policy that
        was scanned, so a direct edit, create/delete, or access transition can
        refuse publication instead of replacing a still-valid sidecar with a
        mixed-generation corpus.
        """
        from . import freshness, recall_policy

        rows: list[tuple[str, tuple[int, int, int]]] = []
        for path in index_paths.iter_index_markdown(self.vault_root):
            rel = index_paths.rel_to_vault(self.vault_root, path)
            if rel is None:
                continue
            try:
                rows.append((rel, freshness.stat_signature(path)))
            except (OSError, ValueError):
                continue
        return recall_policy.recall_policy_identity(self.vault_root), tuple(sorted(rows))

    @staticmethod
    def _build_token(conn: sqlite3.Connection) -> tuple[int, int, int, int]:
        unit_generation = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (SEMANTIC_UNIT_GENERATION,)
        ).fetchone()
        return (*sidecar_store.read_meta_token(conn), int(unit_generation[0]) if unit_generation else 0)

    def rebuild_all(self, *, batch_size: int = 256) -> int:
        """Stage bounded batches in this sidecar and atomically replace serving rows.

        A serving publication or source/policy change during encoding refuses this
        candidate. Staging is invisible to readers and cleanup touches this run only.
        """
        from . import access
        from . import embeddings as embeddings_module
        from . import find as find_module

        scope = index_paths.index_scope()
        if scope == "kb" and not index_paths.kb_index_root(self.vault_root).is_dir():
            return 0
        source_snapshot = self._projected_source_snapshot()
        limit = max(1, int(batch_size))

        def pages() -> Iterator[
            tuple[Any, list[str], semantic_index.SemanticParentIndexState | None]
        ]:
            for md in index_paths.iter_index_markdown(self.vault_root):
                if not index_paths.is_embeddable_path(md):
                    continue
                page = find_module._CACHE.get(md, self.vault_root)
                if page is None or not access.is_indexable(self.vault_root, page.rel_path):
                    continue
                chunks = embeddings_module._chunks_for_page(self.vault_root, page) or []
                try:
                    state = semantic_index.build_parent_index_state(self.vault_root, md)
                except (OSError, UnicodeError, ValueError):
                    state = None
                # Every parsed parent publishes coverage, even with no occurrence.
                if chunks or state is not None:
                    yield page, chunks, state

        inputs = pages()
        first = next(inputs, None)
        if first is None:
            return 0
        run_key = uuid.uuid4().hex
        conn = self._connect()
        total = 0
        producer: recall_space.SpaceIdentity | None = None
        producer_model = recall_space.encoding_model()
        try:
            conn.execute("BEGIN")
            captured_token = self._build_token(conn)
            conn.rollback()
            with conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS embedding_build_runs "
                    "(run_key TEXT PRIMARY KEY, serving_token TEXT NOT NULL, space_identity TEXT)"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS embedding_build_chunks AS "
                    "SELECT CAST(NULL AS TEXT) AS run_key, chunks.* FROM chunks WHERE 0"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS embedding_build_units AS "
                    "SELECT CAST(NULL AS TEXT) AS run_key, semantic_unit_vectors.* "
                    "FROM semantic_unit_vectors WHERE 0"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS embedding_build_coverage "
                    "(run_key TEXT NOT NULL, parent_path TEXT NOT NULL, record TEXT NOT NULL)"
                )
                conn.execute(
                    "INSERT INTO embedding_build_runs(run_key, serving_token) VALUES (?, ?)",
                    (run_key, json.dumps(captured_token)),
                )
            pending_chunks: list[tuple[str, int, str, float]] = []
            pending_units: list[tuple] = []

            def flush(rows: list, *, units: bool = False) -> None:
                nonlocal producer, total
                if not rows:
                    return
                if recall_space.encoding_model() != producer_model:
                    raise recall_space.VectorSpaceMismatch(
                        "rebuild producer changed during staging"
                    )
                texts = [row[_CONTENT_COLUMN] if units else row[2] for row in rows]
                vectors, encoded = embeddings_module._encode_prepared(
                    lambda: embeddings_module.embed_texts(texts, is_query=False)
                )
                if len(vectors) != len(rows):
                    raise ValueError("rebuild encoder returned an invalid vector shape")
                if recall_space.encoding_model() != producer_model or (
                    producer is not None and encoded != producer
                ):
                    raise recall_space.VectorSpaceMismatch(
                        "rebuild producer changed during staging"
                    )
                producer = encoded
                with conn:
                    conn.execute(
                        "UPDATE embedding_build_runs SET space_identity = ? WHERE run_key = ?",
                        (json.dumps([producer.model, producer.fingerprint, producer.dim]), run_key),
                    )
                    if units:
                        conn.executemany(
                            "INSERT INTO embedding_build_units VALUES "
                            f"({', '.join('?' for _ in range(len(OCCURRENCE_COLUMNS) + 3))})",
                            (
                                (run_key, *row[:-2], vector.tobytes(), row[-1])
                                for row, vector in zip(rows, vectors, strict=True)
                            ),
                        )
                    else:
                        conn.executemany(
                            "INSERT INTO embedding_build_chunks VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                (run_key, path, idx, text, vector.tobytes(), mtime)
                                for (path, idx, text, mtime), vector in zip(
                                    rows, vectors, strict=True
                                )
                            ),
                        )
                        total += len(rows)
                rows.clear()

            for page, chunks, state in chain((first,), inputs):
                for idx, text in enumerate(chunks):
                    pending_chunks.append((page.rel_path, idx, text, page.mtime))
                    if len(pending_chunks) >= limit:
                        flush(pending_chunks)
                if state is None:
                    continue
                with conn:
                    conn.execute(
                        "INSERT INTO embedding_build_coverage VALUES (?, ?, ?)",
                        (run_key, state.path, coverage_record(state)),
                    )
                # Build row metadata without allocating placeholder vectors for a page.
                for row in occurrence_rows(state):
                    pending_units.append((*row, None, page.mtime))
                    if len(pending_units) >= limit:
                        flush(pending_units, units=True)
            flush(pending_chunks)
            flush(pending_units, units=True)
            if producer is None:
                return 0
            conn.execute("BEGIN IMMEDIATE")
            try:
                if (
                    self._build_token(conn) != captured_token
                    or self._projected_source_snapshot() != source_snapshot
                ):
                    conn.rollback()
                    log.info(
                        "rebuild_embeddings: serving or source snapshot changed; publication refused"
                    )
                    return 0
                conn.execute("DELETE FROM chunks")
                conn.execute("DELETE FROM semantic_unit_vectors")
                recall_space.write_identity(conn, producer)
                conn.execute(
                    "INSERT INTO chunks SELECT file_path, chunk_idx, chunk_text, vector, file_mtime "
                    "FROM embedding_build_chunks WHERE run_key = ?",
                    (run_key,),
                )
                conn.execute(
                    f"INSERT INTO semantic_unit_vectors SELECT {', '.join(OCCURRENCE_COLUMNS)}, "
                    "vector, file_mtime FROM embedding_build_units WHERE run_key = ?",
                    (run_key,),
                )
                _delete_coverage(conn)
                conn.execute(
                    "INSERT INTO meta(key, value) SELECT ? || parent_path, record "
                    "FROM embedding_build_coverage WHERE run_key = ?",
                    (COVERAGE_PREFIX, run_key),
                )
                mirror = self._publication_vec(conn, producer)
                if mirror is not None:
                    mirror.wipe(conn)
                    mirror.repopulate_all(conn)
                sidecar_store.bump_generation_for_reset(conn, CHUNK_PATH_LOG)
                sidecar_store.bump_meta(conn, "epoch")
                sidecar_store.bump_meta(conn, SEMANTIC_UNIT_GENERATION)
                if self._projected_source_snapshot() != source_snapshot:
                    conn.rollback()
                    return 0
                own_token = self._build_token(conn)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            self._complete_publication(producer, own_token)
            return total
        finally:
            conn.rollback()
            try:
                with conn:
                    for table in (
                        "embedding_build_chunks",
                        "embedding_build_units",
                        "embedding_build_coverage",
                        "embedding_build_runs",
                    ):
                        if (
                            conn.execute(
                                "SELECT 1 FROM sqlite_master WHERE name = ?", (table,)
                            ).fetchone()
                            is not None
                        ):
                            conn.execute(f"DELETE FROM {table} WHERE run_key = ?", (run_key,))
            finally:
                conn.close()

    @staticmethod
    def cache_token(vault_root: Path) -> tuple[int, int, int]:
        """`(epoch, generation, instance)` for this vault's embedding sidecar —
        the freshness signal find keys its hot cache on. `(0, 0, 0)` when the
        sidecar is absent or pre-meta (legacy); find's walk triples cover
        invalidation meanwhile.

        Deliberately NOT the sidecar file's mtime: WAL-checkpoint timing moves the
        mtime independent of content (spurious misses) and an uncheckpointed commit
        leaves it unmoved (stale hits). The in-band generation is bumped inside
        every write's transaction, so it changes iff the content did; `instance`
        additionally guards the ABA case where the sidecar was deleted and
        recreated from scratch (see `sidecar_store.ensure_meta_table`). Precedent and
        rationale: lexstore.cache_token. Read-only: never creates the sidecar.
        """
        path = index_paths.sidecar_path(vault_root)
        return sidecar_store.sidecar_cache_token(path)
