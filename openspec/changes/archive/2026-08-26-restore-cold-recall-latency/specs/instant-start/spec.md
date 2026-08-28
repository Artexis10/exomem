## ADDED Requirements

### Requirement: Projection-First Runtime Activation

The local runtime SHALL start filesystem observation and seed both event-maintained recall scopes before maintained-catalog verification begins. Events observed during the seed SHALL be replayed after authoritative seed publication, and activation SHALL wait for that catch-up replay before catalogue verification begins. When watchdog is unavailable but event indexes remain enabled, reconcile-only polling SHALL establish and maintain the projection; when event indexes are explicitly disabled, the system SHALL run the existing full verification only as background startup work. Graph drain, media reconciliation, and other optional/heavy workers MUST NOT contend with the projection/catalogue path before retrieval is actually admitted. They SHALL serialize behind semantic-corpus work while the initial warm is active, but a terminal semantic soft-failure MUST NOT strand them.

#### Scenario: Watcher seed precedes catalogue verification

- **WHEN** a local server starts with event-maintained indexes and the watcher enabled
- **THEN** filesystem observation starts and both recall scopes become live before catalogue verification reads their checkpoints
- **AND** catalogue verification does not take the non-live projection walk fallback

#### Scenario: Watcher-free startup remains functional

- **WHEN** watchdog is unavailable while event-maintained indexes remain enabled
- **THEN** reconcile-only polling seeds and maintains the recall projection
- **AND** transport liveness remains available

#### Scenario: Event indexes disabled retains explicit rollback behavior

- **WHEN** event-maintained indexes are explicitly disabled
- **THEN** transport liveness remains available
- **AND** required catalogue verification may use the existing background walk fallback
- **AND** the declared legacy lazy request fallback remains available

#### Scenario: An edit observed during seed is not overwritten

- **WHEN** filesystem observation reports an edit while the startup seed is still deriving its replacement maps
- **THEN** dispatch retains that event until seed publication completes
- **AND** activation remains behind the seed barrier until replay updates the published generation
- **AND** catalogue verification cannot admit the stale pre-replay checkpoint

#### Scenario: Terminal catalogue warm failure keeps heavy recovery gated

- **WHEN** the first managed warm finishes without retrieval-catalog admission
- **THEN** graph drain, media reconciliation, and other optional heavy workers remain gated
- **AND** a later proven repair releases them without a process restart

#### Scenario: Terminal semantic warm failure remains soft

- **WHEN** retrieval is admitted but the one-shot semantic-corpus warm finishes unsuccessfully
- **THEN** graph drain, media reconciliation, and other optional heavy workers continue
- **AND** the missing semantic ready bit does not create an unrecoverable startup wait

### Requirement: Retrieval Readiness Recovers After Repair

Retrieval admission SHALL be derived from proven current projection and catalogue state, not only from the outcome of the first warm attempt. The proof SHALL bind both maintained catalogue checkpoints to the exact live projection checkpoints used by the request. If startup catalogue warming fails and a later seed or catalogue repair converges, the runtime SHALL promote retrieval to ready without a process restart. If a previously ready runtime loses its live projection, catalogue equality, or request-pinned generation, it SHALL demote before serving another recall request.

#### Scenario: Later repair heals failed startup admission

- **WHEN** the first catalogue warm attempt fails and readiness reports retrieval unavailable
- **AND** both recall projections later become live and both maintained catalogue checkpoints match them
- **THEN** retrieval admission transitions to ready without restarting the process

#### Scenario: Lost projection cannot retain ready admission

- **WHEN** retrieval was ready and a required live recall projection is subsequently lost or invalidated
- **THEN** the next server recall request does not use walk fallback or stale ready admission
- **AND** retrieval is reported as warming or unavailable until proof converges again

#### Scenario: Projection advance cannot mix request generations

- **WHEN** a projection advances after catalogue admission proof but before the request copies its allowed paths
- **THEN** the request returns the retryable retrieval-warming outcome
- **AND** it does not combine the older catalogue with the newer projection

### Requirement: All Retrieval Modes Obey Admission

Keyword, hybrid, and vector-only requests SHALL obey the same required recall-projection admission boundary. Optional model-backed lanes remain soft-failing after lexical admission and MUST NOT be promoted into required readiness components.

#### Scenario: Vector mode cannot bypass projection warming

- **WHEN** a vector-only server request arrives before the recall projection is live
- **THEN** it returns the same retryable retrieval-warming outcome as other modes
- **AND** it does not construct a walk-backed allowlist

### Requirement: Disabled Warmup Preserves Lazy Operation

Explicitly disabling startup warmup SHALL leave local recall in its unverified lazy mode. The runtime MUST NOT create a managed warming state that can never finish when no warm was started.

#### Scenario: Warmup kill switch does not strand readiness

- **WHEN** `EXOMEM_DISABLE_WARMUP=1` is set at local runtime construction
- **THEN** retrieval admission remains unverified rather than permanently warming
- **AND** the existing lazy caller behavior remains available
