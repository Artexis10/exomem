## 1. Durable Media Jobs

- [x] 1.1 Add the per-vault media job SQLite path and schema with pending/running/blocked/failed states.
- [x] 1.2 Implement idempotent enqueue, atomic claim, completion, failure, recovery, retry, and no-create status APIs.
- [x] 1.3 Add job-store tests for deduplication, stage merging, crash recovery, and concurrent claims.

## 2. Disposable Worker Runtime

- [x] 2.1 Extract current media stage processing behind a child-safe processor with a durable enqueue callback.
- [x] 2.2 Implement the child worker loop, per-vault OS lock, parent-liveness check, and idle exit.
- [x] 2.3 Replace the in-process production thread with a supervisor that launches, monitors, joins, and stops the child.
- [x] 2.4 Preserve an explicit inline execution mode for deterministic tests and emergency rollback.
- [x] 2.5 Update startup scans so pending extraction, CLIP drift, and frame re-embedding enqueue durable jobs.
- [x] 2.6 Add tests for one-worker serialization, idle exit, child crash recovery, soft-fail blocking, and scene-frame follow-up ordering.

## 3. Safe Resource Defaults

- [x] 3.1 Disable ASR prewarm by default on every platform while preserving explicit opt-in.
- [x] 3.2 Make normal mode skip model and O(vault) cache preloads and run the idle reaper by default.
- [x] 3.3 Ensure normal-mode model/cache slots reclaim after idle and update mode/warmup/reaper tests.
- [x] 3.4 Add media queue/worker state to no-allocation resource status and doctor remediation.
- [x] 3.5 Add tests proving status/doctor do not import heavy model modules or create sidecars.

## 4. Bounded Semantic Work

- [x] 4.1 Split live embedding encode calls by `EXOMEM_LIVE_EMBED_MAX_CHUNKS`, including oversized single files.
- [x] 4.2 Persist deferred semantic paths in a rebuildable per-vault SQLite sidecar.
- [x] 4.3 Update index dispatch, drain/clear/status, and explicit heal paths to use the durable registry.
- [x] 4.4 Add tests for bounded ordering, restart durability, deduplication, and successful clearing.

## 5. Standard Multimodal Profile

- [x] 5.1 Add `standard` to doctor/CLI/setup profile handling with soft-fail OCR-tool remediation.
- [x] 5.2 Make Unix and Windows release installers default to standard and map platform extras correctly.
- [x] 5.3 Preserve lean/hybrid/media compatibility mappings and update installer/profile contract tests.
- [x] 5.4 Update quickstart, deployment, README, and env guidance around capability versus residency.

## 6. Verification And Delivery

- [x] 6.1 Add a cross-platform desk-side resource verification script and document the acceptance envelope.
- [x] 6.2 Run focused media/resource/profile tests, PowerShell and shell syntax checks, and installer tests.
- [ ] 6.3 Run the full pytest suite, Ruff on touched Python, and strict OpenSpec validation.
- [x] 6.4 Review the final diff for compatibility and secret/personal-data leakage.
