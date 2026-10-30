## MODIFIED Requirements

### Requirement: Runtime Health Distinguishes Transport And Recall Admission

Local health surfaces SHALL distinguish process/transport liveness from retrieval admission. Retrieval SHALL be reported ready only when both required recall projections are live and both maintained catalogue checkpoints are proven exactly equal to those projections. A previously ready bit SHALL be revoked when that equality no longer holds. A process whose transport responds but whose projection/catalogue is warming or unavailable MUST NOT be reported as fully ready.

Once a managed process has itself proved that equality since its warm began, a catalogue that later trails a live projection SHALL be reported as retrieval state `lagging`, and the serving status SHALL stay ready, while per-request recall can still serve the published catalogue. That holds only when the warm's required stages are done, event indexes are enabled, and, for both scopes, a live projection and a published catalogue checkpoint exist, come from one process instance, and keep the access-policy and semantic-catalogue identity of the vault. `lagging` SHALL report the lagging scopes and the seconds this process has seen the published catalogue checkpoints unchanged. It SHALL NOT report `admitted=true` and SHALL NOT add a reason code. A process that has not proved that equality since its managed warm began, whose live or published projection is lost, or whose identity changed SHALL NOT report `lagging`.

The readiness surface MAY answer from a ready proof for at most 30 seconds. It SHALL NOT reuse a not-ready or failed proof, a proof that reports `lagging`, or a standby's proof. A reused proof SHALL be void once the process records a retrieval admission change or promotes a standby. Nothing else voids it, so the other fields of a reused answer, such as the coordination role, the session-store state and the observability block, may be up to 30 seconds old. The answer SHALL report the proof's age, counted from when its measurement began.

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
- **THEN** retrieval no longer reports ready with `admitted=true`, at once when this process recorded the change and within 30 seconds otherwise
- **AND** it reports `lagging` when the served-lag conditions hold and warming or unavailable otherwise
- **AND** background repair must prove the new equality before retrieval reports ready again

#### Scenario: A served lag keeps a proven process ready

- **WHEN** a managed process has proved both catalogues exactly equal to the live projections since its warm began, and a live projection then advances beyond the maintained catalogue checkpoint while both projections remain present under one identity
- **THEN** readiness answers with status ready and reports retrieval as `lagging` with `admitted=false`, the lagging scopes, and the seconds the published checkpoints have stayed unchanged
- **AND** the answer carries no reason code and the next probe proves again
- **AND** retrieval returns to ready with `admitted=true` once background repair proves the new equality

#### Scenario: A lost projection revokes readiness

- **WHEN** after admission a live projection is lost or invalidated, a scope has no published catalogue checkpoint, or the access-policy identity, the semantic-catalogue identity or the process instance changes
- **THEN** health reports retrieval as warming or unavailable with `admitted=false` and status not ready, at once when this process recorded the change and within 30 seconds otherwise
- **AND** background repair must prove the new equality before readiness returns

#### Scenario: A fresh process waits for a strict proof

- **WHEN** a process begins a managed warm, or holds only a proof made while it was unmanaged, and a live projection is ahead of the maintained catalogue
- **THEN** health reports retrieval as warming or unavailable with `admitted=false` and status not ready
- **AND** only a strict proof made by that managed process allows `lagging` afterwards

#### Scenario: A ready proof answers the next probes

- **WHEN** readiness proved ready less than 30 seconds ago and the process has recorded no admission change since
- **THEN** the next probe answers from that proof without proving again
- **AND** the answer reports `proof_age_seconds` as the proof's age

#### Scenario: A not-ready proof is never reused

- **WHEN** the last proof was not ready or failed
- **THEN** the next probe proves again
