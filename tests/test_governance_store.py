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
from exomem.governance import policy, store, tokens


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


def test_v3_session_grant_rows_are_non_authoritative(vault: Path) -> None:
    conn = store.open_connection(vault)
    try:
        conn.execute(
            "INSERT INTO governance_session_grants "
            "(grant_id, authorization_session, audience, purpose, ceiling, paths, "
            "fingerprints, token_jti, status, created_at, expires_at, "
            "membership_manifest, policy_fingerprint) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "grant-1", "session-1", "external", None, 6, '["Notes/a.md"]',
                '["hash"]', "token-1", "active", 0.0, 4_000_000_000.0,
                '[{"path":"Notes/a.md","scope_ids":["scope-a"]}]', "policy",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    active, identity = store.active_session_grants(
        vault,
        audience="external",
        authorization_session="session-1",
        rel_path="Notes/a.md",
        purpose=None,
    )

    assert active == []
    assert identity == "v3-session-grants-unscoped"


def test_existing_v3_sidecar_gets_purpose_staging_table_idempotently(vault: Path) -> None:
    conn = store.open_connection(vault)
    conn.execute("DROP TABLE governance_session_purpose_staging")
    conn.commit()
    conn.close()

    repaired = store.open_connection(vault)
    try:
        tables = {
            row[0]
            for row in repaired.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        repaired.close()
    assert "governance_session_purpose_staging" in tables


def test_open_connection_preserves_a_newer_sidecar_version(vault: Path) -> None:
    first = store.open_connection(vault)
    first.execute("PRAGMA user_version = 4")
    first.commit()
    first.close()

    second = store.open_connection(vault)
    try:
        assert second.execute("PRAGMA user_version").fetchone()[0] == 4
    finally:
        second.close()


def test_open_connection_migrates_v1_through_governance_schema_v3(vault: Path) -> None:
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
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == 3
        assert migrated.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='receipts_head'"
        ).fetchone()
        columns = {row[1] for row in migrated.execute("PRAGMA table_info(receipts_head)")}
        assert {"path", "byte_offset"} <= columns
        assert migrated.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='receipt_anchor'"
        ).fetchone() is None
    finally:
        migrated.close()


def _prepare_future_sidecar(vault: Path, version: int) -> Path:
    token_conn = tokens._open(vault)
    token_conn.close()
    path = store.sidecar_path(vault)
    conn = sqlite3.connect(path)
    try:
        conn.execute("DROP TABLE receipts_head")
        conn.execute(
            "CREATE TABLE receipts_head ("
            "instance_id TEXT PRIMARY KEY, durable_seq INTEGER NOT NULL, durable_hash TEXT NOT NULL, "
            "observed_seq INTEGER NOT NULL, observed_hash TEXT NOT NULL)"
        )
        conn.execute("DROP TABLE IF EXISTS receipt_anchor")
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return path


def _sidecar_snapshot(path: Path) -> tuple[bytes, tuple[tuple[object, ...], ...], int]:
    conn = sqlite3.connect(path)
    try:
        schema = tuple(
            conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
            )
        )
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()
    return path.read_bytes(), schema, version


@pytest.mark.parametrize("version", [4, 5])
@pytest.mark.parametrize("opener", ["receipt", "token", "policy"])
def test_older_openers_leave_future_schema_without_v2_locator_state_byte_identical(
    vault: Path, version: int, opener: str
) -> None:
    path = _prepare_future_sidecar(vault, version)
    before = _sidecar_snapshot(path)

    if opener == "receipt":
        conn = store.open_connection(vault)
        conn.close()
    elif opener == "token":
        conn = tokens._open(vault)
        conn.close()
    else:
        assert governance_compile.read_snapshot(vault, "missing") is None

    assert _sidecar_snapshot(path) == before


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
