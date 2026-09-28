"""The readings ledger: durable, append-only, content-keyed, outside the vault."""

from __future__ import annotations

import dataclasses
import os
import sqlite3
from pathlib import Path

import pytest

from exomem import sensing, sensing_ledger

IDENTITY = sensing.InstrumentIdentity(
    model="stub",
    revision="0",
    weights_sha256="0" * 64,
    runtime="stub",
    runtime_version="1",
    template_version="nli-pair-v1",
    label_map_version="relation-v1",
    fixture_set="relation-v1-multilingual",
)


def reading(text_a: str, text_b: str, *, identity=IDENTITY, contra: float = 0.97):
    pair = sorted([(text_a, "Knowledge Base/a.md"), (text_b, "Knowledge Base/b.md")],
                  key=lambda item: sensing.text_sha256(item[0]))
    inputs = [
        sensing.InputUnit(f"exomem://vault/{path}#u", path, sensing.text_sha256(text))
        for text, path in pair
    ]
    vector = [round(1 - contra - 0.01, 6), 0.01, contra]
    return sensing.make_reading(
        sensing.PAIR_RELATION,
        identity,
        inputs,
        ab=vector,
        ba=vector,
        abstain_reason=None,
        sensed_at="2026-09-28T00:00:00Z",
    )


def test_the_ledger_lives_in_the_state_root_not_the_vault(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    path = sensing_ledger.ledger_path(vault)
    assert Path(os.environ["EXOMEM_STATE_ROOT"]) in path.parents
    assert vault not in path.parents
    assert path.name == "readings.sqlite" and path.parent.name == "sensing"
    assert sensing_ledger.open_readonly(vault) is None
    assert not path.exists(), "a reader never creates the ledger"


def test_append_is_idempotent_and_lookups_are_by_content(tmp_path: Path) -> None:
    ledger = sensing_ledger.Ledger(tmp_path / "vault")
    conn = ledger.connect()
    first = reading("The pump fails.", "The pump never fails.")
    assert ledger.append(conn, [first]) == 1
    assert ledger.append(conn, [first]) == 0
    other = dataclasses.replace(IDENTITY, revision="1")
    second = reading("The pump fails.", "The pump never fails.", identity=other)
    assert ledger.append(conn, [second]) == 1
    found = sensing_ledger.by_input_key(conn, sensing.PAIR_RELATION, first.input_key)
    assert {row.instrument_id for row in found} == {
        IDENTITY.instrument_id,
        other.instrument_id,
    }
    assert [row.seq for row in sensing_ledger.since(conn, 0)] == [1, 2]
    assert sensing_ledger.instrument(conn, other.instrument_id) == other
    conn.close()


def test_updates_and_deletes_are_refused(tmp_path: Path) -> None:
    ledger = sensing_ledger.Ledger(tmp_path / "vault")
    conn = ledger.connect()
    ledger.append(conn, [reading("A holds.", "A does not hold.")])
    for statement in (
        "UPDATE readings SET verdict='neutral'",
        "DELETE FROM readings",
        "UPDATE instruments SET identity_json='{}'",
        "DELETE FROM instruments",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(statement)
    assert sensing_ledger.max_seq(conn) == 1
    assert next(sensing_ledger.iter_all(conn)).verdict.label == "contradicts"
    conn.close()


def test_no_vault_text_is_stored(tmp_path: Path) -> None:
    ledger = sensing_ledger.Ledger(tmp_path / "vault")
    conn = ledger.connect()
    secret = "The distinctive phrase zqxv must stay in the vault."
    ledger.append(conn, [reading(secret, "Something else entirely.")])
    conn.close()
    raw = sensing_ledger.ledger_path(tmp_path / "vault").read_bytes()
    wal = sensing_ledger.ledger_path(tmp_path / "vault").with_name("readings.sqlite-wal")
    blob = raw + (wal.read_bytes() if wal.exists() else b"")
    assert b"zqxv" not in blob


def test_a_foreign_schema_is_refused_and_left_untouched(tmp_path: Path) -> None:
    ledger = sensing_ledger.Ledger(tmp_path / "vault")
    conn = ledger.connect()
    conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    conn.close()
    with pytest.raises(sensing_ledger.LedgerUnavailable):
        ledger.connect()
    assert sensing_ledger.open_readonly(tmp_path / "vault") is None
    assert ledger.path.exists()
