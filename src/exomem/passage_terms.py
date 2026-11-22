"""Lexical terms of the embedding sidecar's chunk rows, for passage ranking.

`chunk_terms` holds the `bm25.tokenize` stems of each chunk row's text under that row's
rowid, and `chunk_terms_names` holds the row's path and chunk number under the same rowid.
A connection that `attach` prepares writes a row's terms and name in the statement that
writes the row, deletes them in the statement that deletes it, and keeps the count of rows
in `meta`. Those triggers live on the connection only, so an older release that opens the
sidecar never meets a function it lacks. A write it makes leaves `COVERAGE` behind the
sidecar's generation, and lexical ranking then waits until `backfill` proves the tables
whole again.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Set as AbstractSet

from . import sidecar_store

TABLE = "chunk_terms"
_VOCABULARY = "chunk_terms_vocab"
#: Each chunk row's path and chunk number by rowid, so naming a match reads no chunk row.
NAMES = "chunk_terms_names"
#: The `meta` key of the number of chunk rows, which term selection needs on every search.
ROWS = "chunk_terms_rows"
#: The sidecar generation at which every chunk row had its terms and name, no other row had
#: either, and `ROWS` counted the chunk rows.
COVERAGE = "chunk_terms_named_through"
#: The marker of the release that kept terms without names. Its triggers still move it, so
#: this release keeps its own marker; a rebuild deletes this one too, because that release
#: would trust a half-rebuilt terms table after a rollback.
_TERMS_ONLY_COVERAGE = "chunk_terms_through"
_STEMS = "exomem_chunk_stems"
#: Rows one backfill transaction indexes, so a writer never waits long behind it.
BACKFILL_ROWS = 2000


def _stems(text: str | None) -> str:
    # Imported here: bm25 reaches find, which reaches the embedding index that imports this.
    from . import bm25

    return " ".join(bm25.tokenize(text or ""))


def term_budget():
    """How much of a query reaches the MATCH, from the active ranking configuration.

    The MATCH reads every row of each kept stem, so the rows of common units set its cost.
    From `passage_terms_common_min_chunks` (500) chunks, a unit on more than
    `passage_terms_common_fraction` (3%) of them is dropped, and the rarest
    `passage_terms_max_units` (6) of the rest are kept. Below that many chunks the MATCH is
    cheap whatever it reads, so no unit is common.

    On 49 real questions over 33,454 chunks, fused key coverage at k = 5, 15 and 30 was 9,
    21 and 25 with this budget, and 11, 20 and 27 with the previous one (the rarest 16
    units under 10%). The budget keeps the top-15 gain and gives up two cases at top 5 and
    at top 30, for a lexical stage that reads fewer rows. It was chosen on those same 49
    questions, so its one-case top-15 gain is in-sample. On the 40,500-chunk reference, the
    lexical stage alone, timed in process over 40 queries with every count read, had a p95
    of 3.1 to 4.1 ms with this budget and 14.6 ms with the previous one.
    """
    from . import lexstore, ranking_config

    config = ranking_config._active_ranking()
    return lexstore.QueryTermBudget(
        max_units=config.passage_terms_max_units, max_stems=40,
        common_fraction=config.passage_terms_common_fraction,
        common_min_pages=config.passage_terms_common_min_chunks, common_after_units=0,
    )


def exists(conn: sqlite3.Connection) -> bool:
    """Whether the terms and names tables both exist; a release before names made only terms."""
    return conn.execute(
        "SELECT count(*) FROM main.sqlite_master WHERE type = 'table' AND name IN (?, ?)",
        (TABLE, NAMES),
    ).fetchone()[0] == 2


def _triggers() -> dict[str, str]:
    """The connection's TEMP triggers, by name, that keep the terms rows level with the chunks."""
    # TEMP triggers may name main tables; their bodies resolve the unqualified names there.
    write = (
        f"DELETE FROM {TABLE} WHERE rowid = NEW.rowid; "
        f"INSERT INTO {TABLE}(rowid, terms) VALUES (NEW.rowid, {_STEMS}(NEW.chunk_text)); "
        f"DELETE FROM {NAMES} WHERE chunk_rowid = NEW.rowid; "
        f"INSERT INTO {NAMES} VALUES (NEW.rowid, NEW.file_path, NEW.chunk_idx);"
    )
    erase = (
        f"DELETE FROM {TABLE} WHERE rowid = OLD.rowid; "
        f"DELETE FROM {NAMES} WHERE chunk_rowid = OLD.rowid;"
    )
    return {
        f"{TABLE}_insert": (
            f"AFTER INSERT ON main.chunks BEGIN {write} "
            f"UPDATE meta SET value = value + 1 WHERE key = '{ROWS}'; END"
        ),
        f"{TABLE}_delete": (
            f"AFTER DELETE ON main.chunks BEGIN {erase} "
            f"UPDATE meta SET value = value - 1 WHERE key = '{ROWS}'; END"
        ),
        f"{TABLE}_update": f"AFTER UPDATE ON main.chunks BEGIN {erase} {write} END",
        # A write on this connection keeps the tables whole, so coverage moves with it,
        # but only from the generation it already covered.
        f"{TABLE}_coverage": (
            "AFTER UPDATE OF value ON main.meta WHEN NEW.key = 'generation' BEGIN "
            f"UPDATE meta SET value = NEW.value WHERE key = '{COVERAGE}' AND value = OLD.value; END"
        ),
    }


def attach(conn: sqlite3.Connection) -> None:
    """Bind this connection's chunk writes to their terms rows once the tables exist."""
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
    """Bring the terms and names level with the chunk rows, and record the generation they match.

    Tables with coverage are caught up from the sidecar's path log: the rows of every path
    written since the covered generation are derived again, so a writer without the
    triggers leaves no stale terms, even under a reused rowid. When the log does not reach
    back that far, the tables are rebuilt in batches that each commit on their own, then
    caught up from the generation at which the rebuild began. False means writes outran
    the log during the rebuild, and coverage stays unrecorded.
    """
    from .lexstore import _FTS_TOKENIZE

    create = (
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {TABLE} USING fts5(terms, {_FTS_TOKENIZE})",
        f"CREATE TABLE IF NOT EXISTS {NAMES} "
        "(chunk_rowid INTEGER PRIMARY KEY, file_path TEXT NOT NULL, chunk_idx INTEGER NOT NULL)",
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        for statement in create:
            conn.execute(statement)
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {_VOCABULARY} USING fts5vocab({TABLE}, 'row')"
        )
        since, generation = _meta_int(conn, COVERAGE), _meta_int(conn, "generation") or 0
        changed = _changed_since(conn, log, since, generation)
        if changed is None or len(changed) > BACKFILL_ROWS:
            # Dropping is one step where deleting every row is one per row.
            conn.execute(f"DROP TABLE {TABLE}")
            conn.execute(f"DROP TABLE {NAMES}")
            for statement in create:
                conn.execute(statement)
            conn.execute(
                "DELETE FROM meta WHERE key IN (?, ?)", (COVERAGE, _TERMS_ONLY_COVERAGE)
            )
            since = generation
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    missing = (
        "SELECT c.rowid, c.file_path, c.chunk_idx, c.chunk_text FROM chunks AS c "
        f"WHERE c.rowid > ? AND NOT EXISTS (SELECT 1 FROM {NAMES} AS n "
        "WHERE n.chunk_rowid = c.rowid) ORDER BY c.rowid LIMIT ?"
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
            "SELECT rowid, file_path, chunk_idx, chunk_text FROM chunks WHERE file_path IN "
            "(SELECT value FROM json_each(?))",
            (json.dumps(changed, ensure_ascii=False),),
        ).fetchall()
        _write(conn, rows)
        conn.execute(f"DELETE FROM {TABLE} WHERE rowid NOT IN (SELECT rowid FROM chunks)")
        conn.execute(f"DELETE FROM {NAMES} WHERE chunk_rowid NOT IN (SELECT rowid FROM chunks)")
        whole = conn.execute(missing, (0, 1)).fetchone() is None
        if whole:
            for key, value in (
                (ROWS, conn.execute("SELECT count(*) FROM chunks").fetchone()[0]),
                (COVERAGE, generation),
            ):
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return whole


def _write(conn: sqlite3.Connection, rows: list[tuple[int, str, int, str]]) -> None:
    rowids = [(rowid,) for rowid, _path, _chunk, _text in rows]
    conn.executemany(f"DELETE FROM {TABLE} WHERE rowid = ?", rowids)
    conn.executemany(
        f"INSERT INTO {TABLE}(rowid, terms) VALUES (?, ?)",
        ((rowid, _stems(text)) for rowid, _path, _chunk, text in rows),
    )
    conn.executemany(f"DELETE FROM {NAMES} WHERE chunk_rowid = ?", rowids)
    conn.executemany(
        f"INSERT INTO {NAMES} VALUES (?, ?, ?)",
        ((rowid, path, chunk) for rowid, path, chunk, _text in rows),
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


class Counts:
    """The row counts that term selection reads, for one sidecar token.

    `rows` is the number of chunk rows, which the triggers keep in `meta`. Each stem's count
    of rows is read once per `token` (the epoch, chunk generation and instance of a covered
    sidecar), by the first search that names it, because counting reads the stem's whole
    list of rows. A stem on no row is not kept, so the counts never outgrow the vocabulary.
    """

    def __init__(self, conn: sqlite3.Connection, token: tuple[int, int, int]) -> None:
        self.token = token
        self.rows = _meta_int(conn, ROWS) or 0
        self._documents: dict[str, int] = {}

    def documents(self, conn: sqlite3.Connection, stems: list[str]) -> dict[str, int]:
        """The rows holding each of `stems` that any row holds, read in a snapshot at `token`."""
        missing = [stem for stem in stems if stem not in self._documents]
        if missing:
            # One equality lookup per stem; a subquery here would scan the whole vocabulary.
            # Searches at one token read the same counts, so a concurrent update is harmless.
            self._documents.update(conn.execute(
                f"SELECT term, doc FROM {_VOCABULARY} WHERE term IN ({', '.join('?' * len(missing))})",
                missing,
            ).fetchall())
        return {stem: self._documents[stem] for stem in stems if stem in self._documents}


def select(
    conn: sqlite3.Connection, query: str, k: int, eligible: AbstractSet[str], counts: Counts,
) -> list[tuple[str, int, float]]:
    """Eligible `(file_path, chunk_idx, bm25)` rows best first, in the caller's snapshot.

    `counts` must be the counts for the snapshot's token. The corpus statistics span the
    sidecar. Rows of ineligible paths never become candidates, and ties keep the physical
    row order.
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
        units, counts.documents(conn, stems), counts.rows, term_budget(), min_units=0
    )
    tokens = list(dict.fromkeys(stem for unit in kept for stem in unit.stems))
    if not tokens:
        return []
    # Stems are runs of letters, numbers and marks, so no FTS5 syntax can hide in them.
    match = " OR ".join(f'"{token}"' for token in tokens)
    # The terms table alone orders the matches, and the names table names them a batch at
    # a time: a join or lookup of each match's chunk row read one random page of that large
    # table per candidate, and a join here would name every match before the sort.
    ranked = conn.execute(
        f"SELECT rowid, -bm25({TABLE}) AS score FROM {TABLE} WHERE {TABLE} MATCH ? "
        "ORDER BY score DESC, rowid",
        (match,),
    )
    found: list[tuple[str, int, float]] = []
    try:
        while len(found) < k and (batch := ranked.fetchmany(k)):
            names = {
                rowid: (path, int(chunk))
                for rowid, path, chunk in conn.execute(
                    f"SELECT chunk_rowid, file_path, chunk_idx FROM {NAMES} "
                    "WHERE chunk_rowid IN (SELECT value FROM json_each(?))",
                    (json.dumps([rowid for rowid, _score in batch]),),
                )
            }
            for rowid, score in batch:
                named = names.get(rowid)
                if named is not None and named[0] in eligible:
                    found.append((*named, float(score)))
                    if len(found) == k:
                        break
    finally:
        ranked.close()
    return found
