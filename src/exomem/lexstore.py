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
import logging
import os
import sqlite3
import threading
from pathlib import Path

from .kbdir import kb_dirname

log = logging.getLogger(__name__)

_NAV_BASENAMES = frozenset({"index.md", "log.md"})

_PROBE_RESULT: bool | None = None
_PROBE_LOCK = threading.Lock()

_STORES: dict[Path, LexicalStore] = {}
_STORES_LOCK = threading.Lock()


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


def lexical_path(vault_root: Path) -> Path:
    return vault_root / kb_dirname() / ".lexical.sqlite"


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


# ------------------------------------------------------------------ policy


def _usable() -> bool:
    return backend() != "python" and fts5_available()


def search_bm25(
    vault_root: Path,
    query: str,
    k: int,
    *,
    scope: str = "kb",
    freshness: tuple | None = None,
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
    return store.search_bm25(tokens, k, scope, freshness)


def search_substring(
    vault_root: Path,
    query_norm: str,
    *,
    scope: str = "kb",
    freshness: tuple | None = None,
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
    return store.search_substring(tokens, scope, freshness)


def ensure_fresh(vault_root: Path) -> None:
    """Run the reconcile NOW (reconcile's seam) instead of lazily on the next
    search — and paranoidly: verified state is discarded first, so this pass
    exact-checks the sidecar against the walk even where a search would trust
    it. No-op when the backend is off or unavailable."""
    if not _usable():
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


# ------------------------------------------------------------------ write seams


def upsert_after_write(vault_root: Path, written_paths: list[Path]) -> None:
    """Keep the lexical index in lockstep with a writer's markdown change.

    Deliberately NOT gated behind the embeddings extra or its env switches —
    the lexical lanes run on lean installs. Best-effort: a lexical miss must
    never fail a write; sync-on-first-use heals whatever a miss leaves behind.
    No-ops (beyond its own gates) when the sidecar doesn't exist yet — the
    first search builds it whole.
    """
    if not _usable():
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
    if not _usable():
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

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pages("
            " path TEXT PRIMARY KEY,"
            " mtime_ns INTEGER NOT NULL,"
            " updated TEXT NOT NULL DEFAULT '0000-00-00',"
            " in_kb INTEGER NOT NULL DEFAULT 0,"
            " in_vault INTEGER NOT NULL DEFAULT 0,"
            " is_nav INTEGER NOT NULL DEFAULT 0)"
        )
        # Covering indexes so the per-corpus-change count/max reconcile stays
        # index-ranged instead of scanning 100k rows.
        conn.execute("CREATE INDEX IF NOT EXISTS pages_kb ON pages(in_kb, mtime_ns)")
        conn.execute("CREATE INDEX IF NOT EXISTS pages_vault ON pages(in_vault, mtime_ns)")
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(stemmed)")
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS tri USING fts5("
            "title_lower, body_lower, tokenize='trigram case_sensitive 1')"
        )
        # Per-scope walk triples this sidecar was last VERIFIED against
        # (repr'd) — the cross-process "nothing changed while we were down"
        # attestation that lets a restart skip the exact verify.
        conn.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")

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

    def _bless(self, conn: sqlite3.Connection, scope: str, triple: tuple) -> None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (f"triple:{scope}", repr(tuple(triple))),
        )
        conn.commit()
        self._synced[scope] = triple

    def _ensure_synced(self, conn: sqlite3.Connection, scope: str, freshness: tuple | None) -> None:
        """Reconcile against the walk ONCE per observed corpus change.

        `freshness` is the scope's `(count, max_mtime_ns, digest)` walk triple
        (free from the request's FreshnessSnapshot; computed here when
        absent). See the module docstring for the four-rung reconcile ladder
        this implements.
        """
        if freshness is None:
            freshness = self._scope_triple(scope)
        if self._synced.get(scope) == freshness:
            return
        with self._lock:
            if self._synced.get(scope) == freshness:
                return
            self._ensure_schema(conn)
            if self._stored_count_max(conn, scope) != (freshness[0], freshness[1]):
                self._heal_delta(conn)  # incremental: patch only the drifted rows
                return
            witnessed = self._witnessed.pop(scope, None)
            if witnessed == freshness:
                # The hook updated the sidecar for exactly this registry state.
                self._bless(conn, scope, freshness)
                return
            if self._meta_triple(conn, scope) == freshness:
                self._synced[scope] = freshness  # verified before; unchanged
                return
            # Unwitnessed change with matching count/mtime. A path/mtime drift
            # can be healed incrementally. If those legacy row fields still
            # match while the full corpus signature changed, bytes were
            # replaced with a preserved mtime; rebuild so FTS content cannot be
            # blessed stale.
            if self._walk_matches_rows(conn, scope):
                self._rebuild(conn)
            else:
                self._heal_delta(conn)

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
            conn.execute("DELETE FROM fts")
            conn.execute("DELETE FROM tri")
            for path, (in_kb, in_vault) in members.items():
                self._insert_page(conn, path, signatures[path][0], in_kb, in_vault)
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
            # Manual/content-table drift can leave FTS rows whose page row no
            # longer exists. Remove them before SQLite reuses a deleted page
            # rowid, otherwise the replacement FTS insert fails its rowid
            # constraint and retires the sidecar instead of healing it.
            conn.execute("DELETE FROM fts WHERE rowid NOT IN (SELECT rowid FROM pages)")
            conn.execute("DELETE FROM tri WHERE rowid NOT IN (SELECT rowid FROM pages)")
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
        else:
            try:
                rel = path.resolve().relative_to(self.vault_root.resolve()).as_posix()
            except ValueError:
                return
            title_lower = body_lower = stemmed = ""
            updated = "0000-00-00"
        is_nav = path.name.lower() in _NAV_BASENAMES
        cur = conn.execute(
            "INSERT INTO pages(path, mtime_ns, updated, in_kb, in_vault, is_nav) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (rel, mtime_ns, updated, int(in_kb), int(in_vault), int(is_nav)),
        )
        rowid = cur.lastrowid
        conn.execute("INSERT INTO fts(rowid, stemmed) VALUES(?, ?)", (rowid, stemmed))
        conn.execute(
            "INSERT INTO tri(rowid, title_lower, body_lower) VALUES(?, ?, ?)",
            (rowid, title_lower, body_lower),
        )

    def _delete_rowid(self, conn: sqlite3.Connection, rowid: int) -> None:
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

    def upsert_paths(self, paths: list[Path]) -> None:
        """Writer-seam upsert: replace each file's rows in one transaction."""
        if self._failed:
            return
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            with conn:
                for path in paths:
                    try:
                        rel = path.resolve().relative_to(self.vault_root.resolve()).as_posix()
                    except ValueError:
                        continue
                    row = conn.execute("SELECT rowid FROM pages WHERE path = ?", (rel,)).fetchone()
                    if row is not None:
                        self._delete_rowid(conn, row[0])
                    try:
                        mtime_ns = path.stat().st_mtime_ns
                    except OSError:
                        continue  # written then removed → stays deleted
                    in_kb, in_vault = self._membership(path)
                    if not (in_kb or in_vault):
                        continue
                    self._insert_page(conn, path, mtime_ns, in_kb, in_vault)
            self._remember_live_witnesses()
        finally:
            conn.close()

    def delete_rel_paths(self, rel_paths: list[str]) -> None:
        if self._failed:
            return
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            with conn:
                for rel in rel_paths:
                    row = conn.execute("SELECT rowid FROM pages WHERE path = ?", (rel,)).fetchone()
                    if row is not None:
                        self._delete_rowid(conn, row[0])
            self._remember_live_witnesses()
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
        the `reconcile` command's "I edited around the system, heal it" seam."""
        if self._failed:
            return
        try:
            conn = self._connect()
            try:
                self._ensure_schema(conn)
                self._witnessed.clear()
                self._synced.clear()
                conn.execute("DELETE FROM meta WHERE key LIKE 'triple:%'")
                conn.commit()
                self._ensure_synced(conn, "vault", None)
                self._ensure_synced(conn, "kb", None)
            finally:
                conn.close()
        except sqlite3.Error as e:
            self._failed = True
            log.warning(
                "lexical sidecar failed (%s); this process serves the in-process lexical paths",
                e,
            )

    # -------------------------------------------------------------- search

    def search_bm25(
        self, stemmed_tokens: list[str], k: int, scope: str, freshness: tuple | None
    ) -> list[tuple[str, float]] | None:
        if self._failed:
            return None
        try:
            conn = self._connect()
            try:
                self._ensure_synced(conn, scope, freshness)
                return self._bm25_query(conn, stemmed_tokens, k, scope)
            finally:
                conn.close()
        except sqlite3.Error as e:
            self._failed = True
            log.warning(
                "lexical sidecar failed (%s); this process serves the in-process lexical paths",
                e,
            )
            return None

    def _bm25_query(
        self, conn: sqlite3.Connection, tokens: list[str], k: int, scope: str
    ) -> list[tuple[str, float]]:
        # Tokens are [a-z0-9]+ — no FTS5 syntax can hide in them, but quote
        # anyway; OR mirrors get_scores() membership (any-term match).
        match = " OR ".join(f'"{t}"' for t in tokens)
        col = "in_vault" if scope == "vault" else "in_kb"
        rows = conn.execute(
            "SELECT p.path, -bm25(fts) AS score "
            "FROM fts JOIN pages p ON p.rowid = fts.rowid "
            f"WHERE fts MATCH ? AND p.{col} = 1 "
            "ORDER BY bm25(fts), p.path LIMIT ?",
            (match, k),
        ).fetchall()
        return [(p, float(s)) for p, s in rows]

    def search_substring(
        self, tokens: list[str], scope: str, freshness: tuple | None
    ) -> list[str] | None:
        if self._failed:
            return None
        try:
            conn = self._connect()
            try:
                self._ensure_synced(conn, scope, freshness)
                return self._substring_query(conn, tokens, scope)
            finally:
                conn.close()
        except sqlite3.Error as e:
            self._failed = True
            log.warning(
                "lexical sidecar failed (%s); this process serves the in-process lexical paths",
                e,
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
