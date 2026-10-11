"""Both resource profiles preserve source-derived selected winners.

The reference scan maps neutral keys to identities from the fixture's authored
source before ranking. Stored rows cannot supply the expected public meaning.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import numpy as np
import pytest
from test_connector_boundary import configured_boundary as configured_boundary
from test_private_vocabulary_instances import private_instances as private_instances

from exomem import (
    embedding_index,
    embeddings,
    readiness,
    recall_space,
    semantic_index,
    semantic_units,
    sidecar_store,
)
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
    source_units: dict[tuple[str, str], str],
) -> list[embedding_index.SemanticUnitVectorHit]:
    """The pre-change algorithm: read every admitted row, sort all, then validate."""
    clauses, params = [], []
    if allowed_parent_paths is not None:
        clauses.append("parent_path IN (SELECT value FROM json_each(?))")
        params.append(json.dumps(sorted(allowed_parent_paths)))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = sqlite3.connect(index.path)
    try:
        rows = conn.execute(
            "SELECT unit_key, parent_path, parent_generation, parent_source_hash, "
            f"parser_version, vector FROM semantic_unit_vectors{where}",
            params,
        ).fetchall()
    finally:
        conn.close()
    candidates = []
    for key, path, generation, source_hash, version, blob in rows:
        ref = source_units.get((path, key))
        if ref is None or (allowed_unit_refs is not None and ref not in allowed_unit_refs):
            continue
        vector = np.frombuffer(blob, dtype=np.float32)
        if vector.shape == (index.dim,):
            candidates.append((ref, path, generation, source_hash, version, vector))
    if not candidates:
        return []
    scores = np.stack([candidate[5] for candidate in candidates]) @ query
    order = sorted(range(len(candidates)), key=lambda n: (-float(scores[n]), candidates[n][0]))
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
        if (
            validate
            and not semantic_index.validate_parent_record(
                index.vault_root,
                parent_path=path,
                parent_generation_value=generation,
                parent_source_hash=source_hash,
                parser_version=version,
            ).current
        ):
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
    source_units: dict[tuple[str, str], str] = {}
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
        # The authored compact anchors define these identities before indexing.
        for occurrence in state.occurrences:
            ref = f"{state.parent_ref}#{occurrence.anchor}"
            source_units[(state.path, occurrence.key)] = ref
            refs.append(ref)
    for number in range(STALE):
        _write_page(tmp_path, number, body_suffix=" (edited)")
    conn = sqlite3.connect(index.path)
    with conn:
        # A row of another width, which the scan has always skipped.
        conn.execute(
            "INSERT INTO semantic_unit_vectors SELECT 'narrow', record_type, "
            "parent_path, parent_ref, parent_generation, parent_source_hash, parser_version, "
            "form, content, unit_source_hash, 99, ?, file_mtime "
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
                    source_units=source_units,
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


@pytest.mark.parametrize("profile", ["resident", "service-v1"])
def test_selected_winners_and_profiles_ignore_hidden_and_suppressed_transport_rows(
    private_instances,
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
) -> None:
    """A warm k=1 query must fill its slot after suppression, across block ties."""
    from exomem.governance import principal

    monkeypatch.setattr(embedding_index.cloud_cell, "resource_policy", lambda: profile)
    monkeypatch.setattr(embedding_index.EmbeddingIndex, "DISK_BLOCK_ROWS", 3)
    index = embedding_index.EmbeddingIndex(vault)
    query = np.eye(index.dim, dtype=np.float32)[0]
    states = []
    with principal.request_scope(private_instances("full")):
        for number in range(15):
            path = vault / "Knowledge Base" / "Notes" / f"selected-{number}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            body = (
                "## Finding\n- id: container\n\nContainer content.\n"
                "- [decision] Suppressed nested choice. ^suppressed\n"
                "\n### Unrecognized\n\nUnrecognized nested content.\n"
                if number == 0
                else "- [decision] Useful selected choice. ^winner\n"
            )
            path.write_text(
                "---\ntype: insight\n"
                + ("project: private-project\n" if number == 1 else "")
                + f"exomem_id: {uuid.uuid5(uuid.NAMESPACE_URL, f'selected-scan:{number}')}\n---\n\n"
                + f"# Selected {number}\n\n{body}",
                encoding="utf-8",
            )
            state = semantic_index.build_parent_index_state(vault, path)
            vectors = np.zeros((len(state.occurrences), index.dim), dtype=np.float32)
            for n, occurrence in enumerate(state.occurrences):
                vectors[n, 0] = (
                    10
                    if number == 1
                    or (
                        number == 0
                        and (occurrence.form == "compact" or occurrence.kind_raw == "Unrecognized")
                    )
                    else 1
                    if number in (2, 3)
                    else 0
                )
            index.upsert_semantic_units(state, vectors, 1.0)
            states.append(state)
    allowed = {state.path for state in states}
    expected = min(f"{states[n].parent_ref}#winner" for n in (2, 3))
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "get_embedding_index", lambda _root: index)
    monkeypatch.setattr(readiness, "should_defer", lambda _name: False)
    interpretations: dict[str, int] = {}
    interpret = semantic_units.interpret_structural_summary

    def tracked_summary(*args, **kwargs):
        path = kwargs["path"]
        interpretations[path] = interpretations.get(path, 0) + 1
        return interpret(*args, **kwargs)

    def no_markdown(*args, **kwargs):
        raise AssertionError("vector selection reopened Markdown")

    monkeypatch.setattr(semantic_units, "interpret_structural_summary", tracked_summary)
    monkeypatch.setattr(semantic_index, "current_parent_index_state", no_markdown)
    monkeypatch.setattr(semantic_index, "selected_parent_index_state", no_markdown)
    monkeypatch.setattr(semantic_index, "build_parent_index_state", no_markdown)

    def search(refs=None):
        interpretations.clear()
        result = find_module._vector_unit_candidates(
            vault,
            query="selected choice",
            candidate_limit=1,
            allowed_unit_refs=refs,
            allowed_parent_paths=allowed,
            degraded_out=[],
            failed_out=[],
            timings=None,
            query_vector_provider=lambda: (index.identity, query),
        )
        assert interpretations and max(interpretations.values()) == 1
        return result

    load_rows = index._load_unit_rows
    with principal.request_scope(private_instances("limited")):
        first = search()
        assert [hit.unit_ref for hit in first[0]] == [expected]
        assert first[1]["status"] == "participated" and "coverage" not in first[1]
        if profile == "resident":
            monkeypatch.setattr(index, "_load_unit_rows", no_markdown)
        assert search() == first
        assert search({expected}) == first
        assert index.cache_status().get("unit_rows", 0) == (
            0 if profile == "service-v1" else sum(len(state.occurrences) for state in states)
        )
    # A hidden row's corruption and then its absence have identical public profiles.
    with sqlite3.connect(index.path) as conn:
        conn.execute(
            "UPDATE semantic_unit_vectors SET unit_key = 'substituted' WHERE parent_path = ?",
            (states[1].path,),
        )
        sidecar_store.bump_meta(conn, embedding_index.SEMANTIC_UNIT_GENERATION)
    monkeypatch.setattr(index, "_load_unit_rows", load_rows)
    with principal.request_scope(private_instances("limited")):
        assert search() == first
        index.delete_semantic_units(states[1].path)
        (vault / states[1].path).unlink()
        assert search() == first


@pytest.mark.parametrize("profile", ["resident", "service-v1"])
@pytest.mark.parametrize(
    "damage", ["key", "width", "blob", "record", "parser", "format", "missing", "stamp"]
)
def test_unproved_occurrence_coverage_is_visible_for_admitted_parents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    damage: str,
) -> None:
    """Equal row counts, corrupt records and unusable vectors cannot prove coverage."""
    monkeypatch.setattr(embedding_index.cloud_cell, "resource_policy", lambda: profile)
    state = semantic_index.build_parent_index_state(tmp_path, _write_page(tmp_path, 1))
    index = embedding_index.EmbeddingIndex(tmp_path)
    query = np.ones(embedding_index.VECTOR_DIM, dtype=np.float32)
    index.upsert_semantic_units(
        state, np.ones((UNITS_PER_PARENT, len(query)), dtype=np.float32), 1.0
    )
    assert index.search_semantic_units(query, 1, validate=False)
    with sqlite3.connect(index.path) as conn:
        if damage == "key":
            conn.execute(
                "UPDATE semantic_unit_vectors SET unit_key = 'substituted' "
                "WHERE rowid = (SELECT MIN(rowid) FROM semantic_unit_vectors)"
            )
        elif damage in ("width", "blob"):
            conn.execute(
                "UPDATE semantic_unit_vectors SET vector = ? "
                "WHERE rowid = (SELECT MIN(rowid) FROM semantic_unit_vectors)",
                (np.ones(4, dtype=np.float32).tobytes() if damage == "width" else b"bad",),
            )
        elif damage == "record":
            conn.execute(
                "UPDATE meta SET value = '{' WHERE key = ?",
                (embedding_index.COVERAGE_PREFIX + state.path,),
            )
        elif damage == "missing":
            conn.execute(
                "DELETE FROM meta WHERE key = ?", (embedding_index.COVERAGE_PREFIX + state.path,)
            )
        elif damage == "stamp":
            conn.execute(
                "UPDATE semantic_unit_vectors SET parent_source_hash = 'other-source' "
                "WHERE rowid = (SELECT MIN(rowid) FROM semantic_unit_vectors)"
            )
        else:
            field = "$.parser_version" if damage == "parser" else "$.structure.format"
            conn.execute(
                "UPDATE meta SET value = json_set(value, ?, -1) WHERE key = ?",
                (field, embedding_index.COVERAGE_PREFIX + state.path),
            )
        sidecar_store.bump_meta(conn, embedding_index.SEMANTIC_UNIT_GENERATION)
    incomplete: list[str] = []
    index.search_semantic_units(
        query, 1, allowed_parent_paths={state.path}, validate=False, incomplete_out=incomplete
    )
    assert incomplete == [state.path]


@pytest.mark.parametrize("profile", ["resident", "service-v1"])
def test_empty_publication_proves_coverage_but_an_unpublished_parent_does_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
) -> None:
    monkeypatch.setattr(embedding_index.cloud_cell, "resource_policy", lambda: profile)
    path = _write_page(tmp_path, 1)
    path.write_text("---\ntype: note\n---\n# Empty page\n\nOrdinary prose.\n", encoding="utf-8")
    state = semantic_index.build_parent_index_state(tmp_path, path)
    assert not state.occurrences
    index = embedding_index.EmbeddingIndex(tmp_path)
    query = np.ones(index.dim, dtype=np.float32)
    index.upsert_file(
        state.path, ["Ordinary prose."], np.ones((1, len(query)), dtype=np.float32), 1.0
    )
    incomplete: list[str] = []
    assert (
        index.search_semantic_units(
            query, 1, allowed_parent_paths={state.path}, validate=False, incomplete_out=incomplete
        )
        == []
    )
    assert incomplete == [state.path]
    index.upsert_semantic_units(state, np.empty((0, len(query)), dtype=np.float32), 1.0)
    incomplete.clear()
    assert (
        index.search_semantic_units(
            query, 1, allowed_parent_paths={state.path}, validate=False, incomplete_out=incomplete
        )
        == []
    )
    assert incomplete == []


@pytest.mark.parametrize("profile", ["resident", "service-v1"])
def test_publication_after_vector_selection_keeps_rows_and_records_on_one_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
) -> None:
    monkeypatch.setattr(embedding_index.cloud_cell, "resource_policy", lambda: profile)
    path = _write_page(tmp_path, 1)
    first = semantic_index.build_parent_index_state(tmp_path, path)
    index = embedding_index.EmbeddingIndex(tmp_path)
    query = np.ones(embedding_index.VECTOR_DIM, dtype=np.float32)
    vectors = np.ones((UNITS_PER_PARENT, len(query)), dtype=np.float32)
    index.upsert_semantic_units(first, vectors, 1.0)
    before = index.search_semantic_units(query, 1, validate=False)
    path.write_text(path.read_text().replace("^u", "^replacement"), encoding="utf-8")
    replacement = semantic_index.build_parent_index_state(tmp_path, path)
    owner, method = (
        (index, "_unit_vectors") if profile == "resident" else (embedding_index, "_unit_coverage")
    )
    load = getattr(owner, method)
    published = False

    def publish_after_selection(*args):
        nonlocal published
        cached = load(*args)
        if not published:
            index.upsert_semantic_units(replacement, vectors, 2.0)
            published = True
        return cached

    monkeypatch.setattr(owner, method, publish_after_selection)
    incomplete: list[str] = []
    assert (
        index.search_semantic_units(query, 1, validate=False, incomplete_out=incomplete) == before
    )
    assert incomplete == []
    after = index.search_semantic_units(query, 1, validate=False)
    assert [hit.unit_ref for hit in after] == [f"{replacement.parent_ref}#replacement0"]
    assert after != before


@pytest.mark.parametrize("profile", ["resident", "service-v1"])
def test_query_identity_rejects_a_same_width_space_cutover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
) -> None:
    monkeypatch.setattr(embedding_index.cloud_cell, "resource_policy", lambda: profile)
    state = semantic_index.build_parent_index_state(tmp_path, _write_page(tmp_path, 1))
    index = embedding_index.EmbeddingIndex(tmp_path)
    query = np.ones(embedding_index.VECTOR_DIM, dtype=np.float32)
    index.upsert_semantic_units(
        state, np.ones((UNITS_PER_PARENT, len(query)), dtype=np.float32), 1.0
    )
    encoded_for = index.identity
    assert index.search_semantic_units(query, 1, validate=False, encoded_for=encoded_for)
    with sqlite3.connect(index.path) as conn:
        recall_space.write_identity(
            conn, recall_space.SpaceIdentity("other-test-space", None, len(query))
        )
        sidecar_store.bump_meta(conn, embedding_index.SEMANTIC_UNIT_GENERATION)
    with pytest.raises(recall_space.VectorSpaceMismatch):
        index.search_semantic_units(query, 1, validate=False, encoded_for=encoded_for)


@pytest.mark.parametrize("profile", ["resident", "service-v1"])
def test_duplicate_selected_references_spend_one_slot_even_when_every_vector_ties(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
) -> None:
    """Two authored occurrences of one reference cannot crowd out another unit."""
    monkeypatch.setattr(embedding_index.cloud_cell, "resource_policy", lambda: profile)
    monkeypatch.setattr(embedding_index.EmbeddingIndex, "DISK_BLOCK_ROWS", 1)
    path = _write_page(tmp_path, 1)
    path.write_text(
        "---\ntype: note\nexomem_id: 22222222-2222-4222-8222-222222222222\n---\n"
        "# Duplicates\n\n- [decision] First occurrence. ^shared\n"
        "- [decision] Broken first occurrence. ^broken\n"
        "- [decision] Broken second occurrence. ^broken\n"
        "- [decision] Distinct choice. ^unique\n",
        encoding="utf-8",
    )
    state = semantic_index.build_parent_index_state(tmp_path, path)
    duplicate_path = path.with_name("duplicate.md")
    duplicate_path.write_text(
        "---\ntype: note\nexomem_id: 22222222-2222-4222-8222-222222222222\n---\n"
        "# Other carrier\n\n- [decision] Second occurrence. ^shared\n",
        encoding="utf-8",
    )
    duplicate = semantic_index.build_parent_index_state(tmp_path, duplicate_path)
    expected = [f"{state.parent_ref}#shared", f"{state.parent_ref}#unique"]
    assert len(state.occurrences) == 4 and len(expected) == 2
    assert duplicate.document.units[0].unit_ref in expected
    index = embedding_index.EmbeddingIndex(tmp_path)
    query = np.ones(embedding_index.VECTOR_DIM, dtype=np.float32)
    for published in (state, duplicate):
        index.upsert_semantic_units(
            published,
            np.zeros((len(published.occurrences), len(query)), dtype=np.float32),
            1.0,
        )
    assert [
        hit.unit_ref for hit in index.search_semantic_units(query, 2, validate=False)
    ] == expected
