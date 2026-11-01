## ADDED Requirements

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
