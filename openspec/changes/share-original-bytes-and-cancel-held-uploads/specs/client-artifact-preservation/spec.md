# Spec Delta

## ADDED Requirements

### Requirement: A caller can explicitly cancel a held upload

Exomem SHALL expose one bounded cancellation operation across MCP, REST, and CLI through the same leaf. It SHALL accept held-upload handles, including holds restored after failed preservation. Cancellation SHALL remove only the verified caller's unclaimed temporary upload. A failed preservation SHALL otherwise keep its existing retry and expiry behavior.

#### Scenario: Cancellation releases a failed upload's quota

- **WHEN** preservation fails, restores a hold, and the holding session explicitly cancels that hold
- **THEN** the temporary payload and hold record are removed
- **AND** the session can upload another file within the capacity that the removed hold occupied
- **AND** no canonical artifact, sidecar, release grant, or media job is removed

#### Scenario: A caller leaves a failed upload available for retry

- **WHEN** preservation fails and the caller does not cancel the restored hold
- **THEN** its handle and original expiry remain available for the existing retry path

### Requirement: Cancellation preserves upload ownership and nondisclosure

Cancellation SHALL require a verified session and the held upload's opaque handle. It SHALL use the existing upload binding. A foreign, absent, expired, or already spent handle SHALL return the same unavailable outcome, without disclosing another session's upload or any canonical filing. The operation SHALL accept no arbitrary filesystem path and SHALL fetch no external URL.

#### Scenario: Another session presents a known handle

- **WHEN** a different verified session submits the handle of a live held upload
- **THEN** its result equals the unavailable result for an absent handle
- **AND** the holding session retains the upload and its quota accounting

#### Scenario: An attachment URL is submitted for cancellation

- **WHEN** a caller submits a remote attachment URL instead of a held-upload handle
- **THEN** validation names the required handle and sends no network request

### Requirement: Cancellation and redemption have one winner

Cancellation SHALL coordinate with the existing claim boundary. If cancellation wins, a later redemption SHALL not preserve the cancelled bytes. After acquiring that boundary, cancellation SHALL report unavailable if redemption won or the upload cannot be proved unclaimed. It SHALL not interrupt a canonical commit or prevent an unsuccessful redemption from restoring its hold.

#### Scenario: Cancellation wins the claim boundary

- **WHEN** cancellation removes the owned hold before redemption claims it
- **THEN** redemption reports the handle unavailable and creates no artifact

#### Scenario: Redemption wins the claim boundary

- **WHEN** redemption claims the upload before cancellation can remove it
- **THEN** cancellation reports unavailable and leaves preservation to settle
- **AND** a failed preservation can restore its hold for a later explicit cancellation

### Requirement: Cancellation reports only confirmed effects

The operation SHALL return one ordered outcome per supplied file identifier. A cancelled outcome SHALL report the removed hold's verified quota charge. An unavailable or failed outcome SHALL not invent a released-byte count. Removal failures SHALL remain visible and retryable where the record survives. A repeated request SHALL never remove a canonical original or another upload.

#### Scenario: Temporary-byte removal fails

- **WHEN** storage prevents removal of the owned temporary payload
- **THEN** the result reports failure without claiming released capacity
- **AND** the hold record remains available for a later cancellation attempt

#### Scenario: The client loses the cancellation acknowledgement

- **WHEN** cancellation completes and its acknowledgement is lost
- **THEN** a retry removes nothing else and truthfully reports the remaining state

### Requirement: Cancellation stays bounded and keeps handles secret

Cancellation SHALL use direct lookup of its submitted handles, a bounded claim wait, and no unrelated hold or filing scan. A busy claim boundary SHALL return a retry hint. Cancellation SHALL not wait for the canonical writer or trigger indexing. Logs, durable receipts, and errors SHALL contain no raw handle, bearer, or upload URL.

#### Scenario: Other uploads keep the claim boundary busy

- **WHEN** another operation holds the upload claim boundary beyond the cancellation wait budget
- **THEN** cancellation returns a busy result with a retry hint
- **AND** it neither deletes bytes nor occupies a worker indefinitely
