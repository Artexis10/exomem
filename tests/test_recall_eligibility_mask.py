"""`EmbeddingIndex.search` under an allowed-paths filter: score first, mask second.

Issue #951. Every semantic recall passes `allowed_paths`, so the pre-fix scan ran
a Python loop over every chunk row and then fancy-indexed `matrix[keep]` — a fresh
copy of most of a ~200 MB matrix, per query. The fix scores the full cached matrix
once and masks the SCORES, memoizing the row mask.

The load-bearing property is that this is a speedup and NOT a ranking change, so
the primary test here runs the verbatim pre-fix implementation
(`_search_pre_951`, transcribed from `git show 9bf3d804:src/exomem/embedding_index.py`)
against the same index, query and allowed set, and demands the same answer.

All vectors are fabricated — no torch, no model, no live cell. Every test runs
under conftest's injected `EXOMEM_STATE_ROOT` on its own tmpdir vault.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from exomem import embeddings

# Justified in the module docstring's companion measurement (see the identity
# test): OpenBLAS picks its sgemv kernel by row count, so the pre-fix code's
# scores depended on how many rows the filter happened to keep. Slicing 85% of a
# 67,000-row matrix moved 0.09% of scores by at most 2.3e-8 against the
# full-matrix pass. The tolerance is ~40x that measured worst case and ~5x below
# float32 epsilon (1.19e-7), so it admits the kernel wobble and nothing larger.
SCORE_TOLERANCE = 1e-6


@pytest.fixture(autouse=True)
def _clean_memo() -> None:
    """Each test starts and ends with an empty shared-index memo."""
    embeddings.clear_embedding_indexes()
    yield
    embeddings.clear_embedding_indexes()


def _fresh_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "Knowledge Base").mkdir(parents=True)
    return vault


def _unit_rows(rng: np.random.Generator, count: int) -> np.ndarray:
    """`count` normalized float32 rows, the shape real chunk vectors arrive in."""
    rows = rng.standard_normal((count, embeddings.VECTOR_DIM), dtype=np.float32)
    rows /= np.linalg.norm(rows, axis=1, keepdims=True)
    return rows


def _unit_query(rng: np.random.Generator) -> np.ndarray:
    q = rng.standard_normal(embeddings.VECTOR_DIM).astype(np.float32)
    return q / np.linalg.norm(q)


def _sparse_rows(*specs: list[tuple[int, float]]) -> np.ndarray:
    """Rows whose dot products are EXACT in float32 (few non-zero terms)."""
    out = np.zeros((len(specs), embeddings.VECTOR_DIM), dtype=np.float32)
    for row, spec in enumerate(specs):
        for column, value in spec:
            out[row, column] = np.float32(value)
    return out


def _empty_vectors() -> np.ndarray:
    return np.zeros((0, embeddings.VECTOR_DIM), dtype=np.float32)


def _search_pre_951(
    index: embeddings.EmbeddingIndex,
    query_vec: np.ndarray,
    k: int,
    *,
    allowed_paths: set[str] | None = None,
) -> list[tuple[str, int, str, float]]:
    """The pre-fix `EmbeddingIndex.search`, transcribed verbatim.

    Provenance: `git show 9bf3d804:src/exomem/embedding_index.py`, the body of
    `search` before this change. Kept here rather than in production so the
    comparison is against real removed code and the shipped module carries only
    one implementation.
    """
    if allowed_paths is None:
        vec_hits = index._vec_search(query_vec, k)
        if vec_hits is not None:
            return vec_hits
    metadata, matrix = index.all_vectors()
    if not metadata:
        return []
    if allowed_paths is not None:
        keep = [i for i, (path, _chunk) in enumerate(metadata) if path in allowed_paths]
        if not keep:
            return []
        metadata = [metadata[i] for i in keep]
        matrix = matrix[keep]
    scores = matrix @ query_vec.astype(np.float32, copy=False)
    k_eff = min(k, len(scores))
    if k_eff <= 0:
        return []
    top_idx = np.argpartition(-scores, k_eff - 1)[:k_eff]
    top_idx = top_idx[np.argsort(-scores[top_idx])]
    top = [(metadata[i][0], metadata[i][1], float(scores[i])) for i in top_idx]
    texts = index._texts_for([(fp, ci) for fp, ci, _ in top])
    return [(fp, ci, texts.get((fp, ci), ""), score) for fp, ci, score in top]


def _assert_same_answer(
    old: list[tuple[str, int, str, float]],
    new: list[tuple[str, int, str, float]],
    *,
    exact_scores: bool = False,
) -> None:
    """Same rows, same order, same texts; scores exactly or within tolerance."""
    assert [(fp, ci, text) for fp, ci, text, _ in old] == [
        (fp, ci, text) for fp, ci, text, _ in new
    ]
    old_scores = [score for *_rest, score in old]
    new_scores = [score for *_rest, score in new]
    if exact_scores:
        assert old_scores == new_scores
    else:
        assert np.allclose(old_scores, new_scores, rtol=0.0, atol=SCORE_TOLERANCE)


def _populated(vault: Path, rng: np.random.Generator, files: int, per_file: int):
    """An index of `files` x `per_file` chunks, plus its path list."""
    index = embeddings.EmbeddingIndex(vault)
    rows = _unit_rows(rng, files * per_file)
    paths = [f"note-{n:03d}.md" for n in range(files)]
    for n, path in enumerate(paths):
        block = rows[n * per_file : (n + 1) * per_file]
        index.upsert_file(path, [f"{path} chunk {c}" for c in range(per_file)], block, 1.0)
    return index, paths


# --------------------------------------------------------------------------- #
# The one that matters: the fix must not change a single answer.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "scope,k",
    [
        ("none", 10),
        ("empty", 10),
        ("every_row", 10),
        ("one_row", 10),
        ("fewer_than_k", 50),
        ("half", 10),
        ("exactly_k", 12),
    ],
)
def test_masked_search_answers_exactly_as_the_pre_951_slice_did(tmp_path, scope, k):
    """Old slice-the-matrix path and new mask-the-scores path agree, row for row."""
    rng = np.random.default_rng(951)
    vault = _fresh_vault(tmp_path)
    index, paths = _populated(vault, rng, files=40, per_file=3)

    allowed: set[str] | None
    if scope == "none":
        allowed = None
    elif scope == "empty":
        allowed = set()
    elif scope == "every_row":
        allowed = set(paths)
    elif scope == "one_row":
        allowed = {paths[7]}
    elif scope == "fewer_than_k":  # 4 files x 3 chunks = 12 rows, k = 50
        allowed = set(paths[:4])
    elif scope == "exactly_k":  # 4 files x 3 chunks = 12 rows, k = 12
        allowed = set(paths[:4])
    else:
        allowed = set(paths[::2])

    for _ in range(25):  # many queries: an ordering flip anywhere is a failure
        query = _unit_query(rng)
        old = _search_pre_951(index, query, k, allowed_paths=allowed)
        index._mask_cache = None  # the old call cannot have warmed anything
        new = index.search(query, k, allowed_paths=allowed)
        _assert_same_answer(old, new)


def test_masked_search_is_bit_exact_when_the_dot_products_are_exact(tmp_path):
    """With sparse vectors the two paths agree to the last bit, not just closely.

    The tolerance in the test above exists only because OpenBLAS varies its sgemv
    kernel with the row count. Remove that source of noise and the masking change
    is exactly what it claims: the same arithmetic on the same rows.
    """
    vault = _fresh_vault(tmp_path)
    index = embeddings.EmbeddingIndex(vault)
    index.upsert_file("a.md", ["a0", "a1"], _sparse_rows([(0, 1.0)], [(1, 0.5)]), 1.0)
    index.upsert_file("b.md", ["b0", "b1"], _sparse_rows([(2, 0.25)], [(3, 0.125)]), 1.0)
    index.upsert_file("c.md", ["c0"], _sparse_rows([(0, 0.75)]), 1.0)
    query = _sparse_rows([(0, 1.0), (1, 1.0), (2, 1.0), (3, 1.0)])[0]

    for allowed in (None, {"a.md"}, {"a.md", "c.md"}, {"a.md", "b.md", "c.md"}):
        old = _search_pre_951(index, query, 5, allowed_paths=allowed)
        index._mask_cache = None
        new = index.search(query, 5, allowed_paths=allowed)
        _assert_same_answer(old, new, exact_scores=True)


# --------------------------------------------------------------------------- #
# Masked rows must be unreachable, however few survive the filter.
# --------------------------------------------------------------------------- #


def test_empty_allowed_paths_returns_nothing(tmp_path):
    rng = np.random.default_rng(2)
    vault = _fresh_vault(tmp_path)
    index, _paths = _populated(vault, rng, files=6, per_file=2)
    assert index.search(_unit_query(rng), 10, allowed_paths=set()) == []


def test_allowed_paths_naming_nothing_indexed_returns_nothing(tmp_path):
    rng = np.random.default_rng(3)
    vault = _fresh_vault(tmp_path)
    index, _paths = _populated(vault, rng, files=6, per_file=2)
    assert index.search(_unit_query(rng), 10, allowed_paths={"absent.md"}) == []


def test_fewer_eligible_rows_than_k_never_admits_an_ineligible_row(tmp_path):
    """`k` far above the eligible count must not pad the answer with masked rows.

    This is the `-inf` trap: `argpartition` over the FULL score array will happily
    return `k` rows, and the masked ones sit at `-inf` rather than being absent.
    Only clamping `k_eff` to the eligible count keeps them out.
    """
    rng = np.random.default_rng(4)
    vault = _fresh_vault(tmp_path)
    index, paths = _populated(vault, rng, files=30, per_file=3)
    allowed = {paths[11]}  # 1 file x 3 chunks = 3 eligible rows

    hits = index.search(_unit_query(rng), 500, allowed_paths=allowed)

    assert len(hits) == 3
    assert {fp for fp, _ci, _text, _score in hits} == allowed
    assert all(np.isfinite(score) for *_rest, score in hits)


def test_single_eligible_row_returns_only_that_row(tmp_path):
    vault = _fresh_vault(tmp_path)
    index = embeddings.EmbeddingIndex(vault)
    rng = np.random.default_rng(5)
    for n in range(8):
        index.upsert_file(f"n{n}.md", [f"c{n}"], _unit_rows(rng, 1), 1.0)

    hits = index.search(_unit_query(rng), 25, allowed_paths={"n3.md"})

    assert [(fp, ci) for fp, ci, _text, _score in hits] == [("n3.md", 0)]


def test_every_row_eligible_matches_the_unfiltered_ranking(tmp_path):
    """An allow-everything filter must rank exactly as no filter at all."""
    rng = np.random.default_rng(6)
    vault = _fresh_vault(tmp_path)
    index, paths = _populated(vault, rng, files=25, per_file=4)
    query = _unit_query(rng)

    unfiltered = index.search(query, 15)
    permissive = index.search(query, 15, allowed_paths=set(paths))

    _assert_same_answer(unfiltered, permissive)


# --------------------------------------------------------------------------- #
# The memo: fast when it is safe, rebuilt whenever it is not.
# --------------------------------------------------------------------------- #


def test_mask_is_reused_across_queries_at_the_same_scope(tmp_path):
    """The point of the memo: the second query at a scope rebuilds nothing."""
    rng = np.random.default_rng(7)
    vault = _fresh_vault(tmp_path)
    index, paths = _populated(vault, rng, files=20, per_file=3)
    allowed = set(paths[:10])

    index.search(_unit_query(rng), 5, allowed_paths=allowed)
    first = index._mask_cache
    assert first is not None
    index.search(_unit_query(rng), 5, allowed_paths=allowed)

    assert index._mask_cache is first


def test_an_equal_but_distinct_allowed_set_still_hits(tmp_path):
    """`find_candidates` rebuilds `recall_paths & eligible_paths` per call.

    Keying on object identity would therefore miss on every single query, which
    is why the memo keys on the set's CONTENT.
    """
    rng = np.random.default_rng(8)
    vault = _fresh_vault(tmp_path)
    index, paths = _populated(vault, rng, files=20, per_file=3)

    index.search(_unit_query(rng), 5, allowed_paths=set(paths[:10]))
    first = index._mask_cache
    index.search(_unit_query(rng), 5, allowed_paths=set(paths[:10]))  # equal, not the same

    assert index._mask_cache is first


def test_mask_is_rebuilt_when_the_scope_changes(tmp_path):
    rng = np.random.default_rng(9)
    vault = _fresh_vault(tmp_path)
    index, paths = _populated(vault, rng, files=20, per_file=3)
    query = _unit_query(rng)

    narrow = index.search(query, 5, allowed_paths={paths[0]})
    wide = index.search(query, 5, allowed_paths=set(paths))

    assert {fp for fp, *_rest in narrow} == {paths[0]}
    assert len(wide) == 5


def test_a_stale_mask_cannot_survive_the_matrix_being_rebuilt(tmp_path):
    """The invalidation that matters, and the one a wrong answer would hide.

    `a.md` is retired and `c.md` takes its place, so the row COUNT is unchanged
    and only the identity of the rows moved. A mask kept across that rebuild
    still marks rows 0-1 eligible and would return `b.md`'s chunks under a scope
    that now matches nothing at all.
    """
    rng = np.random.default_rng(10)
    vault = _fresh_vault(tmp_path)
    index = embeddings.EmbeddingIndex(vault)
    rows = _unit_rows(rng, 6)
    index.upsert_file("a.md", ["a0", "a1"], rows[0:2], 1.0)
    index.upsert_file("b.md", ["b0", "b1"], rows[2:4], 1.0)
    query = _unit_query(rng)

    warm = index.search(query, 4, allowed_paths={"a.md"})
    assert {fp for fp, *_rest in warm} == {"a.md"}

    index.upsert_file("a.md", [], _empty_vectors(), 2.0)
    index.upsert_file("c.md", ["c0", "c1"], rows[4:6], 2.0)

    assert index.search(query, 4, allowed_paths={"a.md"}) == []


def test_a_stale_mask_cannot_survive_rows_shifting_position(tmp_path):
    """Same guard, caught as a WRONG ROW rather than a wrong emptiness.

    The row count is identical before and after, so nothing about the shape
    gives the staleness away — only the rows moved. `a.md` sheds a chunk and
    `c.md` gains one, which slides `b.md` from index 2 to index 1. A mask kept
    across that still marks index 2, and index 2 is now `c.md`: a scope of
    exactly `{b.md}` would answer with a file it excludes.
    """
    vault = _fresh_vault(tmp_path)
    index = embeddings.EmbeddingIndex(vault)
    index.upsert_file("a.md", ["a0", "a1"], _sparse_rows([(0, 1.0)], [(1, 1.0)]), 1.0)
    index.upsert_file("b.md", ["b0"], _sparse_rows([(2, 1.0)]), 1.0)
    query = _sparse_rows([(2, 1.0)])[0]

    warm = index.search(query, 2, allowed_paths={"b.md"})
    assert [(fp, score) for fp, _ci, _text, score in warm] == [("b.md", 1.0)]

    index.upsert_file("a.md", ["a0"], _sparse_rows([(0, 1.0)]), 2.0)
    index.upsert_file("c.md", ["c0"], _sparse_rows([(3, 1.0)]), 2.0)

    hits = index.search(query, 2, allowed_paths={"b.md"})
    assert [(fp, score) for fp, _ci, _text, score in hits] == [("b.md", 1.0)]


def test_a_stale_mask_cannot_survive_the_allowed_set_being_mutated_in_place(tmp_path):
    """Callers own the set they pass and may narrow it between queries."""
    vault = _fresh_vault(tmp_path)
    index = embeddings.EmbeddingIndex(vault)
    index.upsert_file("a.md", ["a0"], _sparse_rows([(0, 1.0)]), 1.0)
    index.upsert_file("b.md", ["b0"], _sparse_rows([(1, 1.0)]), 1.0)
    query = _sparse_rows([(0, 1.0), (1, 1.0)])[0]
    allowed = {"a.md", "b.md"}

    assert len(index.search(query, 5, allowed_paths=allowed)) == 2

    allowed.discard("a.md")  # same object, different content

    hits = index.search(query, 5, allowed_paths=allowed)
    assert [(fp, ci) for fp, ci, _text, _score in hits] == [("b.md", 0)]


def test_unload_cache_drops_the_mask_memo(tmp_path):
    """The memo pins the metadata list, so an unload must release it too."""
    rng = np.random.default_rng(13)
    vault = _fresh_vault(tmp_path)
    index, paths = _populated(vault, rng, files=10, per_file=2)
    index.search(_unit_query(rng), 5, allowed_paths=set(paths))
    assert index._mask_cache is not None

    index.unload_cache()

    assert index._mask_cache is None


def test_mask_memo_survives_a_reload_only_by_being_rebuilt(tmp_path):
    """After an unload the answer is still right — the memo just pays again."""
    rng = np.random.default_rng(14)
    vault = _fresh_vault(tmp_path)
    index, paths = _populated(vault, rng, files=12, per_file=2)
    allowed = set(paths[:5])
    query = _unit_query(rng)
    before = index.search(query, 8, allowed_paths=allowed)

    index.unload_cache()
    after = index.search(query, 8, allowed_paths=allowed)

    _assert_same_answer(before, after)
    assert index._mask_cache is not None
