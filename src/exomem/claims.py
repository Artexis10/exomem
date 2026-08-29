"""Claim-level hygiene: sharpen contradiction detection from PROXIMITY to POLARITY.

Today's contradiction signal (`corpus_aware.detect_contradictions`,
`audit.corpus_contradictions`) is a PROXIMITY band: two pages whose chunk
embeddings sit in `[floor, dup_threshold)` are "close enough to restate, refine,
OR contradict" — the cosine can't tell agreement from contradiction. This module
adds the missing axis: a **claim-level polarity check** on the specific pairs the
proximity band already flagged.

The design keeps these constraints:
- **No mandatory inline syntax.** Claims are extracted from the claim-bearing
  sections exomem pages *already* have (`## Claim`, `## Conclusion`, `## Decision`)
  with an H1 + lead-paragraph fallback, so the user is never asked to write in a
  special format. Extraction is deterministic and section-based (no LLM required
  for v1); an optional LLM-distillation path can slot in behind
  `extract_claim_text` later without changing callers.
- **No relational/graph DB.** Extracted claims + their bge embeddings live in a
  checksum-keyed per-machine sqlite sidecar (`.claims.sqlite`) modeled EXACTLY on
  `embeddings.EmbeddingIndex` / `.embeddings.sqlite` (WAL pragmas, incremental
  upsert, per-vault memo). It reuses the existing bge model via `embeddings` — no
  new model, no new service.

Everything here is OFF by default and gated behind `EXOMEM_CLAIM_LEVEL=1`. With
the gate unset, `claim_level_enabled()` is False, no sidecar is created, no
polarity is computed, and every wired surface (`corpus_aware.overlap_warning`,
`audit.corpus_contradictions`) is byte-identical to its pre-feature behavior.

STATUS (first increment — needs owner review before production):
- Extraction, the `.claims.sqlite` sidecar, and the deterministic-heuristic
  polarity backend are REAL and unit-tested.
- The NLI cross-encoder backend (`EXOMEM_CLAIM_POLARITY_NLI=1`) is a wired-but-
  UNVERIFIED seam: it will lazily load a local cross-encoder if one is present and
  fall back to the heuristic on any failure. It has not been run against a real
  model in this environment.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np

from . import embeddings, index_paths, reserved_paths, semantic_units, sidecar_store
from .kbdir import kb_dirname

log = logging.getLogger(__name__)


def _sqlite_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
    with reserved_paths._subsystem_authority_scope("claims"):
        return _sqlite_connect_owned(database, *args, **kwargs)


def _sqlite_connect_owned(
    database: Any, *args: Any, **kwargs: Any
) -> sqlite3.Connection:
    return sqlite3.connect(database, *args, **kwargs)


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


def claim_level_enabled() -> bool:
    """Master gate for the whole claim-level subsystem (`EXOMEM_CLAIM_LEVEL`).

    Default OFF. When unset, no `.claims.sqlite` is written and no polarity is
    computed, so every wired surface stays byte-identical to its baseline.
    """
    return bool(os.environ.get("EXOMEM_CLAIM_LEVEL"))


def _max_polarity_pairs() -> int:
    """Hard cap on polarity checks per call (`EXOMEM_CLAIM_POLARITY_MAX_PAIRS`).

    Bounds the lane the way the reranker bounds its `(query, passage)` batch —
    the proximity band can flag many pairs, and each polarity check is real work
    (a heuristic pass now, an NLI forward pass under the optional backend). Pairs
    beyond the cap flow through UNREFINED (polarity stays None) rather than being
    dropped. Default 20; bad values log + fall back.
    """
    raw = os.environ.get("EXOMEM_CLAIM_POLARITY_MAX_PAIRS")
    if raw is None:
        return 20
    try:
        v = int(raw)
        return v if v > 0 else 20
    except ValueError:
        log.warning("invalid EXOMEM_CLAIM_POLARITY_MAX_PAIRS=%r; using 20", raw)
        return 20


# ---------------------------------------------------------------------------
# Claim extraction (deterministic, section-based — no LLM required for v1)
# ---------------------------------------------------------------------------

# Cap on the claim body carried into the embedding/polarity check. A claim is a
# single conclusion, not a whole section — keep it tight so the vector and the
# lexical overlap focus on the assertion, not the supporting prose.
CLAIM_MAX_WORDS = 120

_HEADER_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")

# Per "claim kind" (resolved from a page's type / entity_type), the H2 section(s)
# that carry the page's CONCLUSION, in priority order. These are the sections
# exomem pages already have (page-types.md) — nothing new is imposed on the user.
# A kind absent here, or a page missing its preferred section, falls back to the
# union scan + H1/lead-paragraph in `extract_claim_text`, so extraction NEVER
# requires a particular section to be present.
CLAIM_SECTIONS: dict[str, list[str]] = {
    "insight": ["claim"],
    "experiment": ["conclusion"],
    "decision": ["decision", "summary"],  # entity + entity_type: decision
    "pattern": ["solution", "problem"],
    "failure": ["mechanism", "what happened"],
    "research-note": ["summary", "claim", "conclusion"],
    "production-log": ["summary"],
    "entity": ["summary"],
}

# The union of every claim-bearing header, used when the page's kind is unknown
# (e.g. a draft at write time, where only title+body are in hand). Priority order.
_ANY_CLAIM_HEADERS: list[str] = [
    "claim", "conclusion", "decision", "solution", "summary", "mechanism",
    "what happened", "problem",
]


def _claim_kind(page_type: str | None, entity_type: str | None) -> str | None:
    """Resolve the CLAIM_SECTIONS key. A `type: entity, entity_type: decision`
    page is a lightweight ADR whose conclusion lives under `## Decision`."""
    if page_type == "entity" and (entity_type or "").lower() == "decision":
        return "decision"
    return page_type


def _split_sections(body: str) -> tuple[str, dict[str, str]]:
    """Split a page body into `(h1_title, {h2+_header_lower: text})`.

    Header text is normalized to lowercase/stripped for matching. Section text is
    everything up to the next header of any level, trimmed. The H1 is returned
    separately (it's the page's own claim-as-a-title). Pure/deterministic; no
    dependency on the markdown flavor beyond ATX `#` headers.
    """
    h1 = ""
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []

    def _flush() -> None:
        if current is not None and current not in sections:
            sections[current] = "\n".join(buf).strip()

    for line in (body or "").splitlines():
        m = _HEADER_RE.match(line)
        if m:
            _flush()
            buf = []
            level = len(m.group(1))
            header = m.group(2).strip()
            if level == 1 and not h1:
                h1 = header
                current = None
            else:
                current = header.lower()
        else:
            if current is not None:
                buf.append(line)
    _flush()
    return h1, sections


def _cap_words(text: str, limit: int = CLAIM_MAX_WORDS) -> str:
    words = text.split()
    return text if len(words) <= limit else " ".join(words[:limit])


def _first_paragraph(sections_absent_body: str) -> str:
    """First non-heading, non-empty paragraph of a body — the H1/lead fallback
    (the same shape `demo._excerpt` uses)."""
    para: list[str] = []
    for line in sections_absent_body.splitlines():
        s = line.strip()
        if s.startswith("#"):
            if para:
                break
            continue
        if not s:
            if para:
                break
            continue
        para.append(s)
    return " ".join(para).strip()


def extract_claim_text(
    title: str,
    body: str,
    *,
    page_type: str | None = None,
    entity_type: str | None = None,
) -> str | None:
    """Extract a page's CLAIM as `"{title}\\n\\n{claim body}"` (or None).

    Deterministic and section-based:
    1. Resolve the claim-bearing section from the page kind (`CLAIM_SECTIONS`);
       when the kind is unknown (a write-time draft), scan the union of known
       claim headers in priority order.
    2. Fall back to the H1 + first lead paragraph when no claim section is
       present — so a page in ANY writing shape still yields a claim and the user
       is never forced into a section layout.
    Title is always prepended (mirrors `embeddings.chunk_text`) so the claim
    carries its own topic. Returns None only when there is no usable text at all.

    SEAM: an optional future LLM-distillation path replaces the body-selection
    here (title + a distilled one-sentence claim) WITHOUT changing any caller —
    the return contract (a short claim string, or None) stays the same.
    """
    title = (title or "").strip()
    document = semantic_units.parse_semantic_units(body or "", validate=False)
    semantic_claim = next(
        (
            unit.body
            for unit in document.rich_units
            if unit.kind == "claim" and unit.body and unit.body.strip()
        ),
        None,
    )
    if semantic_claim:
        claim_body = _cap_words(semantic_claim.strip())
        if title and claim_body:
            return f"{title}\n\n{claim_body}"
        return title or claim_body or None

    _, sections = _split_sections(body or "")

    kind = _claim_kind(page_type, entity_type)
    preferred = CLAIM_SECTIONS.get(kind) if kind else None
    header_order = list(preferred or []) + [
        h for h in _ANY_CLAIM_HEADERS if not preferred or h not in preferred
    ]

    claim_body = ""
    for header in header_order:
        text = sections.get(header)
        if text:
            claim_body = text
            break

    if not claim_body:
        claim_body = _first_paragraph(body or "")

    claim_body = _cap_words(claim_body.strip())
    if title and claim_body:
        return f"{title}\n\n{claim_body}"
    return title or claim_body or None


def extract_claim_for_page(page) -> str | None:
    """`extract_claim_text` for a `find.ParsedPage` (pulls type/entity_type)."""
    return extract_claim_text(
        page.title,
        page.body,
        page_type=page.page_type,
        entity_type=page.frontmatter.get("entity_type"),
    )


def _checksum(claim_text: str) -> str:
    """Content key for the sidecar: sha256 of the extracted claim text.

    Keying on the CLAIM (not the file mtime or whole body) is the point — an edit
    that leaves the claim untouched (fix a typo in supporting prose, add a link)
    does not churn the claim embedding, and a claim that genuinely changed always
    does. This is what makes the sidecar "recomputed only when the claim changes".
    """
    return hashlib.sha256(claim_text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Sidecar: .claims.sqlite  (modeled on embeddings.EmbeddingIndex)
# ---------------------------------------------------------------------------


def sidecar_path(vault_root: Path) -> Path:
    """Per-machine claim sidecar. Same dotfile placement rules as
    `.embeddings.sqlite`: outside `_Schema/`, ignored by Obsidian Sync, never
    bundled into a schema upload, rebuildable from the markdown source of truth."""
    from . import state_paths

    return state_paths.vault_state_dir(vault_root) / ".claims.sqlite"


# Only compiled CONCLUSIONS carry a claim worth comparing — mirror the exact
# scope `corpus_aware.detect_contradictions` and `audit` already use so a raw
# source never enters the claim store.
def _claim_types() -> frozenset[str]:
    from . import find as find_module

    return find_module._COMPILED_TYPES


class _ClaimCache(NamedTuple):
    """ClaimIndex's in-memory matrix cache — mirrors `embeddings._EmbCache`.
    `(epoch, generation, instance)` is the write token; `mtime` is retained only
    for the gen==0 legacy fallback. metadata[i] = (file_path, claim_text,
    page_type, status); matrix[i] = its bge vector."""

    epoch: int
    generation: int
    instance: int
    mtime: float
    metadata: list[tuple[str, str, str | None, str | None]]
    matrix: np.ndarray


class ClaimIndex:
    """Per-vault sqlite sidecar holding ONE claim vector per compiled page.

    The lighter cousin of `EmbeddingIndex`: one row per file (not per chunk), so
    the matrix is small — every local write NULLS the cache and the next read does
    one full reload (no copy-on-write splice; there is no `_patch_cache`). Same
    durability contract otherwise — WAL pragmas via
    `sidecar_store.apply_sidecar_pragmas`, incremental checksum-keyed upsert,
    process-shared memo.

    `all_claims()` is cached and invalidated by the same in-band WRITE GENERATION
    the chunk/image indexes use (a `meta` row bumped inside every write's own
    transaction, read via the shared `embeddings._*_token` helpers), NOT the
    sidecar mtime: the sidecar is WAL sqlite, so a commit does not move the main
    file's mtime while a checkpoint does — at a moment no writer runs — making
    mtime keying both spuriously miss and go stale. Third occurrence of this class
    in the repo (after EmbeddingIndex/ClipIndex, PR #125); precedent + rationale:
    the generation-meta note in `embeddings`.
    """

    def __init__(self, vault_root: Path):
        self.vault_root = vault_root
        self.path = sidecar_path(vault_root)
        self._cache: _ClaimCache | None = None
        self._lock = threading.RLock()

    def _connect(self, path: Path | None = None) -> sqlite3.Connection:
        with reserved_paths._subsystem_authority_scope("claims"):
            with reserved_paths._identity_coordination_scope(
                self.vault_root,
                descriptor_ids=("claims-store",),
            ):
                return self._connect_owned(path)

    def _connect_owned(self, path: Path | None = None) -> sqlite3.Connection:
        target = path if path is not None else self.path
        if target == self.path:
            with reserved_paths._sqlite_owner_target_scope(
                self.vault_root,
                target,
                "claims-store",
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
            CREATE TABLE IF NOT EXISTS claims (
                file_path  TEXT NOT NULL PRIMARY KEY,
                claim_text TEXT NOT NULL,
                checksum   TEXT NOT NULL,
                vector     BLOB NOT NULL,
                page_type  TEXT,
                status     TEXT,
                file_mtime REAL NOT NULL
            )
            """
        )
        sidecar_store.ensure_meta_table(conn, "claims", self.path.name)
        # A sidecar becomes globally readable only after ``replace_all`` binds
        # it to the current recall/access identity. Incremental rows may be
        # useful for live fallback repair, but must not attest completeness.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS recall_identity ("
            "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
            "policy_version TEXT NOT NULL, access_fingerprint TEXT NOT NULL, "
            "complete INTEGER NOT NULL)"
        )
        if target == self.path:
            try:
                reserved_paths._publish_sqlite_owner_family(
                    self.vault_root,
                    target,
                    "claims-store",
                    conn,
                )
            except BaseException:
                conn.close()
                raise
        return conn

    def checksums(self) -> dict[str, str]:
        """`{file_path: checksum}` — the incremental-skip map for a re-index."""
        if not self.path.exists():
            return {}
        conn = self._connect()
        try:
            rows = conn.execute("SELECT file_path, checksum FROM claims").fetchall()
        finally:
            conn.close()
        return {fp: cs for fp, cs in rows}

    def get_row(
        self, file_path: str
    ) -> tuple[str, np.ndarray, str | None, str | None] | None:
        """`(claim_text, vector, page_type, status)` for one file, or None."""
        from . import recall_policy

        # A point lookup is an egress surface too. Reject before opening the
        # claims sidecar so legacy Record rows cannot escape through callers
        # that bypass ``claim_text_for_page``.
        candidate = self.vault_root / file_path
        if candidate.exists() and not recall_policy.is_recall_candidate(self.vault_root, candidate):
            return None
        if self._recall_identity_current() is False:
            return None
        return self._get_row_unchecked(file_path)

    def _get_row_unchecked(
        self, file_path: str
    ) -> tuple[str, np.ndarray, str | None, str | None] | None:
        """Raw sidecar lookup for maintenance/tests; never use for egress."""
        if not self.path.exists():
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT claim_text, vector, page_type, status FROM claims "
                "WHERE file_path = ?",
                (file_path,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return row[0], np.frombuffer(row[1], dtype=np.float32), row[2], row[3]

    def upsert_many(
        self,
        rows: list[tuple[str, str, str, np.ndarray, str | None, str | None, float]],
    ) -> None:
        """Insert/replace claim rows in ONE transaction.

        Each row is `(file_path, claim_text, checksum, vector, page_type, status,
        mtime)`. `file_path` is the PK, so `INSERT OR REPLACE` cleanly overwrites a
        changed claim. Bumps the in-band write generation INSIDE the txn (so the
        cache invalidates on content, not mtime) and drops the cache (small matrix
        → a single full reload is cheaper than a splice).
        """
        if not rows:
            return
        conn = self._connect()
        try:
            with conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO claims "
                    "(file_path, claim_text, checksum, vector, page_type, status, file_mtime) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (fp, ct, cs, vec.astype(np.float32).tobytes(), pt, st, mt)
                        for fp, ct, cs, vec, pt, st, mt in rows
                    ],
                )
                self._invalidate_recall_identity(conn)
                sidecar_store.bump_meta(conn, "generation")
        finally:
            conn.close()
        with self._lock:
            self._cache = None

    def delete(self, file_path: str) -> None:
        self.delete_many([file_path])

    def delete_many(
        self, file_paths: list[str], *, connection_path: Path | None = None
    ) -> int:
        """Delete claim rows without consulting model or feature gates.

        This is deliberately safe for generic delete routing: an absent sidecar
        remains absent, duplicate paths are harmless, and a no-op delete does
        not manufacture a generation change.
        """
        target = connection_path if connection_path is not None else self.path
        if not target.exists():
            return 0
        paths = sorted(set(file_paths))
        if not paths:
            return 0
        deleted = 0
        conn = self._connect(target)
        try:
            with conn:
                for start in range(0, len(paths), 900):
                    batch = paths[start : start + 900]
                    placeholders = ",".join("?" for _ in batch)
                    cursor = conn.execute(
                        f"DELETE FROM claims WHERE file_path IN ({placeholders})", batch
                    )
                    deleted += max(0, int(cursor.rowcount))
                if deleted:
                    self._invalidate_recall_identity(conn)
                    sidecar_store.bump_meta(conn, "generation")
        finally:
            conn.close()
        if deleted:
            with self._lock:
                self._cache = None
        return deleted

    def purge_exact_persisted_rows(
        self, values: list[str], *, connection_path: Path | None = None
    ) -> int:
        """Delete quarantined stored values without interpreting them as paths."""
        return self.delete_many(values, connection_path=connection_path)

    def _all_claims_unchecked(
        self,
    ) -> tuple[list[tuple[str, str, str | None, str | None]], np.ndarray]:
        """Raw cached matrix for sidecar maintenance internals.

        Ordinary recall consumers must use :meth:`all_claims`, which admits
        source paths before looking up claim payloads.
        """
        # `(metadata, matrix)` is cached until the sidecar's write generation
        # advances, not its mtime. `metadata[i]` is `(file_path, claim_text,
        # page_type, status)` and `matrix[i]` is the claim vector.
        if not self.path.exists():
            return [], np.zeros((0, embeddings.VECTOR_DIM), dtype=np.float32)
        # Snapshot the cache tuple ONCE: another thread may swap or null it between
        # reads. This fast path takes no lock — the common case.
        c = self._cache
        served = sidecar_store.try_serve_cached(c, self.path)
        if served is not None:
            return served.metadata, served.matrix
        with self._lock:
            # Re-check under the lock: another thread may have loaded while we
            # waited, or the fast-path token read may have failed transiently.
            c = self._cache
            served = sidecar_store.try_serve_cached(c, self.path)
            if served is not None:
                return served.metadata, served.matrix
            loaded = self._load_all_rows()
            log.info(
                "claim matrix full load: reason=%s rows=%d gen=%d epoch=%d",
                sidecar_store.reload_reason(c, loaded.epoch, loaded.generation),
                len(loaded.metadata), loaded.generation, loaded.epoch,
            )
            self._cache = loaded
            return loaded.metadata, loaded.matrix

    def all_claims(
        self,
    ) -> tuple[list[tuple[str, str, str | None, str | None]], np.ndarray]:
        """Return only currently admitted claim rows without raw-sidecar egress."""
        from . import find as find_module
        from . import recall_policy

        if not self.path.exists() or self._recall_identity_current() is False:
            return [], np.zeros((0, embeddings.VECTOR_DIM), dtype=np.float32)
        kb = self.vault_root / kb_dirname()
        if not kb.is_dir():
            return [], np.zeros((0, embeddings.VECTOR_DIM), dtype=np.float32)
        admitted = [
            _vault_relative(self.vault_root, path)
            for path in find_module._walk_md(kb)
            if index_paths.is_embeddable_path(path)
            and recall_policy.is_recall_candidate(self.vault_root, path)
        ]
        paths = [path for path in admitted if path is not None]
        if not paths:
            return [], np.zeros((0, embeddings.VECTOR_DIM), dtype=np.float32)
        metadata: list[tuple[str, str, str | None, str | None]] = []
        vectors: list[np.ndarray] = []
        conn = self._connect()
        try:
            for start in range(0, len(paths), 900):
                batch = paths[start : start + 900]
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    "SELECT file_path, claim_text, page_type, status, vector FROM claims "
                    f"WHERE file_path IN ({placeholders}) ORDER BY file_path",
                    batch,
                ).fetchall()
                for fp, claim, page_type, status, vector in rows:
                    metadata.append((fp, claim, page_type, status))
                    vectors.append(np.frombuffer(vector, dtype=np.float32))
        finally:
            conn.close()
        if not vectors:
            return [], np.zeros((0, embeddings.VECTOR_DIM), dtype=np.float32)
        return metadata, np.stack(vectors, axis=0)

    def _recall_identity_current(self) -> bool | None:
        """Whether a bound full rebuild remains valid; ``None`` is legacy.

        Incremental work deliberately marks a prior full rebuild incomplete: it
        may keep point rows current, but cannot attest that an entire corpus is
        globally complete. Reads then fail closed until a new full rebuild.
        """
        if not self.path.exists():
            return False
        try:
            conn = self._connect_readonly()
        except (OSError, RuntimeError, sqlite3.Error):
            return False
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recall_identity'"
            ).fetchone()
            if exists is None:
                return None
            row = conn.execute(
                "SELECT policy_version, access_fingerprint, complete "
                "FROM recall_identity WHERE singleton = 1"
            ).fetchone()
        except sqlite3.Error:
            return False
        finally:
            conn.close()
        if row is None:
            return False
        from . import recall_policy

        return bool(row[2]) and (str(row[0]), str(row[1])) == recall_policy.recall_policy_identity(
            self.vault_root
        )

    def _connect_readonly(self) -> sqlite3.Connection:
        with reserved_paths._subsystem_authority_scope("claims"):
            with reserved_paths._identity_coordination_scope(
                self.vault_root,
                descriptor_ids=("claims-store",),
                identity_may_change=False,
            ):
                with reserved_paths._sqlite_owner_target_scope(
                    self.vault_root,
                    self.path,
                    "claims-store",
                    create=False,
                ) as retained_path:
                    conn = _sqlite_connect_owned(
                        f"{retained_path.as_uri()}?mode=ro",
                        uri=True,
                    )
                    try:
                        reserved_paths._publish_sqlite_owner_family(
                            self.vault_root,
                            self.path,
                            "claims-store",
                            conn,
                        )
                        return conn
                    except BaseException:
                        conn.close()
                        raise

    @staticmethod
    def _invalidate_recall_identity(conn: sqlite3.Connection) -> bool:
        """Keep a partial write from masquerading as a complete rebuild."""
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recall_identity'"
        ).fetchone()
        if exists is not None:
            cursor = conn.execute("UPDATE recall_identity SET complete = 0 WHERE singleton = 1")
            return bool(cursor.rowcount)
        return False

    def _projected_recall_snapshot(
        self,
    ) -> tuple[tuple[str, str], tuple[tuple[str, tuple[int, int, int]], ...]] | None:
        """Direct, no-parse snapshot of every currently admitted KB page.

        The completeness marker describes the projected recall corpus, not just
        pages that happened to produce a claim. Suppressed Records are rejected
        by policy before a stat or parse; any unreadable admitted page makes the
        snapshot unfit for a complete publication.
        """
        from . import find as find_module
        from . import freshness, recall_policy

        kb = self.vault_root / kb_dirname()
        if not kb.is_dir():
            return (recall_policy.recall_policy_identity(self.vault_root), ())
        identity = recall_policy.recall_policy_identity(self.vault_root)
        entries: list[tuple[str, freshness.FileSignature]] = []
        for md in find_module._walk_md(kb):
            if not index_paths.is_embeddable_path(md):
                continue
            if not recall_policy.is_recall_candidate(self.vault_root, md):
                continue
            rel = _vault_relative(self.vault_root, md)
            if rel is None:
                return None
            try:
                entries.append((rel, freshness.stat_signature(md)))
            except OSError:
                return None
        if recall_policy.recall_policy_identity(self.vault_root) != identity:
            return None
        return identity, tuple(sorted(entries))

    def _mark_repair_needed(self) -> None:
        """Fail closed if a rebuild snapshot is overtaken before publication."""
        if not self.path.exists():
            return
        conn = self._connect()
        try:
            with conn:
                if self._invalidate_recall_identity(conn):
                    sidecar_store.bump_meta(conn, "generation")
        finally:
            conn.close()
        with self._lock:
            self._cache = None

    def _load_all_rows(self) -> _ClaimCache:
        """Full reload from the sidecar → a `_ClaimCache`.

        Reads the meta token AND the rows inside ONE explicit `BEGIN` so they are a
        single consistent snapshot (python sqlite3 runs each bare SELECT in its own
        snapshot in autocommit, so a naive two-statement read could pair a
        generation with rows from a different write — mirrors
        `EmbeddingIndex._load_all_rows`). Kept a named method so tests can count
        genuine full reloads.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            try:
                epoch, gen, instance = sidecar_store.read_meta_token(conn)
                rows = conn.execute(
                    "SELECT file_path, claim_text, page_type, status, vector FROM claims "
                    "ORDER BY file_path"
                ).fetchall()
            finally:
                conn.rollback()  # read-only txn — release the snapshot
        finally:
            conn.close()
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = 0.0
        if not rows:
            return _ClaimCache(
                epoch, gen, instance, mtime, [],
                np.zeros((0, embeddings.VECTOR_DIM), dtype=np.float32),
            )
        metadata: list[tuple[str, str, str | None, str | None]] = []
        vectors: list[np.ndarray] = []
        for fp, ct, pt, st, blob in rows:
            metadata.append((fp, ct, pt, st))
            vectors.append(np.frombuffer(blob, dtype=np.float32))
        return _ClaimCache(epoch, gen, instance, mtime, metadata, np.stack(vectors, axis=0))

    def rebuild_all(self) -> int:
        """Wipe + re-extract/re-embed a claim for every compiled page. Returns the
        row count. The recovery path (mirrors `EmbeddingIndex.rebuild_all`) for a
        lost/stale sidecar; safe to call from an audit-fix lane."""
        from . import find as find_module
        from . import freshness, recall_policy

        kb = self.vault_root / kb_dirname()
        if not kb.is_dir():
            return 0
        claim_types = _claim_types()
        snapshot = self._projected_recall_snapshot()
        if snapshot is None:
            self._mark_repair_needed()
            return 0
        identity, _entries = snapshot
        pending: list[
            tuple[Path, freshness.FileSignature, str, str, str, str | None, str | None, float]
        ] = []
        for md in find_module._walk_md(kb):
            if not index_paths.is_embeddable_path(md):
                continue
            # Admission must precede parsing: raw Records may be high-volume,
            # malformed, or sensitive, and never belong to claim extraction.
            if not recall_policy.is_recall_candidate(self.vault_root, md):
                continue
            try:
                signature = freshness.stat_signature(md)
            except OSError:
                continue
            page = find_module._CACHE.get(md, self.vault_root)
            if page is None or page.page_type not in claim_types:
                continue
            claim = extract_claim_for_page(page)
            if not claim:
                continue
            pending.append(
                (
                    md,
                    signature,
                    page.rel_path,
                    claim,
                    _checksum(claim),
                    page.page_type,
                    page.status,
                    page.mtime,
                )
            )
        vecs = (
            embeddings.embed_texts([p[3] for p in pending], is_query=False)
            if pending
            else np.zeros((0, embeddings.VECTOR_DIM), dtype=np.float32)
        )
        # The post-encode direct projection check catches all corpus changes,
        # including an admitted page that was absent/non-claim during the first
        # scan. Never leave an old complete marker behind on that race.
        if self._projected_recall_snapshot() != snapshot:
            self._mark_repair_needed()
            return 0
        self.replace_all(
            [
                (fp, claim, checksum, vecs[index], page_type, status, mtime)
                for index, (_md, _signature, fp, claim, checksum, page_type, status, mtime) in enumerate(pending)
            ],
            identity=identity,
        )
        return len(pending)

    def replace_all(
        self,
        rows: list[tuple[str, str, str, np.ndarray, str | None, str | None, float]],
        *,
        identity: tuple[str, str],
    ) -> None:
        """Atomically publish a complete, policy-bound rebuild."""
        conn = self._connect()
        try:
            with conn:
                conn.execute("DELETE FROM claims")
                if rows:
                    conn.executemany(
                        "INSERT INTO claims "
                        "(file_path, claim_text, checksum, vector, page_type, status, file_mtime) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [
                            (fp, ct, cs, vec.astype(np.float32).tobytes(), pt, st, mt)
                            for fp, ct, cs, vec, pt, st, mt in rows
                        ],
                    )
                conn.execute(
                    "INSERT OR REPLACE INTO recall_identity "
                    "(singleton, policy_version, access_fingerprint, complete) VALUES (1, ?, ?, 1)",
                    identity,
                )
                sidecar_store.bump_meta(conn, "generation")
        finally:
            conn.close()
        with self._lock:
            self._cache = None


_CLAIM_INDEX_CACHE: dict[str, ClaimIndex] = {}
_CLAIM_INDEX_CACHE_LOCK = threading.Lock()


def get_claim_index(vault_root: Path) -> ClaimIndex:
    """Process-shared `ClaimIndex` for this vault (see `get_embedding_index`)."""
    key = str(Path(vault_root).resolve())
    with _CLAIM_INDEX_CACHE_LOCK:
        idx = _CLAIM_INDEX_CACHE.get(key)
        if idx is None:
            idx = ClaimIndex(vault_root)
            _CLAIM_INDEX_CACHE[key] = idx
        return idx


def clear_claim_indexes() -> None:
    """Drop the shared claim-index memo (test hook; mirrors
    `embeddings.clear_embedding_indexes`)."""
    with _CLAIM_INDEX_CACHE_LOCK:
        _CLAIM_INDEX_CACHE.clear()


def _vault_relative(vault_root: Path, path: Path | str) -> str | None:
    """Return a lexical vault-relative path without resolving a missing target."""
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = vault_root / candidate
    try:
        return candidate.relative_to(vault_root).as_posix()
    except ValueError:
        return None


def _delete_claim_rows_if_present(vault_root: Path, rel_paths: list[str]) -> int:
    """Model-free stale-row cleanup that never creates a sidecar."""
    if not rel_paths or not sidecar_path(vault_root).exists():
        return 0
    return ClaimIndex(vault_root).delete_many(rel_paths)


def delete_after_remove(vault_root: Path, removed_rel_paths: list[str]) -> bool:
    """Generic delete-routing seam: purge claim rows regardless of feature gates."""
    _delete_claim_rows_if_present(vault_root, removed_rel_paths)
    return True


def upsert_claims_after_write(vault_root: Path, written_paths: list[Path]) -> None:
    """Refresh the claim sidecar for each written compiled page (incremental).

    Rides the SAME write seam as `embeddings.upsert_after_write` (called from it
    when the gate is on), so every existing writer keeps the claim sidecar current
    with no per-writer changes. Checksum-keyed: a page whose extracted claim is
    unchanged is skipped (no re-embed); a non-compiled page (raw source, etc.) has
    any stale claim row dropped. No-op when the gate is off or embeddings are
    disabled/unimportable — the same soft-fail contract the vector sidecar honors.
    """
    from . import find as find_module
    from . import freshness, recall_policy

    md_paths = [p for p in written_paths if index_paths.is_embeddable_path(p)]
    if not md_paths:
        return
    # Delete is deliberately before both feature gates and page parsing. A raw
    # Record must clear an old row even on a lean install where claims/vectors
    # are disabled, and must never be opened just to discover that fact.
    suppressed: list[str] = []
    admitted: list[Path] = []
    for md in md_paths:
        if recall_policy.is_recall_candidate(vault_root, md):
            admitted.append(md)
            continue
        rel = _vault_relative(vault_root, md)
        if rel is not None:
            suppressed.append(rel)
    _delete_claim_rows_if_present(vault_root, suppressed)
    if not claim_level_enabled() or os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        return
    if not admitted:
        return
    idx = get_claim_index(vault_root)
    existing = idx.checksums()
    claim_types = _claim_types()
    identity = recall_policy.recall_policy_identity(vault_root)

    pending: list[
        tuple[Path, freshness.FileSignature, str, str, str, str | None, str | None, float]
    ] = []
    for md in admitted:
        try:
            signature = freshness.stat_signature(md)
        except OSError:
            rel = _vault_relative(vault_root, md)
            if rel is not None:
                idx.delete(rel)
            continue
        page = find_module._CACHE.get(md, vault_root)
        if page is None:
            continue
        # Only compiled conclusions in an indexable tree carry a claim.
        if page.page_type not in claim_types:
            idx.delete(page.rel_path)
            continue
        claim = extract_claim_for_page(page)
        if not claim:
            idx.delete(page.rel_path)
            continue
        checksum = _checksum(claim)
        if existing.get(page.rel_path) == checksum:
            continue  # claim unchanged → keep the cached vector, skip re-embed
        pending.append(
            (
                md,
                signature,
                page.rel_path,
                claim,
                checksum,
                page.page_type,
                page.status,
                page.mtime,
            )
        )

    if not pending:
        return
    try:
        vecs = embeddings.embed_texts([p[3] for p in pending], is_query=False)
    except Exception as e:  # noqa: BLE001 — best-effort; leave the sidecar stale
        log.warning("claim encode failed: %s; claim sidecar left stale", e)
        return
    approved: list[
        tuple[
            int,
            tuple[Path, freshness.FileSignature, str, str, str, str | None, str | None, float],
        ]
    ] = []
    for position, pending_row in enumerate(pending):
        md, signature, rel, *_rest = pending_row
        try:
            current = (
                recall_policy.recall_policy_identity(vault_root) == identity
                and freshness.stat_signature(md) == signature
                and recall_policy.is_recall_candidate(vault_root, md)
            )
        except OSError:
            current = False
        if current:
            approved.append((position, pending_row))
        else:
            # A raced path is unsafe to publish; dropping any old row is safer
            # than keeping a claim whose source/version no longer matches.
            idx.delete(rel)
    if not approved:
        return
    idx.upsert_many(
        [
            (fp, ct, cs, vecs[position], pt, st, mt)
            for position, row in approved
            for _md, _signature, fp, ct, cs, pt, st, mt in [row]
        ]
    )


def claim_text_for_page(
    vault_root: Path, rel_path: str, *, index: ClaimIndex | None = None
) -> str | None:
    """Best available claim text for a stored page: the sidecar's cached claim if
    present, else live extraction from the parsed page. Lets the polarity lane work
    even before the sidecar is warm (graceful degradation)."""
    from . import recall_policy

    # Egress begins with the same admission check as all index ingress. This
    # must happen before a stale sidecar lookup or a live page parse.
    if not recall_policy.is_recall_candidate(vault_root, vault_root / rel_path):
        return None
    idx = index or get_claim_index(vault_root)
    row = idx.get_row(rel_path)
    if row and row[0]:
        return row[0]
    from . import find as find_module

    page = find_module._CACHE.get(vault_root / rel_path, vault_root)
    if page is None:
        return None
    return extract_claim_for_page(page)


# ---------------------------------------------------------------------------
# Polarity check  (contradict / refine / duplicate / unrelated)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolarityResult:
    """One polarity verdict for a claim pair.

    `label` ∈ {contradict, refine, duplicate, unrelated}. `score` is a coarse
    [0,1] confidence in the label. `method` names the backend that produced it
    (`heuristic` today, `nli` under the optional cross-encoder path).
    """

    label: str
    score: float
    method: str


# Deterministic-heuristic lexicon. Coarse by design — a lexical stand-in for a
# real NLI model, chosen so v1 is REAL and testable without a model download. The
# owner-review path is to flip on the NLI backend (see `_nli_polarity`).
_STOPWORDS = frozenset(
    """a an the this that these those of for to in on at by with from as is are be
    been being it its and or but if then so than into over under about we you they
    i he she our your their them his her one two use used using via per each any all
    should must can may might will would could do does did done has have had""".split()
)
_NEGATIONS = frozenset(
    """not no never cannot cant can't dont don't doesnt doesn't isnt isn't arent
    aren't wont won't without neither nor fails fail false wrong avoid lacks lack
    unless""".split()
)
_ANTONYM_PAIRS = [
    ("increase", "decrease"), ("increases", "decreases"), ("increase", "reduce"),
    ("increases", "reduces"), ("improve", "degrade"), ("improves", "degrades"),
    ("improved", "degraded"), ("better", "worse"), ("faster", "slower"),
    ("works", "fails"), ("work", "fail"), ("true", "false"), ("more", "less"),
    ("higher", "lower"), ("enable", "disable"), ("enables", "disables"),
    ("help", "hurt"), ("helps", "hurts"), ("gain", "loss"), ("up", "down"),
    ("add", "remove"), ("adds", "removes"), ("positive", "negative"),
    ("win", "lose"), ("succeed", "fail"), ("beneficial", "harmful"),
    ("necessary", "unnecessary"), ("required", "optional"), ("always", "never"),
    ("accept", "reject"), ("include", "exclude"), ("safe", "unsafe"),
]
_ANTONYMS: dict[str, set[str]] = {}
for _a, _b in _ANTONYM_PAIRS:
    _ANTONYMS.setdefault(_a, set()).add(_b)
    _ANTONYMS.setdefault(_b, set()).add(_a)

# Overlap thresholds over content-word Jaccard.
_DUP_JACCARD = 0.80      # near-total topical overlap, same polarity → restatement
_REL_JACCARD = 0.20      # shared enough to be "about the same thing"
_NEG_MIN_JACCARD = 0.40  # negation-only contradiction needs a strong shared topic

_WORD_RE = re.compile(r"[a-z0-9']+")


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


def _content_words(tokens: list[str]) -> set[str]:
    """Topic words: drop stopwords + negation cues so overlap measures SUBJECT,
    not stance (a negated and an asserted claim about the same thing still share
    their topic words)."""
    return {t for t in tokens if t not in _STOPWORDS and t not in _NEGATIONS}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _heuristic_polarity(
    claim_a: str, claim_b: str, *, cosine: float | None = None
) -> PolarityResult:
    """Deterministic lexical polarity: negation-parity + antonym cues over a
    shared-topic gate. REAL and unit-tested (the v1 default backend).

    - **duplicate**: near-total topic overlap, same negation parity, no antonym.
    - **contradict**: same topic AND (an antonym pair spans the two claims, OR one
      claim negates and the other asserts).
    - **refine**: same topic, same stance, but not a restatement (differing detail).
    - **unrelated**: little shared topic and no polarity signal.
    """
    ta, tb = _tokens(claim_a), _tokens(claim_b)
    ca, cb = _content_words(ta), _content_words(tb)
    overlap = _jaccard(ca, cb)

    neg_a = any(t in _NEGATIONS for t in ta)
    neg_b = any(t in _NEGATIONS for t in tb)
    neg_diff = neg_a != neg_b
    antonym_hit = any(w in _ANTONYMS and (_ANTONYMS[w] & cb) for w in ca)

    if overlap >= _DUP_JACCARD and not neg_diff and not antonym_hit:
        return PolarityResult("duplicate", round(overlap, 4), "heuristic")
    if antonym_hit and overlap >= _REL_JACCARD:
        return PolarityResult("contradict", round(min(1.0, 0.5 + overlap / 2), 4), "heuristic")
    if neg_diff and overlap >= _NEG_MIN_JACCARD:
        return PolarityResult("contradict", round(min(1.0, 0.5 + overlap / 2), 4), "heuristic")
    if overlap >= _REL_JACCARD:
        return PolarityResult("refine", round(overlap, 4), "heuristic")
    return PolarityResult("unrelated", round(1.0 - overlap, 4), "heuristic")


def _nli_enabled() -> bool:
    """Opt-in NLI cross-encoder backend (`EXOMEM_CLAIM_POLARITY_NLI`).

    UNVERIFIED in this increment — see module docstring. Default OFF; when unset
    the heuristic is the only backend.
    """
    return bool(os.environ.get("EXOMEM_CLAIM_POLARITY_NLI"))


# The local NLI model name is intentionally an env knob so no specific weight is
# pinned/downloaded by v1. A small entailment cross-encoder (label order
# contradiction/entailment/neutral) is the intended shape.
_NLI_MODEL_ENV = "EXOMEM_CLAIM_NLI_MODEL"
_NLI_MODEL = None
_NLI_LOCK = threading.Lock()
_NLI_IMPORT_FAILED = False


def _get_nli_model():
    """Lazy singleton for the optional NLI cross-encoder (mirrors
    `embeddings.get_reranker`). Returns None if unconfigured/unavailable so the
    caller falls back to the heuristic."""
    global _NLI_MODEL, _NLI_IMPORT_FAILED
    if _NLI_IMPORT_FAILED:
        return None
    if _NLI_MODEL is not None:
        return _NLI_MODEL
    name = os.environ.get(_NLI_MODEL_ENV)
    if not name:
        return None
    with _NLI_LOCK:
        if _NLI_MODEL is not None:
            return _NLI_MODEL
        try:
            from sentence_transformers import CrossEncoder

            from . import accel, model_cache

            device = accel.select_device()
            _NLI_MODEL = model_cache.load_offline_first(
                name,
                lambda **kw: CrossEncoder(name, device=device, **kw),
            )
        except Exception as e:  # noqa: BLE001 — optional path; degrade to heuristic
            log.warning("NLI polarity model unavailable (%s); using heuristic", e)
            _NLI_IMPORT_FAILED = True
            return None
    return _NLI_MODEL


def _nli_polarity(claim_a: str, claim_b: str) -> PolarityResult | None:
    """Optional NLI backend. Returns None (→ heuristic fallback) when the model is
    unconfigured or a forward pass fails. WIRED BUT UNVERIFIED (see module docstring).

    Expected mapping once a model is attached: symmetric contradiction probability
    → `contradict`; high mutual entailment → `duplicate`; one-directional
    entailment → `refine`; neutral → `unrelated`.
    """
    model = _get_nli_model()
    if model is None:
        return None
    try:
        import numpy as _np

        # Score both directions; a real entailment cross-encoder emits 3 logits
        # (contradiction, entailment, neutral). Kept defensive so a differently-
        # shaped model can't crash the write path.
        logits = model.predict([(claim_a, claim_b), (claim_b, claim_a)])
        arr = _np.asarray(logits, dtype=_np.float32)
        if arr.ndim != 2 or arr.shape[1] < 3:
            return None
        probs = _np.exp(arr) / _np.exp(arr).sum(axis=1, keepdims=True)
        contra = float(probs[:, 0].max())
        entail = float(probs[:, 1].min())  # both directions must entail for a dup
        neutral = float(probs[:, 2].mean())
        if contra >= 0.5 and contra >= entail:
            return PolarityResult("contradict", round(contra, 4), "nli")
        if entail >= 0.6:
            return PolarityResult("duplicate", round(entail, 4), "nli")
        if neutral >= 0.5:
            return PolarityResult("unrelated", round(neutral, 4), "nli")
        return PolarityResult("refine", round(max(entail, 1 - contra - neutral), 4), "nli")
    except Exception as e:  # noqa: BLE001 — never break a write on the optional path
        log.debug("NLI polarity failed (%s); falling back to heuristic", e)
        return None


def classify_polarity(
    claim_a: str, claim_b: str, *, cosine: float | None = None
) -> PolarityResult:
    """Polarity of two claims, behind ONE stable interface.

    Dispatches to the optional NLI backend when enabled+available, else the
    deterministic heuristic. This is the seam a better model slots into without
    touching any caller (corpus_aware / audit).
    """
    if _nli_enabled():
        r = _nli_polarity(claim_a, claim_b)
        if r is not None:
            return r
    return _heuristic_polarity(claim_a, claim_b, cosine=cosine)
