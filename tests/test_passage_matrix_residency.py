"""Optional passage residency preserves current ranking under bounded memory."""

import gc
import threading

import numpy as np
import pytest

from exomem import (
    cloud_cell,
    embedding_index,
    index_paths,
    passage_matrix,
    recall_space,
    runtime_resources,
)


@pytest.fixture
def resident(tmp_path, monkeypatch):
    (tmp_path / "Knowledge Base").mkdir()
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_RESOURCE_POLICY", "service-v1")
    monkeypatch.setenv("EXOMEM_VEC_BACKEND", "numpy")
    monkeypatch.setenv("EXOMEM_PASSAGE_MATRIX_BYTES", str(1024 * 1024))
    monkeypatch.setattr(runtime_resources, "available_memory", lambda: 1024 * 1024 * 1024)
    monkeypatch.setattr(runtime_resources, "cgroup_memory_headroom", lambda: None)
    monkeypatch.setattr(passage_matrix, "_BUDGET", passage_matrix.MatrixBudget())
    index = embedding_index.EmbeddingIndex(tmp_path)
    yield index
    loading = index._matrix_loading()
    if loading is not None:
        loading.join(5)
        assert not loading.is_alive()
    index.unload_cache()


def put(index, path, rows):
    vectors = np.asarray(rows, dtype=np.float32)
    index.upsert_file(path, [f"{path}/{n}" for n in range(len(rows))], vectors, 1)


def prepare(index):
    thread = index.prepare_passage_matrix()
    assert thread is not None
    thread.join(5)
    assert not thread.is_alive()


def test_current_resident_selection_preserves_disk_search_and_transient_audit(resident, monkeypatch):
    """An ordinary read must not discard the cache used by the next passage request."""
    put(resident, "one.md", [[1, 0], [.8, .6]])
    put(resident, "denied.md", [[1, 0]])
    prepare(resident)
    cached = resident._cache
    assert cached is not None
    query = np.array([1, 0], dtype=np.float32)
    selected = resident.select_passage_chunks(query, 2, allowed_paths={"one.md"})
    assert selected.scoring_mode == "resident"
    assert [(p, n) for p, n, _text, _score in selected.hits] == [("one.md", 0), ("one.md", 1)]
    assert all(isinstance(row[2], embedding_index.ChunkRef) for row in selected.hits)
    ordinary = resident.search(query, 2, allowed_paths={"one.md"})
    assert all(isinstance(row[2], str) for row in ordinary)
    assert len(resident.search_many(np.array([query]), 1, admits=lambda p: p == "one.md")) == 1
    assert len(resident.all_vectors()[0]) == 3
    assert resident._cache is cached
    assert cloud_cell.resource_policy() == "service-v1"
    resident.unload_cache()
    assert resident._cache is None


def test_lagging_matrix_uses_new_best_row_on_disk_while_refresh_waits(resident, monkeypatch):
    """Masking stale paths would omit a newly eligible winner and change top-k."""
    put(resident, "old.md", [[.6, .8]])
    prepare(resident)
    old = resident._cache
    monkeypatch.setattr(resident, "_start_matrix_catch_up", lambda: None)
    put(resident, "new.md", [[1, 0]])
    assert resident._cache is old
    result = resident.select_passage_chunks(np.array([1, 0]), 1)
    assert result.scoring_mode == "disk"
    assert result.hits[0][0] == "new.md"
    prepare(resident)
    assert resident.select_passage_chunks(np.array([1, 0]), 1).scoring_mode == "resident"
    resident.purge_paths_if_present(["new.md"])
    prepare(resident)
    result = resident.select_passage_chunks(np.array([1, 0]), 1)
    assert result.scoring_mode == "resident"
    assert result.hits[0][0] == "old.md"


@pytest.mark.parametrize("capacity", ["0", "1"])
def test_unadmitted_cache_keeps_disk_available_including_empty_results(resident, monkeypatch, capacity):
    """Zero or insufficient capacity must never turn optional residency into an outage."""
    put(resident, "one.md", [[1, 0]])
    monkeypatch.setenv("EXOMEM_PASSAGE_MATRIX_BYTES", capacity)
    monkeypatch.setattr(resident, "_all_vector_snapshot", lambda: pytest.fail("request full load"))
    result = resident.select_passage_chunks(np.array([1, 0]), 1)
    assert result.scoring_mode == "disk"
    assert result.hits[0][0] == "one.md"
    loading = resident._matrix_loading()
    if loading is not None:
        loading.join(5)
    assert resident._cache is None
    empty = resident.select_passage_chunks(np.array([1, 0]), 1, allowed_paths=set())
    assert empty == embedding_index.ChunkSelection([], "disk")


def test_reader_held_generation_blocks_rollover_until_array_dies(resident, monkeypatch):
    """A retired array still owned by a reader must count against another index's ceiling."""
    put(resident, "one.md", [[1, 0]] * 8)
    prepare(resident)
    held = resident._cache.matrix
    monkeypatch.setenv("EXOMEM_PASSAGE_MATRIX_BYTES", str(held.nbytes + held.shape[1] * 4))
    rollover = embedding_index.EmbeddingIndex(
        resident.vault_root,
        path=resident.path.with_name(index_paths.space_sidecar_name("replacement-space")),
    )
    put(rollover, "next.md", [[0, 1]] * 8)
    resident.unload_cache()
    prepare(rollover)
    assert rollover._cache is None
    assert passage_matrix._BUDGET.used_bytes >= held.nbytes
    del held
    gc.collect()
    monkeypatch.setattr(rollover, "_load_backing_off", lambda: False)
    prepare(rollover)
    assert rollover._cache is not None
    rollover.unload_cache()
    gc.collect()
    assert passage_matrix._BUDGET.used_bytes == 0


def test_stale_large_owner_retires_and_reloads_after_reader_release(resident, monkeypatch):
    """A matrix above half the ceiling must not permanently prevent its own replacement."""
    put(resident, "one.md", [[1, 0]] * 8)
    prepare(resident)
    held = resident._cache.matrix
    monkeypatch.setenv("EXOMEM_PASSAGE_MATRIX_BYTES", str(held.nbytes + held.shape[1] * 4))
    monkeypatch.setattr(resident, "_start_matrix_catch_up", lambda: None)
    put(resident, "one.md", [[0, 1]] * 8)
    prepare(resident)
    assert resident._cache is None
    assert resident.select_passage_chunks(np.array([0, 1]), 1).scoring_mode == "disk"
    del held
    gc.collect()
    monkeypatch.setattr(resident, "_load_backing_off", lambda: False)
    prepare(resident)
    result = resident.select_passage_chunks(np.array([0, 1]), 1)
    assert result.scoring_mode == "resident"
    assert result.hits[0][3] == pytest.approx(1)


def test_stale_owner_reloads_after_one_bounded_retirement_retry(resident, monkeypatch):
    """The loader must release its own reference before retrying, without waiting for a request."""
    put(resident, "one.md", [[1, 0]] * 8)
    prepare(resident)
    matrix_bytes = resident._cache.matrix.nbytes
    monkeypatch.setenv("EXOMEM_PASSAGE_MATRIX_BYTES", str(matrix_bytes + 8))
    monkeypatch.setattr(resident, "_start_matrix_catch_up", lambda: None)
    put(resident, "one.md", [[0, 1]] * 8)
    prepare(resident)
    result = resident.select_passage_chunks(np.array([0, 1]), 1)
    assert result.scoring_mode == "resident"
    assert result.hits[0][3] == pytest.approx(1)


def test_streaming_replacement_fits_when_incremental_scratch_does_not(resident, monkeypatch):
    """A large changed block must fall back before fetching blobs, while its reader stays admitted."""
    put(resident, "one.md", [[1, 0]] * 8)
    prepare(resident)
    held = resident._cache.matrix
    monkeypatch.setenv("EXOMEM_PASSAGE_MATRIX_BYTES", str(2 * held.nbytes + 8))
    monkeypatch.setattr(resident, "_start_matrix_catch_up", lambda: None)
    monkeypatch.setattr(np, "stack", lambda *_args, **_kwargs: pytest.fail("unadmitted replacement stack"))
    put(resident, "one.md", [[0, 1]] * 8)
    prepare(resident)
    assert resident.select_passage_chunks(np.array([0, 1]), 1).scoring_mode == "resident"
    assert passage_matrix._BUDGET.used_bytes == held.nbytes + resident._cache.matrix.nbytes


def test_service_write_completes_while_background_preparation_is_blocked(resident, monkeypatch):
    """Capture publication must not copy or await the corpus matrix on its writer thread."""
    put(resident, "one.md", [[1, 0]])
    prepare(resident)
    entered, release = threading.Event(), threading.Event()
    original = resident._catch_up_cache

    def blocked(*args, **kwargs):
        assert threading.current_thread() is not threading.main_thread()
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(resident, "_catch_up_cache", blocked)
    put(resident, "one.md", [[0, 1]])
    assert entered.wait(5)
    put(resident, "two.md", [[1, 0]])
    assert resident.select_passage_chunks(np.array([1, 0]), 1).hits[0][0] == "two.md"
    release.set()
    prepare(resident)
    assert resident.select_passage_chunks(np.array([1, 0]), 1).scoring_mode == "resident"


def test_simultaneous_loaders_cannot_spend_unmaterialized_headroom_twice(resident, monkeypatch):
    """Two admitted arrays must fit the same observed headroom before either allocates."""
    second = embedding_index.EmbeddingIndex(
        resident.vault_root,
        path=resident.path.with_name(index_paths.space_sidecar_name("another-space")),
    )
    put(resident, "one.md", [[1, 0]] * 128)
    put(second, "two.md", [[0, 1]] * 128)
    monkeypatch.setattr(runtime_resources, "available_memory", lambda: 40_000)
    entered, release = threading.Event(), threading.Event()
    allocate = np.empty

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return allocate(*args, **kwargs)

    monkeypatch.setattr(np, "empty", blocked)
    first_thread = resident.prepare_passage_matrix()
    assert entered.wait(5)
    second_thread = second.prepare_passage_matrix()
    second_thread.join(5)
    assert not second_thread.is_alive()
    assert second._cache is None
    release.set()
    first_thread.join(5)
    assert not first_thread.is_alive()
    assert resident._cache is not None


@pytest.mark.parametrize("already_loaded", [False, True])
def test_unload_during_blocked_preparation_cannot_resurrect_cache(resident, monkeypatch, already_loaded):
    """Pointer equality alone misses an eviction that leaves a cold slot at None."""
    put(resident, "one.md", [[1, 0]])
    if already_loaded:
        prepare(resident)
        monkeypatch.setattr(resident, "_start_matrix_catch_up", lambda: None)
        put(resident, "one.md", [[0, 1]])
    entered, release = threading.Event(), threading.Event()
    method = "_catch_up_cache" if already_loaded else "_load_all_rows"
    original = getattr(resident, method)

    def blocked(*args, **kwargs):
        prepared = original(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return prepared

    monkeypatch.setattr(resident, method, blocked)
    thread = resident.prepare_passage_matrix()
    assert entered.wait(5)
    resident.unload_cache()
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert resident._cache is None
    gc.collect()
    assert passage_matrix._BUDGET.used_bytes == 0


def test_unknown_headroom_and_failed_load_release_optional_reservations(resident, monkeypatch):
    """A missing capacity reading or corrupt stored vector must preserve disk mode honestly."""
    put(resident, "one.md", [[1, 0]])
    monkeypatch.setattr(runtime_resources, "available_memory", lambda: None)
    prepare(resident)
    assert resident._cache is None
    assert resident.select_passage_chunks(np.array([1, 0]), 1).scoring_mode == "disk"
    monkeypatch.setattr(runtime_resources, "available_memory", lambda: 1024 * 1024)
    monkeypatch.setattr(resident, "_load_backing_off", lambda: False)
    conn = resident._connect()
    with conn:
        conn.execute("UPDATE chunks SET vector = ?", (b"invalid",))
    conn.close()
    prepare(resident)
    assert resident._cache is None
    gc.collect()
    assert passage_matrix._BUDGET.used_bytes == 0


def test_space_publication_revokes_an_in_progress_old_space_load(resident, monkeypatch):
    """A fingerprint change without a chunk-generation bump must not resurrect old vectors."""
    put(resident, "one.md", [[1, 0]])
    old_identity = resident.identity
    new_identity = recall_space.SpaceIdentity(old_identity.model, "replacement-fingerprint", 2)
    entered, release = threading.Event(), threading.Event()
    original = resident._load_all_rows

    def blocked(*args, **kwargs):
        prepared = original(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return prepared

    monkeypatch.setattr(resident, "_load_all_rows", blocked)
    loading = resident.prepare_passage_matrix()
    assert entered.wait(5)
    conn = resident._connect()
    with conn:
        recall_space.write_identity(conn, new_identity)
    token = resident._build_token(conn)
    conn.close()
    resident._complete_publication(new_identity, token)
    release.set()
    loading.join(5)
    assert not loading.is_alive()
    assert resident._cache is None
    gc.collect()
    assert passage_matrix._BUDGET.used_bytes == 0
    prepare(resident)
    assert resident._cache.space == new_identity


@pytest.mark.parametrize("mutation", ["upsert", "purge"])
def test_local_writer_rechecks_admission_after_a_cold_load_publishes(resident, monkeypatch, mutation):
    """A writer that saw a cold slot must not copy a matrix admitted before it takes the lock."""
    put(resident, "one.md", [[1, 0]] * 4)
    put(resident, "two.md", [[1, 0]] * 4)
    monkeypatch.delenv("EXOMEM_CLOUD_CELL")
    monkeypatch.setenv("EXOMEM_CLOUD_RESOURCE_POLICY", "legacy")
    monkeypatch.setenv("EXOMEM_PASSAGE_MATRIX_BYTES", "72")
    prepared, publish = threading.Event(), threading.Event()
    before_lock, resume_writer = threading.Event(), threading.Event()
    original_load, original_lock = resident._load_all_rows, resident._lock
    writer = None
    errors = []

    class PublicationWindow:
        def __enter__(self):
            if threading.current_thread() is writer and not before_lock.is_set():
                before_lock.set()
                assert resume_writer.wait(5)
            return original_lock.__enter__()

        def __exit__(self, *args):
            return original_lock.__exit__(*args)

        def __getattr__(self, name):
            return getattr(original_lock, name)

    def paused_load(*args, **kwargs):
        loaded = original_load(*args, **kwargs)
        prepared.set()
        assert publish.wait(5)
        return loaded

    def write():
        try:
            if mutation == "upsert":
                put(resident, "one.md", [[0, 1]] * 4)
            else:
                resident.purge_paths_if_present(["one.md"])
        except BaseException as error:  # noqa: BLE001 — return thread failures to the test
            errors.append(error)

    monkeypatch.setattr(resident, "_lock", PublicationWindow())
    monkeypatch.setattr(resident, "_load_all_rows", paused_load)
    monkeypatch.setattr(resident, "_start_matrix_catch_up", lambda: None)
    loading = resident.prepare_passage_matrix()
    assert prepared.wait(5)
    writer = threading.Thread(target=write)
    writer.start()
    try:
        assert before_lock.wait(5)
        publish.set()
        loading.join(5)
        assert not loading.is_alive()
        held = resident._cache
        assert held is not None and held.passage_admitted
        resume_writer.set()
        writer.join(5)
        assert not writer.is_alive() and not errors
        assert resident._cache is held
        assert passage_matrix._BUDGET.used_bytes == held.matrix.nbytes
        assert held.matrix.nbytes <= passage_matrix.ceiling()
    finally:
        publish.set()
        resume_writer.set()
        writer.join(5)
        loading.join(5)


def test_streaming_load_releases_each_blob_before_fetching_the_next(resident, monkeypatch):
    """A tight admission must cover the real blob lifetime beside the final matrix."""
    put(resident, "one.md", [[1, 0]] * 8)
    monkeypatch.setenv("EXOMEM_PASSAGE_MATRIX_BYTES", "72")
    original_connect = resident._connect
    live_bytes, peak_bytes = 0, 0

    class Blob(bytes):
        def __new__(cls, value):
            nonlocal live_bytes, peak_bytes
            blob = super().__new__(cls, value)
            live_bytes += len(blob)
            peak_bytes = max(peak_bytes, live_bytes)
            return blob

        def __del__(self):
            nonlocal live_bytes
            live_bytes -= len(self)

    class Rows:
        def __init__(self, cursor):
            self.cursor = cursor

        def __iter__(self):
            return self

        def __next__(self):
            path, number, blob = next(self.cursor)
            return path, number, Blob(blob)

    class Connection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, *args):
            cursor = self.connection.execute(sql, *args)
            if sql == "SELECT file_path, chunk_idx, vector FROM chunks ORDER BY file_path, chunk_idx":
                return Rows(cursor)
            return cursor

        def __getattr__(self, name):
            return getattr(self.connection, name)

    monkeypatch.setattr(resident, "_connect", lambda: Connection(original_connect()))
    loaded = resident._load_all_rows(admit_passage=True)
    assert loaded.matrix.nbytes + peak_bytes <= passage_matrix.ceiling()
    assert passage_matrix._BUDGET.used_bytes == loaded.matrix.nbytes
    assert live_bytes == 0
