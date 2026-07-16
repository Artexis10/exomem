## 1. Canonical Media Orchestration

### Task 1

- [x] 1.1 Add failing classification and orchestration tests for `.m4a`, unsupported media, missing sidecars, prose-only sidecar repair, provenance/hash fields, valid transcript preservation, idempotent reconciliation, and path confinement.

### Task 2

- [x] 1.2 Implement the lightweight canonical media-processing leaf and governed sidecar create/repair logic until the orchestration tests pass.

### Task 3

- [x] 1.3 Add failing durable-ledger tests for per-path status, attempt/error details, retryability/next action, deduplicated targeted retry, and failed-job retention, then implement the ledger behavior.

## 2. Automatic Dispatch And Timestamped Processing

### Task 4

- [x] 2.1 Add failing watcher and runtime tests for direct audio discovery, debounce/deduplication, startup and periodic missed-event reconciliation, unsupported attachments, and no binary text-index dispatch.

### Task 5

- [x] 2.2 Wire watcher, startup/periodic reconciliation, and upload preservation into the shared orchestration leaf while keeping repeated reconciliation idempotent.

### Task 6

- [x] 2.3 Add failing extraction/worker tests for explicitly timestamped automatic ASR, `.mp4` regression behavior, configured diarization with neutral/profile labels, unavailable dependencies, corrupt media, durable failure state, and successful sidecar/index refresh.

### Task 7

- [x] 2.4 Implement worker timestamp requests, speaker-verification metadata, and actionable blocked/failed persistence without changing ungated low-level extraction output.

## 3. Product Surface

### Task 8

- [x] 3.1 Add failing command-registry, MCP schema, REST/OpenAPI, and CLI tests for `process_media` process/status/retry operations and shared error behavior.

### Task 9

- [x] 3.2 Register and document `process_media` once across generated product surfaces, update scaffold/product-contract text generically, and make all surface tests pass.

## 4. Verification And Existing-File Recovery

### Task 10

- [ ] 4.1 Run focused media, watcher, preservation, command-surface, API, and CLI tests plus strict OpenSpec validation and lint; fix any regressions.

### Task 11

- [ ] 4.2 Run the broader test suite with embeddings disabled and complete independent whole-branch review, addressing all critical or important findings.

### Task 12

- [ ] 4.3 Deploy/restart the updated service, invoke canonical processing for the two specified `.m4a` Evidence paths, monitor actionable status to completion, and verify timestamped sidecars, preserved hashes/provenance, search refresh, and explicit speaker-verification state.
