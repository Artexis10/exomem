"""SQL-native lexical search: FTS5 + trigram indexes in a per-vault sidecar.

`.lexical.sqlite` (next to the embedding sidecars, same per-machine dotfile
model) holds one row per markdown page and two FTS5 indexes over it:

- `fts` — an inverted index over PRE-STEMMED text. Both the indexed text and
  every query pass through `bm25.tokenize()` (lowercase → `[a-z0-9]+` →
  Snowball), so token and stemming semantics are byte-identical to the
  in-process `rank_bm25` scorer; FTS5 contributes only the posting lists and
  its C `bm25()` ranking. Queries are OR-joined to mirror `get_scores()`
  membership (any-term match), so per-query cost scales with the query's
  posting lists, not with N.
- `tri` — a trigram index over the SAME Python-lowercased title/body strings
  the keyword lane's reference scan compares against (`case_sensitive 1`
  because both sides are already Python-folded; SQLite-side folding could
  diverge from `str.lower()`). Trigram MATCH narrows candidates; an `instr()`
  verification of EVERY token against the stored raw text makes the returned
  match set exactly the reference scan's — including needles below the
  3-char trigram floor, which skip MATCH and rely on the verification scan.

Both lanes are lean-install lanes, so this module adds no dependency and loads
no extension: FTS5 and the trigram tokenizer ship inside CPython's bundled
SQLite (trigram needs SQLite >= 3.34; CPython 3.11 bundles >= 3.37).

Freshness follows the vec0 template, hardened to the digest-strength bar the
python rungs already meet (`test_bm25_sees_rename`: an out-of-band
`os.replace` preserves count AND max-mtime). Writers/watcher/reconcile
dual-write through `upsert_after_write` / `delete_after_remove`. A search
reconciles once per observed corpus change (the walk triple moved), not per
query, via a ladder ordered by cost:

1. page-count + max-mtime vs the triple — a mismatch is definite drift →
   rebuild from markdown (one mechanism: migration AND drift healer);
2. counts match and an in-process hook witnessed a write since the last
   reconcile → trust the hooks (they applied the exact change) and bless the
   current triple into `meta`;
3. counts match and `meta` already holds this exact triple (digest included)
   → verified, done — the steady state across restarts;
4. counts match but the triple is UNKNOWN — something changed while nothing
   was watching, yet count/mtime agree → compare legacy path/mtime rows. A
   rename heals incrementally; a full-signature-only change rebuilds so a
   preserved-mtime content replacement cannot bless stale FTS rows.

Availability is a ladder, decided per process (the `vecstore` idiom):
- `EXOMEM_LEXICAL_BACKEND` = `auto` (default) | `fts5` | `python` (kill
  switch). Policy lives in the module-level `search_*` entry points; the
  store class is mechanism.
- The FTS5 probe soft-fails once per process (`_PROBE_FAILED` memo); any
  runtime error retires the vault's store for the process. Every failure
  path returns `None`, the caller's cue to serve the in-process rung —
  never an exception, never a recorded lane degradation.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import logging
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from .kbdir import kb_dirname

log = logging.getLogger(__name__)

_NAV_BASENAMES = frozenset({"index.md", "log.md"})
SCHEMA_VERSION = 5
CATALOG_FOREGROUND_DELTA_CAP = 32

# Publication-barrier timeouts. Every LIVE-sidecar mutation and journal-mode
# transition shares the per-vault `lexical-catalog-publication` barrier so a
# writer can never commit into the window between a publish's live-WAL quiesce
# and its `os.replace`. Background rebuild/reconcile may take the normal bounded
# wait. Inline write hooks, stale-parent repair, and the bounded foreground delta
# use a very short, effectively-nonblocking wait so requests/writes decline and
# schedule single-flight repair instead of stalling on a publish. The barrier is
# non-reentrant per-thread across ALL namespaces, so a helper already under it
# must be handed ownership, never re-acquire.
_PUBLICATION_TIMEOUT_BACKGROUND = 30.0
_PUBLICATION_TIMEOUT_FOREGROUND = 0.05

_PROBE_RESULT: bool | None = None
_PROBE_LOCK = threading.Lock()

_STORES: dict[Path, LexicalStore] = {}
_STORES_LOCK = threading.Lock()
_REPAIRS_IN_FLIGHT: set[Path] = set()
_REPAIRS_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class SemanticUnitLexicalHit:
    """One internal lexical semantic-unit candidate."""

    record_type: str
    unit_ref: str
    parent_path: str
    parent_ref: str | None
    parent_generation: str
    parent_source_hash: str
    parser_version: int
    form: str
    category_raw: str
    category_key: str
    category: str
    kind: str
    content: str
    tags: tuple[str, ...]
    context: str | None
    source_hash: str
    anchor: str | None
    line: int
    end_line: int
    fingerprint: str | None
    source_order: int
    lexical_score: float | None


@dataclass(frozen=True, slots=True)
class CatalogReadiness:
    """Whether the normal-table catalog may answer an exact category/kind query.

    ``status`` is one of ``available`` (this exact projection is present),
    ``stale`` (well-formed but not yet at the requested freshness, or missing),
    ``unsupported`` (the ``python`` kill switch retired the catalog),
    ``transient_failure`` (a passing lock/interrupt), or ``fatal_failure`` (the
    sidecar proved unusable for the process). Only ``available`` is
    ``complete``; every other status defers to the caller's degraded path.
    ``backend`` records which lexical backend this decision was made under.
    """

    status: str
    complete: bool
    backend: str


@dataclass(frozen=True, slots=True)
class CatalogQueryResult:
    """Typed exact-catalog query result.

    `value` may legitimately be an empty list when `readiness.complete` is true.
    It is None for every incomplete outcome, preserving the distinction that the
    legacy list-or-None wrappers necessarily collapse.
    """

    value: object | None
    readiness: CatalogReadiness


CATALOG_RETRY_AFTER_MS = 250


def catalog_timing_profile(
    readiness: CatalogReadiness | None = None, *, cache_hit: bool = False
) -> dict[str, object]:
    """Fixed privacy-safe diagnostic fragment for exact semantic-catalog recall."""
    if cache_hit:
        return {
            "capability": "semantic_catalog",
            "backend": "not_used",
            "outcome": "available",
            "complete": True,
            "repair_state": "not_applicable",
            "retry_after_ms": None,
        }
    verdict = readiness or CatalogReadiness("unsupported", False, "python")
    if verdict.status == "available":
        repair_state = "none"
    elif verdict.status == "stale":
        repair_state = "requested"
    elif verdict.status == "fatal_failure":
        repair_state = "replacement_needed"
    else:
        repair_state = "not_applicable"
    return {
        "capability": "semantic_catalog",
        "backend": "not_used" if verdict.status == "unsupported" else "metadata_only",
        "outcome": verdict.status,
        "complete": bool(verdict.complete),
        "repair_state": repair_state,
        "retry_after_ms": None if verdict.complete else CATALOG_RETRY_AFTER_MS,
    }


def backend() -> str:
    """`EXOMEM_LEXICAL_BACKEND`: `auto` (default) | `fts5` | `python`.

    Unrecognized values fall back to `auto` — a typo must not silently disable
    the in-process escape hatch someone reached for, nor hard-fail search.
    """
    raw = (os.environ.get("EXOMEM_LEXICAL_BACKEND") or "").strip().lower()
    return raw if raw in ("fts5", "python") else "auto"


def _probe_fts5(conn: sqlite3.Connection) -> None:
    """Raise if this SQLite build lacks FTS5 or the trigram tokenizer.

    Module-level so tests can monkeypatch a deterministic failure.
    """
    conn.execute("CREATE VIRTUAL TABLE temp.__lex_probe USING fts5(x)")
    conn.execute(
        "CREATE VIRTUAL TABLE temp.__lex_probe_tri "
        "USING fts5(x, tokenize='trigram case_sensitive 1')"
    )


def fts5_available() -> bool:
    """One probe per process, memoized both ways."""
    global _PROBE_RESULT
    if _PROBE_RESULT is None:
        with _PROBE_LOCK:
            if _PROBE_RESULT is None:
                conn = sqlite3.connect(":memory:")
                try:
                    _probe_fts5(conn)
                    _PROBE_RESULT = True
                except sqlite3.Error as e:
                    _PROBE_RESULT = False
                    log.info(
                        "FTS5/trigram unavailable (%s); lexical lanes stay on the in-process paths",
                        e,
                    )
                finally:
                    conn.close()
    return _PROBE_RESULT


def reset_memo() -> None:
    """Test seam: forget the probe result."""
    global _PROBE_RESULT
    _PROBE_RESULT = None


# ------------------------------------------------------------------ error triage

# Primary SQLite result codes we classify by (when the driver attaches one).
_TRANSIENT_CODES = frozenset(
    c
    for c in (
        getattr(sqlite3, "SQLITE_BUSY", None),
        getattr(sqlite3, "SQLITE_LOCKED", None),
        getattr(sqlite3, "SQLITE_INTERRUPT", None),
    )
    if c is not None
)
_FATAL_CODES = frozenset(
    c
    for c in (
        getattr(sqlite3, "SQLITE_CORRUPT", None),
        getattr(sqlite3, "SQLITE_NOTADB", None),
    )
    if c is not None
)
# Canonical message fragments for the same codes, for errors carrying no code
# (e.g. hand-built test doubles, or older drivers). Narrow on purpose: a
# fragment must name exactly one proven condition, never a family.
_TRANSIENT_MESSAGES = ("is locked", "database is busy", "interrupted")
_FATAL_MESSAGES = ("disk image is malformed", "file is not a database", "not a database")
_REBUILDABLE_MESSAGES = ("no such table", "no such column", "schema has changed")


def classify_sqlite_error(error: BaseException) -> str:
    """Triage a SQLite error narrowly, by result code then canonical message.

    * ``"transient"`` — BUSY / LOCKED / INTERRUPT: the *current* operation
      fails, but the sidecar is fine; the next call reopens and retries.
    * ``"fatal"`` — CORRUPT / NOTADB: the disposable sidecar is proven
      unusable and should be retired for the process.
    * ``"rebuildable"`` — a schema/version mismatch: wipe and rebuild.
    * ``"unknown"`` — anything else: degrade this one call only. We never
      escalate an unproven error to process-lifetime retirement.
    """
    code = getattr(error, "sqlite_errorcode", None)
    if isinstance(code, int):
        base = code & 0xFF
        if base in _TRANSIENT_CODES:
            return "transient"
        if base in _FATAL_CODES:
            return "fatal"
    message = str(error).lower()
    if any(fragment in message for fragment in _TRANSIENT_MESSAGES):
        return "transient"
    if any(fragment in message for fragment in _FATAL_MESSAGES):
        return "fatal"
    if any(fragment in message for fragment in _REBUILDABLE_MESSAGES):
        return "rebuildable"
    return "unknown"


def lexical_path(vault_root: Path) -> Path:
    return vault_root / kb_dirname() / ".lexical.sqlite"


_CATALOG_IDENTITY_SCHEMA = "exomem.semantic-catalog.projection-identity.v1"
_REGISTRY_ABSENT_MARKER = "absent"


def catalog_semantic_identity(vault_root: Path) -> str:
    """Return the content-addressed identity of the semantic-catalog projection.

    The identity is a compound of the components that make an already-built
    catalog complete for a given corpus, so any of them changing invalidates a
    projection even when no note Markdown changed:

    * this slice's catalog/schema version (``SCHEMA_VERSION``);
    * the semantic-unit ``semantic_index.PARSER_VERSION``;
    * the canonical semantic-authoring contract id/version/content digest — the
      current core category/authoring identity seam;
    * the exact content hash of the extension semantic-language registry at
      ``semantic_language_registry.registry_path`` (an explicit stable marker
      when the registry is absent).

    Serialization is a canonical, sorted, separator-fixed JSON payload hashed
    with SHA-256. No Markdown corpus is walked, no YAML is interpreted, and no
    file is mutated; the registry is read only to hash its bytes, so neither
    personal vocabulary nor raw registry bytes appear in the returned identity.
    """
    from . import semantic_authoring, semantic_index, semantic_language_registry

    contract = semantic_authoring.get_semantic_authoring_contract()
    registry_file = semantic_language_registry.registry_path(Path(vault_root))
    try:
        registry_bytes = registry_file.read_bytes()
    except (FileNotFoundError, IsADirectoryError):
        registry_marker = _REGISTRY_ABSENT_MARKER
    else:
        registry_marker = f"sha256:{hashlib.sha256(registry_bytes).hexdigest()}"

    payload = {
        "schema": _CATALOG_IDENTITY_SCHEMA,
        "catalog_schema_version": SCHEMA_VERSION,
        "parser_version": semantic_index.PARSER_VERSION,
        "authoring_contract_id": contract.contract_id,
        "authoring_contract_version": contract.version,
        "authoring_contract_digest": contract.content_digest,
        "extension_registry_hash": registry_marker,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def get_store(vault_root: Path) -> LexicalStore:
    with _STORES_LOCK:
        store = _STORES.get(vault_root)
        if store is None:
            store = LexicalStore(vault_root)
            _STORES[vault_root] = store
        return store


def clear_stores() -> None:
    """Test seam: drop per-process stores (sync memos, failure flags)."""
    with _STORES_LOCK:
        _STORES.clear()


def _schedule_repair(vault_root: Path) -> None:
    """Start at most one best-effort lexical repair per vault.

    Foreground retrieval uses this seam after declining stale or missing
    sidecar work. The worker builds a detached sibling catalog and publishes it
    with a single atomic rename (`rebuild_atomic`) — never mutating or emptying
    the live sidecar in place — so a concurrent query never observes a
    half-built or emptied catalog, and a fatally-retired store can recover.
    Callers return immediately and may retry on a later request.
    """
    key = vault_root.resolve()
    with _REPAIRS_LOCK:
        if key in _REPAIRS_IN_FLIGHT:
            return
        _REPAIRS_IN_FLIGHT.add(key)

    def _run() -> None:
        try:
            get_store(vault_root).rebuild_atomic()
        except Exception as e:  # noqa: BLE001 - daemon must not escape into stderr
            log.warning("lexical background repair skipped (%s)", e)
        finally:
            with _REPAIRS_LOCK:
                _REPAIRS_IN_FLIGHT.discard(key)

    thread = threading.Thread(
        target=_run,
        name=f"exomem-lexical-repair-{key.name}",
        daemon=True,
    )
    try:
        thread.start()
    except RuntimeError:
        with _REPAIRS_LOCK:
            _REPAIRS_IN_FLIGHT.discard(key)
        log.warning("lexical background repair could not start for %s", key)


# ------------------------------------------------------------------ policy


def _usable() -> bool:
    return backend() != "python" and fts5_available()


def _catalog_usable() -> bool:
    """Whether the normal-table semantic catalog may serve/maintain this vault.

    The catalog (pages + semantic_units metadata) lives in ordinary SQLite
    tables and needs neither FTS5 nor the trigram tokenizer, so — unlike the
    content-ranking lanes gated by ``_usable`` — only the explicit ``python``
    kill switch retires it. Exact category/kind metadata therefore survives an
    FTS5-less SQLite build.
    """
    return backend() != "python"


def search_bm25(
    vault_root: Path,
    query: str,
    k: int,
    *,
    scope: str = "kb",
    freshness: tuple | None = None,
    allowed_paths: set[str] | None = None,
    repair: bool = True,
) -> list[tuple[str, float]] | None:
    """Top-k `(rel_path, score)` from the FTS5 index, or None → use the
    in-process rung. Matches the python rung's shape: OR membership,
    positive scores, deterministic (score, path) ordering, empty query → [].
    """
    if not _usable():
        return None
    if not query.strip():
        return []
    from . import bm25 as bm25_module

    tokens = bm25_module.tokenize(query)
    if not tokens:
        return []
    store = get_store(vault_root)
    return store.search_bm25(tokens, k, scope, freshness, allowed_paths, repair)


def search_substring(
    vault_root: Path,
    query_norm: str,
    *,
    scope: str = "kb",
    freshness: tuple | None = None,
    repair: bool = True,
) -> list[str] | None:
    """The keyword lane's match set (every whitespace token a substring of
    title or body), ordered `updated` desc then path desc, navigation files
    excluded — exactly `_keyword_match_paths`' contract. None → fall back.
    """
    if not _usable():
        return None
    if not query_norm:
        return []
    tokens = query_norm.split()
    if not tokens:
        return []
    store = get_store(vault_root)
    return store.search_substring(tokens, scope, freshness, repair)


def search_semantic_units(
    vault_root: Path,
    query: str,
    k: int,
    *,
    categories: list[str] | None = None,
    kinds: list[str] | None = None,
    clauses: tuple | None = None,
    scope: str = "kb",
    freshness: tuple | None = None,
    allowed_unit_refs: set[str] | None = None,
    literal_all: bool = False,
    _repair_stale: bool = False,
    _validate_current: bool = True,
    repair: bool = True,
) -> list[SemanticUnitLexicalHit] | None:
    """Return exact-metadata semantic-unit candidates from the lexical sidecar.

    Exact metadata (empty-query category/kind selection, and literal ``instr``
    substring selection) is answered from the normal-table catalog and needs no
    FTS5. Only the content-ranked BM25 lane (a non-empty query without
    ``literal_all``) requires the ``unit_fts`` virtual table; when FTS5 is
    unavailable that lane returns ``None`` so the caller serves its in-process
    rung, while exact metadata still resolves.

    ``clauses`` is the planner's branch-preserving DNF (a tuple of
    ``IndexCandidateClause`` values) — the same seed algebra page-level parent
    recall uses. When present it supersedes the flat ``categories``/``kinds``
    cross-product: candidate rows must satisfy at least one same-row
    ``(category... AND kind...)`` branch, so a cross-product row matching no
    single branch is never selected. The simple ``categories``/``kinds`` axes are
    preserved for existing callers that do not need branch correlation.
    """
    from . import bm25 as bm25_module
    from .semantic_units import canonicalize_category

    category_keys = tuple(sorted({canonicalize_category(value) for value in categories or ()}))
    kind_keys = tuple(sorted({canonicalize_category(value) for value in kinds or ()}))
    if category_keys or kind_keys or clauses:
        result = search_semantic_units_result(
            vault_root,
            query,
            k,
            categories=categories,
            kinds=kinds,
            clauses=clauses,
            scope=scope,
            freshness=freshness,
            allowed_unit_refs=allowed_unit_refs,
            literal_all=literal_all,
            _repair_stale=_repair_stale,
            _validate_current=_validate_current,
            repair=repair,
        )
        return result.value if result.readiness.complete else None
    if not _catalog_usable():
        return None
    tokens = bm25_module.tokenize(query) if query.strip() else []
    literal_tokens = tuple(query.lower().split()) if literal_all else ()
    if query.strip() and not tokens and not literal_tokens:
        return []
    if tokens and not literal_tokens and not fts5_available():
        # Content-only ranking may degrade when FTS is absent. Exact metadata
        # calls delegated above never probe or execute the FTS table.
        return None
    hits = get_store(vault_root).search_semantic_units(
        tokens,
        k,
        category_keys,
        kind_keys,
        scope,
        freshness,
        allowed_unit_refs,
        literal_tokens,
        repair,
        clauses=clauses,
    )
    if hits is None:
        return None
    if not _validate_current:
        return hits
    from .semantic_index import validate_parent_record

    current: list[SemanticUnitLexicalHit] = []
    stale_paths: set[str] = set()
    freshness_by_stamp: dict[tuple[str, str, str, int], bool] = {}
    for hit in hits:
        stamp = (
            hit.parent_path,
            hit.parent_generation,
            hit.parent_source_hash,
            hit.parser_version,
        )
        accepted = freshness_by_stamp.get(stamp)
        if accepted is None:
            accepted = validate_parent_record(
                vault_root,
                parent_path=hit.parent_path,
                parent_generation_value=hit.parent_generation,
                parent_source_hash=hit.parent_source_hash,
                parser_version=hit.parser_version,
            ).current
            freshness_by_stamp[stamp] = accepted
        if accepted:
            current.append(hit)
        else:
            stale_paths.add(hit.parent_path)
    if stale_paths and _repair_stale and repair:
        # Registry edits and missed watcher events can invalidate a parent
        # generation without changing the FTS match itself. Repair only the
        # indexed candidate parents and retry once; never parse the corpus.
        store = get_store(vault_root)
        if not store.upsert_paths([vault_root / path for path in sorted(stale_paths)]):
            # The bounded repair could not establish a current parent snapshot.
            # `None` preserves incompleteness so the exact category caller emits
            # warming; returning `current` (often []) would false-empty and cache.
            return None
        return search_semantic_units(
            vault_root,
            query,
            k,
            categories=categories,
            kinds=kinds,
            scope=scope,
            freshness=freshness,
            allowed_unit_refs=allowed_unit_refs,
            literal_all=literal_all,
            _repair_stale=False,
            repair=repair,
        )
    if stale_paths:
        _schedule_repair(vault_root)
        if category_keys or kind_keys or clauses:
            # Exact metadata recall requires a complete current candidate set.
            # Even after a nominal bounded retry, one stale parent makes the
            # answer incomplete; never return/cache a partial or false-empty list.
            return None
    return current


def search_semantic_units_result(
    vault_root: Path,
    query: str,
    k: int,
    *,
    categories: list[str] | None = None,
    kinds: list[str] | None = None,
    clauses: tuple | None = None,
    scope: str = "kb",
    freshness: tuple | None = None,
    allowed_unit_refs: set[str] | None = None,
    literal_all: bool = False,
    _repair_stale: bool = False,
    _validate_current: bool = True,
    repair: bool = True,
) -> CatalogQueryResult:
    """Typed exact-category unit query preserving every catalog outcome."""
    from .semantic_units import canonicalize_category

    if not _catalog_usable():
        return CatalogQueryResult(
            None, CatalogReadiness("unsupported", False, backend())
        )
    category_keys = tuple(sorted({canonicalize_category(v) for v in categories or ()}))
    kind_keys = tuple(sorted({canonicalize_category(v) for v in kinds or ()}))
    if not (category_keys or kind_keys or clauses):
        return CatalogQueryResult(
            None, CatalogReadiness("unsupported", False, backend())
        )
    # This is the exact metadata seam. Do not let a non-empty content query
    # turn catalog availability into an FTS capability probe: literal keyword
    # filtering uses normal-table `instr`, while hybrid/vector callers rank the
    # returned exact candidate set independently.
    tokens: list[str] = []
    literal_tokens = tuple(query.lower().split()) if literal_all else ()
    result = get_store(vault_root).search_semantic_units_result(
        tokens,
        k,
        category_keys,
        kind_keys,
        scope,
        freshness,
        allowed_unit_refs,
        literal_tokens,
        clauses=clauses,
    )
    if not result.readiness.complete or not _validate_current:
        return result

    from .semantic_index import validate_parent_record

    hits = list(result.value or [])
    current: list[SemanticUnitLexicalHit] = []
    stale_paths: set[str] = set()
    freshness_by_stamp: dict[tuple[str, str, str, int], bool] = {}
    for hit in hits:
        stamp = (
            hit.parent_path,
            hit.parent_generation,
            hit.parent_source_hash,
            hit.parser_version,
        )
        accepted = freshness_by_stamp.get(stamp)
        if accepted is None:
            accepted = validate_parent_record(
                vault_root,
                parent_path=hit.parent_path,
                parent_generation_value=hit.parent_generation,
                parent_source_hash=hit.parent_source_hash,
                parser_version=hit.parser_version,
            ).current
            freshness_by_stamp[stamp] = accepted
        if accepted:
            current.append(hit)
        else:
            stale_paths.add(hit.parent_path)
    if not stale_paths:
        return CatalogQueryResult(current, result.readiness)
    if _repair_stale and repair:
        store = get_store(vault_root)
        if store.upsert_paths([vault_root / p for p in sorted(stale_paths)]):
            return search_semantic_units_result(
                vault_root,
                query,
                k,
                categories=categories,
                kinds=kinds,
                clauses=clauses,
                scope=scope,
                freshness=freshness,
                allowed_unit_refs=allowed_unit_refs,
                literal_all=literal_all,
                _repair_stale=False,
                repair=repair,
            )
    _schedule_repair(vault_root)
    return CatalogQueryResult(
        None, CatalogReadiness("stale", False, result.readiness.backend)
    )


def search_semantic_parent_paths(
    vault_root: Path,
    clauses: tuple,
    *,
    scope: str = "kb",
    freshness: tuple | None = None,
) -> list[str] | None:
    """Distinct candidate parent identities for a planner clause disjunction.

    `clauses` is the planner's tuple of `IndexCandidateClause` values (each a
    branch-preserving conjunction over the category/kind axes). Answered from the
    normal-table catalog behind `catalog_readiness`; an incomplete projection
    returns ``None`` (the caller's cue that the maintained index cannot yet serve
    this safe exact seed) rather than an empty candidate set. Returns only
    relative path strings: matching semantic parents plus their in-scope
    scene-frame children.
    """
    if not _catalog_usable():
        return None
    return get_store(vault_root).search_semantic_parent_paths(clauses, scope, freshness)


def search_semantic_parent_paths_result(
    vault_root: Path,
    clauses: tuple,
    *,
    scope: str = "kb",
    freshness: tuple | None = None,
) -> CatalogQueryResult:
    """Typed counterpart to `search_semantic_parent_paths`."""
    if not _catalog_usable():
        return CatalogQueryResult(
            None, CatalogReadiness("unsupported", False, backend())
        )
    return get_store(vault_root).search_semantic_parent_paths_result(
        clauses, scope, freshness
    )


def ensure_fresh(vault_root: Path) -> None:
    """Run the reconcile NOW (reconcile's seam) instead of lazily on the next
    search — and paranoidly: verified state is discarded first, so this pass
    exact-checks the sidecar against the walk even where a search would trust
    it. No-op only when the sidecar is disabled outright (the ``python`` kill
    switch): the normal-table catalog builds even on an FTS5-less SQLite."""
    if not _catalog_usable():
        return
    get_store(vault_root).ensure_fresh()


def cache_token(vault_root: Path) -> str:
    """Which lexical backend would serve this vault right now — a stable part
    of find's hot-cache key, so a mid-process backend flip (env toggle, FTS5
    retirement) can't serve results cached under the other scorer.

    Deliberately NOT the sidecar file's mtime: WAL housekeeping touches the
    file on ordinary reads, which would leak spurious cache misses. A lexical
    REINDEX that changes results always rides a markdown-triple change, and
    the triples are already in the key — this token only pins the scorer.
    """
    if not _usable():
        return "python"
    with _STORES_LOCK:
        store = _STORES.get(vault_root)
    if store is not None and store._failed:
        return "python"
    return "fts5"


def catalog_cache_token(vault_root: Path) -> str:
    """Exact metadata scorer token without probing optional FTS capability."""
    if not _catalog_usable():
        return "python"
    with _STORES_LOCK:
        store = _STORES.get(vault_root)
    if store is not None and store._failed:
        return "unavailable"
    return "metadata_only"


# ------------------------------------------------------------------ write seams


def upsert_after_write(vault_root: Path, written_paths: list[Path]) -> None:
    """Keep the lexical index in lockstep with a writer's markdown change.

    Deliberately NOT gated behind the embeddings extra or its env switches —
    the lexical lanes run on lean installs. Best-effort: a lexical miss must
    never fail a write; sync-on-first-use heals whatever a miss leaves behind.
    No-ops (beyond its own gates) when the sidecar doesn't exist yet — the
    first search builds it whole.
    """
    if not _catalog_usable():
        return
    md = [p for p in written_paths if p.suffix.lower() == ".md" and ".sync-conflict-" not in p.name]
    if not md or not lexical_path(vault_root).exists():
        return
    try:
        get_store(vault_root).upsert_paths(md)
    except Exception as e:  # noqa: BLE001
        log.warning("lexical sidecar upsert skipped (%s)", e)


def delete_after_remove(vault_root: Path, removed_rel_paths: list[str]) -> None:
    """Drop lexical rows for removed files. Same gates as the upsert hook."""
    if not _catalog_usable():
        return
    if not removed_rel_paths or not lexical_path(vault_root).exists():
        return
    try:
        get_store(vault_root).delete_rel_paths(removed_rel_paths)
    except Exception as e:  # noqa: BLE001
        log.warning("lexical sidecar delete skipped (%s)", e)


# ------------------------------------------------------------------ mechanism


class LexicalStore:
    """Mechanism for ONE vault's lexical sidecar: schema, sync, dual-write,
    and the two search primitives. Policy (backend env, probe) lives in the
    module-level entry points."""

    def __init__(self, vault_root: Path) -> None:
        self.vault_root = vault_root
        self.path = lexical_path(vault_root)
        # scope -> the walk-freshness triple this store last reconciled against.
        self._synced: dict[str, tuple] = {}
        # scope -> exact live-registry triple applied by an in-process hook.
        # A witness is single-use and cannot bless a later, different corpus.
        self._witnessed: dict[str, tuple] = {}
        self._failed = False  # runtime-retired for this process
        self._lock = threading.Lock()

    # -------------------------------------------------------------- plumbing

    def _connect(self, path: Path | None = None) -> sqlite3.Connection:
        """Ordinary-read connection: bounded busy/synchronous policy only.

        Journal-mode negotiation is a setup/rebuild concern (`_connect_setup`);
        a plain read must not persist a WAL switch — it only touches the
        sidecar, and WAL housekeeping on reads would leak spurious churn.
        `path` targets a build sibling (`rebuild_atomic`); it defaults to the
        live sidecar.
        """
        target = path if path is not None else self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(target)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
        except BaseException:
            # A corrupt/NOTADB main can fail during connection policy setup.
            # Close the handle before propagating so fatal whole-set recovery can
            # quarantine the main/WAL/SHM on Windows instead of leaking a file
            # handle that makes every replacement retry fail forever.
            conn.close()
            raise
        return conn

    def _connect_setup(self, path: Path | None = None) -> sqlite3.Connection:
        """Setup/rebuild connection: additionally negotiates WAL, soft-failing.

        WAL is a build-time optimization, not a correctness requirement, so a
        read-only or journal-hostile filesystem must still build the sidecar —
        a failed negotiation is logged and ignored.
        """
        conn = self._connect(path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error as e:
            log.debug("lexical sidecar journal-mode negotiation skipped (%s)", e)
        return conn

    @staticmethod
    def _wal_shm_paths(base: Path) -> tuple[Path, Path]:
        """The WAL/SHM sidecar files SQLite keeps alongside `base`."""
        return (Path(str(base) + "-wal"), Path(str(base) + "-shm"))

    def _cleanup_sidecar_files(self, base: Path) -> None:
        """Remove `base` and its WAL/SHM siblings, ignoring what is absent."""
        for candidate in (base, *self._wal_shm_paths(base)):
            try:
                candidate.unlink()
            except OSError:
                pass

    # ---------------------------------------------------- publication serialization

    def _publication_lock(self, timeout: float = _PUBLICATION_TIMEOUT_BACKGROUND):
        """Cross-process serializer for EVERY live-sidecar mutation/publication.

        Reuses the vault's hardened interprocess file-lock namespace so the
        background `rebuild_atomic` replace, the bounded foreground
        `apply_catalog_delta` patch, and every ordinary live writer (`upsert_paths`,
        `delete_rel_paths`, `ensure_fresh`, and the search-path reconcile that may
        rebuild/heal/bless) can never interleave with a publish's live-WAL quiesce
        + `os.replace`. A publish reads the live catalog's generation and replaces
        the file under the same lock a writer commits under, so a writer's commit
        and the replace are serialized rather than racing. `timeout` is the
        background bound by default; the request-path foreground delta passes the
        short bound so a hot query declines fast on contention. Raises
        `vault.VaultLockError` (incl. timeout) when the lock cannot be taken;
        callers treat that as "decline, leave live untouched". The lock is
        non-reentrant across all namespaces in a thread, so an already-locked
        helper must receive ownership via `_maybe_publication_barrier`.
        """
        from .vault import vault_creation_lock

        return vault_creation_lock(
            self.vault_root, "lexical-catalog-publication", timeout=timeout
        )

    def _maybe_publication_barrier(
        self, held: bool, *, timeout: float = _PUBLICATION_TIMEOUT_BACKGROUND
    ):
        """The publication barrier as a context manager, or a no-op when the caller
        already owns it.

        The barrier is non-reentrant (a nested acquire in the same thread raises
        `VAULT_LOCK_NESTED`), so an internal helper reachable BOTH from a public
        locked wrapper (barrier already held) AND directly from a request path
        (barrier not yet held) is handed `held` so it acquires the barrier exactly
        once. `held=True` returns a `nullcontext`; `held=False` acquires the real
        barrier under `timeout`.
        """
        if held:
            return contextlib.nullcontext()
        return self._publication_lock(timeout=timeout)

    def _live_publication_guard(self) -> dict:
        """Snapshot the LIVE sidecar's published state for regression detection.

        Read from the live file only (never a temp build): its per-scope stored
        freshness checkpoint, its catalog identity, and whether its schema is
        current. Captured before a (long) rebuild scan and re-read under the
        publication lock right before replace; if the live catalog advanced
        meanwhile (a foreground delta bumped its checkpoint generation), the
        publish aborts rather than regressing it. All-absent/unreadable reads back
        as an empty guard so a first build or an unreadable live file never blocks
        a legitimate publish.
        """
        guard: dict = {
            "exists": self.path.exists(),
            "schema_current": False,
            "identity": None,
            "checkpoints": {},
            "db_set_token": None,
        }
        if not guard["exists"]:
            guard["db_set_token"] = self._db_set_generation_token()
            return guard
        try:
            conn = self._connect()
        except sqlite3.Error:
            guard["db_set_token"] = self._db_set_generation_token()
            return guard
        try:
            if self._schema_is_current(conn):
                guard["schema_current"] = True
                guard["identity"] = self._meta_catalog_identity(conn)
                for scope in ("kb", "vault"):
                    guard["checkpoints"][scope] = self._meta_checkpoint(conn, scope)
        except sqlite3.Error:
            pass
        finally:
            conn.close()
        guard["db_set_token"] = self._db_set_generation_token()
        return guard

    def _db_set_generation_token(self) -> tuple:
        """Private generation token for the live main/WAL/SHM set.

        The token is used only to decide whether a background temp build may
        replace the live disposable catalog. It contains filesystem identities
        and bounded metadata, never paths or note content. WAL/main mtime and size
        observe commits; SHM deliberately omits mtime because ordinary readers
        update its lock bytes, while existence/identity/size still detects set
        replacement. Conservative false aborts are safe: a later rebuild retries.
        """

        def member_token(path: Path, *, shared_memory: bool = False):
            try:
                stat_result = path.stat()
            except OSError:
                return None
            common = (
                int(getattr(stat_result, "st_dev", 0)),
                int(getattr(stat_result, "st_ino", 0)),
                int(stat_result.st_size),
            )
            if shared_memory:
                return common
            return (*common, int(stat_result.st_mtime_ns))

        wal, shm = self._wal_shm_paths(self.path)
        return (
            member_token(self.path),
            member_token(wal),
            member_token(shm, shared_memory=True),
        )

    @staticmethod
    def _publication_guard_changed(start_guard: dict, now_guard: dict) -> bool:
        """Whether another process/writer changed the live DB set during build.

        Exact equality is intentionally required. Checkpoint generations from
        different process instances are incomparable, so ordering them is unsafe;
        any changed currentness/identity/checkpoint/DB-set token aborts this stale
        publish. Equivalent state compares equal and remains publishable.
        """
        keys = (
            "exists",
            "schema_current",
            "identity",
            "checkpoints",
            "db_set_token",
        )
        changed = tuple(
            key for key in keys if start_guard.get(key) != now_guard.get(key)
        )
        if changed:
            log.debug("lexical publication guard changed fields: %s", changed)
        return bool(changed)

    @staticmethod
    def _live_regressed(now_guard: dict, temp_targets: dict, current_identity: str) -> bool:
        """Would publishing `temp_targets` overwrite a strictly-newer live catalog?

        True only when the live catalog carries the CURRENT projection identity
        (so it is a live, non-stale catalog worth preserving) AND some scope's live
        stored checkpoint is from this same process instance with a generation
        strictly greater than this build's target for that scope — i.e. a
        foreground delta (or another rebuild) advanced the live catalog past what
        this temp captured. A stale-identity live catalog is never "newer"; a
        cross-instance live checkpoint carries no comparable generation and is
        safely replaced by a fresh scan.
        """
        if now_guard.get("identity") != current_identity:
            return False
        for scope, target_cp in temp_targets.items():
            live_cp = now_guard.get("checkpoints", {}).get(scope)
            if live_cp is None or target_cp is None:
                continue
            if (
                live_cp.instance_id == target_cp.instance_id
                and live_cp.generation > target_cp.generation
            ):
                return True
        return False

    @staticmethod
    def _fold_to_single_file(conn: sqlite3.Connection) -> bool:
        """Fold WAL back into the main file and switch to DELETE journal mode.

        Returns True ONLY when the DB provably becomes a single self-contained
        main file: the truncating checkpoint succeeded and `journal_mode` is now
        `delete`. Any SQLite error, or a mode that did not switch, returns False so
        the caller discards the temp rather than publishing a main file whose
        committed data still lives in an un-folded `-wal`. Static so tests can
        monkeypatch it to force a fold failure.
        """
        try:
            checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            # SQLite reports checkpoint contention in-band as the first result
            # column; it need not raise. A nonzero busy count means the WAL was not
            # proven fully folded, even if a later mode pragma appears to succeed.
            if not checkpoint or int(checkpoint[0]) != 0:
                return False
            row = conn.execute("PRAGMA journal_mode=DELETE").fetchone()
        except (sqlite3.Error, TypeError, ValueError):
            return False
        return bool(row) and str(row[0]).lower() == "delete"

    def _quiesce_live_wal(self) -> bool:
        """Fold and drop the LIVE sidecar's `-wal`/`-shm` before a single-file
        replace, so we never (a) strand a live `-wal` after replacing only the main
        file, nor (b) blindly delete a live `-wal` a concurrent reader still
        depends on.

        Returns True when the live file is (now) a self-contained main file in
        persistent DELETE journal mode with no pending WAL — including when there
        is no live file. Visible sidecar absence is NOT sufficient: a closed
        WAL-mode main commonly has no `-wal`/`-shm`, and a later reader would
        recreate them. Returns False when the live DB cannot be safely folded and
        switched (busy, error, or refusal), so the caller declines publication.

        This is the NORMAL/healthy fold-and-replace path only. The two states a
        fold can never resolve — a missing main with orphan `-wal`/`-shm`, and a
        proven-fatal main+WAL that cannot checkpoint itself — are intercepted
        upstream by `_live_set_disposition` and recovered via whole-set quarantine
        (`_publish_over_quarantined_set`) before this is ever reached, so a
        `False` here always means "decline a valid but busy live DB", never
        "stranded forever".
        """
        if not self.path.exists():
            return True
        try:
            conn = self._connect()
        except sqlite3.Error:
            return False
        try:
            folded = self._fold_to_single_file(conn)
        finally:
            conn.close()
        if not folded:
            return False
        wal, shm = self._wal_shm_paths(self.path)
        return not (wal.exists() or shm.exists())

    # ------------------------------------------------ disposable-set recovery

    def _live_set_disposition(self) -> str:
        """Classify the LIVE DB set (main + `-wal` + `-shm`) for replacement.

        The main and its WAL/SHM are ONE disposable set. This decides how a new
        main may be installed over the current live names, run under the
        publication barrier right before the replace:

        * ``"quarantine"`` — the live names cannot be folded in place and a stale
          WAL could otherwise attach to a freshly published main of the same
          name: a MISSING main that still has orphan `-wal`/`-shm` beside it, or a
          proven-fatal (CORRUPT/NOTADB) main carrying a `-wal`/`-shm` that can
          never checkpoint itself. The caller moves the whole live-name set aside
          before installing the temp.
        * ``"normal"`` — every other set: a clean absence (no main, no orphan
          sidecars), a self-contained main with no WAL, or a healthy main whose
          WAL folds cleanly. The ordinary `_quiesce_live_wal` fold-and-replace
          handles these, and still DECLINES an ordinary busy/healthy WAL rather
          than evicting a valid open DB.
        """
        wal, shm = self._wal_shm_paths(self.path)
        sidecars_exist = wal.exists() or shm.exists()
        if not self.path.exists():
            # Orphan `-wal`/`-shm` beside a missing main would be adopted by a new
            # main published at the same name (a stale-generation hazard), so they
            # must be quarantined first. A truly clean absence is a normal first
            # install.
            return "quarantine" if sidecars_exist else "normal"
        # Prove fatality before relying on sidecar presence. SQLite may discard an
        # invalid WAL while opening a corrupt main, so a main+WAL set can become a
        # corrupt main-only set during classification. It still needs quarantine:
        # that preserves rollback semantics if installing the validated temp
        # fails, and avoids treating a proven-bad main as healthy normal state.
        if self._live_main_proven_fatal():
            return "quarantine"
        if not sidecars_exist:
            # A self-contained main with no WAL: replacing it strands nothing.
            return "normal"
        # Main + WAL present. Only a PROVEN-fatal main that can never checkpoint
        # itself routes to quarantine; a busy or otherwise unclassified main stays
        # "normal" so `_quiesce_live_wal` declines rather than evicting a valid DB.
        return "normal"

    def _live_main_proven_fatal(self) -> bool:
        """Does the live main prove CORRUPT/NOTADB when read?

        Only a proven-fatal result code (CORRUPT/NOTADB) — never a transient busy
        lock, an unknown error, or a clean read — makes a main+WAL set
        unfoldable-forever and thus a quarantine candidate. Uses a NON-mutating
        header/schema read (never a checkpoint), so classifying a healthy live DB
        cannot fold, checkpoint, or otherwise touch it. A busy/unknown outcome
        returns False, leaving the normal fold path to decide (and decline).
        """
        try:
            conn = self._connect()
        except sqlite3.Error as e:
            return classify_sqlite_error(e) == "fatal"
        try:
            # Reading the schema faults on a CORRUPT/NOTADB main (raising the fatal
            # code) but only reads a healthy one — no checkpoint, no mutation.
            conn.execute("SELECT count(*) FROM sqlite_master")
        except sqlite3.Error as e:
            return classify_sqlite_error(e) == "fatal"
        finally:
            conn.close()
        return False

    def _publish_over_quarantined_set(self, temp_path: Path) -> bool:
        """Recover a missing-main-with-orphans or proven-fatal live set by moving
        the ENTIRE live-name set aside, then installing the validated temp.

        Every existing live-name member (main, `-wal`, `-shm`) is moved to a
        unique sibling quarantine name BEFORE the temp is installed, so no stale
        WAL can ever share the new main's name. If installing the temp fails, the
        quarantined prior set is restored (safe reverse order) so the live names
        are never left as a mixed generation; if restore itself cannot complete,
        the isolated quarantine files are retained and the publish fails closed
        (never attached to the new main). On success the quarantined disposable
        files are removed best-effort — the new main uses its own names, so this
        never blindly deletes a freshly published live `-wal`/`-shm`.

        Runs with the publication barrier already held by the caller.
        """
        quarantined = self._quarantine_live_set()
        if quarantined is None:
            log.warning(
                "lexical atomic publish declined: could not quarantine the live DB set"
            )
            return False
        try:
            os.replace(temp_path, self.path)
        except OSError as e:
            log.warning(
                "lexical atomic publish failed after quarantine (%s); "
                "restoring the quarantined set",
                e,
            )
            self._restore_quarantined_set(quarantined)
            return False
        self._discard_quarantined_set(quarantined)
        return True

    def _quarantine_live_set(self) -> list[tuple[Path, Path]] | None:
        """Move each existing live-name member to a unique sibling quarantine name.

        Members (main, `-wal`, `-shm`) share ONE token so the isolated set stays
        recognizable and grouped, and are moved in that fixed order so the caller
        can restore in reverse. Returns the `(live_name, quarantine_name)` moves
        actually performed (empty when nothing existed). On the first failed move
        the moves already made are rolled back in reverse so no partial mixture is
        left at the live names, and None is returned so the caller declines.
        """
        token = uuid.uuid4().hex
        members = (self.path, *self._wal_shm_paths(self.path))
        moved: list[tuple[Path, Path]] = []
        for member in members:
            if not member.exists():
                continue
            quarantine = member.with_name(f"{member.name}.quarantine-{token}")
            try:
                os.replace(member, quarantine)
            except OSError:
                for original, previous in reversed(moved):
                    try:
                        os.replace(previous, original)
                    except OSError:
                        pass
                return None
            moved.append((member, quarantine))
        return moved

    def _restore_quarantined_set(self, quarantined: list[tuple[Path, Path]]) -> None:
        """Restore the quarantined live set to its live names in safe reverse order.

        Reverse order restores each `-wal`/`-shm` before the main, so the main
        never reappears at its live name without its own WAL beside it. If ANY
        member cannot be restored, the partial restore is undone — the members
        already restored are moved back under quarantine names — so the whole
        prior set stays clearly isolated and the live names are left empty: never
        a mixed generation, never a stale WAL attached to a new main.
        """
        restored: list[tuple[Path, Path]] = []
        for original, quarantine in reversed(quarantined):
            try:
                os.replace(quarantine, original)
            except OSError:
                for original2, quarantine2 in reversed(restored):
                    try:
                        os.replace(original2, quarantine2)
                    except OSError:
                        pass
                log.warning(
                    "lexical atomic publish could not restore the quarantined set; "
                    "retaining isolated quarantine files and failing closed"
                )
                return
            restored.append((original, quarantine))

    def _discard_quarantined_set(self, quarantined: list[tuple[Path, Path]]) -> None:
        """Best-effort removal of the quarantined disposable files after a publish.

        They are unreferenced once the new main is installed under its own names,
        so an unlink failure only leaves an inert isolated file — it never touches
        the freshly published live `-wal`/`-shm`.
        """
        for _original, quarantine in quarantined:
            try:
                quarantine.unlink()
            except OSError:
                pass

    # The current normal-table shape sentinel: an older (v4) `pages` table lacks
    # this column, so its presence distinguishes a mutation-safe current shape
    # from a legacy shape a new-column index/INSERT would fault against.
    _CURRENT_PAGES_COLUMN = "emitted_parent_path"

    @staticmethod
    def _pages_has_emitted_parent(conn: sqlite3.Connection) -> bool:
        """Read-only: does the `pages` table carry the current emitted-parent
        column? False when the table is absent or is an old (v4) shape."""
        return any(
            row[1] == LexicalStore._CURRENT_PAGES_COLUMN
            for row in conn.execute("PRAGMA table_info(pages)")
        )

    def _schema_is_current(self, conn: sqlite3.Connection) -> bool:
        """Read-only probe: is the catalog schema at the current version AND the
        current mutation-safe shape? Emits NO DDL, so the ordinary read paths
        (`catalog_readiness`, the foreground delta apply) can consult it without
        ever creating, altering, or dropping anything. A missing `meta`/`pages`
        table or a legacy `pages` shape reads as not-current — the caller then
        defers to the atomic rebuild rather than mutating the old shape here."""
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
        except sqlite3.OperationalError:
            return False  # no `meta` table yet
        if not (row and row[0] == str(SCHEMA_VERSION)):
            return False
        return self._pages_has_emitted_parent(conn)

    def _create_catalog_tables(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pages("
            " path TEXT PRIMARY KEY,"
            " mtime_ns INTEGER NOT NULL,"
            " updated TEXT NOT NULL DEFAULT '0000-00-00',"
            " in_kb INTEGER NOT NULL DEFAULT 0,"
            " in_vault INTEGER NOT NULL DEFAULT 0,"
            " is_nav INTEGER NOT NULL DEFAULT 0,"
            # The emitted (parent-video) note a scene-frame child collapses into,
            # NULL for ordinary pages — lets a matched semantic parent expand to
            # its in-scope frame children without a Markdown walk.
            " emitted_parent_path TEXT)"
        )
        # Per-scope walk triples this sidecar was last VERIFIED against
        # (repr'd) — the cross-process "nothing changed while we were down"
        # attestation that lets a restart skip the exact verify.
        conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS semantic_units("
            " record_type TEXT NOT NULL CHECK(record_type = 'semantic_unit'),"
            " unit_ref TEXT NOT NULL,"
            " parent_path TEXT NOT NULL,"
            " parent_ref TEXT,"
            " parent_generation TEXT NOT NULL,"
            " parent_source_hash TEXT NOT NULL,"
            " parser_version INTEGER NOT NULL,"
            " form TEXT NOT NULL,"
            " category_raw TEXT NOT NULL,"
            " category_key TEXT NOT NULL,"
            " category TEXT NOT NULL,"
            " kind TEXT NOT NULL,"
            " content TEXT NOT NULL,"
            " tags_json TEXT NOT NULL,"
            " context TEXT,"
            " unit_source_hash TEXT NOT NULL,"
            " anchor TEXT,"
            " line INTEGER NOT NULL,"
            " end_line INTEGER NOT NULL,"
            " fingerprint TEXT,"
            " source_order INTEGER NOT NULL,"
            " updated TEXT NOT NULL DEFAULT '0000-00-00',"
            " in_kb INTEGER NOT NULL DEFAULT 0,"
            " in_vault INTEGER NOT NULL DEFAULT 0,"
            " UNIQUE(parent_path, unit_ref))"
        )

    def _create_catalog_indexes(self, conn: sqlite3.Connection) -> None:
        # Covering indexes so the per-corpus-change count/max reconcile stays
        # index-ranged instead of scanning 100k rows.
        conn.execute("CREATE INDEX IF NOT EXISTS pages_kb ON pages(in_kb, mtime_ns)")
        conn.execute("CREATE INDEX IF NOT EXISTS pages_vault ON pages(in_vault, mtime_ns)")
        # Scoped emitted-parent lookup for the scene-expansion arm of the
        # candidate-parent query (`emitted_parent_path IN (...)` within a scope).
        conn.execute(
            "CREATE INDEX IF NOT EXISTS pages_emitted_parent "
            "ON pages(emitted_parent_path, in_kb, in_vault)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS semantic_units_parent "
            "ON semantic_units(parent_path, parent_generation)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS semantic_units_category "
            "ON semantic_units(category, in_kb, in_vault)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS semantic_units_kind "
            "ON semantic_units(kind, in_kb, in_vault)"
        )

    def _drop_catalog_tables(self, conn: sqlite3.Connection) -> None:
        """Drop the disposable normal + FTS tables (never `meta`), so a legacy
        shape can be replaced by the current one without a new-column index or
        INSERT ever running against the old table."""
        for table in ("semantic_units", "pages", "fts", "tri", "unit_fts"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")

    def _ensure_schema(self, conn: sqlite3.Connection) -> bool:
        """Setup/rebuild DDL owner: create (and, for a legacy shape, replace) the
        catalog tables/indexes, then report whether the stored version is current.

        A pre-existing old (v4) `pages` table would make the current-shape
        `CREATE TABLE IF NOT EXISTS` a no-op, leaving it without
        `emitted_parent_path`; running the new-column index or an INSERT against
        that shape faults. So an old shape is dropped and recreated current
        BEFORE any new-column DDL, keeping the disposable sidecar migratable in
        place while never faulting on the legacy shape. The read paths never
        reach here — they use the read-only `_schema_is_current` probe."""
        self._create_catalog_tables(conn)
        if not self._pages_has_emitted_parent(conn):
            self._drop_catalog_tables(conn)
            self._create_catalog_tables(conn)
        self._create_catalog_indexes(conn)
        # Optional content-ranking virtual tables — soft, isolated from the
        # normal-table catalog whose readiness is decided below.
        self._ensure_fts_schema(conn)
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        return bool(row and row[0] == str(SCHEMA_VERSION))

    def _ensure_fts_schema(self, conn: sqlite3.Connection) -> None:
        """Create the optional FTS5/trigram virtual tables when available.

        These back the BM25 (`fts`, `unit_fts`) and substring (`tri`) lanes
        only; the normal-table page/semantic-unit catalog does not read them.
        Creation is gated on the process FTS5 probe and additionally soft-fails,
        so an FTS5-less SQLite build (or a probe monkeypatched to fail) still
        completes schema setup — catalog readiness never depends on this."""
        if not fts5_available():
            return
        try:
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(stemmed)")
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS tri USING fts5("
                "title_lower, body_lower, tokenize='trigram case_sensitive 1')"
            )
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS unit_fts USING fts5(stemmed)")
        except sqlite3.Error as e:
            log.debug(
                "lexical FTS/trigram virtual tables unavailable (%s); "
                "the normal-table catalog stays FTS-independent",
                e,
            )

    # -------------------------------------------------------------- freshness

    def _scope_triple(self, scope: str) -> tuple:
        from . import bm25 as bm25_module

        return bm25_module.corpus_key(self.vault_root, scope)

    def _stored_count_max(self, conn: sqlite3.Connection, scope: str) -> tuple[int, int]:
        col = "in_vault" if scope == "vault" else "in_kb"
        row = conn.execute(
            f"SELECT count(*), COALESCE(max(mtime_ns), 0) FROM pages WHERE {col} = 1"
        ).fetchone()
        return int(row[0]), int(row[1])

    def _meta_triple(self, conn: sqlite3.Connection, scope: str) -> tuple | None:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (f"triple:{scope}",)).fetchone()
        if row is None:
            return None
        try:
            val = ast.literal_eval(row[0])
            return tuple(val) if isinstance(val, (list, tuple)) else None
        except (ValueError, SyntaxError):
            return None

    def _meta_catalog_identity(self, conn: sqlite3.Connection) -> str | None:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'catalog_identity'"
        ).fetchone()
        return str(row[0]) if row is not None else None

    def _write_catalog_identity(
        self, conn: sqlite3.Connection, identity: str | None = None
    ) -> None:
        """Upsert the current catalog semantic identity into `meta` (caller owns
        the txn) — no commit, so it is established with the surrounding
        triple/checkpoint write. A caller that parsed under a captured identity
        passes it explicitly, preventing a mid-transaction identity change from
        stamping mixed old/new rows as current."""
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('catalog_identity', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (identity if identity is not None else catalog_semantic_identity(self.vault_root),),
        )

    def _bless(self, conn: sqlite3.Connection, scope: str, triple: tuple) -> None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"triple:{scope}", repr(tuple(triple))),
        )
        # Pin the live freshness checkpoint the sidecar now reflects, alongside
        # the triple, so a later missed-event delta can be applied against a
        # known `from_` (see `apply_catalog_delta`).
        self._store_checkpoint(conn, scope)
        # Persist the catalog identity with the triple/checkpoint so all three
        # are established together (no extra commit of its own).
        self._write_catalog_identity(conn)
        conn.commit()
        self._synced[scope] = triple

    def _write_checkpoint(self, conn: sqlite3.Connection, scope: str, checkpoint) -> None:
        """Persist a freshness checkpoint into `meta` (caller owns the txn)."""
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"checkpoint:{scope}", repr(tuple(checkpoint))),
        )

    def _store_checkpoint(self, conn: sqlite3.Connection, scope: str) -> None:
        """Store the current live freshness checkpoint for `scope`."""
        from . import freshness as freshness_module

        self._write_checkpoint(
            conn, scope, freshness_module.consumer_checkpoint(self.vault_root, scope)
        )

    def _meta_checkpoint(self, conn: sqlite3.Connection, scope: str):
        """The consumer checkpoint stored with the catalog for `scope`, or None."""
        from . import freshness as freshness_module

        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (f"checkpoint:{scope}",)
        ).fetchone()
        if row is None:
            return None
        try:
            val = ast.literal_eval(row[0])
        except (ValueError, SyntaxError):
            return None
        if not isinstance(val, (list, tuple)) or len(val) != 3:
            return None
        instance_id, generation, triple = val
        if not isinstance(instance_id, str) or not instance_id:
            return None
        if isinstance(generation, bool) or not isinstance(generation, int):
            return None
        if triple is not None:
            if not isinstance(triple, (list, tuple)) or len(triple) != 3:
                return None
            count, mtime_ns, digest = triple
            if (
                isinstance(count, bool)
                or not isinstance(count, int)
                or isinstance(mtime_ns, bool)
                or not isinstance(mtime_ns, int)
                or not isinstance(digest, str)
            ):
                return None
            triple = (count, mtime_ns, digest)
        return freshness_module.FreshnessCheckpoint(instance_id, generation, triple)

    def _ensure_synced(
        self,
        conn: sqlite3.Connection,
        scope: str,
        freshness: tuple | None,
        *,
        repair: bool = True,
        barrier_held: bool = False,
    ) -> bool:
        """Reconcile against the walk ONCE per observed corpus change.

        `freshness` is the scope's `(count, max_mtime_ns, digest)` walk triple
        (free from the request's FreshnessSnapshot; computed here when
        absent). See the module docstring for the four-rung reconcile ladder
        this implements.

        Any rung that mutates the LIVE sidecar (`_rebuild`, `_heal_delta`,
        `_bless`) runs under the per-vault publication barrier so a reconcile
        never commits into the window a concurrent publish's `os.replace` is
        racing. `barrier_held` is passed by callers that already own the barrier
        (`ensure_fresh`) so it is not re-acquired (the barrier is non-reentrant).
        A contended barrier declines this reconcile (returns False) rather than
        blocking the publish or false-emptying the caller: the caller schedules a
        single-flight repair and the next request retries.
        """
        if freshness is None:
            if not repair:
                return False
            freshness = self._scope_triple(scope)
        if self._synced.get(scope) == freshness:
            return True
        from .vault import VaultLockError

        # Lock order is ALWAYS barrier-then-`self._lock`, never the reverse, so a
        # search-path reconcile (barrier not yet held) and `ensure_fresh` (barrier
        # already held) can never invert and deadlock. The barrier wraps the
        # `self._lock` critical section; `barrier_held` collapses it to a no-op for
        # callers that already own it.
        try:
            with self._maybe_publication_barrier(barrier_held):
                with self._lock:
                    if self._synced.get(scope) == freshness:
                        return True
                    return self._reconcile_live(conn, scope, freshness, repair)
        except VaultLockError:
            # A concurrent publication holds the barrier; decline rather than
            # race the replace. Readiness/search schedules the repair worker.
            return False

    def _reconcile_live(
        self,
        conn: sqlite3.Connection,
        scope: str,
        freshness: tuple,
        repair: bool,
    ) -> bool:
        """The four-rung reconcile ladder's mutating body.

        Runs with `self._lock` held AND the publication barrier held (or
        explicitly delegated), so a rebuild/heal/bless of the live sidecar can
        never interleave with a concurrent atomic publish.
        """
        if not self._ensure_schema(conn):
            if not repair:
                return False
            self._rebuild(conn)
            return True
        if self._stored_count_max(conn, scope) != (freshness[0], freshness[1]):
            if not repair:
                return False
            self._heal_delta(conn)  # incremental: patch only the drifted rows
            return True
        witnessed = self._witnessed.pop(scope, None)
        if witnessed == freshness:
            # The hook updated the sidecar for exactly this registry state.
            self._bless(conn, scope, freshness)
            return True
        if self._meta_triple(conn, scope) == freshness:
            self._synced[scope] = freshness  # verified before; unchanged
            return True
        if not repair:
            # Establishing whether an unknown digest is a cheap path-only
            # drift or a preserved-mtime content replacement requires an
            # exact corpus comparison. Leave that work to the repair
            # worker rather than putting it on this request.
            return False
        # Unwitnessed change with matching count/mtime. A path/mtime drift
        # can be healed incrementally. If those legacy row fields still
        # match while the full corpus signature changed, bytes were
        # replaced with a preserved mtime; rebuild so FTS content cannot be
        # blessed stale.
        if self._walk_matches_rows(conn, scope):
            self._rebuild(conn)
        else:
            self._heal_delta(conn)
        return True

    def _walk_entries(self):
        """One pass over both walks: membership flags + file signatures."""
        from . import find as find_module
        from . import freshness as freshness_module
        from .vault import walk_vault_md

        kb = self.vault_root / kb_dirname()
        members: dict[Path, list[bool]] = {}  # abs path -> [in_kb, in_vault]
        if kb.is_dir():
            for p in find_module._walk_md(kb):
                members.setdefault(p, [False, False])[0] = True
        for p in walk_vault_md(self.vault_root):
            members.setdefault(p, [False, False])[1] = True
        signatures: dict[Path, freshness_module.FileSignature] = {}
        for p in list(members):
            try:
                signatures[p] = freshness_module.stat_signature(p)
            except OSError:
                del members[p]  # the walk triple skips stat failures too
        return members, signatures

    def _rel(self, path: Path) -> str | None:
        try:
            return path.resolve().relative_to(self.vault_root.resolve()).as_posix()
        except ValueError:
            return None

    def _walk_matches_rows(self, conn: sqlite3.Connection, scope: str) -> bool:
        """Exact (path, mtime_ns) comparison of the scope's current set vs stored
        rows — reads the live freshness registry when available (no filesystem
        walk), else walks. Obsidian-Sync edits preserve mtimes, so this verify
        path is the one a real out-of-band edit hits; it must be walk-free too."""
        members, signatures = self._delta_source()
        idx = 1 if scope == "vault" else 0
        walked = {
            (rel, signatures[p][0])
            for p, flags in members.items()
            if flags[idx] and (rel := self._rel(p)) is not None
        }
        col = "in_vault" if scope == "vault" else "in_kb"
        stored = set(conn.execute(f"SELECT path, mtime_ns FROM pages WHERE {col} = 1").fetchall())
        return walked == stored

    def _rebuild(self, conn: sqlite3.Connection) -> None:
        """Wipe and repopulate pages+fts+tri from the markdown walks — the
        migration for pre-existing vaults and the heal for any drift. The
        walks also yield both scopes' exact triples for free, so the rebuild
        leaves BOTH scopes blessed and memoized."""
        from . import freshness as freshness_module

        members, signatures = self._walk_entries()
        log.info(
            "lexical sync: rebuilding %s from %d markdown file(s)",
            self.path.name,
            len(members),
        )
        with conn:
            conn.execute("DELETE FROM pages")
            conn.execute("DELETE FROM semantic_units")
            if fts5_available():
                conn.execute("DELETE FROM fts")
                conn.execute("DELETE FROM tri")
                conn.execute("DELETE FROM unit_fts")
            for path, (in_kb, in_vault) in members.items():
                self._insert_page(conn, path, signatures[path][0], in_kb, in_vault)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )
        self._witnessed.clear()
        for scope, idx in (("kb", 0), ("vault", 1)):
            entries = [(str(p), signatures[p]) for p, flags in members.items() if flags[idx]]
            self._bless(conn, scope, freshness_module.triple_from_entries(entries))

    def _delta_source(self):
        """`(members, signatures)` for the heal's diff — from the live freshness
        registry (no filesystem walk) when BOTH scopes are live, else a fresh walk.

        Whenever a heal fires, the registry map is already current (it's why the
        scope triple drifted from the sidecar), so reading it in-memory avoids
        re-statting the whole corpus — the ~2.7s drift-walk measured on the real
        D: vault. Byte-identical to `_walk_entries` by the freshness contract (the
        registry mirrors the same two walks). Falls back to the walk when the
        registry isn't live (kill-switched, or a scope never seeded)."""
        from . import freshness as freshness_module

        kb = freshness_module.live_entries(self.vault_root, "kb")
        vault = freshness_module.live_entries(self.vault_root, "vault")
        if kb is None or vault is None:
            return self._walk_entries()
        members: dict[Path, list[bool]] = {}
        signatures = {}
        for sp, signature in kb.items():
            members.setdefault(Path(sp), [False, False])[0] = True
            signatures[Path(sp)] = signature
        for sp, signature in vault.items():
            members.setdefault(Path(sp), [False, False])[1] = True
            signatures[Path(sp)] = signature
        return members, signatures

    def _heal_delta(self, conn: sqlite3.Connection) -> None:
        """Reconcile the sidecar to the markdown walks by touching ONLY the rows
        that drifted — the incremental alternative to `_rebuild`'s wipe-and-repopulate.

        Reaches the same end state (pages+fts+tri match the walks; both scopes
        blessed) but its writes are O(changed files), not O(corpus). This is the
        heal for the common real-vault case: a handful of out-of-band edits that
        the in-process hooks never witnessed (a watcher that missed the events).
        A single changed file therefore costs one delete+reinsert, not a full
        1,900-file rebuild. `rel` is computed once per file and used for both the
        stored-row lookup and the insert, so the delete-key and insert-key cannot
        diverge (the class of `UNIQUE constraint failed: pages.path`)."""
        from . import freshness as freshness_module

        members, signatures = self._delta_source()
        walk: dict[str, tuple[Path, tuple[int, int, int], bool, bool]] = {}
        for path, (in_kb, in_vault) in members.items():
            rel = self._rel(path)
            if rel is not None:
                walk[rel] = (path, signatures[path], in_kb, in_vault)
        # path(rel) -> (rowid, mtime_ns, in_kb, in_vault), snapshotted before writes.
        stored = {
            row[0]: (int(row[1]), int(row[2]), bool(row[3]), bool(row[4]))
            for row in conn.execute("SELECT path, rowid, mtime_ns, in_kb, in_vault FROM pages")
        }
        with conn:
            # External owner-row corruption can leave page and semantic-unit
            # index rows behind; purge them before SQLite reuses rowids.
            self._delete_orphan_rows(conn)
            for rel, (rowid, mtime_ns, in_kb, in_vault) in stored.items():
                w = walk.get(rel)
                if w is None or (w[1][0], w[2], w[3]) != (mtime_ns, in_kb, in_vault):
                    self._delete_rowid(conn, rowid)  # removed, or replaced below
            for rel, (path, signature, in_kb, in_vault) in walk.items():
                s = stored.get(rel)
                if s is None or (s[1], s[2], s[3]) != (signature[0], in_kb, in_vault):
                    self._insert_page(conn, path, signature[0], in_kb, in_vault)
        self._witnessed.clear()
        for scope, idx in (("kb", 0), ("vault", 1)):
            entries = [(str(p), signatures[p]) for p, flags in members.items() if flags[idx]]
            self._bless(conn, scope, freshness_module.triple_from_entries(entries))

    def _insert_page(
        self,
        conn: sqlite3.Connection,
        path: Path,
        mtime_ns: int,
        in_kb: bool,
        in_vault: bool,
    ) -> None:
        """Insert one page row + its fts/tri rows (caller owns the txn).

        Unparseable pages get an empty-text row: they keep the count check
        honest but can never match — mirroring the python rungs, which skip
        pages the parse cache rejects.
        """
        from . import bm25 as bm25_module
        from . import find as find_module

        page = find_module._CACHE.get(path, self.vault_root)
        if page is not None:
            rel = page.rel_path
            title_lower = page.title_norm
            body_lower = page.body_norm
            stemmed = " ".join(bm25_module.tokenize(page.title + " " + page.body))
            updated = page.updated or "0000-00-00"
            # Scene-frame children carry the parent video they collapse into;
            # unparseable rows (page is None) stay NULL like ordinary pages.
            emitted_parent_path = page.parent_media + ".md" if page.parent_media else None
        else:
            try:
                rel = path.resolve().relative_to(self.vault_root.resolve()).as_posix()
            except ValueError:
                return
            title_lower = body_lower = stemmed = ""
            updated = "0000-00-00"
            emitted_parent_path = None
        is_nav = path.name.lower() in _NAV_BASENAMES
        cur = conn.execute(
            "INSERT INTO pages(path, mtime_ns, updated, in_kb, in_vault, is_nav, "
            "emitted_parent_path) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                rel,
                mtime_ns,
                updated,
                int(in_kb),
                int(in_vault),
                int(is_nav),
                emitted_parent_path,
            ),
        )
        rowid = cur.lastrowid
        if fts5_available():
            conn.execute("INSERT INTO fts(rowid, stemmed) VALUES(?, ?)", (rowid, stemmed))
            conn.execute(
                "INSERT INTO tri(rowid, title_lower, body_lower) VALUES(?, ?, ?)",
                (rowid, title_lower, body_lower),
            )
        if page is not None:
            self._insert_semantic_units(
                conn,
                path,
                updated=updated,
                in_kb=in_kb,
                in_vault=in_vault,
            )

    def _insert_semantic_units(
        self,
        conn: sqlite3.Connection,
        path: Path,
        *,
        updated: str,
        in_kb: bool,
        in_vault: bool,
    ) -> None:
        from . import bm25 as bm25_module
        from .semantic_index import current_parent_index_state

        try:
            state = current_parent_index_state(self.vault_root, path)
        except (OSError, UnicodeError, ValueError):
            return
        for source_order, unit in enumerate(state.document.units):
            if unit.unit_ref is None:
                continue
            cur = conn.execute(
                "INSERT INTO semantic_units("
                "record_type, unit_ref, parent_path, parent_ref, parent_generation, "
                "parent_source_hash, parser_version, form, category_raw, category_key, "
                "category, kind, content, tags_json, context, unit_source_hash, anchor, "
                "line, end_line, fingerprint, source_order, updated, in_kb, in_vault) "
                "VALUES('semantic_unit', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    unit.unit_ref,
                    state.path,
                    state.parent_ref,
                    state.parent_generation,
                    state.parent_source_hash,
                    state.parser_version,
                    unit.form,
                    unit.category_raw,
                    unit.category_key,
                    unit.category,
                    unit.kind,
                    unit.content,
                    json.dumps(list(unit.tags), ensure_ascii=False, separators=(",", ":")),
                    unit.context,
                    unit.source_hash,
                    unit.anchor,
                    unit.line,
                    unit.end_line,
                    unit.fingerprint,
                    source_order,
                    updated,
                    int(in_kb),
                    int(in_vault),
                ),
            )
            if fts5_available():
                stemmed = " ".join(bm25_module.tokenize(unit.content))
                conn.execute(
                    "INSERT INTO unit_fts(rowid, stemmed) VALUES(?, ?)",
                    (cur.lastrowid, stemmed),
                )

    def _delete_semantic_units(self, conn: sqlite3.Connection, parent_path: str) -> None:
        if fts5_available():
            rowids = conn.execute(
                "SELECT rowid FROM semantic_units WHERE parent_path = ?", (parent_path,)
            ).fetchall()
            for (rowid,) in rowids:
                conn.execute("DELETE FROM unit_fts WHERE rowid = ?", (rowid,))
        conn.execute("DELETE FROM semantic_units WHERE parent_path = ?", (parent_path,))

    def _delete_orphan_rows(self, conn: sqlite3.Connection) -> None:
        """Remove side-table rows whose owning record was deleted out-of-band."""
        orphan_parents = conn.execute(
            "SELECT DISTINCT u.parent_path FROM semantic_units u "
            "LEFT JOIN pages p ON p.path = u.parent_path WHERE p.path IS NULL"
        ).fetchall()
        for (parent_path,) in orphan_parents:
            self._delete_semantic_units(conn, str(parent_path))
        if fts5_available():
            conn.execute(
                "DELETE FROM unit_fts WHERE rowid NOT IN (SELECT rowid FROM semantic_units)"
            )
            conn.execute("DELETE FROM fts WHERE rowid NOT IN (SELECT rowid FROM pages)")
            conn.execute("DELETE FROM tri WHERE rowid NOT IN (SELECT rowid FROM pages)")

    def _delete_rowid(self, conn: sqlite3.Connection, rowid: int) -> None:
        row = conn.execute("SELECT path FROM pages WHERE rowid = ?", (rowid,)).fetchone()
        if row is not None:
            self._delete_semantic_units(conn, row[0])
        if fts5_available():
            conn.execute("DELETE FROM fts WHERE rowid = ?", (rowid,))
            conn.execute("DELETE FROM tri WHERE rowid = ?", (rowid,))
        conn.execute("DELETE FROM pages WHERE rowid = ?", (rowid,))

    # -------------------------------------------------------------- dual-write

    def _membership(self, path: Path) -> tuple[bool, bool]:
        """Would each walk yield this file? Single-file replay of the walks'
        directory skip rules, so hook-written rows match a rebuild's."""
        from . import find as find_module
        from .vault import VAULT_SCAN_SKIP_DIRS

        try:
            rel_parts = path.resolve().relative_to(self.vault_root.resolve()).parts
        except ValueError:
            return False, False
        dirs = rel_parts[:-1]
        in_vault = not any(d in VAULT_SCAN_SKIP_DIRS for d in dirs)
        in_kb = (
            len(rel_parts) > 1
            and rel_parts[0] == kb_dirname()
            and not any(d in find_module.EXCLUDED_DIR_NAMES for d in dirs[1:])
        )
        return in_kb, in_vault

    def upsert_paths(self, paths: list[Path]) -> bool:
        """Bounded inline upsert under the publication barrier.

        Replacing file rows is a LIVE-sidecar change, so it shares the publication
        barrier. Inline hooks and stale-parent request repair get only the short
        foreground wait. Contention, absence, or a noncurrent schema declines and
        schedules atomic repair; this path never performs DDL or walks the corpus.
        Returns whether the bounded row update committed.
        """
        if self._failed:
            _schedule_repair(self.vault_root)
            return False
        from .vault import VaultLockError

        try:
            with self._publication_lock(timeout=_PUBLICATION_TIMEOUT_FOREGROUND):
                applied = self._upsert_paths_locked(paths)
        except VaultLockError as e:
            log.info("lexical sidecar upsert deferred (%s); heals on next sync", e)
            _schedule_repair(self.vault_root)
            return False
        except sqlite3.Error as e:
            self._note_query_failure(e, "lexical sidecar upsert deferred (%s)")
            _schedule_repair(self.vault_root)
            return False
        except OSError as e:
            log.info("lexical sidecar upsert source changed during repair (%s)", e)
            _schedule_repair(self.vault_root)
            return False
        if not applied:
            _schedule_repair(self.vault_root)
        return applied

    def _upsert_paths_locked(self, paths: list[Path]) -> bool:
        """`upsert_paths` body with the publication barrier already held."""
        from . import freshness as freshness_module

        if not self.path.exists():
            return False
        conn = self._connect()
        try:
            if not self._schema_is_current(conn):
                return False
            prepared: list[tuple[Path, str, tuple[int, int, int], bool, bool]] = []
            for path in paths:
                try:
                    rel = path.resolve().relative_to(self.vault_root.resolve()).as_posix()
                except ValueError:
                    continue
                # Validate every requested source before deleting any existing row.
                # A path disappearing mid-request is an incomplete snapshot, not a
                # successful delete; the dedicated delete seam handles removals.
                signature = freshness_module.stat_signature(path)
                in_kb, in_vault = self._membership(path)
                prepared.append((path, rel, signature, in_kb, in_vault))
            with conn:
                for path, rel, signature, in_kb, in_vault in prepared:
                    row = conn.execute("SELECT rowid FROM pages WHERE path = ?", (rel,)).fetchone()
                    if row is not None:
                        self._delete_rowid(conn, row[0])
                    else:
                        self._delete_semantic_units(conn, rel)
                    if not (in_kb or in_vault):
                        continue
                    self._insert_page(conn, path, signature[0], in_kb, in_vault)
                # `_insert_page` parses file bytes. Prove each prepared source is
                # still exactly that snapshot before commit; a disappearance or
                # edit rolls the transaction back rather than false-emptying the
                # recursive exact-category retry.
                for path, _rel, signature, in_kb, in_vault in prepared:
                    if (
                        freshness_module.stat_signature(path) != signature
                        or self._membership(path) != (in_kb, in_vault)
                    ):
                        raise OSError(f"source changed during bounded upsert: {path.name}")
            self._remember_live_witnesses()
            return True
        finally:
            conn.close()

    def delete_rel_paths(self, rel_paths: list[str]) -> bool:
        """Bounded inline delete under the barrier (see `upsert_paths`)."""
        if self._failed:
            _schedule_repair(self.vault_root)
            return False
        from .vault import VaultLockError

        try:
            with self._publication_lock(timeout=_PUBLICATION_TIMEOUT_FOREGROUND):
                applied = self._delete_rel_paths_locked(rel_paths)
        except VaultLockError as e:
            log.info("lexical sidecar delete deferred (%s); heals on next sync", e)
            _schedule_repair(self.vault_root)
            return False
        except sqlite3.Error as e:
            self._note_query_failure(e, "lexical sidecar delete deferred (%s)")
            _schedule_repair(self.vault_root)
            return False
        if not applied:
            _schedule_repair(self.vault_root)
        return applied

    def _delete_rel_paths_locked(self, rel_paths: list[str]) -> bool:
        """`delete_rel_paths` body with the publication barrier already held."""
        if not self.path.exists():
            return False
        conn = self._connect()
        try:
            if not self._schema_is_current(conn):
                return False
            with conn:
                for rel in rel_paths:
                    row = conn.execute("SELECT rowid FROM pages WHERE path = ?", (rel,)).fetchone()
                    if row is not None:
                        self._delete_rowid(conn, row[0])
                    else:
                        self._delete_semantic_units(conn, rel)
            self._remember_live_witnesses()
            return True
        finally:
            conn.close()

    def _remember_live_witnesses(self) -> None:
        """Remember only the exact watcher-maintained corpus just applied.

        Without a live registry there is no race-free corpus attestation, so
        the next read takes the conservative verify/rebuild path.
        """
        from . import freshness as freshness_module

        for scope in ("kb", "vault"):
            triple = freshness_module.triple(self.vault_root, scope)
            if triple is None:
                self._witnessed.pop(scope, None)
            else:
                self._witnessed[scope] = triple

    def ensure_fresh(self) -> None:
        """Reconcile both scopes against their walks, PARANOIDLY: verified
        state (memo, meta, hook witness) is discarded first, so this pass
        exact-checks the sidecar even where a search would trust it — this is
        the `reconcile` command's "I edited around the system, heal it" seam.

        Runs under the per-vault publication barrier (this whole pass mutates the
        live sidecar — journal negotiation, identity rebuild, and both scopes'
        reconcile) so it never races a concurrent publish's `os.replace`. The
        barrier is acquired ONCE here and handed to `_ensure_synced` via
        `barrier_held=True`, so the non-reentrant barrier is never re-acquired. A
        contended barrier declines this reconcile; the next reconcile retries.
        """
        if self._failed:
            return
        from .vault import VaultLockError

        try:
            with self._publication_lock():
                self._ensure_fresh_locked()
        except VaultLockError as e:
            log.info("lexical ensure_fresh deferred (%s); live sidecar preserved", e)
        except sqlite3.Error as e:
            self._note_query_failure(
                e,
                "lexical sidecar failed (%s); this process serves the in-process lexical paths",
            )

    def _ensure_fresh_locked(self) -> None:
        """`ensure_fresh` body with the publication barrier already held."""
        conn = self._connect_setup()
        try:
            if not self._ensure_schema(conn):
                self._rebuild(conn)
                return
            # Before any `_synced`/triple fast path can attest freshness, a
            # changed catalog identity (schema/parser/authoring/registry)
            # invalidates the projection even with an unchanged corpus. This
            # explicit reconcile is allowed to rebuild.
            if self._meta_catalog_identity(conn) != catalog_semantic_identity(
                self.vault_root
            ):
                self._rebuild(conn)
                return
            self._witnessed.clear()
            self._synced.clear()
            conn.execute("DELETE FROM meta WHERE key LIKE 'triple:%'")
            conn.commit()
            # Barrier already held by `ensure_fresh`; reconcile without re-taking it.
            self._ensure_synced(conn, "vault", None, barrier_held=True)
            self._ensure_synced(conn, "kb", None, barrier_held=True)
        finally:
            conn.close()

    def rebuild_atomic(self) -> bool:
        """Rebuild the catalog into a detached sibling sidecar, then publish it
        with a single atomic rename — never deleting or mutating the live sidecar
        during the build.

        This is the background repair worker's mechanism, and — unlike every
        read/write path — it runs even when the store is `_failed`: a fatally
        retired disposable sidecar recovers only by being replaced whole.

        Sequence:

        1. Under the cross-process publication lock, capture the live DB-set guard,
           each scope's start freshness checkpoint, and the semantic projection
           identity BEFORE the (potentially long) corpus scan. Any later live
           publication changes that exact guard and makes this build ineligible.
        2. Build a fresh temp sidecar from the walk; if the projection identity
           shifted under the scan, discard it (the parsed units used the old
           projection).
        3. Obtain each scope's complete delta from its start checkpoint and
           replay it onto the temp DB WITHOUT the 32-path foreground cap. An
           incomplete/overflowed delta means the exact target checkpoint cannot
           be proven, so the temp is discarded and the live sidecar left
           untouched.
        4. Persist each scope's exact target checkpoint/triple and the matching
           identity in the temp DB, then fold its WAL into a single self-contained
           main file — publication is refused unless that provably succeeds.
        5. Re-walk the source and require its exact triples to still match the temp
           targets. Then, under the publication lock, require the live DB-set guard
           to be equivalent to its start guard. This catches foreign-process
           publications whose checkpoint generations cannot be ordered. Fold the
           live WAL safely and publish with one `os.replace`. On any failure/abort
           the live sidecar is preserved untouched.

        Returns True when it published, False when it declined (transient
        failure, incomplete/overflowed delta, identity change mid-build, a WAL
        that could not be safely folded, or a live catalog that advanced past this
        build).
        Events that arrive after the captured target remain in the registry
        history and surface on the next readiness check.
        """
        from . import freshness as freshness_module
        from .vault import VaultLockError

        if backend() == "python":
            return False

        try:
            with self._publication_lock():
                start_identity = catalog_semantic_identity(self.vault_root)
                start_checkpoints = {
                    scope: freshness_module.consumer_checkpoint(self.vault_root, scope)
                    for scope in ("kb", "vault")
                }
                start_live_guard = self._live_publication_guard()
        except VaultLockError as e:
            log.warning(
                "lexical atomic rebuild baseline deferred (%s); live sidecar preserved", e
            )
            return False
        temp_path = self.path.with_name(f"{self.path.name}.rebuild-{uuid.uuid4().hex}.tmp")
        try:
            try:
                published = self._build_and_publish(
                    temp_path,
                    start_identity,
                    start_checkpoints,
                    start_live_guard,
                )
            except sqlite3.Error as e:
                if classify_sqlite_error(e) == "transient":
                    # A passing lock/interrupt fails only this attempt; the next
                    # request retries. Never escalate to a scheduled repair.
                    return False
                raise
            except VaultLockError as e:
                # Could not take (or timed out on) the publication lock, so this
                # build cannot safely serialize its replace against a concurrent
                # publish. Decline; the live sidecar is untouched.
                log.warning("lexical atomic publish deferred (%s); live sidecar preserved", e)
                return False
            except OSError as e:
                # A failed atomic publish leaves the live sidecar untouched.
                log.warning("lexical atomic publish failed (%s); live sidecar preserved", e)
                return False
        finally:
            self._cleanup_sidecar_files(temp_path)

        if published:
            with self._lock:
                # A published current catalog clears the disposable-failure flag
                # and every stale attestation; the persisted meta triples let the
                # next read re-verify cheaply without a rebuild.
                self._failed = False
                self._synced.clear()
                self._witnessed.clear()
        return published

    def _build_and_publish(
        self,
        temp_path: Path,
        start_identity: str,
        start_checkpoints: dict,
        start_live_guard: dict,
    ) -> bool:
        from . import freshness as freshness_module

        members, signatures = self._walk_entries()  # the corpus scan
        # The projection identity must not have shifted under the scan.
        if catalog_semantic_identity(self.vault_root) != start_identity:
            return False

        # Deltas from each start checkpoint, captured (non-destructively) BEFORE
        # any temp write; an incomplete/overflowed span cannot prove the target.
        scope_targets: dict[str, tuple] = {}
        for scope, idx in (("kb", 0), ("vault", 1)):
            entries = [(str(p), signatures[p]) for p, flags in members.items() if flags[idx]]
            walk_triple = freshness_module.triple_from_entries(entries)
            if freshness_module.live_entries(self.vault_root, scope) is not None:
                delta = freshness_module.delta_since(
                    self.vault_root, scope, start_checkpoints[scope]
                )
                if not delta.complete:
                    return False
                scope_targets[scope] = ("delta", delta, delta.to, delta.to.triple)
            else:
                # No live registry (watcher disabled / never seeded): the walk is
                # the authoritative snapshot. `consumer_checkpoint` reports a
                # triple-None, generation-0 checkpoint for a non-live scope, which
                # readiness could never match — publishing it claims success while
                # readiness stays permanently stale, driving a rebuild storm. Bind
                # the checkpoint to the exact WALK triple the built rows reflect so
                # a non-live rebuild converges: readiness matching the walk triple
                # becomes `available`, not permanent warming.
                base = freshness_module.consumer_checkpoint(self.vault_root, scope)
                checkpoint = base._replace(triple=walk_triple)
                scope_targets[scope] = ("walk", None, checkpoint, walk_triple)

        # The exact target checkpoints this build will publish, per scope — the
        # regression guard below compares them against the live catalog.
        temp_targets = {scope: target[2] for scope, target in scope_targets.items()}

        conn = self._connect_setup(temp_path)
        folded = False
        try:
            self._ensure_schema(conn)
            with conn:
                for path, (in_kb, in_vault) in members.items():
                    self._insert_page(conn, path, signatures[path][0], in_kb, in_vault)
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(SCHEMA_VERSION),),
                )
            with conn:
                for _scope, (kind, delta, _cp, _tr) in scope_targets.items():
                    if kind == "delta":
                        self._apply_delta_rows(conn, delta)  # no foreground cap
                for scope, (_kind, _delta, checkpoint, triple) in scope_targets.items():
                    if triple is not None:
                        conn.execute(
                            "INSERT INTO meta(key, value) VALUES(?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                            (f"triple:{scope}", repr(tuple(triple))),
                        )
                    self._write_checkpoint(conn, scope, checkpoint)
                self._write_catalog_identity(conn)
            # Fold the WAL back into the main file so the published sidecar is a
            # single self-contained file. Publication is FORBIDDEN unless this
            # provably succeeds — otherwise a plain `os.replace` of just the main
            # file would strand committed data in an un-folded temp `-wal`.
            folded = self._fold_to_single_file(conn)
        finally:
            conn.close()

        # The temp must be provably self-contained in its main file before it may
        # replace anything: the fold must have switched to DELETE mode AND left no
        # `-wal`/`-shm` behind. Otherwise discard the temp and preserve live.
        wal, shm = self._wal_shm_paths(temp_path)
        if not folded or wal.exists() or shm.exists():
            log.warning(
                "lexical temp WAL fold incomplete; discarding build and preserving live"
            )
            return False

        # A last identity re-check before publication: a shift after the scan
        # means the built units reflect a superseded projection.
        if catalog_semantic_identity(self.vault_root) != start_identity:
            return False

        # The replay target proves registry continuity, but another machine or
        # process may edit the shared corpus without this process observing a
        # watcher event. A final independent source snapshot closes that gap. It
        # is deliberately background-only: no request path pays for this walk.
        final_members, final_signatures = self._walk_entries()
        for scope, idx in (("kb", 0), ("vault", 1)):
            entries = [
                (str(path), final_signatures[path])
                for path, flags in final_members.items()
                if flags[idx]
            ]
            final_triple = freshness_module.triple_from_entries(entries)
            if final_triple != scope_targets[scope][3]:
                log.info(
                    "lexical atomic publish aborted: %s source changed after target capture",
                    scope,
                )
                return False

        # Serialize the guard re-read + live-WAL fold + replace against any
        # concurrent publish (background rebuild or foreground delta) so a newer
        # live catalog is never overwritten and the replace is snapshot-consistent.
        with self._publication_lock():
            now_guard = self._live_publication_guard()
            if self._publication_guard_changed(start_live_guard, now_guard):
                # Checkpoint generation ordering is meaningful only within one
                # process instance. Exact guard equality also catches a foreign
                # process/out-of-order publish and DB-set replacement that cannot
                # be compared safely; a later rebuild can retry from that baseline.
                log.info("lexical atomic publish aborted: live catalog changed during build")
                return False
            if self._live_regressed(now_guard, temp_targets, start_identity):
                # A foreground delta (or another rebuild) advanced the live catalog
                # past this build's target while we were building. Abort rather
                # than regress it; the later target stays in the registry history
                # and a subsequent rebuild will capture it.
                log.info("lexical atomic publish aborted: live catalog advanced past build")
                return False
            # The live main + `-wal` + `-shm` are one disposable set. A missing
            # main with orphan sidecars, or a proven-fatal main+WAL that can never
            # checkpoint itself, cannot be folded in place: recover by moving the
            # WHOLE live-name set aside before installing the temp, so no stale WAL
            # can share the freshly published main's name.
            if self._live_set_disposition() == "quarantine":
                return self._publish_over_quarantined_set(temp_path)
            # Normal/healthy set: fold the LIVE `-wal`/`-shm` (content-preserving)
            # so the single-file replace cannot strand a live WAL, and so we never
            # blindly delete a live `-wal` a concurrent reader depends on. A busy/
            # healthy WAL declines here rather than evicting a valid open DB.
            if not self._quiesce_live_wal():
                log.info("lexical atomic publish declined: live WAL not safely foldable")
                return False
            os.replace(temp_path, self.path)
            # Live `-wal`/`-shm` were folded away by `_quiesce_live_wal` before the
            # replace; there is nothing to blindly unlink here.
        return True

    # ---------------------------------------------------------- delta sidecar

    def catalog_checkpoint(self, scope: str):
        """The exact consumer checkpoint stored with the catalog for `scope`.

        None when the sidecar has never been built/blessed (no catalog to bind
        a delta against yet).
        """
        try:
            conn = self._connect()
        except sqlite3.Error:
            return None
        try:
            return self._meta_checkpoint(conn, scope)
        except sqlite3.Error:
            return None
        finally:
            conn.close()

    def apply_catalog_delta(self, scope: str, delta) -> None:
        """Apply one complete freshness delta to the catalog transactionally.

        Accepts ONLY a `complete` delta whose `from_` is the checkpoint stored
        with the catalog for `scope` — a partial or unbound delta could silently
        skip or resurrect rows, so it is rejected outright. In a single SQLite
        transaction: prove `delta.to` is still the live registry target and every
        changed path still has its captured target signature, drop/reinsert its
        rows, re-prove target + source + semantic identity, then store `delta.to`
        and the identity captured before replay. If any proof or write fails, the
        whole transaction rolls back — neither rows nor checkpoint advance.

        `changed`/`deleted` are absolute path strings from the live registry;
        each is normalized to a vault-relative row identity before use.
        """
        from .vault import VaultLockError

        if self._failed:
            return
        if not getattr(delta, "complete", False):
            raise ValueError("apply_catalog_delta requires a complete delta")
        if len({*delta.deleted, *delta.changed}) > CATALOG_FOREGROUND_DELTA_CAP:
            raise ValueError(
                "apply_catalog_delta exceeds the foreground delta cap "
                f"({CATALOG_FOREGROUND_DELTA_CAP})"
            )
        # The delta publish is serialized against the background `rebuild_atomic`
        # replace under the shared publication lock: a background publish reads the
        # live generation and aborts on regression, and this patch re-reads the
        # stored checkpoint UNDER the lock and rejects an unbound `from_`, so the
        # two can never interleave to lose one another's advance. This is the
        # request path, so it takes the SHORT (nonblocking) foreground bound: on
        # contention it declines almost immediately rather than stalling a hot
        # query behind a background publish, and the readiness re-check schedules
        # a single-flight background repair.
        try:
            with self._publication_lock(timeout=_PUBLICATION_TIMEOUT_FOREGROUND):
                # Ordinary connection: a bounded foreground patch must NOT
                # negotiate journal mode (`_connect_setup`) — WAL setup is a
                # build/rebuild concern.
                conn = self._connect()
                try:
                    if not self._schema_is_current(conn):
                        # The foreground delta apply is a surgical patch, never a
                        # build: a not-current schema must defer to the repair
                        # worker, not rebuild the corpus on this request.
                        raise ValueError(
                            "apply_catalog_delta requires a current schema; refusing to rebuild"
                        )
                    captured_identity = catalog_semantic_identity(self.vault_root)
                    if self._meta_catalog_identity(conn) != captured_identity:
                        raise ValueError(
                            "catalog identity changed before foreground delta apply"
                        )
                    stored = self._meta_checkpoint(conn, scope)
                    if stored is None or delta.from_ != stored:
                        # A concurrent publish advanced the live checkpoint out
                        # from under this delta; it is no longer bound.
                        raise ValueError(
                            "delta is not bound to the stored catalog checkpoint"
                        )
                    if not self._delta_target_still_current(scope, delta):
                        raise ValueError(
                            "delta target/source is no longer current before replay"
                        )
                    with conn:
                        self._apply_delta_rows(conn, delta)
                        if catalog_semantic_identity(self.vault_root) != captured_identity:
                            raise ValueError(
                                "catalog identity changed during foreground delta replay"
                            )
                        if not self._delta_target_still_current(scope, delta):
                            raise ValueError(
                                "delta target/source changed during foreground replay"
                            )
                        if delta.to.triple is not None:
                            conn.execute(
                                "INSERT INTO meta(key, value) VALUES(?, ?) "
                                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                                (f"triple:{scope}", repr(tuple(delta.to.triple))),
                            )
                        self._write_checkpoint(conn, scope, delta.to)
                        # Stamp exactly the identity under which every replayed row
                        # was parsed; never re-read a possibly newer identity here.
                        self._write_catalog_identity(conn, captured_identity)
                    # Committed: only now advance the in-memory synced attestation.
                    if delta.to.triple is not None:
                        self._synced[scope] = tuple(delta.to.triple)
                finally:
                    conn.close()
        except VaultLockError:
            return

    def _delta_target_still_current(self, scope: str, delta) -> bool:
        """Prove a bounded delta still names the registry and filesystem target.

        The registry checkpoint catches a later observed event. Captured signatures
        catch an edit/delete that happened after `delta_since` but before its event
        reached this process. Both are checked before and after row materialization.
        """
        from . import freshness as freshness_module

        if freshness_module.consumer_checkpoint(self.vault_root, scope) != delta.to:
            return False
        expected = dict(getattr(delta, "target_signatures", ()))
        if set(expected) != set(delta.changed):
            return False
        scope_index = 0 if scope == "kb" else 1
        for sp, signature in expected.items():
            path = Path(sp)
            if not self._membership(path)[scope_index]:
                return False
            try:
                if freshness_module.stat_signature(path) != tuple(signature):
                    return False
            except OSError:
                return False
        for sp in delta.deleted:
            path = Path(sp)
            if path.exists() and self._membership(path)[scope_index]:
                return False
        return freshness_module.consumer_checkpoint(self.vault_root, scope) == delta.to

    def _apply_delta_rows(self, conn: sqlite3.Connection, delta) -> None:
        """Replay one delta's row mutations onto `conn` (caller owns the txn).

        Drops the changed+deleted parents' rows, then reinserts every changed
        path at its current membership/signature. Target-state coalescing in the
        delta guarantees a deleted-at-`to` path is never reinserted and a
        changed-at-`to` path is never left dropped, so apply order is
        irrelevant. Shared by the bounded foreground apply and the unbounded
        background replay in `rebuild_atomic`.
        """
        for sp in (*delta.deleted, *delta.changed):
            rel = self._rel(Path(sp))
            if rel is None:
                continue
            row = conn.execute("SELECT rowid FROM pages WHERE path = ?", (rel,)).fetchone()
            if row is not None:
                self._delete_rowid(conn, row[0])
            else:
                self._delete_semantic_units(conn, rel)
        for sp in delta.changed:
            path = Path(sp)
            if self._rel(path) is None:
                continue
            try:
                mtime_ns = path.stat().st_mtime_ns
            except OSError:
                continue  # changed then gone before we read it → stays out
            in_kb, in_vault = self._membership(path)
            if not (in_kb or in_vault):
                continue
            self._insert_page(conn, path, mtime_ns, in_kb, in_vault)

    # -------------------------------------------------------------- search

    def _note_query_failure(self, error: sqlite3.Error, message: str) -> None:
        """Decide retirement from a failed query, narrowly.

        Only a proven-fatal code (CORRUPT/NOTADB) retires the disposable
        sidecar for the process. A transient lock or an unclassified error
        degrades the current call only — the next request reopens and retries,
        so a generic error must never set the sticky ``_failed`` flag.
        """
        if classify_sqlite_error(error) == "fatal":
            self._failed = True
            log.warning(message, error)
        else:
            log.info(message, error)

    def _serve_synced_live_catalog(
        self,
        scope: str,
        freshness: tuple | None,
        *,
        repair: bool,
        query_fn,
    ):
        """Open, reconcile, and query legacy content-ranked lanes under one barrier.

        This helper may perform the historical synchronous reconcile used by BM25,
        substring, and category-less semantic content search. Safe exact category/
        kind requests MUST NOT call it: they route through
        `_serve_from_ready_catalog`, whose readiness/delta path is short-bounded,
        non-walking, and returns incomplete rather than synchronously rebuilding.

        The publication barrier is acquired *before* opening SQLite. Opening the
        connection first is unsafe: a publisher can replace the main file while
        the request waits for the barrier, after which reconciliation would
        commit to the old/unlinked inode. Keeping connection lifetime inside the
        barrier makes the order unambiguous: barrier -> open -> reconcile/query ->
        close -> release.
        """
        from .vault import VaultLockError

        try:
            with self._publication_lock():
                conn = self._connect()
                try:
                    if not self._ensure_synced(
                        conn,
                        scope,
                        freshness,
                        repair=repair,
                        barrier_held=True,
                    ):
                        _schedule_repair(self.vault_root)
                        return None
                    return query_fn(conn)
                finally:
                    conn.close()
        except VaultLockError:
            _schedule_repair(self.vault_root)
            return None

    def search_bm25(
        self,
        stemmed_tokens: list[str],
        k: int,
        scope: str,
        freshness: tuple | None,
        allowed_paths: set[str] | None = None,
        repair: bool = True,
    ) -> list[tuple[str, float]] | None:
        if self._failed:
            return None
        if not repair and not self.path.exists():
            _schedule_repair(self.vault_root)
            return None
        try:
            return self._serve_synced_live_catalog(
                scope,
                freshness,
                repair=repair,
                query_fn=lambda conn: self._bm25_query(
                    conn, stemmed_tokens, k, scope, allowed_paths
                ),
            )
        except sqlite3.Error as e:
            self._note_query_failure(
                e,
                "lexical sidecar failed (%s); this process serves the in-process lexical paths",
            )
            return None

    def _bm25_query(
        self,
        conn: sqlite3.Connection,
        tokens: list[str],
        k: int,
        scope: str,
        allowed_paths: set[str] | None = None,
    ) -> list[tuple[str, float]]:
        # Tokens are [a-z0-9]+ — no FTS5 syntax can hide in them, but quote
        # anyway; OR mirrors get_scores() membership (any-term match).
        match = " OR ".join(f'"{t}"' for t in tokens)
        col = "in_vault" if scope == "vault" else "in_kb"
        allowed_clause = ""
        params: list[object] = [match]
        if allowed_paths is not None:
            allowed_clause = " AND p.path IN (SELECT value FROM json_each(?))"
            params.append(json.dumps(sorted(allowed_paths), ensure_ascii=False))
        params.append(k)
        rows = conn.execute(
            "SELECT p.path, -bm25(fts) AS score "
            "FROM fts JOIN pages p ON p.rowid = fts.rowid "
            f"WHERE fts MATCH ? AND p.{col} = 1" + allowed_clause + " "
            "ORDER BY bm25(fts), p.path LIMIT ?",
            params,
        ).fetchall()
        return [(p, float(s)) for p, s in rows]

    def catalog_readiness(
        self, scope: str, freshness: tuple | None, *, allow_delta: bool = True
    ) -> CatalogReadiness:
        """Decide, without ever rebuilding/healing/walking, whether the exact
        category/kind projection can be served for `scope` at `freshness`.

        The seam an exact metadata query consults before opening its query
        connection: it inspects the sidecar's schema, catalog identity, and
        stored freshness checkpoint, optionally applies a single bounded
        foreground delta to catch up, and otherwise defers to the repair worker.
        Every non-`available` outcome is reported (never raised); raw SQLite is
        classified here and never escapes this seam.
        """
        backend_name = backend()
        if backend_name == "python":
            return CatalogReadiness("unsupported", False, backend_name)
        if self._failed:
            # Fatal recovery is NOT one-shot. A fatally-retired sidecar recovers
            # only by a whole atomic replacement; if the first `rebuild_atomic`
            # attempt failed (sharing violation, identity shift, incomplete/
            # overflowed delta, aborted publish), `_failed` stays set and no
            # repair is in flight. Every later readiness call must schedule the
            # single-flight repair AGAIN so recovery keeps being retried — else a
            # single failed attempt would strand the store as fatal for the
            # process. `_schedule_repair` is itself single-flight, so this cannot
            # storm.
            _schedule_repair(self.vault_root)
            return CatalogReadiness("fatal_failure", False, backend_name)
        if not self.path.exists():
            _schedule_repair(self.vault_root)
            return CatalogReadiness("stale", False, backend_name)
        try:
            conn = self._connect()
            try:
                if not self._schema_is_current(conn):
                    # Read-only probe: an absent/old-shape (v4) or version-stale
                    # sidecar defers to the atomic rebuild — this seam emits no DDL.
                    _schedule_repair(self.vault_root)
                    return CatalogReadiness("stale", False, backend_name)
                stored_identity = self._meta_catalog_identity(conn)
                if stored_identity is None or stored_identity != catalog_semantic_identity(
                    self.vault_root
                ):
                    _schedule_repair(self.vault_root)
                    return CatalogReadiness("stale", False, backend_name)
                checkpoint = self._meta_checkpoint(conn, scope)
                if checkpoint is None:
                    _schedule_repair(self.vault_root)
                    return CatalogReadiness("stale", False, backend_name)
                if (
                    freshness is not None
                    and checkpoint.triple is not None
                    and tuple(checkpoint.triple) == tuple(freshness)
                ):
                    return CatalogReadiness("available", True, backend_name)
            finally:
                conn.close()
            # Well-formed sidecar whose checkpoint predates the request. Try one
            # bounded foreground delta to catch up before deferring to repair.
            if allow_delta:
                from . import freshness as freshness_module

                if freshness_module.live_entries(self.vault_root, scope) is not None:
                    delta = freshness_module.delta_since(
                        self.vault_root, scope, checkpoint
                    )
                    if (
                        getattr(delta, "complete", False)
                        and len({*delta.changed, *delta.deleted})
                        <= CATALOG_FOREGROUND_DELTA_CAP
                    ):
                        try:
                            self.apply_catalog_delta(scope, delta)
                        except ValueError:
                            # A concurrent publish moved the stored checkpoint out
                            # from under this delta (no longer bound), so it could
                            # not be applied. Defer to the repair worker rather than
                            # letting the mismatch escape this seam.
                            _schedule_repair(self.vault_root)
                            return CatalogReadiness("stale", False, backend_name)
                        return self.catalog_readiness(
                            scope, freshness, allow_delta=False
                        )
            _schedule_repair(self.vault_root)
            return CatalogReadiness("stale", False, backend_name)
        except sqlite3.Error as e:
            return self._catalog_readiness_error(e, backend_name)

    def _serve_from_ready_catalog_result(self, scope, freshness, query_fn, failure_message):
        """Validate readiness AND run `query_fn(conn)` bound to ONE connection and
        read transaction, so a concurrent publication cannot swap the catalog file
        between the readiness proof and the query (the readiness-query TOCTOU).

        `catalog_readiness` decides availability (and may apply a bounded delta to
        catch up); if it is complete, a fresh connection opens a read transaction
        that pins a single catalog snapshot, re-proves the stored checkpoint still
        equals `freshness` under that pin, and only then runs the query on the SAME
        connection. A publication that landed after the verdict is caught by the
        re-proof — the query never runs against an unvalidated generation, so an
        `available`/`complete` verdict can never yield a false empty from a
        replacement N-1. Returns `query_fn`'s result, or None to defer.
        """
        readiness = self.catalog_readiness(scope, freshness)
        if not readiness.complete:
            return CatalogQueryResult(None, readiness)
        try:
            conn = self._connect()
            try:
                # BEGIN + the first read establish one snapshot before any row is
                # returned; the whole validate-then-query runs inside it.
                conn.execute("BEGIN")
                stored = self._meta_checkpoint(conn, scope)
                if (
                    freshness is None
                    or stored is None
                    or stored.triple is None
                    or tuple(stored.triple) != tuple(freshness)
                    or not self._schema_is_current(conn)
                    or self._meta_catalog_identity(conn)
                    != catalog_semantic_identity(self.vault_root)
                ):
                    # The pinned snapshot no longer matches the readiness proof — a
                    # publication landed between the verdict and this transaction.
                    # Defer rather than serve an unvalidated (possibly regressed)
                    # generation; the repair worker reconciles it.
                    _schedule_repair(self.vault_root)
                    return CatalogQueryResult(
                        None, CatalogReadiness("stale", False, readiness.backend)
                    )
                return CatalogQueryResult(query_fn(conn), readiness)
            finally:
                conn.close()
        except sqlite3.Error as e:
            verdict = self._catalog_readiness_error(e, readiness.backend)
            if verdict.status == "transient_failure":
                log.info(failure_message, e)
            elif verdict.status == "fatal_failure":
                log.warning(failure_message, e)
            return CatalogQueryResult(None, verdict)

    def _serve_from_ready_catalog(self, scope, freshness, query_fn, failure_message):
        """Compatibility wrapper retaining the historical list-or-None contract."""
        result = self._serve_from_ready_catalog_result(
            scope, freshness, query_fn, failure_message
        )
        return result.value if result.readiness.complete else None

    def _catalog_readiness_error(
        self, error: sqlite3.Error, backend_name: str
    ) -> CatalogReadiness:
        """Map a raw SQLite failure onto a readiness verdict, so no SQLite error
        escapes the readiness seam. A fatal or unclassified failure schedules one
        atomic repair; a transient lock/interrupt does not (the next call retries).
        """
        kind = classify_sqlite_error(error)
        if kind == "fatal":
            # A proven-fatal disposable sidecar recovers only by whole replacement.
            self._failed = True
            _schedule_repair(self.vault_root)
            return CatalogReadiness("fatal_failure", False, backend_name)
        if kind == "transient":
            # A passing lock/interrupt fails only this call; the next request
            # reopens and retries. It must NOT schedule a whole-corpus repair.
            return CatalogReadiness("transient_failure", False, backend_name)
        _schedule_repair(self.vault_root)
        return CatalogReadiness("stale", False, backend_name)

    def search_semantic_units(
        self,
        stemmed_tokens: list[str],
        k: int,
        categories: tuple[str, ...],
        kinds: tuple[str, ...],
        scope: str,
        freshness: tuple | None,
        allowed_unit_refs: set[str] | None = None,
        literal_tokens: tuple[str, ...] = (),
        repair: bool = True,
        clauses: tuple | None = None,
    ) -> list[SemanticUnitLexicalHit] | None:
        if categories or kinds or clauses:
            # Exact category/kind selection (flat axes or a branch-preserving DNF
            # clause set) consults the readiness seam first and never touches
            # `_ensure_synced`, so the foreground query path can neither rebuild,
            # heal, nor walk the corpus. Readiness AND the query are bound to one
            # connection/read transaction so a concurrent publication cannot swap
            # the catalog between the proof and the query. Incomplete → defer.
            result = self.search_semantic_units_result(
                stemmed_tokens,
                k,
                categories,
                kinds,
                scope,
                freshness,
                allowed_unit_refs,
                literal_tokens,
                clauses=clauses,
            )
            return result.value if result.readiness.complete else None
        if self._failed:
            return None
        if not repair and not self.path.exists():
            _schedule_repair(self.vault_root)
            return None
        try:
            return self._serve_synced_live_catalog(
                scope,
                freshness,
                repair=repair,
                query_fn=lambda conn: self._semantic_unit_query(
                    conn,
                    stemmed_tokens,
                    k,
                    categories,
                    kinds,
                    scope,
                    allowed_unit_refs,
                    literal_tokens,
                ),
            )
        except sqlite3.Error as e:
            self._note_query_failure(
                e,
                "lexical semantic-unit sidecar failed (%s); unit retrieval degrades",
            )
            return None

    def search_semantic_units_result(
        self,
        stemmed_tokens: list[str],
        k: int,
        categories: tuple[str, ...],
        kinds: tuple[str, ...],
        scope: str,
        freshness: tuple | None,
        allowed_unit_refs: set[str] | None = None,
        literal_tokens: tuple[str, ...] = (),
        *,
        clauses: tuple | None = None,
    ) -> CatalogQueryResult:
        """Typed exact category/kind unit query; never used for content-only lanes."""
        if not (categories or kinds or clauses):
            return CatalogQueryResult(
                None, CatalogReadiness("unsupported", False, backend())
            )
        return self._serve_from_ready_catalog_result(
            scope,
            freshness,
            lambda conn: self._semantic_unit_query(
                conn,
                [],
                k,
                categories,
                kinds,
                scope,
                allowed_unit_refs,
                literal_tokens,
                dnf_clauses=clauses,
            ),
            "lexical semantic-unit sidecar failed (%s); unit retrieval degrades",
        )

    def _semantic_unit_query(
        self,
        conn: sqlite3.Connection,
        tokens: list[str],
        k: int,
        categories: tuple[str, ...],
        kinds: tuple[str, ...],
        scope: str,
        allowed_unit_refs: set[str] | None = None,
        literal_tokens: tuple[str, ...] = (),
        dnf_clauses: tuple | None = None,
    ) -> list[SemanticUnitLexicalHit]:
        col = "in_vault" if scope == "vault" else "in_kb"
        clauses = [f"u.{col} = 1"]
        params: list[object] = []
        if dnf_clauses is not None:
            # Branch-preserving DNF over the same semantic-unit row — identical
            # algebra to page-level parent recall. A row that fails every branch
            # (a category/kind cross-product) is never selected or hydrated.
            predicate, dnf_params = self._clause_predicate(dnf_clauses)
            clauses.append(f"({predicate})")
            params.extend(dnf_params)
        if categories:
            placeholders = ",".join("?" for _ in categories)
            clauses.append(f"u.category IN ({placeholders})")
            params.extend(categories)
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            clauses.append(f"u.kind IN ({placeholders})")
            params.extend(kinds)
        if allowed_unit_refs is not None:
            clauses.append("u.unit_ref IN (SELECT value FROM json_each(?))")
            params.append(json.dumps(sorted(allowed_unit_refs), ensure_ascii=False))
        columns = (
            "u.record_type, u.unit_ref, u.parent_path, u.parent_ref, "
            "u.parent_generation, u.parent_source_hash, u.parser_version, u.form, "
            "u.category_raw, u.category_key, u.category, u.kind, u.content, "
            "u.tags_json, u.context, u.unit_source_hash, u.anchor, u.line, "
            "u.end_line, u.fingerprint, u.source_order"
        )
        if literal_tokens:
            literal_clauses = ["instr(lower(u.content), ?) > 0" for _ in literal_tokens]
            rows = conn.execute(
                f"SELECT {columns}, NULL AS lexical_score FROM semantic_units u WHERE "
                + " AND ".join([*clauses, *literal_clauses])
                + " ORDER BY u.updated DESC, u.parent_path DESC, u.source_order LIMIT ?",
                [*params, *literal_tokens, k],
            ).fetchall()
        elif tokens:
            match = " OR ".join(f'"{token}"' for token in tokens)
            rows = conn.execute(
                f"SELECT {columns}, -bm25(unit_fts) AS lexical_score "
                "FROM unit_fts JOIN semantic_units u ON u.rowid = unit_fts.rowid "
                "WHERE unit_fts MATCH ? AND "
                + " AND ".join(clauses)
                + " ORDER BY bm25(unit_fts), u.parent_path, u.source_order LIMIT ?",
                [match, *params, k],
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {columns}, NULL AS lexical_score FROM semantic_units u WHERE "
                + " AND ".join(clauses)
                + " ORDER BY u.updated DESC, u.parent_path DESC, u.source_order LIMIT ?",
                [*params, k],
            ).fetchall()
        return [self._semantic_unit_hit(row) for row in rows]

    @staticmethod
    def _semantic_unit_hit(row: tuple) -> SemanticUnitLexicalHit:
        tags = json.loads(row[13])
        return SemanticUnitLexicalHit(
            record_type=str(row[0]),
            unit_ref=str(row[1]),
            parent_path=str(row[2]),
            parent_ref=str(row[3]) if row[3] is not None else None,
            parent_generation=str(row[4]),
            parent_source_hash=str(row[5]),
            parser_version=int(row[6]),
            form=str(row[7]),
            category_raw=str(row[8]),
            category_key=str(row[9]),
            category=str(row[10]),
            kind=str(row[11]),
            content=str(row[12]),
            tags=tuple(str(tag) for tag in tags),
            context=str(row[14]) if row[14] is not None else None,
            source_hash=str(row[15]),
            anchor=str(row[16]) if row[16] is not None else None,
            line=int(row[17]),
            end_line=int(row[18]),
            fingerprint=str(row[19]) if row[19] is not None else None,
            source_order=int(row[20]),
            lexical_score=float(row[21]) if row[21] is not None else None,
        )

    @staticmethod
    def _clause_predicate(clauses: tuple) -> tuple[str, list[object]]:
        """Branch-preserving DNF over the semantic-unit category/kind axes.

        Each ``IndexCandidateClause`` becomes a same-row conjunction of its
        constrained axes (``u.category IN (...) AND u.kind IN (...)``); the
        clauses are OR-joined, so a cross-product row that matches no single
        branch is never selected. Category and kind seeds are canonicalized
        defensively. An empty positive seed set is preserved as an always-false
        ``0`` predicate rather than being dropped. Shared verbatim by the
        page-level parent query and the unit-level candidate query so both lanes
        evaluate the identical algebra.
        """
        from .semantic_units import canonicalize_category

        clause_sql: list[str] = []
        params: list[object] = []
        for clause in clauses:
            parts: list[str] = []
            for axis, seeds in (
                ("category", clause.category_seeds),
                ("kind", clause.kind_seeds),
            ):
                if seeds is None:
                    continue
                canonical = sorted({canonicalize_category(value) for value in seeds})
                if not canonical:
                    parts.append("0")
                    continue
                placeholders = ",".join("?" for _ in canonical)
                parts.append(f"u.{axis} IN ({placeholders})")
                params.extend(canonical)
            clause_sql.append("(" + " AND ".join(parts) + ")" if parts else "0")
        predicate = " OR ".join(clause_sql) if clause_sql else "0"
        return predicate, params

    def search_semantic_parent_paths(
        self,
        clauses: tuple,
        scope: str,
        freshness: tuple | None,
    ) -> list[str] | None:
        """Distinct candidate parent identities for a planner clause disjunction.

        Consults `catalog_readiness` first (never `_ensure_synced`), so this
        foreground path can neither rebuild, heal, nor walk the corpus; an
        incomplete projection returns ``None``. Readiness and the query are bound
        to one connection/read transaction so a concurrent publication cannot swap
        the catalog between the proof and the query. Emits ONE parameterized
        normal-table query and returns only strings — no unit payload columns,
        no `SemanticUnitLexicalHit`, no tag JSON, no `_semantic_unit_hit`.
        """
        result = self.search_semantic_parent_paths_result(clauses, scope, freshness)
        return result.value if result.readiness.complete else None

    def search_semantic_parent_paths_result(
        self,
        clauses: tuple,
        scope: str,
        freshness: tuple | None,
    ) -> CatalogQueryResult:
        """Typed distinct-parent exact catalog query."""
        return self._serve_from_ready_catalog_result(
            scope,
            freshness,
            lambda conn: self._semantic_parent_paths_query(conn, clauses, scope),
            "lexical semantic-parent sidecar failed (%s); candidate seeding degrades",
        )

    def _semantic_parent_paths_query(
        self,
        conn: sqlite3.Connection,
        clauses: tuple,
        scope: str,
    ) -> list[str]:
        """Sorted distinct candidate parents for the clause disjunction.

        The candidate predicate is a branch-preserving OR of clauses, each
        AND-ing its category and/or kind `$in` values on the same semantic-unit
        row. A `matched` CTE names those parents once; the result is the union of
        matching `semantic_units.parent_path` and every in-scope `pages.path`
        whose `emitted_parent_path` is one of them (scene-frame expansion). Both
        arms are confined to the requested scope.
        """
        col = "in_vault" if scope == "vault" else "in_kb"
        predicate, params = self._clause_predicate(clauses)
        rows = conn.execute(
            "WITH matched AS ("
            " SELECT DISTINCT u.parent_path AS parent_path FROM semantic_units u "
            f"WHERE u.{col} = 1 AND (" + predicate + ")"
            ") "
            "SELECT parent_path FROM matched "
            "UNION "
            f"SELECT p.path FROM pages p WHERE p.{col} = 1 "
            "AND p.emitted_parent_path IN (SELECT parent_path FROM matched)",
            params,
        ).fetchall()
        return sorted(str(row[0]) for row in rows)

    def search_substring(
        self,
        tokens: list[str],
        scope: str,
        freshness: tuple | None,
        repair: bool = True,
    ) -> list[str] | None:
        if self._failed:
            return None
        if not repair and not self.path.exists():
            _schedule_repair(self.vault_root)
            return None
        try:
            return self._serve_synced_live_catalog(
                scope,
                freshness,
                repair=repair,
                query_fn=lambda conn: self._substring_query(conn, tokens, scope),
            )
        except sqlite3.Error as e:
            self._note_query_failure(
                e,
                "lexical sidecar failed (%s); this process serves the in-process lexical paths",
            )
            return None

    def _substring_query(
        self, conn: sqlite3.Connection, tokens: list[str], scope: str
    ) -> list[str]:
        """Exact keyword contract: trigram MATCH narrows (tokens >= 3 chars),
        then instr() verifies EVERY token against the stored raw text — the
        verification is what parity rests on; MATCH is only the accelerator.
        Needles under the trigram floor rely on the verification alone."""
        col = "in_vault" if scope == "vault" else "in_kb"
        clauses: list[str] = [f"p.{col} = 1", "p.is_nav = 0"]
        params: list[object] = []
        long_tokens = [t for t in tokens if len(t) >= 3]
        if long_tokens:
            match = " AND ".join('"' + t.replace('"', '""') + '"' for t in long_tokens)
            clauses.insert(0, "tri MATCH ?")
            params.insert(0, match)
        for t in tokens:
            clauses.append("(instr(tri.title_lower, ?) > 0 OR instr(tri.body_lower, ?) > 0)")
            params.extend((t, t))
        rows = conn.execute(
            "SELECT p.path FROM tri JOIN pages p ON p.rowid = tri.rowid "
            "WHERE " + " AND ".join(clauses) + " "
            "ORDER BY p.updated DESC, p.path DESC",
            params,
        ).fetchall()
        return [r[0] for r in rows]
