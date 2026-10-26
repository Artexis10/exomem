## ADDED Requirements

### Requirement: Bound originals are written through to the cell's media store

On a cell selected for the media store, every original that a governed sidecar binds by `binary_sha256` SHALL be written to the cell's media store after the vault commit, whatever its type or size. The cell SHALL encrypt the original, upload it, confirm the stored size and digest, and only then record the stored-object reference in the sidecar. Offload work SHALL run on the durable media job ledger. Identical originals in one cell SHALL share one stored object. Notes, sidecars, extracted text, indexes and datasets SHALL NOT be offloaded.

#### Scenario: A recording is captured on a selected cell

- **WHEN** an owner captures a recording and its sidecar is committed
- **THEN** one offload job encrypts and uploads the recording
- **AND** the sidecar gains the stored-object reference only after the stored copy's size and digest are confirmed

#### Scenario: The store is unreachable

- **WHEN** the media edge is unreachable or the store is disabled for the cell
- **THEN** the original stays on the volume in the local state, its offload job stays queued and retryable, and nothing is evicted

#### Scenario: The same file is stored twice

- **WHEN** two sidecars in one cell bind the same SHA-256
- **THEN** the cell stores one object and both sidecars reference it

### Requirement: One state function decides where an original is

The system SHALL decide an original's state through one function, from the presence of its file and the stored-object reference in its sidecar: `local` (present, no reference), `cached` (present, reference), `evicted` (absent, reference) or `missing` (absent, no reference). Reconciliation, the governance companion check, the deletion fan-out, media processing, export, download and frame reads SHALL use this function and MUST NOT treat an evicted original as deleted. A reference whose store identity is not the current cell's store SHALL count as no reference. No stub file SHALL be written at an evicted original's path.

#### Scenario: Reconciliation meets an evicted original

- **WHEN** an original is evicted and reconciliation, the governance check and the deletion fan-out run over its folder
- **THEN** its sidecar, extracted text, timestamps and every derived index row remain unchanged
- **AND** no processing job is created

#### Scenario: A sidecar arrives from another cell

- **WHEN** a restored or imported sidecar references a store that is not this cell's store and its original is present
- **THEN** the original is treated as local and is offloaded again to this cell's store

#### Scenario: An original is missing without a reference

- **WHEN** an original is absent and its sidecar holds no reference
- **THEN** the existing missing or deleted behaviour applies unchanged

### Requirement: Eviction frees volume space only from confirmed, idle originals

When volume use passes the configured eviction target, the cell SHALL evict cached originals, largest first and longest idle on ties, until use falls below the target minus the configured margin. The eviction target SHALL be lower than the online-growth trigger. An original SHALL be eligible only when it is cached, idle for at least the configured minimum idle age by the cell's own last-read record, and has no pending or running media job, hydration or download.

#### Scenario: A media-heavy cell fills up

- **WHEN** volume use passes the eviction target
- **THEN** the largest eligible originals are removed from the volume until use is below the target minus the margin
- **AND** small recently read originals stay on the volume

#### Scenario: An original is still being processed

- **WHEN** an original has a pending media job or an active download
- **THEN** eviction skips it

#### Scenario: The stored copy is unconfirmed

- **WHEN** an original's upload has not been confirmed
- **THEN** the original is not evicted, whatever the volume use

### Requirement: Hydration verifies an original before it reappears

The cell SHALL hydrate an evicted original by fetching its stored object through the media edge, decrypting it to a temporary file, verifying size and SHA-256 against the sidecar, and renaming it into place with the recorded modification time. A mismatch SHALL fail closed with `MEDIA_ORIGINAL_UNVERIFIED`, leave the path empty and keep the evicted state. Hydration SHALL NOT create a processing job, rewrite the sidecar or re-index the original. Hydration concurrency and temporary space SHALL be bounded.

#### Scenario: Playback of an evicted lecture

- **WHEN** an owner opens an evicted recording
- **THEN** the cell hydrates it, verifies the digest and serves it
- **AND** the original returns to the cached state with no new job or sidecar change

#### Scenario: The stored object is corrupt

- **WHEN** a hydrated file's SHA-256 differs from the sidecar
- **THEN** the request fails with `MEDIA_ORIGINAL_UNVERIFIED`, the temporary file is removed and nothing appears at the original's path

### Requirement: Originals are encrypted in the cell under a per-cell media key bundle

The cell SHALL encrypt each original with `age` to its own X25519 recipient before upload, and SHALL name each stored object with HMAC-SHA-256 of the original's SHA-256 under a per-cell name key. cellctl SHALL mint the media key bundle (identity and name key) per cell. It SHALL store the bundle once on the cell row, wrapped with AES-GCM under the same master key that wraps the backup key, with cell ID, purpose and version as additional data. It SHALL render the bundle into the cell's Secret. Only the cell runtime and the cell's export and restore Jobs SHALL mount the bundle. The media edge, the gateway and the provider SHALL NOT receive it.

#### Scenario: The bucket is listed

- **WHEN** anyone with bucket read access lists a cell's media prefix
- **THEN** object names are keyed digests that do not match the SHA-256 of any original, and object content is `age` ciphertext

#### Scenario: The edge's configuration is inspected

- **WHEN** the media edge's environment, mounts and Secrets are listed
- **THEN** none holds a media key bundle or a name key

### Requirement: A media edge is the only path between cells and the media bucket

A media edge service SHALL move ciphertext between cells and the media bucket. It SHALL run as its own Deployment behind a Service with a literal ClusterIP and a Recreate rollout. It SHALL hold the only credential for the media bucket, and that credential SHALL NOT be able to delete file versions. The edge SHALL accept multipart upload with a declared size, ranged read, head and hide, and nothing else. It SHALL authenticate a caller by a per-cell credential that cellctl signs, binding audience, cell ID and version, and SHALL verify it with cellctl's public key only. It SHALL derive the object prefix from the credential's cell ID and MUST NOT accept a prefix or cell from the request. A cell selected for the media store SHALL gain one egress edge, to the media edge's ClusterIP and port, with no DNS. The cell's export and restore Jobs SHALL reach it on the same terms.

#### Scenario: A cell asks for another cell's object

- **WHEN** a request presents a valid credential for one cell and names an object of another cell
- **THEN** the edge resolves the name under the credential's own prefix and the other cell's object is never read

#### Scenario: A compromised cell tries to destroy history

- **WHEN** a caller asks the edge to delete a file version
- **THEN** no such operation exists, and the edge's provider credential is refused if used for it

#### Scenario: A selected cell reaches elsewhere

- **WHEN** a selected runtime connects to any destination other than the media edge or, if selected for it, the artifact broker
- **THEN** the network policy refuses the connection

### Requirement: The media bucket keeps hidden versions longer than backups

Media SHALL be stored in a dedicated bucket, separate from backup prefixes. The bucket SHALL keep versions, and a lifecycle rule SHALL delete hidden versions no sooner than 35 days after hiding, which exceeds the 4-weekly backup retention. The bucket SHALL NOT use object lock.

#### Scenario: The oldest backup is restored

- **WHEN** a cell is restored from its oldest retained backup
- **THEN** every stored-object reference in the restored sidecars still resolves to a readable version

#### Scenario: An original is hidden by mistake

- **WHEN** an object is hidden and the error is found within 35 days
- **THEN** its version is still present and can be restored with key-management authority

### Requirement: Owners retrieve originals only with their own authority

The gateway SHALL serve a download route for originals that works only with the owner's signed-in session. The route SHALL forward to the cell, which SHALL hydrate an evicted original if needed and stream it with byte-range support. A tool result MAY carry this route's link, because the link grants nothing without the session. A tool result SHALL NOT carry a bearer link, a presigned URL or ciphertext metadata. `exomem cloud download` SHALL fetch an original over OAuth, stream it to a temporary file, verify size and SHA-256 against the record, and rename it into place only after both match; any failure SHALL delete the partial file.

#### Scenario: A link is opened without a session

- **WHEN** someone without the owner's signed-in session opens a download link
- **THEN** the gateway refuses it and the cell is not contacted

#### Scenario: A browser seeks in a recording

- **WHEN** a signed-in owner plays an evicted recording and seeks to a later timestamp
- **THEN** the cell answers the byte-range request after hydration

#### Scenario: A local agent downloads an original

- **WHEN** an agent runs `exomem cloud download` for an original
- **THEN** the file appears only after its size and SHA-256 match the record

### Requirement: The media allowance is checked before bytes arrive

Each cell SHALL have a media allowance, separate from its volume size, set as a configured value in the one limits source. cellctl SHALL meter stored bytes from the bucket listing. An upload or import whose declared media bytes exceed the remaining allowance SHALL be refused before any byte is accepted, with `MEDIA_ALLOWANCE_EXCEEDED` and the allowance shown. An original that the allowance cannot cover SHALL stay local, and status SHALL report it. The volume size cap SHALL NOT limit stored media.

#### Scenario: An upload exceeds the allowance

- **WHEN** an owner starts an upload whose declared size exceeds the remaining media allowance
- **THEN** the upload is refused with `MEDIA_ALLOWANCE_EXCEEDED` and the configured allowance before any byte is sent

#### Scenario: Offload meets a full allowance

- **WHEN** an original's offload would exceed the remaining allowance
- **THEN** the original stays local and status names it as not offloaded for lack of allowance

### Requirement: Deleting originals and cells removes their stored objects

When the last sidecar that references a stored object leaves the vault, including when trash is emptied, the cell SHALL hide that object through the media edge. An orphan sweep SHALL hide objects that no sidecar has referenced for a configured grace period. When a cell row becomes `deleted`, cellctl SHALL delete every object version under the cell's media prefix and destroy the wrapped media key bundle. It SHALL report `deleted` only after it observes that both are absent. A failed listing MUST NOT count as absence.

#### Scenario: An owner deletes a recording

- **WHEN** an owner deletes a recording and empties trash
- **THEN** its stored object is hidden, and the lifecycle rule deletes it after retention

#### Scenario: A tenant deletes their account

- **WHEN** a cell row is set to `deleted`
- **THEN** every version under its media prefix and its wrapped media key bundle are absent before the row reports `deleted`

#### Scenario: The version listing fails during deletion

- **WHEN** the media bucket's version listing fails during cell deletion
- **THEN** the row stays `deleting` and the check is retried

### Requirement: The media store is off by default and drains on disable

The media store SHALL be off unless a cell is on the canary list or the fleet switch is on. A cell that is not selected SHALL have no media edge egress, no offload and no eviction. Turning the store off for a selected cell SHALL stop new offload and eviction at once. It SHALL keep hydration available until no original of that cell is evicted, and only then remove the cell's edge egress. If the volume cannot hold every evicted original within its size cap, the cell SHALL keep its edge egress and report the shortfall in bytes.

#### Scenario: Default installation

- **WHEN** the chart is installed without a canary list or fleet switch
- **THEN** no cell has edge egress and every original stays on its cell's volume

#### Scenario: Rollback with evicted originals

- **WHEN** the store is turned off for a cell that has evicted originals
- **THEN** the cell hydrates every evicted original back, keeps its edge egress until none remain, and then loses that egress
