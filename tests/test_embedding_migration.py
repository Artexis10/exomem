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
    runtime_resources,
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
    monkeypatch.setattr(embedding_backend, "_importable", lambda _module: True)
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


@pytest.fixture
def preseeded_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An imported corpus, with semantic units but no sidecar or write receipts."""
    vault = tmp_path / "vault"
    for index, (rel, (title, body)) in enumerate(_PAGES.items()):
        page = vault / kb_dirname() / rel
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(
            f"---\ntype: note\ntitle: {title}\nupdated: 2026-09-01\n"
            f"exomem_id: 00000000-0000-4000-8000-{index + 1:012d}\n---\n\n"
            f"# {title}\n\n{body}\n\n## Observations\n\n- [fact] {body} ^seed\n",
            encoding="utf-8",
        )
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    monkeypatch.setenv("EXOMEM_DISABLE_CLIP", "1")
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.delenv(recall_migration.REEMBED_ENV, raising=False)
    monkeypatch.setattr(embeddings, "_IMPORT_FAILED", False)
    monkeypatch.setattr(embeddings, "MODEL_NAME", NEW)
    monkeypatch.setattr(embeddings, "_MODEL", None)
    monkeypatch.setattr(recall_migration, "BATCH_CHUNKS", 2)
    log: list[tuple[str, str, bool, str]] = []
    loads: list[tuple[str, str]] = []

    def load(name: str, **_kwargs) -> _Model:
        loads.append((name, threading.current_thread().name))
        return _Model(name, log)

    monkeypatch.setattr(embedding_backend, "load_encoder", load)
    monkeypatch.setattr(embedding_backend, "_importable", lambda _module: True)
    readiness.reset()
    find_module.clear_cache()
    embeddings.clear_embedding_indexes()
    recall_migration.reset_for_tests()
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


@pytest.mark.parametrize("sidecar", ["missing", "empty"])
def test_cell_builds_preseeded_vault_without_sidecar(preseeded_world, monkeypatch, sidecar) -> None:
    from exomem import semantic_index

    vault, log, loads = preseeded_world
    monkeypatch.setattr(recall_space, "cell_mode", lambda env=None: True)
    if sidecar == "empty":
        empty = index_paths.sidecar_path(vault)
        empty.parent.mkdir(parents=True, exist_ok=True)
        empty.touch()
    assert embeddings.get_embedding_index(vault).identity is None
    _warm(vault)
    assert _explained(vault, "retry backoff")["hits"]
    assert _vector_lane(vault)["status"] != "participated"

    job = recall_migration.start(vault, threading.Event())
    job.join(timeout=60)
    assert not job.is_alive()
    assert recall_migration.status(vault)["state"] == "current"

    active = embeddings.get_embedding_index(vault)
    assert active.identity is not None and active.identity.model == NEW
    assert index_paths.active_sidecar_name(vault) == active.path.name
    assert active.path != index_paths.legacy_sidecar_path(vault)
    metadata, vectors = active.all_vectors()
    expected_chunks = []
    for rel in _PAGES:
        path = vault / kb_dirname() / rel
        page = find_module._CACHE.get(path, vault)
        expected_chunks.extend(embeddings._chunks_for_page(vault, page))
        state = semantic_index.build_parent_index_state(vault, path)
        assert state is not None
        refs = frozenset(unit.unit_ref for unit in state.document.units if unit.unit_ref is not None)
        assert refs
        assert active.semantic_unit_parent_states()[page.rel_path] == (
            frozenset({state.parent_generation}), refs
        )
    assert len(metadata) == len(expected_chunks)
    assert vectors.shape == (len(expected_chunks), _DIMS[NEW])
    unit_vectors = active.all_semantic_unit_vectors()
    assert sum(len(rows) for rows in unit_vectors.values()) == len(_PAGES)
    assert all(row.vector.shape == (_DIMS[NEW],) for rows in unit_vectors.values() for row in rows)
    assert sorted(chunk for chunks in _chunk_texts(vault).values() for chunk in chunks) == sorted(
        expected_chunks
    )
    assert {name for name, _thread in loads} == {NEW}
    assert all(thread != threading.current_thread().name for _name, thread in loads)
    log.clear()
    assert _vector_lane(vault)["status"] == "participated"
    assert _query_encoders(log) == [NEW]


def test_initial_build_resumes_after_interruption(preseeded_world, monkeypatch) -> None:
    vault, log, _loads = preseeded_world
    stop = threading.Event()
    encode = _Model.encode

    def stop_after_first_batch(self, texts, **kwargs):
        result = encode(self, texts, **kwargs)
        stop.set()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(_Model, "encode", stop_after_first_batch)
        job = recall_migration.start(vault, stop)
        job.join(timeout=60)
        assert not job.is_alive()
    assert recall_migration.status(vault)["state"] == "paused"
    first = _passages_by(log, NEW)
    assert 0 < len(first) < len(_PAGES)
    assert index_paths.active_sidecar_name(vault) is None
    assert recall_migration.status(vault)["serving"] is None

    embeddings.unload_model()
    embeddings.clear_embedding_indexes()
    recall_migration.reset_for_tests()
    find_module.clear_cache()
    job = recall_migration.start(vault, threading.Event())
    job.join(timeout=60)
    assert not job.is_alive()
    assert recall_migration.status(vault)["state"] == "current"
    encoded = _passages_by(log, NEW)
    assert encoded[: len(first)] == first
    assert all(encoded.count(text) == 1 for text in first)
    assert len(encoded) == len(set(encoded))
    assert len(embeddings.get_embedding_index(vault).semantic_unit_parent_states()) == len(_PAGES)
    assert _vector_lane(vault)["status"] == "participated"


def test_initial_build_allows_an_empty_active_target_path(preseeded_world) -> None:
    vault, _log, _loads = preseeded_world
    target = recall_migration._target_identity()
    name = index_paths.space_sidecar_name(target.fingerprint)
    path = index_paths.legacy_sidecar_path(vault).parent / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    index_paths.publish_active_sidecar(vault, name)
    plan = recall_migration.plan(vault)
    assert plan is not None and plan.serving is None
    assert plan.shadow_path == path
    assert recall_migration.run(vault, threading.Event()) == "current"
    assert embeddings.get_embedding_index(vault).identity.model == NEW
    assert _vector_lane(vault)["status"] == "participated"


def test_initial_build_resumes_after_a_live_write_and_restart(preseeded_world, monkeypatch) -> None:
    vault, log, _loads = preseeded_world
    monkeypatch.setattr(recall_space, "cell_mode", lambda env=None: True)
    plan = recall_migration.plan(vault)
    assert plan is not None
    assert not recall_migration.build(vault, plan, should_stop=lambda: bool(log))
    committed = list(_passages_by(log, NEW))
    added = vault / kb_dirname() / "Notes/live-write.md"
    added.write_text("# Live write\n\nA page written before the initial cutover.\n", encoding="utf-8")
    assert embeddings.upsert_after_write_status(vault, [added]).status == "completed"
    assert embeddings.get_embedding_index(vault).identity.model == NEW
    assert index_paths.active_sidecar_name(vault) is None

    embeddings.unload_model()
    embeddings.clear_embedding_indexes()
    recall_migration.reset_for_tests()
    find_module.clear_cache()
    stop = threading.Event()
    encode = _Model.encode

    def pause_again(self, texts, **kwargs):
        vectors = encode(self, texts, **kwargs)
        stop.set()
        return vectors

    with monkeypatch.context() as patch:
        patch.setattr(_Model, "encode", pause_again)
        assert recall_migration.run(vault, stop) == "paused"
    assert recall_migration.status(vault)["state"] == "paused"
    assert recall_migration.run(vault, threading.Event()) == "current"
    active = embeddings.get_embedding_index(vault)
    assert active.path == plan.shadow_path
    assert index_paths.active_sidecar_name(vault) == active.path.name
    assert set(active.file_mtimes()) == {
        f"{kb_dirname()}/{rel}" for rel in [*_PAGES, "Notes/live-write.md"]
    }
    assert len(active.semantic_unit_parent_states()) == len(_PAGES)
    assert all(_passages_by(log, NEW).count(text) == 1 for text in committed)
    assert _vector_lane(vault)["status"] == "participated"


def test_initial_build_resumes_after_first_encode_fails_and_a_live_write(
    preseeded_world, monkeypatch
) -> None:
    vault, log, _loads = preseeded_world
    monkeypatch.setattr(recall_space, "cell_mode", lambda env=None: True)

    def crash_before_first_commit(self, texts, **kwargs):
        raise RuntimeError("initial encode interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(_Model, "encode", crash_before_first_commit)
        job = recall_migration.start(vault, threading.Event())
        job.join(timeout=60)
        assert not job.is_alive()
    assert recall_migration.status(vault)["state"] == "failed"
    assert _passages_by(log, NEW) == []
    assert index_paths.active_sidecar_name(vault) is None

    added = vault / kb_dirname() / "Notes/live-write.md"
    added.write_text("# Live write\n\nA page written after the first encode failed.\n", encoding="utf-8")
    assert embeddings.upsert_after_write_status(vault, [added]).status == "completed"
    assert embeddings.get_embedding_index(vault).identity.model == NEW
    embeddings.unload_model()
    embeddings.clear_embedding_indexes()
    recall_migration.reset_for_tests()
    find_module.clear_cache()

    plan = recall_migration.plan(vault)
    assert plan is not None and plan.serving is None
    shadow = EmbeddingIndex(vault, path=plan.shadow_path)
    assert plan.shadow_path.exists()
    assert shadow.identity is None
    assert shadow.file_mtimes() == {}
    assert shadow.semantic_unit_parent_states() == {}
    publish = index_paths.publish_active_sidecar

    def publish_after_build(vault_root, name):
        assert recall_migration.status(vault_root)["state"] == "cutting_over"
        assert index_paths.active_sidecar_name(vault_root) is None
        assert set(shadow.file_mtimes()) == {
            f"{kb_dirname()}/{rel}" for rel in [*_PAGES, "Notes/live-write.md"]
        }
        assert len(shadow.semantic_unit_parent_states()) == len(_PAGES)
        publish(vault_root, name)

    monkeypatch.setattr(index_paths, "publish_active_sidecar", publish_after_build)
    assert recall_migration.run(vault, threading.Event()) == "current"
    active = embeddings.get_embedding_index(vault)
    assert active.path == plan.shadow_path
    assert index_paths.active_sidecar_name(vault) == active.path.name
    assert set(active.file_mtimes()) == {
        f"{kb_dirname()}/{rel}" for rel in [*_PAGES, "Notes/live-write.md"]
    }
    assert len(active.semantic_unit_parent_states()) == len(_PAGES)
    assert _vector_lane(vault)["status"] == "participated"


def _recall(vault: Path, query: str) -> dict:
    from exomem import commands

    find_module.clear_cache()
    result = commands.op_ask_memory(
        vault, query=query, limit=5, mode="vector", scope="kb-only", graph=False, rerank=False,
        detail="compact",
    )
    return {"hits": result} if isinstance(result, list) else result


def test_an_initial_build_serves_its_sidecar_and_live_writes_while_marked_warming(
    preseeded_world,
) -> None:
    # The state a re-seeded cell reached: the build's sidecar holds the vault,
    # a live write gave the serving sidecar the same space, and no pointer
    # names the build yet. Serving only the live write would answer every
    # query with it.
    vault, log, _loads = preseeded_world
    _warm(vault)
    plan = recall_migration.plan(vault)
    assert plan is not None and plan.serving is None
    assert recall_migration.build(vault, plan) is True
    added = vault / kb_dirname() / "Notes/page-new.md"
    added.write_text(
        "---\ntype: note\ntitle: Circuit breaker\nupdated: 2026-09-02\n---\n\n"
        "# Circuit breaker\n\nA breaker opens after repeated failures and probes later.\n",
        encoding="utf-8",
    )
    assert embeddings.upsert_after_write_status(vault, [added]).status == "completed"
    assert index_paths.active_sidecar_name(vault) is None
    assert set(embeddings.get_embedding_index(vault).file_mtimes()) == {f"{kb_dirname()}/Notes/page-new.md"}

    built = _recall(vault, "retry backoff")
    live = _recall(vault, "breaker opens after repeated failures")

    assert built["hits"][0]["path"] == f"{kb_dirname()}/Notes/page-0.md"
    assert live["hits"][0]["path"] == f"{kb_dirname()}/Notes/page-new.md"
    assert built["warming"] == {"components": ["embeddings"], "since_s": None}
    log.clear()
    find_module.clear_cache()
    find_module.find(vault, query="retry backoff", mode="hybrid", result_level="mixed", scope="kb-only")
    assert len(_query_encoders(log)) == 1  # the unit lane's vector fits the build

    recall_migration.cut_over(vault, plan)
    after = _recall(vault, "retry backoff")
    assert after["hits"][0]["path"] == f"{kb_dirname()}/Notes/page-0.md"
    assert "warming" not in after


def test_a_page_rewritten_after_the_build_encoded_it_is_not_recalled_by_its_removed_text(
    preseeded_world,
) -> None:
    # The build keeps a page's old chunks until its catch-up pass. Read as
    # current, they answered with a sentence the page no longer holds.
    from exomem import commands

    vault, _log, _loads = preseeded_world
    _warm(vault)
    plan = recall_migration.plan(vault)
    assert plan is not None and recall_migration.build(vault, plan) is True
    page = vault / kb_dirname() / "Notes/page-0.md"
    page.write_text(
        "---\ntype: note\ntitle: Retry with backoff\nupdated: 2026-09-02\n"
        "exomem_id: 00000000-0000-4000-8000-000000000001\n---\n\n"
        "# Retry with backoff\n\nRetries stop after five attempts and report the failure.\n",
        encoding="utf-8",
    )
    stamp = page.stat().st_mtime + 5
    os.utime(page, (stamp, stamp))
    assert embeddings.upsert_after_write_status(vault, [page]).status == "completed"

    def excerpts(query: str) -> dict[str, str]:
        find_module.clear_cache()
        result = commands.op_ask_memory(
            vault, query=query, limit=5, mode="vector", scope="kb-only", graph=False, rerank=False,
            detail="full",
        )
        hits = result if isinstance(result, list) else result["hits"]
        return {hit["path"]: hit.get("excerpt", "") for hit in hits}

    removed = excerpts("growing delay hammer")
    current = excerpts("five attempts report the failure")

    assert all("growing delay" not in excerpt for excerpt in removed.values())
    assert "five attempts" in current[f"{kb_dirname()}/Notes/page-0.md"]


def test_a_cell_reads_the_build_while_its_old_sidecar_is_refused(world, monkeypatch) -> None:
    # The cell's serving sidecar holds another model's vectors, which it never
    # encodes; the build's sidecar answers alone, so no two spaces meet.
    vault, log, _loads = world
    monkeypatch.setattr(recall_space, "cell_mode", lambda env=None: True)
    _warm(vault)
    plan = recall_migration.plan(vault)
    assert plan is not None and plan.serving.model == OLD
    assert recall_migration.build(vault, plan) is True
    log.clear()

    explained = _explained(vault, "retry backoff")

    assert explained["retrieval_profile"]["lanes"]["vector"]["status"] == "participated"
    assert explained["hits"][0]["path"] == f"{kb_dirname()}/Notes/page-0.md"
    assert _query_encoders(log) == [NEW]
    assert explained["warming"]["components"] == ["embeddings"]


def test_a_personal_server_reads_a_build_of_its_own_model(preseeded_world, monkeypatch) -> None:
    # The serving sidecar was written by another build (artefact digest) of the
    # recall model. The resident build cannot be encoded for it, but the job's
    # sidecar is in the resident build's own space.
    import dataclasses

    vault, log, _loads = preseeded_world
    encodes: list[tuple[str, bool]] = []

    def load_build(digest: str):
        def load(name: str, **_kwargs) -> _Model:
            model = _Model(name, log)
            model.profile = dataclasses.replace(model.profile, artifact_digest=digest)
            encode = model.encode
            model.encode = lambda texts, **kwargs: (
                encodes.extend((digest, text.startswith("q: ")) for text in texts) or encode(texts, **kwargs)
            )
            return model

        return load

    monkeypatch.setattr(embedding_backend, "load_encoder", load_build("aaaaa"))
    embeddings.index_incremental(vault, log_fn=lambda _message: None)
    embeddings.unload_model()
    monkeypatch.setattr(embedding_backend, "load_encoder", load_build("bbbbb"))
    embeddings.get_model()
    plan = recall_migration.plan(vault)
    assert plan is not None and plan.serving.model == plan.target.model == NEW
    assert plan.serving.fingerprint != plan.target.fingerprint
    log.clear()
    assert not recall_migration.build(vault, plan, should_stop=lambda: len(_passages_by(log, NEW)) >= 2)
    built = set(EmbeddingIndex(vault, path=plan.shadow_path).file_mtimes())
    assert built and built != set(embeddings.get_embedding_index(vault).file_mtimes())
    encodes.clear()

    lane = _explained(vault, "retry backoff")["retrieval_profile"]["lanes"]["vector"]
    queries = [digest for digest, is_query in encodes if is_query]
    recalled = _recall(vault, "retry backoff")

    assert lane["status"] == "participated"
    assert queries == ["bbbbb"]
    assert recalled["hits"] and {hit["path"] for hit in recalled["hits"]} <= built
    assert recalled["warming"]["components"] == ["embeddings"]


def test_a_query_finds_the_loaded_encoder_while_a_build_passage_holds_the_model_slot(monkeypatch) -> None:
    # A query used to take the model slot only to look the encoder up, so it
    # waited behind the build's passage once there and again for its own encode.
    loaded = object()
    monkeypatch.setattr(embeddings, "_MODEL", loaded)
    holding, release = threading.Event(), threading.Event()

    def build_passage() -> None:
        with runtime_resources.model_work("bulk"), runtime_resources.model_execution():
            holding.set()
            release.wait(10)

    builder = threading.Thread(target=build_passage)
    builder.start()
    try:
        assert holding.wait(5)
        found: list[object] = []
        query = threading.Thread(target=lambda: found.append(embeddings.get_model()))
        query.start()
        query.join(5)
        assert found == [loaded]
    finally:
        release.set()
        builder.join(5)


def test_healthy_legacy_sidecar_with_drift_does_not_plan_a_build(
    preseeded_world, monkeypatch
) -> None:
    from exomem import semantic_index

    vault, _log, _loads = preseeded_world
    embeddings.index_incremental(vault, log_fn=lambda _message: None)
    active = embeddings.get_embedding_index(vault)
    target = recall_migration._target_identity()
    assert active.identity.accepts(target.model, target.fingerprint)
    assert index_paths.active_sidecar_name(vault) is None
    shadow = active.path.parent / index_paths.space_sidecar_name(target.fingerprint)
    assert not shadow.exists()
    edited = vault / kb_dirname() / list(_PAGES)[-1]
    edited.write_text(edited.read_text(encoding="utf-8") + "\nAn offline edit.\n", encoding="utf-8")
    mtime = active.file_mtimes()[edited.relative_to(vault).as_posix()] + 5
    os.utime(edited, (mtime, mtime))
    find_module.clear_cache()
    calls = {"pages": 0, "units": 0}
    eligible = recall_migration._eligible_pages
    parent_state = semantic_index.build_parent_index_state

    def pages(root):
        calls["pages"] += 1
        return eligible(root)

    def units(*args, **kwargs):
        calls["units"] += 1
        return parent_state(*args, **kwargs)

    monkeypatch.setattr(recall_migration, "_eligible_pages", pages)
    monkeypatch.setattr(semantic_index, "build_parent_index_state", units)
    assert recall_migration.plan(vault) is None
    assert calls == {"pages": 0, "units": 0}
    assert not shadow.exists()


def test_published_current_sidecar_plans_without_enumerating_pages(
    preseeded_world, monkeypatch
) -> None:
    vault, _log, _loads = preseeded_world
    assert recall_migration.run(vault, threading.Event()) == "current"
    assert index_paths.active_sidecar_name(vault) is not None
    calls = []
    eligible = recall_migration._eligible_pages

    def pages(root):
        calls.append(root)
        return eligible(root)

    monkeypatch.setattr(recall_migration, "_eligible_pages", pages)
    assert recall_migration.plan(vault) is None
    assert calls == []


@pytest.mark.parametrize("missing", ["onnxruntime", "tokenizers", None])
def test_served_model_checks_onnx_stack_despite_torch_backend(
    preseeded_world, monkeypatch, missing
) -> None:
    monkeypatch.setattr(embeddings, "MODEL_NAME", "BAAI/bge-m3")
    assert embedding_backend.served_artifact(embeddings.MODEL_NAME) is not None
    monkeypatch.setattr(embedding_backend, "resolve_backend", lambda: embedding_backend.TORCH)
    calls = []

    def importable(module):
        calls.append(module)
        return module != missing

    monkeypatch.setattr(embedding_backend, "_importable", importable)
    assert recall_migration._embedding_skip_reason() == ("unavailable" if missing else None)
    assert calls == (["onnxruntime"] if missing == "onnxruntime" else ["onnxruntime", "tokenizers"])


@pytest.mark.parametrize("fixture", ["world", "preseeded_world"])
@pytest.mark.parametrize("operation", ["plan", "run"])
@pytest.mark.parametrize("reason", ["disabled", "import-failed", "missing-stack"])
def test_embedding_build_skips_disabled_or_unavailable_stack(
    request, monkeypatch, fixture, operation, reason
) -> None:
    vault, log, loads = request.getfixturevalue(fixture)
    fetched = []
    monkeypatch.setattr(embedding_backend, "ensure_served_artifact", lambda *args: fetched.append(args))
    if reason == "disabled":
        monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    elif reason == "import-failed":
        monkeypatch.setattr(embeddings, "_IMPORT_FAILED", True)
    else:
        monkeypatch.setattr(embedding_backend, "_importable", lambda _module: False)
    if operation == "plan":
        assert recall_migration.plan(vault) is None
    else:
        expected = "disabled" if reason == "disabled" else "unavailable"
        assert recall_migration.run(vault, threading.Event()) == expected
        assert recall_migration.status(vault)["state"] == expected
        active = embeddings.get_embedding_index(vault)
        assert recall_migration.status(vault)["serving"] == recall_migration._space(
            active.identity, active.path
        )
    assert loads == []
    assert log == []
    assert fetched == []


@pytest.mark.parametrize("excluded", [False, True])
def test_empty_embedding_corpus_loads_no_encoder(preseeded_world, excluded) -> None:
    from exomem import doctor

    vault, log, loads = preseeded_world
    if excluded:
        (vault / kb_dirname() / "_access.yaml").write_text("excluded:\n  - Notes\n", encoding="utf-8")
    else:
        for rel in _PAGES:
            (vault / kb_dirname() / rel).unlink()
    for _restart in range(2):
        assert recall_migration.plan(vault) is None
        assert recall_migration.run(vault, threading.Event()) == "current"
        check = doctor._check_recall_reembed(vault)
        assert check is None or check.status == "pass"
        check = doctor._check_embedding_sidecar(vault)
        assert check is None or check.status == "pass"
    assert loads == []
    assert log == []


def test_initial_build_progress_counts_only_eligible_pages(preseeded_world, monkeypatch) -> None:
    from exomem import doctor

    vault, log, loads = preseeded_world
    excluded = vault / kb_dirname() / "Private/secret.md"
    excluded.parent.mkdir()
    excluded.write_text("# Excluded\n\nAn excluded page.\n", encoding="utf-8")
    (vault / kb_dirname() / "_access.yaml").write_text("excluded:\n  - Private\n", encoding="utf-8")
    empty = vault / kb_dirname() / "Notes/empty.md"
    empty.write_text("---\ntype: note\ntitle: ''\n---\n", encoding="utf-8")
    chunks = embeddings._chunks_for_page
    monkeypatch.setattr(
        embeddings, "_chunks_for_page",
        lambda root, page, **kwargs: [] if page.path == empty else chunks(root, page, **kwargs),
    )
    check = doctor._check_recall_reembed(vault)
    assert check.details["paths_total"] == len(_PAGES)
    assert loads == []
    plan = recall_migration.plan(vault)
    assert plan is not None
    assert recall_migration.build(vault, plan)
    assert recall_migration.status(vault)["paths_done"] == len(_PAGES)
    assert recall_migration.status(vault)["paths_total"] == len(_PAGES)
    assert recall_migration.disk_status(vault)["building"]["paths_done"] == len(_PAGES)


@pytest.mark.parametrize("started", [False, True])
def test_doctor_reports_the_initial_build(preseeded_world, started) -> None:
    from exomem import doctor

    vault, log, loads = preseeded_world
    if started:
        plan = recall_migration.plan(vault)
        assert plan is not None
        assert not recall_migration.build(
            vault, plan, should_stop=lambda: len(_passages_by(log, NEW)) >= 2
        )
        embeddings.unload_model()
        embeddings.clear_embedding_indexes()
        recall_migration.reset_for_tests()
    loads.clear()
    check = doctor._check_recall_reembed(vault)
    assert check is not None and check.status == "warn"
    assert "initial" in check.message.lower()
    assert "pages built" in check.message
    assert check.details["serving"] is None
    assert check.details["paths_total"] == len(_PAGES)
    if started:
        assert 0 < check.details["building"]["paths_done"] < len(_PAGES)
    else:
        assert "pending" in check.message
        assert f"0/{len(_PAGES)}" in check.message
    sidecar_check = doctor._check_embedding_sidecar(vault)
    assert sidecar_check is None  # embeddings.reembed owns the one progress finding.
    report = doctor.doctor(vault=str(vault), profile="hybrid")
    progress = [item for item in report.checks if "initial dense index build" in item.message]
    assert len(progress) == 1 and progress[0].id == "embeddings.reembed"
    assert loads == []


def test_doctor_reports_an_initial_build_a_live_write_reached(preseeded_world) -> None:
    # The live write gives the serving sidecar the build's space; it still
    # holds that one page, so the build is not a healthy re-embed.
    from exomem import doctor

    vault, log, _loads = preseeded_world
    plan = recall_migration.plan(vault)
    assert plan is not None
    assert not recall_migration.build(vault, plan, should_stop=lambda: len(_passages_by(log, NEW)) >= 2)
    added = vault / kb_dirname() / "Notes/live-write.md"
    added.write_text("# Live write\n\nA page written during the initial build.\n", encoding="utf-8")
    assert embeddings.upsert_after_write_status(vault, [added]).status == "completed"
    recall_migration.reset_for_tests()

    check = doctor._check_recall_reembed(vault)

    assert check.status == "warn"
    assert "initial dense index build" in check.message
    assert check.details["serving"]["model"] == check.details["building"]["model"] == NEW


def test_doctor_reports_a_leftover_build_sidecar_once_the_serving_one_covers_the_vault(
    preseeded_world,
) -> None:
    # An operator reconcile filled the serving sidecar, so the job never
    # resumes the build: reporting it as in progress would warn forever.
    from exomem import doctor

    vault, log, _loads = preseeded_world
    plan = recall_migration.plan(vault)
    assert plan is not None
    assert not recall_migration.build(vault, plan, should_stop=lambda: len(_passages_by(log, NEW)) >= 2)
    embeddings.index_incremental(vault, log_fn=lambda _message: None)
    recall_migration.reset_for_tests()
    assert recall_migration.plan(vault) is None

    check = doctor._check_recall_reembed(vault)

    assert check.status == "warn"
    assert "left over" in check.message
    assert "in progress" not in check.message


def test_doctor_keeps_missing_sidecar_warning_when_initial_build_is_disabled(
    preseeded_world, monkeypatch
) -> None:
    from exomem import doctor

    vault, _log, loads = preseeded_world
    monkeypatch.setenv(recall_migration.REEMBED_ENV, "off")
    check = doctor._check_embedding_sidecar(vault)
    assert check.status == "warn"
    assert "Embedding sidecar is missing" in check.message
    assert "maintain --reconcile" in check.remediation
    assert loads == []


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


def test_a_cell_re_embeds_with_its_one_encoder_while_lexical_recall_serves(
    world, monkeypatch
) -> None:
    # A hosted or cloud cell holds one encoder. The old sidecar is refused
    # rather than served by a second model, lexical recall answers while the
    # new sidecar builds, and the vector lane returns at the cutover.
    vault, log, loads = world
    monkeypatch.setattr(recall_space, "cell_mode", lambda env=None: True)
    _warm(vault)
    vector = _vector_lane(vault)
    assert (vector["status"], vector["reason"]) == ("unavailable", "vector_space_mismatch")
    hits = _explained(vault, "retry backoff")["hits"]
    assert hits and hits[0]["path"] == f"{kb_dirname()}/Notes/page-0.md"

    # A write meanwhile cannot land in the refused sidecar; the build takes it.
    added = vault / kb_dirname() / "Notes/page-new.md"
    added.write_text(
        "---\ntype: note\ntitle: Circuit breaker\nupdated: 2026-09-02\n---\n\n"
        "# Circuit breaker\n\nA breaker opens after repeated failures and probes later.\n",
        encoding="utf-8",
    )
    embeddings.upsert_after_write_status(vault, [added])

    job = recall_migration.start(vault, threading.Event())
    job.join(timeout=60)
    assert not job.is_alive()

    assert recall_migration.status(vault)["state"] == "current"
    assert {name for name, _thread in loads} == {NEW}
    active = embeddings.get_embedding_index(vault)
    assert active.identity.model == NEW
    assert active.stored_chunks_for(f"{kb_dirname()}/Notes/page-new.md")[0]
    log.clear()
    assert _vector_lane(vault)["status"] == "participated"
    assert _query_encoders(log) == [NEW]


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


def test_the_next_start_heals_a_write_a_crash_after_the_swap_left_behind(world) -> None:
    """A write that lands in the old sidecar after the last catch-up pass, when
    the process dies between the pointer swap and the pass after it, is caught
    up by the next start before the old sidecar is retired."""
    vault, _log, _loads = world
    _warm(vault)
    plan = recall_migration.plan(vault)
    assert recall_migration.build(vault, plan) is True
    edited_rel = next(iter(_PAGES))
    edited = vault / kb_dirname() / edited_rel
    publish = index_paths.publish_active_sidecar

    def write_then_swap(vault_root, name):
        # The write lands in the serving (old) sidecar, just before the swap.
        edited.write_text(
            edited.read_text(encoding="utf-8") + "\nWritten in the swap window: a jittered pause.\n",
            encoding="utf-8",
        )
        assert embeddings.upsert_after_write_status(vault, [edited]).status == "completed"
        publish(vault_root, name)
        raise KeyboardInterrupt("the process dies after the swap")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(index_paths, "publish_active_sidecar", write_then_swap)
        with pytest.raises(KeyboardInterrupt):
            recall_migration.cut_over(vault, plan)
    assert index_paths.sidecar_path(vault) == plan.shadow_path
    stale = EmbeddingIndex(vault, path=plan.shadow_path).stored_chunks_for(f"{kb_dirname()}/{edited_rel}")[0]
    assert "jittered" not in " ".join(stale)

    # A new process.
    recall_space.unload_previous()
    embeddings.clear_embedding_indexes()
    recall_migration.reset_for_tests()
    find_module.clear_cache()
    assert recall_migration.run(vault, threading.Event()) == "current"

    active = embeddings.get_embedding_index(vault)
    assert active.path == plan.shadow_path
    assert "jittered" in " ".join(active.stored_chunks_for(f"{kb_dirname()}/{edited_rel}")[0])
    assert not index_paths.legacy_sidecar_path(vault).exists()


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


@pytest.mark.parametrize(("mode", "preloaded"), [("normal", {OLD, NEW}), ("quiet", {OLD})])
def test_warm_up_loads_the_serving_encoder_in_every_mode(world, monkeypatch, mode, preloaded) -> None:
    """Normal and quiet mode preload no model by policy, yet a sidecar still
    served by its previous encoder needs that encoder before writes are
    admitted: a write would otherwise fail to encode and leave its row stale
    until the cutover. Quiet mode accepts that encoder beside the lazy recall
    model for as long as the re-embed runs."""
    from exomem import warmup

    vault, _log, loads = world
    monkeypatch.setenv("EXOMEM_MODE", mode)
    monkeypatch.delenv("EXOMEM_PRELOAD_MODELS", raising=False)
    monkeypatch.setenv("EXOMEM_DISABLE_RANKING", "1")
    monkeypatch.setattr(warmup, "warm_caches", lambda *_args, **_kwargs: {})
    # The shipped recall model is a served artefact; NEW stands in for it.
    monkeypatch.setattr(
        embedding_backend, "served_artifact", lambda name: object() if name == NEW else None
    )
    monkeypatch.setattr(embedding_backend, "ensure_served_artifact", lambda _name: None)
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
    assert {name for name, _thread in loads} == preloaded
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


def test_doctor_reports_a_cells_refused_sidecar_as_dense_recall_off(world, monkeypatch) -> None:
    # A cell does not serve the sidecar another model wrote, so doctor must
    # not report it as serving while the re-embed runs.
    from exomem import doctor

    vault, log, _loads = world
    monkeypatch.setattr(recall_space, "cell_mode", lambda env=None: True)
    _warm(vault)
    plan = recall_migration.plan(vault)
    recall_migration.build(vault, plan, should_stop=lambda: len(_passages_by(log, NEW)) >= 2)
    recall_migration.reset_for_tests()

    check = doctor._check_recall_reembed(vault)

    assert check.status == "warn"
    assert f"refuses its {OLD} sidecar" in check.message
    assert "dense recall covers only the pages the re-embed has built" in check.message
    assert "pages built" in check.message

    monkeypatch.setenv(recall_migration.REEMBED_ENV, "off")
    check = doctor._check_recall_reembed(vault)
    assert check.status == "warn"
    assert "EXOMEM_RECALL_REEMBED=off keeps it off" in check.message

