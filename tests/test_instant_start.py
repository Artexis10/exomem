"""Instant-start boot (OpenSpec: add-instant-start-boot).

Boot no longer blocks on model preloads / cache warm-up: `warmup.start_background`
runs `warm_all` on a daemon thread while the transport serves requests, and
`readiness` is the coordination point between that thread and request paths
(`find`'s vector/CLIP/rerank lanes, `embeddings.upsert_after_write`, and
`commands.op_find`'s response envelope). This file pins the whole contract:

- `exomem.readiness`: the warm-phase registry (begin/finish/mark_ready/
  should_defer/defer/warming_info/wait/reset) and its concurrency guarantees.
- `exomem.warmup.warm_all` / `start_background`: ordering, soft-fail,
  deferred-write-embed draining, and the begin_warm-before-thread-starts +
  finish_warm-in-finally guarantees.
- `exomem.server.build_server`: EAGER_BOOT / DISABLE_WARMUP dispatch.
- `exomem.find.find`'s `degraded_out` gates on the vector and rerank lanes.
- `exomem.commands.op_find`'s "warming" envelope.
- `exomem.bm25.BM25Index`'s single-build-under-concurrency lock.
- `exomem.embeddings.upsert_after_write`'s defer-during-warm + exactly-once drain.
- `exomem warm` CLI.
- `exomem doctor`'s "models.cache" check.

Every test resets readiness state via the autouse fixture below (before AND
after), regardless of whether the test itself also calls finish_warm().
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

from exomem import bm25, commands, embeddings, readiness, server, warmup
from exomem import doctor as doctor_module
from exomem import find as find_module
from exomem.__main__ import main


@pytest.fixture(autouse=True)
def _reset_readiness():
    readiness.reset()
    yield
    readiness.reset()


# ============================================================================
# exomem.readiness — the warm-phase registry
# ============================================================================


def test_components_tuple_and_initial_state() -> None:
    """The four coordinated components, and the never-warmed default state."""
    assert readiness.COMPONENTS == ("lexical", "embeddings", "reranker", "clip")
    assert readiness.is_warming() is False
    assert readiness.warming_info() is None
    for c in readiness.COMPONENTS:
        assert readiness.is_ready(c) is False


def test_begin_warm_resets_previous_events_and_deferred_items() -> None:
    """A fresh begin_warm() wipes prior ready-state AND any parked items —
    a new warm starts from a clean slate, never replaying a stale batch."""
    readiness.begin_warm()
    readiness.mark_ready("lexical")
    assert readiness.defer("embeddings", "stale-item") is True
    assert readiness.is_ready("lexical") is True

    readiness.begin_warm()  # second warm must reset everything above

    assert readiness.is_ready("lexical") is False
    assert readiness.mark_ready("embeddings") == []  # stale item is gone, not drained


def test_finish_warm_ends_window_permanently_even_for_never_ready_components() -> None:
    """finish_warm() closes the window for good — should_defer is False
    forever after, even for a component that never landed (a failed preload
    falls back to inline lazy-load semantics, not permanent deferral)."""
    readiness.begin_warm()
    assert readiness.is_warming() is True

    readiness.finish_warm()

    assert readiness.is_warming() is False
    assert readiness.should_defer("clip") is False
    assert readiness.is_ready("clip") is False


def test_should_defer_and_defer_are_false_before_any_warm_ever_ran() -> None:
    """No begin_warm() has ever run (the reset() default) -> nothing defers,
    which is today's pre-feature behavior."""
    assert readiness.is_warming() is False
    assert readiness.should_defer("embeddings") is False
    assert readiness.defer("embeddings", "x") is False
    assert readiness.mark_ready("embeddings") == []  # nothing was ever recorded


def test_mark_ready_drains_deferred_items_atomically_exactly_once() -> None:
    """mark_ready sets the event AND atomically drains everything parked so
    far, in order, exactly once — a second call comes back empty, and once
    ready, defer() proceeds inline (returns False) instead of parking."""
    readiness.begin_warm()
    assert readiness.defer("embeddings", "a") is True
    assert readiness.defer("embeddings", "b") is True

    drained = readiness.mark_ready("embeddings")

    assert drained == ["a", "b"]
    assert readiness.is_ready("embeddings") is True
    assert readiness.mark_ready("embeddings") == []  # second drain is empty
    assert readiness.defer("embeddings", "c") is False  # ready now; caller proceeds inline


def test_warming_info_shape_and_transitions() -> None:
    """None outside a warm; {"components": [unready...], "since_s": >=0}
    while warming, shrinking as components land; None again after finish."""
    assert readiness.warming_info() is None

    readiness.begin_warm()
    info = readiness.warming_info()
    assert info is not None
    assert set(info) == {"components", "since_s"}
    assert info["components"] == list(readiness.COMPONENTS)
    assert info["since_s"] >= 0

    readiness.mark_ready("lexical")
    info2 = readiness.warming_info()
    assert set(info2["components"]) == {"embeddings", "reranker", "clip"}

    readiness.finish_warm()
    assert readiness.warming_info() is None


def test_wait_unblocks_after_mark_ready_and_times_out_otherwise() -> None:
    """wait() times out (False) when nobody ever marks the component ready,
    and unblocks (True) as soon as another thread does — ordered via an
    Event, no sleeps."""
    readiness.begin_warm()

    assert readiness.wait("clip", timeout=0.05) is False  # never marked ready

    proceed = threading.Event()

    def worker() -> None:
        proceed.wait()
        readiness.mark_ready("reranker")

    t = threading.Thread(target=worker)
    t.start()
    proceed.set()
    ok = readiness.wait("reranker", timeout=5.0)
    t.join(timeout=5.0)

    assert ok is True


def test_reset_clears_everything() -> None:
    """reset() is the full test hook: warm state, ready events, and any
    parked deferred items all go back to never-warmed."""
    readiness.begin_warm()
    readiness.mark_ready("lexical")
    readiness.defer("embeddings", "x")

    readiness.reset()

    assert readiness.is_warming() is False
    assert readiness.warming_info() is None
    assert readiness.is_ready("lexical") is False
    assert readiness.mark_ready("embeddings") == []


def test_defer_and_mark_ready_race_no_loss_no_duplication() -> None:
    """defer() and mark_ready() share one lock, so every defer() call is
    fully ordered relative to the (single) mark_ready() call: whichever
    critical section runs first is complete before the other starts. So the
    drained set must be EXACTLY the set of items whose defer() returned
    True — never fewer (lost) and never duplicated — regardless of how the
    200 producer threads interleave with the drain."""
    readiness.begin_warm()
    n_items = 200
    release = threading.Event()
    outcomes: list[bool | None] = [None] * n_items

    def producer(i: int) -> None:
        release.wait()
        outcomes[i] = readiness.defer("embeddings", i)

    threads = [threading.Thread(target=producer, args=(i,)) for i in range(n_items)]
    for t in threads:
        t.start()
    release.set()
    drained = readiness.mark_ready("embeddings")
    for t in threads:
        t.join(timeout=5.0)

    assert len(drained) == len(set(drained))  # no duplicates
    true_indices = {i for i, ok in enumerate(outcomes) if ok}
    assert set(drained) == true_indices  # exactly the recorded set, nothing lost


# ============================================================================
# exomem.warmup.warm_all — lexical caches, then model preloads
# ============================================================================


def test_warm_all_marks_components_ready_in_lexical_embeddings_reranker_clip_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """warm_all runs warm_caches (lexical) first, then bge, then the
    reranker, then CLIP — in that exact order — marking each component
    ready as it lands."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    call_order: list[str] = []
    monkeypatch.setattr(warmup, "warm_caches", lambda vr: call_order.append("lexical") or {})
    monkeypatch.setattr(embeddings, "get_model", lambda: call_order.append("embeddings") or object())
    monkeypatch.setattr(embeddings, "get_reranker", lambda: call_order.append("reranker") or object())
    monkeypatch.setattr(embeddings, "get_clip_model", lambda: call_order.append("clip") or object())
    monkeypatch.setattr(embeddings, "clip_enabled", lambda: True)

    warmup.warm_all(tmp_path)

    assert call_order == ["lexical", "embeddings", "reranker", "clip"]
    for c in readiness.COMPONENTS:
        assert readiness.is_ready(c) is True


def test_warm_all_skips_model_preloads_when_embeddings_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EXOMEM_DISABLE_EMBEDDINGS must skip ALL model preloads outright — the
    bge/reranker/CLIP getters are never even called. The model components are
    marked ready anyway: a lexical-only install has no models to wait for, so
    finds during the lexical warm must not carry a "warming" marker naming
    models this install will never load."""
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setattr(warmup, "warm_caches", lambda vr: {})

    def _forbidden() -> None:
        raise AssertionError("model getters must not be called when embeddings are disabled")

    monkeypatch.setattr(embeddings, "get_model", _forbidden)
    monkeypatch.setattr(embeddings, "get_reranker", _forbidden)
    monkeypatch.setattr(embeddings, "get_clip_model", _forbidden)

    warmup.warm_all(tmp_path)

    assert readiness.is_ready("lexical") is True
    assert readiness.is_ready("embeddings") is True
    assert readiness.is_ready("reranker") is True
    assert readiness.is_ready("clip") is True


def test_warm_all_soft_fails_one_step_and_continues_to_later_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raising loader must not abort later steps — bge failing leaves only
    "embeddings" unready while reranker/clip still load and mark ready."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(warmup, "warm_caches", lambda vr: {})

    def _boom() -> None:
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(embeddings, "get_model", _boom)
    reranker_called: list[int] = []
    clip_called: list[int] = []
    monkeypatch.setattr(embeddings, "get_reranker", lambda: reranker_called.append(1) or object())
    monkeypatch.setattr(embeddings, "get_clip_model", lambda: clip_called.append(1) or object())
    monkeypatch.setattr(embeddings, "clip_enabled", lambda: True)

    warmup.warm_all(tmp_path)

    assert readiness.is_ready("lexical") is True
    assert readiness.is_ready("embeddings") is False  # get_model raised
    assert reranker_called == [1]
    assert readiness.is_ready("reranker") is True
    assert clip_called == [1]
    assert readiness.is_ready("clip") is True


def test_warm_all_drains_deferred_write_embeds_after_embeddings_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once bge lands, warm_all must drain whatever upsert_after_write
    parked via readiness.defer("embeddings", ...) during the warm and
    replay it through embeddings.upsert_after_write."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(warmup, "warm_caches", lambda vr: {})
    monkeypatch.setattr(embeddings, "get_model", lambda: object())
    monkeypatch.setattr(embeddings, "get_reranker", lambda: object())
    monkeypatch.setattr(embeddings, "clip_enabled", lambda: False)
    drained_calls: list[tuple] = []
    monkeypatch.setattr(
        embeddings, "upsert_after_write",
        lambda vr, paths: drained_calls.append((vr, list(paths))),
    )

    readiness.begin_warm()  # so the manual defer() below actually records
    some_paths = (tmp_path / "a.md", tmp_path / "b.md")
    assert readiness.defer("embeddings", (tmp_path, some_paths)) is True

    warmup.warm_all(tmp_path)

    assert drained_calls == [(tmp_path, list(some_paths))]


def test_warm_all_runs_a_throwaway_encode_after_each_preload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loading weights isn't enough — the backend compiles its kernels on the first
    forward pass (Metal/MPS especially), so warm_all runs one throwaway encode/predict on
    each loaded model, moving that one-time compile off the user's first query. The loader
    is called exactly once — the warm runs on the returned model object."""
    warmed: list[str] = []

    class _FakeST:  # SentenceTransformer-like (bge + CLIP)
        def encode(self, texts, *a, **kw):
            warmed.append("encode")

    class _FakeCE:  # CrossEncoder-like (reranker)
        def predict(self, pairs, *a, **kw):
            warmed.append("predict")

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(warmup, "warm_caches", lambda vr: {})
    monkeypatch.setattr(embeddings, "get_model", _FakeST)
    monkeypatch.setattr(embeddings, "get_reranker", _FakeCE)
    monkeypatch.setattr(embeddings, "get_clip_model", _FakeST)
    monkeypatch.setattr(embeddings, "clip_enabled", lambda: True)

    warmup.warm_all(tmp_path)

    assert warmed == ["encode", "predict", "encode"]  # bge, reranker, CLIP


def test_warm_encode_failure_is_swallowed_and_does_not_block_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A warm-encode is a latency nicety, never a gate: if the dummy encode raises (e.g. an
    unimplemented MPS op with fallback off), the component still marks ready because the
    preload itself succeeded."""

    class _Boom:
        def encode(self, *a, **kw):
            raise RuntimeError("mps kernel compile failed")

        def predict(self, *a, **kw):
            raise RuntimeError("mps kernel compile failed")

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(warmup, "warm_caches", lambda vr: {})
    monkeypatch.setattr(embeddings, "get_model", _Boom)
    monkeypatch.setattr(embeddings, "get_reranker", _Boom)
    monkeypatch.setattr(embeddings, "clip_enabled", lambda: False)

    warmup.warm_all(tmp_path)

    assert readiness.is_ready("embeddings") is True
    assert readiness.is_ready("reranker") is True


# ============================================================================
# exomem.warmup.start_background — the daemon-thread wrapper
# ============================================================================


def test_start_background_begins_warm_before_thread_work_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """begin_warm() must fire synchronously, before the daemon thread's
    warm_all body runs — should_defer is already True the instant
    start_background returns, even while warm_all itself is still blocked."""
    proceed = threading.Event()

    def fake_warm_all(vault_root: Path) -> dict:
        proceed.wait(timeout=5.0)
        return {}

    monkeypatch.setattr(warmup, "warm_all", fake_warm_all)

    t = warmup.start_background(tmp_path)
    try:
        assert t.name == "exomem-warm"
        assert t.daemon is True
        assert readiness.is_warming() is True
        assert readiness.should_defer("embeddings") is True
    finally:
        proceed.set()
        t.join(timeout=5.0)
    assert readiness.is_warming() is False


def test_start_background_runs_finish_warm_in_finally_even_on_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crashing warm_all must still reach finish_warm() (the finally), so
    a background failure never leaves the process deferring forever."""

    def fake_warm_all_raises(vault_root: Path) -> dict:
        raise RuntimeError("boom")

    monkeypatch.setattr(warmup, "warm_all", fake_warm_all_raises)

    t = warmup.start_background(tmp_path)
    t.join(timeout=5.0)

    assert not t.is_alive()
    assert readiness.is_warming() is False


# ============================================================================
# exomem.server.build_server — EAGER_BOOT / DISABLE_WARMUP dispatch
# ============================================================================


def test_build_server_uses_background_warmup_by_default(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No EXOMEM_EAGER_BOOT -> build_server dispatches to start_background,
    never calling warm_all inline (the boot must not block)."""
    monkeypatch.delenv("EXOMEM_DISABLE_WARMUP", raising=False)
    monkeypatch.delenv("EXOMEM_EAGER_BOOT", raising=False)
    # build_server() calls load_dotenv(override=True), which walks up from
    # server.py's own directory and can pick up a REAL .env outside this
    # worktree (e.g. the primary checkout's) that sets unrelated vars
    # directly on os.environ — a write monkeypatch can't auto-undo. Neutralize
    # it: these tests only care about the warmup dispatch, not .env loading.
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **kw: None)
    calls: list[str] = []
    monkeypatch.setattr(warmup, "start_background", lambda vr: calls.append("background"))
    monkeypatch.setattr(warmup, "warm_all", lambda vr: calls.append("eager"))

    server.build_server(require_auth=False)

    assert calls == ["background"]


def test_build_server_uses_eager_warmup_when_env_set(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EXOMEM_EAGER_BOOT=1 -> build_server calls warm_all synchronously,
    never start_background — the rollback lever for deploys."""
    monkeypatch.delenv("EXOMEM_DISABLE_WARMUP", raising=False)
    monkeypatch.setenv("EXOMEM_EAGER_BOOT", "1")
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **kw: None)  # see note above
    calls: list[str] = []
    monkeypatch.setattr(warmup, "start_background", lambda vr: calls.append("background"))
    monkeypatch.setattr(warmup, "warm_all", lambda vr: calls.append("eager"))

    server.build_server(require_auth=False)

    assert calls == ["eager"]


def test_build_server_calls_neither_warmup_path_when_disabled(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EXOMEM_DISABLE_WARMUP set -> neither warm_all nor start_background
    runs; the boot is pure lazy, matching the pre-feature behavior."""
    monkeypatch.setenv("EXOMEM_DISABLE_WARMUP", "1")
    monkeypatch.delenv("EXOMEM_EAGER_BOOT", raising=False)
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **kw: None)  # see note above
    calls: list[str] = []
    monkeypatch.setattr(warmup, "start_background", lambda vr: calls.append("background"))
    monkeypatch.setattr(warmup, "warm_all", lambda vr: calls.append("eager"))

    server.build_server(require_auth=False)

    assert calls == []


# ============================================================================
# exomem.find — degraded_out gates on the vector and rerank lanes
# ============================================================================


def test_find_defers_vector_lane_mid_warm_without_calling_embed_texts(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid-warm with embeddings unready, find() must skip the vector lane
    WITHOUT ever calling embed_texts (the readiness gate fires before the
    model getter, it is not an exception handler), record "embeddings" in
    degraded_out, and rank identically to the natural post-warm fallback —
    proven below: torch is absent in this sandbox, so the post-warm call
    ImportErrors its way to the exact same BM25/keyword-lane ranking."""
    monkeypatch.setenv("EXOMEM_FIND_CACHE_SIZE", "0")
    real_embed_texts = embeddings.embed_texts
    guard = {"forbidden": True}
    call_count = {"n": 0}

    def _guarded(*a, **kw):
        call_count["n"] += 1
        if guard["forbidden"]:
            raise AssertionError("embed_texts must not be called while embeddings are warming")
        return real_embed_texts(*a, **kw)

    monkeypatch.setattr(embeddings, "embed_texts", _guarded)

    readiness.begin_warm()
    degraded: list[str] = []
    mid_warm_hits = find_module.find(
        vault, query="metabolism", mode="hybrid", degraded_out=degraded
    )
    assert call_count["n"] == 0
    assert degraded == ["embeddings"]
    readiness.finish_warm()

    guard["forbidden"] = False  # post-warm: let the real (torch-less) path run
    post_warm_hits = find_module.find(vault, query="metabolism", mode="hybrid")
    assert [h.path for h in mid_warm_hits] == [h.path for h in post_warm_hits]


def test_find_degraded_out_stays_empty_after_finish_warm_natural_fallback(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the warm window has closed, should_defer is False forever —
    find() reverts to today's behavior: embed_texts is genuinely attempted
    (and ImportErrors, since torch isn't installed in this sandbox), the
    silent BM25 fallback applies, and degraded_out stays empty (the
    readiness gate never engages)."""
    monkeypatch.setenv("EXOMEM_FIND_CACHE_SIZE", "0")
    readiness.begin_warm()
    readiness.finish_warm()

    degraded: list[str] = []
    hits = find_module.find(vault, query="metabolism", mode="hybrid", degraded_out=degraded)

    assert hits, "fixture vault should match 'metabolism'"
    assert degraded == []


def test_find_defers_rerank_mid_warm_without_calling_rerank_pairs(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same pattern for the reranker: mid-warm with only the reranker
    unready (embeddings marked ready so the vector lane runs normally),
    find(rerank=True) must skip rerank_pairs entirely and record
    "reranker" in degraded_out."""
    monkeypatch.setenv("EXOMEM_FIND_CACHE_SIZE", "0")
    call_count = {"n": 0}

    def _forbidden(*a, **kw):
        call_count["n"] += 1
        raise AssertionError("rerank_pairs must not be called while the reranker is warming")

    monkeypatch.setattr(embeddings, "rerank_pairs", _forbidden)

    readiness.begin_warm()
    readiness.mark_ready("lexical")
    readiness.mark_ready("embeddings")  # isolate the reranker gate
    degraded: list[str] = []
    hits = find_module.find(
        vault, query="metabolism", mode="hybrid", rerank=True, degraded_out=degraded
    )

    assert call_count["n"] == 0
    assert "reranker" in degraded
    assert hits, "fixture vault should match 'metabolism'"


# ============================================================================
# exomem.commands.op_find — the "warming" envelope
# ============================================================================


def test_op_find_warming_envelope_mid_warm_then_bare_list_after_finish(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """op_find returns {"hits": [...], "warming": {...}} while a lane was
    deferred mid-warm, and reverts to a bare list once finish_warm() runs."""
    monkeypatch.setenv("EXOMEM_FIND_CACHE_SIZE", "0")
    readiness.begin_warm()
    out = commands.op_find(vault, query="metabolism", mode="hybrid")

    assert isinstance(out, dict)
    assert set(out) == {"hits", "warming"}
    assert out["warming"]["components"] == ["embeddings"]
    assert out["warming"]["since_s"] >= 0
    readiness.finish_warm()

    out2 = commands.op_find(vault, query="metabolism", mode="hybrid")
    assert isinstance(out2, list)


# ============================================================================
# exomem.bm25.BM25Index — single-build-under-concurrency lock
# ============================================================================


def test_bm25_build_lock_serializes_concurrent_cold_builds(vault: Path) -> None:
    """Two threads racing .search() on a cold BM25Index must produce exactly
    ONE _build() call — the loser waits on the build lock and reuses the
    winner's freshly-cached corpus instead of rebuilding."""
    idx = bm25.BM25Index()
    real_build = idx._build
    call_count = {"n": 0}
    first_entered = threading.Event()
    release_first = threading.Event()

    def wrapper(vault_root, scope):
        call_count["n"] += 1
        if call_count["n"] == 1:
            first_entered.set()
            release_first.wait(timeout=5.0)
        return real_build(vault_root, scope)

    idx._build = wrapper  # plain function on the instance dict; no auto-`self`

    results: list = [None, None]

    def call_search(i: int) -> None:
        # "EGCG" (unlike "metabolism") isn't a coincidental idf=0 collision in
        # the kb-scope fixture corpus, so it reliably scores > 0 here.
        results[i] = idx.search(vault, "EGCG", 5, scope="kb")

    t1 = threading.Thread(target=call_search, args=(0,))
    t2 = threading.Thread(target=call_search, args=(1,))
    t1.start()
    assert first_entered.wait(timeout=5.0), "first thread should have entered _build"
    t2.start()
    release_first.set()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    assert call_count["n"] == 1
    assert results[0] == results[1]
    assert results[0], "fixture vault should match 'EGCG'"


# ============================================================================
# exomem.embeddings.upsert_after_write — defer-during-warm + exactly-once drain
# ============================================================================


def test_upsert_after_write_defers_during_warm_without_loading_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid-warm, upsert_after_write must park the write via
    readiness.defer("embeddings", ...) instead of touching get_model, and
    mark_ready("embeddings") must return that exact batch exactly once."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)

    def _forbidden() -> None:
        raise AssertionError("get_model must not be called while embeddings are warming")

    monkeypatch.setattr(embeddings, "get_model", _forbidden)

    readiness.begin_warm()
    md_path = tmp_path / "Knowledge Base" / "Notes" / "probe.md"
    embeddings.upsert_after_write(tmp_path, [md_path])

    drained = readiness.mark_ready("embeddings")
    assert len(drained) == 1
    drained_vault, drained_paths = drained[0]
    assert drained_vault == tmp_path
    assert list(drained_paths) == [md_path]
    assert readiness.mark_ready("embeddings") == []  # drained exactly once


def test_upsert_after_write_defer_after_embeddable_filter_logmd_defers_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The embeddable-path filter runs BEFORE the defer call: passing only
    log.md (excluded from embedding) must defer nothing at all."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)

    readiness.begin_warm()
    embeddings.upsert_after_write(tmp_path, [tmp_path / "Knowledge Base" / "log.md"])

    assert readiness.mark_ready("embeddings") == []


# ============================================================================
# `exomem warm` CLI
# ============================================================================


def test_warm_cli_skip_message_when_embeddings_disabled(capsys: pytest.CaptureFixture) -> None:
    """With EXOMEM_DISABLE_EMBEDDINGS set (the suite default), `exomem warm`
    must print a skip message, exit 0, and never import torch."""
    code = main(["warm"])
    out = capsys.readouterr().out

    assert code == 0
    assert "EXOMEM_DISABLE_EMBEDDINGS" in out
    assert "torch" not in sys.modules


def test_warm_cli_success_with_faked_models_clip_left_disabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """`exomem warm` with the models faked light succeeds (exit 0); CLIP
    stays skipped when EXOMEM_DISABLE_CLIP is left set (the suite default)."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "get_model", lambda: object())
    monkeypatch.setattr(embeddings, "get_reranker", lambda: object())
    clip_called: list[int] = []
    monkeypatch.setattr(embeddings, "get_clip_model", lambda: clip_called.append(1) or object())

    code = main(["warm"])
    out = capsys.readouterr().out

    assert code == 0
    assert clip_called == []
    assert "CLIP" in out and "skipped" in out


def test_warm_cli_exit_1_when_a_model_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A raising model loader makes `exomem warm` report FAILED and exit 1."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)

    def _boom() -> None:
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(embeddings, "get_model", _boom)
    monkeypatch.setattr(embeddings, "get_reranker", lambda: object())

    code = main(["warm"])
    err = capsys.readouterr().err

    assert code == 1
    assert "FAILED" in err


def test_warm_cli_vault_flag_also_warms_lexical_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """`exomem warm --vault <path>` also runs warmup.warm_caches for that vault."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "get_model", lambda: object())
    monkeypatch.setattr(embeddings, "get_reranker", lambda: object())
    calls: list[Path] = []
    monkeypatch.setattr(warmup, "warm_caches", lambda vr: calls.append(vr) or {})

    code = main(["warm", "--vault", str(tmp_path)])

    assert code == 0
    assert calls == [tmp_path]


# ============================================================================
# `exomem doctor` — the "models.cache" check
# ============================================================================


def test_doctor_models_cache_warns_when_hf_cache_empty(
    vault: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty/absent HF cache reports "models.cache" as a warn pointing at
    `exomem warm`."""
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))

    report = doctor_module.doctor(vault=str(vault), profile="hybrid")

    check = next(c for c in report.checks if c.id == "models.cache")
    assert check.status == "warn"
    assert "exomem warm" in (check.remediation or "")


def test_doctor_models_cache_passes_when_all_models_cached(
    vault: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """All three model directories present in the HF cache -> "models.cache"
    passes."""
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    hf_home = tmp_path / "hf"
    monkeypatch.setenv("HF_HOME", str(hf_home))
    hub = hf_home / "hub"
    dirnames = [
        "models--" + embeddings.MODEL_NAME.replace("/", "--"),
        "models--" + embeddings.RERANKER_NAME.replace("/", "--"),
        "models--sentence-transformers--" + embeddings.CLIP_MODEL_NAME,
    ]
    for dirname in dirnames:
        snap = hub / dirname / "snapshots" / "x"
        snap.mkdir(parents=True)
        (snap / "f.bin").write_bytes(b"stub")

    report = doctor_module.doctor(vault=str(vault), profile="hybrid")

    check = next(c for c in report.checks if c.id == "models.cache")
    assert check.status == "pass"


# ============================================================================
# Review findings — write-path corpus sweeps and the hot-cache staleness hole
# ============================================================================


def test_corpus_sweep_skipped_mid_warm_without_touching_model(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The duplicate/contradiction sweep that runs inline on add/note/edit
    (and pack assembly) must skip during warm instead of blocking on the
    model lock — _best_cosine_per_file returns its {} no-op without ever
    reaching embed_texts."""
    from exomem import corpus_aware

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("embed_texts must not be called during warm")

    monkeypatch.setattr(embeddings, "embed_texts", _forbidden)
    monkeypatch.setattr(embeddings, "chunk_text", lambda title, body: ["chunk"])

    readiness.begin_warm()
    out = corpus_aware._best_cosine_per_file(vault, title="t", body="draft body")
    assert out == {}


def test_degraded_find_never_cached_even_without_degraded_out(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Internal find() callers (suggest_links, evolution, note/add sweeps)
    pass no degraded_out. A lexical-only ranking produced mid-warm must still
    be kept OUT of the hot cache, or it would be served after the warm ends."""
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)

    readiness.begin_warm()
    find_module.find(vault, query="metabolism", mode="hybrid")
    with find_module._FIND_CACHE_LOCK:
        assert not find_module._FIND_CACHE, "degraded mid-warm result was cached"

    readiness.finish_warm()
    find_module.find(vault, query="metabolism", mode="hybrid")
    with find_module._FIND_CACHE_LOCK:
        assert find_module._FIND_CACHE, "post-warm results must cache normally"
