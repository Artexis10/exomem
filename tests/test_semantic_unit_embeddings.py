"""Optional semantic-unit vectors remain parent-owned and soft-failing."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np

from exomem import embedding_index, embeddings, readiness, semantic_index
from exomem import find as find_module

_PAGE_ID = "22222222-2222-4222-8222-222222222222"


def _write_page(root: Path, body: str) -> Path:
    path = root / "Knowledge Base" / "Notes" / "vectors.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "type: insight\n"
        "title: Unit vector fixture\n"
        f"exomem_id: {_PAGE_ID}\n"
        "updated: 2026-07-15\n"
        "---\n"
        "# Unit vector fixture\n\n"
        f"{body.rstrip()}\n",
        encoding="utf-8",
    )
    find_module.clear_cache()
    return path


def _vectors(count: int, start: float = 0.0) -> np.ndarray:
    return np.asarray(
        [[start + float(i)] * embedding_index.VECTOR_DIM for i in range(count)],
        dtype=np.float32,
    )


def _unit_rows(root: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(embedding_index.index_paths.sidecar_path(root))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM semantic_unit_vectors ORDER BY source_order"
        ).fetchall()
    finally:
        conn.close()


def test_unit_vectors_store_keys_generation_and_parent_linkage(tmp_path: Path) -> None:
    page = _write_page(
        tmp_path,
        "- [config] first vector ^first\n- [rule] second vector ^second\n",
    )
    state = semantic_index.build_parent_index_state(tmp_path, page)
    index = embedding_index.EmbeddingIndex(tmp_path)

    index.upsert_semantic_units(state, _vectors(2), page.stat().st_mtime)

    rows = _unit_rows(tmp_path)
    assert [row["unit_key"] for row in rows] == [
        unit.unit_ref for unit in state.document.units
    ]
    assert {row["record_type"] for row in rows} == {"semantic_unit"}
    assert {row["parent_path"] for row in rows} == {state.path}
    assert {row["parent_ref"] for row in rows} == {state.parent_ref}
    assert {row["parent_generation"] for row in rows} == {
        state.parent_generation
    }
    assert {row["parent_source_hash"] for row in rows} == {
        state.parent_source_hash
    }
    assert {row["parser_version"] for row in rows} == {state.parser_version}
    assert all(len(row["vector"]) == embedding_index.VECTOR_DIM * 4 for row in rows)


def test_unit_vector_replacement_and_file_delete_remove_old_rows(tmp_path: Path) -> None:
    page = _write_page(
        tmp_path,
        "- [config] old vector ^old\n- [rule] removed vector ^removed\n",
    )
    index = embedding_index.EmbeddingIndex(tmp_path)
    before = semantic_index.build_parent_index_state(tmp_path, page)
    index.upsert_semantic_units(before, _vectors(2), page.stat().st_mtime)

    page = _write_page(tmp_path, "- [rule] current vector ^current\n")
    current = semantic_index.build_parent_index_state(tmp_path, page)
    index.upsert_semantic_units(current, _vectors(1, 9.0), page.stat().st_mtime)

    rows = _unit_rows(tmp_path)
    assert [row["unit_key"] for row in rows] == [current.document.units[0].unit_ref]
    assert rows[0]["content"] == "current vector"
    assert rows[0]["parent_generation"] != before.parent_generation

    index.delete_file(current.path)
    assert _unit_rows(tmp_path) == []


def test_unit_vector_schema_mismatch_drops_only_derived_unit_rows(
    tmp_path: Path,
) -> None:
    page = _write_page(tmp_path, "- [config] versioned vector ^versioned\n")
    source = page.read_bytes()
    state = semantic_index.build_parent_index_state(tmp_path, page)
    index = embedding_index.EmbeddingIndex(tmp_path)
    index.upsert_semantic_units(state, _vectors(1), page.stat().st_mtime)

    conn = sqlite3.connect(embedding_index.index_paths.sidecar_path(tmp_path))
    try:
        conn.execute(
            "UPDATE meta SET value = 0 WHERE key = 'semantic_unit_schema_version'"
        )
        conn.commit()
    finally:
        conn.close()

    assert index.semantic_unit_parent_states() == {}
    assert page.read_bytes() == source


def test_unit_vector_rebuild_uses_current_markdown_generation(
    tmp_path: Path, monkeypatch
) -> None:
    page = _write_page(tmp_path, "- [config] rebuilt unit ^rebuilt\n")
    monkeypatch.setattr(
        embeddings, "_chunks_for_page", lambda _root, _page: ["page chunk"]
    )

    def _embed(texts, *, is_query):
        assert is_query is False
        return _vectors(len(texts), 3.0)

    monkeypatch.setattr(embeddings, "embed_texts", _embed)
    index = embedding_index.EmbeddingIndex(tmp_path)

    assert index.rebuild_all() == 1

    state = semantic_index.build_parent_index_state(tmp_path, page)
    rows = _unit_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["unit_key"] == state.document.units[0].unit_ref
    assert rows[0]["parent_generation"] == state.parent_generation


def test_hierarchy_migration_rebuilds_current_vector_unit_set_without_source_write(
    tmp_path: Path, monkeypatch
) -> None:
    page = _write_page(
        tmp_path,
        """\
## Finding

Parent conclusion.

### Decision

Nested recognized content.

- [config] Compact-shaped body content.
""",
    )
    source = page.read_bytes()
    current = semantic_index.build_parent_index_state(tmp_path, page)
    assert [(unit.form, unit.kind) for unit in current.document.units] == [
        ("rich", "finding")
    ]
    index = embedding_index.EmbeddingIndex(tmp_path)
    index.upsert_semantic_units(current, _vectors(1), page.stat().st_mtime)

    conn = sqlite3.connect(embedding_index.index_paths.sidecar_path(tmp_path))
    try:
        columns = [
            str(row[1])
            for row in conn.execute("PRAGMA table_info(semantic_unit_vectors)")
        ]
        existing = conn.execute(
            "SELECT * FROM semantic_unit_vectors WHERE parent_path = ?",
            (current.path,),
        ).fetchone()
        assert existing is not None
        old_parent = dict(zip(columns, existing, strict=True))
        old_parent.update(
            {
                "unit_key": "old-parent-ref",
                "unit_ref": "old-parent-ref",
                "parent_generation": "pre-hierarchy",
                "parser_version": semantic_index.PARSER_VERSION - 1,
            }
        )
        old_nested = {
            **old_parent,
            "unit_key": "old-nested-ref",
            "unit_ref": "old-nested-ref",
            "content": "Nested recognized content.",
            "source_order": 1,
        }
        conn.execute(
            "DELETE FROM semantic_unit_vectors WHERE parent_path = ?",
            (current.path,),
        )
        placeholders = ", ".join("?" for _ in columns)
        conn.executemany(
            f"INSERT INTO semantic_unit_vectors({', '.join(columns)}) "
            f"VALUES({placeholders})",
            [
                tuple(old_parent[column] for column in columns),
                tuple(old_nested[column] for column in columns),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(
        embeddings, "_chunks_for_page", lambda _root, _page: ["page chunk"]
    )
    monkeypatch.setattr(
        embeddings,
        "embed_texts",
        lambda texts, *, is_query: _vectors(len(texts), 6.0),
    )

    assert index.rebuild_all() == 1

    rows = _unit_rows(tmp_path)
    assert page.read_bytes() == source
    assert len(rows) == 1
    assert rows[0]["unit_key"] == current.document.units[0].unit_ref
    assert rows[0]["parent_generation"] == current.parent_generation
    assert rows[0]["parser_version"] == semantic_index.PARSER_VERSION


def test_incremental_index_repairs_unit_rows_even_when_page_chunks_are_current(
    tmp_path: Path, monkeypatch
) -> None:
    page = _write_page(tmp_path, "- [config] incremental unit ^incremental\n")
    monkeypatch.setattr(
        embeddings, "_chunks_for_page", lambda _root, _page: ["page chunk"]
    )
    monkeypatch.setattr(
        embeddings,
        "embed_texts",
        lambda texts, *, is_query: _vectors(len(texts), 4.0),
    )

    first = embeddings.index_incremental(tmp_path, log_fn=lambda *_args: None)
    assert first["unit_parents_to_embed"] == 1
    assert len(_unit_rows(tmp_path)) == 1

    index = embeddings.get_embedding_index(tmp_path)
    index.delete_semantic_units("Knowledge Base/Notes/vectors.md")
    assert _unit_rows(tmp_path) == []

    second = embeddings.index_incremental(tmp_path, log_fn=lambda *_args: None)
    assert second["files_to_embed"] == 0
    assert second["unit_parents_to_embed"] == 1
    assert second["unit_vectors_embedded"] == 1
    assert len(_unit_rows(tmp_path)) == 1

    page.unlink()
    third = embeddings.index_incremental(tmp_path, log_fn=lambda *_args: None)
    assert third["files_pruned"] == 1
    assert _unit_rows(tmp_path) == []


def test_disabled_or_warming_embedding_path_never_loads_unit_work(
    tmp_path: Path, monkeypatch
) -> None:
    page = _write_page(tmp_path, "- [config] deferred unit ^deferred\n")

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("disabled/deferred path must not load model or index")

    monkeypatch.setattr(embeddings, "get_model", _forbidden)
    monkeypatch.setattr(embeddings, "get_embedding_index", _forbidden)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    disabled = embeddings.upsert_after_write_status(tmp_path, [page])
    assert (disabled.status, disabled.code) == ("disabled", "embeddings_disabled")

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS")
    monkeypatch.setattr(embeddings, "_IMPORT_FAILED", True)
    unavailable = embeddings.upsert_after_write_status(tmp_path, [page])
    assert (unavailable.status, unavailable.code) == (
        "disabled",
        "embeddings_import_unavailable",
    )

    monkeypatch.setattr(embeddings, "_IMPORT_FAILED", False)
    readiness.reset()
    readiness.begin_warm()
    try:
        deferred = embeddings.upsert_after_write_status(tmp_path, [page])
        assert (deferred.status, deferred.code) == (
            "deferred",
            "deferred_warmup",
        )
    finally:
        readiness.finish_warm()
        readiness.reset()


def test_post_start_unit_encode_failure_degrades_without_raising(
    tmp_path: Path, monkeypatch
) -> None:
    page_path = _write_page(tmp_path, "- [config] soft fallback ^fallback\n")
    page = find_module._CACHE.get(page_path, tmp_path)
    assert page is not None
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "_IMPORT_FAILED", False)
    monkeypatch.setattr(embeddings, "get_model", lambda: object())
    monkeypatch.setattr(readiness, "defer", lambda *_args: False)
    monkeypatch.setattr(find_module._CACHE, "get", lambda *_args: page)
    monkeypatch.setattr(embeddings, "_chunks_for_page", lambda *_args: ["page chunk"])

    calls = 0

    def _embed(chunks):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("unit backend failed after startup")
        return _vectors(len(chunks), 7.0)

    monkeypatch.setattr(embeddings, "_embed_live_chunks", _embed)
    written: list[str] = []

    class _Index:
        def upsert_file(self, rel_path, _chunks, _vectors, _mtime):
            written.append(rel_path)

        def upsert_semantic_units(self, *_args):
            raise AssertionError("failed unit vectors must not be stored")

        def delete_file(self, _rel_path):
            raise AssertionError("existing file must not be deleted")

        def delete_semantic_units(self, _rel_path):
            raise AssertionError("current unit rows must remain for freshness rejection")

    monkeypatch.setattr(embeddings, "get_embedding_index", lambda _root: _Index())

    status = embeddings.upsert_after_write_status(tmp_path, [page_path])

    assert written == [page.rel_path]
    assert (status.status, status.code) == (
        "degraded",
        "semantic_unit_embedding_encode_failed",
    )
