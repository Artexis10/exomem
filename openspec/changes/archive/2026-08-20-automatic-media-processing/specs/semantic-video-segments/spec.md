## ADDED Requirements

### Requirement: Canonical Automatic Jobs Request Timed Rendering
The media worker SHALL explicitly request timed rendering for canonical automatic audio/video jobs, independently of the semantic-segmentation feature gate. The low-level extraction API default SHALL remain byte-compatible for callers that do not request timestamps, and semantic boundary fusion SHALL remain controlled by its existing gate.

#### Scenario: Automatic job is timed with gate unset
- **WHEN** the media worker transcribes an automatic audio/video job while `EXOMEM_SEMANTIC_SEGMENTS` is unset
- **THEN** the transcript is rendered as timed ASR segment lines and the engine includes `+timed`

#### Scenario: Direct low-level caller remains compatible
- **WHEN** a caller invokes the low-level extraction API without requesting timestamps and the gate is unset
- **THEN** transcript rendering remains byte-identical to the pre-change flat output

#### Scenario: Semantic segmentation gate still controls chunk fusion
- **WHEN** a timed automatic sidecar is indexed while semantic segmentation is disabled
- **THEN** the existing non-semantic chunking policy applies despite the transcript carrying timestamps
