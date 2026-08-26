# Tasks — instant-start boot

## 1. Tests First

- [x] 1.1 Add `tests/test_instant_start.py` covering `readiness.py`'s state machine in isolation
      (no torch, no server): `begin_warm`/`finish_warm` reset and close the warm window;
      `should_defer` is true only while active+unfinished+unready and false before `begin_warm`,
      after `mark_ready`, and after `finish_warm` regardless of readiness; `is_warming` and
      `warming_info()` shape (`components`, `since_s`); `reset()` test hook.
- [x] 1.2 Add tests for `defer`/`mark_ready` atomicity: items recorded via `defer()` while a
      component is unready are returned exactly once by the `mark_ready()` that follows; a `defer()`
      call after `mark_ready()` (or after `finish_warm()`) returns `False` (caller proceeds inline)
      instead of being silently swallowed.
- [x] 1.3 Add `warm_all` ordering tests using injected fake preload/warm functions (monkeypatched,
      no real models): lexical readiness lands before `embeddings`, `embeddings` before `reranker`,
      `reranker` before `clip`; a fake preload failure leaves its component permanently not-ready
      for that warm window and does not stop later stages; durations dict includes every stage that
      ran.
- [x] 1.4 Add `start_background` tests: `begin_warm()` has already run by the time the function
      returns (a caller can observe `should_defer` truthy immediately after the call); the thread is
      named `exomem-warm` and daemonized; `finish_warm()` runs even when the target raises
      (simulate via a monkeypatched `warm_all` that throws).
- [x] 1.5 Add request-path defer-gate tests for `find._find_semantic` using injected readiness
      state (`EXOMEM_DISABLE_WARMUP` off, component marked not-ready directly via the readiness
      module): vector lane is skipped and reported skipped/degraded without calling
      `embeddings.get_model`; CLIP lane skipped without calling `embeddings.get_clip_model`; rerank
      stage skipped without calling `embeddings.get_reranker`; keyword/BM25 lanes are unaffected by
      any component's readiness.
- [x] 1.6 Add tests proving a deferred lane never touches the corresponding model getter at all
      (patch the getter to raise if called) and that the call still returns promptly with the
      lanes that were ready.
- [x] 1.7 Add `op_find` envelope tests: a call with no deferred lane returns the current bare
      `list`/pack shape unchanged; a call with at least one deferred lane returns
      `{"hits": [...], "warming": {"components": [...], "since_s": N}}`; `pack=true` composes with
      `warming` the same way it already composes with `timings`.
- [x] 1.8 Add `embeddings.upsert_after_write` deferred-write tests: a write while `embeddings` is
      not ready records a deferred item and does not call `get_model`; the drain triggered by
      `mark_ready("embeddings")` embeds every deferred file exactly once; a second, unrelated drain
      does not re-embed already-drained files.
- [x] 1.9 Add `EXOMEM_EAGER_BOOT` tests against `server.build_server` (embeddings/warmup
      monkeypatched to fast fakes): eager boot calls the full warm sequence synchronously before
      returning, and no readiness component ever reports `should_defer` as true afterward for that
      process.
- [x] 1.10 Add `bm25.BM25Index._fresh_corpus` and `find._get_query_resolver` concurrency tests: two
      threads racing a cold build both get a correct, identical result and the underlying build
      function runs once (assert call count), matching the existing `get_model`-style
      double-checked-lock contract.
- [x] 1.11 Add `doctor` tests for a new `models.cache` check under `hybrid`/`media` profiles:
      passes when the HF hub cache already contains the model directories, warns with remediation
      "run `exomem warm`" when one or more are missing, and never triggers a network call or
      download (assert no `huggingface_hub` download function is invoked).
- [x] 1.12 Add CLI tests for `exomem warm`: exits `0` and reports per-step durations when preloads
      succeed (mocked models); exits `1` when a required preload fails; `--vault` additionally
      warms lexical caches (assert the warm helper is called); skips model preloads with an
      explanatory message and still exits `0` when `EXOMEM_DISABLE_EMBEDDINGS` is set.

## 2. Readiness Module

- [x] 2.1 Add `src/exomem/readiness.py`: `COMPONENTS = ("lexical", "embeddings", "reranker",
      "clip")`, per-component `threading.Event`s, a single `threading.Lock` guarding
      begin/finish/mark/defer state.
- [x] 2.2 Implement `begin_warm()` / `finish_warm()` / `mark_ready(component)` /
      `is_ready(component)` / `is_warming()` / `should_defer(component)` per the design's narrow
      semantics (defer only while active, unfinished, and unready; permanently false after
      `finish_warm`).
- [x] 2.3 Implement `defer(component, item) -> bool` sharing the same lock as `mark_ready` so an
      item is never lost in the set-event/drain window, and `mark_ready` returns the drained list
      atomically.
- [x] 2.4 Implement `warming_info()` (`{"components": [...], "since_s": N}` or `None`) and a
      `reset()` test hook mirroring `find.clear_cache()`.

## 3. Warm Sequence

- [x] 3.1 Add `warmup.warm_all(vault_root)`: run the existing lexical warm steps, call
      `readiness.mark_ready("lexical")`, then preload embeddings/reranker/CLIP in order, calling
      `readiness.mark_ready(...)` after each successful step (draining and re-embedding deferred
      write items right after `embeddings` becomes ready); keep every step soft-failing and
      duration-tracked; log a final `warm complete: <durations>` line.
- [x] 3.2 Add `warmup.start_background(vault_root)`: call `readiness.begin_warm()` synchronously
      before spawning, run `warm_all` on a `daemon=True` thread named `exomem-warm`, and call
      `readiness.finish_warm()` in a `finally` so a crashed warm thread cannot leave the process
      deferring forever.

## 4. Boot Wiring

- [x] 4.1 Replace `server.py`'s inline preload + `warmup.warm_caches` boot block with: if
      `EXOMEM_EAGER_BOOT` is truthy, call `warmup.warm_all(vault_root)` synchronously; else, when
      warm-up is enabled, call `warmup.start_background(vault_root)` and continue immediately to
      the rest of `build_server()`.
- [x] 4.2 Confirm the same boot block runs for both stdio and http transports (no transport-specific
      branching in the warm-up wiring).

## 5. Request-Path Defer Gates

- [x] 5.1 In `find._find_semantic`, check `readiness.should_defer("embeddings")` before the vector
      lane calls `embeddings.EmbeddingIndex`/`embed_texts`; when deferring, skip the lane, record it
      skipped in `timings`, and add `"embeddings"` to a request-scoped degraded list threaded back
      to the caller.
- [x] 5.2 Add the same gate for the CLIP lane (`readiness.should_defer("clip")` before
      `embeddings.ClipIndex`/`embed_clip_text`) and the rerank stage (`readiness.should_defer
      ("reranker")` before `embeddings.get_reranker`).
- [x] 5.3 Thread the degraded-component list from `_find_semantic` back through to `op_find` (or an
      equivalent request-scoped signal) so the command layer can build the `warming` marker without
      re-deriving it from timings.
- [x] 5.4 In `embeddings.upsert_after_write`, check `readiness.should_defer("embeddings")` before
      calling `get_model()`; when deferring, call `readiness.defer("embeddings", (vault_root,
      md_paths))` instead of embedding inline.
- [x] 5.5 Add a double-checked build lock to `bm25.BM25Index._fresh_corpus` (lock around the
      freshness-key check + rebuild, fast path stays cheap for an already-fresh corpus) and the
      same pattern to `find._get_query_resolver`.

## 6. Response Envelope

- [x] 6.1 Update `op_find` in `commands.py` to add a `warming` sibling field
      (`{"components": [...], "since_s": N}`) to the returned envelope only when at least one lane
      was deferred, composing with the existing `pack`/`timings` envelope fields.
- [x] 6.2 Confirm the bare-list default return shape is unchanged when nothing was deferred.

## 7. CLI: Explicit Warm Command

- [x] 7.1 Add `exomem warm` to `__main__.py` following the existing admin-subcommand pattern
      (`doctor`, `backfill-media`): preload embedding model, reranker, and (when `clip_enabled()`)
      CLIP with HF progress bars on a TTY, per-step durations, exit `0`/`1`.
- [x] 7.2 Add `--vault PATH` to additionally run the lexical warm steps for that vault.
- [x] 7.3 Respect `EXOMEM_DISABLE_EMBEDDINGS`: skip model preloads with an explanatory message and
      exit `0`.

## 8. Doctor: Models Cache Check

- [x] 8.1 Add `_check_models_cache()` (or equivalent) to `doctor.py`: read-only inspection of the
      local Hugging Face hub cache directories for `embeddings.MODEL_NAME`, `RERANKER_NAME`, and (on
      the `media` profile, when CLIP is enabled) `CLIP_MODEL_NAME` — no network call, no download.
- [x] 8.2 Wire the check into the `hybrid` and `media` profile branches of `doctor()`; warn-level
      status naming missing models with remediation "run `exomem warm`".

## 9. Test Harness Updates

- [x] 9.1 Add `EXOMEM_DISABLE_WARMUP=1` to the autouse fixture in `tests/conftest.py` so the suite
      never spawns the `exomem-warm` thread.
- [x] 9.2 Confirm `tests/test_instant_start.py`'s readiness/warm tests explicitly manage
      `readiness.reset()` / their own env vars where they need warm-up enabled, rather than relying
      on suite-wide defaults.

## 10. Validation

- [x] 10.1 Run targeted tests: `test_instant_start.py`, `find.py`/`embeddings.py`/`bm25.py`
      touch points, doctor, CLI.
- [x] 10.2 Run the full suite: `PYTHONPATH=src EXOMEM_DISABLE_EMBEDDINGS=1 python -m pytest -q`.
- [x] 10.3 Run `ruff check` clean on all touched files.
- [x] 10.4 Run `npm exec --yes @fission-ai/openspec -- validate add-instant-start-boot --strict`.
- [x] 10.5 Regenerate `docs/capabilities.md` via `python scripts/generate-capabilities.py` and
      confirm the diff reflects `op_find`'s new `warming` envelope field.
- [x] 10.6 Pure-substrate check: `readiness.py` contains no model/embedding import and no
      note-content access — it only tracks process/thread state; the `warming` marker exposes that
      state, never a ranking judgment; `exomem warm` triggers only the same model loads that already
      exist, with no new inference path.
