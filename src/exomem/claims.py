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
polarity is computed, and `audit.corpus_contradictions` is byte-identical to its
pre-feature behavior.

POLARITY REACHES EXACTLY ONE SURFACE, UNDER ADMISSION CONTROL. The synchronous
write path invokes no polarity classification at all: `corpus_aware` warnings
carry no stance clause, on any gate. The only channel is the asynchronous audit
contradiction sweep, and it enriches only through an ADMITTED frozen verifier —
a pin in `VERIFIER_PINS` (a repository artifact, never runtime configuration)
whose resolved weights match its sha256 digest, whose label map is a version
this build ships, and whose verification fixture set is green at that exact
pair, with `EXOMEM_CLAIM_POLARITY_NLI` set. Anything short of all of that
refuses the verifier, and refusal degrades to ABSENCE: the entry carries no
label. The deterministic lexical heuristic below is retired from queue
enrichment — it had no admission control — and survives only as the comparison
arm of the fixture-set precision table and as `classify_polarity`'s fallback for
callers that are not writing an admitted, provenance-marked label.
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

    ORPHANED as of the frozen-verifier slice and kept only so the knob does not
    change meaning under anyone who set it: its one caller was the write path's
    `_refine_contradictions`, which is gone. The surviving polarity lane is the
    audit sweep, and that lane is bounded by the surfaced set it runs over —
    `EXOMEM_CONTRADICTION_TOP_N` — not by this. See follow-up 6.3 in the
    `add-frozen-stance-verification` change. Default 20; bad values log + fall back.
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
    (`heuristic` from the lexical fallback, `nli` from an admitted frozen
    verifier — and ONLY an admitted one may ever carry the `nli` name).
    """

    label: str
    score: float
    method: str


# Deterministic-heuristic lexicon. Coarse by design — a lexical stand-in for a
# real NLI model, chosen so v1 was REAL and testable without a model download.
# RETIRED from queue enrichment (it has no admission control); it remains the
# comparison arm of `VERIFICATION_FIXTURES` and `classify_polarity`'s fallback.
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


# ---------------------------------------------------------------------------
# Label map v1 — the frozen verifier's logit → label contract (design D4)
# ---------------------------------------------------------------------------

#: The closed label set a verifier may emit. Shared by the heuristic and the
#: frozen verifier so a label can never be minted outside this vocabulary.
POLARITY_LABELS = frozenset({"contradict", "refine", "duplicate", "unrelated"})

#: The label-map version this build ships. A pin names the version it was
#: verified against; changing a threshold, the label set, the column order, or
#: the direction convention bumps this and demands fixture re-verification
#: before the (digest, label-map) pair is admitted again.
LABEL_MAP_VERSION = "v1"


@dataclass(frozen=True)
class LabelMap:
    """A versioned, in-repo mapping from cross-encoder logits to the closed set.

    Data reviewed in diff, not arithmetic buried in code. `columns` declares the
    logit column SEMANTICS AND THEIR ORDER: a model whose head orders
    entailment/contradiction differently is a different (digest, label-map)
    pair and needs its own verified version, so a head whose width does not
    match this declaration is refused rather than coerced.

    `direction` names the aggregation convention over the two orderings of the
    pair: contradiction takes the max (either direction contradicting is a
    contradiction), entailment the min (both directions must entail for a
    restatement), neutral the mean.
    """

    version: str
    columns: tuple[str, ...]
    direction: str
    contradict_min: float
    duplicate_min: float
    unrelated_min: float
    labels: frozenset[str] = POLARITY_LABELS

    def apply(self, logits: Any) -> PolarityResult | None:
        """One verdict from a (2, len(columns)) logit block, or None if refused.

        None means "this head is not the one this map was verified against" —
        the caller degrades to absence, never to another backend's label.
        """
        arr = np.asarray(logits, dtype=np.float32)
        if arr.ndim != 2 or arr.shape[1] != len(self.columns):
            return None
        if arr.shape[0] != 2:
            return None
        if not np.isfinite(arr).all():
            return None
        shifted = arr - arr.max(axis=1, keepdims=True)
        exp = np.exp(shifted)
        probs = exp / exp.sum(axis=1, keepdims=True)
        contra = float(probs[:, 0].max())
        entail = float(probs[:, 1].min())
        neutral = float(probs[:, 2].mean())
        if contra >= self.contradict_min and contra >= entail:
            return PolarityResult("contradict", round(contra, 4), "nli")
        if entail >= self.duplicate_min:
            return PolarityResult("duplicate", round(entail, 4), "nli")
        if neutral >= self.unrelated_min:
            return PolarityResult("unrelated", round(neutral, 4), "nli")
        return PolarityResult(
            "refine", round(max(0.0, min(1.0, max(entail, 1 - contra - neutral))), 4), "nli"
        )


#: Every label map this build can load. A pin may only name a key present here.
_LABEL_MAPS: dict[str, LabelMap] = {
    "v1": LabelMap(
        version="v1",
        columns=("contradiction", "entailment", "neutral"),
        direction="bidirectional:contradiction=max,entailment=min,neutral=mean",
        contradict_min=0.5,
        duplicate_min=0.6,
        unrelated_min=0.5,
    ),
}


def get_label_map(version: str | None) -> LabelMap:
    """The label map a pin names, or refuse.

    An unversioned or unknown map is never loaded: the version is half of the
    admitted pair, so guessing one would let a threshold change ride in behind
    a digest that was verified against different arithmetic.
    """
    if not version:
        raise ValueError(
            "label map version is required; an unversioned label map is never loaded"
        )
    try:
        return _LABEL_MAPS[version]
    except KeyError:
        raise ValueError(f"unknown label map version: {version!r}") from None


def _nli_enabled() -> bool:
    """The frozen verifier's opt-in gate (`EXOMEM_CLAIM_POLARITY_NLI`).

    Default OFF. This is an ADMISSION CONDITION, not a backend selector: unset,
    the verifier is refused for the process and the queue carries no label at
    all — it does not hand the lane to another backend.
    """
    value = os.environ.get("EXOMEM_CLAIM_POLARITY_NLI")
    return bool(value) and value.strip().lower() not in {"", "0", "false", "no", "off"}


# ---------------------------------------------------------------------------
# The pin registry (design D1) — a REPOSITORY artifact, reviewed in diff
# ---------------------------------------------------------------------------

#: RETIRED knob. Model identity comes only from `VERIFIER_PINS`; a value set
#: here selects nothing. It is still READ — once, to report it as ignored on
#: the diagnostic surface — so an operator who set it learns it is inert rather
#: than assuming it took effect.
_NLI_MODEL_ENV = "EXOMEM_CLAIM_NLI_MODEL"


@dataclass(frozen=True)
class VerifierPin:
    """One admitted `(model, weights, label map, fixture set)` tuple.

    All four move together. A pin is only ever added or changed in a reviewed
    diff: no environment value, runtime configuration, or vault content may add,
    select, or alter one, which is what makes "an unpinned model never labels"
    a property of the code rather than of a deployment's configuration.
    """

    model_name: str
    weights_sha256: str
    label_map_version: str
    fixture_set: str


#: Every verifier this build may run. EMPTY in this build: no cross-encoder
#: weights have been resolved and hashed in a reviewed environment yet, and a
#: digest is never guessed. An empty registry refuses every verifier, which is
#: the correct behaviour — absence, not a heuristic wearing the model's name.
VERIFIER_PINS: tuple[VerifierPin, ...] = ()


def _active_pin() -> VerifierPin | None:
    """The pin this build runs, or None.

    Reads the repository artifact and nothing else. Deliberately free of any
    environment or vault read: this function is the whole surface through which
    a model identity can be selected, so keeping it configuration-blind is the
    mechanism behind "runtime configuration cannot supply a model".
    """
    return VERIFIER_PINS[0] if VERIFIER_PINS else None


#: Closed set of refusal causes, so a degradation record names one of a known
#: vocabulary instead of free prose.
VERIFIER_REFUSAL_REASONS = frozenset(
    {
        "gate-off",
        "no-pin",
        "label-map-unknown",
        "weights-missing",
        "digest-mismatch",
        "dependency-missing",
        "fixtures-failed",
    }
)


@dataclass(frozen=True)
class VerifierAdmission:
    """The verifier tier's status: admitted, or refused with a named cause.

    This IS the degradation record. Every refusal path returns one, so the
    diagnostic surface can always say which of the admission conditions was
    not met rather than reporting a silent absence.
    """

    admitted: bool
    reason: str
    detail: str = ""
    model_name: str | None = None
    model_digest: str | None = None
    label_map_version: str | None = None
    fixture_set: str | None = None
    ignored_model_env: str | None = None

    def as_dict(self) -> dict:
        return {
            "admitted": self.admitted,
            "reason": self.reason,
            "detail": self.detail,
            "model_name": self.model_name,
            "model_digest": self.model_digest,
            "label_map_version": self.label_map_version,
            "fixture_set": self.fixture_set,
            "ignored_model_env": self.ignored_model_env,
        }


# ---------------------------------------------------------------------------
# Resolve-and-hash: the pinned digest, computed once per process
# ---------------------------------------------------------------------------

_WEIGHTS_DIGEST_CACHE: dict[str, tuple[str | None, str]] = {}
_WEIGHTS_DIGEST_LOCK = threading.Lock()

_VERIFIER_MODEL: Any = None
_VERIFIER_MODEL_NAME: str | None = None
_VERIFIER_LOCK = threading.Lock()


def _directory_digest(root: Path) -> str:
    """sha256 over a snapshot directory's content, path-order stable.

    Names are hashed alongside bytes so a rename is a different digest, and the
    walk is sorted so the answer does not depend on filesystem iteration order.
    """
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _hash_resident_snapshot(model_name: str) -> tuple[str | None, str]:
    """`(digest, detail)` for the model's resident weights through the offline cache.

    Exactly one resident revision is required. Zero means the weights are not
    there; more than one means the loader's choice is not determined by this
    directory, and a digest that does not name what will actually be loaded
    would be a pin in name only.
    """
    from . import model_cache

    root = model_cache.hub_dir() / model_cache.snapshot_dirname(model_name) / "snapshots"
    try:
        revisions = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return None, f"no resident snapshot for {model_name!r} under {root}"
    if not revisions:
        return None, f"no resident snapshot for {model_name!r} under {root}"
    if len(revisions) > 1:
        return None, (
            f"resident snapshot for {model_name!r} is ambiguous: {len(revisions)} "
            f"revisions under {root}"
        )
    try:
        return _directory_digest(revisions[0]), str(revisions[0])
    except OSError as error:
        return None, f"weights for {model_name!r} are unreadable: {error}"


def _resolve_weights(model_name: str) -> tuple[str | None, str]:
    """Memoized `_hash_resident_snapshot` — hashed once per process."""
    cached = _WEIGHTS_DIGEST_CACHE.get(model_name)
    if cached is not None:
        return cached
    with _WEIGHTS_DIGEST_LOCK:
        cached = _WEIGHTS_DIGEST_CACHE.get(model_name)
        if cached is not None:
            return cached
        resolved = _hash_resident_snapshot(model_name)
        _WEIGHTS_DIGEST_CACHE[model_name] = resolved
    return resolved


def resolve_weights_digest(model_name: str) -> str | None:
    """The resident weights digest for `model_name`, or None if unresolvable."""
    return _resolve_weights(model_name)[0]


def reset_verifier_cache() -> None:
    """Drop the process-cached digest and loaded model.

    The digest and the fixture verdict are deliberately computed once per
    process, so a caller that has changed what is on disk (a test planting a
    different snapshot; an operator who has just replaced weights) needs an
    explicit way to say the process's answer is stale.
    """
    global _VERIFIER_MODEL, _VERIFIER_MODEL_NAME

    with _WEIGHTS_DIGEST_LOCK:
        _WEIGHTS_DIGEST_CACHE.clear()
    with _FIXTURE_LOCK:
        _FIXTURE_VERDICTS.clear()
    with _VERIFIER_LOCK:
        _VERIFIER_MODEL = None
        _VERIFIER_MODEL_NAME = None


# ---------------------------------------------------------------------------
# Verification fixture set (design D6) — the pair's admission evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FixturePair:
    """One golden claim pair with the label an admitted verifier must produce.

    `heuristic_fails` records whether the retired lexical stand-in gets this
    pair wrong. It is documentation of WHY the tier exists, and the input to
    the fixture-set precision table — not something admission consults.
    """

    claim_a: str
    claim_b: str
    expected: str
    note: str
    heuristic_fails: bool = False


#: Golden pairs drawn from the f22 corpus shapes — genuine contradiction,
#: concordant evidence, restatement, unrelated — plus the lexical heuristic's
#: known failure cases. A (digest, label-map) pair is admitted only when it
#: answers every one of these correctly.
VERIFICATION_FIXTURES: dict[str, tuple[FixturePair, ...]] = {
    "stance-v1": (
        FixturePair(
            "Raising the cache TTL reduces p99 latency.",
            "Raising the cache TTL increases p99 latency.",
            "contradict",
            "genuine contradiction, shared vocabulary",
        ),
        FixturePair(
            "The rollout regressed checkout throughput.",
            "Checkout got faster after we shipped it.",
            "contradict",
            "genuine contradiction across differing surface forms",
            heuristic_fails=True,
        ),
        FixturePair(
            "Caching improves latency for repeat reads.",
            "Caching improves latency on repeat reads.",
            "duplicate",
            "restatement, near-identical surface",
        ),
        FixturePair(
            "Owned files are what retrieval quality depends on.",
            "Retrieval quality depends on owning the files.",
            "duplicate",
            "restatement, reordered surface",
            heuristic_fails=True,
        ),
        FixturePair(
            "Batching similar work helps focus.",
            "Batching similar work helps focus, most reliably in the morning.",
            "refine",
            "same stance, added detail",
        ),
        FixturePair(
            "Enabling the cache improves throughput.",
            "Disabling the cache degrades throughput.",
            "refine",
            "concordant evidence: antonym vocabulary, one stance",
            heuristic_fails=True,
        ),
        FixturePair(
            "Batching does not hurt focus.",
            "Batching helps focus.",
            "refine",
            "concordant evidence: negation parity differs, one stance",
            heuristic_fails=True,
        ),
        FixturePair(
            "Tesseract is required for image OCR on Windows.",
            "Pair dormancy reuses the stale-review activation calculation.",
            "unrelated",
            "disjoint topics",
        ),
        FixturePair(
            "The upload route parses multipart bodies through Starlette.",
            "Dormant notes in a close pair are the forgotten-conclusion case.",
            "unrelated",
            "disjoint topics, shared house vocabulary",
        ),
    ),
}

_FIXTURE_VERDICTS: dict[tuple[str, str, str, str], tuple[bool, str]] = {}
_FIXTURE_LOCK = threading.Lock()


def _run_fixture_set(pin: VerifierPin, label_map: LabelMap, predict) -> tuple[bool, str]:
    """Run every fixture in `pin.fixture_set` through the label map.

    Green means every pair produced exactly its expected label. One miss
    refuses the whole pair: the fixture set is the evidence that this digest
    and this label map belong together, and partial evidence is none.
    """
    pairs = VERIFICATION_FIXTURES.get(pin.fixture_set)
    if not pairs:
        return False, f"unknown fixture set {pin.fixture_set!r}"
    for pair in pairs:
        try:
            logits = predict([(pair.claim_a, pair.claim_b), (pair.claim_b, pair.claim_a)])
        except Exception as error:  # noqa: BLE001 — a forward pass that raises is a refusal
            return False, f"fixture {pair.claim_a!r} raised: {error}"
        result = label_map.apply(logits)
        produced = result.label if result is not None else "no label"
        if produced != pair.expected:
            return False, (
                f"fixture {pair.claim_a!r} expected {pair.expected!r}, got {produced!r}"
            )
    return True, f"{len(pairs)} fixtures green for {pin.fixture_set!r}"


def _verify_fixtures(
    pin: VerifierPin, digest: str, label_map: LabelMap, predict
) -> tuple[bool, str]:
    """Memoized `_run_fixture_set`, keyed by the exact pair it verified."""
    key = (pin.model_name, digest, pin.label_map_version, pin.fixture_set)
    cached = _FIXTURE_VERDICTS.get(key)
    if cached is not None:
        return cached
    with _FIXTURE_LOCK:
        cached = _FIXTURE_VERDICTS.get(key)
        if cached is not None:
            return cached
        verdict = _run_fixture_set(pin, label_map, predict)
        _FIXTURE_VERDICTS[key] = verdict
    return verdict

# ---------------------------------------------------------------------------
# Admission, and the one channel a model-produced label may come from
# ---------------------------------------------------------------------------


def verifier_admission() -> VerifierAdmission:
    """Whether the frozen verifier may run, or the named cause it may not.

    The conditions, in order: the opt-in gate is set; a pin exists in the
    repository registry; the pin's label map is one this build ships; the
    pinned weights resolve; and their digest matches the pin. Any failure
    refuses — and refusal degrades to ABSENCE, never to another backend's
    label under this one's method name.
    """
    ignored = os.environ.get(_NLI_MODEL_ENV) or None
    if not _nli_enabled():
        return VerifierAdmission(
            False,
            "gate-off",
            detail="EXOMEM_CLAIM_POLARITY_NLI is not set (default off).",
            ignored_model_env=ignored,
        )
    pin = _active_pin()
    if pin is None:
        return VerifierAdmission(
            False,
            "no-pin",
            detail="the repository pin registry is empty; no verifier is admitted.",
            ignored_model_env=ignored,
        )
    try:
        label_map = get_label_map(pin.label_map_version)
    except ValueError as error:
        return VerifierAdmission(
            False,
            "label-map-unknown",
            detail=str(error),
            model_name=pin.model_name,
            label_map_version=pin.label_map_version,
            fixture_set=pin.fixture_set,
            ignored_model_env=ignored,
        )
    digest, detail = _resolve_weights(pin.model_name)
    if digest is None:
        return VerifierAdmission(
            False,
            "weights-missing",
            detail=detail,
            model_name=pin.model_name,
            label_map_version=pin.label_map_version,
            fixture_set=pin.fixture_set,
            ignored_model_env=ignored,
        )
    if digest != pin.weights_sha256:
        return VerifierAdmission(
            False,
            "digest-mismatch",
            detail=(
                f"resolved weights digest {digest} does not match the pinned "
                f"{pin.weights_sha256} for {pin.model_name!r}."
            ),
            model_name=pin.model_name,
            model_digest=digest,
            label_map_version=pin.label_map_version,
            fixture_set=pin.fixture_set,
            ignored_model_env=ignored,
        )
    predict = _load_verifier_predictor(pin.model_name)
    if predict is None:
        return VerifierAdmission(
            False,
            "dependency-missing",
            detail=(
                f"the `nli` extra is not installed, or {pin.model_name!r} could not "
                "be loaded from the offline model cache."
            ),
            model_name=pin.model_name,
            model_digest=digest,
            label_map_version=pin.label_map_version,
            fixture_set=pin.fixture_set,
            ignored_model_env=ignored,
        )
    green, fixture_detail = _verify_fixtures(pin, digest, label_map, predict)
    if not green:
        return VerifierAdmission(
            False,
            "fixtures-failed",
            detail=fixture_detail,
            model_name=pin.model_name,
            model_digest=digest,
            label_map_version=pin.label_map_version,
            fixture_set=pin.fixture_set,
            ignored_model_env=ignored,
        )
    return VerifierAdmission(
        True,
        "admitted",
        detail=fixture_detail,
        model_name=pin.model_name,
        model_digest=digest,
        label_map_version=pin.label_map_version,
        fixture_set=pin.fixture_set,
        ignored_model_env=ignored,
    )



def verifier_status() -> dict:
    """The verifier tier's status for the diagnostic surface (doctor, status).

    Always answerable and never fetches a model: an admitted verifier is loaded
    only from the exact resident snapshot whose digest was checked. The payload
    reports which knob is the gate, which knob is RETIRED and whether a value is
    sitting in it being ignored, what the repository registry actually pins,
    and which label maps this build ships. An operator who set the retired knob
    learns from here that it selected nothing.
    """
    payload = verifier_admission().as_dict()
    payload["gate"] = "EXOMEM_CLAIM_POLARITY_NLI"
    payload["retired_model_env"] = _NLI_MODEL_ENV
    payload["pinned_models"] = [pin.model_name for pin in VERIFIER_PINS]
    payload["label_map_versions"] = sorted(_LABEL_MAPS)
    return payload


def _load_verifier_predictor(model_name: str):
    """The cross-encoder's `predict`, or None when the `nli` extra is absent.

    The constructor receives the exact snapshot directory whose bytes were
    hashed for admission, with local-only loading forced. A local load failure
    refuses the verifier; this path never retries a repository name through the
    hub and therefore cannot execute bytes other than the admitted snapshot.

    Two claim texts enter as a classification PAIR and logits come out. There is
    no prompt assembly, no instruction template, and no generation anywhere on
    this path (design D5) — which is why vault text can never reach instruction
    position through this seam.
    """
    global _VERIFIER_MODEL, _VERIFIER_MODEL_NAME

    if _VERIFIER_MODEL is not None and _VERIFIER_MODEL_NAME == model_name:
        return _VERIFIER_MODEL.predict
    with _VERIFIER_LOCK:
        if _VERIFIER_MODEL is not None and _VERIFIER_MODEL_NAME == model_name:
            return _VERIFIER_MODEL.predict
        try:
            from sentence_transformers import CrossEncoder

            from . import accel

            digest, snapshot_detail = _resolve_weights(model_name)
            if digest is None:
                return None
            device = accel.select_device()
            model = CrossEncoder(
                str(Path(snapshot_detail)),
                device=device,
                local_files_only=True,
            )
        except Exception as error:  # noqa: BLE001 — absent extra is a refusal, not a crash
            log.warning("frozen verifier unavailable (%s); no label is produced", error)
            return None
        _VERIFIER_MODEL = model
        _VERIFIER_MODEL_NAME = model_name
    return _VERIFIER_MODEL.predict


def verifier_polarity(claim_a: str, claim_b: str) -> PolarityResult | None:
    """The ONLY channel a model-produced polarity label may come from.

    Returns None whenever the verifier is not admitted — absence, never a
    differently-produced label wearing the verifier's method name. A forward
    pass that raises propagates to the caller, which records that one entry as
    degraded and carries on (the pass is never aborted by one bad pair).
    """
    admission = verifier_admission()
    if not admission.admitted:
        log.debug(
            "frozen verifier refused (%s): %s", admission.reason, admission.detail
        )
        return None
    label_map = get_label_map(admission.label_map_version)
    predict = _load_verifier_predictor(str(admission.model_name))
    if predict is None:
        return None
    return label_map.apply(predict([(claim_a, claim_b), (claim_b, claim_a)]))


def classify_polarity(
    claim_a: str, claim_b: str, *, cosine: float | None = None
) -> PolarityResult:
    """Polarity of two claims behind ONE stable interface.

    The admitted frozen verifier when it is admitted, else the deterministic
    lexical heuristic. NOT the review-queue channel: the queue calls
    `verifier_polarity` directly, precisely so a heuristic verdict can never
    reach queue metadata (design D3). This seam remains for callers that want a
    best-effort verdict and are not writing an admitted, provenance-marked label.
    """
    verdict = verifier_polarity(claim_a, claim_b)
    if verdict is not None:
        return verdict
    return _heuristic_polarity(claim_a, claim_b, cosine=cosine)
