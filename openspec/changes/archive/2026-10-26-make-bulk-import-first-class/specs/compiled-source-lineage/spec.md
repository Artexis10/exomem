## ADDED Requirements

### Requirement: A back-reference is a citation, not a completion claim

A Source's `ingested_into` SHALL record every page that cites it, whatever the citing page's status. Consumers SHALL treat it as a citation edge. Whether a Source is compiled SHALL be decided by the attention queue's source-coverage rule, never by the presence of a back-reference alone.

#### Scenario: A draft citation still writes its back-reference

- **WHEN** a draft note cites a Source that supports back-references
- **THEN** the Source's `ingested_into` names the draft in the same guarded batch, and the Source is not counted as compiled
