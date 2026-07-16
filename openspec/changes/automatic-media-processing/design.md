## Context

The extraction registry already classifies `.m4a` as audio and sends audio/video through the same Whisper backend. Agent uploads also already create a canonical pending sidecar and enqueue the durable media worker. The divergence happens earlier for direct filesystem drops: the watcher rejects every non-Markdown path, restart recovery scans only canonical `extracted_by: pending` sidecars, and `ensure_media_sidecar` treats any existing `<binary>.md` as valid even when it is only prose. The affected recordings therefore have neither a durable job nor a machine-actionable state.

The implementation must preserve Evidence binaries byte-for-byte, avoid expensive work on request threads, retain the optional/degradable media stack, and avoid coupling binary events to the text-index freshness machinery that previously caused full-corpus rebuild thrash.

## Goals / Non-Goals

**Goals:**

- Give upload, manual copy, Obsidian, and file-sync ingress one idempotent media-processing path.
- Produce timestamped audio/video transcripts and refresh text search after success.
- Repair noncanonical sidecars without losing their existing prose or valid transcript.
- Keep durable, inspectable pending/running/blocked/failed state with explicit retry.
- Preserve original bytes, relative path, filename, SHA-256, size, and filesystem timestamps.
- Expose the same process/status/retry action through MCP, REST/OpenAPI, and CLI.

**Non-Goals:**

- Running a reasoning LLM or inferring speaker identity from names in filenames or notes.
- Automatically retrying permanently failed/corrupt media in an unbounded loop.
- Moving or renaming existing Evidence binaries.
- Making diarization a mandatory dependency.
- Replacing the existing text-index watcher or media worker.

## Decisions

### 1. Add one orchestration leaf around existing preservation and worker primitives

A lightweight `media_processing` module will own classification, Evidence-path validation, sidecar inspection/repair, durable enqueue, reconciliation, status, and retry. Upload handling, watcher events, startup reconciliation, periodic reconciliation, and the `process_media` command will call this leaf rather than reimplementing policy.

This keeps the model-heavy worker disposable and the orchestration import-safe. A watcher-only extension or a Yolo-specific recovery script was rejected because either would leave other ingress paths divergent.

### 2. Observe binary events separately from Markdown freshness

`FileWatcher` will retain its current Markdown upsert/delete sets and add a separate debounced set for supported media under the governed Knowledge Base. A media event dispatches only targeted reconciliation for those paths. The existing periodic watcher reconciliation will also run a bounded media discovery pass, and startup will run the same pass after the worker starts.

Binary paths never enter BM25/vector text-index dispatch directly. Only the canonical sidecar is embedded after creation or extraction, avoiding the historical full-corpus rebuild failure mode.

### 3. Treat the binary as immutable evidence and the sidecar as governed derived state

New sidecars use the existing canonical `<binary-filename>.md` convention and `type: source` Evidence frontmatter. Reconciliation records the binary pointer, original filename, SHA-256, size, and filesystem timestamps. It never modifies binary bytes.

If the canonical sidecar is missing, reconciliation creates it pending. If the path contains noncanonical prose, reconciliation renders canonical frontmatter and carries the original body forward verbatim under a preserved-notes section. If a sidecar already has a completed extraction marker and substantial extracted text, it is considered valid and is not re-enqueued or overwritten. Reconciliation is byte-stable after convergence.

### 4. Keep the durable ledger authoritative for work state

The existing SQLite ledger remains the queue. Its unique media key deduplicates repeated watcher/reconcile calls. Completed jobs are removed only after the canonical sidecar commits successfully. Dependency absence becomes `blocked`; extraction/container failure becomes retained `failed`. Status includes paths, attempt count, reason, retryability, and next action. Explicit retry can target one path or all retryable jobs and never overwrites a valid transcript.

Sidecar frontmatter mirrors the actionable state so deleting the rebuildable ledger does not create a dead end. Startup reconciliation reconstructs pending work from binaries and sidecars.

### 5. Force timestamps only for canonical automatic jobs

The extraction API gains an optional timestamp request. `MediaWorker` supplies it for canonical automatic audio/video jobs, including uploads and reconciled drops. The existing environment-gated low-level extraction default remains unchanged, so direct callers and legacy backfill modes retain compatibility. Timed rendering soft-fails only when the renderer itself fails; that failure is persisted as actionable processing failure rather than silently committing an untimed transcript.

Diarization remains configuration-dependent and soft-imported. Anonymous clusters use stable `Speaker A`, `Speaker B`, and so on. Profile-matched names may be rendered by the existing voice-profile resolver, but sidecar metadata distinguishes profile matching from human verification.

### 6. Add one generated product command

`process_media(path=None, operation="process")` supports:

- `process`: validate and enqueue one supported governed artifact, or reconcile all when no path is supplied;
- `status`: inspect aggregate and per-path state without loading model dependencies;
- `retry`: requeue blocked/failed work, optionally restricted to one path.

One command-registry entry generates MCP, REST, OpenAPI, and CLI surfaces. The command returns stable result/error fields and does not wait for ASR completion.

## Risks / Trade-offs

- **Large direct copies can emit events before completion** → debounce and dedup events; verify binary identity before committing; periodic reconciliation re-enqueues a still-pending artifact after the copy settles.
- **Periodic discovery adds filesystem work** → restrict it to supported extensions under the governed Knowledge Base and reuse the existing mode-dependent reconciliation interval; never rebuild text indexes from the scan.
- **Repairing prose sidecars changes derived Evidence metadata** → preserve the original body verbatim, retain any existing identifier where valid, write atomically, and never touch the binary.
- **Failed jobs can accumulate** → keep bounded status output and explicit retry; failures remain visible instead of being deleted silently.
- **Optional ASR/diarization may be unavailable** → record blocked state and remediation while keeping the core service healthy.
- **Profile matching is not human verification** → persist an explicit `speaker_verification` value and never infer names from filenames or conversation context.

## Migration Plan

1. Deploy code and restart Exomem; startup reconciliation discovers existing supported media and repairs/enqueues missing canonical sidecars.
2. Invoke `process_media(operation="process", path=...)` for each affected recording to make recovery immediate rather than waiting for the periodic pass.
3. Monitor `process_media(operation="status")` until both jobs complete; retry only from recorded blocked/failed state after addressing its next action.
4. Verify canonical timestamped sidecars and search hits, then keep the automatic watcher/reconciliation path enabled for future drops.

Rollback is code-only: original binaries remain unchanged, valid completed sidecars remain readable, and the derived SQLite ledger can be deleted and rebuilt.

## Open Questions

None. The user explicitly selected automatic processing for both agent uploads and manual drops.
