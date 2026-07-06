## ADDED Requirements

### Requirement: Timing Diagnostics Include Request Profile Metadata
The system SHALL include compact request/profile metadata in `find` timing
diagnostics when `include_timings=true`. This metadata MUST be limited to
diagnostic flags and compute policy, and MUST NOT include note content, excerpts,
expanded query text, vectors, or private vault paths.

#### Scenario: Timed find includes profile metadata
- **WHEN** `find` is called with `include_timings=true`
- **THEN** `timings` includes a profile block identifying request knobs such as
  mode, detail, pack, graph, and rerank request state
- **AND** `timings` includes the current compute policy
- **AND** the hit ranking is unchanged compared with the same timed request before
  metadata serialization

#### Scenario: Untimed find shape is unchanged
- **WHEN** `find` is called without `include_timings=true`
- **THEN** the default response shape remains the existing hit list or pack envelope
- **AND** no timing profile metadata is returned

### Requirement: Structured Upload Success Metadata
The system SHALL return structured upload success metadata from `/upload` in
addition to the existing stored path fields. The metadata SHALL include the stored
binary path, byte size, SHA-256 hash, hash algorithm, media identifier, and content
type when available.

#### Scenario: Upload response identifies stored artifact
- **WHEN** a file is uploaded successfully through `/upload`
- **THEN** the JSON response includes existing `path` and `sidecar_path` fields
- **AND** it includes `stored_path`, `size`, `hash`, `hash_algorithm`,
  `media_id`, and `content_type`

#### Scenario: Upload metadata does not change authorization or duplicate behavior
- **WHEN** upload auth fails, a file is oversized, or a duplicate path is uploaded
- **THEN** the existing error codes and status codes are preserved
