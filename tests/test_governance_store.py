"""Governance sidecar lifecycle + compiled-policy snapshot roundtrip.

`.governance.sqlite` is a derived, per-machine convenience (design D1/D6) —
never the enforcement authority, never consulted by the live `policy.load`/
`membership.evaluate`/`decisions.decide` path. Only an explicit
`compile.write_snapshot` call touches it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from exomem.governance import compile as governance_compile
from exomem.governance import policy, store


def test_open_connection_creates_sidecar_with_pragmas_and_meta(vault: Path) -> None:
    sidecar = store.sidecar_path(vault)
    assert not sidecar.exists()

    conn = store.open_connection(vault)
    try:
        assert sidecar.exists()
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(journal_mode).lower() == "wal"
        meta_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        assert meta_exists is not None
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert user_version == store.SCHEMA_USER_VERSION
        data_table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (store.DATA_TABLE,),
        ).fetchone()
        assert data_table_exists is not None
    finally:
        conn.close()


def test_open_connection_is_idempotent(vault: Path) -> None:
    first = store.open_connection(vault)
    first.close()
    second = store.open_connection(vault)
    try:
        # No error re-creating tables/pragmas against an existing sidecar.
        assert second.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_USER_VERSION
    finally:
        second.close()


def test_open_connection_preserves_a_newer_sidecar_version(vault: Path) -> None:
    first = store.open_connection(vault)
    first.execute("PRAGMA user_version = 3")
    first.commit()
    first.close()

    second = store.open_connection(vault)
    try:
        assert second.execute("PRAGMA user_version").fetchone()[0] == 3
    finally:
        second.close()


def test_open_connection_migrates_v1_to_receipt_schema_v2(vault: Path) -> None:
    path = store.sidecar_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE compiled_policy "
            "(fingerprint TEXT PRIMARY KEY, snapshot TEXT NOT NULL, compiled_at REAL NOT NULL)"
        )
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    finally:
        conn.close()

    migrated = store.open_connection(vault)
    try:
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == 2
        assert migrated.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='receipts_head'"
        ).fetchone()
    finally:
        migrated.close()


def _write_scope(vault: Path) -> None:
    p = vault / "Knowledge Base" / "_Governance" / "scopes" / "acmeco.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
        "paths: [\"Projects/AcmeCo/**\"]\n",
        encoding="utf-8",
    )


def test_snapshot_roundtrip_keyed_by_fingerprint(vault: Path) -> None:
    _write_scope(vault)
    pol = policy.load(vault)
    assert not pol.empty

    governance_compile.write_snapshot(vault, pol)
    snapshot = governance_compile.read_snapshot(vault, pol.fingerprint)
    assert snapshot is not None
    assert snapshot["fingerprint"] == pol.fingerprint
    assert "01ARZ3NDEKTSV4RRFFQ69G5FAV" in snapshot["scopes"]
    assert snapshot["scopes"]["01ARZ3NDEKTSV4RRFFQ69G5FAV"]["paths"] == ["Projects/AcmeCo/**"]

    # A different fingerprint was never written under this key.
    assert governance_compile.read_snapshot(vault, "not-a-real-fingerprint") is None


def test_snapshot_write_replaces_prior_row_for_same_fingerprint(vault: Path) -> None:
    _write_scope(vault)
    pol = policy.load(vault)
    governance_compile.write_snapshot(vault, pol)
    governance_compile.write_snapshot(vault, pol)  # idempotent re-write, no duplicate row

    conn = sqlite3.connect(store.sidecar_path(vault))
    try:
        count = conn.execute(
            f"SELECT COUNT(*) FROM {store.DATA_TABLE} WHERE fingerprint = ?",
            (pol.fingerprint,),
        ).fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_load_and_evaluate_never_touch_the_sidecar(vault: Path) -> None:
    """The live read path (load/membership/decide) is pure in-process YAML
    parsing — the sidecar only exists behind an explicit compile call."""
    _write_scope(vault)
    policy.load(vault)
    assert not store.sidecar_path(vault).exists()


def test_write_snapshot_refuses_empty_policy(vault: Path) -> None:
    """`EMPTY_POLICY` has nothing to inspect — persisting it under the
    sentinel "missing" fingerprint would be misleading, not a real compile."""
    with pytest.raises(ValueError):
        governance_compile.write_snapshot(vault, policy.EMPTY_POLICY)
    assert not store.sidecar_path(vault).exists()


def test_write_snapshot_refuses_blocked_policy(vault: Path) -> None:
    """A cold-start `.blocked` refusal is a floor, not a real compile — it
    must never be persisted as though it were a genuine snapshot."""
    conflict = vault / "Knowledge Base" / "_Governance" / "scopes" / "acmeco.yaml"
    conflict.parent.mkdir(parents=True, exist_ok=True)
    conflict.write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\nceiling: 9\n",
        encoding="utf-8",
    )
    (
        vault
        / "Knowledge Base"
        / "_Governance"
        / "scopes"
        / "acmeco (conflicted copy).yaml"
    ).write_text("governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n", encoding="utf-8")

    pol = policy.load(vault)
    assert pol.blocked is True

    with pytest.raises(ValueError):
        governance_compile.write_snapshot(vault, pol)
    assert not store.sidecar_path(vault).exists()
