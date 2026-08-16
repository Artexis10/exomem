"""SQLite sidecar lifecycle helpers shared by derived indexes."""

from __future__ import annotations

import logging
import random
import sqlite3
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger(__name__)

INSTANCE_MIN = 1
INSTANCE_MAX = 2**31 - 1
_POLICY_META_KEYS = (
    "recall_policy_version",
    "recall_access_fingerprint",
)


def apply_sidecar_pragmas(conn: sqlite3.Connection) -> None:
    """Apply WAL-oriented pragmas for local per-machine sidecars."""
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
    except sqlite3.Error as e:  # pragma: no cover - WAL unavailable on unusual FSes
        log.warning("sidecar WAL pragmas failed (%s); continuing on default journal", e)


def file_block(keys: list[str], rel_path: str) -> tuple[int, int]:
    """Locate `rel_path`'s contiguous row block in a sorted key list."""
    lo = hi = None
    for i, k in enumerate(keys):
        if k == rel_path:
            if lo is None:
                lo = i
            hi = i + 1
        elif hi is not None:
            break
    if lo is not None:
        return lo, hi
    ins = len(keys)
    for i, k in enumerate(keys):
        if k > rel_path:
            ins = i
            break
    return ins, ins


def ensure_meta_table(
    conn: sqlite3.Connection, data_table: str, sidecar_name: str
) -> None:
    """Create the generation-token table if absent."""
    existed = (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        is not None
    )
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER)")
    if not existed:
        instance = random.SystemRandom().randint(INSTANCE_MIN, INSTANCE_MAX)
        conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('instance', ?)",
            (instance,),
        )
        conn.commit()
        has_rows = (
            conn.execute(f"SELECT 1 FROM {data_table} LIMIT 1").fetchone() is not None
        )
        if has_rows:
            log.info(
                "%s: created generation-meta over an existing non-empty sidecar "
                "(legacy migration; mtime-keyed cache until the first gen-bumping write)",
                sidecar_name,
            )


def read_meta_token(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """Return `(epoch, generation, instance)` from a sidecar meta table."""
    rows = conn.execute(
        "SELECT key, value FROM meta WHERE key IN ('epoch', 'generation', 'instance')"
    ).fetchall()
    d = {k: v for k, v in rows}
    return (
        int(d.get("epoch") or 0),
        int(d.get("generation") or 0),
        int(d.get("instance") or 0),
    )


def bump_meta(conn: sqlite3.Connection, key: str) -> int:
    """Increment `meta[key]` inside the caller's open write transaction."""
    cur = conn.execute("UPDATE meta SET value = value + 1 WHERE key = ?", (key,))
    if cur.rowcount == 0:
        conn.execute("INSERT INTO meta (key, value) VALUES (?, 1)", (key,))
    return int(
        conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()[0]
    )


def read_recall_policy_identity(
    conn: sqlite3.Connection, *, table: str = "meta"
) -> tuple[str, str] | None:
    """Read the policy projection identity stamped on an existing sidecar.

    This deliberately stores only policy inputs, not a synthetic corpus
    checkpoint.  Callers still own the correct freshness/reconciliation model
    for their particular sidecar.
    """
    if table not in {"meta", "graph_meta"}:
        raise ValueError("unsupported sidecar metadata table")
    rows = dict(
        conn.execute(
            f"SELECT key, value FROM {table} WHERE key IN (?, ?)",
            _POLICY_META_KEYS,
        ).fetchall()
    )
    version = rows.get(_POLICY_META_KEYS[0])
    fingerprint = rows.get(_POLICY_META_KEYS[1])
    if not isinstance(version, str) or not isinstance(fingerprint, str):
        return None
    return version, fingerprint


def write_recall_policy_identity(
    conn: sqlite3.Connection,
    policy_version: str,
    access_policy_fingerprint: str,
    *,
    table: str = "meta",
) -> None:
    """Stamp semantic-sidecar policy inputs inside the caller's transaction."""
    if table not in {"meta", "graph_meta"}:
        raise ValueError("unsupported sidecar metadata table")
    if not policy_version or not access_policy_fingerprint:
        raise ValueError("recall policy identity must be nonempty")
    conn.executemany(
        f"INSERT INTO {table}(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (
            (_POLICY_META_KEYS[0], policy_version),
            (_POLICY_META_KEYS[1], access_policy_fingerprint),
        ),
    )


def reload_reason(old_cache, new_epoch: int, new_gen: int) -> str:
    """Observability tag for a full matrix load: cold|legacy|epoch|genuine."""
    if old_cache is None:
        return "cold"
    if new_gen == 0:
        return "legacy"
    if new_epoch != old_cache.epoch:
        return "epoch"
    return "genuine"


# ------------------------------------------------------------ bounded catch-up
#
# A generation delta says HOW MANY writes happened, never WHICH rows they
# touched: one bump can retire a whole batch of paths (a purge) and many bumps
# can rewrite one path (repeated upserts). So the delta alone cannot drive an
# incremental read — the sidecar has to record what each generation changed.
#
# `<table>(file_path, generation)` is that record: one row per path, holding the
# generation at which it was LAST mutated (inserted, updated, or deleted — a
# delete leaves its log row behind precisely so the reader can see it). It is
# written inside the same transaction as the generation bump, so a path appears
# in the delta iff its rows changed in that window.
#
# THE DANGEROUS CASE is a generation bump that wrote NO log row: the reader would
# read it as "this write changed nothing" and let a stale cache advance its label
# without the rows. That is not hypothetical — a generation-AWARE binary that
# predates this log (0.52.2) bumps the generation and writes no log row, and
# `scripts/upgrade.ps1` can leave exactly that CLI pointed at a vault a newer
# service also writes. On the pre-catch-up code such a writer was safe, because
# ANY bump invalidated the matrix; the catch-up must not regress that.
#
# A row count cannot detect it (an in-place re-embed of one chunk preserves the
# count) and neither can "the newest generation was logged" (a gap earlier in the
# window still passes). So the two markers in `meta` bound a CONTIGUOUS RUN of
# logged generations: `<upto_key>` is the newest logged generation and
# `<from_key>` the oldest generation of the unbroken run ending there. Each
# logged bump extends the run, or — finding itself not adjacent to the previous
# logged generation — restarts it, permanently excluding the window containing
# the gap. Catch-up then requires `upto == gen` (the newest generation is logged)
# AND `cache.generation >= from` (every generation since is too). Anything else,
# plus gen 0, an epoch change, or an instance change, takes the full reload.

CATCHUP_REASON = "catchup"


class PathChangeLog(NamedTuple):
    """Names of one sidecar's change-log table and its two `meta` run markers."""

    table: str
    from_key: str
    upto_key: str

    def validate(self) -> None:
        """Reject a non-identifier table name (it is interpolated into SQL)."""
        if not self.table.isidentifier():
            raise ValueError(f"unsupported change-log table name: {self.table!r}")


def ensure_path_change_log(conn: sqlite3.Connection, log: PathChangeLog) -> None:
    """Create the per-path change log if absent, in the sidecar's connect path.

    Call this BEFORE any write on the connection, so every writer that goes
    through it records what it changed.

    Creating the table also CLEARS the run markers: a table that had to be
    created while markers survived means the log was dropped out from under them
    (a repair, a partial restore), and an empty log must never read as "nothing
    changed". Catch-up then stays refused until a fresh run is established.
    """
    log.validate()
    existed = (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (log.table,)
        ).fetchone()
        is not None
    )
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {log.table} ("
        "file_path TEXT PRIMARY KEY, generation INTEGER NOT NULL)"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS {log.table}_generation ON {log.table}(generation)"
    )
    if not existed:
        conn.execute(
            "DELETE FROM meta WHERE key IN (?, ?)", (log.from_key, log.upto_key)
        )
        conn.commit()


def _extend_logged_run(conn: sqlite3.Connection, log: PathChangeLog, generation: int) -> None:
    """Advance the contiguous logged run to `generation`, in the caller's txn.

    When the previous logged generation is not `generation - 1`, some bump in
    between bypassed these helpers and its rows are unrecorded, so the run
    RESTARTS here instead of extending — that is what keeps a gap from being
    waved through once a later write is logged normally.
    """
    previous = _read_meta_int(conn, log.upto_key)
    if previous != generation - 1:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (log.from_key, generation - 1),
        )
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (log.upto_key, generation),
    )


def bump_generation_for_paths(conn: sqlite3.Connection, log: PathChangeLog, paths) -> int:
    """Bump `generation` AND record the paths it changed, in the caller's txn.

    Bumping and recording are ONE call by design. The reader treats an unlogged
    generation as "this write changed no rows", so a bump that forgot to declare
    its paths would let a stale cache advance its label without ever receiving
    the rows — the same corruption the write-side contiguity gate exists to
    prevent, arriving through the read side instead.

    Safe against concurrent writers: SQLite serializes write transactions, so the
    read-modify-write of the run markers happens under the write lock this
    transaction already holds.
    """
    log.validate()
    generation = bump_meta(conn, "generation")
    rows = [(path, generation) for path in dict.fromkeys(paths)]
    if rows:
        conn.executemany(
            f"INSERT INTO {log.table} (file_path, generation) VALUES (?, ?) "
            "ON CONFLICT(file_path) DO UPDATE SET generation = excluded.generation",
            rows,
        )
    _extend_logged_run(conn, log, generation)
    return generation


def bump_generation_for_reset(conn: sqlite3.Connection, log: PathChangeLog) -> int:
    """Bump `generation` for a whole-table rewrite, in the caller's txn.

    A wipe-and-rebuild changes every path at once; enumerating them would make
    the log as big as the corpus and buy nothing, since such a write also bumps
    the epoch and no cache may be caught up across that. So the log is emptied
    and a fresh run starts at the new generation — catch-up resumes from the next
    ordinary write.
    """
    log.validate()
    generation = bump_meta(conn, "generation")
    conn.execute(f"DELETE FROM {log.table}")
    conn.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        ((log.from_key, generation), (log.upto_key, generation)),
    )
    return generation


def _read_meta_int(conn: sqlite3.Connection, key: str) -> int | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0])


def read_logged_run(conn: sqlite3.Connection, log: PathChangeLog) -> tuple[int | None, int | None]:
    """`(from, upto)` — the contiguous run of generations the log fully covers."""
    return _read_meta_int(conn, log.from_key), _read_meta_int(conn, log.upto_key)


def changed_paths_since(
    conn: sqlite3.Connection, log: PathChangeLog, generation: int, *, limit: int
) -> list[str]:
    """Paths mutated strictly after `generation`, per the change log.

    Reads `limit + 1` rows so the caller can detect "too many to be worth
    patching" without materializing a delta it is going to throw away.

    Read this in the SAME explicit transaction as the meta token and the rows it
    selects — python sqlite3 in autocommit gives every bare SELECT its own
    snapshot, so a split read could pair a generation with another write's log.
    """
    log.validate()
    return [
        str(row[0])
        for row in conn.execute(
            f"SELECT file_path FROM {log.table} WHERE generation > ? LIMIT ?",
            (generation, max(limit, 0) + 1),
        )
    ]


def catchup_is_eligible(
    c,
    epoch: int,
    gen: int,
    instance: int,
    *,
    run: tuple[int | None, int | None],
    max_generations: int,
) -> bool:
    """Whether a warm cache may be patched forward instead of fully reloaded.

    ONLY a forward generation delta on the same physical sidecar, entirely
    covered by the log's contiguous run, qualifies:

    * `upto == gen` — the sidecar's CURRENT generation was logged. Any bump that
      bypassed the logging helpers (an older generation-aware binary, a future
      in-tree writer, a manual repair) leaves `upto < gen` and is refused here.
    * `c.generation >= run_from` — and so was every generation since the cache
      was labelled, because the run is unbroken by construction.

    An epoch change (a re-embed replaced vectors the log never names), an
    instance change (the ABA case: the sidecar was deleted and recreated, so its
    counters restarted and its rows are unrelated), and a generation-0 legacy
    sidecar (mtime-keyed, no log history) all still force the full reload.
    """
    run_from, run_upto = run
    if c is None or gen < 1 or c.generation < 1:
        return False
    if c.epoch != epoch or c.instance != instance:
        return False
    if gen <= c.generation:  # already fresh, or a sidecar that moved backwards
        return False
    if run_upto != gen:
        return False  # the newest generation was not recorded by the log
    if run_from is None or c.generation < run_from:
        return False  # the window reaches back past a gap in the log
    return (gen - c.generation) <= max_generations


def peek_sidecar_token(path: Path) -> tuple[int, int, int] | None:
    """Read `(epoch, generation, instance)` without creating or migrating a sidecar."""
    if not path.exists():
        return (0, 0, 0)
    try:
        conn = sqlite3.connect(path)
        try:
            has_meta = (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
                ).fetchone()
                is not None
            )
            return read_meta_token(conn) if has_meta else (0, 0, 0)
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def sidecar_cache_token(path: Path) -> tuple[int, int, int]:
    """Read-only cache token for hot-cache freshness keys."""
    return peek_sidecar_token(path) or (0, 0, 0)


def cache_is_fresh(c, path: Path, epoch: int, gen: int, instance: int) -> bool:
    """Return whether a warm matrix cache can serve the current sidecar token."""
    if gen >= 1:
        return c.generation == gen and c.epoch == epoch and c.instance == instance
    try:
        return c.mtime == path.stat().st_mtime
    except OSError:
        return False


def try_serve_cached(c, path: Path):
    """Return `c` if it can serve the current sidecar token, else None."""
    token = peek_sidecar_token(path)
    if token is None:
        if c is not None:
            log.warning(
                "sidecar token read failed for %s; serving the warm cache", path
            )
        return c
    if c is not None and cache_is_fresh(c, path, *token):
        return c
    return None
