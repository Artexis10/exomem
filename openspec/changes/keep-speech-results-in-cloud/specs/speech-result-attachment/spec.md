## ADDED Requirements

### Requirement: An agent can attach a speech result to a recording's Source

The write door SHALL offer `media.attach-speech`, which attaches a speech result to an existing audio or video Source. The call SHALL carry the original's `binary_sha256` and `binary_size`, timed segments, and the engine identity. It MAY carry speaker turns, a track name for each segment and turn, and speaker names. The call SHALL use the caller's existing connection and write access, with no new credential, scope or service.

#### Scenario: The owner's agent attaches a labelled transcript

- **WHEN** the owner's agent transcribes and diarises a recording on the owner's machine, and calls `media.attach-speech` for its Source
- **THEN** the Source holds the transcript with its speaker labels, and no speech job runs for it

#### Scenario: The agent guide names the operation

- **WHEN** an agent reads the media workflow guidance
- **THEN** it finds `media.attach-speech`, the fields it takes and the codes it can return

### Requirement: An attached result binds to the original's bytes

`media.attach-speech` SHALL refuse with `ATTACH_BINARY_MISMATCH` when the call's `binary_sha256` or `binary_size` differs from the Source's original. It SHALL refuse a Source that has no audio or video original with `ATTACH_NOT_MEDIA`. A refused call SHALL write nothing.

#### Scenario: A result for other bytes is refused

- **WHEN** the call's `binary_sha256` differs from the Source's original
- **THEN** the operation refuses with `ATTACH_BINARY_MISMATCH`, and no page changes

### Requirement: The cell renders an attached result as it renders its own

The cell SHALL assign each segment to the speaker turn that overlaps it most, and merge the tracks by start, with the code that the speech pipeline uses. A segment without an end SHALL end at the next segment's start. The cell SHALL render the timed lines with the pipeline's transcript renderer, and split a long result into part pages as extraction does. A track name and a speaker label SHALL combine as the multitrack rules say.

#### Scenario: An attached result reads as an in-cell result

- **WHEN** the same segments and turns give one page through the speech pipeline and one through `media.attach-speech`
- **THEN** both pages hold the same timed lines, the same `speakers` and the same part pages

### Requirement: An attached result is published as an extraction

The cell SHALL publish an attached result through the in-cell extraction publication, guarded by the original's identity and the Source page's content hash. The Source SHALL record `extraction_origin: attached`, the engine identity as `extracted_by`, and the digest of the attached result. The Source SHALL NOT claim that the cell checked the engine identity.

#### Scenario: The Source says where its result came from

- **WHEN** an attached result is published
- **THEN** the Source records `extraction_origin: attached`, the engine that the call declared, and the result's digest

### Requirement: An attached result is checked against the recording's length

The cell SHALL read the recording's duration from its container metadata, without decoding. It SHALL refuse with `ATTACH_PAYLOAD_OUT_OF_BOUNDS`, naming the bound, a segment or turn outside the duration plus a tolerance, and a segment that ends before it starts. It SHALL also refuse a count of segments, turns or characters above a rate per audio second. A recording whose container states no duration SHALL be refused with `ATTACH_DURATION_UNKNOWN`.

#### Scenario: A segment past the end is refused

- **WHEN** a segment of a 60-minute recording starts at 75 minutes
- **THEN** the operation refuses with `ATTACH_PAYLOAD_OUT_OF_BOUNDS`, names the time bound, and no page changes

#### Scenario: Too much text for the recording is refused

- **WHEN** a call carries more characters than the rate per audio second allows for the recording's duration
- **THEN** the operation refuses with `ATTACH_PAYLOAD_OUT_OF_BOUNDS` and names the text bound

### Requirement: An attach replaces only an unlabelled or unfinished result

`media.attach-speech` SHALL replace a pending or failed extraction, and a completed one with no speaker labels and no `speaker_names`. It SHALL refuse with `ATTACH_REPLACES_LABELS` to replace a result with speaker labels or `speaker_names`, unless the call sets `replace_labelled: true`. A replaced completed extraction SHALL stay on a linked prior extraction page. The attach SHALL retire a waiting speech job, and a running job's later result SHALL NOT replace it.

#### Scenario: A pending recording takes the attached result

- **WHEN** the agent attaches a result to a Source whose extraction is pending
- **THEN** the Source holds the attached result, and no speech job waits for it

#### Scenario: An in-cell transcript is replaced and kept as prior

- **WHEN** the agent attaches a labelled result to a Source that holds an unlabelled in-cell transcript
- **THEN** the Source holds the attached result, and the in-cell transcript stays on a linked prior extraction page

#### Scenario: A labelled result is not replaced silently

- **WHEN** the agent attaches a result to a Source whose transcript carries speaker labels, without `replace_labelled`
- **THEN** the operation refuses with `ATTACH_REPLACES_LABELS`, and no page changes

#### Scenario: An explicit replacement of a labelled result

- **WHEN** the agent repeats that call with `replace_labelled: true`
- **THEN** the Source holds the attached result, and the labelled transcript stays on a linked prior extraction page

### Requirement: Names survive an attach, and an attach keeps no voice

When an attach replaces a result with `speaker_names`, the cell SHALL carry each name to a new label by the rule of a reprocess. A name that the call gives for a label SHALL win. The reply SHALL list each name that the cell could not carry. Each name that the call gives SHALL state whether a person gave it or a voice profile matched it. `speaker_verification` SHALL be `human-verified` only when a person named every label. An attach SHALL NOT enrol a voice profile.

#### Scenario: A person's naming survives an attach

- **WHEN** the agent attaches a labelled result with `replace_labelled: true` to a Source whose `Speaker B` a person named `Host`, and the new `Speaker A` covers mostly those lines
- **THEN** the new lines of `Speaker A` read `Host`, and `speaker_names` records it

#### Scenario: Names in the call enrol no voice

- **WHEN** the call names a label `Host`, given by a person
- **THEN** the Source's lines read `Host`, and the voice-profile store does not change

### Requirement: A repeated attach writes nothing new

Before it applies the replacement rules, `media.attach-speech` SHALL compare the call's original and result digest with the Source's current attached result. When they are equal, it SHALL write nothing and SHALL report the earlier outcome. A call retried after an unknown outcome SHALL follow the write door's mutation identity rules.

#### Scenario: A retry after a lost reply

- **WHEN** the agent's connection drops after an attach, and the agent repeats the same call
- **THEN** the operation reports the earlier outcome, and no page changes
