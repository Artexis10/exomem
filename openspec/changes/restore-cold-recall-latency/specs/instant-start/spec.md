## ADDED Requirements

### Requirement: Projection-First Runtime Activation

The local runtime SHALL start filesystem observation and seed both event-maintained recall scopes before maintained-catalog verification begins. When event indexes or the watcher are unavailable, the system SHALL run the existing full verification only as background startup work. Graph drain, media reconciliation, and other optional/heavy workers MUST NOT contend with the projection/catalogue admission path before required retrieval and semantic state is admitted.

#### Scenario: Watcher seed precedes catalogue verification

- **WHEN** a local server starts with event-maintained indexes and the watcher enabled
- **THEN** filesystem observation starts and both recall scopes become live before catalogue verification reads their checkpoints
- **AND** catalogue verification does not take the non-live projection walk fallback

#### Scenario: Watcher-free startup remains functional

- **WHEN** watchdog or event-maintained indexes are explicitly unavailable
- **THEN** transport liveness remains available
- **AND** required catalogue verification may use the existing background walk fallback
- **AND** no server request performs that fallback

### Requirement: Retrieval Readiness Recovers After Repair

Retrieval admission SHALL be derived from proven current projection and catalogue state, not only from the outcome of the first warm attempt. If startup catalogue warming fails and a later seed or catalogue repair converges, the runtime SHALL promote retrieval to ready without a process restart. If a previously ready runtime loses its live projection or catalogue proof, it SHALL demote before serving another recall request.

#### Scenario: Later repair heals failed startup admission

- **WHEN** the first catalogue warm attempt fails and readiness reports retrieval unavailable
- **AND** both recall projections later become live and both maintained catalogue checkpoints match them
- **THEN** retrieval admission transitions to ready without restarting the process

#### Scenario: Lost projection cannot retain ready admission

- **WHEN** retrieval was ready and a required live recall projection is subsequently lost or invalidated
- **THEN** the next server recall request does not use walk fallback or stale ready admission
- **AND** retrieval is reported as warming or unavailable until proof converges again

### Requirement: All Retrieval Modes Obey Admission

Keyword, hybrid, and vector-only requests SHALL obey the same required recall-projection admission boundary. Optional model-backed lanes remain soft-failing after lexical admission and MUST NOT be promoted into required readiness components.

#### Scenario: Vector mode cannot bypass projection warming

- **WHEN** a vector-only server request arrives before the recall projection is live
- **THEN** it returns the same retryable retrieval-warming outcome as other modes
- **AND** it does not construct a walk-backed allowlist
