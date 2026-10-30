## ADDED Requirements

### Requirement: Bound originals are written through to the cell's media store

On a cell selected for the media store, every original that a governed sidecar binds by `binary_sha256` SHALL be written to the cell's media store after the vault commit, whatever its type. The cell SHALL encrypt the original, upload it as a new version, confirm that version's size and digest, and only then record the stored-object reference, with its version ID, in the sidecar. Offload work SHALL run on the durable media job ledger. Before an offload uploads a version or reuses one, it SHALL record a protection record for the object name and commit it with a full sync, so that any later crash-consistent capture of the volume contains it. An offload SHALL reuse an existing version only when a live sidecar references that version at the moment the protection record commits. The protection record SHALL be retired only after the sidecar write that publishes the reference has itself been synced to disk. Notes, sidecars, extracted text, indexes and datasets SHALL NOT be offloaded. Offload SHALL apply the classification rule before it encrypts. An original that the rule calls resident SHALL get a recorded refusal, and offload SHALL write no temporary file for it.

#### Scenario: A recording is captured on a selected cell

- **WHEN** an owner captures a recording and its sidecar is committed
- **THEN** one offload job encrypts and uploads the recording
- **AND** the sidecar gains the stored-object reference only after the stored copy's size and digest are confirmed

#### Scenario: A resident original is never encrypted

- **WHEN** an offload job reaches a 1.5 GiB video, and the object bound is 1 GiB
- **THEN** the worker records the video as resident without encrypting it
- **AND** no temporary file for it appears on the volume, and the video stays local

#### Scenario: The store is unreachable

- **WHEN** the media edge is unreachable or the store is disabled for the cell
- **THEN** the original stays on the volume in the local state, its offload job stays queued and retryable, and nothing is evicted

#### Scenario: The same file is stored twice

- **WHEN** a second sidecar binds the same SHA-256 as a live sidecar that already references a stored version
- **THEN** both sidecars reference that one version

#### Scenario: The same file returns after deletion

- **WHEN** an original arrives whose bytes match a version that no live sidecar references
- **THEN** the cell uploads a new version and references it

### Requirement: One state function decides where an original is

The system SHALL decide an original's state through one function, from the presence of its file and the stored-object reference in its sidecar. The reference SHALL hold the store identity, key version, object name and version ID. The states are: `local` (present, no reference), `cached` (present, reference), `evicted` (absent, reference) or `missing` (absent, no reference). Reconciliation, the governance companion check, the deletion fan-out, media processing, export, download and frame reads SHALL use this function and MUST NOT treat an evicted original as deleted. A reference whose store identity is not the current cell's store SHALL count as no reference. No stub file SHALL be written at an evicted original's path.

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

When volume use passes the configured eviction target, the cell SHALL evict cached originals, largest first and longest idle on ties, until use falls below the target minus the configured margin. The cell SHALL check the target after each offload and each media job, and on a timer. The eviction target SHALL be lower than the online-growth trigger. While an import series records a demand, the cell SHALL also evict in the same order. It SHALL stop when the window budget covers the demand or no eligible original is left. A demand SHALL lapse 15 minutes after the sender last renewed it. An original SHALL be eligible only when it is cached, its hydration fits the hydration capacity, and nothing keeps it on the volume. A media job that a worker has claimed SHALL keep its original until the job completes, fails or is blocked. This SHALL hold also while a timeout slice, a memory brake or an engine wait returns the job to the queue as pending. A pin, a hydration or a download SHALL keep its original. A pending media job that no worker has claimed, a failed job and a blocked job SHALL NOT keep an original. An eligible original SHALL also be unread by any owner, or last read by an owner at least the configured minimum idle age ago, as the cell's own record shows. Only an owner's playback, download or viewing SHALL count as a read; offload, extraction, hydration for processing and export packing SHALL NOT. Before it evicts an original, or reuses a version for an offload, the cell SHALL confirm with a head request that the pinned version exists with the recorded size. A reference to an absent version SHALL count as no reference: a present original returns to `local` and is offloaded again, and an absent one is `missing` and is reported so.

#### Scenario: A media-heavy cell fills up

- **WHEN** volume use passes the eviction target
- **THEN** the largest eligible originals are removed from the volume until use is below the target minus the margin
- **AND** small recently read originals stay on the volume

#### Scenario: A series waits for room

- **WHEN** a 20 GiB cell has an eviction target of 0.7 and a margin of 0.1
- **AND** it has 11.5 GiB in use and a window budget of 0
- **AND** an import series records a demand of 4 GiB
- **THEN** eviction removes eligible originals, largest first, until the window budget covers 4 GiB
- **AND** without the demand, eviction would not run, because use is below the target

#### Scenario: A demand lapses

- **WHEN** the sender stops renewing its demand, and 15 minutes pass
- **THEN** eviction runs only above the everyday target again

#### Scenario: An original that could not come back stays

- **WHEN** a stored original's plaintext and ciphertext together exceed the hydration capacity, and volume use is above the target
- **THEN** eviction skips the original

#### Scenario: An original is still being processed

- **WHEN** an original has a claimed media job, a held pin or an active download
- **THEN** eviction skips it

#### Scenario: A recording waits for transcription

- **WHEN** a filed recording that no owner has read has a pending transcription job that no worker has claimed, and volume use is above the target
- **THEN** eviction removes the recording from the volume
- **AND** the worker hydrates it when it claims the job, and that hydration records no read

#### Scenario: A long transcription keeps its recording between slices

- **WHEN** a claimed transcription job returns to the queue at the end of a timeout slice, and volume use is above the target
- **THEN** eviction skips its recording until the job ends

#### Scenario: A failed job does not pin

- **WHEN** a recording that no owner has read has only a failed media job, and volume use is above the target
- **THEN** eviction treats the recording as eligible and removes it in size order

#### Scenario: An owner-read recording stays

- **WHEN** an owner played a recording less than the minimum idle age ago and volume use is above the target
- **THEN** eviction skips it until the minimum idle age has passed

#### Scenario: Eviction follows an offload

- **WHEN** an offload confirms a stored copy and volume use is above the target
- **THEN** eviction runs without a restart and removes the eligible originals

#### Scenario: A vault restore brings back a reference to a destroyed version

- **WHEN** an owner restores an exported vault into the same cell, and a restored sidecar pins a version that was destroyed after the export
- **THEN** the head check finds the version absent, the original returns to `local` and is offloaded again, and it is never evicted on the old reference

#### Scenario: The stored copy is unconfirmed

- **WHEN** an original's upload has not been confirmed
- **THEN** the original is not evicted, whatever the volume use

### Requirement: A media job defers when its original cannot be hydrated

When a worker claims a media job whose original is evicted, it SHALL hydrate the original first. A hydration can fail for a transport error, or because other hydrations hold the hydration capacity. Such a failure SHALL return the job to the queue for a later attempt. That return SHALL consume no attempt and SHALL keep the job's recorded speech progress. A worker SHALL NOT claim a returned job again before its retry time. The retry time SHALL start at 1 minute and double with each return, up to 15 minutes. An original whose hydration needs more than the whole hydration capacity SHALL block the job with `MEDIA_HYDRATION_TOO_LARGE`. A verification mismatch SHALL fail the job with `MEDIA_ORIGINAL_UNVERIFIED`.

#### Scenario: The edge is down when a job is claimed

- **WHEN** a worker claims a transcription job for an evicted recording and the media edge is unreachable
- **THEN** the job returns to the queue with its attempt count unchanged
- **AND** a later claim hydrates the recording and continues from the recorded speech progress

#### Scenario: Hydration capacity is in use

- **WHEN** a worker claims a job for an evicted original while other hydrations hold the whole hydration capacity
- **THEN** the job returns to the queue with its attempt count unchanged, and no failure is recorded

#### Scenario: A returned job waits its retry time

- **WHEN** a job returned to the queue 30 seconds ago for the first time, and a job whose original is on the volume waits
- **THEN** the worker claims the job whose original is on the volume
- **AND** it claims the returned job only after 1 minute has passed

#### Scenario: An original larger than the hydration capacity

- **WHEN** a worker claims a job for an evicted original whose plaintext and ciphertext together exceed the whole hydration capacity
- **THEN** the job is blocked with `MEDIA_HYDRATION_TOO_LARGE`, and the worker does not claim it again

### Requirement: The worker bounds the jobs that keep originals

After offload jobs, the worker SHALL keep at most `max_hydrations` claimed but unfinished media jobs. At that cap, it SHALL resume one of them before it claims a fresh job. Below the cap, it SHALL claim a job whose original is on the volume before a job whose original must be hydrated. Within each group, it SHALL claim a fresh job before a resumed one.

#### Scenario: Long recordings reach the cap

- **WHEN** `max_hydrations` is 2, two transcription jobs have each run one timeout slice, and fresh jobs wait
- **THEN** the worker resumes one of the two transcription jobs before it claims a fresh job

#### Scenario: Local originals run first

- **WHEN** the oldest fresh job's original is evicted, and a newer fresh job's original is on the volume
- **THEN** the worker claims the newer job first

### Requirement: Offload work precedes extraction

The media worker SHALL claim a pending offload job before any pending extraction job, so that a long extraction queue never delays the volume space that an offload frees.

#### Scenario: An offload waits behind queued transcription

- **WHEN** a filed PDF's offload job enters the queue after fifty pending transcription jobs
- **THEN** the worker claims the offload job before any of those transcription jobs

### Requirement: Adoption queues an offload for every offloadable artifact that it files

When adoption files an offloadable artifact on a cell selected for the media store, it SHALL queue the artifact's offload job at filing, whatever its type. The classification rule decides what is offloadable. A resident artifact SHALL get no offload job.

#### Scenario: A filed PDF offloads without a restart

- **WHEN** adoption files a PDF on a selected cell
- **THEN** the PDF's offload job exists when the apply call returns
- **AND** the worker stores the PDF without a restart or a discovery pass

#### Scenario: A resident video gets no offload job

- **WHEN** adoption files a 1.5 GiB video, and the object bound is 1 GiB
- **THEN** the video has no offload job, and it stays on the volume

### Requirement: Hydration verifies an original before it reappears

The cell SHALL hydrate an evicted original by fetching the version its reference names through the media edge, decrypting it to a temporary file, verifying size and SHA-256 against the sidecar, and renaming it into place. A mismatch SHALL fail closed with `MEDIA_ORIGINAL_UNVERIFIED`, leave the path empty and keep the evicted state. Hydration SHALL record the placed file's cache identity (inode, change time, size and verified SHA-256) in cell-local state. Reconciliation, the governance companion check and the file watcher SHALL treat a file that matches its cache identity as unchanged and SHALL NOT rewrite the sidecar's recorded provenance for it. Eviction and hydration SHALL NOT publish a deletion or creation to the deletion fan-out, and hydration SHALL NOT create a processing job or re-index the original. Hydration concurrency and temporary space SHALL be bounded.

#### Scenario: Playback of an evicted lecture

- **WHEN** an owner opens an evicted recording
- **THEN** the cell hydrates it, verifies the digest and serves it
- **AND** the original returns to the cached state with no new job and no sidecar change

#### Scenario: A hydrated file has a new change time

- **WHEN** reconciliation runs over a hydrated original whose inode and change time differ from those recorded at ingest but match its cache identity
- **THEN** reconciliation treats it as unchanged and does not rewrite the sidecar

#### Scenario: The stored object is corrupt

- **WHEN** a hydrated file's SHA-256 differs from the sidecar
- **THEN** the request fails with `MEDIA_ORIGINAL_UNVERIFIED`, the temporary file is removed and nothing appears at the original's path

### Requirement: Originals are encrypted in the cell under a per-cell media key bundle

The cell SHALL encrypt each original with `age` to its own X25519 recipient before upload.
Object names SHALL be HMAC-SHA-256 of the original's SHA-256 under a per-cell name key.
cellctl SHALL mint the media key bundle (identity and name key) per cell.
The bundle SHALL be wrapped with AES-GCM under that cell's root key.
The wrap SHALL bind cell ID, purpose and version as additional data.
The platform master key SHALL NOT wrap the media bundle directly.

In standard mode, cellctl SHALL store the wrapped bundle once on the cell row and render the bundle into the cell's Secret.
Tenant-locked storage and delivery SHALL follow `cloud-cell-key-custody`; the bundle SHALL NOT enter a Kubernetes Secret in that mode.
Only the cell runtime and the export packing step SHALL receive an unwrapped bundle.
No pod with public egress SHALL receive it.
A restore needs no media key, because evicted originals stay in the store.
The media edge, the gateway and the provider SHALL NOT receive the bundle.

#### Scenario: The stored bundle is inspected

- **WHEN** a cell's stored media key bundle is inspected
- **THEN** that cell's root key wraps it with the cell ID, purpose and version bound as additional data
- **AND** the platform master key does not wrap it directly

#### Scenario: A tenant-locked cell has media work

- **WHEN** a tenant-locked cell is locked and media work is requested
- **THEN** the work reports `locked` and waits for the tenant's unlock
- **AND** no Kubernetes Secret contains the media key bundle

#### Scenario: The bucket is listed

- **WHEN** anyone with bucket read access lists a cell's media prefix
- **THEN** object names are keyed digests that do not match the SHA-256 of any original, and object content is `age` ciphertext

#### Scenario: The edge's configuration is inspected

- **WHEN** the media edge's environment, mounts and Secrets are listed
- **THEN** none holds a media key bundle or a name key

#### Scenario: The export upload step is inspected

- **WHEN** the export's upload step, which has public egress, is rendered
- **THEN** it mounts no media key bundle

### Requirement: A media edge is the only path between cells and the media bucket

A media edge service SHALL move ciphertext between cells and the media bucket. It SHALL run as its own Deployment behind a Service with a literal ClusterIP and a Recreate rollout. It SHALL hold the only credential for the media bucket, and that credential SHALL NOT be able to delete file versions. The edge SHALL accept multipart upload with a declared size up to a per-object bound, ranged read of a named version, and head, and nothing else. It SHALL NOT hide, list or delete.

The edge SHALL authenticate a caller by a per-cell credential that cellctl signs, binding audience, cell ID and version, and SHALL verify the signature with cellctl's public key only. It SHALL accept a credential only while that cell ID and version are in the active set that cellctl publishes, and SHALL report a set generation only after it has loaded that set and every request it accepted under an earlier generation has finished. It SHALL derive the object prefix from the credential's cell ID and MUST NOT accept a prefix or cell from the request.

The edge SHALL persist provider write intent before dispatch and retain unresolved intents across restart.
It SHALL distinguish the loaded generation from the drained generation and SHALL NOT acknowledge a drain while an earlier write remains uncertain.
Timeout, cancellation or a missing object response SHALL NOT prove that a provider write cannot finish.
Uncertain uploads SHALL remain visible by cell without preventing unrelated requests under the loaded active set.

A cell selected for the media store SHALL gain one egress edge, to the media edge's ClusterIP and port, with no DNS. The cell's export packing step SHALL reach it on the same terms and have no other egress.

#### Scenario: A cell asks for another cell's object

- **WHEN** a request presents a valid credential for one cell and names an object of another cell
- **THEN** the edge resolves the name under the credential's own prefix and the other cell's object is never read

#### Scenario: A credential outlives its cell

- **WHEN** a captured credential of a deleted or rotated cell is presented with a valid signature
- **THEN** the edge refuses it, because that cell and version are no longer in the loaded active set

#### Scenario: The edge restarts after an upload reply is lost

- **WHEN** the edge restarts with an earlier provider write whose result is unknown
- **THEN** it retains that write's drain obligation and reports its pending cell
- **AND** it acknowledges the generation only after the write settles
- **AND** a missing HEAD does not release the obligation
- **AND** unrelated requests continue under the loaded active set

#### Scenario: A compromised cell tries to destroy history

- **WHEN** a caller asks the edge to delete a file version
- **THEN** no such operation exists, and the edge's provider credential is refused if used for it

#### Scenario: A selected cell reaches elsewhere

- **WHEN** a selected runtime connects to any destination other than the media edge or, if selected for it, the artifact broker
- **THEN** the network policy refuses the connection

### Requirement: No stored version that a retained backup references is destroyed

Media SHALL be stored in a dedicated bucket, separate from backup prefixes, that keeps versions, hides nothing and uses no object lock. A bucket lifecycle rule MAY cancel unfinished multipart uploads and SHALL NOT delete versions.

Each backup SHALL write a reference manifest for its snapshot, computed from the clone it backs up. The manifest SHALL list every version that the captured sidecars reference, trash included, and every object name that a protection record in the captured media job ledger names. It SHALL be stored in the cell's backup prefix, keyed by snapshot ID.

Only cellctl's key-management authority SHALL destroy versions. It SHALL destroy them only within a backup hold, after that hold's backup and prune succeed, and only a version that meets all of these:
- it appears in a listing of the cell's media prefix that cellctl completed before it took the capture of the newest retained snapshot;
- no retained snapshot's manifest names it or lists its object name under a protection record;
- every retained snapshot of the cell has a readable manifest.

No age or time cutoff SHALL decide destruction. cellctl SHALL delete the manifests of pruned snapshots.

#### Scenario: A stopped cell keeps an old backup

- **WHEN** a cell's retained snapshots include one taken months ago that references a version the vault no longer references
- **THEN** that version stays until that snapshot is pruned

#### Scenario: A sidecar is deleted while its clone is being backed up

- **WHEN** a clone captures a sidecar, the owner then deletes that sidecar, and restic backs up the clone afterwards
- **THEN** the snapshot's manifest names the version, and the version is not destroyed while that snapshot is retained

#### Scenario: An old backup is restored into the same cell

- **WHEN** a cell is restored from its oldest retained backup and later backups follow
- **THEN** every restored reference reads its pinned version, and no version that any retained manifest names is destroyed

#### Scenario: A snapshot has no manifest

- **WHEN** any retained snapshot of a cell lacks a readable manifest
- **THEN** cellctl destroys no version of that cell

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

Each cell SHALL have a media allowance, separate from its volume size, set as a configured value in the one limits source. The cell SHALL meter media storage from the versions its sidecars reference, counting each version once. Versions kept only for retained backups SHALL NOT count against the allowance. cellctl SHALL meter each prefix's physical bytes from the bucket listing for the operator. An upload or import whose declared media bytes exceed the remaining allowance SHALL be refused before any byte is accepted, with `MEDIA_ALLOWANCE_EXCEEDED` and the allowance shown. An original that the allowance cannot cover SHALL stay local, and status SHALL report it. The volume size cap SHALL NOT limit stored media. Status SHALL report two separately named figures: vault storage, which counts the volume's use without cached copies of stored originals, against the volume size; and media storage, which counts each stored original once, against the allowance. A figure that cannot be read SHALL be reported as unknown, never as zero.

The owner allowance SHALL count original plaintext bytes, including versions referenced by sidecars in trash until trash is emptied.
Multiple live or trash references to one version SHALL count once.
The operator's physical meter SHALL count provider ciphertext bytes separately.

The owner meter SHALL require complete canonical reference custody, including Trash, and SHALL report unknown when that custody is incomplete.
It SHALL exclude registry-owned private state without reusing recall exclusions.
A malformed potential companion or conflicting binding SHALL NOT prove that no referenced version exists.
Offload SHALL check the allowance before encryption or provider upload, and SHALL recheck its proposed reference union before publication.
A batch of offloads SHALL take one owner census, not one per member. It SHALL charge each version that it newly references to that census. The batch SHALL reuse its census across guard releases, and SHALL NOT hold the mutation boundary for the whole batch.
A quota failure or unknown meter SHALL leave the original local with retryable demand.

#### Scenario: A complete meter meets incomplete custody

- **WHEN** a canonical companion is malformed, an ordinary alias is unresolved, or a subtree cannot be read
- **THEN** owner usage is unknown and offload sends no bytes
- **AND** local originals and core operations remain available

#### Scenario: Trash retains the current version

- **WHEN** the owner trashes a stored binary
- **THEN** its canonical Trash companion and placement metadata are durable before its live reference retires
- **AND** the same provider version remains charged once until its last current reference disappears

#### Scenario: Recovery retires a Trash reference

- **WHEN** recovery restores a stored original as local bytes
- **THEN** it retires the Trash companion before removing its placement metadata
- **AND** a failed cleanup retains the placement evidence needed by the surviving reference


#### Scenario: An owner keeps media in trash

- **WHEN** live originals reference 60 GB and trash references another 20 GB of distinct original bytes
- **THEN** the owner meter reports 80 GB against the media allowance
- **AND** versions referenced only by retained backups do not increase that figure

#### Scenario: An upload exceeds the allowance

- **WHEN** an owner starts an upload whose declared size exceeds the remaining media allowance
- **THEN** the upload is refused with `MEDIA_ALLOWANCE_EXCEEDED` and the configured allowance before any byte is sent

#### Scenario: A batch of offloads takes one census

- **WHEN** the worker offloads a batch of 500 filed originals
- **THEN** it reads the owner's references once for the batch
- **AND** each member's admission and publication check use that census with the versions that the batch has already published
- **AND** other writes run between two members

#### Scenario: Offload meets a full allowance

- **WHEN** an original's offload would exceed the remaining allowance
- **THEN** the original stays local and status names it as not offloaded for lack of allowance

#### Scenario: An owner reads their storage

- **WHEN** a cell holds cached and evicted originals and the owner reads storage status
- **THEN** vault storage excludes the cached originals, media storage counts each stored original once, and each figure names its limit

#### Scenario: A meter cannot be read

- **WHEN** the cell cannot read its sidecar references, or its volume use, during a status call
- **THEN** the affected figure is reported as unknown, not as zero

### Requirement: Deleting originals and cells removes their stored objects

An original that the owner deletes SHALL leave the media store once the vault no longer references its version and every retained snapshot whose manifest names that version has been pruned, as the retention requirement allows.

When a cell row becomes `deleted`, cellctl SHALL retire its media content keys under `cloud-cell-key-custody`, independently of provider cleanup.
Live-copy absence and retention evidence SHALL continue to govern the key-destruction claim.
Cleanup MAY retain only the non-content credentials it needs.
Its provider steps SHALL:
1. remove the cell from the edge's active set;
2. observe that the edge has loaded that set;
3. abort every unfinished multipart upload under the cell's prefix;
4. delete every object version under the prefix.

cellctl SHALL report `deleted` only after the versions and bundle are absent and the existing cell deletion proof holds.
A failed listing or an unobserved edge generation MUST NOT count as done.

#### Scenario: The provider is unreachable during deletion

- **WHEN** a deleted cell's media provider is unreachable
- **THEN** content-key retirement proceeds independently and unknown live-copy removal remains pending
- **AND** provider cleanup retries without reporting the cell deleted or claiming an unsupported destruction date

#### Scenario: An owner deletes a recording

- **WHEN** an owner deletes a recording, empties trash, and the backups that referenced it are later pruned
- **THEN** its version is destroyed in the next backup hold after the last such snapshot is pruned

#### Scenario: An upload is confirmed but not yet referenced

- **WHEN** a backup captures the cell between a confirmed upload and the write of its reference into the sidecar
- **THEN** the captured ledger holds the synced protection record, the manifest lists the object name, and the version is not destroyed

#### Scenario: An offload reuses a version that another sidecar is about to drop

- **WHEN** an offload selects a version that a live sidecar references, that sidecar is deleted before the next capture, and the offload publishes its own reference after the capture
- **THEN** the captured protection record names the object, and the version is not destroyed

#### Scenario: A tenant deletes their account

- **WHEN** a cell row is set to `deleted`
- **THEN** the edge stops accepting the cell's credential before any version is deleted, and every version under its media prefix and its wrapped media key bundle are absent before the row reports `deleted`

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
