## ADDED Requirements

### Requirement: Runtime Health Distinguishes Transport And Recall Admission

Local health surfaces SHALL distinguish process/transport liveness from retrieval admission. Retrieval SHALL be reported ready only when both required recall projections are live and both maintained catalogue checkpoints are proven exactly equal to those projections. A previously ready bit SHALL be revoked when that equality no longer holds. A process whose transport responds but whose projection/catalogue is warming or unavailable MUST NOT be reported as fully ready.

#### Scenario: Live transport with warming recall is not fully ready

- **WHEN** the service responds to health probes while required recall projection or catalogue proof is incomplete
- **THEN** liveness reports the process as running
- **AND** readiness reports retrieval as warming or unavailable with `admitted=false`

#### Scenario: Converged repair updates readiness

- **WHEN** a later background repair proves both maintained catalogues against live recall checkpoints
- **THEN** readiness reports retrieval as ready with `admitted=true`
- **AND** installers and deployment acceptance can distinguish that state from a generic HTTP response

#### Scenario: Stale catalogue proof revokes readiness

- **WHEN** either live projection advances beyond the maintained catalogue checkpoint after admission
- **THEN** health reports retrieval as warming or unavailable with `admitted=false`
- **AND** background repair must prove the new equality before readiness returns
