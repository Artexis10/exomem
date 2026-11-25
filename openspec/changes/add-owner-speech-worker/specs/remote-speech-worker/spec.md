## ADDED Requirements

### Requirement: A worker-mode cell leaves speech to an outside worker
A cell whose configuration sets `speech_mode` to `worker` SHALL keep each speech job pending for an outside worker and SHALL NOT run it on the cell's CPU, unless the job's fallback applies. Every other media stage SHALL run in the cell as before. A cell without that setting SHALL run speech in the cell.

#### Scenario: Speech waits for a worker
- **WHEN** an owner uploads a recording to a cell in worker mode
- **THEN** the recording's sidecar stays pending and its speech job waits with no lease
- **AND** the cell's media worker runs no speech for it

#### Scenario: Other stages still run
- **WHEN** an owner uploads a scanned PDF to a cell in worker mode
- **THEN** the cell extracts its text as it does in local mode

#### Scenario: Fallback after the configured wait
- **WHEN** a speech job has waited longer than the cell's `worker_fallback_after` with no live lease
- **THEN** the cell runs that job on its CPU without speaker labels
- **AND** status reports the job as run in fallback

### Requirement: A claim grants one exclusive, expiring lease
A claim by an authorized principal SHALL return at most one pending speech job as a lease. The lease SHALL name the job, its claim revision, the original's path, SHA-256 and size, the job's options, any recorded progress and the lease's expiry. No other principal SHALL hold a lease on the same job at the same time.

#### Scenario: Two workers never get the same job
- **WHEN** two workers claim at the same time and one job is pending
- **THEN** exactly one receives the lease and the other receives no job

#### Scenario: Nothing to do
- **WHEN** a worker claims and no speech job is pending within the claim's wait
- **THEN** the cell answers with no job, and held no database connection while it waited

### Requirement: A lease lives only while the worker reports
A heartbeat SHALL extend the lease and record progress. An expired lease SHALL return the job to pending with its recorded progress. A cell SHALL refuse a claim that would give one principal more live leases than the configured cap.

#### Scenario: A vanished worker's job returns
- **WHEN** a worker stops sending heartbeats and its lease expires
- **THEN** the job is pending again and the next claim can receive it, with its recorded progress

#### Scenario: Lease cap
- **WHEN** a principal that already holds the cap of live leases claims again
- **THEN** the cell answers with no job

### Requirement: Completion is idempotent and bound to the original
A completion SHALL carry the job ID, the claim revision and the original's SHA-256. The cell SHALL accept it only for the current revision and only when the SHA-256 equals the sidecar's recorded `binary_sha256`. A repeat of an accepted completion SHALL return the first result without a second publication. The cell SHALL record an accepted result durably before it answers, and SHALL publish it through the same stale-input check as an in-cell result.

#### Scenario: Stale claim refused
- **WHEN** a worker completes with a claim revision older than the job's current one
- **THEN** the cell refuses with `stale_claim` and keeps nothing

#### Scenario: Changed original refused
- **WHEN** the owner replaced the recording after the claim and the worker completes with the old SHA-256
- **THEN** the cell refuses with `original_changed` and keeps nothing

#### Scenario: Retried completion
- **WHEN** a worker loses the response to an accepted completion and sends it again
- **THEN** the cell answers `already_completed` and the sidecar holds one transcript

### Requirement: The cell renders worker results with the in-cell logic
A completion SHALL carry timed ASR segments, the speaker turns when labels were asked for, and the engine identity. The cell SHALL validate the payload against its schema with size bounds that scale with the original's duration, and SHALL produce the transcript, speaker labels and timed text with the same merge and rendering that in-cell speech uses. The sidecar SHALL record the worker's engine identity.

#### Scenario: Same rendering as in-cell
- **WHEN** a worker completes with segments and turns that equal what the in-cell engine produced for the same recording
- **THEN** the published sidecar text equals the in-cell result, except for the engine identity

#### Scenario: Oversized or malformed payload refused
- **WHEN** a completion holds more segment text than the recording's duration allows, or a segment outside the recording's duration
- **THEN** the cell refuses it before any write

### Requirement: The worker credential is narrow and revocable
A worker SHALL act only under an authorization that the owner granted with the `exomem.speech` scope. That scope SHALL admit only the speech claim, heartbeat, complete and release requests, and a download of an original under a live lease that the same principal holds. Home SHALL list each such authorization with its last use, and the owner SHALL be able to revoke it there; a revoked authorization SHALL be refused on its next request.

#### Scenario: Speech scope cannot read notes
- **WHEN** a speech-scoped token calls the MCP endpoint, a search, an upload, or a download of an original without its lease
- **THEN** the gateway or the cell refuses it

#### Scenario: Revocation stops the worker
- **WHEN** the owner revokes the worker's authorization in Home
- **THEN** the worker's next claim or heartbeat is refused, and its lease expires normally

### Requirement: The owner sets up a worker with one command
`evermatter speech-worker setup` SHALL sign the owner in with the speech scope, detect the GPU, prepare the speech engines and the diarizer, and install a user service on Linux and WSL. The worker SHALL verify each download's size and SHA-256 before it transcribes, delete its local copy after completion or release, run one job per GPU, and resume a job from its recorded progress after a restart.

#### Scenario: Restart mid-recording
- **WHEN** the owner's machine sleeps during a three-hour recording and wakes before the lease expires
- **THEN** the worker resumes from its last recorded progress and completes the job once

#### Scenario: Tampered download
- **WHEN** the downloaded bytes do not match the lease's size and SHA-256
- **THEN** the worker keeps no file, transcribes nothing and releases the job with a reason

### Requirement: The owner sees the worker queue
Status SHALL report the speech jobs that wait for a worker and since when, the last worker contact with the authorization's display name, and each job run in fallback. An unknown worker state SHALL be shown as unknown, never as idle.

#### Scenario: No worker running
- **WHEN** speech jobs wait and no worker has contacted the cell for an hour
- **THEN** status shows how many wait, since when, and the time of the last contact

### Requirement: The contract does not depend on the kind of worker
The cell SHALL authorize speech requests by principal and scope. The contract SHALL NOT assume that the worker is the owner's own machine, so that a later platform-operated worker can use it under its own principal and an owner's opt-in.

#### Scenario: Two principals, one contract
- **WHEN** two authorized principals with the speech scope claim from the same cell
- **THEN** each receives leases under the same rules and caps
