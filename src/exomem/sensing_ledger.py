"""The readings ledger: `<vault state dir>/sensing/readings.sqlite`.

Durable, per vault, append-only. Every instrument judgement is one row, and no
row is ever updated or deleted: `BEFORE UPDATE` and `BEFORE DELETE` triggers
abort the statement, so the property holds for any process that opens the file,
not just this module. The ledger is outside the vault (a write here fires no
watcher event, no freshness change and no graph debt) and separate from the
dreamer's disposable sidecar: wiping that sidecar costs a reprojection, never
evidence. Losing this file costs re-sensing, never correctness.

It never stores vault text. Inputs are identified by unit ref, page path and
the sha256 of the exact text fed in.

Writers: only the sensor worker child (and tests). Readers: the dreamer's tick
and request-time status, through non-waiting read-only connections.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import sensing
from .state_paths import vault_state_dir

SCHEMA_VERSION = 1
DIRNAME = "sensing"
FILENAME = "readings.sqlite"

_TABLES = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS instruments ("
    " instrument_id TEXT PRIMARY KEY, identity_json TEXT NOT NULL)",
    """
    CREATE TABLE IF NOT EXISTS readings (
        reading_id TEXT PRIMARY KEY,
        seq INTEGER NOT NULL UNIQUE,
        question_type TEXT NOT NULL,
        instrument_id TEXT NOT NULL,
        input_key TEXT NOT NULL,
        inputs_json TEXT NOT NULL,
        output_json TEXT NOT NULL,
        verdict TEXT NOT NULL,
        label_map_version TEXT NOT NULL,
        fixture_set TEXT NOT NULL,
        sensed_at TEXT NOT NULL,
        placement TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS readings_input ON readings(question_type, input_key)",
    *(
        f"CREATE TRIGGER IF NOT EXISTS {table}_append_only_{verb.lower()} "
        f"BEFORE {verb} ON {table} BEGIN "
        "SELECT RAISE(ABORT, 'the readings ledger is append-only'); END"
        for table in ("readings", "instruments")
        for verb in ("UPDATE", "DELETE")
    ),
)


class LedgerUnavailable(RuntimeError):
    """The ledger cannot be used as this build expects. Never wiped for it."""


def ledger_path(vault_root: Path) -> Path:
    return vault_state_dir(Path(vault_root)) / DIRNAME / FILENAME


def exists(vault_root: Path) -> bool:
    return ledger_path(vault_root).is_file()


@dataclass(frozen=True)
class StoredReading:
    """One ledger row, parsed. `vectors` is None for an instrument-decided abstention."""

    seq: int
    reading_id: str
    question_type: str
    instrument_id: str
    input_key: str
    inputs: tuple[dict[str, Any], ...]
    vectors: dict[str, list[float]] | None
    verdict: sensing.Verdict
    label_map_version: str
    fixture_set: str
    sensed_at: str
    placement: str

    @property
    def hashes(self) -> tuple[str, ...]:
        return tuple(str(item.get("text_sha256") or "") for item in self.inputs)


def _row(row: tuple[Any, ...]) -> StoredReading:
    (seq, rid, question, instrument, key, inputs_json, output_json, _verdict, lmv, fixtures,
     sensed_at, placement) = row
    output = json.loads(output_json)
    verdict = output.get("verdict") or {}
    directions = output.get("directions")
    return StoredReading(
        seq=int(seq),
        reading_id=str(rid),
        question_type=str(question),
        instrument_id=str(instrument),
        input_key=str(key),
        inputs=tuple(json.loads(inputs_json)),
        vectors=None if directions is None else {k: list(v) for k, v in directions.items()},
        verdict=sensing.Verdict(
            str(verdict.get("label")),
            verdict.get("p"),
            verdict.get("direction"),
            verdict.get("reason"),
        ),
        label_map_version=str(lmv),
        fixture_set=str(fixtures),
        sensed_at=str(sensed_at),
        placement=str(placement),
    )


_COLUMNS = (
    "seq, reading_id, question_type, instrument_id, input_key, inputs_json, output_json, "
    "verdict, label_map_version, fixture_set, sensed_at, placement"
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Ledger:
    """One vault's ledger. Connections are short-lived and owned by the caller."""

    def __init__(self, vault_root: Path) -> None:
        self.vault_root = Path(vault_root)
        self.path = ledger_path(self.vault_root)

    def connect(self, *, busy_ms: int = 2000) -> sqlite3.Connection:
        """A read-write connection, creating the ledger. Refuses a foreign schema."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=busy_ms / 1000.0, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("BEGIN IMMEDIATE")
            for statement in _TABLES:
                conn.execute(statement)
            stored = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if stored is None:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif stored[0] != str(SCHEMA_VERSION):
                conn.execute("ROLLBACK")
                raise LedgerUnavailable(
                    f"readings ledger schema {stored[0]!r} is not {SCHEMA_VERSION}; left untouched"
                )
            conn.execute("COMMIT")
        except BaseException:
            conn.close()
            raise
        return conn

    def append(self, conn: sqlite3.Connection, readings: Iterable[sensing.Reading]) -> int:
        """Append readings; an id already present is ignored. Returns rows added."""
        added = 0
        conn.execute("BEGIN IMMEDIATE")
        try:
            for reading in readings:
                identity = reading.instrument
                conn.execute(
                    "INSERT OR IGNORE INTO instruments(instrument_id, identity_json) VALUES (?, ?)",
                    (identity.instrument_id, _canonical(identity.as_dict())),
                )
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO readings(reading_id, seq, question_type, instrument_id, "
                    "input_key, inputs_json, output_json, verdict, label_map_version, fixture_set, "
                    "sensed_at, placement) "
                    "SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ? FROM readings",
                    (
                        reading.reading_id,
                        reading.question_type,
                        identity.instrument_id,
                        reading.input_key,
                        _canonical([item.as_dict() for item in reading.inputs]),
                        _canonical(reading.output()),
                        reading.verdict.label,
                        identity.label_map_version,
                        identity.fixture_set,
                        reading.sensed_at,
                        reading.placement,
                    ),
                )
                added += max(0, cursor.rowcount)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return added


def open_readonly(vault_root: Path) -> sqlite3.Connection | None:
    """A read-only, query-only, non-waiting connection, or None. Never creates."""
    path = ledger_path(Path(vault_root))
    if not path.is_file():
        return None
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0, isolation_level=None)
        conn.execute("PRAGMA query_only=ON")
        stored = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.Error:
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        return None
    if stored is None or stored[0] != str(SCHEMA_VERSION):
        conn.close()
        return None
    return conn


def by_input_key(conn: sqlite3.Connection, question_type: str, key: str) -> list[StoredReading]:
    return [
        _row(row)
        for row in conn.execute(
            f"SELECT {_COLUMNS} FROM readings WHERE question_type=? AND input_key=? "
            "ORDER BY reading_id",
            (question_type, key),
        )
    ]


def since(conn: sqlite3.Connection, seq: int, *, limit: int = 512) -> list[StoredReading]:
    """Rows appended after `seq`, oldest first: the dreamer's ingestion cursor."""
    return [
        _row(row)
        for row in conn.execute(
            f"SELECT {_COLUMNS} FROM readings WHERE seq > ? ORDER BY seq LIMIT ?",
            (int(seq), int(limit)),
        )
    ]


def iter_all(conn: sqlite3.Connection) -> Iterator[StoredReading]:
    for row in conn.execute(f"SELECT {_COLUMNS} FROM readings ORDER BY reading_id"):
        yield _row(row)


def has_reading(conn: sqlite3.Connection, rid: str) -> bool:
    return conn.execute("SELECT 1 FROM readings WHERE reading_id=?", (rid,)).fetchone() is not None


def max_seq(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM readings").fetchone()
    return int(row[0] if row else 0)


def instrument(conn: sqlite3.Connection, instrument_id: str) -> sensing.InstrumentIdentity | None:
    row = conn.execute(
        "SELECT identity_json FROM instruments WHERE instrument_id=?", (instrument_id,)
    ).fetchone()
    return None if row is None else sensing.InstrumentIdentity.from_dict(json.loads(row[0]))
