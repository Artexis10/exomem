## Why

Exomem must not make the host machine worse to use while it is idle. On a single
gaming/dev box, the current failure mode is concrete: the server can occupy enough
VRAM and RAM, and run enough background indexing work, to change foreground system
behavior before the user has made a query.

This change completes quiet mode as a real low-resource operating state, not only
a CUDA policy. The goal is that leaving Exomem running should be compatible with
gaming and other foreground workloads: no idle GPU residency, bounded idle RAM,
deferred/healed background work, and a visible way to enter and inspect that state.

## What Changes

- Tighten the compute modes around explicit resource posture:
  - `quiet`: low-resource / do-not-interrupt mode. No CUDA use, no heavy startup
    preloads, no large find/index caches held merely because the server is idle,
    idle unload enabled, and background maintenance throttled.
  - `normal`: safe default. CPU steady-state, CUDA only for short-lived bulk CLI
    work when capable, normal latency-oriented CPU caches allowed.
  - `performance`: explicit opt-in to GPU and warm caches for speed.
- Add RAM residency controls for quiet mode:
  - Do not warm or retain embedding/CLIP matrices, BM25 corpora, resolver/page
    caches, or other O(vault) structures unless an actual request needs them.
  - Evict large CPU-side caches after an idle window or when switching into quiet.
  - Keep correctness: the first query may pay cold-cache cost, but it must return
    the same result as normal mode.
- Keep the existing GPU protections and make them measurable:
  - CUDA remains opt-in and capability-gated.
  - Reranker and CLIP remain lazy and never preload in quiet.
  - Idle unload still calls model unload and best-effort CUDA cache release.
  - Add diagnostics that report current mode, model residency, CUDA memory
    accounting when available, and large CPU cache residency.
- Make watcher/reconcile behavior mode-aware:
  - In quiet mode, coalesce filesystem bursts more aggressively, avoid
    user-visible CPU spikes, and cap/defer reconcile-driven re-embedding.
  - Prevent O(corpus) sidecar repair from running repeatedly on the hot path.
  - Maintain eventual correctness through deferred batches, explicit reconcile,
    and logged drift/caps.
- Add an optional auto-quiet layer:
  - Default off and soft-fail: if GPU-pressure/fullscreen-process detection is
    unavailable, Exomem behaves exactly as manual mode does.
  - When enabled, observe local machine pressure signals and switch to quiet when
    the GPU or system memory is under pressure, then restore the previous mode
    after the pressure clears.
  - This is pure substrate measurement and policy switching: it runs no reasoning
    model and makes no note-content judgment.
- Extend mode UX without requiring code edits:
  - Keep `exomem mode quiet|normal|performance`.
  - Add low-resource aliases such as `resource-saver`/`low-resource` only if they
    normalize to `quiet`.
  - Expose a stable JSON status suitable for scripts and a pre-game routine.

No breaking changes are intended. Existing env overrides and manual GPU opt-in
remain available for users who deliberately want the old high-throughput behavior.

## Capabilities

### New Capabilities

- `resource-governance`: Defines Exomem's machine-resource modes, resource-saver
  guarantees, RAM/VRAM residency expectations, idle unload behavior, optional
  auto-quiet switching, and diagnostics. This is the main contract for "Exomem
  must not negatively affect foreground system use."

### Modified Capabilities

- `find-recall-efficiency`: Quiet mode changes the residency policy for find's
  large CPU-side structures. The requirement change is that `find` remains
  correct when matrices/corpora/resolver/page caches are lazy or evicted, while
  normal/performance modes may still keep latency-oriented caches warm.
- `live-index-freshness`: Watcher and periodic reconcile behavior become
  mode-aware. Quiet mode may defer, coalesce, throttle, and cap background repair
  work, while preserving eventual freshness and explicit reconcile correctness.
- `command-surface`: The CLI must expose mode switching and resource status in a
  scriptable form, including low-resource aliases that map to quiet mode and JSON
  status suitable for automation.
- `install-readiness`: Doctor/setup checks must treat CPU-only and marginal-GPU
  hosts as first-class supported configurations, and should surface the current
  resource posture and remediation without loading models or allocating CUDA.

## Impact

- Affected code:
  - `src/exomem/mode.py`: resource-mode policy, aliases, live mode application.
  - `src/exomem/accel.py`: GPU capability/headroom diagnostics and CUDA-safe
    accounting.
  - `src/exomem/model_reaper.py`: idle unload for model and large-cache slots.
  - `src/exomem/warmup.py`: quiet-mode cache warm-up suppression.
  - `src/exomem/embeddings.py`: embedding/CLIP matrix residency, lazy reload, and
    unload hooks.
  - `src/exomem/bm25.py`, `src/exomem/find.py`, resolver/page caches: RAM cache
    accounting and eviction hooks.
  - `src/exomem/file_watcher.py`, `src/exomem/reconcile.py`,
    `src/exomem/index_sync.py`: quiet-mode debounce, batch caps, and deferred
    repair.
  - `src/exomem/__main__.py`, `src/exomem/doctor.py`, setup paths: CLI/status and
    no-allocation diagnostics.
- APIs and surfaces:
  - Existing `exomem mode` remains the baseline control.
  - JSON status may gain resource fields for current mode, residency, and policy.
  - Optional auto-quiet configuration is default-off and soft-fail.
- Dependencies:
  - No mandatory new heavy dependency.
  - Any OS/GPU pressure detector must be optional, platform-gated, and degrade to
    manual mode when unavailable.
- Verification:
  - Unit tests for mode policy, cache eviction decisions, watcher throttling, and
    no-CUDA/no-torch soft failure.
  - Integration or desk-side checks for idle VRAM near zero in quiet/normal when
    GPU features are unused.
  - Regression checks that `find` results stay identical across warm and evicted
    cache states, with latency allowed to trade off only in quiet mode.
