## MODIFIED Requirements

### Requirement: Runtime Health Distinguishes Transport And Recall Admission

Local health surfaces SHALL distinguish process/transport liveness from retrieval admission. Retrieval SHALL be reported ready only when both required recall projections are live and both maintained catalogue checkpoints are proven exactly equal to those projections. A previously ready bit SHALL be revoked when that equality no longer holds. A process whose transport responds but whose projection/catalogue is warming or unavailable MUST NOT be reported as fully ready.

The readiness surface MAY answer from a ready proof for at most 30 seconds. It SHALL NOT reuse a not-ready or failed proof, or a standby's proof. A reused proof SHALL be void once the process records a retrieval admission change or promotes a standby. Nothing else voids it, so the other fields of a reused answer, such as the coordination role, the session-store state and the observability block, may be up to 30 seconds old. The answer SHALL report the proof's age, counted from when its measurement began.

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
- **THEN** health reports retrieval as warming or unavailable with `admitted=false`, at once when this process recorded the change and within 30 seconds otherwise
- **AND** background repair must prove the new equality before readiness returns

#### Scenario: A ready proof answers the next probes

- **WHEN** readiness proved ready less than 30 seconds ago and the process has recorded no admission change since
- **THEN** the next probe answers from that proof without proving again
- **AND** the answer reports `proof_age_seconds` as the proof's age

#### Scenario: A not-ready proof is never reused

- **WHEN** the last proof was not ready or failed
- **THEN** the next probe proves again
