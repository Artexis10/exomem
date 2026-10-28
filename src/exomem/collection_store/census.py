"""Handle-owned, disposable evidence for paged view reconciliation.

The main database stays read-only. TEMP holds exact names, duplicate candidates and
settling metadata on disk; a window retains only its bounded inputs and one iterator.
A completed sweep describes what it observed, not an atomic filesystem snapshot.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path

from .. import held_fs, memory_refs, record_formats, records, sync_providers, vault
from .. import structured_collections as collections
from . import authority, capability, connection, edit_back, views

logger = logging.getLogger(__name__)


class Census:
    def __init__(self, handle):
        self.db = connection.open_query_reader(handle.path)
        self.db.executescript("""
            CREATE TEMP TABLE roots(
              cid TEXT PRIMARY KEY, source TEXT, held_root TEXT, manifest TEXT,
              generation INTEGER, failed INTEGER NOT NULL DEFAULT 0,
              complete INTEGER NOT NULL DEFAULT 0) STRICT;
            CREATE TEMP TABLE directories(
              path TEXT PRIMARY KEY, cid TEXT, done INTEGER NOT NULL DEFAULT 0) STRICT;
            CREATE INDEX temp.directories_pending ON directories(done,path);
            CREATE TEMP TABLE projections(path TEXT PRIMARY KEY, cid TEXT, kind TEXT) STRICT;
            CREATE TEMP TABLE candidates(
              path TEXT PRIMARY KEY, cid TEXT, item_key TEXT, stamp TEXT,
              digest TEXT, stat TEXT, version TEXT, conflict INTEGER NOT NULL DEFAULT 0,
              old_path TEXT, due REAL NOT NULL, handled INTEGER NOT NULL DEFAULT 0) STRICT;
            CREATE INDEX temp.candidates_by_key ON candidates(cid,item_key,conflict,
              json_extract(stamp,'$.s'),json_extract(stamp,'$.i'),json_extract(stamp,'$.v'),json_extract(stamp,'$.h'));
            CREATE INDEX temp.candidates_due ON candidates(handled,due,path);
            CREATE TEMP TABLE settling(
              path TEXT PRIMARY KEY, digest TEXT, since REAL NOT NULL,
              invalid_since REAL, retry REAL, pending INTEGER NOT NULL DEFAULT 0,
              observed INTEGER NOT NULL DEFAULT 0, detached INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL) STRICT;
            CREATE INDEX temp.settling_due ON settling(detached,due,path);
            CREATE TEMP TABLE hints(path TEXT, destination TEXT, directory INTEGER,
              PRIMARY KEY(path,destination,directory)) STRICT;
            CREATE TEMP TABLE outmoves(old_path TEXT PRIMARY KEY,path TEXT,cid TEXT,done INTEGER DEFAULT 0) STRICT;
            CREATE INDEX temp.outmoves_pending ON outmoves(done,old_path);
            CREATE TEMP TABLE failures(path TEXT PRIMARY KEY, code TEXT NOT NULL) STRICT;
        """)
        # These phases are private cursor operations, not user-defined statuses.
        self.phase = "idle"
        self.after = ""
        self.next_full = 0.0
        self.restart = False
        self._failure_count = 0
        self._fs = self._parent = self._iterator = None
        self._directory = self._cid = None
        self._manifest = None

    def close(self):
        self._close_directory()
        self.db.close()

    def _close_directory(self):
        if self._iterator is not None:
            self._iterator.close()
        if self._parent is not None:
            self._parent.close()
        if self._fs is not None:
            self._fs.close()
        self._fs = self._parent = self._iterator = None
        self._directory = self._cid = self._manifest = None

    def hints(self, changes, dirty):
        from .runtime import _VIEW_CHANGE_CAPACITY

        # Retained hints share the mailbox's engineering capacity; overflow needs enumeration, not more metadata.
        for path, destination, directory in changes:
            if destination is None:
                continue  # A completed enumeration consumes create/delete/directory dirtiness; only moves need endpoint evidence.
            self.db.execute("INSERT OR REPLACE INTO temp.hints SELECT ?,?,? WHERE "
                            "EXISTS(SELECT 1 FROM temp.hints WHERE path=? AND destination=? AND directory=?) "
                            "OR (SELECT count(*) FROM temp.hints)<?",
                            (path, destination or "", int(directory), path, destination or "", int(directory),
                             _VIEW_CHANGE_CAPACITY))
        if changes or dirty:
            if self.phase == "idle":
                self.next_full = 0.0
            else:
                self.restart = True

    def enqueue(self, paths):
        self.db.executemany(
            "INSERT INTO temp.settling(path,since,pending,due) VALUES (?,0.0,1,0.0) "
            "ON CONFLICT(path) DO UPDATE SET pending=1,due=0.0,detached=0", ((path,) for path in paths))

    def seen(self, path):
        row = self.db.execute("SELECT digest,since,invalid_since,retry,observed FROM temp.settling WHERE path=?",
                              (path,)).fetchone()
        return edit_back._Seen(*row[:4]) if row is not None and row[4] else None

    def remember(self, path, seen, *, detached=False):
        self.db.execute(
            "INSERT INTO temp.settling VALUES (?,?,?,?,?,0,1,?,?) ON CONFLICT(path) DO UPDATE SET "
            "digest=excluded.digest,since=excluded.since,invalid_since=excluded.invalid_since,"
            "retry=excluded.retry,pending=0,observed=1,detached=excluded.detached,due=excluded.due",
            (path, seen.digest, seen.since, seen.invalid_since, seen.retry, int(detached), seen.due()))

    def forget(self, path):
        self.db.execute("DELETE FROM temp.settling WHERE path=?", (path,))

    def due(self, now, limit):
        return self.db.execute(
            "SELECT path FROM temp.settling WHERE detached=0 AND due<=? ORDER BY due,path LIMIT ?",
            (now, limit)).fetchall()

    def wait(self, now):
        if self.phase not in ("idle", "candidates"):
            return 0.0
        candidate_due = None
        if self.phase == "candidates":
            candidate = self.db.execute("SELECT due FROM temp.candidates WHERE handled=0 ORDER BY due,path LIMIT 1").fetchone()
            if candidate is None:
                return 0.0
            candidate_due = candidate[0]
        due = self.db.execute("SELECT due FROM temp.settling WHERE detached=0 ORDER BY due,path LIMIT 1").fetchone()
        due = None if due is None else due[0]
        return max(0.0, min(self.next_full, float("inf") if due is None else due,
                            float("inf") if candidate_due is None else candidate_due) - now)

    def missing_ready(self, cid):
        if self.phase not in ("observe", "idle"):
            return False
        row = self.db.execute("SELECT complete FROM temp.roots WHERE cid=?", (cid,)).fetchone()
        return row is not None and bool(row[0])

    def start(self, now):
        self._close_directory()
        self.phase, self.after = "reset", ""
        self.next_full, self.restart = now + edit_back.FULL_PASS_SECONDS, False
        self._failure_count = 0
        self._hint_tail = self.db.execute("SELECT coalesce(max(rowid),0) FROM temp.hints").fetchone()[0]
        self._hint_row, self._hint_active = 0, None

    def fail(self, cid, path, code):
        self.db.execute("UPDATE temp.roots SET failed=1,complete=0 WHERE cid=?", (cid,))
        if self.db.execute("SELECT 1 FROM temp.failures WHERE path=?", (path,)).fetchone() is None:
            self._failure_count += 1
        self.db.execute("INSERT OR REPLACE INTO temp.failures VALUES (?,?)", (path, code))
        logger.warning("collection view reconciliation remains incomplete: %s (%s)", path, code)

    def status(self):
        failed = self._failure_count
        return {"status": "incomplete" if failed else "current" if self.phase == "idle" else "running",
                "failures": failed, "source": "current handle view census"}

    def step(self, writer, *, now, limit, candidate):
        if self.phase == "idle":
            if now < self.next_full:
                return
            self.start(now)
        if self.phase == "reset":
            for table in ("roots", "directories", "projections", "candidates", "outmoves", "failures"):
                # The names are the closed TEMP schema above; no user input reaches SQL identifiers.
                removed = self.db.execute(f"DELETE FROM temp.{table} WHERE rowid IN "
                                          f"(SELECT rowid FROM temp.{table} LIMIT ?)", (limit,)).rowcount
                if removed:
                    return
            self.phase = "catalog"
        elif self.phase == "catalog":
            self._catalog(writer, limit)
        elif self.phase == "projections":
            rows = self.db.execute(
                "SELECT path,collection_id,kind FROM main.projection_state WHERE path>? ORDER BY path LIMIT ?",
                (self.after, limit)).fetchall()
            self.db.executemany("INSERT OR REPLACE INTO temp.projections VALUES (?,?,?)", rows)
            if rows:
                self.after = rows[-1][0]
            else:
                self.phase, self.after = "hints", ""
        elif self.phase == "hints":
            self._hints(limit)
        elif self.phase == "enumerate":
            self._enumerate(limit, now)
        elif self.phase == "complete":
            rows = self.db.execute("SELECT cid FROM temp.roots WHERE cid>? ORDER BY cid LIMIT ?",
                                   (self.after, limit)).fetchall()
            self.db.executemany("UPDATE temp.roots SET complete=1 WHERE cid=? AND failed=0", rows)
            if rows:
                self.after = rows[-1][0]
            else:
                self.phase = "outmoves"
        elif self.phase == "outmoves":
            rows = self.db.execute("SELECT old_path,path,cid FROM temp.outmoves WHERE done=0 ORDER BY old_path LIMIT ?",
                                   (limit,)).fetchall()
            for old, path, cid in rows:
                try:
                    with held_fs.acquire(self._root).require() as fs, fs.parent(str(Path(path).parent)).require() as parent:
                        opened = fs.file(parent, Path(path).name)
                        if opened.ok:
                            with opened.require() as file:
                                self._candidate(fs, file, path, cid, now, old=old)
                        elif opened.error.code != "MISSING":
                            raise opened.error
                        fs.validate_directory(parent).require()
                except (held_fs.HeldFsError, OSError) as error:
                    if getattr(error, "code", None) != "MISSING":
                        self.fail(cid, path, getattr(error, "code", "IO_ERROR"))
                self.db.execute("UPDATE temp.outmoves SET done=1 WHERE old_path=?", (old,))
            if not rows:
                self.phase = "candidates"
        elif self.phase == "candidates":
            rows = self.db.execute(
                "SELECT c.path,c.cid,c.item_key,c.stamp,c.digest,c.stat,c.version,c.old_path FROM temp.candidates c "
                "WHERE c.handled=0 AND c.due<=? ORDER BY c.due,c.path LIMIT ?", (now, limit)).fetchall()
            for row in rows:
                complete = self.db.execute("SELECT complete FROM temp.roots WHERE cid=?", (row[1],)).fetchone()
                if complete != (1,) or candidate(row, now):
                    self.db.execute("UPDATE temp.candidates SET handled=1 WHERE path=?", (row[0],))
            if not self.db.execute("SELECT 1 FROM temp.candidates WHERE handled=0 LIMIT 1").fetchone():
                self.phase, self.after = "observe", ""
        elif self.phase == "observe":
            return  # EditBack pages the current canonical paths after possible moves.
        elif self.phase == "finish":
            self.finish(limit)

    def _catalog(self, writer, limit):
        rows = self.db.execute(
            "SELECT c.collection_id,c.generation,c.manifest_path,m.manifest_text,c.view_mode,c.layout "
            "FROM main.collections c JOIN main.collection_manifests m ON m.collection_id=c.collection_id "
            "AND m.manifest_version=c.manifest_version WHERE c.collection_id>? ORDER BY c.collection_id LIMIT ?",
            (self.after, limit)).fetchall()
        # One ownership proof for the step; each collection is still admitted or not on its own.
        admitted = authority.admitted_collections(writer) if rows else frozenset()
        for cid, generation, path, text, mode, layout in rows:
            if cid not in admitted:
                continue
            manifest = collections.parse_manifest_bytes(writer.root, path, text.encode())
            if not capability.summary_route_open(writer.root, manifest):
                continue
            self.db.execute("INSERT INTO temp.roots(cid,source,held_root,manifest,generation) VALUES (?,?,?,?,?)",
                            (cid, manifest.storage.source, records._held_directory(manifest), text, generation))
            if mode == "items" and layout == "markdown-items":
                self.db.execute("INSERT INTO temp.directories(path,cid) VALUES (?,?)", (manifest.storage.source, cid))
        if rows:
            self.after = rows[-1][0]
        else:
            self.phase, self.after = "projections", ""

    def _enumerate(self, limit, now):
        if self._iterator is None:
            row = self.db.execute("SELECT path,cid FROM temp.directories WHERE done=0 ORDER BY path LIMIT 1").fetchone()
            if row is None:
                self.phase, self.after = "complete", ""
                return
            self._directory, self._cid = row
            try:
                self._fs = held_fs.acquire(self._root).require()
                self._parent = self._fs.parent(self._directory).require()
                self._iterator = self._fs.iter_names(self._parent)
                self._manifest = self.db.execute("SELECT manifest FROM temp.roots WHERE cid=?", (self._cid,)).fetchone()[0]
            except (held_fs.HeldFsError, OSError) as error:
                if getattr(error, "code", None) != "MISSING":
                    self.fail(self._cid, self._directory, getattr(error, "code", "IO_ERROR"))
                self.db.execute("UPDATE temp.directories SET done=1 WHERE path=?", (self._directory,))
                self._close_directory()
                return
        try:
            for _ in range(limit):
                name = next(self._iterator)
                path = f"{self._directory}/{name}"
                held_root = self.db.execute("SELECT held_root FROM temp.roots WHERE cid=?", (self._cid,)).fetchone()[0]
                # Publication token prefixes are closed internal file protocols, never detached input.
                if (path == held_root or path.startswith(held_root + "/")
                        or name.startswith((views.STAGE_PREFIX, views.ASIDE_PREFIX))
                        or vault._is_registered_internal_state_artifact(path)):
                    continue
                opened = self._fs.file(self._parent, name)
                if not opened.ok:
                    directory = self._fs.parent(path)
                    if directory.ok:
                        directory.require().close()
                        self.db.execute("INSERT OR IGNORE INTO temp.directories(path,cid) VALUES (?,?)", (path, self._cid))
                        continue
                    self.fail(self._cid, path, opened.error.code)
                    continue
                with opened.require() as file:
                    if Path(name).suffix.lower() != ".md" or self.db.execute(
                            "SELECT 1 FROM temp.projections WHERE path=?", (path,)).fetchone():
                        continue
                    self._candidate(self._fs, file, path, self._cid, now)
            self._fs.validate_directory(self._parent).require()
        except StopIteration:
            try:
                self._fs.validate_directory(self._parent).require()
            except (held_fs.HeldFsError, OSError) as error:
                self.fail(self._cid, self._directory, getattr(error, "code", "IO_ERROR"))
            self.db.execute("UPDATE temp.directories SET done=1 WHERE path=?", (self._directory,))
            self._close_directory()
        except (held_fs.HeldFsError, OSError) as error:
            self.fail(self._cid, self._directory, getattr(error, "code", "IO_ERROR"))
            self.db.execute("UPDATE temp.directories SET done=1 WHERE path=?", (self._directory,))
            self._close_directory()

    def bind_root(self, root):
        self._root = Path(root)

    def _hints(self, limit):
        if self._hint_active is None:
            row = self.db.execute("SELECT rowid,path,destination,directory FROM temp.hints "
                                  "WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT 1",
                                  (self._hint_row, self._hint_tail)).fetchone()
            if row is None:
                self.phase, self.after = "enumerate", ""
                return
            self._hint_active, self.after = row[0], ""
        else:
            row = self.db.execute("SELECT rowid,path,destination,directory FROM temp.hints WHERE rowid=?",
                                  (self._hint_active,)).fetchone()
        identifier, source, destination, directory = row
        try:
            vault._safe_guard_target(destination)
            vault._safe_guard_target(source)
        except vault.PathGuardError:
            self._hint_row, self._hint_active = identifier, None
            return  # Absolute or unsafe endpoints never authorize a read outside the vault.
        sql = "SELECT p.path,p.cid,r.source FROM temp.projections p JOIN temp.roots r ON r.cid=p.cid WHERE p.kind='item' "
        if directory:
            # '/' has a fixed lexical successor; this range uses the path index for one exact subtree.
            rows = self.db.execute(sql + "AND p.path>? AND p.path>=? AND p.path<? ORDER BY p.path LIMIT ?",
                                   (self.after, source + "/", source + "0", limit)).fetchall()
        else:
            rows = self.db.execute(sql + "AND p.path=?", (source,)).fetchall()
        for old, cid, root in rows:
            target = destination + old[len(source):]
            if not target.startswith(root + "/"):
                self.db.execute("INSERT OR REPLACE INTO temp.outmoves(old_path,path,cid) VALUES (?,?,?)", (old, target, cid))
        if directory and len(rows) == limit:
            self.after = rows[-1][0]
        else:
            self._hint_row, self._hint_active = identifier, None

    def _candidate(self, fs, file, path, cid, now, *, old=None):
        before = file.version()
        raw = fs.read(file, max_bytes=record_formats._MAX_ITEM_BYTES).require()
        if file.version() != before:
            raise held_fs.HeldFsError("INPUT_CHANGED", "detached candidate changed")
        item_key, stamp = None, None
        try:
            fm, _body, _marker = vault.parse_frontmatter(record_formats._decode_item_bytes(raw), strict=True)
            manifest_path, text = self.db.execute(
                "SELECT c.manifest_path,r.manifest FROM main.collections c JOIN temp.roots r ON r.cid=c.collection_id "
                "WHERE c.collection_id=?", (cid,)).fetchone()
            manifest = collections.parse_manifest_bytes(self._root, manifest_path, text.encode())
            profile = record_formats.profile_for(manifest.semantic_profile)
            if isinstance(fm, Mapping) and fm.get("collection_id") == cid:
                item_key = memory_refs.normalize_id(fm.get(profile.item_id_property))
            stamp = edit_back._stamp(fm)
        except (ValueError, UnicodeError, collections.CollectionError):
            pass  # Malformed bytes remain detached input, never an inferred item.
        if old is not None:
            item = self.db.execute("SELECT item_key FROM main.items WHERE collection_id=? AND view_path=?", (cid, old)).fetchone()
            item_key = item[0] if item is not None else None
        digest = hashlib.sha256(raw).hexdigest()
        seen = self.seen(path)
        if seen is None or seen.digest != digest:
            seen = edit_back._Seen(digest, now)
            self.remember(path, seen, detached=True)
        self.db.execute("INSERT OR REPLACE INTO temp.candidates(path,cid,item_key,stamp,digest,stat,version,conflict,old_path,due) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)", (path, cid, item_key, json.dumps(stamp), digest,
                        edit_back._stat(before), json.dumps(asdict(before)),
                        int(sync_providers.is_conflict_copy(self._root, Path(path).name)), old, seen.due()))

    def finish(self, limit):
        removed = self.db.execute("DELETE FROM temp.hints WHERE rowid IN "
            "(SELECT rowid FROM temp.hints WHERE rowid<=? LIMIT ?)", (self._hint_tail, limit)).rowcount
        rows = self.db.execute("SELECT rowid,path,detached FROM temp.settling WHERE rowid>? AND rowid<=? "
                               "ORDER BY rowid LIMIT ?", (self._finish_after, self._finish_tail, limit)).fetchall()
        for identifier, path, detached in rows:
            live = self.db.execute("SELECT 1 FROM temp.candidates WHERE path=? AND handled=0" if detached else
                                   "SELECT 1 FROM main.projection_state WHERE path=?", (path,)).fetchone()
            if live is None:
                self.forget(path)
            self._finish_after = identifier
        if removed or rows:
            return
        self.phase, self.after = "idle", ""
        if self.restart:
            self.next_full = 0.0

    def observation_paths(self, limit):
        rows = self.db.execute(
            "SELECT path FROM main.projection_state WHERE kind IN (SELECT value FROM json_each(?)) "
            "AND path>? ORDER BY path LIMIT ?", (json.dumps(edit_back.OBSERVED_KINDS), self.after, limit)).fetchall()
        if rows:
            self.after = rows[-1][0]
        else:
            self.phase = "finish"
            self._finish_after = 0
            self._finish_tail = self.db.execute("SELECT coalesce(max(rowid),0) FROM temp.settling").fetchone()[0]
        return [row[0] for row in rows]
