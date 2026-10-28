"""Write bytes per imported row of a derived collection import, and of a stopped import.

A manual instrument, not a CI test: README.md says how to run it. It drives the public
import tool against the in-process service from the test suite, and samples this
process's `write_bytes` from `/proc` while the import runs.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import random
import sqlite3
import time
import zipfile
from pathlib import Path

from test_collection_store_service import (  # noqa: F401 - the service fixture
    Service,
    call,
    imported,
    service,
)

from exomem import archive_members
from exomem.collection_store import connection, derived_rows

CID = "77777777-7777-4777-8777-777777777777"
PATH = "Knowledge Base/Records/Probe samples/_collection.md"
OUT = Path(os.environ["DERIVED_PROBE_OUT"])
SHAPE = os.environ.get("DERIVED_PROBE_SHAPE", "date-series-index")
MEMBERS = int(os.environ.get("DERIVED_PROBE_MEMBERS", "160"))
ROWS = int(os.environ.get("DERIVED_PROBE_ROWS", "2880"))
# Rows staged by a second import before it is cancelled; 0 skips the cancel arm.
CANCEL_ROWS = int(os.environ.get("DERIVED_PROBE_CANCEL_ROWS", "0"))
POLL = float(os.environ.get("DERIVED_PROBE_POLL", "2"))
# Ablation only: extra PRAGMA statements on the derived connection, separated by ";".
PRAGMAS = [f"PRAGMA {item}" for item in os.environ.get("DERIVED_PROBE_PRAGMAS", "").split(";") if item]

_SAMPLE = {"value": {"type": "number"}, "local_time": {"type": "string"}}
# Three natural-key shapes: a date-led key, a wall-clock key and a text-led key.
SHAPES = {
    "date-series-index": (
        {"natural_key": ["local_date", "series", "sample_index"], "fields": {
            "local_date": {"type": "date", "filterable": True, "sortable": True, "required": True},
            "series": {"type": "string", "filterable": True, "required": True},
            "sample_index": {"type": "integer", "required": True}, **_SAMPLE}},
        {"rows": "samples[]", "fields": {"series": {"const": "probe"}, "sample_index": "$index",
                                         "local_time": "localTime", "value": "value"},
         "on_invalid": "skip", "time": {"from": [{"date": "$.date"}], "local_date": "local_date"}},
    ),
    "series-floating": (
        {"natural_key": ["series", "local_datetime"], "fields": {
            "local_date": {"type": "date", "filterable": True, "sortable": True, "required": True},
            "local_datetime": {"type": "string", "sortable": True, "required": True},
            "series": {"type": "string", "filterable": True, "required": True}, **_SAMPLE}},
        {"rows": "samples[]", "fields": {"series": {"const": "probe"}, "local_time": "localTime", "value": "value"},
         "on_invalid": "skip", "time": {"from": [{"instant": "wallClock", "floating": True}],
                                         "local_date": "local_date", "local_datetime": "local_datetime"}},
    ),
    "session-index": (
        {"natural_key": ["session", "sample_index"], "fields": {
            "session": {"type": "string", "filterable": True, "required": True},
            "local_date": {"type": "date", "filterable": True, "sortable": True, "required": True},
            "sample_index": {"type": "integer", "required": True}, **_SAMPLE}},
        {"rows": "samples[]", "fields": {"session": "$.session", "sample_index": "$index",
                                         "local_time": "localTime", "value": "value"},
         "on_invalid": "skip", "time": {"from": [{"date": "$.date"}], "local_date": "local_date"}},
    ),
}


def _manifest() -> str:
    schema, mapping = SHAPES[SHAPE]
    return ("---\ntype: collection\nexomem_id: " + CID + "\ntitle: Probe samples\nsemantic_profile: records\n"
            "collection_version: 1\nschema_version: 1\nlifecycle: active\nview_mode: summary\nderived: true\n"
            'storage: {"strategy": "markdown-items", "source": "Items", "format_version": 1}\n'
            "item_schema: " + json.dumps(schema) + "\nimports: " + json.dumps({"probe": {
                "format": "json-document", "mapping": mapping}}) + "\n---\n")


def _member(day: int, rows: int, rng: random.Random) -> dict:
    date = dt.date(2000, 1, 1) + dt.timedelta(days=day)
    start = dt.datetime.combine(date, dt.time())
    samples = []
    for i in range(rows):
        wall = start + dt.timedelta(seconds=i * 30)
        samples.append({"localTime": wall.strftime("%H:%M:%S.000"), "wallClock": wall.strftime("%Y-%m-%dT%H:%M:%S.000"),
                        "value": round(rng.uniform(0.5, 9.5), 4)})
    # A text session id that does not ascend with the member order.
    return {"date": date.isoformat(), "session": f"s-{rng.getrandbits(64):016x}", "samples": samples}


def _zip(members: dict) -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as zipped:
        for name, document in members.items():
            zipped.writestr(name, json.dumps(document))
    return data.getvalue()


def _written() -> int:
    for line in Path("/proc/self/io").read_text().splitlines():
        if line.startswith("write_bytes"):
            return int(line.split()[1])
    raise AssertionError("no write_bytes in /proc/self/io")


def _derived_bytes(root: Path) -> int:
    base = derived_rows.path(connection.store_path(root))
    return sum(path.stat().st_size for path in base.parent.glob(base.name + "*") if path.is_file())


def _emit(record: dict) -> None:
    with OUT.open("a") as out:
        out.write(json.dumps(record) + "\n")


def _digest(root: Path) -> str:
    """Hash the collection's rows, rollups and applied sequence in the derived file."""
    path = derived_rows.path(connection.store_path(root))
    digest = hashlib.sha256()
    with contextlib.closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as reader:
        prefix = "cq_" + CID.replace("-", "") + "_"
        tables = sorted(name for (name,) in reader.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name LIKE ? ESCAPE '\\'",
            (prefix.replace("_", "\\_") + "%",)) if not name.endswith("_undo"))
        statements = [f'SELECT * FROM "{name}" ORDER BY row_id' for name in tables]
        statements += ["SELECT * FROM rollup_buckets ORDER BY 1,2,3", "SELECT * FROM rollup_members ORDER BY 1,2,3,4",
                       "SELECT * FROM derived_flagged ORDER BY 1",
                       "SELECT applied_seq FROM derived_collections WHERE collection_id = '" + CID + "'"]
        for statement in statements:
            digest.update(statement.encode())
            for row in reader.execute(statement):
                digest.update(repr(row).encode())
    return digest.hexdigest()


def _run(root: Path, phase: str, token: str, *, stop=None, poll: float = POLL) -> dict:
    """Poll a job until it ends or `stop` says so; emit one sample per poll."""
    while True:
        status = imported(root, collection=CID, mode="status", continuation=token)
        _emit({"phase": phase, "t": time.monotonic(), "written": _written(), "rows": status["rows"],
               "batches": status["batches"], "state": status["state"], "derived_bytes": _derived_bytes(root)})
        if status["state"] != "running" or (stop is not None and stop(status)):
            return status
        time.sleep(poll)


def _ablation(monkeypatch) -> None:
    if not PRAGMAS:
        return
    original = derived_rows.Store._open

    def _open(self):
        conn = original(self)
        for statement in PRAGMAS:
            conn.execute(statement)
        return conn

    monkeypatch.setattr(derived_rows.Store, "_open", _open)


def test_write_probe(service: Service, monkeypatch):  # noqa: F811 - the imported fixture
    _ablation(monkeypatch)
    root = service.root
    _emit({"phase": "config", "shape": SHAPE, "members": MEMBERS, "rows_per_member": ROWS,
           "cancel_rows": CANCEL_ROWS, "pragmas": PRAGMAS})
    call(root, "record_memory", action="create", manifest_path=PATH, manifest_text=_manifest(), why="probe")
    rng = random.Random(7)
    members = {f"main-{n:05d}.json": _member(n, ROWS, rng) for n in range(MEMBERS)}
    if CANCEL_ROWS:
        # One member far larger than CANCEL_ROWS, so the cancel always lands inside it.
        members["extra-00000.json"] = _member(10_000, max(ROWS * 8, CANCEL_ROWS * 8), rng)
    data = _zip(members)
    receipt, _ = archive_members.preserve_members(
        root, guard=contextlib.nullcontext, scope="device", category="export", filename="probe.zip",
        stream=io.BytesIO(data), max_bytes=len(data), verified="upload")
    job = imported(root, collection=CID, mode="start", source_ref=receipt["path"], mapping="probe", members="main-*")
    final = _run(root, "import", job["continuation"])
    assert final["state"] == "complete", final
    time.sleep(3)  # let the replica publisher settle after the last commit
    inspected = call(root, "record_memory", action="inspect", collection=PATH)
    _emit({"phase": "import_done", "t": time.monotonic(), "written": _written(), "rows": final["rows"],
           "derived_bytes": _derived_bytes(root), "derived_rows": inspected.get("derived_rows")})
    if not CANCEL_ROWS:
        return
    before = _digest(root)
    job = imported(root, collection=CID, mode="start", source_ref=receipt["path"], mapping="probe", members="extra-*")
    staged = _run(root, "extra", job["continuation"], poll=0.1,
                  stop=lambda status: status["rows"]["imported"] >= CANCEL_ROWS)["rows"]["imported"]
    cancelled = imported(root, collection=CID, mode="cancel", continuation=job["continuation"])
    _emit({"phase": "cancelled", "t": time.monotonic(), "written": _written(), "staged_rows": staged,
           "state": cancelled["state"]})
    while True:
        state = call(root, "record_memory", action="inspect", collection=PATH).get("derived_rows") or {}
        _emit({"phase": "settling", "t": time.monotonic(), "written": _written(), "derived": state})
        if state.get("state") == "ready":
            break
        time.sleep(POLL)
    time.sleep(3)
    after = _digest(root)
    _emit({"phase": "settled", "t": time.monotonic(), "written": _written(), "derived": state,
           "digest_before": before, "digest_after": after})
