"""Optional model preloads do not hold retrieval readiness.

Measured 2026-09-28 on the personal service's 0.96.0 promotion: retrieval
admission was revoked 10 s into the promoted worker's warm, and readiness
stayed `not_ready` for 53 s, flipping back at the exact moment the warm logged
completion. The warm was held open by the optional reranker preload (46.7 s,
queued on the model gate); `retrieval_admission` re-proves a revoked catalogue
only once the whole warm has finished.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from exomem import freshness, lexstore, readiness, warmup


@pytest.fixture(autouse=True)
def _fresh_readiness():
    readiness.reset()
    yield
    readiness.reset()


def _managed_warm_with_revoked_catalogue(monkeypatch: pytest.MonkeyPatch, proof: list[bool]):
    monkeypatch.setattr(freshness, "event_indexes_enabled", lambda: True)
    monkeypatch.setattr(
        lexstore,
        "runtime_retrieval_catalog_current",
        lambda _root, **_kwargs: proof[0],
    )
    readiness.manage_runtime()
    readiness.begin_warm()
    readiness.mark_ready("retrieval_catalog")
    readiness.mark_unready("retrieval_catalog")


def test_a_revoked_catalogue_is_re_proved_once_the_required_warm_is_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proof = [True]
    _managed_warm_with_revoked_catalogue(monkeypatch, proof)
    readiness.finish_required_warm()
    assert readiness.is_warming()  # optional preloads are still running
    assert readiness.retrieval_admission(tmp_path) == {"state": "ready", "admitted": True}


def test_before_the_required_warm_is_done_a_revoked_catalogue_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proof = [True]
    _managed_warm_with_revoked_catalogue(monkeypatch, proof)
    assert readiness.retrieval_admission(tmp_path) == {"state": "warming", "admitted": False}


def test_a_failing_proof_after_the_required_warm_still_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proof = [False]
    _managed_warm_with_revoked_catalogue(monkeypatch, proof)
    readiness.finish_required_warm()
    assert readiness.retrieval_admission(tmp_path)["admitted"] is False
    proof[0] = True
    assert readiness.retrieval_admission(tmp_path)["admitted"] is True


def test_a_new_warm_clears_the_required_mark() -> None:
    readiness.manage_runtime()
    readiness.begin_warm()
    readiness.finish_required_warm()
    assert readiness.required_warm_finished()
    readiness.begin_warm()
    assert not readiness.required_warm_finished()


def test_warm_all_finishes_the_required_warm_before_any_model_preload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []

    def reranker():
        seen.append(readiness.required_warm_finished())
        raise RuntimeError("no model in this test")

    monkeypatch.setattr(warmup, "model_preload_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(warmup, "warm_retrieval_catalog", lambda _root: True)
    monkeypatch.setattr(warmup, "warm_graph_handoff", lambda _root: {})
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.delenv("EXOMEM_DISABLE_RANKING", raising=False)
    from exomem import embeddings, semantic_contract

    monkeypatch.setattr(semantic_contract, "build_corpus_context", lambda _root: None)
    monkeypatch.setattr(embeddings, "get_model", lambda *_a, **_k: seen.append(
        readiness.required_warm_finished()
    ) or (_ for _ in ()).throw(RuntimeError("no model")))
    monkeypatch.setattr(embeddings, "get_reranker", reranker)
    monkeypatch.setattr(warmup, "warm_caches", lambda *_a, **_k: {})
    readiness.begin_warm()
    warmup.warm_all(tmp_path)
    assert seen and all(seen)


def test_the_readiness_probe_does_not_block_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At 06:13:16 one `/health/ready` blocked 5.8 s on a reserved-state lock
    inside the event loop, and two liveness polls behind it timed out."""
    httpx = pytest.importorskip("httpx")
    from fastmcp import FastMCP

    from exomem import runtime_readiness, server_assets

    started = threading.Event()

    def slow_readiness(**_kwargs):
        started.set()
        time.sleep(1.0)
        return {"status": "ready"}

    monkeypatch.setattr(runtime_readiness, "runtime_readiness", slow_readiness)
    app = FastMCP("readiness-loop-probe")
    server_assets.register_health_routes(app)
    asgi = app.http_app(transport="streamable-http")

    async def scenario() -> float:
        transport = httpx.ASGITransport(app=asgi)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            t0 = time.monotonic()
            ready = asyncio.create_task(client.get("/health/ready"))
            # Let the readiness handler start. A handler that blocks the loop
            # holds this sleep, and the liveness poll behind it, for its full
            # second.
            await asyncio.sleep(0.05)
            live = await client.get("/health")
            elapsed = time.monotonic() - t0
            assert started.is_set()
            assert live.status_code == 200
            assert (await ready).status_code == 200
            return elapsed

    assert asyncio.run(scenario()) < 0.5
