## MODIFIED Requirements

### Requirement: Every Ingress Path Reaches Durable Processing
The system SHALL route supported media from agent/API upload, manual filesystem copy, Obsidian, file sync, startup discovery, and periodic reconciliation through one idempotent sidecar-and-job orchestration path. Automatic processing SHALL remain off only when media extraction is explicitly disabled, in which case pending state MUST remain actionable.

When deployment configuration disables only the engine that an artifact's processing stage needs, the artifact SHALL keep its canonical pending sidecar and SHALL receive no job or stage for that engine while the engine stays disabled. Reconciliation SHALL enqueue that stage once the engine is enabled.

#### Scenario: Upload enqueues canonical work
- **WHEN** an agent uploads supported audio without supplying extracted text
- **THEN** the preserved binary receives a canonical pending sidecar and one durable processing job

#### Scenario: Manual drop enqueues canonical work
- **WHEN** supported audio is created under the governed Knowledge Base outside an Exomem writer
- **THEN** the debounced watcher creates or repairs its canonical sidecar and enqueues one durable processing job

#### Scenario: Missed event is healed
- **WHEN** a supported media event is missed while the service is stopped or disconnected
- **THEN** startup or periodic reconciliation discovers and enqueues the artifact

#### Scenario: A disabled engine creates no job bound to fail
- **WHEN** supported audio is uploaded while the deployment's speech engine is disabled
- **THEN** the binary receives its canonical pending sidecar
- **AND** no speech job or stage is created, so no blocked or failed job appears for it

#### Scenario: Enabling an engine enqueues the media that waited for it
- **WHEN** the deployment enables an engine that was disabled while media needing it arrived
- **THEN** reconciliation enqueues one stage for that engine for each such artifact
- **AND** an artifact with a completed valid extraction is not enqueued again

### Requirement: Processing Failure Is Durable And Actionable
The durable ledger and sidecar SHALL retain the artifact path, processing state, attempt count, failure reason, retryability, and next action for blocked or failed work. A dependency-unavailable condition SHALL be blocked and retryable. A corrupt or unreadable artifact SHALL be failed with its concrete exception reason and SHALL NOT be retried automatically in a hot loop.

On an Exomem Cloud cell, the next action SHALL NOT be an instruction to install software. The tenant cannot act on one, so a shipped engine that cannot load SHALL be reported as unavailable on this deployment, for the operator to repair.

#### Scenario: ASR dependency is unavailable
- **WHEN** the ASR backend cannot be loaded on a personal install
- **THEN** the job remains blocked with retryability and installation/remediation guidance

#### Scenario: A Hosted cell keeps its current guidance
- **WHEN** the ASR backend cannot be loaded in a Hosted cell
- **THEN** the job remains blocked and retryable, with the remediation guidance it receives today
- **AND** whether the cell runs media at all still follows its Hosted media entitlement

#### Scenario: A Cloud engine cannot load
- **WHEN** a Cloud cell's enabled engine cannot be loaded
- **THEN** the job remains blocked and retryable
- **AND** its next action reports the engine as unavailable on this deployment, with no installation instruction

#### Scenario: Corrupt audio fails visibly
- **WHEN** ASR rejects a corrupt audio container
- **THEN** the job remains failed with the exception type and bounded message
- **AND** status reports the artifact path and explicit retry or replacement next action

#### Scenario: Explicit retry requeues work
- **WHEN** a caller retries a blocked or failed artifact after remediation
- **THEN** the existing job returns to pending without creating a duplicate
- **AND** a valid completed transcript is not overwritten

## ADDED Requirements

### Requirement: Open and e-book document formats and HEIC images extract on every install
Every install SHALL classify EPUB, OpenDocument text, spreadsheet and presentation (ODT, ODS and ODP) and RTF artifacts as documents through the canonical media registry, and SHALL extract their text through the same sidecar-and-job path as the existing document types. Every install SHALL decode HEIC images and process them as images. On Cloud, the image-build gate that extracts a real sample of each format SHALL be the proof that Cloud supports the format, as the `cloud-multimodal-processing` capability states. On a personal install, a format whose extraction dependency is missing SHALL keep the existing blocked state with install guidance.

#### Scenario: An EPUB is searchable
- **WHEN** an EPUB book is added to the governed Knowledge Base
- **THEN** it receives a canonical document sidecar and one durable processing job
- **AND** its chapter text is extracted and found by search

#### Scenario: An OpenDocument spreadsheet is searchable
- **WHEN** an ODS spreadsheet is uploaded
- **THEN** its cell text is extracted into its sidecar and found by search

#### Scenario: An iPhone photo is read
- **WHEN** a HEIC photo containing printed text is processed
- **THEN** it is decoded as an image and its OCR text is extracted

#### Scenario: A personal install without the format's library keeps today's guidance
- **WHEN** an EPUB is processed on a personal install that lacks the EPUB extractor's dependency
- **THEN** the job is blocked and retryable, with install guidance, as for any missing media dependency today
