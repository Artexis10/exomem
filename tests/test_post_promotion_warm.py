"""A promoted worker warms its own request path, not a standby's caches.

Ruling 2026-09-28: a quiet-mode standby keeps skipping its cache warm (the
overlap with the outgoing worker is what quiet mode protects). Instead, once
retrieval is admitted, activation runs ONE internal activation that writes no
activation log, and after the startup drain the embedding matrix is warmed
off the activation thread so nothing waits on it.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from exomem import commands, query_log, readiness, server_runtime


@pytest.fixture(autouse=True)
def _fresh_readiness():
    readiness.reset()
    yield
    readiness.reset()


def test_the_request_path_warms_after_admission_and_the_matrix_after_the_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXOMEM_DISABLE_WARMUP", raising=False)
    vault = tmp_path / "vault"
    vault.mkdir()
    calls: list[str] = []
    matrix_warmed = threading.Event()
    done = threading.Event()

    class Watcher:
        def wait_until_seeded(self, timeout=None) -> bool:
            return True

        def finish_startup_recovery(self) -> None:
            calls.append("drain")

    def start_compute(_root) -> None:
        readiness.begin_warm()
        readiness.mark_ready("retrieval_catalog")
        readiness.mark_ready("semantic_corpus")

    monkeypatch.setattr(server_runtime, "_start_compute_runtime", start_compute)
    monkeypatch.setattr(server_runtime, "_start_file_watcher", lambda _root: Watcher())
    monkeypatch.setattr(server_runtime, "_start_graph_drain", lambda _root: calls.append("graph"))
    monkeypatch.setattr(
        server_runtime, "_start_media_worker", lambda _root: (calls.append("media"), done.set())[0]
    )
    monkeypatch.setattr(
        server_runtime, "_warm_request_path", lambda _root: calls.append("request-path")
    )
    monkeypatch.setattr(
        server_runtime,
        "_warm_embedding_matrix",
        lambda _root: (calls.append("matrix"), matrix_warmed.set())[0],
    )
    activation = server_runtime.LocalRuntimeActivation(vault, fallback_seconds=60.0)

    async def exercise() -> None:
        async with activation.lifespan()(SimpleNamespace()):
            activation.start()
            assert done.wait(timeout=5.0)
            assert matrix_warmed.wait(timeout=5.0)

    asyncio.run(exercise())
    assert calls.index("request-path") < calls.index("drain") < calls.index("matrix")
    assert calls.index("drain") < calls.index("graph")
    assert calls.count("request-path") == 1


def test_the_internal_activation_writes_no_activation_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    turns: list[str] = []

    def body(_root, turn, *_args, **kwargs):
        turns.append(turn)
        assert kwargs.get("session") is None and kwargs.get("client") is None
        return {"abstained": True}

    logged: list[object] = []
    monkeypatch.setattr(commands, "_op_activate_context_body", body)
    monkeypatch.setattr(query_log, "log_activation_call", lambda *a, **k: logged.append(a))
    server_runtime._warm_request_path(tmp_path)
    assert len(turns) == 1 and turns[0].strip()
    assert logged == []


def test_a_failed_internal_activation_never_stops_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def body(*_args, **_kwargs):
        raise RuntimeError("cold index")

    monkeypatch.setattr(commands, "_op_activate_context_body", body)
    server_runtime._warm_request_path(tmp_path)


def test_the_matrix_warm_is_skipped_without_embeddings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from exomem import embeddings

    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    touched: list[object] = []
    monkeypatch.setattr(embeddings, "get_embedding_index", lambda root: touched.append(root))
    server_runtime._warm_embedding_matrix(tmp_path)
    assert touched == []


@pytest.mark.parametrize("policy,expected", [("legacy", [8]), ("service-v1", [])])
def test_the_matrix_warm_runs_only_for_a_resident_corpus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str, expected: list[int]
) -> None:
    from exomem import embeddings

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_RESOURCE_POLICY", policy)
    searches: list[int] = []

    class Index:
        dim = 8

        def search(self, query, k=1):
            searches.append(len(query))
            return []

    monkeypatch.setattr(embeddings, "get_embedding_index", lambda _root: Index())
    server_runtime._warm_embedding_matrix(tmp_path)
    assert searches == expected
