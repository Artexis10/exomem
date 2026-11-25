#!/usr/bin/env python3
"""Latency of Records timeline writes and reads on an invented equipment collection (record-value-timeline 1.1).

Diagnosis harness, not a gate. Each run is a fresh child process with a temp vault and state root. It creates
an invented equipment collection in the store and writes ``--rows`` rows through the real bulk upsert. The
typed run uses a summary collection, which the store creates typed. The JSON run uses one view file per row,
which keeps the JSON encoding; the run publishes no view file. Each run reports the encoding the store holds. Each row gets 5 to 20 versions, about 10 on average, each with a stated effective time. A
40-value `location` field changes in about half the versions. A 4-value `status` field changes in about a
third of them and is skewed toward one value; the run reports the share of status spans that value holds.
About 1% of rows then gain a past point and 0.5% a correction, through the real edit.

The write cases time one owner edit by row id through the governed Records route: with a stated
`effective_at` that lands current, with one that lands past, and with `corrects`. Each case records wall
time, the invoking thread's CPU time, the SQL statement count and the outcome. The query, import and index
cases are listed as skipped until the timeline query and its span index exist.

    uv run python scripts/measure-records-timeline.py --rows 5000,100000 --out records-timeline.json

Nothing here touches a live service or a real state root.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CID = "3c7e1d52-8a4b-4f6e-9d21-5b0a6c8e2f47"
TITLE = "Equipment"
EVIDENCE = "Knowledge Base/Sources/Invented/stock-counts.md"
BATCH = 500
FIRST_DAY = dt.date(2025, 1, 6)
ROUND_DAYS = 7  # each round of versions takes effect a week after the one before
#: Between the first round and the second, so an edit dated then lands past on every row that has moved.
PAST_DAYS = (dt.timedelta(days=3), dt.timedelta(days=4))

MANIFEST = f"""---
type: collection
exomem_id: {CID}
title: {TITLE}
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
{{storage}}  source: Items
  format_version: 1
item_schema:
  natural_key: [asset_tag]
  fields:
    asset_tag: {{type: string, required: true}}
    location: {{type: string}}
    status: {{type: string}}
    checks: {{type: integer}}
---

Invented equipment for a timeline measurement.
"""

#: The typed run's summary collection; the JSON run keeps one view file per row.
STORAGE = {"typed": "view_mode: summary\nstorage:\n  strategy: sqlite\n",
           "json-v1": "storage:\n  strategy: markdown-items\n"}

LOCATIONS = [f"Room {number:02d}" for number in range(40)]
# Skewed toward one status, so a key-order read meets a low-cardinality, common value. A change never repeats
# the value it leaves, so that value's share of spans stays well under its 70% weight; the run reports it.
STATUSES, STATUS_WEIGHTS = ["in service", "lent", "in repair", "retired"], [70, 12, 12, 6]

# The timeline query, import and index cases need the query and its span index (record-value-timeline 5.x).
SKIPPED = ["one_row_timeline_by_natural_key", "at_instant_eq", "at_date", "range_eq", "at_instant_eq_common_status",
           "at_instant_contains", "changes_one_month", "changes_one_year", "current_filter_with_timeline",
           "import_5000_one_indexed_field", "index_build_one_field"]


def _day(round_number: int) -> str:
    return (FIRST_DAY + dt.timedelta(days=ROUND_DAYS * round_number)).isoformat()


def _other(rng: random.Random, values: list[str], *taken: str, weights: list[int] | None = None) -> str:
    while True:
        value = rng.choices(values, weights)[0]
        if value not in taken:
            return value


def _seed(store, rows: int, rng: random.Random) -> dict[str, object]:
    """Write every row's versions round by round, one bulk upsert per batch, and return what each row holds."""
    versions = [max(5, min(20, round(rng.triangular(5, 20, 5.5)))) for _ in range(rows)]
    held = [{"asset_tag": f"EQ-{number:06d}", "location": rng.choice(LOCATIONS),
             "status": rng.choices(STATUSES, STATUS_WEIGHTS)[0], "checks": 0} for number in range(rows)]
    first = [item["location"] for item in held]
    status_spans = {status: 0 for status in STATUSES}
    for item in held:
        status_spans[item["status"]] += 1
    last_move: dict[int, str] = {}  # row -> the transition of its latest location change
    for round_number in range(max(versions)):
        due = [number for number in range(rows) if versions[number] > round_number]
        moved = set()
        if round_number:
            for number in due:
                item = held[number]
                item["checks"] = round_number
                if rng.random() < 0.5:
                    item["location"] = _other(rng, LOCATIONS, item["location"])
                    moved.add(number)
                if rng.random() < 1 / 3:
                    item["status"] = _other(rng, STATUSES, item["status"], weights=STATUS_WEIGHTS)
                    status_spans[item["status"]] += 1
        for start in range(0, len(due), BATCH):
            batch = due[start:start + BATCH]
            guard = store.inspect_collection(CID)["lifecycle_guards"]["expected_container_hash"]
            reply = store.bulk_upsert_records(
                CID, rows=[{"item": dict(held[number])} for number in batch],
                why="stock count", source=EVIDENCE, expected_container_hash=guard, effective_at=_day(round_number))
            for number in moved.intersection(batch):
                last_move[number] = reply["first_transition"]
    common = max(status_spans.values()) / sum(status_spans.values())
    return {"versions": sum(versions), "held": held, "first": first, "last_move": last_move,
            "common_status_span_share": round(common, 3)}


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
    (vault / EVIDENCE).write_text("---\ntype: evidence\n---\n\nInvented stock counts.\n", encoding="utf-8")
    sys.path.insert(0, str(Path(args.src).resolve()))

    import exomem
    from exomem import structured_collections as collections
    from exomem.collection_store import connection
    from exomem.collection_store.preview import preview_store
    from exomem.collection_store.writer import CollectionWriter
    from exomem.governance.principal import library_scope, owner_principal, request_scope
    from exomem.record_memory import record_memory

    rng = random.Random(args.seed)
    result: dict[str, object] = {"rows": args.rows, "encoding": args.encoding, "seed": args.seed,
                                 "src": str(Path(exomem.__file__).resolve().parents[1]), "cases": {},
                                 "skipped": {name: "needs slice B: the timeline query and its span index"
                                             for name in SKIPPED}}
    store_path = tmp / "collections.sqlite"
    with connection.open_writer(store_path, lease_check=lambda: True) as handle, library_scope():
        store = CollectionWriter(vault, handle)
        with request_scope(owner_principal()):
            store.create_collection(f"Knowledge Base/Records/{TITLE}/_collection.md",
                                    MANIFEST.replace("{storage}", STORAGE[args.encoding]), why="measure", scaffold=False)
            started = time.perf_counter()
            seeded = _seed(store, args.rows, rng)
            result["seed_s"] = round(time.perf_counter() - started, 1)
        result["stored_encoding"] = handle.connection.execute(
            "SELECT encoding FROM collections WHERE collection_id=?", (CID,)).fetchone()[0]
        held, first, last_move = seeded["held"], seeded["first"], seeded["last_move"]
        moved = sorted(last_move)
        rng.shuffle(moved)
        statements = [0]
        handle.connection.set_trace_callback(None)

        def edit(number, changes, **timeline):
            with request_scope(owner_principal(surface="mcp")), preview_store(vault, handle):
                [row] = record_memory(vault, "query", collection=CID,
                                      filters=[{"column": "asset_tag", "op": "eq",
                                                "value": held[number]["asset_tag"]}])["rows"]
                handle.connection.set_trace_callback(lambda _sql: statements.__setitem__(0, statements[0] + 1))
                statements[0] = 0
                wall, cpu = time.perf_counter(), time.thread_time()
                try:
                    reply = record_memory(vault, "update", collection=CID, item_id=row["id"], changes=changes,
                                          why="measure", **timeline)
                    outcome, detail = reply["outcome"], {"timeline": reply.get("timeline")}
                except collections.CollectionError as error:
                    outcome, detail = error.code, {"message": str(error)[:200]}
                finally:
                    handle.connection.set_trace_callback(None)
            return {"wall_ms": round((time.perf_counter() - wall) * 1000, 1),
                    "thread_cpu_ms": round((time.thread_time() - cpu) * 1000, 1),
                    "statements": statements[0], "outcome": outcome, **detail}

        def new_location(number):
            """A location the row neither holds now nor held at first, so a past edit always records a point."""
            return _other(rng, LOCATIONS, held[number]["location"], first[number])

        def past(number, offset):
            return edit(number, {"location": new_location(number)}, effective_at=(FIRST_DAY + offset).isoformat())

        # About 1% of rows hold a past point and 0.5% a correction, written through the real edit.
        pasts, corrections = max(1, args.rows // 100), max(1, args.rows // 200)
        for number in moved[:pasts]:
            past(number, PAST_DAYS[0])
        for number in moved[pasts:pasts + corrections]:
            edit(number, {"location": new_location(number)}, corrects=last_move[number])
        spare = moved[pasts + corrections:]
        today = dt.datetime.now(dt.UTC).date().isoformat()
        cases = {
            "edit_by_id_effective_at_current": lambda number: edit(
                number, {"location": new_location(number)}, effective_at=today),
            "edit_by_id_effective_at_past": lambda number: past(number, PAST_DAYS[1]),
            "edit_by_id_corrects": lambda number: edit(
                number, {"location": new_location(number)}, corrects=last_move[number]),
        }
        for name, case in cases.items():
            case(spare.pop())  # warm imports and caches, as a running service has them
            result["cases"][name] = [case(spare.pop()) for _ in range(args.repeats)]
        result.update(seeded_versions=seeded["versions"], common_status_span_share=seeded["common_status_span_share"],
                      past_points=pasts, corrections=corrections, store_bytes=store_path.stat().st_size)
    return result


def _summary(result: dict) -> dict[str, object]:
    found = {}
    for name, samples in result["cases"].items():
        walls = sorted(sample["wall_ms"] for sample in samples)
        found[name] = {"wall_ms_median": walls[len(walls) // 2], "wall_ms_max": walls[-1],
                       "thread_cpu_ms_max": max(sample["thread_cpu_ms"] for sample in samples),
                       "statements_max": max(sample["statements"] for sample in samples),
                       "outcomes": sorted({sample["outcome"] for sample in samples}),
                       "placements": sorted({json.dumps((sample.get("timeline") or {}).get("placements"))
                                             for sample in samples})}
    return found


def _parent(args: argparse.Namespace) -> int:
    report: dict[str, object] = {"src": str(Path(args.src).resolve()), "repeats": args.repeats, "runs": {}}
    for rows in (int(part) for part in args.rows.split(",")):
        for encoding in args.encodings.split(","):
            print(f"== rows {rows} {encoding} ==", file=sys.stderr, flush=True)
            with tempfile.TemporaryDirectory(prefix="exomem-timeline-measure-", ignore_cleanup_errors=True) as work:
                completed = subprocess.run(
                    [sys.executable, __file__, "--child", "--work-dir", work, "--row-count", str(rows),
                     "--encoding", encoding, "--src", args.src, "--repeats", str(args.repeats),
                     "--seed", str(args.seed)],
                    capture_output=True, text=True, check=False,
                )
            if completed.returncode:
                sys.stderr.write(completed.stderr)
                return completed.returncode
            line = next(row for row in reversed(completed.stdout.splitlines()) if row.startswith("RESULT-JSON:"))
            result = json.loads(line.removeprefix("RESULT-JSON:"))
            report["runs"][f"{rows}/{encoding}"] = {"summary": _summary(result), "run": result}
            Path(args.out).write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    for run, entry in report["runs"].items():
        print(f"{run} stored_encoding={entry['run']['stored_encoding']} seed_s={entry['run']['seed_s']} "
              f"versions={entry['run']['seeded_versions']} "
              f"common_status_span_share={entry['run']['common_status_span_share']} "
              f"store_bytes={entry['run']['store_bytes']}", file=sys.stderr)
        for name, summary in entry["summary"].items():
            print(f"{run} {name} {json.dumps(summary)}", file=sys.stderr)
    print(f"wrote {args.out}", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rows", default="5000,100000", help="comma-separated collection sizes")
    parser.add_argument("--encodings", default="json-v1,typed", help="comma-separated: json-v1, typed")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20260307, help="the fixture's random seed")
    parser.add_argument("--src", default=str(REPO / "src"), help="the source tree to measure")
    parser.add_argument("--out", default="records-timeline.json")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--work-dir", help=argparse.SUPPRESS)
    parser.add_argument("--row-count", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--encoding", default="json-v1", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        args.rows = args.row_count
        print("RESULT-JSON:" + json.dumps(_child(args)))
        return 0
    return _parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
