#!/usr/bin/env python3
"""Latency, CPU time and outcome of Records queries on a store summary collection.

Diagnosis harness, not a gate. Each run is a fresh child process with a temp vault and state
root. It creates an invented summary collection, writes ``--rows`` rows through the real bulk
upsert, then runs a few owner queries through the governed Records route: a filtered and sorted
page, a filtered aggregate and a sorted first page. Each query runs ``--repeats`` times beside 0,
1 and 2 CPU-bound Python threads, standing in for the service's request and background passes in
the same process.

For each query it records wall time, the invoking thread's CPU time, the outcome (``ok`` or the
refusal code) and, without contenders, the number of SQL statements. Every statement gives up the
GIL, so the statement count multiplies the wait that busy threads impose. ``--src`` names the
source tree to measure, so the same harness measures a tree before and after a change, and
``--whole-read`` makes a tree that selects rows read every row instead, as before it did.

    uv run python scripts/measure-records-query.py --rows 154,5000 --out records-query.json

Nothing here touches a live service or a real state root.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CID = "6b0e3f9a-2c41-4d7e-8f15-3a9c0d2b7e64"
TITLE = "Listening Log"
EVIDENCE = "Knowledge Base/Sources/Invented/listening-export.md"

MANIFEST = f"""---
type: collection
exomem_id: {CID}
title: {TITLE}
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
view_mode: summary
storage:
  strategy: sqlite
  source: Items
  format_version: 1
item_schema:
  natural_key: [title]
  fields:
    title: {{type: string, required: true}}
    artist: {{type: string}}
    plays: {{type: integer}}
    date: {{type: date}}
---

Invented listening entries for a query measurement.
"""

QUERIES = {
    # The shape of the reported timeout: a prefix filter and a sort on a summary collection.
    "filter_sort_page": {"filters": [{"column": "title", "op": "startswith", "value": "Track 1"}],
                         "sort_by": "plays", "descending": True, "limit": 50},
    "filter_group": {"filters": [{"column": "plays", "op": "gte", "value": 10}], "aggregate": "group:artist"},
    "sort_page": {"sort_by": "date", "limit": 50},
}


def _rows(count: int) -> list[dict]:
    return [{"title": f"Track {number}", "artist": f"Artist {number % 97}", "plays": number % 53,
             "date": f"2026-{number % 12 + 1:02d}-{number % 28 + 1:02d}"} for number in range(count)]


def _spin(stop: threading.Event) -> None:
    value = 0
    while not stop.is_set():
        for i in range(10_000):
            value = (value * 31 + i) % 1_000_003


def _child(args: argparse.Namespace) -> dict[str, object]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _records_measurement_env import clear_inherited, isolate

    clear_inherited()
    tmp = Path(args.work_dir)
    vault = tmp / "vault"
    isolate(tmp, vault)
    (vault / "Knowledge Base").mkdir(parents=True)
    (vault / "Knowledge Base/log.md").write_text("# Activity\n", encoding="utf-8")
    (vault / EVIDENCE).parent.mkdir(parents=True)
    (vault / EVIDENCE).write_text("---\ntype: evidence\n---\n\nInvented export.\n", encoding="utf-8")
    sys.path.insert(0, str(Path(args.src).resolve()))

    import exomem
    from exomem import record_governance
    from exomem import structured_collections as collections
    from exomem.collection_store import connection, reader
    from exomem.collection_store.preview import preview_store
    from exomem.collection_store.writer import CollectionWriter
    from exomem.governance.principal import library_scope, owner_principal, request_scope

    result: dict[str, object] = {"rows": args.rows, "src": str(Path(exomem.__file__).resolve().parents[1]),
                                 "switch_interval_s": sys.getswitchinterval(), "queries": {},
                                 "whole_read": args.whole_read and hasattr(reader.StoreAdapter, "selecting")}
    if result["whole_read"]:
        @contextlib.contextmanager
        def declined(self):
            yield None

        reader.StoreAdapter.selecting = declined
    with connection.open_writer(tmp / "collections.sqlite", lease_check=lambda: True) as handle, library_scope():
        store = CollectionWriter(vault, handle)
        with request_scope(owner_principal()):
            store.create_collection(f"Knowledge Base/Records/{TITLE}/_collection.md", MANIFEST,
                                    why="measure", scaffold=False)
            started = time.perf_counter()
            items = _rows(args.rows)
            for start in range(0, len(items), 500):
                guard = store.inspect_collection(CID)["lifecycle_guards"]["expected_container_hash"]
                store.bulk_upsert_records(CID, rows=[{"item": item} for item in items[start:start + 500]],
                                          why="measure", source=EVIDENCE, expected_container_hash=guard)
        result["seed_s"] = round(time.perf_counter() - started, 1)
        result["encoding"] = handle.connection.execute(
            "SELECT encoding FROM collections WHERE collection_id=?", (CID,)).fetchone()[0]
        statements = [0]

        def run(arguments):
            wall, cpu = time.perf_counter(), time.thread_time()
            try:
                with request_scope(owner_principal(surface="mcp")), preview_store(vault, handle):
                    answer = record_governance.query_collection(vault, CID, **arguments)
                outcome = "ok"
                detail = {"total_matched": answer.total_matched, "returned": answer.returned}
            except collections.CollectionError as error:
                outcome, detail = error.code, {"message": str(error)[:200]}
            return {"wall_ms": round((time.perf_counter() - wall) * 1000, 1),
                    "thread_cpu_ms": round((time.thread_time() - cpu) * 1000, 1),
                    "outcome": outcome, **detail}

        for name, arguments in QUERIES.items():
            run(arguments)  # warm imports and caches, as a running service has them
            by_contenders = {}
            for contenders in (int(part) for part in args.contenders.split(",")):
                stop = threading.Event()
                spinners = [threading.Thread(target=_spin, args=(stop,), daemon=True) for _ in range(contenders)]
                for spinner in spinners:
                    spinner.start()
                # The trace callback is a Python call per statement and contends for the GIL
                # itself, so statements are counted only in uncontended runs.
                if not contenders:
                    connection_trace = lambda _sql: statements.__setitem__(0, statements[0] + 1)  # noqa: E731
                    handle.connection.set_trace_callback(connection_trace)
                    _trace_new_connections(connection, connection_trace)
                samples = []
                try:
                    for _ in range(args.repeats):
                        statements[0] = 0
                        sample = run(arguments)
                        if not contenders:
                            sample["statements"] = statements[0]
                        samples.append(sample)
                finally:
                    handle.connection.set_trace_callback(None)
                    _trace_new_connections(connection, None)
                    stop.set()
                    for spinner in spinners:
                        spinner.join()
                by_contenders[str(contenders)] = samples
            result["queries"][name] = by_contenders
    return result


_ORIGINAL_CONNECT = []


def _trace_new_connections(connection, trace) -> None:
    """Count the statements of connections a query opens, such as its read session's."""
    if not _ORIGINAL_CONNECT:
        _ORIGINAL_CONNECT.append(connection._connect)
    original = _ORIGINAL_CONNECT[0]
    if trace is None:
        connection._connect = original
        return

    def traced(*args, **kwargs):
        opened = original(*args, **kwargs)
        opened.set_trace_callback(trace)
        return opened

    connection._connect = traced


def _summary(result: dict) -> dict[str, object]:
    found = {}
    for name, by_contenders in result["queries"].items():
        for contenders, samples in by_contenders.items():
            walls = sorted(sample["wall_ms"] for sample in samples)
            found[f"{name}/c{contenders}"] = {
                "wall_ms_median": walls[len(walls) // 2], "wall_ms_max": walls[-1],
                "thread_cpu_ms_max": max(sample["thread_cpu_ms"] for sample in samples),
                "outcomes": sorted({sample["outcome"] for sample in samples}),
                "statements": samples[0].get("statements"),
            }
    return found


def _parent(args: argparse.Namespace) -> int:
    report: dict[str, object] = {"src": str(Path(args.src).resolve()), "repeats": args.repeats,
                                 "whole_read": args.whole_read, "by_rows": {}}
    for rows in (int(part) for part in args.rows.split(",")):
        print(f"== rows {rows} ==", file=sys.stderr, flush=True)
        with tempfile.TemporaryDirectory(prefix="exomem-query-measure-", ignore_cleanup_errors=True) as work:
            completed = subprocess.run(
                [sys.executable, __file__, "--child", "--work-dir", work, "--row-count", str(rows),
                 "--src", args.src, "--contenders", args.contenders, "--repeats", str(args.repeats),
                 *(["--whole-read"] if args.whole_read else [])],
                capture_output=True, text=True, check=False,
            )
        if completed.returncode:
            sys.stderr.write(completed.stderr)
            return completed.returncode
        line = next(row for row in reversed(completed.stdout.splitlines()) if row.startswith("RESULT-JSON:"))
        result = json.loads(line.removeprefix("RESULT-JSON:"))
        report["by_rows"][str(rows)] = {"summary": _summary(result), "run": result}
        Path(args.out).write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    for rows, entry in report["by_rows"].items():
        for name, summary in entry["summary"].items():
            print(f"rows={rows} {name} {json.dumps(summary)}", file=sys.stderr)
    print(f"wrote {args.out}", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rows", default="154,5000", help="comma-separated collection sizes")
    parser.add_argument("--contenders", default="0,1,2", help="comma-separated contender thread counts")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--src", default=str(REPO / "src"), help="the source tree to measure")
    parser.add_argument("--whole-read", action="store_true", help="read every row, as before rows were selected")
    parser.add_argument("--out", default="records-query.json")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--work-dir", help=argparse.SUPPRESS)
    parser.add_argument("--row-count", type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        args.rows = args.row_count
        print("RESULT-JSON:" + json.dumps(_child(args)))
        return 0
    return _parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
