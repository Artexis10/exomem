## Why

Supported media copied directly into the governed Knowledge Base can remain permanently unprocessed because the live watcher intentionally handles Markdown only, while only the `/upload` path creates an actionable sidecar and durable media job. The gap is especially misleading for audio such as `.m4a`: classification and ASR support exist, but prose status notes and silent queue absence make the artifact look pending without any processing or retry path.

## What Changes

- Automatically discover supported media created or modified under the governed Knowledge Base, regardless of whether it arrived through agent/API upload, manual copy, Obsidian, or file sync.
- Reconcile missed media events at startup and periodically through the existing durable media-job ledger without triggering full text-index rebuilds.
- Create or repair canonical governed media sidecars while preserving original binary bytes, provenance, existing notes, hashes, timestamps, and valid transcripts.
- Route automatic audio/video work through timestamped ASR; run diarization when configured and available, keeping neutral labels unless profile matching supports attribution and recording explicit speaker-verification state.
- Persist actionable processing state including attempts, failure reason, retryability, and next action; retain failed jobs until an explicit retry succeeds.
- Add a canonical `process_media` product command for process, status, and retry operations, generated consistently across MCP, REST, OpenAPI, and CLI.
- Keep reconciliation idempotent, preserve valid existing transcripts, and retain the existing `.mp4` processing path and optional-dependency soft-fail behavior.

## Capabilities

### New Capabilities

- `automatic-media-processing`: Governed discovery, sidecar repair, durable dispatch, observability, retry, and idempotent reconciliation for supported media.

### Modified Capabilities

- `live-index-freshness`: The watcher continues Markdown indexing as before but also coalesces supported governed-media events into media reconciliation without treating binaries as text-index inputs.
- `command-surface`: A single `process_media` registry entry exposes the same process/status/retry contract through MCP, REST, OpenAPI, and CLI.
- `semantic-video-segments`: Automatic audio/video jobs explicitly request timestamped transcript rendering while ungated low-level extraction callers retain their existing byte-compatible default.

## Impact

Affected areas include media classification/extraction, Evidence sidecar rendering and repair, the durable media-job store and worker, file-watcher/startup reconciliation, upload dispatch, command registry/product catalog, CLI/REST/MCP schemas, embedding refresh, and focused media regression tests. No reasoning model is added: ASR, diarization, hashing, and indexing remain deterministic pure-substrate measurements. Heavy ASR/diarization dependencies remain optional and soft-fail into actionable blocked state rather than blocking the core service.
