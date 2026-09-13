## MODIFIED Requirements

### Requirement: Governed append-only persistence and truthful outcomes
Each successfully staged file SHALL be committed through the existing `preserve_stream` Evidence path under the active vault mutation boundary, preserving append-only collision refusal, sanitization, sidecars, hashing, index updates, and media reconciliation behavior. The command SHALL return exactly one ordered outcome per input `file_id`, and the batch SHALL carry the mutation's `request_id` and, when available, `receipt_id`. Each outcome SHALL carry a terminal `state` in `stored`, `already_stored`, or `failed`. A stored outcome SHALL include stored path, stable ref, size, SHA-256, hash algorithm, media ID, and content type; an `already_stored` outcome SHALL name the existing path and ref of the artifact under the same destination whose SHA-256 equals the staged bytes, and SHALL write nothing; a failed outcome SHALL include a stable code and sanitized reason. The batch summary SHALL count all three states. One item failure SHALL NOT erase or misreport another item's successful commit. The index and graph fan-out that media reconciliation triggers SHALL NOT run while a preservation mutation guard is held, and SHALL NOT run before the batch terminal is persisted; it SHALL take the deferred derived path that note writes use, or run after terminal persistence when no derived path is available, and the batch's `derived_sync` SHALL report it.

#### Scenario: Mixed batch outcome

- **WHEN** two files are valid and a third has an expired temporary URL
- **THEN** the valid files are preserved and reported as stored
- **AND** the expired file is reported as failed with no claimed stored path
- **AND** the batch summary reports both stored and failed counts

#### Scenario: Missing filename uses deterministic fallback

- **WHEN** a valid file object omits `file_name`
- **THEN** Exomem derives a sanitized deterministic filename from the staged SHA-256 and available MIME extension before preservation

#### Scenario: Identical mutation is retried

- **WHEN** an MCP client retries the byte-identical mutating call within the implicit replay window
- **THEN** Exomem returns the cached terminal batch result without fetching or writing the files again

#### Scenario: Duplicate bytes arrive under a new identity

- **WHEN** a client retries a preservation with a different request identity and one staged file's SHA-256 already exists under the same `Evidence/<scope>/<category>/` destination
- **THEN** that file's outcome is `already_stored` with the existing path and ref, no new artifact or sidecar is written, and the audit chain does not advance for it
- **AND** the other files in the batch are stored normally

#### Scenario: Acknowledgement is lost after the batch commits

- **WHEN** every file in a batch commits and deterministic fault injection drops the response before the client receives it
- **THEN** a same-identity retry returns the persisted batch terminal with the same per-file paths, hashes, `request_id` and `receipt_id`
- **AND** each artifact exists exactly once

#### Scenario: Media fan-out does not run inside the critical section

- **WHEN** a batch preserves an image whose sidecar triggers index and graph work
- **THEN** no index upsert or graph refresh runs while the preservation mutation guard is held or before the batch terminal is persisted
- **AND** the batch response returns with `derived_sync` reporting the deferred work

#### Scenario: Terminal persistence fails after media sidecar commit

- **WHEN** the media sidecar commits without a fast-acknowledgement session and batch terminal persistence fails
- **THEN** no request-owned media fan-out runs before the terminal is durable
- **AND** a durable full-refresh record retains the indexing work for background repair
- **AND** a same-identity retry reports `derived_sync=pending` without restaging or duplicating the artifact

#### Scenario: New refresh demand arrives after another drain

- **WHEN** a background drain retires the media refresh record and another write queues a new refresh for the same path
- **THEN** completion of the earlier request's foreground media fan-out does not retire that newer refresh demand
