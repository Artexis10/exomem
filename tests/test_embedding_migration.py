"""Blue/green re-embedding of the recall sidecar into a new encoder's space.

When the recall encoder changes, the sidecar that serves recall was written by
the old one. Recall keeps serving from it, with the old encoder, while a
background job builds a sidecar for the new space beside it, path by path and
resumably. Writes made meanwhile go to the serving sidecar as always, and the
build picks them up in its catch-up. The new sidecar becomes the serving one by
one atomic swap of the active pointer; the old one is removed only by a later
start, so a failed cutover falls back to it. `EXOMEM_RECALL_REEMBED=off` keeps
the old sidecar serving and builds nothing. Models load on the warm-up and the
job's threads, never on a request thread.
"""

from __future__ import annotations

import os
import threading
import zlib
from pathlib import Path

import numpy as np
import pytest

from exomem import (
    embedding_backend,
    embeddings,
    index_paths,
    readiness,
    recall_migration,
    recall_space,
)
from exomem import find as find_module
from exomem.embedding_index import EmbeddingIndex
from exomem.kbdir import kb_dirname

OLD = "fake/old-space"
NEW = "fake/new-space"
_DIMS = {OLD: 8, NEW: 12}

_PAGES = {
    f"Notes/page-{index}.md": (title, body)
    for index, (title, body) in enumerate(
        [
            ("Retry with backoff", "Retries wait a growing delay so clients do not hammer a service."),
            ("Incident review", "The review found that clients did not wait for a recovering service."),
            ("Kitchen rota", "The kitchen rota lists who cleans the coffee machine each week."),
            ("Garden plan", "Tomatoes go in the south bed and beans along the fence."),
            ("Reading list", "Three books on distributed systems and one on typography."),
            ("Travel notes", "The night train leaves at nine and arrives before breakfast."),
        ]
    )
}


class _Model:
    """A resident encoder with a profile; logs every encode with its thread."""

    concurrent_encodes = False
    device = "cpu"

    def __init__(self, name: str, log: list[tuple[str, str, bool, str]]) -> None:
        self.name = name
        self._log = log
        self.profile = embedding_backend.EncoderProfile(
            model=name,
            pooling="cls",
            query_prefix="q: ",
            passage_prefix="",
            max_seq=512,
            pad_token="<pad>",
            revision="0" * 40,
            quantization="fake",
            file_format="fake",
            artifact_digest=name[-5:],
        )

    def encode(self, texts, **_kwargs) -> np.ndarray:
        dim = _DIMS[self.name]
        out = np.zeros((len(texts), dim), dtype=np.float32)
        for row, text in enumerate(texts):
            is_query = text.startswith("q: ")
            self._log.append((self.name, threading.current_thread().name, is_query, text))
            for word in text.removeprefix("q: ").lower().split():
                out[row, zlib.crc32(word.strip(".,").encode()) % dim] += 1.0
        out /= np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-9)
        return out

    def release(self) -> None:
        pass


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    vault = tmp_path / "vault"
    for rel, (title, body) in _PAGES.items():
        page = vault / kb_dirname() / rel
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(
            f"---\ntype: note\ntitle: {title}\nupdated: 2026-09-01\n---\n\n# {title}\n\n{body}\n",
            encoding="utf-8",
        )
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.delenv(recall_migration.REEMBED_ENV, raising=False)
    monkeypatch.setattr(embeddings, "_IMPORT_FAILED", False)
    monkeypatch.setattr(recall_migration, "BATCH_CHUNKS", 2)
    log: list[tuple[str, str, bool, str]] = []
    loads: list[tuple[str, str]] = []

    def load(name: str, **_kwargs) -> _Model:
        loads.append((name, threading.current_thread().name))
        return _Model(name, log)

    monkeypatch.setattr(embedding_backend, "load_encoder", load)
    readiness.reset()
    find_module.clear_cache()
    embeddings.clear_embedding_indexes()
    recall_migration.reset_for_tests()
    # The installed vault: a sidecar written by the old encoder.
    monkeypatch.setattr(embeddings, "MODEL_NAME", OLD)
    monkeypatch.setattr(embeddings, "_MODEL", None)
    embeddings.index_incremental(vault, log_fn=lambda _message: None)
    embeddings.unload_model()
    # The upgrade: recall now encodes with the new encoder.
    monkeypatch.setattr(embeddings, "MODEL_NAME", NEW)
    log.clear()
    loads.clear()
    yield vault, log, loads
    recall_space.unload_previous()
    embeddings.unload_model()
    embeddings.clear_embedding_indexes()
    recall_migration.reset_for_tests()
    find_module.clear_cache()
    readiness.reset()


def _warm(vault: Path) -> None:
    """What the service warm-up preloads, on a thread of its own."""

    def run() -> None:
        embeddings.get_model()
        recall_migration.preload_serving_encoder(vault)

    thread = threading.Thread(target=run, name="exomem-warm")
    thread.start()
    thread.join()


def _explained(vault: Path, query: str) -> dict:
    from exomem import commands

    find_module.clear_cache()
    return commands.op_ask_memory(
        vault,
        query=query,
        limit=10,
        mode="hybrid",
        scope="kb-only",
        graph=False,
        rerank=False,
        detail="compact",
        explain=True,
    )


def _vector_lane(vault: Path) -> dict:
    return _explained(vault, "retry backoff")["retrieval_profile"]["lanes"]["vector"]


def _query_encoders(log) -> list[str]:
    return [model for model, _thread, is_query, _text in log if is_query]


def _passages_by(log, model: str) -> list[str]:
    return [text for name, _thread, is_query, text in log if name == model and not is_query]


def _chunk_texts(vault: Path, path: Path | None = None) -> dict[str, list[str]]:
    index = EmbeddingIndex(vault, path=path) if path is not None else EmbeddingIndex(vault)
    return {
        f"{kb_dirname()}/{rel}": index.stored_chunks_for(f"{kb_dirname()}/{rel}")[0]
        for rel in _PAGES
    }


def test_dense_recall_serves_from_the_old_sidecar_until_the_cutover(world) -> None:
    vault, log, _loads = world
    _warm(vault)
    plan = recall_migration.plan(vault)
    assert plan is not None
    assert (plan.serving.model, plan.target.model) == (OLD, NEW)

    assert recall_migration.build(vault, plan) is True
    log.clear()
    assert _vector_lane(vault)["status"] == "participated"
    assert _query_encoders(log) == [OLD]
    assert index_paths.sidecar_path(vault) == index_paths.legacy_sidecar_path(vault)

    recall_migration.cut_over(vault, plan)

    assert index_paths.sidecar_path(vault).name == plan.shadow_path.name
    log.clear()
    assert _vector_lane(vault)["status"] == "participated"
    assert _query_encoders(log) == [NEW]
    assert embeddings.get_embedding_index(vault).identity.model == NEW
    # Nothing selects the old encoder any more, so it is not kept resident.
    assert recall_space.previous_resident(OLD) is None


def test_an_interrupted_build_resumes_without_re_encoding_done_paths(world) -> None:
    vault, log, _loads = world
    _warm(vault)
    plan = recall_migration.plan(vault)

    stopped = recall_migration.build(vault, plan, should_stop=lambda: bool(_passages_by(log, NEW)))
    assert stopped is False
    first = _passages_by(log, NEW)
    assert 0 < len(first) < len(_PAGES)

    assert recall_migration.build(vault, plan) is True
    encoded = _passages_by(log, NEW)
    every_chunk = [chunk for chunks in _chunk_texts(vault).values() for chunk in chunks]
    assert len(encoded) == len(set(encoded))
    assert sorted(encoded) == sorted(every_chunk)
    assert encoded[: len(first)] == first
    assert _chunk_texts(vault, plan.shadow_path) == _chunk_texts(vault)


def test_writes_during_the_build_appear_after_the_cutover(world) -> None:
    vault, log, _loads = world
    _warm(vault)
    plan = recall_migration.plan(vault)
    recall_migration.build(vault, plan, should_stop=lambda: bool(_passages_by(log, NEW)))
    built_first = _passages_by(log, NEW)[0]
    edited_rel = next(rel for rel, (title, _body) in _PAGES.items() if title in built_first)
    edited = vault / kb_dirname() / edited_rel
    edited.write_text(
        edited.read_text(encoding="utf-8") + "\nEdited during the build: a jittered pause.\n",
        encoding="utf-8",
    )
    added = vault / kb_dirname() / "Notes/page-new.md"
    added.write_text(
        "---\ntype: note\ntitle: Circuit breaker\nupdated: 2026-09-02\n---\n\n"
        "# Circuit breaker\n\nA breaker opens after repeated failures and probes later.\n",
        encoding="utf-8",
    )

    status = embeddings.upsert_after_write_status(vault, [edited, added])

    assert status.status == "completed"
    # The write went to the serving sidecar, in its own space.
    serving = EmbeddingIndex(vault)
    assert serving.identity.model == OLD
    assert "jittered" in " ".join(serving.stored_chunks_for(f"{kb_dirname()}/{edited_rel}")[0])

    assert recall_migration.build(vault, plan) is True
    recall_migration.cut_over(vault, plan)

    active = embeddings.get_embedding_index(vault)
    assert active.path == plan.shadow_path
    assert "jittered" in " ".join(active.stored_chunks_for(f"{kb_dirname()}/{edited_rel}")[0])
    assert active.stored_chunks_for(f"{kb_dirname()}/Notes/page-new.md")[0]
    hits = _explained(vault, "breaker opens after repeated failures")["hits"]
    assert hits[0]["path"] == f"{kb_dirname()}/Notes/page-new.md"


def test_a_failed_cutover_leaves_the_old_sidecar_serving(world) -> None:
    vault, log, _loads = world
    _warm(vault)
    plan = recall_migration.plan(vault)
    assert recall_migration.build(vault, plan) is True

    def crash(*_args, **_kwargs):
        raise OSError("disk full")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(index_paths, "publish_active_sidecar", crash)
        with pytest.raises(OSError):
            recall_migration.cut_over(vault, plan)

    assert index_paths.sidecar_path(vault) == index_paths.legacy_sidecar_path(vault)
    log.clear()
    assert _vector_lane(vault)["status"] == "participated"
    assert _query_encoders(log) == [OLD]
    assert recall_space.previous_resident(OLD) is not None
    assert plan.shadow_path.exists()

    recall_migration.cut_over(vault, plan)
    assert index_paths.sidecar_path(vault) == plan.shadow_path


def test_no_request_thread_loads_a_model(world) -> None:
    vault, _log, loads = world
    # Cold: a request neither loads the old encoder nor waits for it.
    vector = _vector_lane(vault)
    assert (vector["status"], vector["reason"]) == ("warming", "model_warming")
    assert loads == []

    _warm(vault)
    stop = threading.Event()
    job = recall_migration.start(vault, stop)
    job.join(timeout=60)
    assert not job.is_alive()
    assert recall_migration.status(vault)["state"] == "current"
    assert loads and all(thread != threading.current_thread().name for _name, thread in loads)

    before = list(loads)
    assert _vector_lane(vault)["status"] == "participated"
    assert loads == before


def test_the_kill_switch_keeps_the_old_sidecar_serving(world, monkeypatch) -> None:
    vault, log, _loads = world
    monkeypatch.setenv(recall_migration.REEMBED_ENV, "off")
    _warm(vault)

    assert recall_migration.run(vault, threading.Event()) == "disabled"

    status = recall_migration.status(vault)
    assert status["state"] == "disabled"
    assert status["serving"]["model"] == OLD
    assert sorted(path.name for path in index_paths.legacy_sidecar_path(vault).parent.glob(".embeddings.*.sqlite")) == []
    log.clear()
    assert _vector_lane(vault)["status"] == "participated"
    assert _query_encoders(log) == [OLD]


def test_the_old_sidecar_is_retired_by_the_next_start_not_the_cutover(world) -> None:
    vault, _log, _loads = world
    legacy = index_paths.legacy_sidecar_path(vault)
    _warm(vault)
    assert recall_migration.run(vault, threading.Event()) == "current"
    assert legacy.exists()

    # A new process: nothing of the cutover's process state survives.
    recall_space.unload_previous()
    embeddings.clear_embedding_indexes()
    recall_migration.reset_for_tests()
    assert recall_migration.run(vault, threading.Event()) == "current"

    assert not legacy.exists()
    assert not legacy.with_name(legacy.name + "-wal").exists()
    active = embeddings.get_embedding_index(vault)
    assert active.path != legacy and active.identity.model == NEW
    assert _vector_lane(vault)["status"] == "participated"


def test_status_reports_progress_and_the_serving_space(world) -> None:
    vault, log, _loads = world
    _warm(vault)
    plan = recall_migration.plan(vault)
    recall_migration.build(vault, plan, should_stop=lambda: len(_passages_by(log, NEW)) >= 2)

    status = recall_migration.status(vault)

    assert status["state"] == "paused"
    assert status["serving"]["model"] == OLD
    assert status["target"]["model"] == NEW
    assert status["paths_total"] == len(_PAGES)
    assert 0 < status["paths_done"] < len(_PAGES)
    assert status["reembed"] == "on"


def test_warm_up_loads_the_serving_encoder_before_writes_are_admitted(
    world, monkeypatch
) -> None:
    from exomem import warmup

    vault, _log, loads = world
    monkeypatch.setenv("EXOMEM_PRELOAD_MODELS", "1")
    monkeypatch.setenv("EXOMEM_DISABLE_RANKING", "1")
    monkeypatch.setattr(warmup, "warm_caches", lambda *_args, **_kwargs: {})
    admitted_when_loaded: list[bool] = []
    real_load = embedding_backend.load_encoder

    def load(name: str, **kwargs):
        if name == OLD:
            admitted_when_loaded.append(readiness.is_ready("embeddings"))
        return real_load(name, **kwargs)

    monkeypatch.setattr(embedding_backend, "load_encoder", load)
    readiness.begin_warm()
    thread = threading.Thread(target=warmup.warm_all, args=(vault,), name="exomem-warm")
    thread.start()
    thread.join(timeout=120)
    readiness.finish_warm()

    assert admitted_when_loaded == [False]
    assert {name for name, _thread in loads} == {OLD, NEW}
    assert all(thread == "exomem-warm" for _name, thread in loads)
    assert _vector_lane(vault)["status"] == "participated"


def test_switched_off_the_job_still_loads_the_serving_encoder(world, monkeypatch) -> None:
    vault, log, loads = world
    monkeypatch.setenv(recall_migration.REEMBED_ENV, "off")
    job = recall_migration.start(vault, threading.Event())
    job.join(timeout=60)

    assert recall_migration.status(vault)["state"] == "disabled"
    assert [name for name, _thread in loads] == [OLD]
    log.clear()
    assert _vector_lane(vault)["status"] == "participated"
    assert _query_encoders(log) == [OLD]


def test_the_cli_index_loads_the_serving_encoder_itself(world) -> None:
    vault, log, loads = world
    page = vault / kb_dirname() / "Notes/page-0.md"
    page.write_text(page.read_text(encoding="utf-8") + "\nOne more line.\n", encoding="utf-8")
    later = page.stat().st_mtime + 10  # past the index's one-second slack
    os.utime(page, (later, later))

    stats = embeddings.index_incremental(vault, log_fn=lambda _message: None)

    assert stats["files_to_embed"] == 1
    assert [name for name, _thread in loads] == [OLD]
    assert _passages_by(log, OLD) and not _passages_by(log, NEW)
    assert EmbeddingIndex(vault).identity.model == OLD


def test_doctor_reads_the_build_from_disk(world) -> None:
    from exomem import doctor

    vault, log, _loads = world
    _warm(vault)
    plan = recall_migration.plan(vault)
    recall_migration.build(vault, plan, should_stop=lambda: len(_passages_by(log, NEW)) >= 2)
    recall_migration.reset_for_tests()

    check = doctor._check_recall_reembed(vault)

    assert check.status == "pass"
    details = check.details
    assert details["serving"]["model"] == OLD
    assert details["building"]["model"] == NEW
    assert details["building"]["sidecar"] == plan.shadow_path.name
    assert 0 < details["building"]["paths_done"] < details["paths_total"] == len(_PAGES)
