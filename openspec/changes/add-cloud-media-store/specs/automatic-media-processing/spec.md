## ADDED Requirements

### Requirement: An evicted original keeps its processing state

On a Cloud cell with the media store, media processing SHALL resolve each original through the store's state function. An evicted original SHALL keep its sidecar, extraction marker, transcript and derived rows, and reconciliation SHALL treat it as unchanged. Eviction SHALL NOT select an original with pending or running processing. An explicit re-process or retry of an evicted original SHALL hydrate and verify it before the worker reads it. Processing status SHALL report an evicted original as stored, not missing.

#### Scenario: Reconciliation passes over an evicted recording

- **WHEN** periodic reconciliation runs over a completed recording that is evicted
- **THEN** it creates no job and does not rewrite the sidecar

#### Scenario: An owner re-processes an evicted recording

- **WHEN** a caller explicitly re-processes an evicted recording
- **THEN** the original is hydrated and verified first, and the job then runs on the verified bytes

#### Scenario: Status lists an evicted recording

- **WHEN** a caller reads processing status for an evicted recording
- **THEN** the status reports it as stored with its last processing state, not as missing
