"""The readings ledger: durable, append-only, content-keyed, outside the vault."""

from __future__ import annotations

import dataclasses
import os
import sqlite3
from pathlib import Path

import pytest
import sensing_fixture as sf

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


def test_batch_lookup_keeps_content_groups_and_instrument_filter(tmp_path: Path) -> None:
    ledger = sensing_ledger.Ledger(tmp_path / "vault")
    conn = ledger.connect()
    first = reading("A holds.", "A does not hold.")
    other = dataclasses.replace(IDENTITY, revision="1")
    same_content = reading("A holds.", "A does not hold.", identity=other)
    second = reading("B holds.", "B does not hold.")
    ledger.append(conn, [first, same_content, second])
    queries = []
    conn.set_trace_callback(queries.append)
    found = sensing_ledger.by_input_keys(
        conn, sensing.PAIR_RELATION, [first.input_key, second.input_key, "missing"],
        instrument_ids=[IDENTITY.instrument_id],
    )
    conn.set_trace_callback(None)
    assert len(queries) == 1
    assert [row.reading_id for row in found[first.input_key]] == [first.reading_id]
    assert [row.reading_id for row in found[second.input_key]] == [second.reading_id]
    assert found["missing"] == []
    assert [row.reading_id for row in sensing_ledger.by_input_key(conn, sensing.PAIR_RELATION, first.input_key)] == sorted([first.reading_id, same_content.reading_id])
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


def test_insert_or_replace_cannot_rewrite_a_reading(tmp_path: Path) -> None:
    ledger = sensing_ledger.Ledger(tmp_path / "vault")
    conn = ledger.connect()
    ledger.append(conn, [reading("A holds.", "A does not hold.")])
    row = conn.execute("SELECT * FROM readings").fetchone()
    forged = list(row)
    forged[7] = "neutral"
    conn.execute(f"INSERT OR REPLACE INTO readings VALUES ({','.join('?' * len(forged))})", forged)
    assert conn.execute("SELECT * FROM readings").fetchall() == [row]
    other = list(row)
    other[0] = "f" * 64
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(f"INSERT OR REPLACE INTO readings VALUES ({','.join('?' * len(other))})", other)
    assert conn.execute("SELECT * FROM readings").fetchall() == [row]
    identity = conn.execute("SELECT * FROM instruments").fetchone()
    conn.execute("INSERT OR REPLACE INTO instruments VALUES (?, '{}')", (identity[0],))
    assert conn.execute("SELECT * FROM instruments").fetchone() == identity
    genesis = sensing_ledger.genesis(conn)
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('genesis', 'forged')")
    assert sensing_ledger.genesis(conn) == genesis
    conn.close()


def test_the_ledger_has_a_stable_identity_and_a_replacement_a_new_one(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    ledger = sensing_ledger.Ledger(vault)
    conn = ledger.connect()
    first = sensing_ledger.genesis(conn)
    conn.close()
    conn = ledger.connect()
    assert sensing_ledger.genesis(conn) == first and first
    conn.close()
    reader = sensing_ledger.open_readonly(vault)
    assert sensing_ledger.genesis(reader) == first
    reader.close()
    for suffix in ("", "-wal", "-shm"):
        ledger.path.with_name(ledger.path.name + suffix).unlink(missing_ok=True)
    conn = ledger.connect()
    assert sensing_ledger.genesis(conn) not in {None, first}
    conn.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_the_ledger_and_its_directory_are_private(tmp_path: Path) -> None:
    ledger = sensing_ledger.Ledger(tmp_path / "vault")
    ledger.connect().close()
    assert ledger.path.stat().st_mode & 0o777 == 0o600
    assert ledger.path.parent.stat().st_mode & 0o777 == 0o700


def test_version_two_roundtrip_keeps_sql_schema_one_and_foreign_refs_private(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    texts = ("A current claim.", "Another current claim.")
    inputs = tuple(
        sensing.InputSlot(
            f"old-{slot}", f"old-{slot}.md", slot, "page-claim-v1", sensing.text_sha256(text)
        )
        for slot, text in zip(("a", "b"), texts, strict=True)
    )
    request = sensing.SenseRequest(sensing.PAIR_RELATION, 2, inputs, texts)
    reading = sensing.make_request_reading(
        request,
        sf.STUB,
        vectors={"ab": sf.CONTRA, "ba": sf.CONTRA},
        abstain_reason=None,
        sensed_at="2026-01-01T00:00:00Z",
    )
    ledger = sensing_ledger.Ledger(tmp_path)
    conn = ledger.connect()
    assert ledger.append(conn, [reading]) == 1
    assert ledger.append(conn, [reading]) == 0
    [stored] = sensing_ledger.by_input_key(conn, sensing.PAIR_RELATION, request.input_key)
    assert stored.output_version == 2 and stored.columns == (
        "entailment",
        "neutral",
        "contradiction",
    )
    assert stored.vectors == {"ab": sf.CONTRA, "ba": sf.CONTRA}
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() == ("1",)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM readings")
    conn.close()
    moved = replace(
        request,
        inputs=tuple(
            replace(item, path=f"current-{item.slot}.md", unit_ref="current") for item in inputs
        ),
    )
    current = sensing_ledger.lookup_current(
        tmp_path, moved, {sf.STUB.instrument_id: sf.STUB}, "relation-v1"
    )
    assert current.reading_id == reading.reading_id and current.verdict.label == "contradicts"
    assert not hasattr(current, "inputs") and "old-" not in repr(current)


@pytest.mark.parametrize(
    "vectors,columns",
    [
        ({"ab": [0.01, 0.02, 0.97]}, ("entailment", "neutral", "contradiction")),
        (
            {"ab": [0.01, 0.02, 0.97], "ba": [0.01, float("nan"), 0.97]},
            ("entailment", "neutral", "contradiction"),
        ),
        ({"ab": [0.01, 0.97], "ba": [0.01, 0.97]}, ("entailment", "neutral", "contradiction")),
        (
            {"ab": [0.01, 0.02, 0.97], "ba": [0.01, 0.02, 0.97]},
            ("contradiction", "neutral", "entailment"),
        ),
    ],
)
def test_consumer_refuses_unusable_stored_vectors(vectors, columns) -> None:
    assert (
        sensing.rederive(
            vectors, sensing.Verdict("contradicts", 0.97), "relation-v1", columns=columns
        )
        is None
    )


def test_injected_question_can_roundtrip_one_answer_vector(tmp_path: Path, monkeypatch) -> None:
    from dataclasses import replace
    from types import SimpleNamespace

    question = replace(
        sensing.question(sensing.PAIR_RELATION),
        question_type="test.fixed-question",
        label_map_version="test-map-v1",
        slot_names=("left", "right"),
        extractors=("test-extractor-v1",),
        vector_names=("answer",),
    )
    mapping = SimpleNamespace(
        columns=("same", "different", "abstain"),
        apply_vectors=lambda vectors: sensing.Verdict("same", vectors["answer"][0]),
    )
    monkeypatch.setitem(sensing.QUESTIONS, question.question_type, question)
    monkeypatch.setitem(sensing.LABEL_MAPS, question.label_map_version, mapping)
    identity = replace(
        sf.STUB, label_map_version=question.label_map_version, label_token_map="test-tokens-v1"
    )
    texts = ("First explicit context.", "Second explicit context.")
    inputs = tuple(
        sensing.InputSlot(slot, f"{slot}.md", slot, "test-extractor-v1", sensing.text_sha256(text))
        for slot, text in zip(("left", "right"), texts, strict=True)
    )
    request = sensing.SenseRequest(question.question_type, 2, inputs, texts)
    reading = sensing.make_request_reading(
        request,
        identity,
        vectors={"answer": [0.98, 0.01, 0.01]},
        abstain_reason=None,
        sensed_at="2026-01-01T00:00:00Z",
    )
    ledger = sensing_ledger.Ledger(tmp_path)
    conn = ledger.connect()
    ledger.append(conn, [reading])
    [stored] = sensing_ledger.by_input_key(conn, question.question_type, request.input_key)
    conn.close()
    assert stored.vectors == {"answer": [0.98, 0.01, 0.01]}
    current = sensing_ledger.lookup_current(
        tmp_path, request, {identity.instrument_id: identity}, question.label_map_version
    )
    assert current.verdict == sensing.Verdict("same", 0.98)
