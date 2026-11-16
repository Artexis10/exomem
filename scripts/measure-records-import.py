#!/usr/bin/env python3
"""Throughput, store statements and correctness of one Records import job.

Diagnosis harness, not a gate. Each run is a fresh child process with a temp
vault and state root. It writes an invented JSON document shaped like a music
playlist export: 57 ordered lists and one 734-row list whose required
``position`` is ``null``, written last. It then previews and runs one
``records.import`` job through the real importer, one batch at a time.

For each batch it records wall time, the invoking thread's CPU time, and the
steps the batch took on the store connection. Python's sqlite3 gives up the GIL at
every step: each ``execute`` call, each parameter set of ``executemany`` and each
fetched row. The step count therefore multiplies the wait that other busy threads
impose. A SQLite trace callback is not used: it also fires for trigger programs,
which run inside one step, and it expands every statement's SQL, so it inflates both
the count and the wall time. The count comes from a connection and cursor subclass
that add one integer per call, so wall time stays comparable across runs.
``--contenders N`` runs N CPU-bound Python threads beside the import, standing in
for the service's request and background passes in the same process.

    uv run python scripts/measure-records-import.py --contenders 0,1,2 --repeats 3 \\
        --out records-import.json

Nothing here touches a live service or a real state root.
"""

from __future__ import annotations

import argparse
import json
import random
import resource
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CID = "4a7d1c2e-5b6f-4a8b-9c0d-1e2f3a4b5c6d"
TITLE = "Playlist Entries"
SOURCE = "Knowledge Base/Sources/Invented/playlists.json"
# The list sizes of the export this fixture imitates; the totals are what matter.
LIST_SIZES = (
    0, 14, 18, 25, 27, 28, 28, 29, 30, 31, 33, 35, 38, 43, 44, 44, 45, 47, 50, 50, 50, 53, 54, 54, 63, 64,
    65, 67, 70, 75, 75, 75, 76, 77, 80, 82, 86, 87, 87, 88, 88, 90, 91, 94, 96, 97, 98, 99, 112, 116, 117,
    119, 120, 138, 153, 154, 192,
)
NULL_TAIL = 734

MANIFEST = f"""---
type: collection
exomem_id: {CID}
title: {TITLE}
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: sqlite
  source: Items
  format_version: 1
item_schema:
  natural_key: [platform, list_id, position]
  fields:
    platform: {{type: string, required: true}}
    list_id: {{type: string, required: true}}
    position: {{type: integer, required: true}}
    track_id: {{type: string}}
    title: {{type: string}}
    artist: {{type: string}}
    album: {{type: string}}
    duration_seconds: {{type: integer}}
    snapshot_on: {{type: date}}
---

Invented playlist entries for an import measurement.
"""
MAPPING = {
    "rows": "[].tracks[]",
    "fields": {
        "platform": {"const": "invented"}, "list_id": "$.[].listId", "position": "position",
        "track_id": "trackId", "title": "title", "artist": "artist", "album": "album",
        "duration_seconds": "durationSec", "snapshot_on": {"const": "2026-05-29"},
    },
}


def _source() -> list[dict]:
    rng = random.Random(7)
    lists = [
        {"listId": f"list-{n:03d}", "tracks": [
            {"position": i + 1, "trackId": f"t{rng.randrange(10**8)}", "title": f"Song {i}",
             "artist": f"Artist {rng.randrange(256)}", "album": f"Album {rng.randrange(755)}",
             "durationSec": rng.randrange(60, 600)}
            for i in range(size)]}
        for n, size in enumerate(LIST_SIZES)
    ]
    lists.append({"listId": "liked", "tracks": [
        {"position": None, "trackId": f"liked-{i}", "title": f"Liked {i}", "artist": "A", "album": "B",
         "durationSec": 200}
        for i in range(NULL_TAIL)]})
    return lists


def _spin(stop: threading.Event) -> None:
    value = 0
    while not stop.is_set():
        for i in range(10_000):
            value = (value * 31 + i) % 1_000_003


class _Steps:
    """Counts of the GIL-releasing steps that one connection takes, reset per batch."""

    def __init__(self) -> None:
        self.executes = self.parameter_sets = self.rows = 0

    def reset(self) -> None:
        self.executes = self.parameter_sets = self.rows = 0

    def snapshot(self) -> dict[str, int]:
        return {"executes": self.executes, "parameter_sets": self.parameter_sets, "rows_fetched": self.rows,
                "steps": self.executes + self.parameter_sets + self.rows}


def _counting_connection(steps: _Steps) -> type[sqlite3.Connection]:
    """A connection class whose cursors count into ``steps``; results and errors are unchanged."""

    class Cursor(sqlite3.Cursor):
        def execute(self, sql, parameters=()):
            steps.executes += 1
            return super().execute(sql, parameters)

        def executemany(self, sql, seq_of_parameters):
            seq = list(seq_of_parameters)
            steps.parameter_sets += len(seq)
            return super().executemany(sql, seq)

        def __next__(self):
            row = super().__next__()
            steps.rows += 1
            return row

        def fetchone(self):
            row = super().fetchone()
            steps.rows += row is not None
            return row

        def fetchmany(self, *args):
            rows = super().fetchmany(*args)
            steps.rows += len(rows)
            return rows

        def fetchall(self):
            rows = super().fetchall()
            steps.rows += len(rows)
            return rows

    class Connection(sqlite3.Connection):
        def cursor(self, factory=None):
            return super().cursor(factory or Cursor)

        def execute(self, sql, parameters=()):
            return self.cursor().execute(sql, parameters)

        def executemany(self, sql, seq_of_parameters):
            return self.cursor().executemany(sql, seq_of_parameters)

    return Connection


def _child(args: argparse.Namespace) -> dict[str, object]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _records_measurement_env import clear_inherited, isolate

    clear_inherited()
    tmp = Path(args.work_dir)
    vault = tmp / "vault"
    isolate(tmp, vault)
    (vault / "Knowledge Base").mkdir(parents=True)
    (vault / "Knowledge Base/log.md").write_text("# Activity\n", encoding="utf-8")
    source = _source()
    (vault / SOURCE).parent.mkdir(parents=True)
    (vault / SOURCE).write_text(json.dumps(source), encoding="utf-8")
    sys.path.insert(0, str(REPO / "src"))

    from exomem.collection_store import connection, importer
    from exomem.collection_store.preview import preview_store
    from exomem.collection_store.writer import CollectionWriter
    from exomem.governance.principal import library_scope, owner_principal, request_scope
    from exomem.record_memory import record_memory

    stop = threading.Event()
    for _ in range(args.contenders):
        threading.Thread(target=_spin, args=(stop,), daemon=True).start()
    steps = _Steps()
    # The store's own seam for the connection class; the import then runs on the counting connection.
    connection._CONNECTION_FACTORY = _counting_connection(steps)
    result: dict[str, object] = {
        "contenders": args.contenders,
        "rows_in_source": sum(len(item["tracks"]) for item in source),
        "null_position_rows": NULL_TAIL,
        "switch_interval_s": sys.getswitchinterval(),
        "batches": [],
    }

    def import_request(handle, body):
        with request_scope(owner_principal()), preview_store(vault, handle):
            return record_memory(vault, action="import", collection=CID, import_request=body)

    with connection.open_writer(tmp / "collections.sqlite", lease_check=lambda: True) as handle, library_scope():
        store = CollectionWriter(vault, handle)
        store.create_collection(f"Knowledge Base/Records/{TITLE}/_collection.md", MANIFEST, why="measure",
                                scaffold=False)
        for _ in range(16):
            if store.migrate_typed_encoding(CID) == "ready":
                break
        request = {"source_ref": SOURCE, "format": "json-document", "mapping": MAPPING}
        started = time.perf_counter()
        preview = import_request(handle, {"mode": "preview", **request})
        result["preview_s"] = round(time.perf_counter() - started, 3)
        result["preview_rows"] = preview.get("rows")
        job = import_request(handle, {"mode": "start", **request})
        started = time.perf_counter()
        while True:
            steps.reset()
            wall, cpu = time.perf_counter(), time.thread_time()
            with preview_store(vault, handle):
                ran = importer.run_jobs(vault, max_batches=1)
            if not ran.get("batches"):
                break
            result["batches"].append({
                "wall_s": round(time.perf_counter() - wall, 4),
                "thread_cpu_s": round(time.thread_time() - cpu, 4),
                **steps.snapshot(),
            })
        result["import_s"] = round(time.perf_counter() - started, 3)
        final = import_request(handle, {"mode": "status", "continuation": job["continuation"]})
        result["final"] = {"state": final.get("state"), "rows": final.get("rows")}
        result["stored_rows"] = handle.connection.execute(
            "SELECT COUNT(*) FROM items WHERE collection_id=?", (CID,)).fetchone()[0]
        result["stored_null_key_rows"] = handle.connection.execute(
            "SELECT COUNT(*) FROM items WHERE collection_id=? AND natural_key LIKE '%null]]]'",
            (CID,)).fetchone()[0]
    stop.set()
    result["peak_rss_mib"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    return result


def _summary(runs: list[dict]) -> dict[str, object]:
    walls = sorted(batch["wall_s"] for run in runs for batch in run["batches"])
    steps = [batch["steps"] for run in runs for batch in run["batches"]]
    return {
        "runs": len(runs),
        "import_s": [run["import_s"] for run in runs],
        "batch_wall_s_max": walls[-1] if walls else None,
        "batch_wall_s_median": walls[len(walls) // 2] if walls else None,
        "steps_per_batch_max": max(steps) if steps else None,
        "final_rows": runs[-1]["final"]["rows"] if runs else None,
        "stored_rows": runs[-1]["stored_rows"] if runs else None,
        "stored_null_key_rows": runs[-1]["stored_null_key_rows"] if runs else None,
    }


def _parent(args: argparse.Namespace) -> int:
    report: dict[str, object] = {"fixture": {"rows": sum(LIST_SIZES) + NULL_TAIL, "null_tail": NULL_TAIL},
                                 "by_contenders": {}}
    for contenders in (int(part) for part in args.contenders.split(",")):
        runs = []
        for repeat in range(args.repeats):
            print(f"== contenders {contenders} repeat {repeat + 1} ==", file=sys.stderr, flush=True)
            with tempfile.TemporaryDirectory(prefix="exomem-import-measure-", ignore_cleanup_errors=True) as work:
                completed = subprocess.run(
                    [sys.executable, __file__, "--child", "--work-dir", work, "--contender-count", str(contenders)],
                    capture_output=True, text=True, check=False,
                )
            if completed.returncode:
                sys.stderr.write(completed.stderr)
                return completed.returncode
            line = next(row for row in reversed(completed.stdout.splitlines()) if row.startswith("RESULT-JSON:"))
            runs.append(json.loads(line.removeprefix("RESULT-JSON:")))
        report["by_contenders"][str(contenders)] = {"summary": _summary(runs), "runs": runs}
        Path(args.out).write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    for contenders, entry in report["by_contenders"].items():
        print(f"contenders={contenders} {json.dumps(entry['summary'])}", file=sys.stderr)
    print(f"wrote {args.out}", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--contenders", default="0,1,2", help="comma-separated contender thread counts")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--out", default="records-import.json")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--work-dir", help=argparse.SUPPRESS)
    parser.add_argument("--contender-count", type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        args.contenders = args.contender_count
        print("RESULT-JSON:" + json.dumps(_child(args)))
        return 0
    return _parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
