"""D1-T3: the dreamer's disposable SQLite sidecar.

The store is derived state in the machine-local vault state directory: deleting
it costs a reseed and nothing else, because every triage decision lives in the
portable review state under ids that do not depend on the store.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from exomem import dreamer_store, review_state, state_paths

NOW = 1_800_000_000.0
FAMILY = "upkeep_hydration"
KIND = "curation.hydrate"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    (root / "Knowledge Base").mkdir(parents=True)
    dreamer_store.clear_reader_memo()
    yield root
    dreamer_store.clear_reader_memo()


def _evidence(*names: str) -> list[dict]:
    return [
        {
            "path": f"Knowledge Base/Notes/{name}.md",
            "ref": f"exomem://vault/Knowledge%20Base/Notes/{name}.md",
            "sig": "1:2:3",
            "role": "contributor",
            "origin": f"source:{name}",
        }
        for name in names
    ]


def _propose(store, conn, *, subject="Acme", evidence=("one", "two"), producer="dreamer", **kw):
    with store.write(conn):
        return store.upsert_proposal(
            conn,
            family=kw.pop("family", FAMILY),
            kind=kw.pop("kind", KIND),
            subject_path=f"Knowledge Base/Notes/Entities/{subject}.md",
            subject_ref=f"exomem://vault/Knowledge%20Base/Notes/Entities/{subject}.md",
            proposal_key=kw.pop("proposal_key", ""),
            evidence=_evidence(*evidence),
            route={"tool": "maintain_memory", "args": {"mode": "curation"}},
            reason_code="newer_linked_facts",
            producer=producer,
            signal_version=kw.pop("signal_version", "v1"),
            now=kw.pop("now", NOW),
            **kw,
        )


def test_schema_mismatch_wipes(vault: Path) -> None:
    store = dreamer_store.DreamerStore(vault)
    conn = store.connect()
    cid = _propose(store, conn)
    with store.write(conn):
        store.seen_set(conn, "Knowledge Base/Notes/one.md", (1, 2, 3))
    conn.execute("UPDATE meta SET value = '0' WHERE key = 'schema'")
    conn.commit()
    conn.close()

    conn = dreamer_store.DreamerStore(vault).connect()
    assert conn.execute("SELECT count(*) FROM candidates").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM seen").fetchone()[0] == 0
    schema = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()[0]
    assert schema == str(dreamer_store.SCHEMA_VERSION)
    conn.close()
    assert cid

    # A file that is not a database at all is wiped the same way.
    store.path.write_bytes(b"not a database")
    conn = dreamer_store.DreamerStore(vault).connect()
    assert conn.execute("SELECT count(*) FROM candidates").fetchone()[0] == 0
    conn.close()


def test_every_write_bumps_the_generation(vault: Path) -> None:
    store = dreamer_store.DreamerStore(vault)
    conn = store.connect()
    start = store.generation(conn)
    with store.write(conn):
        store.seen_set(conn, "Knowledge Base/Notes/one.md", (1, 2, 3))
    assert store.generation(conn) == start + 1
    _propose(store, conn)
    assert store.generation(conn) == start + 2
    with pytest.raises(RuntimeError):
        with store.write(conn):
            store.seen_set(conn, "Knowledge Base/Notes/two.md", (1, 2, 3))
            raise RuntimeError("abort")
    # A rolled-back transaction neither writes nor bumps.
    assert store.generation(conn) == start + 2
    assert store.seen_get(conn, "Knowledge Base/Notes/two.md") is None
    conn.close()


def test_caps_evict_the_weakest_open_candidate(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dreamer_store, "MAX_OPEN_PER_FAMILY", 3)
    store = dreamer_store.DreamerStore(vault)
    conn = store.connect()
    strong = [
        _propose(store, conn, subject=f"Strong {i}", evidence=("a", "b", "c", "d"))
        for i in range(3)
    ]
    weak = _propose(store, conn, subject="Weak", evidence=("a", "b"))
    ids = {row[0] for row in conn.execute("SELECT id FROM candidates WHERE state = 'open'")}
    assert ids == set(strong)
    assert weak is None
    # It comes back when its evidence grows past the weakest resident.
    grown = _propose(store, conn, subject="Weak", evidence=("a", "b", "c", "d", "e"))
    ids = {row[0] for row in conn.execute("SELECT id FROM candidates WHERE state = 'open'")}
    assert grown in ids
    assert len(ids) == 3
    # The evicted row left no reverse-index rows behind.
    orphans = conn.execute(
        "SELECT count(*) FROM candidate_paths WHERE id NOT IN (SELECT id FROM candidates)"
    ).fetchone()[0]
    assert orphans == 0
    conn.close()


def test_size_cap_disables_only_global_families(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = dreamer_store.DreamerStore(vault)
    conn = store.connect()
    _propose(store, conn)
    assert store.family_enabled(conn, "upkeep_alias") is True
    monkeypatch.setattr(dreamer_store, "SIZE_CAP_BYTES", 1)
    assert store.capacity_exceeded() is True
    assert store.family_enabled(conn, "upkeep_alias") is False
    assert store.family_enabled(conn, "upkeep_convention") is False
    assert store.family_enabled(conn, "upkeep_link") is True
    assert store.family_enabled(conn, "upkeep_hydration") is True
    refused = _propose(
        store, conn, family="upkeep_alias", kind="alias.add", proposal_key="acme tools"
    )
    assert refused is None
    kept = _propose(store, conn, subject="Other")
    assert kept is not None
    conn.close()


def test_readonly_reader_memoises_on_generation(vault: Path) -> None:
    assert dreamer_store.read_view(vault) is None
    assert not dreamer_store.sidecar_path(vault).exists()

    store = dreamer_store.DreamerStore(vault)
    conn = store.connect()
    first_id = _propose(store, conn)
    view = dreamer_store.read_view(vault)
    assert view is not None
    assert [row["id"] for row in view.candidates] == [first_id]
    assert dreamer_store.read_view(vault) is view
    second_id = _propose(store, conn, subject="Other")
    moved = dreamer_store.read_view(vault)
    assert moved is not view
    assert {row["id"] for row in moved.candidates} == {first_id, second_id}
    # The reader never writes: its connection is query-only.
    with pytest.raises(sqlite3.Error):
        dreamer_store._open_readonly(dreamer_store.sidecar_path(vault)).execute(
            "DELETE FROM candidates"
        )
    conn.close()


def test_same_proposal_from_two_producers_is_one_row(vault: Path) -> None:
    store = dreamer_store.DreamerStore(vault)
    conn = store.connect()
    first = _propose(store, conn, evidence=("one", "two"), producer="dreamer")
    second = _propose(
        store, conn, evidence=("two", "three"), producer="correction", signal_version="c1"
    )
    assert first == second
    assert first == dreamer_store.candidate_id(
        KIND, "Knowledge Base/Notes/Entities/Acme.md", ""
    )
    rows = conn.execute("SELECT evidence_json, evidence_count FROM candidates").fetchall()
    assert len(rows) == 1
    paths = sorted(item["path"] for item in json.loads(rows[0][0]))
    assert paths == [
        "Knowledge Base/Notes/one.md",
        "Knowledge Base/Notes/three.md",
        "Knowledge Base/Notes/two.md",
    ]
    assert rows[0][1] == 3
    # Withdrawing one producer keeps the other's evidence.
    with store.write(conn):
        store.resolve(conn, first, producer="correction", now=NOW)
    state, evidence = conn.execute("SELECT state, evidence_json FROM candidates").fetchone()
    assert state == "open"
    assert sorted(item["path"] for item in json.loads(evidence)) == [
        "Knowledge Base/Notes/one.md",
        "Knowledge Base/Notes/two.md",
    ]
    with store.write(conn):
        store.resolve(conn, first, producer="dreamer", now=NOW)
    assert conn.execute("SELECT state FROM candidates").fetchone()[0] == "resolved"
    conn.close()


def test_ids_survive_a_wipe_so_dismissals_still_apply(vault: Path) -> None:
    store = dreamer_store.DreamerStore(vault)
    conn = store.connect()
    cid = _propose(store, conn)
    fingerprint = conn.execute("SELECT fingerprint FROM candidates").fetchone()[0]
    review_state.ReviewStateStore(vault).apply(cid, fingerprint, action="dismiss", why="handled: x")
    conn.close()
    store.path.unlink()
    for suffix in ("-wal", "-shm"):
        sibling = store.path.with_name(store.path.name + suffix)
        if sibling.exists():
            sibling.unlink()

    conn = dreamer_store.DreamerStore(vault).connect()
    again = _propose(store, conn)
    assert again == cid
    assert conn.execute("SELECT fingerprint FROM candidates").fetchone()[0] == fingerprint
    state, _decision = review_state.ReviewStateStore(vault).effective_state(cid, fingerprint)
    assert state == "dismissed"
    assert store.path.parent == state_paths.vault_state_dir(vault)
    conn.close()


def test_health_never_stores_a_path(vault: Path) -> None:
    store = dreamer_store.DreamerStore(vault)
    conn = store.connect()
    secret = str(vault / "Knowledge Base" / "Notes" / "private.md")
    with store.write(conn):
        store.set_health(
            conn,
            {
                "last_tick_at": NOW,
                "last_error_code": secret,
                "waiting_reason": "Knowledge Base/Notes/private.md",
                "consecutive_failures": 2,
                "reseed_remaining": 5,
                "evidence_complete": {"upkeep_link": True, secret: True},
                "hour_cpu_used": 1.5,
                "path": secret,
                "detail": "Knowledge Base/Notes/private.md",
            },
        )
    raw = conn.execute("SELECT value FROM meta WHERE key = 'health'").fetchone()[0]
    assert "private" not in raw
    assert "/" not in raw and "\\" not in raw
    health = store.health(conn)
    assert health["consecutive_failures"] == 2
    assert health["reseed_remaining"] == 5
    assert health["evidence_complete"] == {"upkeep_link": True}
    assert health["last_error_code"] == "UNKNOWN"
    assert "path" not in health and "detail" not in health
    conn.close()
