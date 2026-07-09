## Context

The server currently creates one in-process `MediaWorker` thread per Exomem process. The
thread owns an in-memory queue and calls OCR, ASR, CLIP, frame persistence, and re-embedding
directly. Pending sidecars reconstruct extraction work after restart, but queue state is not
explicit, post-processing jobs are transient, and any model or accelerator context loaded by a
job remains in the long-lived MCP process.

That shape is particularly expensive under stdio process fan-out and on Apple Silicon, where
each process can retain a separate MLX model in unified memory. It is also impossible to return
CUDA to a near-zero idle footprint after first use from inside the same process because the
primary context survives model unload. The native HTTP service reduces process fan-out, but the
runtime must remain safe even when users have stale or duplicate clients.

The pipeline is pure substrate: OCR, ASR, embeddings, CLIP, and frozen voice/caption models
transduce user-owned evidence into deterministic searchable representations. No reasoning LLM
or content judgment is introduced.

## Goals / Non-Goals

**Goals:**

- Make the product install multimodal by default without loading heavy models at service start.
- Persist every queued media operation and recover safely after parent or child crashes.
- Guarantee at most one heavy media worker per vault across HTTP and stdio server processes.
- Keep models warm across a short burst, then reclaim RAM and accelerator contexts by exiting
  the child process.
- Preserve current sidecar/index output and current soft-fail semantics.
- Make normal mode lazy at startup and reclaim idle model/cache residency.
- Bound live text embedding calls by chunk count and persist deferred semantic paths.
- Expose queue and worker state without importing model modules or creating accelerator state.
- Preserve existing profile names and environment overrides.

**Non-Goals:**

- No cloud inference, hosted model service, reasoning model, or automatic note interpretation.
- No parallel heavy media execution; serialization is deliberate resource governance.
- No replacement of Whisper/CLIP/embedding model families in this change.
- No requirement that optional image captioning or speaker diarization be enabled by default.
- No guarantee of zero cold-start latency after the worker has returned resources.
- No automatic installation of OS package-manager dependencies such as Tesseract.

## Decisions

### D1. Add a product-facing `standard` profile

`standard` becomes the default profile for native release service installers. It maps to
`exomem[embeddings,media]`, plus `media-mlx` on macOS arm64. This provides hybrid retrieval,
PDF/Office extraction, OCR bindings, ASR, and CLIP. Missing optional OS tools such as Tesseract
are warnings in `standard`, not a reason to deny the rest of the service; the existing `media`
profile remains the strict/full compatibility profile and continues to install vision and
diarization extras through the service installers.

`lean`, `hybrid`, and `media` remain accepted. Advanced generated captioning and speaker
diarization stay default-off because baseline OCR/ASR/CLIP already satisfies the multimodal
product contract without pulling every specialized model into the default environment.

Alternative: keep `hybrid` as the blessed default and describe media as optional. Rejected
because it makes the advertised product experience false on first install.

### D2. Use a rebuildable SQLite job ledger

Add `.media-jobs.sqlite` under the KB root beside other derived sidecars. A job records the
vault-relative binary and sidecar paths, media type, requested extraction/CLIP/re-embed stages,
state, attempts, timestamps, and last error. Enqueue is idempotent: duplicate pending work
merges stage flags rather than adding another heavy operation.

The ledger supports atomic claim, complete/delete, blocked, failed, recovery of interrupted
`running` rows, queue counts, and a bounded status sample. User-authored Markdown remains the
source of truth; deleting the job DB is safe because startup scans reconstruct extraction and
CLIP drift from pending sidecars and indexes.

Alternative: continue relying only on pending sidecars. Rejected because CLIP and ordered
post-frame re-embedding are not fully represented there and operators cannot inspect queue
state.

### D3. Split supervisor and child worker at a process boundary

The long-lived `MediaWorker` becomes a stdlib-only supervisor. It writes jobs, starts a child
with the current interpreter, and monitors it. The child claims jobs from SQLite, processes
them serially with the existing extraction/index functions, and exits after
`EXOMEM_MEDIA_IDLE_SECONDS` (default 300) with no pending work.

One child processes an entire burst, preserving model reuse. Process exit is the reclamation
primitive for Python heaps, CTranslate2, MLX caches, MPS state, and CUDA primary contexts. The
parent never prewarms ASR and does not import model stacks merely to start media support.

A per-vault OS file lock allows only one child to enter the processing loop. Competing children
from duplicate server processes exit before importing heavy modules. The child checks parent
liveness and the supervisor terminates it during orderly shutdown, limiting orphan overlap.

Alternative: a process per media file. Rejected because repeated model loads make imports
unacceptably slow. Alternative: `multiprocessing.Queue`. Rejected because queue state is not
durable and ownership becomes ambiguous across multiple server processes.

### D4. Keep processing idempotent and soft-fail

The current stage functions become a child-side processor with an enqueue callback for scene
frame OCR and parent re-embedding. Re-running after a crash is safe because sidecar updates and
index upserts are idempotent. `ExtractionUnavailable` marks work blocked and visible instead of
hot-looping; restart or explicit retry makes blocked work eligible again. Corrupt input retains
the existing failed sidecar marker and does not kill the worker.

The service starts and serves lexical retrieval even if the ledger is unavailable, the worker
cannot spawn, a model is missing, or a child crashes. These are logged and surfaced through
status/doctor.

### D5. Make startup residency opt-in and normal-mode reclamation automatic

Normal and quiet modes do not preload models or O(vault) CPU caches. Performance mode may
preload for explicitly chosen low-latency behavior. The idle reaper runs in all modes by
default; normal and quiet release model and large-cache slots after the configured idle window,
while performance may retain CPU caches but still releases idle model weights unless overridden.

Environment overrides remain authoritative for operators who deliberately want prewarm or
retention. This makes the product default safe without depending on heuristic auto-quiet.

### D6. Bound and persist semantic write work

`embeddings.upsert_after_write` groups live writes by a configurable maximum chunk count
(`EXOMEM_LIVE_EMBED_MAX_CHUNKS`, default 256) rather than flattening every chunk into one encode.
Oversized single files are sliced into bounded encode calls before their rows are committed.

The existing deferred semantic registry moves from process memory to a tiny SQLite table beside
the embedding sidecar. Deferred paths survive restart, remain deduplicated, appear in resource
status, and clear only after explicit or background processing dispatches them.

### D7. Extend no-allocation observability

Resource status reads the media and deferred-index SQLite metadata directly. It reports pending,
running, blocked, and failed media counts, worker PID/active state when known, and semantic
deferred counts. It must not import `torch`, `embeddings`, `extract`, MLX, or CTranslate2 solely
to answer status.

Doctor reports the selected profile, lazy/prewarm policy, job DB health, worker multiplicity,
and the remediation for blocked jobs. The child process is identified separately from duplicate
server processes so a healthy supervisor+worker pair does not produce a false fan-out warning.

### D8. Treat resource budgets as release acceptance criteria

On the maintained 2,000-note fixture after startup and after the media worker idle deadline:

- the persistent core targets no CUDA compute process and less than 200 MiB GPU delta;
- the worker process count is zero and the server process count is one in HTTP service mode;
- persistent-core RSS targets 512 MiB or less before user-triggered cache growth;
- idle CPU targets less than 1% averaged over 60 seconds;
- queued work remains durable and visible while no worker is resident.

Unit tests assert the deterministic lifecycle and no-allocation properties. Desk-side scripts
record RSS/GPU/process results on Windows, Linux, and Apple Silicon because hosted Ubuntu-only
CI cannot prove launchd/NSSM/MPS resource behavior.

## Risks / Trade-offs

- [First media job after idle is slower] -> Keep the child hot for a bounded burst, pre-download
  models through explicit `exomem warm`, and expose `loading` rather than hiding the delay.
- [SQLite claim or lock bugs strand work] -> Keep sidecars/index drift reconstructable, recover
  `running` rows at startup, and make the ledger safely deletable.
- [Two supervisors repeatedly race to spawn] -> Use the per-vault OS lock plus launch backoff;
  losing children exit before model imports.
- [Service termination can orphan a child briefly] -> Parent-liveness checks and a short idle
  deadline bound the lifetime; orderly shutdown explicitly terminates the child.
- [Standard profile increases install size] -> Keep a documented `lean` opt-out, avoid generated
  caption/diarization extras, and distinguish disk-installed capability from memory residency.
- [Missing Tesseract weakens image OCR] -> Warn with exact remediation while preserving CLIP,
  ASR, PDF, Office, and core service operation.
- [Lazy normal mode increases first query latency] -> Preserve performance mode and explicit
  warm commands for users who choose latency over host coexistence.

## Migration Plan

1. Add the job ledger and child processor behind `EXOMEM_MEDIA_WORKER_MODE=process|inline`, with
   `process` as product default and `inline` retained for deterministic tests/debugging.
2. Move current stage methods without changing output contracts; convert enqueue and startup
   scans to durable writes.
3. Change prewarm/reaper defaults and add status/doctor reporting.
4. Add `standard` across doctor, setup, installers, and docs; preserve old profile behavior.
5. Add bounded live embedding and durable deferred semantic work.
6. Run focused/full tests, OpenSpec validation, installer contract tests, and desk-side resource
   verification where hosts are available.

Rollback: set `EXOMEM_MEDIA_WORKER_MODE=inline` to restore in-process execution, select an
existing profile explicitly, or revert the runtime change. The new SQLite sidecar is derived and
can be removed without touching evidence or notes.

## Open Questions

- A future release may prefetch model files during installation, but this change keeps model
  network downloads explicit because their size and availability vary by profile and host.
- Byte-capped LRU caches are preferable to count-only caches, but the first implementation uses
  the existing idle reaper; per-cache byte ceilings can follow after real-host measurements.
