"""Throwaway spike for the `move-structured-collections-to-sqlite` design.

It builds the proposed collection store schema in a temporary SQLite file,
fills it with invented rows, and times the operations the design sets targets
for: a guarded single append, a 500-row bulk upsert in one transaction, a
governed structured query (authorize first, then reduce), a consistent
snapshot through the backup API, and rendering plus atomically publishing one
Markdown projection file.

It is not product code and imports nothing from `exomem`. The schema here is
the one in `openspec/changes/move-structured-collections-to-sqlite/design.md`;
if they disagree, the design wins. All data is invented.

    uv run python benchmarks/collections_sqlite_spike/spike.py --rows 10000
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sqlite3
import statistics
import tempfile
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE store_meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT;

CREATE TABLE collections(
  collection_id TEXT PRIMARY KEY,
  family TEXT NOT NULL CHECK (family IN ('records', 'planning')),
  name TEXT NOT NULL UNIQUE,
  projection_root TEXT NOT NULL UNIQUE,
  schema_json TEXT NOT NULL,
  schema_version INTEGER NOT NULL,
  natural_key_json TEXT,
  generation INTEGER NOT NULL DEFAULT 0,
  audit_head TEXT,
  created_txn INTEGER NOT NULL,
  updated_txn INTEGER NOT NULL
) STRICT;

CREATE TABLE items(
  row_id INTEGER PRIMARY KEY,
  collection_id TEXT NOT NULL REFERENCES collections(collection_id),
  item_key TEXT NOT NULL,
  natural_key TEXT,
  row_version INTEGER NOT NULL,
  values_json TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  payload_hash TEXT NOT NULL,
  governance_path TEXT NOT NULL,
  audience_json TEXT,
  created_txn INTEGER NOT NULL,
  updated_txn INTEGER NOT NULL,
  UNIQUE (collection_id, item_key),
  UNIQUE (governance_path)
) STRICT;

CREATE UNIQUE INDEX items_natural_key
  ON items(collection_id, natural_key) WHERE natural_key IS NOT NULL;

CREATE TABLE item_versions(
  row_id INTEGER NOT NULL REFERENCES items(row_id),
  row_version INTEGER NOT NULL,
  values_json TEXT NOT NULL,
  body TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  txn_id INTEGER NOT NULL,
  PRIMARY KEY (row_id, row_version)
) STRICT, WITHOUT ROWID;

CREATE TABLE item_sources(
  row_id INTEGER NOT NULL REFERENCES items(row_id),
  row_version INTEGER NOT NULL,
  ordinal INTEGER NOT NULL,
  source_ref TEXT NOT NULL,
  PRIMARY KEY (row_id, row_version, ordinal)
) STRICT, WITHOUT ROWID;

CREATE TABLE txns(
  txn_id INTEGER PRIMARY KEY,
  transition_id TEXT NOT NULL UNIQUE,
  collection_id TEXT NOT NULL,
  operation TEXT NOT NULL,
  generation_before INTEGER NOT NULL,
  generation_after INTEGER NOT NULL,
  why TEXT NOT NULL,
  actor TEXT NOT NULL,
  request_id TEXT UNIQUE,
  request_hash TEXT,
  committed_at TEXT NOT NULL,
  prev_event_hash TEXT,
  event_hash TEXT NOT NULL,
  receipt_json TEXT NOT NULL
) STRICT;

CREATE TABLE audit_effects(
  txn_id INTEGER NOT NULL REFERENCES txns(txn_id),
  ordinal INTEGER NOT NULL,
  row_id INTEGER NOT NULL,
  effect TEXT NOT NULL CHECK (effect IN ('insert', 'update')),
  version_before INTEGER,
  version_after INTEGER NOT NULL,
  hash_before TEXT,
  hash_after TEXT NOT NULL,
  PRIMARY KEY (txn_id, ordinal)
) STRICT, WITHOUT ROWID;

CREATE INDEX txns_by_collection ON txns(collection_id, txn_id);
CREATE INDEX audit_by_row ON audit_effects(row_id, txn_id);
"""

FIELDS = ("site", "day", "reading", "unit", "note")
SITES = [f"site-{i:03d}" for i in range(200)]


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def invented_row(rng: random.Random, n: int) -> dict[str, object]:
    return {
        "site": SITES[n % len(SITES)],
        "day": f"2026-{1 + (n // 28) % 12:02d}-{1 + n % 28:02d}-{n // 336:05d}",
        "reading": round(rng.uniform(0, 100), 3),
        "unit": "units",
        "note": f"invented observation {n}",
    }


def open_store(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def create_collection(conn: sqlite3.Connection) -> str:
    collection_id = str(uuid.UUID(int=1))
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO collections VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            collection_id,
            "records",
            "sample-readings",
            "Records/sample-readings",
            canonical({"fields": list(FIELDS)}),
            1,
            canonical(["site", "day"]),
            0,
            None,
            0,
            0,
        ),
    )
    conn.execute("COMMIT")
    return collection_id


def commit_rows(
    conn: sqlite3.Connection,
    collection_id: str,
    rows: list[dict[str, object]],
    *,
    expected_generation: int | None,
    request_id: str,
    source_ref: str,
) -> dict[str, object]:
    """One governed mutation: guard, upsert N rows, version, audit, chain."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        prior = conn.execute(
            "SELECT receipt_json FROM txns WHERE request_id=?", (request_id,)
        ).fetchone()
        if prior is not None:
            conn.execute("ROLLBACK")
            return json.loads(prior[0])
        generation, head = conn.execute(
            "SELECT generation, audit_head FROM collections WHERE collection_id=?",
            (collection_id,),
        ).fetchone()
        if expected_generation is not None and expected_generation != generation:
            conn.execute("ROLLBACK")
            return {"error": "COLLECTION_GENERATION_MISMATCH", "generation": generation}
        transition_id = str(uuid.uuid4())
        cur = conn.execute(
            "INSERT INTO txns(transition_id, collection_id, operation, generation_before,"
            " generation_after, why, actor, request_id, request_hash, committed_at,"
            " prev_event_hash, event_hash, receipt_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                transition_id,
                collection_id,
                "bulk_upsert" if len(rows) > 1 else "append",
                generation,
                generation,
                "spike",
                "owner",
                request_id,
                None,
                "2026-09-29T00:00:00Z",
                head,
                "pending",
                "{}",
            ),
        )
        txn_id = cur.lastrowid
        effects: list[str] = []
        outcomes = {"inserted": 0, "updated": 0, "unchanged": 0}
        for ordinal, values in enumerate(rows):
            natural_key = canonical([values["site"], values["day"]])
            values_json = canonical(values)
            payload_hash = sha(values_json + "\x00")
            existing = conn.execute(
                "SELECT row_id, row_version, payload_hash FROM items"
                " WHERE collection_id=? AND natural_key=?",
                (collection_id, natural_key),
            ).fetchone()
            if existing is not None and existing[2] == payload_hash:
                outcomes["unchanged"] += 1
                continue
            if existing is None:
                item_key = sha(natural_key)[:16]
                cur = conn.execute(
                    "INSERT INTO items(collection_id, item_key, natural_key, row_version,"
                    " values_json, payload_hash, governance_path, created_txn, updated_txn)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        collection_id,
                        item_key,
                        natural_key,
                        1,
                        values_json,
                        payload_hash,
                        f"Records/sample-readings/{item_key}.md",
                        txn_id,
                        txn_id,
                    ),
                )
                row_id, before_version, before_hash, after_version = cur.lastrowid, None, None, 1
                effect = "insert"
                outcomes["inserted"] += 1
            else:
                row_id, before_version, before_hash = existing
                after_version = before_version + 1
                conn.execute(
                    "UPDATE items SET row_version=?, values_json=?, payload_hash=?,"
                    " updated_txn=? WHERE row_id=?",
                    (after_version, values_json, payload_hash, txn_id, row_id),
                )
                effect = "update"
                outcomes["updated"] += 1
            conn.execute(
                "INSERT INTO item_versions VALUES (?,?,?,?,?,?)",
                (row_id, after_version, values_json, "", payload_hash, txn_id),
            )
            conn.execute(
                "INSERT INTO item_sources VALUES (?,?,?,?)", (row_id, after_version, 0, source_ref)
            )
            conn.execute(
                "INSERT INTO audit_effects VALUES (?,?,?,?,?,?,?,?)",
                (
                    txn_id,
                    ordinal,
                    row_id,
                    effect,
                    before_version,
                    after_version,
                    before_hash,
                    payload_hash,
                ),
            )
            effects.append(f"{ordinal}:{row_id}:{effect}:{after_version}:{payload_hash}")
        written = outcomes["inserted"] + outcomes["updated"]
        if written == 0:
            conn.execute("ROLLBACK")
            return {"committed": False, "counts": outcomes, "generation": generation}
        new_generation = generation + 1
        event_hash = sha(canonical([head, transition_id, collection_id, new_generation, effects]))
        receipt = {
            "committed": True,
            "transition_id": transition_id,
            "counts": outcomes,
            "generation": new_generation,
        }
        conn.execute(
            "UPDATE txns SET generation_after=?, event_hash=?, receipt_json=? WHERE txn_id=?",
            (new_generation, event_hash, canonical(receipt), txn_id),
        )
        conn.execute(
            "UPDATE collections SET generation=?, audit_head=?, updated_txn=?"
            " WHERE collection_id=?",
            (new_generation, event_hash, txn_id, collection_id),
        )
        conn.execute("COMMIT")
        return receipt
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def percentile(samples: list[float], q: float) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def summarize(samples: list[float]) -> dict[str, float]:
    return {
        "n": len(samples),
        "p50_ms": round(statistics.median(samples) * 1000, 3),
        "p95_ms": round(percentile(samples, 0.95) * 1000, 3),
        "max_ms": round(max(samples) * 1000, 3),
    }


def governed_query(conn: sqlite3.Connection, collection_id: str, denied_prefix: str) -> dict:
    """Authorize on identity-only columns first, then reduce over the survivors."""
    # Phase 1: identity-only projection; no value is parsed before authorization.
    candidates = conn.execute(
        "SELECT row_id, governance_path, audience_json FROM items WHERE collection_id=?",
        (collection_id,),
    ).fetchall()
    # Stand-in for one per-operation resolved policy evaluated per path.
    allowed = [(row_id,) for row_id, path, _aud in candidates if not path.startswith(denied_prefix)]
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS authorized(row_id INTEGER PRIMARY KEY)")
    conn.execute("BEGIN")
    conn.execute("DELETE FROM authorized")
    conn.executemany("INSERT INTO authorized VALUES (?)", allowed)
    conn.execute("COMMIT")
    # Phase 2: filter, sort, paginate and aggregate only over authorized rows.
    page = conn.execute(
        "SELECT i.item_key, i.values_json FROM items i JOIN authorized a USING(row_id)"
        " WHERE json_extract(i.values_json,'$.site')=? ORDER BY json_extract(i.values_json,'$.day')"
        " DESC LIMIT 50",
        (SITES[7],),
    ).fetchall()
    total, avg = conn.execute(
        "SELECT count(*), avg(json_extract(i.values_json,'$.reading')) FROM items i"
        " JOIN authorized a USING(row_id) WHERE json_extract(i.values_json,'$.site')=?",
        (SITES[7],),
    ).fetchone()
    return {"page": len(page), "total": total, "avg": avg, "authorized": len(allowed)}


def uniform_release_query(conn: sqlite3.Connection, collection_id: str) -> dict:
    """Fast path: the resolved policy cannot tell this collection's rows apart.

    One collection-level decision covers every row, so filters, sort, limit and
    aggregates push down into SQL over a declared-field expression index.
    """
    page = conn.execute(
        "SELECT item_key, values_json FROM items WHERE collection_id=?"
        " AND json_extract(values_json,'$.site')=?"
        " ORDER BY json_extract(values_json,'$.day') DESC LIMIT 50",
        (collection_id, SITES[7]),
    ).fetchall()
    total, avg = conn.execute(
        "SELECT count(*), avg(json_extract(values_json,'$.reading')) FROM items"
        " WHERE collection_id=? AND json_extract(values_json,'$.site')=?",
        (collection_id, SITES[7]),
    ).fetchone()
    return {"page": len(page), "total": total, "avg": avg}


def render_projection(item_key: str, row_version: int, values: dict[str, object]) -> str:
    front = "\n".join(f"{k}: {json.dumps(values[k])}" for k in FIELDS)
    return (
        f"---\n{front}\nexomem_view:\n  item: {item_key}\n  version: {row_version}\n---\n"
        f"\n{values['note']}\n"
    )


def publish_file(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)


def run(rows: int, appends: int, bulk: int, seed: int) -> dict[str, object]:
    rng = random.Random(seed)
    with tempfile.TemporaryDirectory(prefix="collections-spike-") as tmp:
        root = Path(tmp)
        db = root / "state" / "collections.sqlite"
        db.parent.mkdir()
        conn = open_store(db)
        conn.executescript(SCHEMA)
        collection_id = create_collection(conn)

        started = time.perf_counter()
        seeded = 0
        while seeded < rows:
            chunk = [invented_row(rng, seeded + i) for i in range(min(500, rows - seeded))]
            commit_rows(
                conn,
                collection_id,
                chunk,
                expected_generation=None,
                request_id=f"seed-{seeded}",
                source_ref="Evidence/sample-source.md",
            )
            seeded += len(chunk)
        seed_seconds = time.perf_counter() - started

        append_samples: list[float] = []
        next_n = rows
        for i in range(appends):
            generation = conn.execute(
                "SELECT generation FROM collections WHERE collection_id=?", (collection_id,)
            ).fetchone()[0]
            row = invented_row(rng, next_n)
            next_n += 1
            t0 = time.perf_counter()
            receipt = commit_rows(
                conn,
                collection_id,
                [row],
                expected_generation=generation,
                request_id=f"append-{i}",
                source_ref="Evidence/sample-source.md",
            )
            append_samples.append(time.perf_counter() - t0)
            assert receipt["committed"], receipt

        replay_t0 = time.perf_counter()
        replay = commit_rows(
            conn,
            collection_id,
            [row],
            expected_generation=None,
            request_id=f"append-{appends - 1}",
            source_ref="x",
        )
        replay_seconds = time.perf_counter() - replay_t0
        assert replay["committed"] and replay["counts"]["inserted"] == 1

        stale = commit_rows(
            conn,
            collection_id,
            [invented_row(rng, next_n)],
            expected_generation=0,
            request_id="stale",
            source_ref="x",
        )
        assert stale.get("error") == "COLLECTION_GENERATION_MISMATCH"

        bulk_rows = [invented_row(rng, next_n + i) for i in range(bulk // 2)]
        bulk_rows += [
            dict(invented_row(rng, i), reading=-1.0) for i in range(bulk - len(bulk_rows))
        ]
        t0 = time.perf_counter()
        bulk_receipt = commit_rows(
            conn,
            collection_id,
            bulk_rows,
            expected_generation=None,
            request_id="bulk-1",
            source_ref="Evidence/sample-source.md",
        )
        bulk_seconds = time.perf_counter() - t0
        t0 = time.perf_counter()
        bulk_again = commit_rows(
            conn,
            collection_id,
            bulk_rows,
            expected_generation=None,
            request_id="bulk-2",
            source_ref="Evidence/sample-source.md",
        )
        bulk_replay_seconds = time.perf_counter() - t0

        query_samples = []
        for _ in range(20):
            t0 = time.perf_counter()
            query = governed_query(conn, collection_id, "Records/sample-readings/0")
            query_samples.append(time.perf_counter() - t0)

        conn.execute(
            "CREATE INDEX IF NOT EXISTS items_site"
            " ON items(collection_id, json_extract(values_json,'$.site'))"
        )
        uniform_samples = []
        for _ in range(20):
            t0 = time.perf_counter()
            uniform = uniform_release_query(conn, collection_id)
            uniform_samples.append(time.perf_counter() - t0)

        snapshot = root / "vault" / ".exomem" / "collections.snapshot.sqlite"
        snapshot.parent.mkdir(parents=True)
        t0 = time.perf_counter()
        staged = snapshot.with_name(snapshot.name + ".partial")
        dest = sqlite3.connect(staged)
        conn.backup(dest)
        dest.execute("PRAGMA journal_mode=DELETE")
        dest.close()
        os.replace(staged, snapshot)
        snapshot_seconds = time.perf_counter() - t0
        check = sqlite3.connect(snapshot)
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        snapshot_rows = check.execute("SELECT count(*) FROM items").fetchone()[0]
        check.close()

        render_samples = []
        sample_rows = conn.execute(
            "SELECT item_key, row_version, values_json FROM items ORDER BY row_id DESC LIMIT 50"
        ).fetchall()
        for item_key, version, values_json in sample_rows:
            t0 = time.perf_counter()
            text = render_projection(item_key, version, json.loads(values_json))
            publish_file(root / "vault" / "Records" / "sample-readings" / f"{item_key}.md", text)
            render_samples.append(time.perf_counter() - t0)

        live_rows = conn.execute("SELECT count(*) FROM items").fetchone()[0]
        db_bytes = db.stat().st_size + (
            db.with_name(db.name + "-wal").stat().st_size
            if db.with_name(db.name + "-wal").exists()
            else 0
        )
        conn.close()
        return {
            "sqlite_version": sqlite3.sqlite_version,
            "pragmas": {"journal_mode": "wal", "synchronous": "full"},
            "seed_rows": rows,
            "seed_seconds": round(seed_seconds, 3),
            "live_rows_after": live_rows,
            "guarded_append_at_n": summarize(append_samples),
            "idempotent_replay_ms": round(replay_seconds * 1000, 3),
            "bulk_upsert": {
                "rows": bulk,
                "counts": bulk_receipt["counts"],
                "seconds": round(bulk_seconds, 4),
                "replay_counts": bulk_again["counts"],
                "replay_seconds": round(bulk_replay_seconds, 4),
            },
            "governed_query_per_row": {**summarize(query_samples), **query},
            "governed_query_uniform_release": {**summarize(uniform_samples), **uniform},
            "snapshot": {
                "seconds": round(snapshot_seconds, 4),
                "integrity": integrity,
                "rows": snapshot_rows,
                "store_bytes": db_bytes,
            },
            "projection_render_publish": summarize(render_samples),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rows", type=int, default=10000)
    parser.add_argument("--appends", type=int, default=200)
    parser.add_argument("--bulk", type=int, default=500)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    result = run(args.rows, args.appends, args.bulk, args.seed)
    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
