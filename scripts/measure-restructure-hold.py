#!/usr/bin/env python3
"""How long one restructure holds the collection store's writer, per step kind and its undo, at one collection size.

Diagnosis harness that sets `restructure.MAX_COLLECTION_ROWS`; not a gate. It builds an invented collection of ROWS
rows in a temporary vault and state root, runs each step kind through `CollectionWriter.restructure_collection` and
then its undo, and prints one JSON line per step kind and repeat. The hold is the longest time inside
`CollectionWriter._mutation` that the call spent (a split makes its new collection in a mutation of its own, so a
call can span two). The load average is the one-minute average after the step.

    uv run python scripts/measure-restructure-hold.py 2000 --repeats 3

The steps cover the largest restructure that one call allows (`restructure.MAX_REWRITTEN_ROWS` rows written and
retracted together, a moved row counting twice): a retitle and an added field of the ROWS-row collection; a rename and
a retype of a field that 256 rows hold; a split of 128 rows out of it; a merge of a 128-row collection into it; and a
rekey of a 128-row collection, which rewrites every row twice. Each repeat builds a fresh store, so the history of one
repeat does not reach the next.

Nothing here touches a live service or a real state root: the process points the state, configuration and vault
settings at a temporary tree before it imports `exomem`. The store carries no marker, so it measures the direct path
and not a call through the service.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WHY = "measure a restructure"
EVIDENCE = "Knowledge Base/Evidence/import-a.md"
BATCH = 500
WIDE, SPARSE, SPLIT, SMALL = "2db90f18-70df-4e41-986e-2d7d7db1caca", 256, 128, 128
MERGED, NARROW = "5f0c56f6-1d0e-4c55-9a43-0e7f0b7e2a11", "6f1c2d01-5c0f-4f8e-9a51-4a438d0b6b4b"

MANIFEST = """---
type: collection
exomem_id: {cid}
title: {title}
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
    count: {{type: integer}}
    note: {{type: string}}
    level: {{type: integer}}
---
"""


def _path(title: str) -> str:
    return f"Knowledge Base/Records/{title}/_collection.md"


def _steps(wide: str, small: str, narrow: str):
    """Each step kind as `(name, collection, call arguments)`; the undo of each is called with the entry's id."""
    return (
        ("retitle", wide, {"steps": [{"step": "retitle", "title": "Work retitled"}]}),
        ("add_field", wide, {"steps": [{"step": "add_field", "field": "extra", "type": "string"}]}),
        ("rename_field", wide, {"steps": [{"step": "rename_field", "field": "note", "to": "memo"}]}),
        ("retype_field", wide, {"steps": [{"step": "retype_field", "field": "level", "type": "string"}]}),
        ("split_out", wide, {"steps": [
            {"step": "split_out", "where": {"field": "count", "op": "lt", "value": SPLIT}, "title": "Apart"}]}),
        ("merge_into", small, {"steps": [{"step": "merge_into", "target": wide}]}),
        ("rekey", narrow, {"steps": [{"step": "rekey", "key": ["title", "count"]}]}),
    )


def _run(args: argparse.Namespace, base: Path, repeat: int) -> None:
    from exomem.collection_store import connection
    from exomem.collection_store.writer import CollectionWriter
    from exomem.governance.principal import library_scope

    holds: list[float] = []
    original = CollectionWriter._mutation

    @contextmanager
    def timed(self, *arguments, **keywords):
        started = time.perf_counter()
        try:
            with original(self, *arguments, **keywords) as value:
                yield value
        finally:
            holds.append(time.perf_counter() - started)

    root = base / f"vault-{repeat}"
    (root / "Knowledge Base").mkdir(parents=True)
    (root / "Knowledge Base/log.md").write_text("# Existing log\n", encoding="utf-8")
    (root / EVIDENCE).parent.mkdir(parents=True)
    (root / EVIDENCE).write_text("---\ntype: evidence\n---\n\nPreserved import.\n", encoding="utf-8")
    with connection.open_writer(base / f"collections-{repeat}.sqlite", lease_check=lambda: True) as handle, \
            library_scope():
        store = CollectionWriter(root, handle)
        for cid, title, rows in ((WIDE, "Work", args.rows), (MERGED, "Small", SMALL), (NARROW, "Narrow", SMALL)):
            store.create_collection(_path(title), MANIFEST.format(cid=cid, title=title), why=WHY, scaffold=False)
            for first in range(0, rows, BATCH):
                batch = [{"item": {"title": f"{title.lower()} {n:06d}", "count": n,
                                   **({"note": f"note {n}", "level": n} if n < SPARSE else {})}}
                         for n in range(first, min(rows, first + BATCH))]
                after = store.inspect_collection(cid)["lifecycle_guards"]["expected_container_hash"]
                store.bulk_upsert_records(cid, rows=batch, why=WHY, source=EVIDENCE, expected_container_hash=after)
        CollectionWriter._mutation = timed
        try:
            for name, cid, call in _steps(WIDE, MERGED, NARROW):
                line: dict[str, object] = {"rows": args.rows, "repeat": repeat + 1, "step": name}
                entry = ""
                for phase in ("apply", "undo"):
                    holds.clear()
                    started = time.perf_counter()
                    try:
                        applied = store.restructure_collection(
                            cid, why=WHY, **(call if phase == "apply" else {"undo": entry}))
                        entry, outcome = applied["shape"]["id"], applied["outcome"]
                    except Exception as error:  # noqa: BLE001 - a refusal is a measurement, named by its code
                        outcome = getattr(error, "code", type(error).__name__)
                    line[phase] = {"outcome": outcome, "hold_s": round(max(holds, default=0.0), 3),
                                   "wall_s": round(time.perf_counter() - started, 3)}
                    if outcome != "committed":
                        break
                line["load1"] = round(os.getloadavg()[0], 2)
                line["at"] = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                print(json.dumps(line), flush=True)
        finally:
            CollectionWriter._mutation = original


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("rows", type=int, help="the rows of the collection that the steps reshape")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--work-dir", help="where the temporary vault and state root go (default: the system temp dir)")
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _records_measurement_env import clear_inherited, isolate

    with tempfile.TemporaryDirectory(prefix="exomem-restructure-hold-", dir=args.work_dir,
                                     ignore_cleanup_errors=True) as work:
        base = Path(work)
        clear_inherited()
        isolate(base, base / "vault")
        sys.path.insert(0, str(REPO / "src"))
        from exomem import restructure

        # The bound is not under measurement here: a refusal would hide the hold it protects against. The merge adds
        # its source's rows to the collection, so that collection needs room for them.
        restructure.MAX_COLLECTION_ROWS = max(restructure.MAX_COLLECTION_ROWS, args.rows + SMALL)
        for repeat in range(args.repeats):
            _run(args, base, repeat)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
