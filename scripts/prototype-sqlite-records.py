#!/usr/bin/env python3
"""Throwaway prototype: SQLite-authoritative Records, Markdown as a projection.

NOT product code and not wired into anything. It answers one question with numbers:
if a collection's rows lived in a per-vault SQLite table (same natural key, an
O(1) guard, a per-row audit event) and the Markdown item files were only a rendered
read-only projection, what would a single guarded append and a guard refresh cost at
1,000 and 10,000 items?

Two projection modes are timed:

* ``sync``  -- the item file is rendered and atomically written inside the append,
  before the transaction commits (a failed write rolls the row back).
* ``async`` -- the transaction commits the row and an outbox entry; a worker thread
  renders and writes the file afterwards. Foreground latency excludes the render;
  the worker's lag is reported separately.

What is deliberately NOT here, so the numbers are storage-layer only: request
validation, the mutation-boundary lease, the idempotency ledger, governance and
withheld-is-absent filtering, index fan-out and the response. The findings doc adds
the flat, size-independent cost of those (measured on the real path) back on.

    uv run python scripts/prototype-sqlite-records.py --sizes 1000,10000 --appends 30
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import queue
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

SCHEMA = """
CREATE TABLE collections (
  collection_id TEXT PRIMARY KEY, generation INTEGER NOT NULL, manifest_hash TEXT NOT NULL);
CREATE TABLE records (
  collection_id TEXT NOT NULL, item_key TEXT NOT NULL, natural_key TEXT NOT NULL,
  values_json TEXT NOT NULL, body TEXT NOT NULL, item_hash TEXT NOT NULL,
  version INTEGER NOT NULL,
  PRIMARY KEY (collection_id, item_key),
  UNIQUE (collection_id, natural_key));
CREATE TABLE audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, collection_id TEXT NOT NULL,
  transition_id TEXT NOT NULL, parent_id TEXT, operation TEXT NOT NULL,
  item_key TEXT NOT NULL, before_hash TEXT, after_hash TEXT NOT NULL,
  payload_hash TEXT NOT NULL, why TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE outbox (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, collection_id TEXT NOT NULL,
  item_key TEXT NOT NULL, enqueued_ns INTEGER NOT NULL, done_ns INTEGER);
"""


def _load_harness():
    """The latency harness owns the synthetic manifest and item generator; reuse both."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "records_append_latency_harness", REPO / "scripts" / "measure-records-append-latency.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    rank = (len(ordered) - 1) * q
    lo, hi = int(rank), min(int(rank) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)


def _summary(values: list[float]) -> dict[str, float]:
    return {
        "p50_ms": round(1000 * _pct(values, 0.5), 2),
        "p95_ms": round(1000 * _pct(values, 0.95), 2),
    }


class Store:
    """The prototype: rows are authoritative, files are rendered from them."""

    def __init__(self, root: Path, manifest, sync_mode: str, synchronous: str) -> None:
        from exomem import record_formats
        from exomem import structured_collections as collections

        self.record_formats = record_formats
        self.collections = collections
        self.root = root
        self.manifest = manifest
        self.cid = manifest.collection_id
        self.mode = sync_mode
        self.db = sqlite3.connect(root / "records.sqlite", isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(f"PRAGMA synchronous={synchronous}")
        self.db.executescript(SCHEMA)
        self.db.execute(
            "INSERT INTO collections VALUES (?,?,?)",
            (self.cid, 0, manifest.manifest_version.hash),
        )
        self.items_dir = root / manifest.storage.source
        self.items_dir.mkdir(parents=True, exist_ok=True)
        self.lag_seconds: list[float] = []
        self._queue: queue.Queue[tuple[str, dict, str] | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        if sync_mode == "async":
            self._worker = threading.Thread(target=self._drain, daemon=True)
            self._worker.start()

    # -- guard ------------------------------------------------------------------
    def refresh_guard(self) -> int:
        """The client's container-version read: one indexed row."""
        row = self.db.execute(
            "SELECT generation FROM collections WHERE collection_id=?", (self.cid,)
        ).fetchone()
        return int(row[0])

    # -- projection ---------------------------------------------------------------
    def _render(self, key: str, values: dict, correlation: str) -> str:
        return self.record_formats.render_markdown_item(
            self.manifest, values, key, "", correlation, resolve_relationship=None
        )

    def _write_projection(self, key: str, text: str) -> None:
        target = self.items_dir / f"{key}.md"
        temp = target.with_suffix(".tmp")
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)

    def _drain(self) -> None:
        while (job := self._queue.get()) is not None:
            key, values, correlation, enqueued = job  # type: ignore[misc]
            self._write_projection(key, self._render(key, values, correlation))
            self.lag_seconds.append((time.perf_counter_ns() - enqueued) / 1e9)
            self._queue.task_done()

    # -- append -------------------------------------------------------------------
    def append(self, values: dict, why: str, expected_generation: int | None) -> None:
        natural = json.dumps(
            [values["observed_on"], values["subject"]], separators=(",", ":")
        )
        key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.cid}/{natural}"))
        correlation = uuid.uuid4().hex[:24]
        payload = json.dumps(values, sort_keys=True, separators=(",", ":"))
        payload_hash = hashlib.sha256(payload.encode()).hexdigest()
        db = self.db
        db.execute("BEGIN IMMEDIATE")
        try:
            generation, manifest_hash = db.execute(
                "SELECT generation, manifest_hash FROM collections WHERE collection_id=?",
                (self.cid,),
            ).fetchone()
            if expected_generation is not None and expected_generation != generation:
                raise RuntimeError("STALE_RECORD")
            twin = db.execute(
                "SELECT item_key FROM records WHERE collection_id=? AND natural_key=?",
                (self.cid, natural),
            ).fetchone()
            if twin is not None:
                raise RuntimeError("RECORD_ID_CONFLICT")
            item_hash = payload_hash
            db.execute(
                "INSERT INTO records VALUES (?,?,?,?,?,?,?)",
                (self.cid, key, natural, payload, "", item_hash, 1),
            )
            parent = db.execute(
                "SELECT transition_id FROM audit WHERE collection_id=? ORDER BY seq DESC LIMIT 1",
                (self.cid,),
            ).fetchone()
            db.execute(
                "INSERT INTO audit (collection_id, transition_id, parent_id, operation,"
                " item_key, before_hash, after_hash, payload_hash, why, at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    self.cid,
                    correlation,
                    parent[0] if parent else None,
                    "append",
                    key,
                    None,
                    item_hash,
                    payload_hash,
                    why,
                    dt.datetime.now(dt.timezone.utc).isoformat(),
                ),
            )
            db.execute(
                "UPDATE collections SET generation=generation+1 WHERE collection_id=?",
                (self.cid,),
            )
            if self.mode == "sync":
                self._write_projection(key, self._render(key, values, correlation))
            else:
                db.execute(
                    "INSERT INTO outbox (collection_id, item_key, enqueued_ns) VALUES (?,?,?)",
                    (self.cid, key, time.perf_counter_ns()),
                )
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        if self.mode == "async":
            self._queue.put((key, values, correlation, time.perf_counter_ns()))  # type: ignore[arg-type]

    def flush(self) -> None:
        if self._worker is not None:
            self._queue.join()
            self._queue.put(None)
            self._worker.join()

    def seed(self, count: int, item, project: bool) -> float:
        started = time.perf_counter()
        db = self.db
        db.execute("BEGIN")
        for index in range(count):
            values = item(index)
            natural = json.dumps(
                [values["observed_on"], values["subject"]], separators=(",", ":")
            )
            key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.cid}/{natural}"))
            payload = json.dumps(values, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(payload.encode()).hexdigest()
            db.execute(
                "INSERT INTO records VALUES (?,?,?,?,?,?,?)",
                (self.cid, key, natural, payload, "", digest, 1),
            )
            if project:
                self._write_projection_fast(key, values)
        db.execute("UPDATE collections SET generation=?", (count,))
        db.execute("COMMIT")
        return time.perf_counter() - started

    def _write_projection_fast(self, key: str, values: dict) -> None:
        text = self._render(key, values, "seed")
        (self.items_dir / f"{key}.md").write_text(text, encoding="utf-8")


def run(size: int, appends: int, mode: str, synchronous: str) -> dict[str, object]:
    shared = _load_harness()

    tmp = Path(tempfile.mkdtemp(prefix="exomem-sqlite-proto-"))
    try:
        os.environ.setdefault("EXOMEM_STATE_ROOT", str(tmp / "state"))
        manifest_file = tmp / shared.COLLECTION
        manifest_file.parent.mkdir(parents=True)
        manifest_file.write_text(shared.MANIFEST, encoding="utf-8")
        from exomem import structured_collections as collections

        manifest = collections.load_manifest(tmp, manifest_file)
        store = Store(manifest_file.parent, manifest, mode, synchronous)
        seeded = store.seed(size, shared._item, project=True)
        refresh: list[float] = []
        wall: list[float] = []
        for index in range(appends):
            start = time.perf_counter()
            generation = store.refresh_guard()
            refresh.append(time.perf_counter() - start)
            start = time.perf_counter()
            store.append(shared._item(size + index), "prototype", generation)
            wall.append(time.perf_counter() - start)
        store.flush()
        return {
            "size": size,
            "mode": mode,
            "synchronous": synchronous,
            "seed_seconds": round(seeded, 1),
            "append": _summary(wall),
            "guard_refresh": _summary(refresh),
            "projection_lag_ms": _summary(store.lag_seconds) if store.lag_seconds else None,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default="1000,10000")
    parser.add_argument("--appends", type=int, default=30)
    parser.add_argument("--synchronous", default="FULL", choices=["FULL", "NORMAL"])
    parser.add_argument("--out", default="sqlite-prototype-results.json")
    args = parser.parse_args()
    results = []
    for size in [int(part) for part in args.sizes.split(",")]:
        for mode in ("sync", "async"):
            print(f"== size {size} mode {mode} ==", file=sys.stderr, flush=True)
            results.append(run(size, args.appends, mode, args.synchronous))
    Path(args.out).write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
