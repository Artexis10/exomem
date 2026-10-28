"""An idle visual lane is inapplicable and stays model-free until vectors arrive."""

import sqlite3

import numpy as np
import pytest

from exomem import embeddings, find_candidates, preserve, readiness, warmup
from exomem import find as find_module
from exomem.retrieval_explain import RetrievalTrace


@pytest.fixture(autouse=True)
def _isolated_readiness():
    readiness.reset()
    yield
    readiness.reset()


@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize("disabled", [False, True])
def test_idle_clip_trace_never_loads_or_defers(vault, monkeypatch, prepared, disabled):
    monkeypatch.setenv("EXOMEM_FIND_CACHE_SIZE", "0")
    if disabled:
        monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    else:
        monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    calls = []
    probes = []
    index = embeddings.get_clip_index(vault)
    real_probe = index.has_vectors
    monkeypatch.setattr(index, "has_vectors", lambda: probes.append(True) or real_probe())
    monkeypatch.setattr(embeddings, "embed_clip_text", lambda _q: calls.append(True))
    readiness.begin_warm()
    readiness.mark_ready("retrieval_catalog")
    inputs = find_candidates.prepare_query_inputs(vault, "metabolism", unit=False, page=True) if prepared else None
    trace = RetrievalTrace("hybrid", "page", False, False)
    degraded = []
    find_module.find(
        vault, query="metabolism", mode="hybrid", result_level="page", rerank=False,
        retrieval_trace=trace, degraded_out=degraded, _prepared_queries=inputs,
    )
    assert calls == []
    assert probes == ([] if disabled else [True])
    assert "clip" not in degraded
    assert trace.lane_profiles["clip"]["status"] == ("disabled" if disabled else "non_applicable")
    assert trace.lane_profiles["clip"]["reason"] == ("clip_disabled" if disabled else "no_image_vectors")
    assert not embeddings.get_clip_index(vault).path.exists()


def test_clip_rechecks_empty_index_then_retrieves_inserted_image(vault, monkeypatch):
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    monkeypatch.setenv("EXOMEM_FIND_CACHE_SIZE", "0")
    calls = []
    vector = np.zeros(embeddings.CLIP_DIM, dtype=np.float32)
    vector[0] = 1
    monkeypatch.setattr(embeddings, "embed_clip_text", lambda _q: calls.append(True) or vector)
    first = find_candidates.prepare_query_inputs(vault, "unusual scene", unit=False, page=True)
    assert first.clip is not None
    assert calls == []
    image = preserve.preserve_bytes(
        vault, scope="Rental", category="photos", filename="scene.jpg", data=b"image", text="a photograph"
    )
    index = embeddings.get_clip_index(vault)
    index.upsert(image.path, vector, 1)
    probes = []
    real_probe = index.has_vectors
    monkeypatch.setattr(index, "has_vectors", lambda: probes.append(True) or real_probe())
    trace = RetrievalTrace("hybrid", "page", False, False)
    hits = find_module.find(vault, query="unusual scene", result_level="page", rerank=False, retrieval_trace=trace)
    assert any(hit.path == image.sidecar_path for hit in hits), ([hit.path for hit in hits], trace.lane_profiles, calls)
    assert trace.lane_profiles["clip"]["status"] == "participated"
    assert calls == [True]
    assert probes == [True]


def test_clip_probe_surfaces_corrupt_storage_without_loading_matrix(vault, monkeypatch):
    index = embeddings.ClipIndex(vault)
    index.path.write_bytes(b"broken sqlite file")
    with pytest.raises(sqlite3.DatabaseError):
        index.has_vectors()
    assert index._cache is None


def test_idle_clip_cache_warm_does_not_search(vault, monkeypatch):
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_WARMUP", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    index = embeddings.get_clip_index(vault)
    monkeypatch.setattr(index, "search", lambda *_a, **_k: pytest.fail("empty CLIP search"))
    durations = warmup.warm_caches(vault, preload_models=True, preload_cpu_caches=True)
    assert "clip_matrix" not in durations


@pytest.mark.parametrize("disabled", [False, True])
def test_idle_clip_preload_finishes_readiness_without_loading(vault, monkeypatch, disabled):
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    if not disabled:
        monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    monkeypatch.setattr(warmup, "warm_caches", lambda *_a, **_k: {})
    monkeypatch.setattr(embeddings, "get_model", lambda: object())
    monkeypatch.setattr(embeddings, "get_reranker", lambda: object())
    monkeypatch.setattr(embeddings, "get_clip_query_model", lambda: pytest.fail("idle model preload"))
    readiness.begin_warm()
    durations = warmup.warm_all(vault)
    assert readiness.is_ready("clip")
    assert "model_clip" not in durations
    assert not embeddings.get_clip_index(vault).path.exists()


def test_clip_storage_error_is_failed_even_while_models_warm(vault, monkeypatch):
    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    monkeypatch.setenv("EXOMEM_FIND_CACHE_SIZE", "0")
    index = embeddings.get_clip_index(vault)
    index.path.write_bytes(b"broken sqlite file")
    monkeypatch.setattr(embeddings, "embed_clip_text", lambda _q: pytest.fail("corrupt store cannot encode"))
    readiness.begin_warm()
    readiness.mark_ready("retrieval_catalog")
    trace = RetrievalTrace("hybrid", "page", False, False)
    failed = []
    find_module.find(
        vault, query="metabolism", result_level="page", rerank=False,
        retrieval_trace=trace, failed_out=failed,
    )
    assert trace.lane_profiles["clip"]["status"] == "failed"
    assert failed == ["clip"]


@pytest.mark.parametrize("populated", [False, True])
def test_direct_candidate_lane_uses_the_same_image_eligibility(vault, monkeypatch, populated):
    from exomem.ranking_config import DEFAULT_RANKING

    monkeypatch.delenv("EXOMEM_DISABLE_CLIP", raising=False)
    vector = np.zeros(embeddings.CLIP_DIM, dtype=np.float32)
    vector[0] = 1
    calls = []
    monkeypatch.setattr(embeddings, "embed_clip_text", lambda _q: calls.append(True) or vector)
    paths = set()
    if populated:
        image = preserve.preserve_bytes(
            vault, scope="Rental", category="photos", filename="direct.jpg", data=b"image", text="a photograph"
        )
        paths.add(image.sidecar_path)
        embeddings.get_clip_index(vault).upsert(image.path, vector, 1)
    bundle = find_candidates.collect_candidates(
        vault, query="unusual scene", query_norm="unusual scene", limit=10,
        scope="kb", mode="vector", graph=False, temporal=False, intent="conceptual",
        prefer_compiled=False, prefer_active=False, prefer_used=False,
        config=DEFAULT_RANKING, timings=None, snapshot=find_module.FreshnessSnapshot(vault),
        page_of=lambda _path: None,
        keyword_match_paths=lambda *_a, **_k: [], outbound_wikilink_paths=lambda *_a, **_k: [],
        get_query_resolver=lambda *_a, **_k: None, record_degradation=lambda _lane: None,
        degraded_out=[], failed_out=[], recall_paths=paths, capture_trace=True,
    )
    assert bundle.clip_ranking == list(paths)
    assert bundle.lane_statuses["clip"]["status"] == ("participated" if populated else "non_applicable")
    assert calls == ([True] if populated else [])
