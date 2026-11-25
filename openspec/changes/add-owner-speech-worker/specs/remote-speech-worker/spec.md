## ADDED Requirements

### Requirement: Remote speech leaves the speech stage to a speech worker
A cell whose configuration sets `speech_mode` to `remote` SHALL record each recording's speech stage as a speech item for a speech worker, and SHALL NOT run it in the media worker unless its fallback applies. Every other stage of the same recording SHALL run in the cell as in `in_cell` mode. A cell with its speech engine switched off SHALL create no speech item.

#### Scenario: Speech waits for a speech worker
- **WHEN** an owner uploads a recording to a cell in remote speech
- **THEN** the recording's sidecar stays pending and one speech item waits with no lease
- **AND** the media worker runs no speech for it

#### Scenario: A video's other stages still run
- **WHEN** an owner uploads a video to a cell in remote speech
- **THEN** the media worker computes its frame vectors without waiting for the speech item

#### Scenario: Reconciliation keeps one item
- **WHEN** periodic reconciliation runs while a recording's speech item is pending or leased
- **THEN** no second speech item is created for that recording

### Requirement: A claim grants one exclusive lease
A claim by an authorized principal SHALL return at most one speech item that is pending or whose lease has expired, as a lease naming the item, its revision, the original's size and SHA-256, the job's options, the principal's own recorded progress, and the expiry. Each claim SHALL increment the item's revision. No two principals SHALL hold a live lease on the same item.

#### Scenario: Two speech workers never get the same item
- **WHEN** two speech workers claim at the same time and one item is pending
- **THEN** exactly one receives the lease and the other receives no item

#### Scenario: Nothing to do
- **WHEN** a speech worker claims and no item is ready within the claim's wait
- **THEN** the cell answers with no item, and held no database connection while it waited

### Requirement: A lease ends only by its own expiry, completion or release
A heartbeat by the leasing principal at the current revision SHALL extend the lease and record progress; any other heartbeat SHALL be answered `lease_lost`. A lease SHALL become claimable only when its expiry has passed, it completes, or it is released. Recovery of the media worker's interrupted jobs SHALL NOT end a lease. A cell SHALL give one principal no more live leases than the configured cap.

#### Scenario: Media worker restarts keep the lease
- **WHEN** the media worker child restarts while a speech worker holds a live lease
- **THEN** the lease and its revision are unchanged

#### Scenario: A vanished speech worker's item returns
- **WHEN** a speech worker stops sending heartbeats and its lease expires
- **THEN** the next claim can receive the item at a new revision

#### Scenario: Lease cap
- **WHEN** a principal that holds the cap of live leases claims again
- **THEN** the cell answers with no item

### Requirement: Completion is accepted once, for the current lease and original
A completion SHALL carry the item, its revision and the original's SHA-256. The cell SHALL accept it only from the principal that holds the live lease at that revision, and only when the SHA-256 equals the sidecar's recorded `binary_sha256`. The cell SHALL record the rendered result durably before it answers, SHALL publish it through the same stale-input check as an in-cell result, and SHALL keep a receipt that answers a repeated completion without a second publication.

#### Scenario: Stale or lost lease refused
- **WHEN** a speech worker completes after its lease expired and another principal claimed the item
- **THEN** the cell answers `lease_lost` and keeps nothing

#### Scenario: Changed original refused
- **WHEN** the owner replaced the recording after the claim and the speech worker completes with the old SHA-256
- **THEN** the cell answers `original_changed` and keeps nothing

#### Scenario: Retried completion
- **WHEN** a speech worker loses the response to an accepted completion and sends it again
- **THEN** the cell answers `already_completed` and the sidecar holds one transcript

### Requirement: The cell renders remote results with the in-cell logic
A completion SHALL carry timed ASR segments, the speaker turns when labels were asked for, one embedding per speaker label when the owner turned speaker naming on, and the engine identity. The cell SHALL validate it against a schema whose bounds scale with the recording's duration, and SHALL produce the transcript, labels, names and timed text with the same merge and rendering that in-cell speech uses. The sidecar SHALL record the speech worker's engine identity.

#### Scenario: Same rendering as in-cell
- **WHEN** a completion holds segments and turns equal to what the in-cell engine produced for the same recording
- **THEN** the published sidecar text equals the in-cell result, except for the engine identity

#### Scenario: Malformed payload refused
- **WHEN** a completion holds a segment outside the recording's duration, or more text than its duration allows
- **THEN** the cell refuses it before any write

#### Scenario: No embeddings without naming
- **WHEN** the owner has speaker naming off
- **THEN** the completion carries no embeddings and the cell accepts it

### Requirement: The speech credential reaches only speech routes
A speech worker SHALL act only under an authorization that the owner granted with the `exomem.speech` scope. The gateway SHALL admit that scope only on the speech claim, heartbeat, complete, release and original routes. The cell SHALL serve an original on the speech original route only to the principal that holds its live lease.

#### Scenario: Speech scope cannot read notes or other originals
- **WHEN** a speech-scoped token calls `/mcp`, a search, `/upload`, `/download`, or the original route for an item it does not lease
- **THEN** the gateway or the cell refuses it

### Requirement: Speech requests carry their own proof purpose
The gateway SHALL sign each speech request with the `speech` proof purpose, bound to the route, the item and the revision, and carrying the principal. A cell SHALL refuse a proof purpose it does not implement.

#### Scenario: An older cell refuses speech requests
- **WHEN** the gateway sends a `speech` proof to a cell built before this capability
- **THEN** the cell refuses the request

### Requirement: The owner can see and revoke speech authorizations
Home SHALL list each speech authorization with its last use. Revoking one SHALL refuse its next request.

#### Scenario: Revocation stops the speech worker
- **WHEN** the owner revokes the authorization in Home
- **THEN** the next claim or heartbeat is answered `revoked`, and the lease expires normally

### Requirement: The speech worker keeps no plaintext longer than its lease
The speech worker SHALL verify each original's size and SHA-256 before it transcribes. It SHALL delete its copy of the original and any partial transcript when the job completes or is released, when the cell answers `lease_lost`, `original_changed` or `revoked`, and when the recording is deleted.

#### Scenario: Tampered download
- **WHEN** the downloaded bytes do not match the lease's size and SHA-256
- **THEN** the speech worker keeps no file, transcribes nothing and releases the item with a reason

#### Scenario: Lost lease clears local data
- **WHEN** a heartbeat is answered `lease_lost`
- **THEN** the speech worker stops the job and deletes its copy and partial transcript

### Requirement: The owner sets up a speech worker with one command
`evermatter speech-worker setup` SHALL sign the owner in with the speech scope, detect the GPU, prepare the speech engines and the diarizer, and install a user service on Linux and WSL. The speech worker SHALL run one job per GPU and SHALL resume from its own recorded progress when it regains the same lease.

#### Scenario: Short sleep mid-recording
- **WHEN** the owner's machine sleeps during a long recording and wakes before the lease expires
- **THEN** the speech worker resumes from its recorded progress and completes the item once

### Requirement: The owner sees remote speech in status
Status SHALL report the speech items that wait and since when, the last speech worker contact with the authorization's display name, and each transcript produced by the fallback, marked as having no speaker labels. An unknown speech worker state SHALL be shown as unknown, never as idle.

#### Scenario: No speech worker running
- **WHEN** speech items wait and no speech worker has contacted the cell for an hour
- **THEN** status shows how many wait, since when, and the time of the last contact

### Requirement: Fallback runs a long-waiting item in the cell
When a cell sets `remote_speech_fallback_after` and a speech item has existed that long with no live lease, the media worker SHALL run its speech in the cell without speaker labels and mark the item done. A later reprocess of that recording SHALL go to a speech worker.

#### Scenario: Fallback after the configured wait
- **WHEN** a speech item has waited longer than the fallback setting with no live lease
- **THEN** the cell publishes an unlabelled transcript and status marks it as a fallback

### Requirement: The contract does not depend on the kind of speech worker
The cell SHALL authorize speech requests by principal and scope, and SHALL apply the same lease rules and caps to every principal, so that a later platform-operated speech worker can use the contract under its own principal and the owner's opt-in.

#### Scenario: Two principals, one contract
- **WHEN** two authorized principals with the speech scope claim from the same cell
- **THEN** each receives leases under the same rules and caps
