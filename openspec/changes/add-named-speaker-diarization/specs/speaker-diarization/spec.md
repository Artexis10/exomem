## ADDED Requirements

### Requirement: Named-Speaker Attribution via Voice Profiles

The system SHALL resolve anonymous diarization clusters to enrolled speaker names when ASR
diarization is enabled (`KB_MCP_DIARIZE`) and at least one voice profile is enrolled — by
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

The capability SHALL change no behavior unless `KB_MCP_DIARIZE` is set AND at least one profile
is enrolled. With diarization disabled, or enabled with zero enrolled profiles, the system
SHALL produce output byte-identical to the current anonymous diarization (or plain transcript).

#### Scenario: No profiles enrolled

- **WHEN** `KB_MCP_DIARIZE` is set but no voice profiles are enrolled
- **THEN** diarization runs exactly as today, emitting anonymous `[Speaker A]: …` turns
- **AND** no voice-embedding model is loaded

#### Scenario: Diarization disabled

- **WHEN** `KB_MCP_DIARIZE` is unset
- **THEN** extraction emits the plain transcript with no diarization and no profile lookup

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

The system SHALL expose enrollment via the `python -m kb_mcp` CLI: `enroll-speaker`
(extract an ECAPA centroid from an audio sample and persist a profile, with a `--self` flag
for the vault owner), `list-speakers`, and `remove-speaker`. Enrollment SHALL NOT be exposed
as an MCP connector tool.

#### Scenario: Enroll the vault owner

- **WHEN** `python -m kb_mcp enroll-speaker --name Hugo --self <sample.wav>` is run
- **THEN** a profile "Hugo" with `is_self: true` and a 192-dim centroid is stored
- **AND** `list-speakers` reports it

#### Scenario: Remove a profile

- **WHEN** `python -m kb_mcp remove-speaker --name Hugo` is run
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
