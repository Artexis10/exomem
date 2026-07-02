## ADDED Requirements

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

## MODIFIED Requirements

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
