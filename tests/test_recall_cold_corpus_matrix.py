"""The service never loads the whole corpus matrix on a recall request's thread.

On a long-running service a cold corpus matrix took 2-203 s to load, paid by
whichever ask arrived first after a promotion or an idle reap. The service now
answers that request from its other lanes, reports the dense lane as
`matrix_warming`, and loads the matrix once in the background for the requests
after it. A one-shot caller has no later request, so it keeps the inline load.
"""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
import pytest

from exomem import commands, embeddings, lexstore, readiness, warmup

_PAGE = "Knowledge Base/Sources/Sessions/2026-05-05-metabolism-curriculum-design.md"


def _seed_page_vector(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Store one vector for the page and encode every query to that vector."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(readiness, "should_defer", lambda _component: False)
    vector = np.zeros(embeddings.VECTOR_DIM, dtype=np.float32)
    vector[0] = 1.0
    monkeypatch.setattr(embeddings, "embed_texts", lambda _texts, *, is_query: [vector])
    embeddings.get_embedding_index(vault).upsert_file(
        _PAGE, ["metabolism curriculum"], vector[None, :], 1.0
    )


def _vector_lane(vault: Path, query: str) -> dict:
    explained = commands.op_ask_memory(
        vault,
        query=query,
        mode="hybrid",
        graph=False,
        rerank=False,
        scope="kb-only",
        detail="compact",
        explain=True,
    )
    return explained["retrieval_profile"]["lanes"]["vector"]


@pytest.fixture
def managed_vault(vault: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("EXOMEM_EAGER_BOOT", "1")
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "auto")
    _seed_page_vector(vault, monkeypatch)
    lexstore.ensure_fresh(vault)
    readiness.manage_runtime()
    readiness.begin_warm()
    try:
        warmup.warm_retrieval_catalog(vault)
    finally:
        readiness.finish_warm()
    yield vault
    readiness.reset()


def test_the_service_loads_a_cold_matrix_once_in_the_background(
    managed_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = embeddings.get_embedding_index(managed_vault)
    index.unload_cache()
    release = threading.Event()
    loads: list[str] = []
    real_load = index._load_all_rows

    def held_load():
        loads.append(threading.current_thread().name)
        release.wait(timeout=30)
        return real_load()

    monkeypatch.setattr(index, "_load_all_rows", held_load)

    # Distinct queries, so no result cache answers a later call.
    first = _vector_lane(managed_vault, "metabolism")
    second = _vector_lane(managed_vault, "metabolism curriculum")
    release.set()
    index._matrix_load.join(timeout=30)
    third = _vector_lane(managed_vault, "curriculum design")

    assert (first["status"], first["reason"]) == ("warming", "matrix_warming")
    assert (second["status"], second["reason"]) == ("warming", "matrix_warming")
    assert loads == ["exomem-matrix-load"]
    assert third["status"] == "participated"


def test_a_one_shot_caller_keeps_the_dense_lane_by_loading_inline(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_page_vector(vault, monkeypatch)
    loads: list[str] = []
    index = embeddings.get_embedding_index(vault)
    real_load = index._load_all_rows

    def counted_load():
        loads.append(threading.current_thread().name)
        return real_load()

    monkeypatch.setattr(index, "_load_all_rows", counted_load)

    lane = _vector_lane(vault, "metabolism")

    assert lane["status"] == "participated"
    assert loads == [threading.current_thread().name]
