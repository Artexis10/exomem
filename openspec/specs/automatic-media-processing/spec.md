# automatic-media-processing Specification

## Purpose
Route supported audio and video from every ingress path (upload, filesystem copy, Obsidian, file sync, startup discovery, periodic reconciliation) to durable, idempotent processing that preserves the original artifact. It produces timestamped transcripts, applies conservative speaker attribution, records actionable failure state, and stays pure-substrate and soft-fail.

## Requirements

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

### Requirement: Speech Language Follows A Fixed Precedence

The speech stage SHALL choose a transcript's language in this order: a language requested on an explicit reprocess call, the language pinned on the sidecar by an earlier request, the deployment's configured language (`EXOMEM_ASR_LANGUAGE`; unset means none), and detection on the recording's speech. A requested, pinned or configured language SHALL replace detection. The stage SHALL accept a code only if the loaded speech model publishes it. A code that the model does not publish SHALL fail the job with a durable, actionable reason that lists the supported codes, and the stage MUST NOT fall back to detection.

#### Scenario: A request beats a pin and the configured language

- **WHEN** a sidecar pins one language, the deployment configures another, and a caller requests a third on a reprocess call
- **THEN** the speech stage runs with the requested language
- **AND** the sidecar records the requested language as `asr_language_source: requested`

#### Scenario: A pin beats the configured language

- **WHEN** a sidecar pins a language, the deployment configures another, and the caller requests none
- **THEN** the speech stage runs with the pinned language

#### Scenario: Nothing decides, so speech does

- **WHEN** no request, pin or configured language exists
- **THEN** the engine detects the language on the recording's speech
- **AND** the sidecar records the detected language and its probability

#### Scenario: An unsupported code fails and does not fall back

- **WHEN** a caller requests a language code that the speech model does not publish
- **THEN** the job fails with a reason that lists the supported codes and names the next action
- **AND** the speech stage does not run with a detected language instead

### Requirement: Speech Decoding Policy Is Data

The speech stage SHALL read its decoding options and a policy revision from the model registry data, not from code. The options SHALL filter non-speech before language detection, SHALL NOT condition each window on the previous text, and SHALL apply no gain or normalization. The sidecar SHALL record the policy revision that produced its transcript.

#### Scenario: Language is detected on speech

- **WHEN** a recording opens with a quiet or non-speech stretch and the language is not set
- **THEN** the engine detects the language on speech, not on that opening

#### Scenario: Loud samples are not clipped

- **WHEN** the speech stage decodes a recording whose samples exceed full scale
- **THEN** the engine receives the decoded values unclipped

### Requirement: The Sidecar Records What The Speech Engine Decided

A completed audio or video sidecar SHALL record, as frontmatter with correct YAML types, `asr_language`, `asr_language_probability`, `asr_language_source` (`requested`, `configured` or `detected`), `asr_language_requested` (only when a request supplied the language), `asr_speech_seconds`, `asr_duration_seconds` and `asr_policy`. A language that was requested or configured SHALL have a null probability. A value that the engine did not report SHALL be absent or null and SHALL NOT be recorded as zero. The `extracted_by` marker format SHALL NOT change.

#### Scenario: A detected language carries its probability

- **WHEN** the engine detects the language of a completed transcript
- **THEN** the sidecar records `asr_language`, a numeric `asr_language_probability` and `asr_language_source: detected`

#### Scenario: A requested language carries no probability

- **WHEN** a transcript runs with a requested language
- **THEN** the sidecar records `asr_language_source: requested`, `asr_language_requested` and a null `asr_language_probability`

### Requirement: Explicit Reprocessing Replaces A Completed Extraction And Keeps The Prior One

The system SHALL offer an explicit `reprocess` media operation, with an optional language code, that requeues a completed item of any extractable media type and replaces its extraction. A language code SHALL be accepted for speech media (audio and video) only and SHALL be refused for other media. The request SHALL be refused while the media runtime is marked unavailable, as an explicit retry queues nothing then. Only this request MAY bypass the completed-transcript check. Before the new extraction becomes current, the prior extraction SHALL be kept as an immutable page next to the sidecar, in the same atomic write as the new sidecar. That page SHALL be marked superseded through the existing supersession convention, SHALL take the `type`, `source_type` and `tags` of the sidecar it was taken from, SHALL be linked from the sidecar in `prior_extractions`, and SHALL NOT itself be a media item or an item of the compile backlog. The page SHALL NOT take the number of a page that was listed before, even if that page was deleted. A request that names a language SHALL pin that language on the sidecar for later runs. A failed or stale reprocess MUST leave the prior text intact and SHALL fail the job with an actionable reason. The sidecar MUST NOT be left without either the old or the new text.

#### Scenario: Reprocess replaces the text and keeps the old one

- **WHEN** a caller reprocesses a completed audio item with a language
- **THEN** the sidecar holds the new text, the `asr_*` fields and a `prior_extractions` link
- **AND** the linked page holds the old extraction fields and text, marked superseded by the sidecar

#### Scenario: A prior page owes no compilation

- **WHEN** a completed item was reprocessed once
- **THEN** the unprocessed-source audit lists no prior extraction page

#### Scenario: A blocked reprocess does not rewrite the completed sidecar

- **WHEN** a reprocess is pending over a completed sidecar and the media runtime is then marked unavailable
- **THEN** the sidecar keeps its completed transcript and state
- **AND** only the job is marked blocked
- **AND** a new reprocess request is refused while the runtime stays unavailable

#### Scenario: Automatic retry leaves a completed transcript alone

- **WHEN** reconciliation or an explicit retry runs over a completed sidecar after a reprocess request was served
- **THEN** no job is queued for it and the sidecar is not rewritten
- **AND** a failed or blocked reprocess row does not hold a place in the automatic retry pass

#### Scenario: A stale reprocess keeps the prior text

- **WHEN** the extracted text of the sidecar changes while its reprocess runs
- **THEN** the new extraction is not committed
- **AND** the job fails with a reason that says the transcript changed
- **AND** the sidecar keeps its text

#### Scenario: A failed reprocess does not rewrite the sidecar

- **WHEN** a reprocess fails, for example on an unsupported language
- **THEN** the sidecar bytes are unchanged
- **AND** the job reports the failure and the next action

### Requirement: GPU Speech Decoding Runs Batched With Timestamped Lines

When the local speech stage runs on CUDA, it SHALL decode through the engine's batched pipeline with the batch size and options that `media_models.json` declares, and it SHALL keep segment timestamps so that each transcript line covers a citable span. When the stage runs on CPU, it SHALL decode sequentially. When the batched pipeline runs out of CUDA memory before it yields a segment, the stage SHALL decode that file sequentially. The sidecar SHALL record the policy revision that produced it and the decoding path that the backend ran, as `asr_decoding: batched` or `asr_decoding: sequential`.

#### Scenario: A GPU run decodes batched

- **WHEN** the speech stage processes a recording on CUDA
- **THEN** it decodes with the batched pipeline and the declared batch size
- **AND** the transcript lines carry segment timestamps, not one line per decoding window
- **AND** the sidecar records `asr_decoding: batched`

#### Scenario: A GPU too small for the batch size

- **WHEN** the batched pipeline raises a CUDA out-of-memory error before it yields its first segment
- **THEN** the stage decodes the file sequentially with the declared sequential options
- **AND** the sidecar records `asr_decoding: sequential`

#### Scenario: A CPU run stays sequential

- **WHEN** the speech stage processes a recording on CPU
- **THEN** it decodes sequentially with the declared sequential options
- **AND** the sidecar records `asr_decoding: sequential`

### Requirement: A Caller Declares One Language Or Several

A processing or reprocess request SHALL accept either one language code or a multilingual mode, and SHALL refuse a request that carries both. A declared mode SHALL follow the same precedence as a requested language and SHALL pin on the sidecar. In multilingual mode the engine SHALL detect the language per decoding window. The sidecar SHALL record `asr_language_mode` as `single` or `multilingual`.

#### Scenario: Several languages declared at first processing

- **WHEN** a caller processes a new recording with the multilingual mode
- **THEN** the engine detects the language per decoding window
- **AND** the sidecar records `asr_language_mode: multilingual` and `asr_language_source: requested`

#### Scenario: Both a language and the multilingual mode

- **WHEN** a request carries a language code and the multilingual mode together
- **THEN** the request is refused with a reason that names the conflict
- **AND** no job is queued

#### Scenario: An engine that reads one language per file

- **WHEN** a caller declares the multilingual mode and the configured speech engine cannot detect the language per decoding window
- **THEN** the job fails with a next action that names the two ways out: give a language code or use the faster-whisper engine
- **AND** the existing sidecar is left unchanged

### Requirement: Plain-Text Artifacts Are Read In The Encoding Their Bytes Show

The system SHALL decode the bytes of a plain-text, RTF or calendar artifact with one rule. A byte-order mark (UTF-8, UTF-16 or UTF-32) SHALL name the codec, and a cut-off last character SHALL become one replacement character. Without a mark, bytes whose valid multi-byte UTF-8 characters outnumber their invalid bytes SHALL be read as UTF-8 with replacement characters; valid UTF-8 and pure ASCII are this case. Otherwise, for at least 32 bytes, a detected encoding SHALL be used only when the detector's confidence on the first 64 KiB is at least 0.9. The detector SHALL NOT choose UTF-16 or UTF-32 without a byte-order mark, and Windows-1252 SHALL win only among guesses with the best confidence. Bytes that no rule explains SHALL be read as UTF-8 with replacement characters. The original bytes of the artifact SHALL NOT change. The sidecar's `extracted_by` SHALL name a non-UTF-8 encoding as `text:<codec>`.

#### Scenario: A UTF-16 file with a byte-order mark is searchable

- **WHEN** a `.txt` artifact holds UTF-16 text that starts with a byte-order mark
- **THEN** the extracted text holds the file's own characters, not characters separated by NUL
- **AND** `extracted_by` is `text:utf-16`

#### Scenario: A Latin-1 file keeps its accented letters

- **WHEN** a `.txt` artifact holds Latin-1 text that is not valid UTF-8
- **THEN** the extracted text holds the accented letters as written
- **AND** the artifact's bytes are unchanged

#### Scenario: Text that no codec explains reads as before

- **WHEN** a `.txt` artifact holds bytes that are not valid UTF-8 and no detected encoding reaches the confidence bound
- **THEN** the extracted text is the UTF-8 reading with replacement characters
- **AND** `extracted_by` is `text`

#### Scenario: A short file keeps its ASCII words

- **WHEN** a `.txt` artifact holds fewer than 32 bytes that are not valid UTF-8
- **THEN** the extracted text is the UTF-8 reading with replacement characters, so every ASCII word is kept

#### Scenario: UTF-8 with a stray byte keeps its valid characters

- **WHEN** a `.txt` artifact holds mostly valid UTF-8 text with one invalid byte, such as a cut-off last character
- **THEN** every valid character is kept and only the invalid byte becomes a replacement character
