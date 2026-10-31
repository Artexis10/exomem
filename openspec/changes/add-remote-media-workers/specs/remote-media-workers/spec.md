## ADDED Requirements

### Requirement: A remote worker leases a media job through the claim fence

The service SHALL let a remote worker lease a media job through
`POST /media-worker/v1/claim`. A lease SHALL use the same claim transition as a
local claim: it SHALL increment `attempts` and `claim_revision`, set the job's
pin, and take a hydration slot when the original needs one. Each later call for
the lease SHALL name the `claim_revision`. The service SHALL refuse a call that
names another revision with `409 MEDIA_LEASE_LOST`.

#### Scenario: A worker leases a pending speech job
- **WHEN** a speech job is pending and a present worker with a matching capability calls claim
- **THEN** the job becomes `running` with an incremented `claim_revision`
- **AND** the response returns the revision, the lease expiry and the job's processing settings

#### Scenario: Nothing is eligible
- **WHEN** no job is eligible for the worker during the long-poll window
- **THEN** the service returns `204` and changes no job

#### Scenario: A superseded lease is refused
- **WHEN** a worker sends a heartbeat that names a revision older than the job's current `claim_revision`
- **THEN** the service returns `409 MEDIA_LEASE_LOST`

### Requirement: An expired lease returns its job

A lease SHALL expire when the worker sends no heartbeat within the configured
lease length, and SHALL NOT be renewed past the job timeout. The media
supervisor SHALL return each job with an expired lease from `running` to
`pending` and clear its pin and `hydrated` flag in the same write. An expiry
SHALL count against the job's timeout-stop budget, and a job that exhausts the
budget SHALL become `blocked` with `over_budget`.

A worker that hands a job back because it cannot run it now SHALL leave the
job pinned with its hydration slot freed, `returns` incremented and a `retry_at`
that doubles from 1 to 15 minutes. A person's retry SHALL reset `returns`.

#### Scenario: A worker disappears mid-job
- **WHEN** a worker holds a lease and stops sending heartbeats past the lease length
- **THEN** the job returns to `pending` and its pin is released
- **AND** another executor can claim it

#### Scenario: A hand-back keeps the original
- **WHEN** a worker releases a hydrated job because its device is busy
- **THEN** the job is `pending` with its pin kept, its hydration slot free and a `retry_at` in the future
- **AND** the next claim does not download the original again

#### Scenario: A late result is refused
- **WHEN** a worker hands in a result after its lease expired and the job was claimed again
- **THEN** the service refuses the result with `409 MEDIA_LEASE_LOST` and writes nothing

### Requirement: The vault owner publishes every remote result

The service SHALL take the sidecar hash, the evidence version, the binary
identity, the language pin and, for `reprocess`, the digest of the replaced
extraction when it grants a lease. It SHALL record a remote result with that
snapshot and publish it through the same parent publication path and
preconditions as a local result. A remote worker SHALL NOT write to the vault.

#### Scenario: A remote transcript is published
- **WHEN** a worker hands in an extraction for a lease it holds and the sidecar and original are unchanged since the grant
- **THEN** the service publishes the sidecar with the transcript and the `asr_*` facts that the worker reported

#### Scenario: The sidecar changed during the lease
- **WHEN** the sidecar changed between the grant and the result
- **THEN** the service does not publish the result and records the job as stale, as for a local result

### Requirement: The worker reads the original only through its lease

The service SHALL stream a leased job's original through
`GET /media-worker/v1/leases/{job}/original` from a held read handle, with
`Range` support. A store-backed original SHALL hydrate under the lease's pin and
hydration slot. The claim response SHALL carry the original's size and SHA-256.
The route SHALL apply the release gate of an original download. A job whose
original the gate does not release, and a job with `do_offload`, SHALL NOT be
eligible for a remote claim.

#### Scenario: A worker resumes a broken download
- **WHEN** a worker's download of a leased original breaks and it requests the remaining range
- **THEN** the service streams the remaining bytes

#### Scenario: A withheld original stays local
- **WHEN** the release gate withholds a pending job's original
- **THEN** no remote claim returns that job and the local worker can still process it

### Requirement: A worker credential admits only the worker routes

The owner SHALL issue, list and revoke worker credentials with the host CLI.
The service SHALL store only a hash of each token. A worker credential SHALL
admit the five `/media-worker/v1/` routes and no other route. A heartbeat, read,
result or release SHALL also require that the credential's worker holds the
lease.

#### Scenario: A worker credential cannot call a tool
- **WHEN** a request presents a worker credential to the MCP endpoint, a REST route or an upload route
- **THEN** the service refuses it as unauthenticated

#### Scenario: A worker cannot touch another worker's lease
- **WHEN** worker B sends a result for a lease that worker A holds
- **THEN** the service refuses it and the lease is unchanged

#### Scenario: A revoked credential stops at once
- **WHEN** the owner revokes a credential that holds a lease
- **THEN** the next call with it is refused and the lease expires normally

### Requirement: Configuration routes media kinds between executors

The service's per-vault media configuration SHALL name the media kinds that remote workers may
lease, whether the local worker may still take them when no remote worker is
present, and the lease, heartbeat and presence durations. A worker SHALL be
present when its credential made a claim or a heartbeat within the presence
duration. While a worker is present, or when local fallback is off, the local
worker SHALL skip the routed kinds. A remote claim SHALL return only routed
kinds that the worker declared, in the same claim order and under the same
hydration cap as a local claim. With no remote-worker configuration, the
service SHALL route nothing.

#### Scenario: Speech waits for the remote worker
- **WHEN** speech is routed with local fallback off and no worker is present
- **THEN** speech jobs stay `pending` and the local worker processes the other media kinds

#### Scenario: The local worker covers an absent worker
- **WHEN** speech is routed with local fallback on and no worker has been seen within the presence duration
- **THEN** the local worker processes speech jobs as before

### Requirement: Voice profiles stay with the vault owner

A remote worker SHALL return anonymous speaker turns and one voice embedding
per raw cluster. The service SHALL match the clusters against its local
voice-profile store and render the transcript with the same functions as the
local path. The service SHALL NOT send voice profiles to a worker and SHALL NOT
store the returned embeddings.

#### Scenario: An enrolled owner is named in a remote transcript
- **WHEN** a worker diarises a recording and one cluster matches the owner's enrolled profile
- **THEN** the published transcript labels that cluster's turns with the owner's name and the other turns with anonymous labels

### Requirement: The job's settings travel with the lease

The claim request SHALL carry the protocol version and the worker's
capabilities. The service SHALL refuse an unsupported version with
`426 MEDIA_WORKER_PROTOCOL_UNSUPPORTED`. The claim response SHALL carry the
language pin and mode, the reprocess flag, the timestamp setting and the ASR
decoding policy. The worker SHALL apply them in place of its own configuration.

#### Scenario: A language pin reaches the worker
- **WHEN** a job has the language pin `et` and a worker leases it
- **THEN** the worker decodes with `et`, and the sidecar records `asr_language_source: requested`

### Requirement: Media status shows remote leases

Media status SHALL list each active remote lease with its worker name, job and
lease age, and each worker credential with the time it was last seen.

#### Scenario: The owner sees where a job runs
- **WHEN** a worker holds a lease and the owner reads media status
- **THEN** the status names the worker, the job and the lease age
