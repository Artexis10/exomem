## Why

Exomem's multimodal pipeline is a core product differentiator, but its current in-process
media thread can retain multi-gigabyte ASR/CLIP/MPS/CUDA state for the lifetime of every
server process. The default product path must accept and enrich multimodal evidence without
making an idle machine materially worse to use or requiring users to understand model
residency and service-process fan-out.

## What Changes

- Make a `standard` capability profile the default native product install: hybrid retrieval,
  document/PDF extraction, OCR, ASR, and CLIP are available without a reinstall; `lean`,
  `hybrid`, and `media` remain accepted compatibility profiles.
- Replace the in-process media extraction thread with a lightweight supervisor, durable
  SQLite job ledger, and one serialized child worker process that starts on demand, stays
  warm for a bounded burst, and exits after idle to reclaim RAM plus MPS/MLX/CUDA state.
- Keep model residency and prewarm off at startup on every OS. Model-backed extraction is
  deterministic transduction (pure-substrate measurement), remains soft-fail, and never
  prevents the core service from starting or serving lexical retrieval.
- Make queued media state explicit and recoverable across worker/service crashes, including
  extraction, CLIP, and post-frame re-embedding jobs.
- Turn normal-mode model idle release on by default and expose media queue/worker residency
  through the no-allocation resource status and doctor surfaces.
- Bound live semantic indexing by chunk count and durable deferred work rather than flattening
  an arbitrarily large write batch in one model call.
- Keep advanced generated captioning and speaker diarization default-off; they remain optional
  enrichments rather than requirements for the standard multimodal claim.

## Capabilities

### New Capabilities

- `multimodal-job-runtime`: Durable multimodal jobs, serialized disposable worker lifecycle,
  crash recovery, soft-fail behavior, and observable job/worker state.

### Modified Capabilities

- `install-readiness`: Native product installs default to the standard multimodal dependency
  set while preserving explicit low-resource and compatibility profiles.
- `resource-governance`: Normal mode releases idle model residency and defines a measurable
  persistent-core resource envelope independent of transient worker usage.
- `live-index-freshness`: Live semantic writes use bounded chunk batches and durable deferral
  so imports cannot create unbounded model work.
- `command-surface`: Resource status and doctor report media queue depth, active worker state,
  and remediation without importing or loading model stacks.

## Impact

- Runtime: `media_worker.py`, a new durable job-store/child-worker boundary, server startup,
  model/reaper policy, extraction/CLIP call sites, and resource status.
- Installation: PyPI extras/profile mapping in Unix and Windows service installers, doctor
  profile validation, setup defaults, and deployment documentation.
- Storage: one rebuildable per-vault `.media-jobs.sqlite` sidecar under the governed KB root;
  no user-authored Markdown schema changes.
- Compatibility: current enqueue callers and legacy profile names remain supported; failed or
  missing optional engines leave durable work visible and the core service operational.
- Verification: deterministic unit/integration tests for queue recovery, one-worker
  serialization, idle exit, process crash recovery, bounded embedding batches, profile mapping,
  and no-allocation status, plus real-host macOS/Windows/Linux resource acceptance guidance.
