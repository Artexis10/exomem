"""Latency of the `subject_records` block per (anchor, collection) pair (invented data, not a gate).

    python benchmarks/subject_records/latency.py --out latency.json [--sizes 5000,100000]

Each run builds a temporary vault and store: one person page, one Records ledger whose rows cite it
(`size` rows, plus one row in ten citing a second person), with one null amount and one undated row in
every hundred. It times `subject_records.summaries` for that page, which is all the work the block adds
to an activation, in two cases: a turn that touches the ledger's claims (the block is served) and one
that does not (the gate refuses it, the cost every other activation with a cited ledger pays). Each
sample opens its own read session, as an activation does. Machine state goes under a temporary
`EXOMEM_STATE_ROOT`, never the user's own.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRATCH = Path(tempfile.mkdtemp(prefix="subject-records-latency-"))
# Before exomem is imported: every state path resolves under this run's own root.
os.environ["EXOMEM_STATE_ROOT"] = str(SCRATCH / "state")
os.environ["EXOMEM_WRITER_LEASE_STATE_DIR"] = str(SCRATCH / "state" / "writer-lease")
os.environ["EXOMEM_DISABLE_EMBEDDINGS"] = "1"
sys.path.insert(0, str(REPO / "src"))

from exomem import freshness, semantic_contract, subject_records  # noqa: E402
from exomem.collection_store import connection  # noqa: E402
from exomem.collection_store.writer import ITEMS_MODE_MAX_ROWS, CollectionWriter  # noqa: E402
from exomem.governance.principal import owner_principal, request_scope  # noqa: E402
from exomem.query_engine import runtime  # noqa: E402
from exomem.vault import walk_vault_md  # noqa: E402

LEDGER = "a1a1a1a1-0000-4000-8000-00000000a1a1"
ANCHOR = "b2b2b2b2-0000-4000-8000-00000000b2b2"
OTHER = "c3c3c3c3-0000-4000-8000-00000000c3c3"
EVIDENCE = "Knowledge Base/Evidence/import-a.md"
SAMPLES, BATCH = 30, 500
SERVED, GATED = "what does Ada owe me", "how is Ada doing these days"
STORAGE = {"typed": "view_mode: summary\nstorage:\n  strategy: sqlite\n  source: Items\n  format_version: 1\n",
           "json": "storage:\n  strategy: markdown-items\n  source: Items\n  format_version: 1\n"}


def manifest(layout: str) -> str:
    return (f"---\ntype: collection\nexomem_id: {LEDGER}\ntitle: Tab\nsemantic_profile: records\n"
            "collection_version: 1\nschema_version: 1\nlifecycle: active\n"
            "claims:\n  terms: [owe, owes, lent, borrowed]\n"
            "subject: {field: person, measure: amount, state: status, order: day, show: [note]}\n"
            f"{STORAGE[layout]}item_schema:\n  natural_key: [label]\n  fields:\n"
            "    label: {type: string, required: true}\n    person: {type: link}\n    amount: {type: integer}\n"
            "    status: {type: enum, enum: [settled, provisional]}\n    day: {type: date}\n    note: {type: string}\n---\n")


def person(root: Path, name: str, identity: str) -> None:
    target = root / "Knowledge Base" / "Entities" / "People" / f"{name}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"---\ntype: entity\nentity_type: person\nexomem_id: {identity}\nstatus: active\n---\n\n# {name}\n")


def row(number: int) -> dict:
    cited = OTHER if number % 10 == 9 else ANCHOR
    item = {"label": f"r{number}", "person": f"exomem://memory/{cited}", "status": ("settled", "provisional")[number % 3 == 0],
            "amount": None if number % 100 == 7 else (number % 97) - 40, "note": f"entry {number}"}
    if number % 100 != 11:
        item["day"] = f"20{20 + number % 7}-{1 + number % 12:02}-{1 + number % 28:02}"
    return item


def pct(samples: list[float]) -> dict:
    ordered = sorted(samples)
    return {"p50_ms": round(statistics.median(ordered) * 1e3, 2),
            "p95_ms": round(ordered[int(0.95 * (len(ordered) - 1) + 0.5)] * 1e3, 2), "samples": len(ordered)}


def run(size: int, layout: str, scratch: Path) -> dict:
    root = scratch / "vault"
    (root / "Knowledge Base").mkdir(parents=True)
    (root / "Knowledge Base/log.md").write_text("# Log\n")
    person(root, "Ada Lindqvist", ANCHOR)
    person(root, "Quen Marsh", OTHER)
    (root / EVIDENCE).parent.mkdir(parents=True)
    (root / EVIDENCE).write_text("---\ntype: evidence\n---\n\nInvented import.\n")
    started = time.perf_counter()
    with connection.open_writer(connection.store_path(root), lease_check=lambda: True) as handle, \
            request_scope(owner_principal()):
        store = CollectionWriter(root, handle)
        store.create_collection("Knowledge Base/Records/Tab/_collection.md", manifest(layout), why="bench", scaffold=False)
        guard = store.inspect_collection(LEDGER)["lifecycle_guards"]["expected_container_hash"]
        rows = size + size // 9  # one row in ten cites the other person
        if layout == "json":
            # A Markdown-items collection holds at most this many rows; the report records how many cite the anchor.
            rows = min(rows, ITEMS_MODE_MAX_ROWS)
        for start in range(0, rows, BATCH):
            result = store.bulk_upsert_records(LEDGER, rows=[{"item": row(n)} for n in range(start, min(start + BATCH, rows))],
                                               why="bench", expected_container_hash=guard, source=EVIDENCE)
            assert result["committed"], result["rows"][:1]
            guard = result["after_container_hash"]
        # The serving store thread builds the reference index in the background; here it runs to completion.
        store.backfill_derived(LEDGER, limit=BATCH)
        while handle.connection.execute(
                "SELECT 1 FROM item_reference_state WHERE collection_id=? AND ready=0", (LEDGER,)).fetchone():
            store.backfill_derived(LEDGER, limit=BATCH)
        cited = handle.connection.execute(
            "SELECT count(*) FROM item_references WHERE collection_id=? AND match=?", (LEDGER, f"memory:{ANCHOR}")).fetchone()[0]
    built = time.perf_counter() - started
    freshness.seed(root, "vault", ((str(found), freshness.stat_signature(found)) for found in walk_vault_md(root)))
    semantic_contract.build_corpus_context(root)
    anchors = [(f"exomem://memory/{ANCHOR}", "Ada Lindqvist")]
    result = {"rows_in_collection": rows, "rows_citing_anchor": cited, "build_s": round(built, 1)}
    opened, statements = runtime.read_session, []

    @contextlib.contextmanager
    def traced(*args, **kwargs):
        with opened(*args, **kwargs) as session:
            session.connection.set_trace_callback(statements.append)
            yield session

    for case, turn in (("served", SERVED), ("gated", GATED)):
        times, states = [], set()
        runtime.read_session = traced
        try:
            with request_scope(owner_principal()):
                for _ in range(SAMPLES):
                    statements.clear()
                    began = time.perf_counter()
                    block = subject_records.summaries(root, anchors, turn)
                    times.append(time.perf_counter() - began)
                    # A gated turn reads no block; a served one carries its entries, and its own state if it has one.
                    entries = block.entries if block is not None else []
                    states.update(entry["state"] for entry in entries)
                    if block is not None and block.state:
                        states.add(f"block:{block.state['state']}")
        finally:
            runtime.read_session = opened
        result[case] = pct(times) | {"states": sorted(states), "statements_per_call": len(statements),
                                     "row_statements_per_call": sum("item_references r CROSS JOIN" in s for s in statements)}
        if case == "served":
            result["served"]["entry"] = {key: value for key, value in entries[0].items() if key != "newest"} if entries else None
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--sizes", default="5000,100000", help="comma-separated rows citing the anchor")
    parser.add_argument("--layouts", default="typed,json")
    args = parser.parse_args()
    sha = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short=9", "HEAD"], capture_output=True, text=True).stdout.strip()
    report = {"exomem_git_sha": sha, "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
              "cpu_count": os.cpu_count(), "samples_per_case": SAMPLES, "scope": "invented data, single run, not a gate",
              "target": "p95 <= 300 ms at 5,000 rows and <= 1 s at 100,000 rows per (anchor, collection) pair",
              "runs": {}}
    try:
        for size in map(int, args.sizes.split(",")):
            for layout in args.layouts.split(","):
                with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
                    data = run(size, layout, Path(tmp))
                report["runs"][f"N={size},{layout}"] = data
                print(size, layout, json.dumps(data), flush=True)
                # Each run is written as it ends, so a later failure keeps the earlier measurements.
                Path(args.out).write_text(json.dumps(report, indent=2) + "\n")
    finally:
        import shutil

        shutil.rmtree(SCRATCH, ignore_errors=True)


if __name__ == "__main__":
    main()
