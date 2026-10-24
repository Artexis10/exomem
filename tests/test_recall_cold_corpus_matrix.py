"""The service never loads the whole corpus matrix on a recall request's thread.

On a long-running service a cold corpus matrix took 2-203 s to load, paid by
whichever ask arrived first after a promotion or an idle reap. The service now
answers that request from its other lanes, reports the dense lane as
`matrix_warming`, and loads the matrix once in the background, off the index
lock, for the requests after it. A one-shot caller has no later request, so it
keeps the inline load.
"""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
import pytest

from exomem import commands, embedding_index, embeddings, lexstore, memory_refs, readiness, warmup

_PAGE = "Knowledge Base/Sources/Sessions/2026-05-05-metabolism-curriculum-design.md"
_OTHER_PAGE = "Knowledge Base/Sources/Sessions/2026-05-06-later-session.md"


def _unit_vector() -> np.ndarray:
    vector = np.zeros(embeddings.VECTOR_DIM, dtype=np.float32)
    vector[0] = 1.0
    return vector


def _seed_page_vector(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Store one vector for the page and encode every query to that vector."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(readiness, "should_defer", lambda _component: False)
    vector = _unit_vector()
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


def _count_loads(index: embeddings.EmbeddingIndex, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the thread of every full matrix load."""
    loads: list[str] = []
    real_load = index._load_all_rows

    def counted_load():
        loads.append(threading.current_thread().name)
        return real_load()

    monkeypatch.setattr(index, "_load_all_rows", counted_load)
    return loads


def _settle(index: embeddings.EmbeddingIndex) -> None:
    """Wait for the background load a recall started; starting one here would add a load."""
    loading = index._matrix_load
    if loading is not None:
        loading.join(timeout=30)


@pytest.fixture
def managed_vault(vault: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("EXOMEM_EAGER_BOOT", "1")
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "auto")
    _seed_page_vector(vault, monkeypatch)
    lexstore.ensure_fresh(vault)
    # A managed recall never builds the reference sidecar on its own thread.
    memory_refs.ReferenceIndex(vault).rebuild_all()
    readiness.manage_runtime()
    readiness.begin_warm()
    try:
        warmup.warm_retrieval_catalog(vault)
    finally:
        readiness.finish_warm()
    yield vault
    readiness.reset()


def test_the_service_loads_a_cold_matrix_once_in_the_background_without_stalling_a_save(
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
    # A save's cache patch takes the index lock; the load must not hold it.
    save = threading.Thread(
        target=index.upsert_file,
        args=(_OTHER_PAGE, ["later session"], _unit_vector()[None, :], 2.0),
    )
    save.start()
    save.join(timeout=10)
    saved_during_load = not save.is_alive()
    release.set()
    _settle(index)
    third = _vector_lane(managed_vault, "curriculum design")

    assert (first["status"], first["reason"]) == ("warming", "matrix_warming")
    assert (second["status"], second["reason"]) == ("warming", "matrix_warming")
    assert saved_during_load
    assert loads == ["exomem-matrix-load"]
    assert third["status"] == "participated"


@pytest.mark.parametrize(
    "path_bound,expected",
    [
        (embedding_index.CATCHUP_MAX_PATHS, ("participated", None, [])),
        (0, ("warming", "matrix_warming", ["exomem-matrix-load"])),
    ],
    ids=["within-catch-up-bound", "beyond-catch-up-bound"],
)
def test_a_matrix_behind_another_writer_is_served_while_it_catches_up_or_loads_in_the_background(
    managed_vault: Path, monkeypatch: pytest.MonkeyPatch, path_bound: int, expected: tuple
) -> None:
    """Within the bound the request serves the lagging matrix and the background catches it up.

    Beyond the bound the matrix is too far behind to serve, so the request warms and
    the one background load reads the sidecar whole. Neither runs on the request thread.
    """
    index = embeddings.get_embedding_index(managed_vault)
    index.all_vectors()
    # Another instance's write leaves the resident matrix one path behind.
    embeddings.EmbeddingIndex(managed_vault).upsert_file(
        _OTHER_PAGE, ["later session"], _unit_vector()[None, :], 2.0
    )
    monkeypatch.setattr(embedding_index, "CATCHUP_MAX_PATHS", path_bound)
    loads = _count_loads(index, monkeypatch)

    lane = _vector_lane(managed_vault, "metabolism")
    _settle(index)

    assert (lane["status"], lane.get("reason"), loads) == expected


def test_a_failed_background_load_reports_the_lane_failed_without_retrying_at_once(
    managed_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = embeddings.get_embedding_index(managed_vault)
    index.unload_cache()
    # A backoff no test run can outlast, so the second recall falls inside it.
    monkeypatch.setattr(embedding_index, "MATRIX_LOAD_RETRY_SECONDS", 3600.0)
    loads: list[str] = []

    def failing_load():
        loads.append(threading.current_thread().name)
        raise OSError("sidecar unreadable")

    monkeypatch.setattr(index, "_load_all_rows", failing_load)

    first = _vector_lane(managed_vault, "metabolism")
    _settle(index)
    loads_before_second = len(loads)
    second = _vector_lane(managed_vault, "metabolism curriculum")

    assert (first["status"], first["reason"]) == ("warming", "matrix_warming")
    assert second["status"] == "failed"
    assert len(loads) == loads_before_second


def test_an_inline_reader_waits_for_the_background_load_instead_of_reading_again(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second full read would hold two corpus matrices at once (about 950 MB here)."""
    _seed_page_vector(vault, monkeypatch)
    index = embeddings.get_embedding_index(vault)
    index.unload_cache()
    release = threading.Event()
    loads: list[str] = []
    real_load = index._load_all_rows

    def held_load():
        loads.append(threading.current_thread().name)
        release.wait(timeout=30)
        return real_load()

    monkeypatch.setattr(index, "_load_all_rows", held_load)
    loading = index.start_corpus_matrix_load()
    # An audit, or an offline caller's write advisory, reads the matrix inline while the load runs.
    reader = threading.Thread(target=index.all_vectors, name="inline-reader")
    reader.start()
    reader.join(timeout=1)
    release.set()
    reader.join(timeout=30)
    loading.join(timeout=30)

    assert loads == ["exomem-matrix-load"]


def test_a_managed_pack_raises_instead_of_loading_a_cold_matrix(
    managed_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deep ask's pack waited on the very load its own find had just started."""
    from exomem import corpus_aware, recall_space

    index = embeddings.get_embedding_index(managed_vault)
    index.upsert_file(_OTHER_PAGE, ["later session"], _unit_vector()[None, :], 1.0)
    index.unload_cache()
    loads = _count_loads(index, monkeypatch)
    pages = [(_PAGE, ["metabolism curriculum"]), (_OTHER_PAGE, ["later session"])]

    with pytest.raises(recall_space.CorpusMatrixCold):
        corpus_aware.pairwise_best_cosine_from_sidecar(managed_vault, pages)
    _settle(index)
    after_load = corpus_aware.pairwise_best_cosine_from_sidecar(managed_vault, pages)

    assert loads == ["exomem-matrix-load"]
    assert after_load[1] == {_PAGE, _OTHER_PAGE}


def test_a_one_shot_caller_keeps_the_dense_lane_by_loading_inline(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_page_vector(vault, monkeypatch)
    loads = _count_loads(embeddings.get_embedding_index(vault), monkeypatch)

    lane = _vector_lane(vault, "metabolism")

    assert lane["status"] == "participated"
    assert loads == [threading.current_thread().name]


def test_a_save_skips_its_overlap_advisory_instead_of_loading_a_cold_matrix(
    managed_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A save that arrived first after a promotion paid the whole load, about 100 s here."""
    from exomem import corpus_aware

    # The encoder is stubbed (`_seed_page_vector`); only the matrix is cold here.
    monkeypatch.setattr(embeddings, "advisory_passages_fit", lambda _texts: True)
    index = embeddings.get_embedding_index(managed_vault)
    index.unload_cache()
    loads = _count_loads(index, monkeypatch)

    during_load = corpus_aware._best_cosine_per_file(
        managed_vault, title="Metabolism", body="metabolism curriculum"
    )
    _settle(index)
    after_load = corpus_aware._best_cosine_per_file(
        managed_vault, title="Metabolism", body="metabolism curriculum"
    )

    assert during_load == {}
    assert loads == ["exomem-matrix-load"]
    assert after_load[_PAGE] > 0.99
