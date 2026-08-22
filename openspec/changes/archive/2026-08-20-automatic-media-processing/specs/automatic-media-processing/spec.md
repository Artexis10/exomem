## ADDED Requirements

### Requirement: Supported Governed Media Is Classified Consistently
The system SHALL classify `.m4a`, `.mp3`, `.wav`, `.flac`, `.ogg`, and the existing supported video formats through the canonical media registry regardless of whether the artifact arrived through upload or direct filesystem discovery. MIME metadata MAY be recorded but MUST NOT create a divergent dispatch path.

#### Scenario: M4A is supported audio
- **WHEN** a governed artifact has an `.m4a` extension with any letter casing
- **THEN** the system classifies it as audio and selects the ASR processing stage

#### Scenario: Unsupported extension is not queued
- **WHEN** discovery observes a binary whose extension is not in the canonical media registry
- **THEN** automatic reconciliation leaves it unqueued
- **AND** an explicit process request returns an actionable unsupported-media error

### Requirement: Every Ingress Path Reaches Durable Processing
The system SHALL route supported media from agent/API upload, manual filesystem copy, Obsidian, file sync, startup discovery, and periodic reconciliation through one idempotent sidecar-and-job orchestration path. Automatic processing SHALL remain off only when media extraction is explicitly disabled, in which case pending state MUST remain actionable.

#### Scenario: Upload enqueues canonical work
- **WHEN** an agent uploads supported audio without supplying extracted text
- **THEN** the preserved binary receives a canonical pending sidecar and one durable processing job

#### Scenario: Manual drop enqueues canonical work
- **WHEN** supported audio is created under the governed Knowledge Base outside an Exomem writer
- **THEN** the debounced watcher creates or repairs its canonical sidecar and enqueues one durable processing job

#### Scenario: Missed event is healed
- **WHEN** a supported media event is missed while the service is stopped or disconnected
- **THEN** startup or periodic reconciliation discovers and enqueues the artifact

### Requirement: Media Reconciliation Preserves Governance And Is Idempotent
Reconciliation SHALL preserve the original artifact bytes and path, record original filename, SHA-256, size, and filesystem timestamps, and create or repair a governed sidecar using the canonical naming and frontmatter conventions. Repeated reconciliation over unchanged state SHALL produce no additional job or sidecar rewrite. A completed valid transcript SHALL NOT be overwritten unless an explicit reprocessing mode requests it.

#### Scenario: Missing sidecar is created without changing evidence
- **WHEN** a supported Evidence binary has no sidecar
- **THEN** reconciliation creates the canonical pending sidecar and records provenance
- **AND** the binary hash and bytes remain unchanged

#### Scenario: Prose-only status note is repaired
- **WHEN** `<binary>.md` exists but lacks canonical media frontmatter and contains only prose status or notes
- **THEN** reconciliation converts it into the canonical sidecar while preserving the prior prose verbatim
- **AND** the artifact becomes actionable pending work

#### Scenario: Existing valid transcript is preserved
- **WHEN** a canonical sidecar has a completed extraction marker and non-empty extracted text for the unchanged binary
- **THEN** reconciliation does not rewrite the sidecar or enqueue ASR

#### Scenario: Reconciliation repeats as a no-op
- **WHEN** reconciliation runs twice over the same pending or completed artifact
- **THEN** the durable job key and sidecar remain singular and stable

### Requirement: Automatic Audio And Video Transcripts Are Timestamped
Canonical automatic audio/video processing SHALL render one line per ASR segment with a human-readable timestamp and SHALL record a timed extraction marker. Successful extraction SHALL atomically update the sidecar and refresh the text search index.

#### Scenario: Audio transcript contains timestamps
- **WHEN** an automatic audio job succeeds with ASR segments
- **THEN** its extracted text contains `[m:ss]` or `[h:mm:ss]` timestamps
- **AND** the extraction marker includes `+timed`

#### Scenario: MP4 retains the canonical media path
- **WHEN** an `.mp4` artifact is processed after this change
- **THEN** it still uses the existing video ASR and optional CLIP stages
- **AND** its successful transcript remains timestamped and indexed

### Requirement: Speaker Attribution Is Conservative And Explicit
When diarization is configured and available, automatic ASR SHALL diarize the recording and use stable neutral labels for clusters that do not satisfy the existing voice-profile matching rules. The sidecar SHALL record whether speaker attribution is unavailable, anonymous, profile-matched, or human-verified; profile matching alone MUST NOT be represented as human verification.

#### Scenario: Unknown speakers remain neutral
- **WHEN** diarization succeeds but no enrolled profile clears the matching rules
- **THEN** transcript lines use stable neutral labels such as `Speaker A`
- **AND** speaker verification is recorded as anonymous or pending

#### Scenario: Diarization dependency is absent
- **WHEN** diarization is enabled but its optional dependency is unavailable
- **THEN** ASR continues according to the existing soft-fail policy
- **AND** the sidecar explicitly records that speaker verification was unavailable

### Requirement: Processing Failure Is Durable And Actionable
The durable ledger and sidecar SHALL retain the artifact path, processing state, attempt count, failure reason, retryability, and next action for blocked or failed work. A dependency-unavailable condition SHALL be blocked and retryable. A corrupt or unreadable artifact SHALL be failed with its concrete exception reason and SHALL NOT be retried automatically in a hot loop.

#### Scenario: ASR dependency is unavailable
- **WHEN** the ASR backend cannot be loaded
- **THEN** the job remains blocked with retryability and installation/remediation guidance

#### Scenario: Corrupt audio fails visibly
- **WHEN** ASR rejects a corrupt audio container
- **THEN** the job remains failed with the exception type and bounded message
- **AND** status reports the artifact path and explicit retry or replacement next action

#### Scenario: Explicit retry requeues work
- **WHEN** a caller retries a blocked or failed artifact after remediation
- **THEN** the existing job returns to pending without creating a duplicate
- **AND** a valid completed transcript is not overwritten

### Requirement: Processing Remains Pure-Substrate And Soft-Fail
Automatic media processing SHALL use deterministic extraction, ASR, diarization, hashing, and indexing only; it MUST NOT invoke a reasoning model. Missing optional heavy dependencies MUST NOT prevent the core Exomem service from starting or serving non-media operations.

#### Scenario: Lean installation remains usable
- **WHEN** the service starts without ASR or diarization extras
- **THEN** non-media commands remain healthy
- **AND** discovered supported media is represented by actionable pending or blocked state
