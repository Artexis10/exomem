## MODIFIED Requirements

### Requirement: Named-Speaker Attribution via Voice Profiles

The system SHALL resolve anonymous diarization clusters to enrolled speaker names when ASR
diarization is enabled (`EXOMEM_DIARIZE`) and at least one voice profile is enrolled — by
computing a per-cluster ECAPA voice embedding and matching it against profile centroids by
cosine similarity. A cluster SHALL be assigned a profile name only when the match clears the
configured threshold, margin, and standout rules; otherwise it SHALL remain anonymous.

Transcript rendering SHALL depend on the semantic-segments gate: with
`EXOMEM_SEMANTIC_SEGMENTS` unset, diarized transcripts render as merged same-speaker turns
(`[Hugo]: …`) exactly as today; with it set, they render as one timed line per ASR segment
(`[m:ss] [Hugo]: …`) with the label repeated per segment. In both modes the structured
`speakers` field SHALL carry the merged-turn shape unchanged.

#### Scenario: Enrolled speaker is named in the transcript

- **WHEN** a media file is diarized for a vault with an enrolled profile "Hugo" and a cluster's
  ECAPA centroid matches the Hugo centroid above threshold and margin
- **THEN** that cluster's turns are rendered as `[Hugo]: …` in the transcript text (prefixed
  `[m:ss]` per segment when `EXOMEM_SEMANTIC_SEGMENTS` is set)
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

#### Scenario: Timed rendering keeps merged-turn structure

- **WHEN** a diarized transcript is rendered with `EXOMEM_SEMANTIC_SEGMENTS` set
- **THEN** the text is per-segment timed lines while the structured `speakers` list remains
  the merged-turn shape, so the `speakers:` frontmatter and speaker filters are unaffected
