# live-index-freshness Specification

## Purpose
TBD - created by archiving change improve-find-latency-token-cost. Update Purpose after archive.
## Requirements
### Requirement: Suppress Exomem Self-Write Watcher Echo

The system SHALL suppress live file-watcher events that correspond to filesystem mutations already
performed by Exomem writer paths and already handled by the writer embedding hooks. A suppressed
self-write event MUST NOT trigger a duplicate `embeddings.upsert_after_write` or
`embeddings.delete_after_remove` call from the watcher. Suppression MUST be bounded and keyed to the
same vault-relative path that the watcher would otherwise enqueue.

#### Scenario: Batch write does not reindex twice

- **WHEN** an Exomem writer updates markdown through the normal atomic batch-write path
- **AND** that writer has already refreshed embeddings for the written markdown
- **THEN** the watcher does not enqueue a second upsert for the same self-authored filesystem event

#### Scenario: Self-authored delete does not delete twice

- **WHEN** an Exomem writer removes, trashes, or moves a markdown file and already updates the
  embedding sidecar for the removed path
- **THEN** the watcher does not enqueue a duplicate delete for the same self-authored filesystem
  event

### Requirement: External File Edits Still Reindex Live

The system SHALL continue to reindex out-of-band markdown edits observed by the live watcher,
including edits from Obsidian, mobile sync, manual filesystem writes, and git updates. Self-write
suppression MUST NOT disable the existing debounce, batching, upsert, or delete behavior for events
that were not registered as Exomem-authored mutations.

#### Scenario: Manual markdown edit still upserts

- **WHEN** a markdown file under `Knowledge Base/` is modified outside an Exomem writer path
- **THEN** the watcher debounces the event and calls `embeddings.upsert_after_write` for that file

#### Scenario: Manual markdown delete still deletes sidecar rows

- **WHEN** a markdown file under `Knowledge Base/` is deleted outside an Exomem writer path
- **THEN** the watcher debounces the event and calls `embeddings.delete_after_remove` with the
  vault-relative path

### Requirement: Self-Write Suppression Cannot Hide Later External Edits

The system SHALL make self-write suppression temporary and freshness-aware. For create/modify
events, suppression MUST match the self-authored file signature, such as mtime plus size, before the
watcher drops an event. A later external edit to the same path MUST be treated as a normal watcher
event once the signature changes or the suppression entry expires.

#### Scenario: Later edit to same path reindexes

- **WHEN** Exomem writes a markdown file and registers the self-write for watcher suppression
- **AND** a later external edit changes that same file
- **THEN** the watcher treats the later event as external
- **AND** the watcher enqueues an embedding upsert for the edited file

#### Scenario: Suppression entries expire

- **WHEN** a self-write suppression entry is older than its bounded lifetime
- **THEN** the watcher no longer uses that entry to drop events

