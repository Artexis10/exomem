from __future__ import annotations

import sqlite3
from pathlib import Path

from exomem import freshness, lexstore


def _manifest() -> str:
    return """---
type: collection
exomem_id: 12345678-1234-4abc-8def-123456789abc
title: Measurements
semantic_profile: records
collection_version: 1
lifecycle: active
schema_version: 1
storage:
  strategy: markdown-items
  format_version: 1
  source: items
item_schema:
  natural_key: [observed]
  fields:
    observed:
      type: string
---
manifest needle
"""


def test_fts_persists_projected_checkpoint_and_never_stores_raw_records(tmp_path: Path) -> None:
    records = tmp_path / "Knowledge Base" / "Records" / "Health"
    records.mkdir(parents=True)
    (records / "items").mkdir()
    (records / "_collection.md").write_text(_manifest(), encoding="utf-8")
    (records / "items" / "raw.md").write_text("raw needle", encoding="utf-8")
    (records / "_summary.md").write_text("summary needle", encoding="utf-8")

    hits = lexstore.search_bm25(tmp_path, "needle", 10)

    assert hits == [("Knowledge Base/Records/Health/_collection.md", hits[0][1])]
    with sqlite3.connect(lexstore.lexical_path(tmp_path)) as conn:
        assert conn.execute(
            "SELECT value FROM meta WHERE key = 'recall_checkpoint:kb'"
        ).fetchone()
        assert conn.execute(
            "SELECT path FROM pages WHERE path LIKE '%raw.md' OR path LIKE '%_summary.md'"
        ).fetchall() == []


def test_legacy_raw_rows_are_purged_once_by_projected_repair(tmp_path: Path) -> None:
    records = tmp_path / "Knowledge Base" / "Records" / "Health"
    records.mkdir(parents=True)
    (records / "items").mkdir()
    (records / "_collection.md").write_text(_manifest(), encoding="utf-8")
    raw = records / "items" / "raw.md"
    raw.write_text("raw needle", encoding="utf-8")
    assert lexstore.search_bm25(tmp_path, "needle", 10)

    # Simulate a pre-projection sidecar: an old FTS row is present even though
    # the new source policy excludes it.  The first explicit repair removes it;
    # a converged second repair is content/metadata-idempotent.
    store = lexstore.get_store(tmp_path)
    with sqlite3.connect(store.path) as conn:
        with conn:
            rowid = conn.execute(
                "INSERT INTO pages(path, mtime_ns, updated, in_kb, in_vault, is_nav) "
                "VALUES(?, 1, '', 1, 1, 0)",
                ("Knowledge Base/Records/Health/items/raw.md",),
            ).lastrowid
            conn.execute("INSERT INTO fts(rowid, stemmed) VALUES(?, 'needle raw')", (rowid,))
    store.ensure_fresh()
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT path FROM pages WHERE rowid = ?", (rowid,)).fetchone() is None
        first = conn.execute(
            "SELECT value FROM meta WHERE key = 'recall_checkpoint:kb'"
        ).fetchone()[0]
    store.ensure_fresh()
    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT value FROM meta WHERE key = 'recall_checkpoint:kb'"
        ).fetchone()[0] == first


def test_foreign_projected_checkpoint_with_same_state_is_ready(tmp_path: Path) -> None:
    page = tmp_path / "Knowledge Base" / "Notes" / "page.md"
    page.parent.mkdir(parents=True)
    page.write_text("needle", encoding="utf-8")
    assert lexstore.search_bm25(tmp_path, "needle", 10)
    store = lexstore.get_store(tmp_path)
    current = store.catalog_checkpoint("kb")
    foreign = freshness.RecallFreshnessCheckpoint(
        "foreign", 1, current.triple, current.policy_version, current.access_policy_fingerprint
    )
    with sqlite3.connect(store.path) as conn:
        with conn:
            conn.execute(
                "UPDATE meta SET value = ? WHERE key = 'recall_checkpoint:kb'", (repr(tuple(foreign)),)
            )

    assert store.catalog_readiness("kb", None).complete


def test_missing_projected_metadata_fails_closed_then_repair_converges(tmp_path: Path) -> None:
    page = tmp_path / "Knowledge Base" / "Notes" / "page.md"
    page.parent.mkdir(parents=True)
    page.write_text("needle", encoding="utf-8")
    assert lexstore.search_bm25(tmp_path, "needle", 10)
    store = lexstore.get_store(tmp_path)
    with sqlite3.connect(store.path) as conn:
        with conn:
            conn.execute("DELETE FROM meta WHERE key = 'recall_checkpoint:kb'")
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('checkpoint:kb', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (repr(("legacy", 99, (1, 1, "legacy"))),),
            )

    assert not store.catalog_readiness("kb", None).complete
    store.ensure_fresh()
    assert store.catalog_readiness("kb", None).complete
