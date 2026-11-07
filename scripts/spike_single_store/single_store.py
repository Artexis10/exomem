"""Prototype: one SQLite store is canonical for pages, search rows and graph.

One file per vault holds the page bytes (`store_pages`), today's lexical
catalogue tables (copied from `.lexical.sqlite`) and today's graph tables
(copied from `.graph.sqlite`). A page write derives its lexical and graph rows
with Exomem's own code, then commits bytes, rows, an audit row and a projection
intent in one BEGIN IMMEDIATE transaction. Markdown is a byte-exact projection
written after commit; `store_manifest` records what is on disk, so a file whose
hash differs from the manifest is an outside edit, imported behind a
base-revision check or held as a correction.

Exomem's derivation reads pages from disk. The store feeds it bytes instead,
through `_bytes_view`, which overrides the parse cache and the parent-state
read for the one path being written. This is spike scaffolding, not a design.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

STORE_DDL = """
CREATE TABLE IF NOT EXISTS store_pages(
  path TEXT PRIMARY KEY, body BLOB NOT NULL, hash TEXT NOT NULL,
  frontmatter_json TEXT, rev INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS store_manifest(
  path TEXT PRIMARY KEY, hash TEXT NOT NULL, rev INTEGER NOT NULL,
  size INTEGER, mtime_ns INTEGER);
CREATE TABLE IF NOT EXISTS store_projection(path TEXT PRIMARY KEY, rev INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS store_audit(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT, rev INTEGER, op TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS store_held(
  path TEXT, base_rev INTEGER, store_rev INTEGER, file_hash TEXT, body BLOB, ts REAL);
CREATE TABLE IF NOT EXISTS store_meta(key TEXT PRIMARY KEY, value);
"""


# The text formats Exomem itself writes into a vault; binaries stay files.
STORED_SUFFIXES = {".md", ".json", ".jsonl", ".yaml", ".yml"}


def _frontmatter_json(data: bytes) -> str | None:
    from exomem import vault as vault_module

    try:
        return json.dumps(vault_module.parse_frontmatter(data.decode("utf-8"))[0],
                          default=str, sort_keys=True)
    except Exception:  # noqa: BLE001 - unparseable pages keep their bytes
        return None


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def store_path(state_dir: Path) -> Path:
    return state_dir / "single-store.sqlite"


def connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db, isolation_level=None, check_same_thread=False, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    # Canonical bytes live here, so every commit is durable at acknowledgement.
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def migrate(vault: Path, db: Path) -> dict:
    """Build the store from today's sidecars plus every Markdown page's bytes."""
    from exomem import epistemic_graph, lexstore
    from exomem import vault as vault_module

    started = time.perf_counter()
    if db.exists():
        db.unlink()
    src = sqlite3.connect(lexstore.lexical_path(vault))
    dst = sqlite3.connect(db)
    src.backup(dst)
    src.close()
    lex_tables = {r[0] for r in dst.execute("select name from sqlite_master")}
    # The graph's triggers name `main.` objects, so the file cannot be
    # attached under another schema name; copy schema and rows instead.
    graph = sqlite3.connect(f"file:{epistemic_graph.sidecar_path(vault)}?mode=ro", uri=True)
    objects = graph.execute(
        "select type, name, sql from sqlite_master where sql is not null "
        "and name not like 'sqlite_%' order by case type when 'table' then 0 "
        "when 'index' then 1 else 2 end").fetchall()
    clash = [name for _t, name, _s in objects if name in lex_tables]
    if clash:
        raise RuntimeError(f"graph and lexical tables share names: {clash}")
    for kind, name, sql in objects:
        if kind == "table":
            dst.execute(sql)
            cur = graph.execute(f'SELECT * FROM "{name}"')
            width = len(cur.description)
            dst.executemany(f'INSERT INTO "{name}" VALUES ({",".join("?" * width)})', cur)
    for kind, _name, sql in objects:
        if kind != "table":
            dst.execute(sql)
    dst.commit()
    graph.close()
    dst.executescript(STORE_DDL)
    pages = 0
    with dst:
        for p in vault.rglob("*"):
            rel = p.relative_to(vault).as_posix()
            if (not p.is_file() or rel.startswith(".") or "/." in rel
                    or p.suffix not in STORED_SUFFIXES):
                continue
            data = p.read_bytes()
            fm_json = _frontmatter_json(data) if p.suffix == ".md" else None
            st = p.stat()
            dst.execute("INSERT INTO store_pages VALUES (?,?,?,?,1)",
                        (rel, data, sha(data), fm_json))
            dst.execute("INSERT INTO store_manifest VALUES (?,?,1,?,?)",
                        (rel, sha(data), st.st_size, st.st_mtime_ns))
            pages += 1
        dst.execute("INSERT OR REPLACE INTO store_meta VALUES ('store_rev', 0)")
    dst.execute("PRAGMA journal_mode=WAL")
    dst.close()
    return {"migrate_s": round(time.perf_counter() - started, 2), "pages": pages,
            "db_bytes": db.stat().st_size}


@contextlib.contextmanager
def _bytes_view(abs_path: Path, page, text: str, data: bytes):
    """Make Exomem's derivation read `text` for `abs_path` instead of the disk."""
    from exomem import find, recall_policy, semantic_index

    cache = find._CACHE
    original_get = cache.get
    original_state = semantic_index.current_parent_index_state
    key = str(abs_path)

    def get(path, vault_root, *a, **kw):
        if str(path) == key:
            return page
        return original_get(path, vault_root, *a, **kw)

    def state(vault_root, path, *, source=None):
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = Path(vault_root) / candidate
        if source is None and str(candidate) == key:
            source = text
        return original_state(vault_root, path, source=source)

    original_candidate = recall_policy.is_recall_candidate

    def candidate(vault_root, path):
        # The page is not on disk until projected; decide from its bytes, the
        # policy's own pre-creation entry point.
        p = Path(path)
        if not p.is_absolute():
            p = Path(vault_root) / p
        if str(p) == key:
            return recall_policy.admits_markdown_content(vault_root, p, data)
        return original_candidate(vault_root, path)

    cache.get = get
    semantic_index.current_parent_index_state = state
    recall_policy.is_recall_candidate = candidate
    try:
        yield
    finally:
        cache.get = original_get
        semantic_index.current_parent_index_state = original_state
        recall_policy.is_recall_candidate = original_candidate


class Store:
    def __init__(self, vault: Path, db: Path):
        from exomem import epistemic_graph, find, lexstore

        self.vault = vault
        self.db = db
        self.conn = connect(db)
        self.lex = lexstore.get_store(vault)
        self.resolver = find.shared_resolver(vault)
        self.type_basis = epistemic_graph._producer_type_basis(vault)
        self.write_lock = threading.Lock()

    # -- write ---------------------------------------------------------------
    def _derive(self, rel: str, data: bytes) -> dict:
        """Compute one Markdown page's graph rows with Exomem's own code."""
        from exomem import epistemic_graph as eg
        from exomem import find, semantic_index, semantic_units

        abs_path = self.vault / rel
        text = data.decode("utf-8")
        page = find._parse_page(abs_path, time.time(), self.vault, content=data,
                                resolved_relative=rel)
        with _bytes_view(abs_path, page, text, data):
            state = semantic_index.current_parent_index_state(self.vault, abs_path, source=text)
            file_node = eg._file_node(self.vault, page, text, state=state,
                                      type_basis=self.type_basis)
            unit_nodes = [eg._candidate_node(page, unit, state)
                          for unit in semantic_units.candidate_units(state.candidates)]
            edges = eg._structural_edges_for_page(
                self.vault, page, state, source_hash=file_node.source_hash,
                resolver=self.resolver)
        return {"abs": abs_path, "page": page, "text": text, "state": state,
                "file_node": file_node, "unit_nodes": unit_nodes, "edges": edges,
                "deps": eg._dependency_records(state.body, state.candidates)}

    def _apply_page(self, c: sqlite3.Connection, rel: str, data: bytes, d: dict) -> None:
        from exomem import epistemic_graph as eg

        old = c.execute("SELECT rowid FROM pages WHERE path=?", (rel,)).fetchone()
        if old:
            self.lex._delete_rowid(c, old[0])
        else:
            self.lex._delete_semantic_units(c, rel)
        with _bytes_view(d["abs"], d["page"], d["text"], data):
            in_kb, in_vault = self.lex._membership(d["abs"])
            if in_kb or in_vault:
                self.lex._insert_page(c, d["abs"], time.time_ns(), in_kb, in_vault)
        for table, column in (("graph_edges", "source_path"), ("graph_nodes", "path"),
                              ("graph_parent_refs", "path"), ("graph_dependencies", "source_path"),
                              ("graph_dependency_coverage", "source_path")):
            c.execute(f"DELETE FROM {table} WHERE {column} = ?", (rel,))
        for node in [d["file_node"], *d["unit_nodes"]]:
            eg._insert_node(c, node)
        if d["state"].parent_ref is not None:
            c.execute("INSERT OR REPLACE INTO graph_parent_refs(path, parent_ref) VALUES (?, ?)",
                      (rel, d["state"].parent_ref))
        for edge in d["edges"]:
            eg._insert_edge(c, edge)
        c.executemany("INSERT INTO graph_dependencies(source_path, lookup_key, raw_target) "
                      "VALUES (?, ?, ?)", [(rel, k, t) for k, t in d["deps"]])
        c.execute("INSERT INTO graph_dependency_coverage(source_path, source_hash, "
                  "dependency_format, expected_count) VALUES (?, ?, ?, ?)",
                  (rel, d["file_node"].source_hash, eg._DEPENDENCY_FORMAT, len(d["deps"])))

    def put_many(self, items: list[tuple[str, bytes]], *, op: str = "write",
                 disk_stats: dict | None = None) -> dict:
        """Commit one operation's files, their search rows and graph rows together."""
        from exomem import epistemic_graph as eg

        t0 = time.perf_counter()
        derived = {rel: self._derive(rel, data) for rel, data in items if rel.endswith(".md")}
        t1 = time.perf_counter()
        revs = {}
        with self.write_lock:
            c = self.conn
            c.execute("BEGIN IMMEDIATE")
            try:
                for rel, data in items:
                    row = c.execute("SELECT rev FROM store_pages WHERE path=?", (rel,)).fetchone()
                    rev = revs[rel] = (row[0] if row else 0) + 1
                    fm_json = _frontmatter_json(data) if rel.endswith(".md") else None
                    c.execute("INSERT OR REPLACE INTO store_pages VALUES (?,?,?,?,?)",
                              (rel, data, sha(data), fm_json, rev))
                    if rel in derived:
                        self._apply_page(c, rel, data, derived[rel])
                    st = (disk_stats or {}).get(rel)
                    if st is None:
                        c.execute("INSERT OR REPLACE INTO store_projection VALUES (?,?)", (rel, rev))
                    else:
                        c.execute("INSERT OR REPLACE INTO store_manifest VALUES (?,?,?,?,?)",
                                  (rel, sha(data), rev, st.st_size, st.st_mtime_ns))
                    c.execute("INSERT INTO store_audit(path, rev, op, ts) VALUES (?,?,?,?)",
                              (rel, rev, op, time.time()))
                if derived:
                    eg._bump_generation(c)
                c.execute("UPDATE store_meta SET value = value + 1 WHERE key = 'store_rev'")
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        t2 = time.perf_counter()
        return {"revs": revs, "derive_ms": (t1 - t0) * 1000, "commit_ms": (t2 - t1) * 1000,
                "edges": sum(len(d["edges"]) for d in derived.values()), "files": len(items)}

    def put(self, rel: str, data: bytes, *, op: str = "write",
            disk_stat: os.stat_result | None = None) -> dict:
        res = self.put_many([(rel, data)], op=op,
                            disk_stats={rel: disk_stat} if disk_stat else None)
        res["rev"] = res["revs"][rel]
        return res

    # -- reads used by the visibility checks ----------------------------------
    def lexical(self, path: str, token: str) -> bool:
        from exomem import bm25

        terms = bm25.tokenize(token)
        if not terms:
            return False
        rows = self.conn.execute(
            "SELECT p.path FROM fts JOIN pages p ON p.rowid = fts.rowid WHERE fts MATCH ?",
            (" ".join(f'"{t}"' for t in terms),)).fetchall()
        return any(r[0] == path for r in rows)

    def graph_edge(self, path: str, link: str) -> bool:
        cur = self.conn.execute("SELECT * FROM graph_edges WHERE source_path = ?", (path,))
        names = [d[0] for d in cur.description]
        needle = link.lower()
        return any(needle in json.dumps(dict(zip(names, r)), default=str).lower()
                   for r in cur.fetchall())

    # -- projection ------------------------------------------------------------
    def project_once(self, conn: sqlite3.Connection, limit: int = 64,
                     on_item=None) -> int:
        rows = conn.execute(
            "SELECT q.path, q.rev, s.body FROM store_projection q "
            "JOIN store_pages s ON s.path = q.path LIMIT ?", (limit,)).fetchall()
        for rel, rev, body in rows:
            t0 = time.perf_counter()
            dest = self.vault / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(f".{dest.name}.projecting")
            with open(tmp, "wb") as fh:
                fh.write(body)
                fh.flush()
                # A projection marked done must survive power loss, or a torn
                # file would later read as an outside edit.
                os.fsync(fh.fileno())
            # Exomem's own writer waits out a transient Windows sharing refusal
            # (an antivirus or indexer holding the target); projection must too.
            from exomem import vault as vault_module

            vault_module.replace_tolerating_transient_sharing(lambda: os.replace(tmp, dest))
            st = dest.stat()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR REPLACE INTO store_manifest VALUES (?,?,?,?,?)",
                         (rel, sha(body), rev, st.st_size, st.st_mtime_ns))
            conn.execute("DELETE FROM store_projection WHERE path = ? AND rev = ?", (rel, rev))
            conn.execute("COMMIT")
            if on_item:
                on_item(rel, rev, (time.perf_counter() - t0) * 1000, time.perf_counter())
        return len(rows)

    def start_projector(self, on_item=None, idle_s: float = 0.02):
        stop = threading.Event()
        conn = connect(self.db)

        self.projector_errors: list[str] = []

        def loop():
            while not stop.is_set():
                try:
                    done = self.project_once(conn, on_item=on_item)
                except Exception as exc:  # noqa: BLE001 - recorded; the queue retries it
                    if conn.in_transaction:
                        conn.rollback()
                    self.projector_errors.append(f"{type(exc).__name__}: {exc}"[:300])
                    done = 0
                if done == 0:
                    time.sleep(idle_s)

        thread = threading.Thread(target=loop, name="store-projector", daemon=True)
        thread.start()
        return stop, thread

    # -- outside edits -----------------------------------------------------------
    def reconcile(self) -> dict:
        """Import Markdown that differs from the manifest; hold it if the store moved."""
        manifest = {r[0]: r[1:] for r in self.conn.execute(
            "SELECT path, hash, rev, size, mtime_ns FROM store_manifest")}
        pending = {r[0] for r in self.conn.execute("SELECT path FROM store_projection")}
        out = {"scanned": 0, "hashed": 0, "imported": 0, "held": 0, "unchanged_touch": 0}
        for p in (self.vault / "Knowledge Base").rglob("*"):
            if not p.is_file() or p.suffix not in STORED_SUFFIXES or p.name.startswith("."):
                continue
            rel = p.relative_to(self.vault).as_posix()
            out["scanned"] += 1
            st = p.stat()
            m = manifest.get(rel)
            if m and (st.st_size, st.st_mtime_ns) == (m[2], m[3]):
                continue
            data = p.read_bytes()
            out["hashed"] += 1
            if m and sha(data) == m[0]:
                out["unchanged_touch"] += 1
                continue
            row = self.conn.execute("SELECT rev FROM store_pages WHERE path=?", (rel,)).fetchone()
            store_rev = row[0] if row else None
            base_rev = m[1] if m else None
            if store_rev == base_rev and rel not in pending:
                self.put(rel, data, op="outside-edit", disk_stat=st)
                out["imported"] += 1
            else:
                with self.write_lock:
                    self.conn.execute("INSERT INTO store_held VALUES (?,?,?,?,?,?)",
                                      (rel, base_rev, store_rev, sha(data), data, time.time()))
                out["held"] += 1
        return out
