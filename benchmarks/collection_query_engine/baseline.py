"""Baseline latency, WAL bytes and RSS of the dark Records store (invented data, not a gate).

    python benchmarks/collection_query_engine/baseline.py --out baseline.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import resource
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
os.environ["EXOMEM_COLLECTION_STORE_PREVIEW"] = "1"

from exomem.collection_store import connection  # noqa: E402
from exomem.collection_store.writer import CollectionWriter  # noqa: E402
from exomem.query_engine import legacy, runtime  # noqa: E402
from exomem.query_engine import sqlite as qsqlite  # noqa: E402
from exomem.query_engine.typed_rows import execute_rows  # noqa: E402
from exomem.query_engine.validation import normalize_query  # noqa: E402

CID = "2db90f18-70df-4e41-986e-2d7d7db1caca"
PATH = "Knowledge Base/Records/Work/_collection.md"
EVIDENCE = "Knowledge Base/Evidence/import-a.md"
SEED, SAMPLES, BULK_SAMPLES, BATCH = 20261005, 30, 5, 500
STATUSES = ("open", "closed", "blocked", "review")
FIELDS = {"title": "string", "count": "integer", "status": "string", "amount": "integer"}
INDEXES = "indexes:\n" + "".join(f"  by_{f}:\n    keys: [{{field: {f}}}]\n" for f in FIELDS)


def manifest(indexed: bool) -> str:
    flags = ", filterable: true, sortable: true" if indexed else ""
    fields = "".join(f"    {f}: {{type: {t}{', required: true' if f == 'title' else ''}{flags}}}\n"
                     for f, t in FIELDS.items())
    return (f"---\ntype: collection\nexomem_id: {CID}\ntitle: Work\nsemantic_profile: records\n"
            "collection_version: 1\nschema_version: 1\nlifecycle: active\nstorage:\n"
            "  strategy: markdown-items\n  source: Items\n  format_version: 1\n"
            f"item_schema:\n  natural_key: [title]\n  fields:\n{fields}{INDEXES if indexed else ''}---\n")


def pct(samples):
    ordered = sorted(samples)
    return {"p50_ms": round(statistics.median(ordered) * 1e3, 3),
            "p95_ms": round(ordered[int(0.95 * (len(ordered) - 1) + 0.5)] * 1e3, 3), "samples": len(ordered)}


def timed(fn, n=SAMPLES):
    out = []
    for _ in range(n):
        start = time.perf_counter()
        try:
            fn()
        except runtime.QueryError as error:  # the product's own refusal (200 ms deadline, no ready projection)
            return {"refused": str(error).split(":")[0], "after_samples": len(out)}
        out.append(time.perf_counter() - start)
    return pct(out)


def run(size: int, indexed: bool, scratch: Path) -> dict:
    rng = random.Random(SEED + size)
    counter = iter(range(10**9))

    def row():
        n = next(counter)
        return {"title": f"Record {n}", "count": rng.randrange(10**6),
                "status": rng.choice(STATUSES), "amount": rng.randrange(10**4)}

    root = scratch / "vault"
    (root / "Knowledge Base").mkdir(parents=True)
    (root / "Knowledge Base/log.md").write_text("# Log\n")
    (root / EVIDENCE).parent.mkdir(parents=True)
    (root / EVIDENCE).write_text("---\ntype: evidence\n---\n\nInvented import.\n")
    with connection.open_writer(scratch / "collections.sqlite", lease_check=lambda: True) as handle:
        store = CollectionWriter(root, handle)
        wal = Path(f"{handle.path}-wal")
        store.create_collection(PATH, manifest(indexed), why="baseline", scaffold=False)
        guard = {"hash": None}

        def container():
            return store.inspect_collection(CID)["lifecycle_guards"]["expected_container_hash"]

        def bulk(rows):
            result = store.bulk_upsert_records(CID, rows=rows, why="baseline", expected_container_hash=guard["hash"],
                                                source=EVIDENCE)
            guard["hash"] = result["after_container_hash"]
            assert result["counts"]["inserted"] == len(rows), (result["counts"], result["rows"][0])

        def append():
            result = store.append_record(CID, item=row(), item_key=str(uuid.UUID(int=rng.getrandbits(128), version=4)),
                                         expected_container_hash=guard["hash"], why="baseline")
            guard["hash"] = result["after_container_hash"]

        guard["hash"] = container()
        for _ in range(size // BATCH):
            bulk([{"item": row()} for _ in range(BATCH)])
        # Autocheckpoint off so the -wal file only grows; its size delta is the bytes the mutation logged.
        handle.connection.execute("PRAGMA wal_autocheckpoint=0")

        def wal_per_mutation(fn, n=SAMPLES):
            deltas = []
            for _ in range(n):
                before = wal.stat().st_size if wal.exists() else 0
                fn()
                deltas.append((wal.stat().st_size if wal.exists() else 0) - before)
            return {"p50": statistics.median(deltas), "max": max(deltas)}

        result = {"append": timed(append), "bulk_500": timed(lambda: bulk([{"item": row()} for _ in range(BATCH)]), BULK_SAMPLES)}
        result["wal_bytes_per_append"] = wal_per_mutation(append)
        result["wal_bytes_per_bulk_500"] = wal_per_mutation(lambda: bulk([{"item": row()} for _ in range(BATCH)]), 5)
        declarations = {CID: {"domain": "collections", "type": "records", "vault": "baseline", "fields": {
            "item_key": {"type": "string"}, **{f: {"type": t} for f, t in FIELDS.items()}}}}
        typed = normalize_query({"version": 1, "select": ["title"], "order_by": [{"field": "count"}],
                                 "page": {"limit": 50}}, collection=CID, declarations=declarations)
        assert not typed.findings, typed.findings
        # A read session carries a 200 ms deadline from its opening, so each sample opens its own.
        def legacy_run(**kw):
            with runtime.read_session(root, handle.path) as session:
                admitted = session.admit(CID)
                plan = legacy.normalize(columns_available=admitted.fields, **kw)
                return qsqlite.execute_legacy(admitted, plan, path="source", format="markdown-items")

        def typed_run():
            with runtime.read_session(root, handle.path) as session:
                return execute_rows(session.admit_query(typed.query, as_of="2026-10-05T00:00:00+00:00"))

        result["legacy_page50_sorted"] = timed(lambda: legacy_run(sort_by="count", limit=50))
        result["legacy_sum"] = timed(lambda: legacy_run(aggregate="sum:count"))
        result["typed_page50_sorted"] = timed(typed_run)
    result["rows_at_start"] = size
    return result


def host() -> dict:
    return {"cpu_count": os.cpu_count(), "os_family": platform.system(), "kernel_release_family": platform.release().split("-")[0],
            "machine": platform.machine(), "filesystem": subprocess.run(
                ["stat", "-f", "-c", "%T", tempfile.gettempdir()], capture_output=True, text=True).stdout.strip()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--sizes", default="1000,10000", help="comma-separated N")
    parser.add_argument("--variants", default="no_index,4_indexes")
    args = parser.parse_args()
    sha = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short=9", "HEAD"], capture_output=True, text=True).stdout.strip()
    report = {"exomem_git_sha": sha, "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
              "host": host(), "pragmas": {"journal_mode": "wal", "synchronous": "FULL", "foreign_keys": "ON", "wal_autocheckpoint": "0 during runs (set by harness)"}, "seed": SEED, "samples_per_metric": SAMPLES, "bulk_samples": BULK_SAMPLES,
              "scope": "dark store, invented data, single run, not a gate",
              "wal_method": "wal_autocheckpoint=0; -wal file size delta around one mutation (frames x (page+24) bytes)",
              "peak_rss_note": "process high-water mark after each run (cumulative, KiB)", "runs": {}}
    for size in map(int, args.sizes.split(",")):
        for indexed in (v == "4_indexes" for v in args.variants.split(",")):
            started = time.perf_counter()
            with tempfile.TemporaryDirectory() as tmp:
                data = run(size, indexed, Path(tmp))
            data["peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            report["runs"][f"N={size},{'4_indexes' if indexed else 'no_index'}"] = data
            print(size, indexed, f"{time.perf_counter() - started:.0f}s", json.dumps(data), flush=True)
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
