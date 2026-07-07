## Context

The v0.10.0 codebase already contains the core GPU residency fix: `mode.py`
defines `quiet|normal|performance`, `accel.py` makes CUDA opt-in and
headroom-gated, `warmup.py` skips model preloads in quiet, and
`model_reaper.py` unloads idle model singletons. That solves the largest idle
VRAM problem, but it does not yet make quiet mode a complete low-resource state.

The remaining user-visible problem is broader resource residency and background
work:

- large CPU caches can remain resident after warm-up or queries: parsed pages,
  hot `find` results, resolver state, BM25 corpora/tokens, embedding matrices,
  and CLIP matrices;
- writer/watcher/reconcile paths can perform CPU-heavy indexing while the user is
  trying to game or run another foreground workload;
- the current status surface explains the mode policy, but not what is actually
  resident or deferred.

The design constraint is to keep Exomem correct and self-hostable while allowing
quiet mode to trade latency and freshness for lower host interference. CPU-only
and marginal-GPU hosts remain first-class. Optional detectors must be default-off
and soft-fail. Any detector is substrate measurement only: it observes process,
GPU, memory, or window state and switches policy; it never reasons over notes.

## Goals / Non-Goals

**Goals:**

- Make `quiet` mean a full resource-saver posture:
  - no CUDA allocation by default;
  - no heavy startup preloads;
  - large CPU-side caches are lazy and evictable;
  - idle unload reclaims model and cache residency;
  - watcher/reconcile work is coalesced, capped, or deferred.
- Preserve result correctness when caches are evicted. A cold query may be slower,
  but it must compute from disk/sidecars correctly.
- Make semantic freshness in quiet explicit. Lightweight lexical/freshness
  updates remain live; expensive embedding/CLIP work may be deferred and surfaced.
- Keep `normal` as a safe default: CPU steady-state, no CUDA residency, but normal
  latency-oriented CPU caches may remain warm.
- Keep `performance` as the explicit high-throughput opt-in.
- Provide a scriptable status surface that reports policy, residency, and
  deferred work without loading torch models or allocating CUDA.
- Make auto-quiet optional, default-off, and safe when probes are unavailable.

**Non-Goals:**

- No retrieval architecture rewrite, ANN backend, or new vector database.
- No mandatory tray app in this change. CLI/config-file control remains the
  baseline; a tray companion can be sized separately.
- No server-side reasoning model, ranking judgment, or note-content inference.
- No guarantee that quiet mode has the same latency as normal/performance. Quiet
  intentionally favors foreground system behavior over warm-cache latency.
- No guarantee that recently edited files have immediately fresh semantic/CLIP
  rows while quiet mode is actively deferring expensive work. The lexical lane,
  freshness metadata, and explicit reconcile/index paths remain the correctness
  backstop.

## Decisions

### D1. Put policy in `mode.py`, not in each resource owner

Add a small torch-free resource policy API to `mode.py`, for example:

- `preload_cpu_caches() -> bool`
- `retain_cpu_caches() -> bool`
- `defer_expensive_indexes() -> bool`
- `watcher_policy() -> WatcherPolicy`
- `resolved() -> dict` including the above fields

Existing helpers (`preload_models`, `release_when_idle`, `bulk_gpu_opted`) stay
as compatibility wrappers over the same policy.

Rationale: `mode.py` already owns the per-machine mode, config-file resolution,
and live application. Keeping policy there avoids duplicating string checks in
`warmup`, `find`, `embeddings`, and `file_watcher`, and preserves the current
torch-free CLI dispatch path.

Alternatives considered:

- Read `EXOMEM_MODE` directly in every module. Rejected because it scatters
  policy and makes live mode changes inconsistent.
- Move mode policy into `accel.py`. Rejected because quiet mode now governs RAM
  and CPU background work, not only torch/CUDA placement.

### D2. Generalize the idle reaper to resource slots

Extend `model_reaper.py` from model-only slots to generic idle resource slots
while keeping the module entry points stable. The current `ModelSlot` shape is
already close to the required contract:

- `name`
- `is_loaded()`
- `inflight()`
- `last_activity()`
- `unload() -> bool`

Add slots for large CPU resources:

- bge/reranker/CLIP model singletons (existing);
- embedding matrix caches and CLIP matrix caches;
- BM25 corpora/token caches;
- parsed page cache, resolver cache, and hot find-result cache;
- optional lexical sidecar in-memory state if it is proven to hold meaningful RAM.

Each resource owner exposes a narrow unload/status hook. The reaper coordinates
only through the generic slot contract; it does not know how a BM25 index or
embedding matrix is internally represented.

Rationale: one idle loop already exists, is mode-aware, and is tested. Reusing it
keeps live mode switching simple: entering quiet calls `apply_live()`, unloads
models immediately, clears heavy caches, and ensures the reaper stays running.

Alternatives considered:

- Add separate reaper threads per module. Rejected because independent timers are
  harder to reason about and can reintroduce background noise.
- Clear every cache on every request end. Rejected because it would make quiet
  mode unnecessarily slow and would punish short bursts of legitimate KB use.

### D3. Add explicit unload/status hooks to heavy cache owners

Implement narrow hooks rather than using broad test-only reset functions.

Suggested hooks:

- `embeddings.unload_index_caches()`: iterates process-shared
  `EmbeddingIndex`/`ClipIndex` instances and atomically drops their `_cache`
  matrices without deleting the index objects themselves.
- `embeddings.index_cache_status()`: reports row counts and `matrix.nbytes`
  where loaded.
- `bm25.unload_cache()` / `bm25.cache_status()`: clears `_INDEX` corpora and
  token caches, and reports corpus/token counts.
- `find.unload_ram_caches()` / `find.cache_status()`: clears parsed pages,
  resolver cache, and hot find cache, without clearing freshness registries or
  inbound-link indexes unless explicitly requested.
- `lexstore.cache_status()` and an unload hook only if the current store cache is
  confirmed to hold material RAM beyond SQLite handles and small metadata.

Do not use `find.clear_cache()` as the quiet-mode unload path because it also
clears freshness and inbound state. In quiet mode the target is to remove heavy
content/corpus residency while preserving lightweight metadata that prevents
avoidable O(corpus) work.

Concurrent safety rule: every unload hook swaps cache references to `None` or an
empty dict under the module's existing lock. It must not mutate a matrix/list that
an in-flight query may currently be reading. Existing queries may keep local
references until they finish; future queries cold-load if needed.

Rationale: explicit hooks give diagnostics and unload behavior a stable contract
without changing the retrieval logic.

### D4. Make startup warm-up mode-aware for CPU caches

Change `warmup.warm_all()` and `warmup.warm_caches()` so quiet mode skips heavy
CPU cache warm-up in addition to model preloads. In quiet:

- do not walk all pages just to fill `find._CACHE`;
- do not warm BM25 corpora;
- do not warm resolver state;
- do not touch embedding or CLIP matrices.

Readiness behavior should still mark components so requests do not remain
deferred forever. The first request computes what it needs lazily.

Normal mode may keep today's CPU warm-up because it avoids CUDA and favors lower
first-query latency. Performance mode keeps both CPU cache warm-up and model
preloads.

Rationale: quiet mode's idle RAM target is incompatible with warming O(vault)
structures on boot.

### D5. Defer expensive semantic/visual indexing in quiet mode

Split index maintenance into cheap and expensive lanes:

- cheap lanes: freshness registry, inbound/resolver patching, lexical sidecar
  updates when they do not trigger O(corpus) repair;
- expensive lanes: bge embedding, CLIP embedding, full lexical heal, and large
  reconcile drift re-embedding.

In quiet mode, writer/watcher paths should do cheap lanes promptly and queue or
cap expensive lanes. The deferred queue should be durable enough for correctness
across a server restart if feasible; otherwise it must be reconstructable by
existing drift/reconcile checks. A pending-expensive-work status counter is
required either way.

When leaving quiet mode, a background flusher may process the queued expensive
lanes according to the new mode:

- `normal`: CPU, modest batches;
- `performance`: GPU-capable batches when policy allows;
- explicit `exomem index` / `reconcile`: process all pending work under user
  control.

Find behavior while expensive lanes are deferred:

- keyword/BM25 and graph freshness should reflect the latest markdown;
- vector/CLIP results may lag for changed files;
- timing/status should surface that semantic or visual indexes have deferred
  work, rather than pretending the sidecar is current.

Rationale: a quiet pre-game state cannot also promise immediate semantic
re-embedding for every filesystem or writer event. The important product promise
is that Exomem does not interrupt the foreground workload, while correctness can
be healed when the user leaves quiet mode or requests an explicit index.

Alternatives considered:

- Keep embedding-on-write in quiet because CPU is "safe". Rejected because the
  user problem includes RAM and CPU spikes, not only VRAM.
- Disable the watcher entirely in quiet. Rejected because it would lose cheap
  freshness/inbound maintenance and make later reconciliation more expensive.

### D6. Make watcher and reconcile read mode policy at runtime

`FileWatcher` should consult a mode-derived policy instead of only constructor
constants. Suggested fields:

- `debounce_seconds`
- `reconcile_interval_seconds`
- `max_embed_files_per_batch`
- `max_reconcile_embed_files`
- `defer_expensive_indexes`

Normal keeps today's behavior or close to it. Quiet increases debounce, lowers
per-cycle caps, avoids BM25 warm-after-reconcile if that would materialize a
large corpus, and queues expensive work instead of immediately embedding large
drift batches.

The reconcile loop should not need a restart to observe mode changes. It can read
policy each cycle; the dispatch loop can read policy before waiting and before
flushing.

Rationale: the existing watcher is already the central point for coalescing and
drift healing. Making it policy-aware is lower risk than adding a separate
scheduler.

### D7. Add a no-allocation resource status surface

Extend `exomem mode --json` or add `exomem status --resources --json` using the
same underlying helper. The status collection must be no-allocation:

- do not import torch solely to answer status;
- do not call APIs that create a CUDA context;
- do not load models or sidecars;
- report unknown/unavailable rather than probing destructively.

Fields should include:

- effective mode, source, and config path;
- policy fields from `mode.resolved()`;
- model residency booleans;
- CUDA memory accounting only when CUDA is already initialized;
- matrix/BM25/page/resolver/hot-cache residency estimates;
- watcher/deferred-work counters;
- auto-quiet state if enabled.

Rationale: the user needs a quick "why is Exomem affecting my machine?" surface
that is safe to call before gaming or from a script.

### D8. Keep auto-quiet optional and probe with non-invasive signals

Add `auto_quiet.py` only after manual quiet/resource-saver behavior is correct.
It is controlled by `EXOMEM_AUTO_QUIET=1` or a config-file field and is disabled
by default.

Preferred signal order:

1. Existing mode/config state: never override `EXOMEM_MODE`, because env is an
   operator hard pin.
2. GPU pressure from non-torch probes, for example `nvidia-smi` when present.
   Do not import torch just to poll pressure.
3. System memory pressure through platform-specific stdlib/optional probes.
4. Fullscreen/game process detection only as a stretch path and only when it is
   reliable enough to avoid flapping.

Auto-quiet stores the previous config-file mode and restores it after pressure
clears for a hysteresis window. It should not restore over a manual user change
made while pressure was active.

Rationale: auto-quiet is useful, but a wrong auto-switch is worse than a manual
toggle. The manual `exomem mode quiet` path must remain the reliable baseline.

## Risks / Trade-offs

- Cold first query after quiet may be slower -> Accept this as the quiet-mode
  trade-off; keep normal/performance for warm-cache latency.
- Deferred semantic indexing can make vector/CLIP recall stale for recent edits
  -> Surface pending work in status/timings, keep lexical freshness live, and
  provide explicit `exomem index`/`reconcile` healing.
- Cache eviction could race an in-flight query -> Use atomic reference swaps
  under existing locks; never mutate arrays/lists in place.
- Clearing too much metadata could cause more O(corpus) work and worsen CPU
  interruption -> Keep lightweight freshness/inbound metadata by default; evict
  heavy content/corpus/matrix caches first.
- Auto-quiet could flap between modes -> Use hysteresis, cooldowns, and never
  override an explicit `EXOMEM_MODE` env pin or a manual mode change.
- Resource estimates may be approximate -> Label them as estimates and keep the
  status surface useful for directionality, not byte-perfect accounting.
- Optional probes may be missing or platform-specific -> Treat absence as
  "unknown" and continue in manual mode without errors.

## Migration Plan

1. Land policy helpers in `mode.py` and tests for `quiet`, `normal`, and
   `performance`.
2. Add heavy-cache unload/status hooks without changing runtime behavior.
3. Extend `model_reaper.py` to include cache slots and apply them on quiet
   entry/idle.
4. Make `warmup.py` honor CPU-cache preload policy.
5. Make watcher/reconcile/index dispatch mode-aware, with deferred expensive
   work surfaced in status.
6. Add resource status JSON and doctor/setup messaging that does not allocate
   CUDA or load models.
7. Add optional auto-quiet after manual quiet behavior is tested.

Rollback strategy: because this is mode-gated, rollback can first set mode to
`normal` or disable the new policy fields through env/config flags. Code rollback
is straightforward because the core retrieval algorithms and sidecar schemas do
not change.

## Open Questions

- What should the quiet idle threshold default be for CPU caches? The existing
  model threshold is 15 minutes; quiet may need a shorter default such as 1-5
  minutes.
- Should deferred expensive work be persisted as a small queue, or is it enough
  to rely on drift detection plus explicit `index`/`reconcile`? Persistence is
  more explicit; drift reconstruction is less state.
- Should `normal` retain embedding matrices after a query? Current behavior
  favors latency. The design keeps that for now and limits aggressive RAM
  eviction to quiet unless measurements show normal is still too heavy.
- Which memory-pressure probe is worth supporting first on Windows without a new
  dependency?
