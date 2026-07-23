"""Semantic-unit sidecar parity audit and deterministic reconcile repair."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

import numpy as np

from exomem import (
    commands,
    delete_file,
    embedding_index,
    embeddings,
    epistemic_graph,
    lexstore,
    readiness,
    reconcile,
    recover_from_trash,
    semantic_index,
)
from exomem import (
    find as find_module,
)
from exomem import move_file as move_file_module

_PAGE_ID = "55555555-5555-4555-8555-555555555555"
_REL = "Knowledge Base/Sources/reconcile-units.md"


def _write(
    root: Path,
    body: str,
    *,
    rel: str = _REL,
    page_type: str = "source",
    status: str | None = None,
) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    status_line = f"status: {status}\n" if status else ""
    path.write_text(
        "---\n"
        f"type: {page_type}\n"
        "title: Reconcile units\n"
        f"exomem_id: {_PAGE_ID}\n"
        f"{status_line}"
        "---\n"
        "# Reconcile units\n\n"
        f"{body.rstrip()}\n",
        encoding="utf-8",
    )
    find_module.clear_cache()
    return path


def _seed_sidecars(
    root: Path,
    *,
    rel: str = _REL,
    page_type: str = "source",
    status: str | None = None,
) -> semantic_index.SemanticParentIndexState:
    path = _write(
        root,
        "- [config] first observation ^first\n- [rule] second observation ^second\n",
        rel=rel,
        page_type=page_type,
        status=status,
    )
    assert lexstore.search_semantic_units(root, "observation", k=10, scope="kb")
    state = semantic_index.build_parent_index_state(root, path)
    embedding_index.EmbeddingIndex(root).upsert_semantic_units(
        state,
        np.zeros((2, embedding_index.VECTOR_DIM), dtype=np.float32),
        path.stat().st_mtime,
    )
    epistemic_graph.EpistemicGraphIndex(root).rebuild_all()
    return state


def test_hierarchy_parser_and_sidecar_versions_are_incremented() -> None:
    assert semantic_index.PARSER_VERSION == 4
    assert embedding_index.SEMANTIC_UNIT_SCHEMA_VERSION == 3
    assert lexstore.SCHEMA_VERSION == 5
    assert epistemic_graph.SCHEMA_VERSION == 6


def test_explicit_upgrade_reconcile_reprojects_unchanged_rich_units_everywhere(
    tmp_path: Path, monkeypatch
) -> None:
    path = _write(
        tmp_path,
        """\
## Decision
- category: runtime reliability
- id: projection-upgrade
- tags: Reliability, runtime/retry
- context: Edge path

Keep retry windows bounded.
""",
    )
    original_bytes = path.read_bytes()
    original_mtime_ns = path.stat().st_mtime_ns
    assert lexstore.search_semantic_units(
        tmp_path, "retry windows", k=10, scope="kb"
    )
    state = semantic_index.build_parent_index_state(tmp_path, path)
    embedding_index.EmbeddingIndex(tmp_path).upsert_semantic_units(
        state,
        np.zeros((1, embedding_index.VECTOR_DIM), dtype=np.float32),
        path.stat().st_mtime,
    )
    epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()

    conn = sqlite3.connect(lexstore.lexical_path(tmp_path))
    try:
        conn.execute(
            "UPDATE semantic_units SET tags_json = '[]', context = NULL, "
            "parser_version = ?, parent_generation = 'pre-projection' "
            "WHERE parent_path = ?",
            (semantic_index.PARSER_VERSION - 1, _REL),
        )
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(lexstore.SCHEMA_VERSION - 1),),
        )
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(embedding_index.index_paths.sidecar_path(tmp_path))
    try:
        conn.execute(
            "UPDATE semantic_unit_vectors SET parser_version = ?, "
            "parent_generation = 'pre-projection' WHERE parent_path = ?",
            (semantic_index.PARSER_VERSION - 1, _REL),
        )
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(epistemic_graph.sidecar_path(tmp_path))
    try:
        rows = conn.execute(
            "SELECT node_key, metadata FROM graph_nodes WHERE path = ?",
            (_REL,),
        ).fetchall()
        for node_key, raw_metadata in rows:
            metadata = json.loads(raw_metadata)
            if metadata.get("record_type") != "semantic_unit":
                continue
            metadata.update(
                tags=[],
                context=None,
                parser_version=semantic_index.PARSER_VERSION - 1,
                parent_generation="pre-projection",
            )
            conn.execute(
                "UPDATE graph_nodes SET metadata = ? WHERE node_key = ?",
                (json.dumps(metadata, sort_keys=True), node_key),
            )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "_IMPORT_FAILED", False)
    monkeypatch.setattr(embeddings, "get_model", lambda: object())
    monkeypatch.setattr(readiness, "should_defer", lambda *_args: False)
    monkeypatch.setattr(
        embeddings,
        "_embed_live_chunks",
        lambda texts: np.zeros(
            (len(texts), embedding_index.VECTOR_DIM), dtype=np.float32
        ),
    )
    monkeypatch.setattr(
        embeddings,
        "embed_texts",
        lambda texts, *, is_query=False: np.zeros(
            (len(texts), embedding_index.VECTOR_DIM), dtype=np.float32
        ),
    )

    repaired = commands.op_maintain_memory(tmp_path, mode="reconcile")

    rows = lexstore.search_semantic_units(
        tmp_path, "retry windows", k=10, scope="kb"
    )

    assert rows is not None
    assert [(row.tags, row.context) for row in rows] == [
        (("reliability", "runtime/retry"), "Edge path")
    ]
    graph_rows = sqlite3.connect(epistemic_graph.sidecar_path(tmp_path)).execute(
        "SELECT metadata FROM graph_nodes WHERE path = ?",
        (_REL,),
    ).fetchall()
    graph_units = [
        json.loads(raw_metadata)
        for (raw_metadata,) in graph_rows
        if json.loads(raw_metadata).get("record_type") == "semantic_unit"
    ]
    assert [(row["tags"], row["context"]) for row in graph_units] == [
        (["reliability", "runtime/retry"], "Edge path")
    ]
    vector_hits = find_module.find(
        tmp_path,
        query="vector projection probe",
        scope="kb-only",
        mode="vector",
        graph=False,
        result_level="unit",
        limit=10,
    )
    assert [
        (hit.as_dict()["tags"], hit.as_dict()["context"])
        for hit in vector_hits
    ] == [(["reliability", "runtime/retry"], "Edge path")]
    assert vector_hits[0].as_dict()["signals"]["vector_rank"] == 1
    assert repaired["semantic_unit_indexes_status"] == "repaired"
    assert path.read_bytes() == original_bytes
    assert path.stat().st_mtime_ns == original_mtime_ns


def test_hierarchy_reconcile_refreshes_derived_state_without_rewriting_markdown(
    tmp_path: Path, monkeypatch
) -> None:
    path = _write(
        tmp_path,
        """\
## Finding

Parent conclusion.

### Mechanism

- [config] This compact-shaped row belongs to the rich body.
""",
    )
    source = path.read_bytes()
    state = semantic_index.build_parent_index_state(tmp_path, path)
    assert [(unit.form, unit.kind) for unit in state.document.units] == [
        ("rich", "finding")
    ]

    assert lexstore.search_semantic_units(
        tmp_path, "Parent conclusion", k=10, scope="kb"
    )
    epistemic_graph.EpistemicGraphIndex(tmp_path).rebuild_all()

    conn = sqlite3.connect(lexstore.lexical_path(tmp_path))
    try:
        conn.execute(
            "UPDATE semantic_units SET parser_version = ?, "
            "parent_generation = 'pre-hierarchy', unit_ref = 'old-overlap-ref' "
            "WHERE parent_path = ?",
            (semantic_index.PARSER_VERSION - 1, _REL),
        )
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(epistemic_graph.sidecar_path(tmp_path))
    try:
        rows = conn.execute(
            "SELECT node_key, metadata FROM graph_nodes WHERE path = ?", (_REL,)
        ).fetchall()
        for node_key, raw_metadata in rows:
            metadata = json.loads(raw_metadata)
            if metadata.get("record_type") != "semantic_unit":
                continue
            metadata.update(
                {
                    "unit_ref": "old-overlap-ref",
                    "parent_generation": "pre-hierarchy",
                    "parser_version": semantic_index.PARSER_VERSION - 1,
                }
            )
            conn.execute(
                "UPDATE graph_nodes SET metadata = ? WHERE node_key = ?",
                (json.dumps(metadata, sort_keys=True), node_key),
            )
        edge_rows = conn.execute(
            "SELECT edge_key, metadata FROM graph_edges "
            "WHERE source_path = ? AND relation_type = 'derived_from'",
            (_REL,),
        ).fetchall()
        for edge_key, raw_metadata in edge_rows:
            metadata = json.loads(raw_metadata)
            if metadata.get("record_type") != "semantic_unit":
                continue
            metadata["unit_ref"] = "old-overlap-ref"
            conn.execute(
                "UPDATE graph_edges SET metadata = ? WHERE edge_key = ?",
                (json.dumps(metadata, sort_keys=True), edge_key),
            )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    repaired = reconcile.reconcile(tmp_path)
    current = semantic_index.build_parent_index_state(tmp_path, path)

    assert path.read_bytes() == source
    assert repaired.semantic_unit_indexes_status == "repaired"
    assert repaired.semantic_unit_parents_refreshed == 1
    assert semantic_index.audit_semantic_unit_sidecars(
        tmp_path,
        {_REL: current},
        include_vectors=False,
        include_graph=True,
    ) == ()
    assert [unit.unit_ref for unit in current.document.units] == [
        state.document.units[0].unit_ref
    ]


def test_sidecar_parity_classifies_missing_mixed_moved_and_graph_edge_drift(
    tmp_path: Path,
) -> None:
    state = _seed_sidecars(tmp_path)

    lex_path = lexstore.lexical_path(tmp_path)
    conn = sqlite3.connect(lex_path)
    try:
        first = conn.execute(
            "SELECT * FROM semantic_units ORDER BY source_order LIMIT 1"
        ).fetchone()
        columns = [row[1] for row in conn.execute("PRAGMA table_info(semantic_units)")]
        conn.execute(
            "DELETE FROM semantic_units WHERE parent_path = ? AND source_order = 1",
            (_REL,),
        )
        copied = dict(zip(columns, first, strict=True))
        copied["parent_path"] = "Knowledge Base/Sources/old-location.md"
        placeholders = ", ".join("?" for _ in columns)
        conn.execute(
            f"INSERT INTO semantic_units({', '.join(columns)}) VALUES({placeholders})",
            tuple(copied[column] for column in columns),
        )
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(embedding_index.index_paths.sidecar_path(tmp_path))
    try:
        conn.execute(
            "UPDATE semantic_unit_vectors SET parent_generation = 'old-generation' "
            "WHERE parent_path = ? AND source_order = 0",
            (_REL,),
        )
        conn.commit()
    finally:
        conn.close()

    graph_path = epistemic_graph.sidecar_path(tmp_path)
    conn = sqlite3.connect(graph_path)
    try:
        edge = conn.execute(
            "SELECT edge_key, metadata FROM graph_edges "
            "WHERE source_path = ? AND relation_type = 'derived_from' LIMIT 1",
            (_REL,),
        ).fetchone()
        assert edge is not None
        metadata = json.loads(edge[1])
        assert metadata["record_type"] == "semantic_unit"
        conn.execute("DELETE FROM graph_edges WHERE edge_key = ?", (edge[0],))
        conn.commit()
    finally:
        conn.close()

    drift = semantic_index.audit_semantic_unit_sidecars(
        tmp_path,
        {_REL: state},
        include_vectors=True,
        include_graph=True,
    )
    by_key = {(item.sidecar, item.parent_path): set(item.reasons) for item in drift}

    assert "unit_set_mismatch" in by_key[("lexical", _REL)]
    assert {"orphaned", "moved"} <= by_key[
        ("lexical", "Knowledge Base/Sources/old-location.md")
    ]
    assert {"stale", "mixed_generation"} <= by_key[("vector", _REL)]
    assert "missing_derived_edge" in by_key[("graph", _REL)]
    assert "mixed_generation" in by_key[("cross_sidecar", _REL)]


def test_reconcile_repairs_unit_drift_without_markdown_changes_and_is_idempotent(
    tmp_path: Path, monkeypatch
) -> None:
    state = _seed_sidecars(tmp_path)
    source = (tmp_path / _REL).read_bytes()
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")

    conn = sqlite3.connect(lexstore.lexical_path(tmp_path))
    try:
        rowids = conn.execute(
            "SELECT rowid FROM semantic_units WHERE parent_path = ?", (_REL,)
        ).fetchall()
        conn.executemany("DELETE FROM unit_fts WHERE rowid = ?", rowids)
        conn.execute("DELETE FROM semantic_units WHERE parent_path = ?", (_REL,))
        conn.commit()
    finally:
        conn.close()
    conn = sqlite3.connect(epistemic_graph.sidecar_path(tmp_path))
    try:
        rows = conn.execute(
            "SELECT node_key, metadata FROM graph_nodes WHERE path = ?", (_REL,)
        ).fetchall()
        for node_key, raw_metadata in rows:
            metadata = json.loads(raw_metadata)
            if metadata.get("record_type") == "semantic_unit":
                metadata["parent_generation"] = "stale-generation"
                conn.execute(
                    "UPDATE graph_nodes SET metadata = ? WHERE node_key = ?",
                    (json.dumps(metadata, sort_keys=True), node_key),
                )
        conn.commit()
    finally:
        conn.close()

    repaired = reconcile.reconcile(tmp_path)

    assert (tmp_path / _REL).read_bytes() == source
    assert repaired.semantic_unit_indexes_status == "repaired", json.dumps(
        repaired.semantic_unit_index_remaining, sort_keys=True
    )
    assert repaired.semantic_unit_parents_refreshed == 1
    assert repaired.semantic_unit_index_drift
    assert semantic_index.audit_semantic_unit_sidecars(
        tmp_path,
        {_REL: state},
        include_vectors=False,
        include_graph=True,
    ) == ()

    repeated = reconcile.reconcile(tmp_path)
    assert repeated.semantic_unit_indexes_status == "current"
    assert repeated.semantic_unit_parents_refreshed == 0
    assert repeated.semantic_unit_index_drift == []


def test_move_trash_and_recovery_keep_all_unit_sidecars_on_the_live_parent(
    tmp_path: Path, monkeypatch
) -> None:
    live_rel = "Knowledge Base/Notes/Insights/reconcile-units.md"
    state = _seed_sidecars(
        tmp_path,
        rel=live_rel,
        page_type="insight",
        status="draft",
    )
    moved_rel = "Knowledge Base/Notes/Insights/moved-reconcile-units.md"
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "_IMPORT_FAILED", False)
    monkeypatch.setattr(embeddings, "get_model", lambda: object())
    monkeypatch.setattr(readiness, "defer", lambda *_args: False)
    monkeypatch.setattr(
        embeddings, "_chunks_for_page", lambda _root, _page: ["page chunk"]
    )
    monkeypatch.setattr(
        embeddings,
        "_embed_live_chunks",
        lambda texts: np.zeros(
            (len(texts), embedding_index.VECTOR_DIM), dtype=np.float32
        ),
    )

    move_file_module.move_file(tmp_path, old_path=live_rel, new_path=moved_rel)

    moved = semantic_index.build_parent_index_state(tmp_path, tmp_path / moved_rel)
    assert moved.parent_generation == state.parent_generation
    for sidecar in ("lexical", "vector", "graph"):
        assert not any(
            item.sidecar == sidecar
            for item in semantic_index.audit_semantic_unit_sidecars(
                tmp_path,
                {moved_rel: moved},
                include_vectors=True,
                include_graph=True,
            )
        )

    trashed = delete_file.delete_file(
        tmp_path,
        path=moved_rel,
        confirm=True,
        now=dt.datetime(2026, 7, 15, 12, 0, 0),
    )
    assert semantic_index.audit_semantic_unit_sidecars(
        tmp_path,
        {},
        include_vectors=True,
        include_graph=True,
    ) == ()

    recovered = recover_from_trash.recover_from_trash(
        tmp_path,
        trash_path=trashed.trash_path,
        restore_path=moved_rel,
        today=dt.date(2026, 7, 15),
    )
    assert recovered.restored_path == moved_rel
    restored = semantic_index.build_parent_index_state(tmp_path, tmp_path / moved_rel)
    assert semantic_index.audit_semantic_unit_sidecars(
        tmp_path,
        {moved_rel: restored},
        include_vectors=True,
        include_graph=True,
    ) == ()
