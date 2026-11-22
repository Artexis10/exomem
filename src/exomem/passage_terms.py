"""Lexical terms of the embedding sidecar's chunk rows, for passage ranking.

`chunk_terms` holds the `bm25.tokenize` stems of each chunk row's text under that row's
rowid. A connection that `attach` prepares writes a row's terms in the statement that
writes the row, and deletes them in the statement that deletes it. Those triggers live
on the connection only, so an older release that opens the sidecar never meets a
function it lacks. A write it makes leaves `COVERAGE` behind the sidecar's generation,
and lexical ranking then waits until `backfill` proves the table whole again.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Set as AbstractSet

from . import sidecar_store

TABLE = "chunk_terms"
_VOCABULARY = "chunk_terms_vocab"
#: The sidecar generation at which every chunk row had its terms row, and no other row had one.
COVERAGE = "chunk_terms_through"
_STEMS = "exomem_chunk_stems"
#: Rows one backfill transaction indexes, so a writer never waits long behind it.
BACKFILL_ROWS = 2000


def _stems(text: str | None) -> str:
    # Imported here: bm25 reaches find, which reaches the embedding index that imports this.
    from . import bm25

    return " ".join(bm25.tokenize(text or ""))


def term_budget():
    """How much of a query reaches the MATCH.

    The MATCH reads every row of each kept stem, so the rows of common units set its cost.
    From 500 chunks, a unit on more than 3% of them is dropped, and the rarest 6 of the
    rest are kept. Below 500 chunks the MATCH is cheap whatever it reads, so no unit is
    common. On 49 real questions over 33,454 chunks, fused top-15 coverage was 21 of 49
    (20 for the previous budget), and on 40,500 chunks the lexical stage measured p95
    about 4 ms in process (15 ms).
    """
    from . import lexstore

    return lexstore.QueryTermBudget(
        max_units=6, max_stems=40, common_fraction=0.03, common_min_pages=500,
        common_after_units=0,
    )


def exists(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM main.sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)
    ).fetchone() is not None


def _triggers() -> dict[str, str]:
    """The connection's TEMP triggers, by name, that keep the terms rows level with the chunks."""
    # TEMP triggers may name main tables; their bodies resolve the unqualified names there.
    write = (
        f"DELETE FROM {TABLE} WHERE rowid = NEW.rowid; "
        f"INSERT INTO {TABLE}(rowid, terms) VALUES (NEW.rowid, {_STEMS}(NEW.chunk_text));"
    )
    return {
        f"{TABLE}_insert": f"AFTER INSERT ON main.chunks BEGIN {write} END",
        f"{TABLE}_delete": (
            f"AFTER DELETE ON main.chunks BEGIN DELETE FROM {TABLE} WHERE rowid = OLD.rowid; END"
        ),
        f"{TABLE}_update": (
            f"AFTER UPDATE ON main.chunks BEGIN DELETE FROM {TABLE} WHERE rowid = OLD.rowid; "
            f"{write} END"
        ),
        # A write on this connection keeps the table whole, so coverage moves with it,
        # but only from the generation it already covered.
        f"{TABLE}_coverage": (
            "AFTER UPDATE OF value ON main.meta WHEN NEW.key = 'generation' BEGIN "
            f"UPDATE meta SET value = NEW.value WHERE key = '{COVERAGE}' AND value = OLD.value; END"
        ),
    }


def attach(conn: sqlite3.Connection) -> None:
    """Bind this connection's chunk writes to their terms rows once the table exists."""
    conn.create_function(_STEMS, 1, _stems, deterministic=True)
    if not exists(conn):
        return
    for name, definition in _triggers().items():
        conn.execute(f"CREATE TEMP TRIGGER IF NOT EXISTS {name} {definition}")


def suspend(conn: sqlite3.Connection) -> None:
    """Stop this connection deriving terms, and mark the terms rows out of date.

    Call it in a write transaction that rewrites every chunk row. Deriving each row's terms
    there holds the write lock as long as stemming the whole corpus takes. Without coverage,
    lexical ranking waits while the background backfill rebuilds the table in batches.
    """
    for name in _triggers():
        conn.execute(f"DROP TRIGGER IF EXISTS temp.{name}")
    conn.execute("DELETE FROM meta WHERE key = ?", (COVERAGE,))


def covered(conn: sqlite3.Connection, generation: int) -> bool:
    """Whether the terms rows match the chunk rows at `generation`, read in the caller's snapshot."""
    if not exists(conn):
        return False
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (COVERAGE,)).fetchone()
    return row is not None and int(row[0]) == generation


def backfill(conn: sqlite3.Connection, log: sidecar_store.PathChangeLog) -> bool:
    """Bring the terms rows level with the chunk rows, and record the generation they match.

    A table with coverage is caught up from the sidecar's path log: the rows of every path
    written since the covered generation are derived again, so a writer without the
    triggers leaves no stale terms, even under a reused rowid. When the log does not reach
    back that far, the table is rebuilt in batches that each commit on their own, then
    caught up from the generation at which the rebuild began. False means writes outran
    the log during the rebuild, and coverage stays unrecorded.
    """
    from .lexstore import _FTS_TOKENIZE

    create = f"CREATE VIRTUAL TABLE IF NOT EXISTS {TABLE} USING fts5(terms, {_FTS_TOKENIZE})"
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(create)
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {_VOCABULARY} USING fts5vocab({TABLE}, 'row')"
        )
        since, generation = _meta_int(conn, COVERAGE), _meta_int(conn, "generation") or 0
        changed = _changed_since(conn, log, since, generation)
        if changed is None or len(changed) > BACKFILL_ROWS:
            # Dropping is one step where deleting every row is one per row.
            conn.execute(f"DROP TABLE {TABLE}")
            conn.execute(create)
            conn.execute("DELETE FROM meta WHERE key = ?", (COVERAGE,))
            since = generation
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    missing = (
        f"SELECT c.rowid, c.chunk_text FROM chunks AS c WHERE c.rowid > ? AND NOT EXISTS "
        f"(SELECT 1 FROM {TABLE} AS t WHERE t.rowid = c.rowid) ORDER BY c.rowid LIMIT ?"
    )
    after = 0
    while True:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(missing, (after, BACKFILL_ROWS)).fetchall()
            _write(conn, rows)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        if len(rows) < BACKFILL_ROWS:
            break
        after = rows[-1][0]
    conn.execute("BEGIN IMMEDIATE")
    try:
        generation = _meta_int(conn, "generation") or 0
        changed = _changed_since(conn, log, since, generation)
        if changed is None or len(changed) > BACKFILL_ROWS:
            conn.commit()
            return False
        rows = conn.execute(
            "SELECT rowid, chunk_text FROM chunks WHERE file_path IN "
            "(SELECT value FROM json_each(?))",
            (json.dumps(changed, ensure_ascii=False),),
        ).fetchall()
        _write(conn, rows)
        conn.execute(f"DELETE FROM {TABLE} WHERE rowid NOT IN (SELECT rowid FROM chunks)")
        whole = conn.execute(missing, (0, 1)).fetchone() is None
        if whole:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (COVERAGE, generation),
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return whole


def _write(conn: sqlite3.Connection, rows: list[tuple[int, str]]) -> None:
    conn.executemany(f"DELETE FROM {TABLE} WHERE rowid = ?", ((rowid,) for rowid, _text in rows))
    conn.executemany(
        f"INSERT INTO {TABLE}(rowid, terms) VALUES (?, ?)",
        ((rowid, _stems(text)) for rowid, text in rows),
    )


def _meta_int(conn: sqlite3.Connection, key: str) -> int | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None or row[0] is None else int(row[0])


def _changed_since(
    conn: sqlite3.Connection, log: sidecar_store.PathChangeLog, since: int | None, generation: int,
) -> list[str] | None:
    """The paths written after `since`, or None when the log's run does not cover them all."""
    if since is None or since > generation:
        return None
    if since == generation:
        return []
    start, upto = sidecar_store.read_logged_run(conn, log)
    if start is None or start > since or upto != generation:
        return None
    return sidecar_store.changed_paths_since(conn, log, since, limit=BACKFILL_ROWS)


class Rows:
    """The chunk rows as the lexical stage reads them at one sidecar token.

    It holds each row's path and chunk number by rowid, so naming a match reads no chunk row,
    and each stem's count of rows. Every lexical search reads them. They change only with the
    chunk rows, so each set is kept for one `token` (the epoch, chunk generation and instance
    of a covered sidecar) and read again at the next. A complete set, read whole after a
    backfill, holds every stem's count. Otherwise a stem's count is read once, by the first
    search that names it; a stem on no row is not kept, so the set never outgrows the
    vocabulary.
    """

    def __init__(self, conn: sqlite3.Connection, token: tuple[int, int, int], *, complete: bool) -> None:
        self.token = token
        # The primary key index covers this read, so it scans no chunk row. One object per
        # path keeps the map small, since each path repeats once per chunk.
        paths: dict[str, str] = {}
        self.names: dict[int, tuple[str, int]] = {
            rowid: (paths.setdefault(path, path), int(chunk))
            for rowid, path, chunk in conn.execute("SELECT rowid, file_path, chunk_idx FROM chunks")
        }
        self._complete = complete
        self._documents: dict[str, int] = (
            dict(conn.execute(f"SELECT term, doc FROM {_VOCABULARY}").fetchall()) if complete else {}
        )

    def documents(self, conn: sqlite3.Connection, stems: list[str]) -> dict[str, int]:
        """The rows holding each of `stems` that any row holds, read in a snapshot at `token`."""
        missing = [] if self._complete else [stem for stem in stems if stem not in self._documents]
        if missing:
            # One equality lookup per stem; a subquery here would scan the whole vocabulary.
            # Searches at one token read the same counts, so a concurrent update is harmless.
            self._documents.update(conn.execute(
                f"SELECT term, doc FROM {_VOCABULARY} WHERE term IN ({', '.join('?' * len(missing))})",
                missing,
            ).fetchall())
        return {stem: self._documents[stem] for stem in stems if stem in self._documents}


def select(
    conn: sqlite3.Connection, query: str, k: int, eligible: AbstractSet[str], rows: Rows,
) -> list[tuple[str, int, float]]:
    """Eligible `(file_path, chunk_idx, bm25)` rows best first, in the caller's snapshot.

    `rows` must be the set for the snapshot's token. The corpus statistics span the sidecar.
    Rows of ineligible paths never become candidates, and ties keep the physical row order.
    """
    from . import bm25, lexstore

    if k <= 0 or not eligible:
        return []
    units = bm25.token_units(query, query=True)
    stems = sorted({
        stem
        for unit in units
        for stem in (bm25.run_content_stems(unit.stems) if unit.run else unit.stems)
    })
    if not stems:
        return []
    # A query of only common units would read most rows to add little to the dense
    # ranking, so it gets no lexical candidates rather than its rarest common units.
    kept, _counted, _dropped = lexstore.select_query_units(
        units, rows.documents(conn, stems), len(rows.names), term_budget(), min_units=0
    )
    tokens = list(dict.fromkeys(stem for unit in kept for stem in unit.stems))
    if not tokens:
        return []
    # Stems are runs of letters, numbers and marks, so no FTS5 syntax can hide in them.
    match = " OR ".join(f'"{token}"' for token in tokens)
    # The terms table alone orders the matches, and `rows` names them: a join or lookup of
    # each match's chunk row read one random page of that large table per candidate.
    ranked = conn.execute(
        f"SELECT rowid, -bm25({TABLE}) AS score FROM {TABLE} WHERE {TABLE} MATCH ? "
        "ORDER BY score DESC, rowid",
        (match,),
    )
    found: list[tuple[str, int, float]] = []
    try:
        for rowid, score in ranked:
            named = rows.names.get(rowid)
            if named is not None and named[0] in eligible:
                found.append((*named, float(score)))
                if len(found) == k:
                    break
    finally:
        ranked.close()
    return found
