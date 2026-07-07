## 1. Policy And Tests

- [x] 1.1 Add unit tests for `mode.normalize()` covering `quiet`, `normal`, `performance`, `gpu`, `turbo`, `resource-saver`, and `low-resource`.
- [x] 1.2 Add unit tests for `mode.resolved()` resource fields: CPU-cache preload, CPU-cache retention, expensive-index deferral, watcher policy, idle release, and bulk GPU policy.
- [x] 1.3 Add tests proving `EXOMEM_MODE` remains the highest-precedence hard pin and cannot be overridden by config-file or auto-quiet state.
- [x] 1.4 Add tests proving policy/status helpers do not import torch in a lean process.
- [x] 1.5 Add fake-probe tests for CPU-only, CUDA probe exception, low free VRAM, and capable GPU behavior.

## 2. Resource Policy

- [x] 2.1 Extend `mode.py` aliases so accepted low-resource aliases normalize to `quiet`.
- [x] 2.2 Add torch-free policy helpers in `mode.py` for CPU-cache preload, CPU-cache retention, expensive-index deferral, and watcher policy.
- [x] 2.3 Include new resource policy fields in `mode.resolved()` and `exomem mode --json`.
- [x] 2.4 Update `mode.apply_live()` so entering quiet unloads model singletons and heavy RAM caches.
- [x] 2.5 Preserve existing env/config precedence, atomic config writes, and live mode-watch behavior.

## 3. Cache Hooks And Reaper

- [x] 3.1 Add `embeddings.unload_index_caches()` and `embeddings.index_cache_status()` with tests for loaded and absent embedding/CLIP matrix caches.
- [x] 3.2 Add `bm25.unload_cache()` and `bm25.cache_status()` with tests proving later searches rebuild correctly.
- [x] 3.3 Add `find.unload_ram_caches()` and `find.cache_status()` without clearing freshness or inbound-link metadata.
- [x] 3.4 Audit `lexstore` residency and add status/unload hooks only if it holds material RAM.
- [x] 3.5 Generalize `model_reaper.py` from model-only slots to generic resource slots while preserving start/stop behavior.
- [x] 3.6 Add default reaper slots for models, embedding/CLIP matrices, BM25 caches, and find RAM caches.
- [x] 3.7 Add tests proving the reaper unloads only loaded, idle, not-in-flight resources and soft-fails per slot.

## 4. Quiet Startup And Find Correctness

- [x] 4.1 Update `warmup.warm_caches()` so quiet skips parsed-page, BM25, resolver, embedding-matrix, and CLIP-matrix warm-up.
- [x] 4.2 Preserve normal/performance warm-up behavior.
- [x] 4.3 Add tests proving quiet startup marks readiness correctly and does not strand deferred writer work.
- [x] 4.4 Add tests proving quiet startup does not call heavy CPU/vector warm-up helpers.
- [x] 4.5 Add a regression test proving `find` after quiet cold start returns the same ranked paths as a warm-cache request over the same state.

## 5. Watcher, Reconcile, And Deferred Work

- [x] 5.1 Add tests proving `FileWatcher` reads watcher policy at flush/reconcile time so mode changes apply without restart.
- [x] 5.2 Add quiet-mode burst coalescing tests for filesystem events.
- [x] 5.3 Split index dispatch into cheap lanes and expensive lanes behind a shared dispatcher.
- [x] 5.4 Add a deferred-work registry for expensive quiet-mode semantic/visual indexing with safe dedupe and status counts.
- [x] 5.5 In quiet mode, update watcher flush so freshness/inbound/resolver work runs promptly while embedding/CLIP work is queued or capped.
- [x] 5.6 In quiet mode, update periodic reconcile so freshness registries are corrected but large semantic/visual reindex work is capped or deferred.
- [x] 5.7 Preserve non-quiet watcher behavior: external edits still upsert/delete embedding rows promptly when policy does not defer expensive work.
- [x] 5.8 Add tests proving explicit index/reconcile processing clears corresponding deferred work.

## 6. CLI, Doctor, And Setup

- [x] 6.1 Add no-allocation resource status JSON with effective mode, source, config path, policy fields, loaded model flags, cache residency, deferred work, and CUDA accounting when already initialized.
- [x] 6.2 Add tests proving resource status does not import torch, load models, create sidecars, read matrices, or initialize CUDA solely to answer status.
- [x] 6.3 Add CLI tests for low-resource aliases persisting canonical `quiet`.
- [x] 6.4 Add a no-allocation doctor resource-posture check for effective mode, CPU fallback, GPU availability, and marginal-GPU remediation.
- [x] 6.5 Add doctor tests for CPU-only and marginal-GPU hosts across lean/hybrid profiles.
- [x] 6.6 Update setup messaging so GPU performance mode remains explicit opt-in and quiet/resource-status commands appear in next steps.

## 7. Optional Auto-Quiet

- [x] 7.1 Add pure decision tests for auto-quiet hysteresis: enter quiet after sustained pressure, restore after clear, and do not restore over manual changes.
- [x] 7.2 Implement optional/default-off `auto_quiet.py` using non-torch pressure probes.
- [x] 7.3 Ensure auto-quiet never imports torch or initializes CUDA solely for pressure polling.
- [x] 7.4 Add tests proving unavailable probes soft-fail and do not change mode.
- [x] 7.5 Wire auto-quiet startup only when explicitly enabled by env/config.

## 8. Verification And Docs

- [x] 8.1 Run targeted tests for mode policy, accel fallback, cache hooks, reaper behavior, warm-up policy, watcher deferral, CLI status, doctor posture, and auto-quiet decisions.
- [x] 8.2 Run `openspec validate --changes`.
- [x] 8.3 Run `ruff check` on touched Python files.
- [x] 8.4 Run `uv run python -m pytest -q` with embeddings disabled where appropriate.
- [x] 8.5 Add desk-side verification notes for idle VRAM and RAM before/after quiet mode.
- [x] 8.6 Update README/QUICKSTART or relevant docs for `quiet`, `normal`, `performance`, resource status, and quiet-mode freshness trade-offs.
