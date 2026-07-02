# speaker-diarization Specification

## Purpose
TBD - created by archiving change add-named-speaker-diarization. Update Purpose after archive.
## Requirements
### Requirement: Named-Speaker Attribution via Voice Profiles

The system SHALL resolve anonymous diarization clusters to enrolled speaker names when ASR
diarization is enabled (`EXOMEM_DIARIZE`) and at least one voice profile is enrolled — by
computing a per-cluster ECAPA voice embedding and matching it against profile centroids by
cosine similarity. A cluster SHALL be assigned a profile name only when the match clears the
configured threshold, margin, and standout rules; otherwise it SHALL remain anonymous.

#### Scenario: Enrolled speaker is named in the transcript

- **WHEN** a media file is diarized for a vault with an enrolled profile "Hugo" and a cluster's
  ECAPA centroid matches the Hugo centroid above threshold and margin
- **THEN** that cluster's turns are rendered as `[Hugo]: …` in the transcript text
- **AND** the structured `speakers` field carries `speaker: "Hugo"` for those turns

#### Scenario: Unknown voice stays anonymous

- **WHEN** a cluster's centroid does not clear any profile's threshold/margin/standout rules
- **THEN** the cluster is labeled with a stable anonymous label (`Speaker A`, `Speaker B`, … by
  first-onset order)
- **AND** no profile name is applied to it

#### Scenario: Over-split single speaker is merged before attribution

- **WHEN** pyannote splits one speaker into two clusters whose centroids are within the merge
  threshold
- **THEN** the two clusters are merged via average-linkage before attribution
- **AND** a single profile can label the merged group

### Requirement: Default-Off and Anonymous Fallback

The capability SHALL change no behavior unless `EXOMEM_DIARIZE` is set truthy AND at least one
profile is enrolled. The values `""`, `0`, `false`, `no`, and `off` (case-insensitive, after
trimming whitespace) SHALL be treated as unset. With diarization disabled, or enabled with zero
enrolled profiles, the system SHALL produce output byte-identical to the current anonymous
diarization (or plain transcript).

#### Scenario: No profiles enrolled

- **WHEN** `EXOMEM_DIARIZE` is set but no voice profiles are enrolled
- **THEN** diarization runs exactly as today, emitting anonymous `[Speaker A]: …` turns
- **AND** no voice-embedding model is loaded

#### Scenario: Diarization disabled

- **WHEN** `EXOMEM_DIARIZE` is unset
- **THEN** extraction emits the plain transcript with no diarization and no profile lookup

#### Scenario: Falsy value counts as unset

- **WHEN** `EXOMEM_DIARIZE` is set to `0`, `false`, `no`, `off`, or `""` (any case)
- **THEN** extraction behaves exactly as if the variable were unset

### Requirement: Soft-Fail Degradation

The named-attribution path SHALL soft-fail. Any failure — a missing `speechbrain`
dependency, an unloadable ECAPA model, a GPU/cuDNN error, or an inference exception — SHALL
be logged and degrade to the existing anonymous diarization (or plain transcript). The
transcript extraction MUST still complete successfully; the path MUST NOT raise.

#### Scenario: Voice-embedding dependency absent

- **WHEN** the `[diarization]` extra's `speechbrain` is not importable
- **THEN** the failure is logged once and diarization proceeds anonymously
- **AND** transcript extraction completes normally

#### Scenario: Embedding inference fails on GPU

- **WHEN** ECAPA inference raises (e.g. a cuDNN shadow or OOM)
- **THEN** the error is logged and the file's clusters stay anonymous
- **AND** the transcript and its other extracted fields are persisted unchanged

### Requirement: Local Voice-Profile Store

Voice profiles SHALL be persisted in a single local JSON store that is operational
infrastructure beside the embedding sidecar — NOT under the vault's note trees, NOT a
queryable markdown sidecar, and never indexed by `find`. Each profile SHALL record its name,
a 192-dim ECAPA centroid, a per-profile threshold, a sample count, and an `is_self` flag.

#### Scenario: Profile persisted outside vault content

- **WHEN** a speaker is enrolled
- **THEN** the profile is written to the JSON store in the operational sidecar directory
- **AND** no file under the vault's `Knowledge Base/` note trees is created or modified

#### Scenario: Multi-sample enrollment averages the centroid

- **WHEN** the same name is enrolled from an additional audio sample
- **THEN** the stored centroid is the running average over all samples
- **AND** the `samples` count reflects the number of samples enrolled

### Requirement: CLI Speaker Enrollment

The system SHALL expose enrollment via the `python -m exomem` CLI: `enroll-speaker`
(extract an ECAPA centroid from an audio sample and persist a profile, with a `--self` flag
for the vault owner), `list-speakers`, and `remove-speaker`. Enrollment SHALL NOT be exposed
as an MCP connector tool.

#### Scenario: Enroll the vault owner

- **WHEN** `python -m exomem enroll-speaker --name Hugo --self <sample.wav>` is run
- **THEN** a profile "Hugo" with `is_self: true` and a 192-dim centroid is stored
- **AND** `list-speakers` reports it

#### Scenario: Remove a profile

- **WHEN** `python -m exomem remove-speaker --name Hugo` is run
- **THEN** the "Hugo" profile is deleted from the store
- **AND** subsequent diarization labels that voice anonymously again

### Requirement: Deterministic Pure-Substrate Measurement

Speaker attribution SHALL be a deterministic measurement: a frozen ECAPA embedding plus fixed
cosine thresholds, with no generative or reasoning model in the path. To preserve embedding
parity the implementation SHALL disable TF32 for voice-embedding inference. The system SHALL
prefer leaving a cluster anonymous over assigning an uncertain name (never mis-name).

#### Scenario: Deterministic labeling

- **WHEN** the same audio and the same profile store are processed twice
- **THEN** the resolved speaker labels are identical across runs

#### Scenario: Ambiguous match prefers anonymity

- **WHEN** a cluster is near two profiles' centroids within the margin (ambiguous)
- **THEN** no name is assigned and the cluster stays anonymous

### Requirement: Re-Diarize Back-Fill

The system SHALL provide a re-diarize mode on the media back-fill (`backfill-media --rediarize`)
that re-extracts audio/video files whose sidecar records a completed ASR engine WITHOUT the
`+diarized` marker, so recordings transcribed before diarization was enabled gain labeled turns
and `speakers:` frontmatter. The mode SHALL be idempotent (a `+diarized` sidecar is never
re-processed), SHALL only target audio/video (never image/pdf, never `pending`/`failed:`/
`no-audio` sidecars), SHALL NOT re-run CLIP indexing for already-indexed media, and SHALL be a
logged no-op when diarization is not enabled. When a re-extraction comes back without the
`+diarized` marker (diarization soft-failed), the system SHALL leave the existing sidecar
unchanged and stop re-diarizing for the remainder of the pass.

#### Scenario: Pre-diarization recording gains speakers

- **WHEN** `backfill-media --rediarize` runs with `EXOMEM_DIARIZE` enabled over a sidecar with
  `extracted_by: faster-whisper:large-v3` and no `speakers:`
- **THEN** the file is re-extracted and the sidecar records the `+diarized` engine, the labeled
  transcript, and a `speakers:` frontmatter list

#### Scenario: Second run is a no-op

- **WHEN** `backfill-media --rediarize` runs again after a successful re-diarize pass
- **THEN** no file is re-extracted (the `+diarized` marker is the done-state)

#### Scenario: Disabled flag guards the pass

- **WHEN** `--rediarize` is passed but `EXOMEM_DIARIZE` is not enabled
- **THEN** re-diarization is skipped with a logged message and the rest of the back-fill
  (sidecars, OCR, CLIP) proceeds unchanged

#### Scenario: Soft-failed diarization leaves the sidecar untouched

- **WHEN** a re-extraction returns a plain (non-`+diarized`) transcript because diarization
  soft-failed
- **THEN** the existing sidecar bytes are unchanged and re-diarization stops for the remaining
  files in the pass

#### Scenario: Attribution uses the invoked vault's profiles

- **WHEN** `backfill-media --rediarize --vault <root>` runs without `EXOMEM_VAULT_PATH`
  exported in the shell
- **THEN** named attribution matches against `<root>`'s voice-profile store (the vault is
  threaded through extraction; env resolution is only a fallback for callers without one)
- **AND** the media worker likewise attributes against its own vault's store

### Requirement: Startup Readiness Diagnostics

The system SHALL log one diarization-readiness summary when the media worker starts, reporting:
whether diarization is enabled, whether the diarizer sidecar venv is provisioned, whether a
HuggingFace token is present (as a boolean only — the token value SHALL never be logged), and the
count and names of enrolled voice profiles. The line SHALL log at WARNING when diarization is
enabled but the sidecar venv or token is missing, else at INFO. The check SHALL never raise.
Additionally, when diarization is enabled and the sidecar venv is missing at extraction time, the
soft-fail SHALL be logged at WARNING (not DEBUG).

#### Scenario: Healthy stack logs INFO

- **WHEN** the media worker starts with diarization enabled, the sidecar venv provisioned, a
  token present, and one enrolled profile
- **THEN** an INFO line reports `enabled=True`, `sidecar_venv=True`, `hf_token=True`,
  `profiles=1` with the profile name

#### Scenario: Broken stack logs WARNING

- **WHEN** the media worker starts with diarization enabled but the sidecar venv missing
- **THEN** the readiness line logs at WARNING

#### Scenario: Token value never appears

- **WHEN** the readiness line is logged with `HUGGINGFACE_TOKEN` set
- **THEN** the log output contains `hf_token=True` and does not contain the token value

