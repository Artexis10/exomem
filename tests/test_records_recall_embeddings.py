"""Records must stay out of every vector-backed ordinary-recall path."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from exomem import embedding_index, embeddings, semantic_index
from exomem import find as find_module
from exomem.clip_index import CLIP_DIM, ClipIndex
from exomem.ranking_config import RankingConfig


def _write_page(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "type: insight\n"
        "title: Vector fixture\n"
        "exomem_id: 33333333-3333-4333-8333-333333333333\n"
        "updated: 2026-08-02\n"
        "---\n"
        "# Vector fixture\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    find_module.clear_cache()
    return path


def _vector() -> np.ndarray:
    return np.ones((1, embedding_index.VECTOR_DIM), dtype=np.float32)


def test_disabled_write_purges_raw_record_vectors_without_loading_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _write_page(
        tmp_path,
        "Knowledge Base/Records/Health/Training Log.md",
        "- [finding] raw training session ^raw",
    )
    index = embedding_index.EmbeddingIndex(tmp_path)
    index.upsert_file("Knowledge Base/Records/Health/Training Log.md", ["raw"], _vector(), 1.0)
    state = semantic_index.build_parent_index_state(tmp_path, raw)
    index.upsert_semantic_units(state, _vector(), 1.0)

    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setattr(
        embeddings,
        "get_model",
        lambda: (_ for _ in ()).throw(AssertionError("raw purge must not load a model")),
    )

    status = embeddings.upsert_after_write_status(tmp_path, [raw])

    assert status.code == "embeddings_disabled"
    assert index.search(_vector()[0], k=10) == []
    assert index.search_semantic_units(_vector()[0], k=10, validate=False) == []

    # Import failure has the same model-free purge guarantee.
    index.upsert_file("Knowledge Base/Records/Health/Training Log.md", ["raw"], _vector(), 2.0)
    index.upsert_semantic_units(state, _vector(), 2.0)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS")
    monkeypatch.setattr(embeddings, "_IMPORT_FAILED", True)
    unavailable = embeddings.upsert_after_write_status(tmp_path, [raw])

    assert unavailable.code == "no_eligible_paths"
    assert index.search(_vector()[0], k=10) == []
    assert index.search_semantic_units(_vector()[0], k=10, validate=False) == []


def test_semantic_unit_vector_search_excludes_raw_record_parents_before_top_k(
    tmp_path: Path,
) -> None:
    raw = _write_page(
        tmp_path,
        "Knowledge Base/Records/Health/Training Log.md",
        "- [finding] raw training session ^raw",
    )
    admitted = _write_page(
        tmp_path,
        "Knowledge Base/Notes/allowed.md",
        "- [finding] ordinary note ^allowed",
    )
    index = embedding_index.EmbeddingIndex(tmp_path)
    raw_state = semantic_index.build_parent_index_state(tmp_path, raw)
    admitted_state = semantic_index.build_parent_index_state(tmp_path, admitted)
    index.upsert_semantic_units(raw_state, _vector(), 1.0)
    index.upsert_semantic_units(admitted_state, _vector(), 1.0)

    hits = index.search_semantic_units(
        _vector()[0],
        k=1,
        allowed_parent_paths={"Knowledge Base/Notes/allowed.md"},
        validate=False,
    )

    assert [hit.parent_path for hit in hits] == ["Knowledge Base/Notes/allowed.md"]


def test_vector_allowlists_prevent_stale_raw_rows_from_starving_admitted_hits(
    tmp_path: Path,
) -> None:
    index = embedding_index.EmbeddingIndex(tmp_path)
    query = _vector()[0]
    for number in range(64):
        index.upsert_file(
            f"Knowledge Base/Records/Health/raw-{number}.md",
            ["raw"],
            _vector(),
            float(number),
        )
    index.upsert_file("Knowledge Base/Notes/allowed.md", ["allowed"], _vector(), 100.0)

    hits = index.search(query, k=1, allowed_paths={"Knowledge Base/Notes/allowed.md"})

    assert [hit[0] for hit in hits] == ["Knowledge Base/Notes/allowed.md"]


def test_unit_allowlists_prevent_stale_raw_rows_from_starving_admitted_hits(
    tmp_path: Path,
) -> None:
    raw = _write_page(
        tmp_path,
        "Knowledge Base/Records/Health/Training Log.md",
        "\n".join(f"- [finding] raw {number} ^raw-{number}" for number in range(64)),
    )
    admitted = _write_page(
        tmp_path,
        "Knowledge Base/Notes/allowed.md",
        "- [finding] admitted ^admitted",
    )
    index = embedding_index.EmbeddingIndex(tmp_path)
    raw_state = semantic_index.build_parent_index_state(tmp_path, raw)
    admitted_state = semantic_index.build_parent_index_state(tmp_path, admitted)
    index.upsert_semantic_units(raw_state, np.repeat(_vector(), 64, axis=0), 1.0)
    index.upsert_semantic_units(admitted_state, _vector(), 1.0)

    hits = index.search_semantic_units(
        _vector()[0],
        k=1,
        allowed_parent_paths={"Knowledge Base/Notes/allowed.md"},
        validate=False,
    )

    assert [hit.parent_path for hit in hits] == ["Knowledge Base/Notes/allowed.md"]


def test_semantic_parent_validation_rejects_raw_record_before_reading_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _write_page(
        tmp_path,
        "Knowledge Base/Records/Health/Training Log.md",
        "- [finding] raw training session ^raw",
    )

    monkeypatch.setattr(
        Path,
        "read_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not read raw record")),
    )

    freshness = semantic_index.validate_parent_record(
        tmp_path,
        parent_path="Knowledge Base/Records/Health/Training Log.md",
        parent_generation_value="stale",
        parent_source_hash="stale",
        parser_version=semantic_index.PARSER_VERSION,
    )

    assert freshness.code == "recall_parent_not_admitted"
    assert raw.exists()


def test_live_upsert_refuses_and_purges_a_path_that_drifts_before_unit_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = _write_page(
        tmp_path,
        "Knowledge Base/Notes/drifting.md",
        "- [finding] current before drift ^current",
    )
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "_IMPORT_FAILED", False)
    monkeypatch.setattr(embeddings, "get_model", lambda: object())
    monkeypatch.setattr(embeddings, "_chunks_for_page", lambda *_args: ["page chunk"])
    calls = 0

    def encode(chunks: list[str]) -> np.ndarray:
        nonlocal calls
        calls += 1
        if calls == 1:
            page.write_text(page.read_text(encoding="utf-8") + "\ndirect edit\n", encoding="utf-8")
        return np.repeat(_vector(), len(chunks), axis=0)

    monkeypatch.setattr(embeddings, "_embed_live_chunks", encode)

    status = embeddings.upsert_after_write_status(tmp_path, [page], defer_during_warm=False)
    index = embedding_index.EmbeddingIndex(tmp_path)

    assert status.code == "embedding_input_drifted"
    assert index.search(_vector()[0], k=10) == []
    assert index.search_semantic_units(_vector()[0], k=10, validate=False) == []


def test_find_vector_lane_pre_filters_raw_record_rows_before_top_k_and_hydration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _write_page(
        tmp_path,
        "Knowledge Base/Records/Health/Training Log.md",
        "- [finding] raw training session ^raw",
    )
    _write_page(
        tmp_path,
        "Knowledge Base/Notes/allowed.md",
        "- [finding] admitted planning note ^admitted",
    )
    index = embedding_index.EmbeddingIndex(tmp_path)
    raw_rel = "Knowledge Base/Records/Health/Training Log.md"
    admitted_rel = "Knowledge Base/Notes/allowed.md"
    raw_vector = np.full((1, embedding_index.VECTOR_DIM), 10.0, dtype=np.float32)
    admitted_vector = np.zeros((1, embedding_index.VECTOR_DIM), dtype=np.float32)
    admitted_vector[0, 0] = 1.0
    index.upsert_file(raw_rel, ["raw"], raw_vector, 1.0)
    index.upsert_file(admitted_rel, ["admitted"], admitted_vector, 1.0)

    real_get = find_module._CACHE.get

    def no_raw_hydration(path: Path, vault_root: Path):
        if path == raw:
            raise AssertionError("raw Record must be filtered before hydration")
        return real_get(path, vault_root)

    monkeypatch.setattr(find_module._CACHE, "get", no_raw_hydration)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "numpy")
    monkeypatch.setattr(
        embeddings,
        "embed_texts",
        lambda _texts, *, is_query=False: np.eye(1, embedding_index.VECTOR_DIM, dtype=np.float32),
    )
    find_module.clear_cache()

    hits = find_module.find(
        tmp_path,
        query="progress",
        limit=1,
        mode="vector",
        graph=False,
        temporal=False,
        rerank=False,
        config=RankingConfig(candidate_multiplier=1, candidate_floor=1),
    )

    assert [hit.path for hit in hits] == [admitted_rel]


def test_find_unit_vector_lane_pre_filters_raw_record_parents_before_top_k(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _write_page(
        tmp_path,
        "Knowledge Base/Records/Health/Training Log.md",
        "\n".join(f"- [finding] raw {index} ^raw-{index}" for index in range(64)),
    )
    admitted = _write_page(
        tmp_path,
        "Knowledge Base/Notes/allowed.md",
        "- [finding] admitted unit ^admitted",
    )
    index = embedding_index.EmbeddingIndex(tmp_path)
    raw_state = semantic_index.build_parent_index_state(tmp_path, raw)
    admitted_state = semantic_index.build_parent_index_state(tmp_path, admitted)
    index.upsert_semantic_units(
        raw_state,
        np.full((64, embedding_index.VECTOR_DIM), 10.0, dtype=np.float32),
        1.0,
    )
    admitted_vector = np.zeros((1, embedding_index.VECTOR_DIM), dtype=np.float32)
    admitted_vector[0, 0] = 1.0
    index.upsert_semantic_units(admitted_state, admitted_vector, 1.0)

    real_get = find_module._CACHE.get

    def no_raw_hydration(path: Path, vault_root: Path):
        if path == raw:
            raise AssertionError("raw Record parent must not be hydrated")
        return real_get(path, vault_root)

    monkeypatch.setattr(find_module._CACHE, "get", no_raw_hydration)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "numpy")
    monkeypatch.setattr(
        embeddings,
        "embed_texts",
        lambda _texts, *, is_query=False: np.eye(1, embedding_index.VECTOR_DIM, dtype=np.float32),
    )
    find_module.clear_cache()

    hits = find_module.find(
        tmp_path,
        query="progress",
        limit=1,
        result_level="unit",
        mode="vector",
        graph=False,
    )

    assert [hit.parent_path for hit in hits] == ["Knowledge Base/Notes/allowed.md"]


def test_find_clip_lane_pre_filters_raw_record_sidecars_before_top_k(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_paths = [
        _write_page(
            tmp_path,
            f"Knowledge Base/Records/Health/raw-{index}.jpg.md",
            f"- [finding] raw image {index} ^raw-{index}",
        )
        for index in range(16)
    ]
    admitted = _write_page(
        tmp_path,
        "Knowledge Base/Notes/allowed.jpg.md",
        "- [finding] admitted image ^admitted-image",
    )
    clip = ClipIndex(tmp_path)
    raw_vector = np.full(CLIP_DIM, 10.0, dtype=np.float32)
    admitted_vector = np.zeros(CLIP_DIM, dtype=np.float32)
    admitted_vector[0] = 1.0
    for path in raw_paths:
        clip.upsert(path.relative_to(tmp_path).as_posix().removesuffix(".md"), raw_vector, 1.0)
    admitted_rel = admitted.relative_to(tmp_path).as_posix()
    clip.upsert(admitted_rel.removesuffix(".md"), admitted_vector, 1.0)

    real_get = find_module._CACHE.get
    raw_set = set(raw_paths)

    def no_raw_hydration(path: Path, vault_root: Path):
        if path in raw_set:
            raise AssertionError("raw Record image sidecar must not be hydrated")
        return real_get(path, vault_root)

    monkeypatch.setattr(find_module._CACHE, "get", no_raw_hydration)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    monkeypatch.setattr(embeddings, "embed_clip_text", lambda _query: np.eye(1, CLIP_DIM)[0])
    find_module.clear_cache()

    hits = find_module.find(
        tmp_path,
        query="progress",
        limit=1,
        mode="vector",
        graph=False,
        temporal=False,
        rerank=False,
        config=RankingConfig(candidate_multiplier=1, candidate_floor=1),
    )

    assert [hit.path for hit in hits] == [admitted_rel]


def test_rebuild_refuses_to_publish_when_projected_source_changes_mid_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = _write_page(
        tmp_path,
        "Knowledge Base/Notes/allowed.md",
        "- [finding] before rebuild ^before",
    )
    rel = "Knowledge Base/Notes/allowed.md"
    index = embedding_index.EmbeddingIndex(tmp_path)
    index.upsert_file(rel, ["old sidecar row"], _vector(), 1.0)
    monkeypatch.setattr(embeddings, "_chunks_for_page", lambda *_args: ["new sidecar row"])
    calls = 0

    def encode(texts: list[str], *, is_query: bool) -> np.ndarray:
        nonlocal calls
        assert is_query is False
        calls += 1
        if calls == 1:
            page.write_text(page.read_text(encoding="utf-8") + "\ndirect edit\n", encoding="utf-8")
        return np.repeat(_vector(), len(texts), axis=0)

    monkeypatch.setattr(embeddings, "embed_texts", encode)

    assert index.rebuild_all() == 0
    assert index.search(_vector()[0], k=1)[0][2] == "old sidecar row"


def test_clip_purge_maps_raw_markdown_sidecar_and_never_creates_absent_store(
    tmp_path: Path,
) -> None:
    raw_rel = "Knowledge Base/Records/Health/photo.jpg.md"
    raw = _write_page(tmp_path, raw_rel, "- [finding] raw photo sidecar ^raw-photo")
    clip = ClipIndex(tmp_path)
    vector = np.ones(CLIP_DIM, dtype=np.float32)
    clip.upsert(raw_rel.removesuffix(".md"), vector, 1.0)

    assert not clip.is_recall_candidate(raw_rel.removesuffix(".md"))
    assert clip.purge_markdown_paths_if_present([raw_rel]) == 1
    assert not clip.has(raw_rel.removesuffix(".md"))

    absent = tmp_path / "second"
    assert ClipIndex(absent).purge_markdown_paths_if_present([raw_rel]) == 0
    assert not (absent / "Knowledge Base" / ".clip.sqlite").exists()
    assert raw.exists()


def test_clip_allowlist_prevents_raw_record_vectors_from_starving_admitted_hit(
    tmp_path: Path,
) -> None:
    clip = ClipIndex(tmp_path)
    vector = np.ones(CLIP_DIM, dtype=np.float32)
    for number in range(64):
        clip.upsert(f"Knowledge Base/Records/Health/raw-{number}.jpg", vector, float(number))
    clip.upsert("Knowledge Base/Notes/allowed.jpg", vector, 100.0)

    hits = clip.search(
        vector,
        k=1,
        allowed_paths={"Knowledge Base/Notes/allowed.jpg"},
    )

    assert [hit[0] for hit in hits] == ["Knowledge Base/Notes/allowed.jpg"]
