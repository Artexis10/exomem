"""The unit vector lane must answer exactly as the per-query full scan did.

`EmbeddingIndex.search_semantic_units` used to read, sort and stack every unit
vector on each query. It now ranks a resident matrix (local profile) or a
streamed one (`service-v1`) through a bounded window. These tests pin the
answer to the old algorithm, inlined below, and pin the cache to the sidecar's
unit generation.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import numpy as np
import pytest

from exomem import embedding_index, semantic_index
from exomem import find as find_module

PARENTS = 30
UNITS_PER_PARENT = 9
#: Parents edited after indexing, so validation rejects every unit they hold.
#: Enough that even a filtered slice holds more stale rows than the first
#: validation window (33 rows at k=1), so the walk has to widen.
STALE = 14


def _write_page(root: Path, number: int, *, body_suffix: str = "") -> Path:
    path = root / "Knowledge Base" / "Notes" / f"page-{number:02d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    units = "\n".join(
        f"- [finding] page {number} finding {unit}{body_suffix} ^u{unit}"
        for unit in range(UNITS_PER_PARENT)
    )
    path.write_text(
        "---\n"
        "type: insight\n"
        f"title: Page {number}\n"
        f"exomem_id: {uuid.uuid5(uuid.NAMESPACE_URL, f'unit-scan:{number}')}\n"
        "updated: 2026-10-10\n"
        "---\n\n"
        f"# Page {number}\n\n{units}\n",
        encoding="utf-8",
    )
    find_module.clear_cache()
    return path


def _full_scan(
    index: embedding_index.EmbeddingIndex,
    query: np.ndarray,
    k: int,
    *,
    allowed_unit_refs: set[str] | None,
    allowed_parent_paths: set[str] | None,
    validate: bool,
) -> list[embedding_index.SemanticUnitVectorHit]:
    """The pre-change algorithm: read every admitted row, sort all, then validate."""
    clauses, params = [], []
    if allowed_unit_refs is not None:
        clauses.append("unit_ref IN (SELECT value FROM json_each(?))")
        params.append(json.dumps(sorted(allowed_unit_refs)))
    if allowed_parent_paths is not None:
        clauses.append("parent_path IN (SELECT value FROM json_each(?))")
        params.append(json.dumps(sorted(allowed_parent_paths)))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = sqlite3.connect(index.path)
    try:
        rows = conn.execute(
            "SELECT unit_ref, parent_path, parent_generation, parent_source_hash, "
            f"parser_version, vector FROM semantic_unit_vectors{where}",
            params,
        ).fetchall()
    finally:
        conn.close()
    candidates = []
    for ref, path, generation, source_hash, version, blob in rows:
        vector = np.frombuffer(blob, dtype=np.float32)
        if vector.shape == (index.dim,):
            candidates.append((ref, path, generation, source_hash, version, vector))
    if not candidates:
        return []
    scores = np.stack([candidate[5] for candidate in candidates]) @ query
    order = sorted(
        range(len(candidates)), key=lambda n: (-float(scores[n]), candidates[n][0])
    )
    filtered = allowed_unit_refs is not None or allowed_parent_paths is not None
    if not validate:
        limit = k
    elif filtered:
        limit = len(order)
    else:
        limit = max(k * 4, k + 32)
    hits = []
    for n in order[:limit]:
        ref, path, generation, source_hash, version, _vector = candidates[n]
        if validate and not semantic_index.validate_parent_record(
            index.vault_root,
            parent_path=path,
            parent_generation_value=generation,
            parent_source_hash=source_hash,
            parser_version=version,
        ).current:
            continue
        hits.append(
            embedding_index.SemanticUnitVectorHit(
                ref, path, generation, source_hash, version, float(scores[n])
            )
        )
        if len(hits) == k:
            break
    return hits


@pytest.mark.parametrize("profile", ["resident", "service-v1"])
def test_unit_vector_search_returns_what_the_full_scan_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    dim = embedding_index.VECTOR_DIM
    rng = np.random.default_rng(1688)
    index = embedding_index.EmbeddingIndex(tmp_path)
    paths: list[str] = []
    refs: list[str] = []
    for number in range(PARENTS):
        state = semantic_index.build_parent_index_state(tmp_path, _write_page(tmp_path, number))
        # Small integers on eight axes: every float32 dot product is exact under
        # any BLAS blocking, so scores compare with ==, and many tie.
        vectors = np.zeros((UNITS_PER_PARENT, dim), dtype=np.float32)
        vectors[:, :8] = rng.integers(-2, 3, size=(UNITS_PER_PARENT, 8))
        if number < STALE:
            vectors[:, 8] = 25  # above the 8-axis range of 24: stale rows rank first
        index.upsert_semantic_units(state, vectors, 1.0)
        paths.append(state.path)
        refs.extend(unit.unit_ref for unit in state.document.units)
    for number in range(STALE):
        _write_page(tmp_path, number, body_suffix=" (edited)")
    conn = sqlite3.connect(index.path)
    with conn:
        # A row of another width, which the scan has always skipped.
        conn.execute(
            "INSERT INTO semantic_unit_vectors SELECT 'narrow', record_type, 'narrow', "
            "parent_path, parent_ref, parent_generation, parent_source_hash, parser_version, "
            "form, category, kind, content, unit_source_hash, 99, ?, file_mtime "
            "FROM semantic_unit_vectors LIMIT 1",
            (np.ones(4, dtype=np.float32).tobytes(),),
        )
    conn.close()
    if profile == "service-v1":
        monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
        monkeypatch.setenv("EXOMEM_CLOUD_RESOURCE_POLICY", "service-v1")
        monkeypatch.setattr(embedding_index.EmbeddingIndex, "DISK_BLOCK_ROWS", 7)
    query = np.zeros(dim, dtype=np.float32)
    query[:9] = [1, -1, 1, 0, 1, -1, 0, 1, 1]
    some_parents = set(paths[::2]) | {"Knowledge Base/Notes/absent.md"}
    some_refs = set(refs[::3])

    compared = 0
    for allowed_refs, allowed_parents in [
        (None, None),
        (None, some_parents),
        (some_refs, None),
        (some_refs, some_parents),
    ]:
        for k in (1, 5, 40, 500):
            for validate in (False, True):
                expected = _full_scan(
                    index,
                    query,
                    k,
                    allowed_unit_refs=allowed_refs,
                    allowed_parent_paths=allowed_parents,
                    validate=validate,
                )
                actual = index.search_semantic_units(
                    query,
                    k,
                    allowed_unit_refs=allowed_refs,
                    allowed_parent_paths=allowed_parents,
                    validate=validate,
                )
                case = (allowed_refs is not None, allowed_parents is not None, k, validate)
                assert actual == expected, case
                compared += bool(expected)
    # Guard against a fixture where every case is trivially empty.
    assert compared >= 24


def test_a_unit_write_after_a_warm_query_changes_the_next_answer(tmp_path: Path) -> None:
    dim = embedding_index.VECTOR_DIM
    index = embedding_index.EmbeddingIndex(tmp_path)
    first, second = (
        semantic_index.build_parent_index_state(tmp_path, _write_page(tmp_path, number))
        for number in (1, 2)
    )
    query = np.zeros(dim, dtype=np.float32)
    query[0] = 1.0

    def vectors(score: float) -> np.ndarray:
        out = np.zeros((UNITS_PER_PARENT, dim), dtype=np.float32)
        out[:, 0] = score
        return out

    def best_parent() -> list[str]:
        return [hit.parent_path for hit in index.search_semantic_units(query, 1, validate=False)]

    index.upsert_semantic_units(first, vectors(0.9), 1.0)
    index.upsert_semantic_units(second, vectors(0.5), 1.0)
    assert best_parent() == [first.path]

    index.upsert_semantic_units(second, vectors(1.0), 2.0)
    assert best_parent() == [second.path]

    index.delete_semantic_units(second.path)
    assert best_parent() == [first.path]


def test_the_idle_reaper_can_release_the_resident_unit_matrix(tmp_path: Path) -> None:
    index = embedding_index.EmbeddingIndex(tmp_path)
    state = semantic_index.build_parent_index_state(tmp_path, _write_page(tmp_path, 1))
    index.upsert_semantic_units(
        state, np.ones((UNITS_PER_PARENT, embedding_index.VECTOR_DIM), dtype=np.float32), 1.0
    )
    index.search_semantic_units(
        np.ones(embedding_index.VECTOR_DIM, dtype=np.float32), 1, validate=False
    )

    assert index.cache_status()["unit_rows"] == UNITS_PER_PARENT
    assert index.unload_cache() is True
    assert index.cache_status()["loaded"] is False
