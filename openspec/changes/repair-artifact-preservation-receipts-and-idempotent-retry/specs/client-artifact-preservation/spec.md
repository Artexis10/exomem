## ADDED Requirements

### Requirement: Destination segments are validated before retrieval
The preservation commands SHALL treat `scope` and `category` as single path segments. A value containing a path separator, a reserved filesystem character, a control character, leading or trailing whitespace, or that differs from its sanitised form SHALL be refused with `INVALID_PRESERVE` whose `details` name the offending field, the reason, and the accepted form, before any file handle is fetched or staged. The commands SHALL NOT silently normalise a destination segment.

#### Scenario: Scope carries a path separator
- **WHEN** `preserve_artifacts` is called with `scope="food/caviarhouse-group-order"` and a valid category
- **THEN** the call refuses with `INVALID_PRESERVE`, `details.field="scope"`, and a reason that says a segment cannot contain `/`
- **AND** no handle is fetched, no byte is staged, and no Evidence path is created

#### Scenario: Category carries a reserved character
- **WHEN** `preserve_evidence` is called with a category containing `:` or a control character
- **THEN** the call refuses with `INVALID_PRESERVE` naming `category`
- **AND** nothing is written

#### Scenario: Valid segments are unchanged
- **WHEN** `scope` and `category` are each one clean segment
- **THEN** the destination is `Evidence/<scope>/<category>/` byte-for-byte as supplied

## MODIFIED Requirements

### Requirement: Governed append-only persistence and truthful outcomes
Each successfully staged file SHALL be committed through the existing `preserve_stream` Evidence path under the active vault mutation boundary, preserving append-only collision refusal, sanitization, sidecars, hashing, index updates, and media reconciliation behavior. The command SHALL return exactly one ordered outcome per input `file_id`, and the batch SHALL carry the mutation's `request_id` and, when available, `receipt_id`. Each outcome SHALL carry a terminal `state` in `stored`, `already_stored`, or `failed`. A stored outcome SHALL include stored path, stable ref, size, SHA-256, hash algorithm, media ID, and content type; an `already_stored` outcome SHALL name the existing path and ref of the artifact under the same destination whose SHA-256 equals the staged bytes, and SHALL write nothing; a failed outcome SHALL include a stable code and sanitized reason. The batch summary SHALL count all three states. One item failure SHALL NOT erase or misreport another item's successful commit.

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
