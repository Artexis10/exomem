"""Neither a read nor a write request catches the embedding matrix up; the background loader does.

OpenSpec: bound-request-latency-across-surfaces (live-index-freshness), task 3.10.

A committed batch hands the catch-up of the resident matrix to the one
background loader. When a matrix still lags the sidecar (another writer, a
batch in flight), a managed request serves it as a lagging view and starts the
same catch-up. A view never serves rows or text of a path changed after the
matrix, and a different recall-policy identity still refuses the matrix.
Assertions are by thread and by returned rows, never by wall-clock. All
fabricated vectors: no torch/model.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
import pytest

from exomem import call_spans, embedding_index, embeddings, recall_policy, recall_space


@pytest.fixture(autouse=True)
def _clean_memo() -> None:
    embeddings.clear_embedding_indexes()
    yield
    embeddings.clear_embedding_indexes()


def _pad(vals: list[float]) -> np.ndarray:
    out = np.zeros(embeddings.VECTOR_DIM, dtype=np.float32)
    out[: len(vals)] = vals
    return out


def _mat(*rows: list[float]) -> np.ndarray:
    return np.stack([_pad(r) for r in rows], axis=0)


def _warm_index(tmp_path: Path) -> embeddings.EmbeddingIndex:
    """The shared index with a.md, b.md and c.md written and the matrix resident."""
    vault = tmp_path / "vault"
    (vault / "Knowledge Base").mkdir(parents=True)
    idx = embeddings.get_embedding_index(vault)
    idx.upsert_file("a.md", ["old a"], _mat([1, 0]), 1.0)
    idx.upsert_file("b.md", ["b"], _mat([0.9, 0.1]), 1.0)
    idx.upsert_file("c.md", ["c"], _mat([0, 1]), 1.0)
    idx.all_vectors()
    return idx


def _lag_behind_another_writer(idx: embeddings.EmbeddingIndex, path: str, text: str, vec: list[float]) -> None:
    """Another process's write: the sidecar moves on, the resident matrix does not."""
    embeddings.EmbeddingIndex(idx.vault_root).upsert_file(path, [text], _mat(vec), 2.0)


def _settle(idx: embeddings.EmbeddingIndex) -> None:
    loading = idx._matrix_load
    if loading is not None:
        loading.join(timeout=30)
        assert not loading.is_alive()


def _commit_batch_and_settle(idx: embeddings.EmbeddingIndex, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Commit one batch from a drain-named thread; the threads that ran a catch-up, once settled."""
    catch_up_threads: list[str] = []
    real_catch_up = idx._catch_up_cache

    def recording(c, fields):
        catch_up_threads.append(threading.current_thread().name)
        return real_catch_up(c, fields)

    monkeypatch.setattr(idx, "_catch_up_cache", recording)
    drain = threading.Thread(
        target=idx.upsert_batch,
        args=([("d.md", ["d"], _mat([0, 2]), 2.0)],),
        kwargs={"identity": idx.identity},
        name="derived-drain",
    )
    drain.start()
    drain.join(timeout=30)
    assert not drain.is_alive()
    _settle(idx)
    return catch_up_threads


def test_a_committed_batch_is_carried_into_the_matrix_by_the_background_load_not_the_next_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idx = _warm_index(tmp_path)
    allowed = {"a.md", "b.md", "c.md", "d.md"}
    idx.search(_pad([1, 0]), 1, allowed_paths=allowed, defer_cold_load=True)
    catch_up_threads = _commit_batch_and_settle(idx, monkeypatch)

    token = "request-after-batch"
    handle = call_spans.MCP_CALL_TOKEN.set(token)
    try:
        resident = idx.require_resident_matrix()
        hits = idx.search(_pad([0, 1]), 1, allowed_paths=allowed, defer_cold_load=True)
        request_spans = {row["name"] for row in call_spans.pop_call_spans(token)}
    finally:
        call_spans.pop_call_spans(token)
        call_spans.MCP_CALL_TOKEN.reset(handle)

    assert catch_up_threads == ["exomem-matrix-load"]
    assert "embeddings.matrix_catch_up" not in request_spans
    assert resident.stale_paths is None
    assert [hit[0] for hit in hits] == ["d.md"]
    # The catch-up rebuilt the mask for the new metadata, so the request pays no row pass.
    assert idx._mask_cache.metadata is resident.metadata


def test_a_caller_of_upsert_batch_returns_without_running_the_catch_up_on_its_own_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Write requests reach upsert_batch through index_sync.upsert_after_write."""
    idx = _warm_index(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    catch_up_threads: list[str] = []
    real_catch_up = idx._catch_up_cache

    def held(c, fields):
        catch_up_threads.append(threading.current_thread().name)
        entered.set()
        release.wait(timeout=30)
        return real_catch_up(c, fields)

    monkeypatch.setattr(idx, "_catch_up_cache", held)
    writer = threading.Thread(
        target=idx.upsert_batch,
        args=([("d.md", ["d"], _mat([0, 2]), 2.0)],),
        kwargs={"identity": idx.identity},
        name="write-request",
    )
    try:
        writer.start()
        assert entered.wait(timeout=10)
        writer.join(timeout=5)
        writer_returned = not writer.is_alive()
    finally:
        release.set()
    writer.join(timeout=30)
    _settle(idx)

    assert writer_returned
    assert catch_up_threads == ["exomem-matrix-load"]
    assert idx.require_resident_matrix().stale_paths is None


@pytest.mark.parametrize("state", ["cold", "beyond-catch-up-bound"])
def test_a_batch_starts_no_background_load_for_a_matrix_it_cannot_carry_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    """A write must not begin a vault-sized read; the next read request starts that load."""
    idx = _warm_index(tmp_path)
    if state == "cold":
        idx.unload_cache()
    else:
        monkeypatch.setattr(embedding_index, "CATCHUP_MAX_PATHS", 0)
    started: list[str] = []
    monkeypatch.setattr(idx, "start_corpus_matrix_load", lambda: started.append(state))

    idx.upsert_batch([("d.md", ["d"], _mat([0, 2]), 2.0)], identity=idx.identity)

    assert started == []


def test_a_lagging_matrix_never_serves_rows_or_text_of_a_path_changed_since_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idx = _warm_index(tmp_path)
    idx._vec_failed = True  # an unfiltered search scans the matrix, not the vec0 mirror
    monkeypatch.setattr(idx, "start_corpus_matrix_load", lambda: None)  # hold the lag for the whole test
    _lag_behind_another_writer(idx, "a.md", "new a", [0, 1])
    query = _pad([1, 0])
    allowed = {"a.md", "b.md", "c.md"}

    passages = idx.select_chunks(query, 3, allowed_paths=allowed, defer_cold_load=True)
    eager = idx.search(query, 3, allowed_paths=allowed, defer_cold_load=True)
    unfiltered = idx.search(query, 3, defer_cold_load=True)
    many = idx.search_many(
        query[None, :], 3, admits=lambda path: path in allowed, defer_cold_load=True
    )[0]

    proof = passages[0][2].proof
    assert {path for path, *_rest in passages} == {"b.md", "c.md"}
    assert idx._texts_for([("b.md", 0)], proof=proof) == {("b.md", 0): "b"}
    assert idx._texts_for([("a.md", 0)], proof=proof) == {}
    assert {(path, text) for path, _chunk, text, _score in eager} == {("b.md", "b"), ("c.md", "c")}
    assert {path for path, *_rest in unfiltered} == {"b.md", "c.md"}
    assert {path for path, _chunk, _score in many} == {"b.md", "c.md"}
    # A lagging matrix is served as lagging: it is never reported current.
    assert idx.resident_matrix() is None
    assert idx.require_resident_matrix().stale_paths == frozenset({"a.md"})


def test_a_policy_identity_change_still_refuses_a_lagging_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idx = _warm_index(tmp_path)
    _lag_behind_another_writer(idx, "a.md", "new a", [0, 1])
    with monkeypatch.context() as held:
        held.setattr(idx, "start_corpus_matrix_load", lambda: None)
        assert idx.require_resident_matrix().stale_paths == frozenset({"a.md"})
    loads: list[str] = []
    real_load = idx._load_all_rows

    def counted_load(*args, **kwargs):
        loads.append(threading.current_thread().name)
        return real_load(*args, **kwargs)

    monkeypatch.setattr(idx, "_load_all_rows", counted_load)
    other_policy = ("other-policy-version", "other-access-fingerprint")
    monkeypatch.setattr(recall_policy, "recall_policy_identity", lambda _root: other_policy)

    with pytest.raises(recall_space.CorpusMatrixCold):
        idx.require_resident_matrix()
    _settle(idx)
    reloaded = idx.require_resident_matrix()

    assert loads == ["exomem-matrix-load"]
    assert reloaded.recall_policy_identity == other_policy
    assert reloaded.stale_paths is None


def test_concurrent_requests_during_a_lag_start_one_catch_up_and_none_waits_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    idx = _warm_index(tmp_path)
    _lag_behind_another_writer(idx, "a.md", "new a", [0, 1])
    entered = threading.Event()
    release = threading.Event()
    catch_up_threads: list[str] = []
    real_catch_up = idx._catch_up_cache

    def held(c, fields):
        catch_up_threads.append(threading.current_thread().name)
        entered.set()
        release.wait(timeout=30)
        return real_catch_up(c, fields)

    monkeypatch.setattr(idx, "_catch_up_cache", held)
    served: list[frozenset[str] | None] = []
    failures: list[BaseException] = []

    def request() -> None:
        try:
            served.append(idx.require_resident_matrix().stale_paths)
        except BaseException as error:  # noqa: BLE001 — the test reports it
            failures.append(error)

    requests = [threading.Thread(target=request, name=f"request-{n}") for n in range(6)]
    try:
        for thread in requests:
            thread.start()
        assert entered.wait(timeout=10)
        deadline = time.monotonic() + 5
        for thread in requests:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        waiting = [thread.name for thread in requests if thread.is_alive()]
    finally:
        release.set()
    for thread in requests:
        thread.join(timeout=30)
    _settle(idx)

    assert waiting == []
    assert failures == []
    assert served == [frozenset({"a.md"})] * 6
    assert catch_up_threads == ["exomem-matrix-load"]
    assert idx.require_resident_matrix().stale_paths is None


def test_a_lagging_request_never_waits_for_a_reader_that_holds_the_cache_lock(
    tmp_path: Path,
) -> None:
    """A full load holds the cache lock while it reads every row.

    A lagging request connects to read the change log, inside the store's
    coordination scope. If that connection waited there for the cache lock, the
    request would stall behind the load, and every other connection to the
    store would stall behind the request until MUTATION_BUSY.
    """
    idx = _warm_index(tmp_path)
    _lag_behind_another_writer(idx, "a.md", "new a", [0, 1])
    served: list[frozenset[str] | None] = []
    request = threading.Thread(
        target=lambda: served.append(idx.require_resident_matrix().stale_paths), name="request"
    )
    with idx._lock:  # a reader in the middle of a full load
        request.start()
        request.join(timeout=10)
        waited = request.is_alive()
    request.join(timeout=30)
    _settle(idx)

    assert not waited
    assert served == [frozenset({"a.md"})]
    assert idx.require_resident_matrix().stale_paths is None


def test_a_write_that_commits_during_the_background_catch_up_is_carried_without_a_full_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The swapped matrix lags that write by one path, which is within the catch-up bound."""
    idx = _warm_index(tmp_path)
    real_catch_up = idx._catch_up_cache
    writers: list[threading.Thread] = []

    def write_during(c, fields):
        patched = real_catch_up(c, fields)
        if not writers:
            # Another request's write commits while the loader builds its replacement.
            writer = threading.Thread(
                target=idx.upsert_batch,
                args=([("e.md", ["e"], _mat([0, 3]), 2.0)],),
                kwargs={"identity": idx.identity},
                name="write-request",
            )
            writers.append(writer)
            writer.start()
            writer.join(timeout=30)
        return patched

    loads: list[str] = []
    real_load = idx._load_all_rows

    def counted_load(*args, **kwargs):
        loads.append(threading.current_thread().name)
        return real_load(*args, **kwargs)

    monkeypatch.setattr(idx, "_catch_up_cache", write_during)
    monkeypatch.setattr(idx, "_load_all_rows", counted_load)
    idx.upsert_batch([("d.md", ["d"], _mat([0, 2]), 2.0)], identity=idx.identity)
    _settle(idx)
    resident = idx.require_resident_matrix()

    assert [writer.is_alive() for writer in writers] == [False]
    assert loads == []
    assert resident.stale_paths is None
    assert {path for path, _chunk in resident.metadata} == {"a.md", "b.md", "c.md", "d.md", "e.md"}
