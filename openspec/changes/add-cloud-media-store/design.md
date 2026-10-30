## Context

A Cloud cell is one pod with one block volume. The volume holds the vault (notes, originals and their sidecars) and the custody home (indexes and state). Backups are restic snapshots of both, in a per-cell prefix of a private B2 bucket.

Media enters through capture, client attachments, imports and uploads. Each original gets a sidecar `<name>.md` that records its SHA-256, size and timestamps. Extraction (ASR, OCR, CLIP) runs once in the cell's media worker and writes text into that sidecar and vectors into the indexes. After that:

- Search and recall read only sidecar text and the indexes.
- These need the original bytes again: playback, viewing, download, re-processing, video frames, CLIP re-encoding after a model change, and export.

Costs per gigabyte-month, with sources:

| Medium | Price | Source |
|---|---|---|
| Network volume (current cells) | EUR 0.044–0.057 | provider documentation; `move-cloud-cells-to-local-storage` proposal |
| Node-local NVMe (planned cells) | about EUR 0.14 before snapshot reserve | smallest node in `docs/cloud-hardware-options.md`, about 476 GiB usable |
| Backblaze B2 | USD 0.00695 (USD 6.95/TB) | provider pricing page, read 2026-10-26 |

The planned node-local storage reserves twice the largest cell per concurrent backup on each node (D6), caps a cell at 20 GiB, and budgets about 16 minutes to restore a 10 GiB cell (D4). Media-heavy cells break all three numbers.

## Goals / Non-Goals

**Goals**
- Keep cells small enough for node-local storage while owners keep tens to hundreds of gigabytes of originals.
- Keep every original byte-identical and verifiable against its sidecar SHA-256.
- Keep extraction in the cell, and keep the media key out of every pod that has public egress. The export archive remains the export contract's own exception (D8).
- Give owners playback and download of what they stored, from every client.
- State truthfully who can read stored originals.

**Non-Goals**
- Defining another key hierarchy or unlock channel. `cloud-cell-key-custody` owns those rules (D5).
- A second copy at another provider (D7).
- Connector sync from third-party drives.
- Moving notes, sidecars, indexes or datasets off the volume.
- A server-side model. Speech engine selection stays in `add-cloud-multimodal-processing`.
- The local (non-Cloud) product. A local vault keeps originals on its own disk; the filesystem store exists for tests.

## Decisions

### D1. Every bound original is written through; the volume is a cache

Every original that a governed sidecar binds by `binary_sha256` is written through to the cell's media store after the vault commit. The rule is the same for audio, video, images, PDFs, documents and every other preserved type. It uses no type or size list.

The vault path on the volume stays the original's home and acts as a verified cache. Notes, sidecars, extracted text, indexes, structured collections and other datasets stay on the volume and are never offloaded.

Alternatives:
- **Offload only audio and video.** This is simple to explain, but scan-heavy vaults stay large, and a type list must be maintained.
- **Offload only above a size threshold.** Two durability models would exist for the same kind of file.

Write-through keeps one rule and lets eviction (D3) decide what leaves the volume.

### D2. The sidecar holds the stored-object reference; four states

A sidecar gains one governed descriptor key: the stored-object reference. It holds the store identity, the key version, the object name and the provider's version ID. A reference therefore pins exact bytes: a later upload under the same name never changes what it reads. An original is in exactly one state:

| On volume | Reference | State | Meaning |
|---|---|---|---|
| yes | no | local | offload pending, or the store is disabled |
| yes | yes | cached | durable in the store and present on the volume |
| no | yes | evicted | durable in the store only; **not** a deletion |
| no | no | missing | deleted or lost, as today |

One function, `original_state`, decides the state. The reconciler, the governance companion check, the deletion fan-out, media processing, export, download and frame reads all call it. None of them tests file existence directly.

A reference whose store identity is not this cell's store counts as no reference, for example after a restore into another cell. That original is then offloaded again.

No stub file sits at an evicted path, because a stub would be transcribed, hashed or exported as if it were the original. A missing file fails loudly.

Alternative: **references in cell-local custody state.** If that state were lost or disagreed with the sidecar, every evicted original would look deleted. The sidecar is already the canonical record of the original, so the reference lives there.

### D3. Eviction: largest and longest idle first, only after confirmation

When volume use passes the eviction target, the cell evicts cached originals until use falls below the target minus a margin. The target is a configured fraction, set below the online-growth trigger (80%), so a media-heavy cell evicts before it grows.

- **Candidates:** originals in the cached state, idle for at least the minimum idle age, with no pending or running media job, hydration or download, and whose pinned version a head request confirms exists with the recorded size. A reference to an absent version counts as no reference, and the original is offloaded again.
- **Order:** largest first within the eligible set, ties broken by longest idle. This frees the space with the fewest evictions.
- **Idle time:** the cell's own last-read record, not file access time.

Small originals such as scans, photos and documents are expected to stay cached. This is an expectation that canary data checks, not a guarantee under all pressure.

**Hydration** brings an evicted original back. The cell fetches the ciphertext through the edge, decrypts it to a temporary file, verifies size and SHA-256 against the sidecar and renames it into place. A mismatch fails closed and leaves the path empty.

A replaced file gets a new inode and change time, and restoring the modification time cannot restore the change time. So hydration records a *cache identity* for the file in cell-local state: its inode, change time, size and verified SHA-256. Reconciliation, the governance companion check and the file watcher treat a file that matches its cache identity as unchanged. None of them rewrites the sidecar's recorded provenance, which keeps describing the original as it was ingested. Eviction and hydration are managed operations, so neither publishes a deletion or creation to the deletion fan-out.

Hydration concurrency and temporary space are bounded, so a bulk re-encode or an export cannot fill the volume.

Pre-canary repairs keep the existing ledger and mutation boundaries.
An exact-input verification refusal parks discovery until its digest changes or the owner explicitly retries it.
The refusal records a nullable ciphertext size, because verification can fail before encryption.
Size refusals compare their measured ciphertext with the current object bound.

Status reads a bounded candidate prefix from the cache and refusal tables, then resolves canonical sidecars.
Its overview always reports partial coverage and an unknown total; exact-path status does not require a candidate row.
Missing custody reports unavailable without rebuilding it.
Candidate failures remain path-bearing rows, and no sibling count or truncation flag reveals withheld candidates.
Exhaustive browsing would need a separate interface decision.

Ordinary pins insert without scanning existing pins.
Hydration accounts only for indexed hydration reservations; eviction cleans only indexed pins for its target.
Dead idle pins can remain until that path is considered, without spending hydration capacity.
Page moves prepare the catalog and validate bindings before hydration, while retaining pins through rename, rollback and publication.
Hydration classifies only backend open and read errors as transport failures; local staging errors retain their cause.

**Amendment, 2026-10-30 (task 5.4): eviction runs during an import.**

An import larger than the volume needs room while it runs. Two rules change, and the text above stays as the first design.

- **M3, when eviction runs.** Eviction runs after each offload and each media job when use is above the target, and on a timer. It no longer waits for the worker's start-up scan. It takes candidates from an indexed query on the cache table, which gains a size column. It does not read every sidecar.
- **M4, what counts as use.** Idle time counts owner reads only: playback, download and viewing. Offload, extraction, hydration for processing and export packing record no read. An original that no owner has read is eligible whatever the minimum idle age. A pending job that no worker has claimed no longer keeps an original on the volume. A running job, a pin or a download still does. The worker hydrates an evicted original when it claims the job.
- **Scope (pending operator confirmation).** Both rules apply to every original, not only to imported ones. Eviction still starts only above the target. A captured recording that the owner has not played can leave the volume before its transcript exists, and the worker then hydrates it once. A rule for imported originals only would keep captures unchanged. It needs an origin flag on each cache record.

### D4. Encryption in the cell

The cell encrypts each original with `age` to its own X25519 recipient before upload. `age` streams 64 KiB authenticated chunks, and the cell image already ships it for imports.

The object name is HMAC-SHA-256 of the original's SHA-256 under a per-cell name key. A bucket listing then cannot match a stored object to a known file by name. Object sizes stay visible, because `age` ciphertext size follows plaintext size, so a file of distinctive length can be confirmed by size. An offload reuses a stored version only when a live sidecar already references that version. Otherwise it uploads a new version, so identical originals inside one cell usually share one version.

On the media-storage path, the media edge and the provider see only ciphertext. Playback and export are other paths, and D8 and the table below cover them.

### D5. Key custody follows the cell's key hierarchy

cellctl mints a per-cell media key bundle: the `age` identity and the name key.
It wraps the bundle with AES-GCM under that cell's root key, binding the cell ID, purpose and version as additional data.
The platform master key does not wrap the media bundle directly.

In standard mode, cellctl stores the wrapped bundle on the cell row and renders the bundle into the cell's Secret.
Tenant-locked storage and delivery follow `cloud-cell-key-custody`; the bundle never enters a Kubernetes Secret in that mode.
Only the runtime and the export packing step (D8) receive an unwrapped bundle, and neither has public egress.
A restore needs no media key, because evicted originals stay in the store.
The edge never receives the bundle.

The media bundle shares the backup and volume keys' cell root and has no separate custody or recovery path.
The platform can unwrap that root in standard mode.
In tenant-locked mode, the tenant holds the root; media work reports `locked` until the tenant unlocks it.
This contract does not imply that tenant lock or its delivery channel is implemented.

### Who can read an original

| Where the original is | Who can read the plaintext |
|---|---|
| In the store, any version | Nobody without the media key. The provider and the edge hold only ciphertext and keyed names. |
| The media key | Whoever can unwrap the cell root: the platform in standard mode, or the tenant in tenant-locked mode. The tenant-held claim applies only after `lock_complete_at`. |
| In the cell during processing, hydration or playback | The cell runtime. An operator with live node access can deliberately read unlocked content; the access record does not prevent that attack. |
| On its way to the owner | The public TLS edge and the gateway, which see requests and responses in transit, as the privacy copy already discloses. |
| In an export | The packing step decrypts without public egress (D8). The archive follows `hosted-vault-portability`: a plain ZIP over the owner's authorized export path. Key custody does not change that delivery contract. |

The privacy copy states this custody truthfully (`cloud-operator-access`).

### D6. A media edge holds the only bucket credential

A new in-cluster service, the media edge, moves ciphertext between cells and the media bucket. It follows the artifact broker's shape: its own Deployment and Service on a literal ClusterIP, Recreate rollout, and a canary list plus a fleet switch.

- **Network.** A selected cell's runtime gains one egress edge, to the edge's ClusterIP and port, with no DNS. The export packing step (D8) reaches it on the same terms and has no other egress.
- **Authentication.** cells carry no ServiceAccount token. cellctl signs a per-cell edge credential with its own signing key, binding audience, cell ID and version, and renders it into the cell's Secret. The edge holds only the public key. It maps the credential's cell ID to that cell's object prefix and accepts no prefix from the request.
- **Revocation.** A valid signature is not enough. cellctl publishes the active set: each selected cell's ID and current credential version. The edge accepts a credential only while its cell and version are in that set. Rotation bumps the version, and the old credential stops working once the edge loads the new set. The edge reports a set generation only after it has loaded that set and drained every request accepted under an earlier one, so cellctl can observe when a revocation takes effect.
- **Operations.** Multipart upload with a declared size up to a per-object bound, ranged read of a named version, and head. The edge cannot hide, list or delete. Its bucket key cannot delete file versions. Only cellctl's key-management authority destroys versions (D9).
- **Quota.** The cell admits uploads against its allowance (D10). cellctl meters each prefix's physical bytes from the bucket listing and alerts when a cell exceeds its allowance plus retained-version headroom. The alert detects abuse; it does not prevent it. Until the operator revokes it, an active cell credential can upload unreferenced objects below the per-object bound. Each such object costs storage, and the operator carries that exposure.

Refinements from the build (2026-10-27):
- **Prefix.** The provider name is `cells/<cell>/<name>`, with the cell ID taken from the credential and checked against the canonical cell ID shape. A prefix in a path, header or query is ignored. A version ID of another cell reads as absent, because the edge compares the provider's file name with the cell's own name.
- **Admit preflight.** Before it sends any byte, the cell store posts the declared size and digest to an `admit` action. The edge answers 413 above the per-object bound, or 409 while an earlier write to the same name is unsettled, so a refusal costs no upload.
- **Settled generation.** The reported generation is the lowest of the loaded set, every request still in flight and every write intent that is still unsettled. The edge records an intent durably before it sends a provider write. A lost reply, an absent HEAD or a restart therefore keeps the generation open until the provider shows the write or the file ID is gone.
- **Large files.** A large upload is found again by its file ID: a finished file is the version, an unfinished one is finished or cancelled, and a missing one never became a version. A small upload is found again by a nonce that it carries in its file info.

Alternatives:
- **Projected ServiceAccount tokens.** These would break the cell's no-token rule.
- **A forwarding proxy to the provider.** Its endpoint is shared by all of the provider's customers, so it would be an exfiltration channel.
- **Folding this into the artifact broker.** The broker fetches arbitrary public URLs; storage credentials do not belong behind that surface.

Decision, 2026-10-26: build a narrow custom edge. Neither gateway can enforce the two controls that decide the choice:
- a per-object bound on the declared upload size, refused before the store accepts any byte;
- a set generation that the edge reports only after every request accepted under an earlier set has drained, so cellctl can observe when a revocation takes effect.

rclone has two further gaps. A delete through it becomes a provider hide, which the edge must refuse, and it cannot read a named version.

The 1 GB in-cluster hydration measurement is pending a live run. Task 2.1 stays open until design.md records it; this decision claims no live transfer performance.

#### Source comparison, 2026-10-26

This comparison covers versitygw 1.10.0 and rclone 1.75.2. Neither dependency is added to the build.

| Option | Reusable capability | Remaining work |
|---|---|---|
| versitygw 1.10.0 | Its S3 proxy forwards an exact version ID and byte range to the provider. Since 1.8 it issues STS credentials with role policies. | It cannot bound a declared upload size or report a drained generation. Its web-identity verifier does not accept Ed25519 tokens, which the artifact broker's credential shape uses. |
| rclone 1.75.2 | Its authentication proxy can select a caller-specific backend root. | The same two gaps, plus hide-on-delete and no version reads: its S3 server does not support versioning and returns no version ID after multipart completion. |
| A narrow custom edge | Reuse maintained HTTP, JWT and provider clients. | Maintain the media protocol, provider adapter, bounded upload sessions and generation accounting. Avoid a general S3 server or custom cryptography. |

The source evidence is the tagged [versitygw version/range forwarding](https://github.com/versity/versitygw/blob/v1.10.0/backend/s3proxy/s3.go#L1085-L1206),
[rclone authentication proxy](https://github.com/rclone/rclone/blob/v1.75.2/cmd/serve/proxy/proxy.go#L88-L154),
[rclone versioning limitation](https://github.com/rclone/rclone/blob/v1.75.2/cmd/serve/s3/serve_s3.md#L449)
and [multipart completion](https://github.com/rclone/rclone/blob/v1.75.2/cmd/serve/s3/multipart.go#L866-L907).

The provider's `writeFiles` capability also permits hiding an object. A key without `deleteFiles` prevents permanent version deletion, but the edge must still filter operations.
See [B2 application-key capabilities](https://www.backblaze.com/docs/cloud-storage-application-key-capabilities).

The custom edge reuses the existing HTTP and JWT dependencies and the controller's native B2 integration pattern.
Both gateways still need the same credential, prefix, size and recovery controls; their wider S3 surface adds no required capability.
The added maintenance is a small media protocol, provider adapter and durable upload records.
Task 2.1 remains incomplete until the 1 GB cluster hydration measurement is recorded.

#### Revocation across an edge restart

The edge records provider write intent with a full sync before dispatch and retains unresolved records on persistent storage across replacement.
These records track admitted uploads, not another offload queue.
The edge distinguishes the loaded active-set generation from the generation whose earlier requests and writes have settled.
A timeout, disconnect, process exit or absent HEAD cannot turn an uncertain write into a completed drain.
Unrelated requests continue under the loaded active set; pending records are visible by cell and do not fail fleet readiness.
The existing global drain acknowledgement still waits for every earlier unresolved write.

For native multipart upload, the edge records the provider file ID and ordered part hashes before finishing the file.
It recovers a lost completion reply by inspecting that exact file ID.
The provider documents [successful completion after a timeout](https://www.backblaze.com/apidocs/b2-finish-large-file).

Small ciphertext uses one upload attempt with a unique opaque nonce in provider metadata.
The edge records its intent first, sends it once, and serializes writes to that object name until the result settles.
A HEAD that matches the nonce, size and digest supplies the completed version ID.
Missing or different metadata leaves the attempt unknown; it does not authorize automatic retransmission.
See the provider's [upload metadata](https://www.backblaze.com/apidocs/b2-upload-file) and [HEAD response](https://www.backblaze.com/apidocs/b2-download-file-by-name).

Native multipart requires [at least two parts, with a 5 MB minimum except for the final part](https://www.backblaze.com/docs/cloud-storage-large-files).
It therefore does not cover small ciphertext without changing its format.
No documented small-write fence proves that an unseen attempt will never complete.
A crash before dispatch can consequently leave an unknown record indefinitely and delay global drain acknowledgement.
This prevents a false deletion proof, at the cost of a pending cleanup for the affected operator.
The smallest provider check drops one successful upload reply, restarts the edge, and proves recovery by nonce and exact version.
It proves positive recovery, not a finite negative fence; neither new padding nor a storage-format change is selected.

### D7. A separate, versioned bucket on the backup provider

Media lives in its own B2 bucket. It is not under the backup prefixes, so the edge's bucket-wide key cannot reach restic packs.

The bucket keeps versions, and nothing is hidden. A version is destroyed only when no retained backup snapshot and no live capture can still reference it (D9). No age or time cutoff decides this. Two facts rule out a cutoff:
- restic keeps the latest snapshot of each of the last 4 weeks that *have* snapshots, so a stopped cell can keep a months-old snapshot.
- A snapshot's restic timestamp follows its clone's capture time.

Every restorable backup therefore still resolves its references. The only lifecycle rule cancels unfinished multipart uploads. No object lock is set, so cell deletion can complete.

The same provider holds backups and media. A second copy at another provider was considered and rejected on cost for now. The revisit trigger is any of:
- the fleet's stored media passes 1 TB;
- a provider incident;
- a paying tenant asks for it.

### D8. Data paths

- **Arrival.** Files arrive as today: capture, attachments, imports, uploads. After the vault commit, an offload job on the existing durable media job ledger encrypts and uploads the original, confirms the new version's size and digest with a head, then writes the reference, version ID included, into the sidecar.
- **Live capture.** The gateway proxies resumable (tus) uploads to the cell's existing upload sessions. This lifts the Cloud exclusion of those routes by its recorded condition.

- **Processing.** Extraction runs at ingest while the original is local. Eviction never selects an original with pending work. Re-processing and CLIP re-encoding hydrate first.
- **Playback and download.** The gateway serves a download route behind the owner's signed-in session. It forwards to the cell, which hydrates if needed and streams the original with range support. A tool result may carry this link, because it grants nothing without the owner's session. Local agents use `exomem cloud download` over OAuth, which verifies size and SHA-256 before it renames the file into place. No tool result carries a bearer link.
  - *Agent download.* The agent's route is `GET /download?path=<vault path>` on the gateway with the owner's OAuth access token, and it needs no ticket. The gateway validates the token, resolves the owner's cell, and signs a fresh gateway origin proof for each request, range requests included. The proof carries `purpose: "download"` and `target_sha256`, the SHA-256 of the requested path. The cell route checks the cell bearer, then the proof, and refuses with `DOWNLOAD_UNAVAILABLE` before it resolves anything. It then serves through the same artifact recovery as a local recovery, with its governance, custody page, size and SHA-256 checks. `transfer_artifact` download on Cloud returns the descriptor and a `next_step` command (`exomem cloud download '<path>'`), never a URL.
  - *Browser link.* The signed-in browser link stays open as task 5.1.
- **Export.** Export runs in two steps, so the media key never shares a pod with public egress:
  - A *packing* step mounts the media key. It reaches only the media edge and builds the archive on the export staging volume, writing evicted originals byte-identical. Each manifest SHA-256 must equal the sidecar's.
  - A separate *upload* step without the media key transfers the archive as today's export does.

  The archive's format and delivery stay those of the export contract. Export of more media than the staging volume can hold is an open question; until it is settled, no cell other than the canary may store more media than its export can stage (D11).
- **Restore from backup.** restic restores sidecars and any cached originals. Evicted originals stay in the store, because no version that a retained snapshot references is destroyed (D9). A restored reference reads its pinned version, so a restore needs no reactivation step.
- **Restore from an export.** A vault restored from an export can carry references to versions destroyed since the export. So the cell confirms with a head request that a pinned version exists before it evicts an original or reuses the version. A reference to an absent version counts as no reference, and the original is offloaded again (D3).

The existing upload service carries the selected lane. Source sessions use the canonical Source writer and its existing mutation replay protocol.
Their immutable session identity separates transport retries from caller-authored Source identity.
The Source writer shares metadata validation with byte admission; the session passes its staged file directly to publication.
Unknown canonical outcomes retain the upload without allocating another Source pair.

The receipt reports semantic compilation as unassessed; extraction does not establish a compiled conclusion.
Uploaders reconcile an uncertain part against its accepted offset before repeating writes, and reduce subsequent part sizes after interruption.
The gateway route and its acceptance remain task 5.3; local Source support does not prove Cloud delivery.
Activated vocabulary image handling remains a resource and compatibility gap for large binaries until task 5.3.3 proves that boundary.

The job ledger is the job architecture. A Postgres-backed task queue such as Procrastinate was considered and does not fit: a cell runs on SQLite and holds no Postgres.

**Amendment, 2026-10-30 (task 5.4): data paths for an import larger than the volume.**

The shape in D12 is pending operator confirmation. It adds windows, a stage reference and filing by reference, and it changes one rule above.

- **Arrival in windows.** A sender splits a folder larger than the cell's window budget into ordinary `adopt` imports, called windows. Each window has its own import ID, recipient and checkpoint. C5, cellctl and the import Job do not change.
- **Landing.** The import Job has public egress, so it never holds the media key. Stage offload therefore runs in the runtime, after the Job and its hold end. For each store-bound member that is present and has no stage reference, the runtime does these steps:
  1. It checks the staged file against its receipt entry (size and SHA-256).
  2. It reuses a live version of the same digest. Otherwise it commits a protection record with a full sync, encrypts, uploads through the edge and confirms with a head request.
  3. It writes the stage reference with a full sync, then retires the protection record.
  4. It removes the staged file under a held handle, with an identity check.

  Text, members above the object bound and members that changed stay on the volume. A store outage leaves them there with a retryable job.
- **Stage reference.** A stage reference holds the import ID, member path, version, size and SHA-256. It lives in the media job ledger beside the protection records. Backups already capture that ledger, and each write costs one row. D2 rejects references in cell-local state, because losing that state would make every evicted original look deleted. That reason does not carry over. A stage reference lasts only until filing, and the person still holds the original, because both importers copy. If the state is lost, the unfiled stored members of that stage become unavailable and the person imports them again. After filing, the sidecar holds the reference, as D2 says. The discard Job holds no media key and does not touch the ledger. The runtime releases a stage reference at its next start, once no import receipt lists the member.
- **Filing by reference.** Adoption files a stored member without reading it. The Source gets `evidence_file`, `binary_sha256`, `binary_size` and a stored-object reference to the stage version, so the original starts `evicted`. Adoption releases the stage reference after the sidecar is synced, which is the order of D9. A member that is still on the volume files by copy.
- **Processing.** Extraction of a member filed by reference queues without reading the original. The worker hydrates it when it claims the job. The rule above that eviction never selects an original with pending work gives way to M4 in D3.
- **Retention.** Reference manifests list the versions that captured stage references name (D9).

### D9. Deletion

Collection is new lifecycle logic. No existing sweep supplies it: the archive member pool's sweep removes only abandoned temporary files. Collection works by reachability, with no hide step:

- **Reference manifests.** Each backup computes a reference manifest from the clone it backs up. The manifest lists every version that the captured sidecars reference, trash included, and every object name that a protection record in the captured media job ledger names. It is stored beside the snapshot in the cell's backup prefix, keyed by snapshot ID.
- **Protection records.** A crash-consistent clone may omit the newest commits of a ledger that syncs lazily (WAL with `synchronous=NORMAL`). So the ordering is explicit:
  1. Before an offload uploads or reuses a version, it commits a protection record for the object name with a full sync. Any later capture therefore contains the record.
  2. The offload publishes the sidecar reference and syncs that write to disk.
  3. Only then does it retire the protection record.

  At every capture after step 1, the clone holds the record, the synced reference, or both. A reuse also checks that a live sidecar references the version at the moment its record commits.
- **Destroy.** cellctl destroys versions only in the backup hold, after that hold's backup and prune succeed. Holds are exclusive per cell, so no restore and no other capture runs in between. It destroys a version only if all of these hold:
  - the version appears in a listing of the cell's media prefix that cellctl completed before it took the newest retained snapshot's capture;
  - no retained snapshot's manifest names that version or lists its object name under a protection record;
  - every retained snapshot has a readable manifest. Otherwise nothing is destroyed for that cell.

  cellctl then deletes the manifests of pruned snapshots.
- **Why that is enough.** A reference that exists at capture is in the newest manifest. A reference written after capture names a version that a protection record covers. That record was synced before the upload or reuse began, so it is in every capture until the reference itself is synced. Only versions in a listing that cellctl completed before the newest capture are candidates, so no clocks are compared. A restic restore brings back sidecars whose versions some retained manifest names.
- **Owner deletion.** An original the owner deletes leaves the store when the last retained backup that references it is pruned, as deleted vault files leave restic today.
- **Cell deletion.** Media content-key retirement follows `cloud-cell-key-custody` and does not wait for edge or provider cleanup.
  Live-copy absence and retention evidence still govern completion; removing the database bundle alone proves neither.
  Cleanup keeps only the non-content credentials it needs. Its provider steps retain this order:
  1. Remove the cell from the edge's active set.
  2. Observe that the edge has loaded that set.
  3. Abort every unfinished multipart upload under the cell's prefix.
  4. Delete every version under the prefix.

  It reports `deleted` only after it observes that the versions and the bundle are absent. A revoked credential cannot repopulate the prefix after the absence check.

### D10. Quota: an included media allowance

Each cell has a media allowance, separate from its volume size. It is a configured value in the one limits source; its first value is 100 GB. Paid blocks follow the existing paid-storage observation.

- **Metering.** The cell meters original plaintext bytes from the versions its live and trash sidecars reference, each version counted once. Trash counts until it is emptied. A version kept only for retained backups does not count against the owner. cellctl separately meters physical ciphertext bytes per prefix from the bucket listing, for the operator.
- **Admission.** An upload or import whose declared media bytes exceed the remaining allowance is refused before any byte arrives, with a code that names the limit.
- **Overflow.** An original that the allowance cannot cover stays local and is reported in status.
- **Two meters.** Status and the Home display report two figures, each named for what it counts:
  - *Vault storage:* notes, sidecars, extracted text, indexes and datasets on the volume, against the volume size. It excludes cached copies of stored originals, which would otherwise be counted twice.
  - *Media storage:* each stored original counted once, against the allowance.

  Vault storage is provisioned per cell and costs more per gigabyte; media storage is billed by the bytes stored. A meter that cannot be read shows as unknown, never as zero.

#### Complete reference accounting

The owner meter reads canonical sidecars, including Trash, under the existing consistency guard.
It deduplicates the selected store's object versions and checks that duplicate plaintext sizes and digests agree.
The cache ledger and provider listings cannot prove the owner's current reference total.
A strict census extends the existing reserved-path boundary; it adds no inventory database or second exclusion list.
Registry-owned private state stays excluded.
Ordinary aliases, incomplete enumeration, invalid placement or malformed potential companions make usage unknown.
Closing checks compare the complete namespace and the versions of selected headers and used placement files.
These witnesses detect changes across the census; they do not create an atomic snapshot against external writers.
The shared frontmatter reader gives custody readers a `custody` mode that distinguishes absent frontmatter from an unterminated block; strict reads already reject a non-mapping root, and an empty or null root holds no fields.
Ordinary strict readers keep their meaning, so a plain note that opens with a thematic break never blocks a governed write.
Unknown usage defers offload, while local bytes and core operations remain available.
This prevents unmetered uploads; its cost is a retry after reference custody is repaired.

Trashing a stored binary writes a canonical companion for its physical Trash path.
Both that companion and the placement metadata become durable before the old live reference retires.
The transition checks all destination names and reverses only files it created.
Directory companions can retain historical pointers; one strict placement reader shares recovery's original-root and descendant-suffix mapping.
Recovery either transfers a reference to a compatible live companion or restores local bytes before retiring its Trash companion and metadata.
Both recovery entry points use the same rule.
A failed cleanup keeps enough placement evidence for the retained reference.
Older Trash bytes without a canonical reference remain local; no cache or provider listing invents their former version.

The existing Cloud worker lock serializes offloads; no second queue or reservation database is added.
The cell checks complete usage before encryption or provider upload, then recomputes the proposed reference union under the publication guard.
A referenced version adds no bytes; a newly referenced version adds its original plaintext size.
Reuse requires a current canonical companion outside Trash; historical placement alone does not prove that the live binding remains.
If provider confirmation or the protection check invalidates reuse, admission assesses a new version before encryption.
A publication refusal retains the plaintext and the durable protection record.

The chart's media edge store policy supplies `allowance_bytes` through the existing cell configuration path.
The runtime has no business default; a missing or invalid allowance makes admission unknown.
A quota or unknown-meter refusal retains retryable worker demand and reassesses current custody and policy on a later scan.
It does not enter the exact-input terminal refusal rule.
Restricted status rows show the current refusal code without global usage figures.
The first implementation scans directly; a cache requires measured cost and an explicit invalidation contract.

Complete ingress admission and the operator's physical meter remain part of task 4.5.

The 20 GiB volume cap is unchanged. It now bounds notes, sidecars, indexes, datasets and the cache, not media.

S2 import admission is unchanged by this change. A later streaming-import slice splits it in two:
- **Volume admission:** current use plus twice the largest declared file plus the reserve.
- **Store admission:** total declared media bytes against the remaining allowance.

**Amendment, 2026-10-30 (task 5.4): the allowance and the S2 split.**

This amendment replaces the two bullets above. For one window, `declared` is its bytes, S its store-bound bytes and L its largest store-bound member. Σ is the store-bound bytes of the whole series.

- **Allowance.** A stage reference counts toward the allowance, with the sidecar references. Each version counts once. The cell checks the allowance before it encrypts a staged member. A member that the allowance cannot cover stays on the volume, and status names the reason.
- **Volume admission.** Substrate refuses `IMPORT_STORAGE_LIMIT` when 2 × (declared − S) + S + L + reserve is above the cell's cap. A claim grows toward current use + 2 × (declared − S) + reserve. Store-bound bytes never grow a claim, because the sender sizes each window from the cell's free space.
- **Store admission (pending operator confirmation).** Substrate refuses `MEDIA_ALLOWANCE_EXCEEDED` only when Σ is above the whole allowance. The usage reading that Substrate holds can be stale, so its check is a necessary check only. The cell decides the remainder with its live meter. The sender reads that remainder before its first upload, and stage offload refuses beyond it.
- **Published limits.** cellctl writes `media_limits`, which holds `allowance_bytes` and `max_object_bytes`, for a cell selected for the store. It writes NULL for any other cell, a cell that only retains a disabled store included. The values come from the configuration that cellctl renders into the cell's store policy, so the published number is the enforced number.
- **Older senders and cells.** An import without the new fields, or a cell without `media_limits`, keeps today's formula. A cell with `media_limits` and no `import_limits` gets the store check only.

**Dependency on Substrate.** The Substrate change `admit-cloud-imports-against-published-limits` carries the Substrate side. It adds these optional, immutable C5 create fields, all or none: `declared_store_bytes` (S), `declared_largest_store_bytes` (L) and `series_store_bytes` (Σ). They satisfy 0 ≤ L ≤ S ≤ `declared_bytes` and S ≤ Σ. It also adds the `media_limits` column with its migration and grants, the formula and the refusal. This change fixes the contract only. Each production step needs the operator: the Substrate migration and deploy, the cellctl and chart rollout, the cell image rollout, and the store canary. A reimport of customer data needs the operator as well.

### D11. Rollout and sequencing

1. Ship core behind the store being disabled. Tasks 3.x prove the states and eviction against a filesystem-backed store.
2. Ship the bucket, edge, keys and policies with the edge off.
3. Enable one canary cell. Back-fill: upload and verify every bound original, then evict to target.
4. Enable the fleet switch only after export can deliver every selected cell's media (task 5.5). Until then, only the canary may store more media than its export can stage.

Phases 1–6 of `move-cloud-cells-to-local-storage` do not depend on this change. Phase 7 does for any media-heavy cell. Either this change's rollout finishes first, or a media-light cell migrates first.

### D12. An import larger than the volume arrives in windows

Added 2026-10-30 for task 5.4. It builds on `scale-agent-led-imports`, which supplies the stage manifests, consume-on-filing, paging, collection identity and the space stop.

Today an import fails at the first member that does not fit, after the whole upload, and the Job deletes its staging tree. Nothing lands.

**Decision (pending operator confirmation).** A sender splits the folder into windows. The runtime stores the store-bound members of each landed window in the media store. Adoption files them by reference (D8). Text stays on the volume and files by copy.

- **Window budget.** The budget is free space, less the reserve, the temporary-space bound (`max_temporary_bytes`), the unfiled staged resident bytes and the largest local store-bound staged member. A window is sized so that 2 × its resident bytes + its store-bound bytes + its largest store-bound member does not exceed the budget. The cell reports the budget in `adoption_studio status`. It reports unknown, never 0, when an input cannot be read.
- **Store-bound rule.** A member is store-bound when it is not text and its size is at most the store's per-object bound. Text means a suffix in the importer's text set and a size within the inline text limit. Every other member is resident. One module holds the rule. The CLI, stage offload and the adoption inventory import it. The cell reports the rule's inputs, so Home keeps no copy.
- **Order.** Windows go store-bound first, resident last, in path order. This keeps the artifacts-before-text order of `scale-agent-led-imports` across windows.
- **Pacing.** A sender creates the next window only when the budget covers it, so a prepare hold never interrupts a drain. A window that the Job refuses for space is planned again from the current budget. Landed windows are never sent again.
- **Senders in slice 1 (pending operator confirmation).** Both the Cloud CLI and the Home page send windows, so an agent without a shell, such as claude.ai or ChatGPT, gets the same capability. The filing flow stays in MCP tools for every agent: `adoption_studio` from `start` to `finish`, then `import_discard`. Only the sending needs the CLI or Home.
- **Holds (pending operator confirmation).** Each window needs at least two holds, prepare and import. Each hold restarts the cell. Slice 1 accepts this cost.
- **Rehearsal.** The rehearsal runs on a local-storage cell, where S2 admission is inactive. Its store is a B2 sandbox bucket.

Options considered:

| Option | Holds | Agent between windows | Large media | Verdict |
|---|---|---|---|---|
| A: windows, files copied | 2 per window | required | blocked by the 200 MiB ceiling | rejected |
| B: windows, store-backed stage, filing by reference | 2 per window | no | stored, never copied | recommended |
| R: the runtime streams one archive from the media bucket through the edge | about 0 | no | stored, never copied | follow-up if holds hurt |
| P: per-file gateway upload (tus) | 0 | no | caps of 100 MiB and 2 GiB | rejected for now; depends on the paused gateway work (task 5.3) |
| One archive unpacked in parts | n | not applicable | not applicable | rejected: a resume decrypts the prefix again, and the client holds the whole ciphertext |

R needs a new C5 transport, media-bucket multipart in cellctl, a D9 import object and a runtime unpacker.

The recommendation changes in three cases:
- The rehearsal measures hold downtime above about 5 minutes per window, or more than about 20 windows for typical sizes. Move to R and keep the stage and filing paths of D8.
- Backup retention cannot list stage references. Use A, with the writer fix.
- The CLI cannot call cell tools through the gateway. The agent then mediates each window: it reads the budget and passes it to the CLI.

Delivery slices:
1. **Slice 1.** Exomem: the shared predicate, M1 to M4, stage offload and references, filing by reference, budget status, CLI windows and tool text. Substrate: the Home sender. The local-storage rehearsal proves it.
2. **Slice 2.** The S2 split (D10), before the Substrate admission change deploys to store-selected attachable cells.
3. **Slice 3.** One key per series to pipeline uploads, and R if the rehearsal shows that holds hurt.

## Comparison

| Concern | Media on the volume (today) | Media store |
|---|---|---|
| Cost per GB-month | EUR 0.044–0.057 network volume; about EUR 0.14 node-local before reserve | about EUR 0.0064 |
| Cell cap and S2 | 20 GiB cap limits media | cap bounds text, indexes and cache; media has its own allowance |
| Backup | restic carries every original | restic carries the vault and cached originals; versions that retained snapshots reference are kept |
| Restore time | grows with media (about 16 min per 10 GiB) | bounded by the volume cap |
| Export | reads the volume | a packing step without public egress fetches evicted originals; a separate step uploads the archive |
| Deletion proof | namespace, volume, backup versions, backup key | adds the edge revocation, media bucket versions and the media key |
| Egress | runtime has none | one fixed in-cluster edge |
| Privacy | operator holds volume and backup keys | operator also holds the media key; provider sees ciphertext |

## Rejected options

- **Raise caps and keep media on the volume.** This works on network volumes today. It breaks the node-local reserve, density and restore-time budgets.
- **JuiceFS CSI or an rclone crypt mount.** The mount process holds plaintext in a pod with public egress, needs a privileged mount, and adds a metadata engine with its own backup. FUSE also breaks the inode and change-time identity checks in media reconciliation.
- **restic as the store.** Retention prunes anything no longer on the volume, and single-file reads load the whole index in a Job.
- **git-annex.** The vault is not a Git repository. Its pattern, keyed content dropped only after a verified copy, is kept.
- **Provider-side encryption with customer keys.** The provider receives the key with every request and handles plaintext.
- **Object storage at the compute provider.** Its credentials are project-wide (unverified), and it would put media and live cells with one provider.

## Risks / Trade-offs

- **Absence read as deletion.** A reader that treats a missing file as deleted would purge transcripts and vectors of an evicted original. This is the main risk.
  - Mitigation: one state function that every consumer calls. Today's consumers that test physical presence are media reconciliation and provenance, governance companions, the deletion fan-out, orphan healing, move and rename, trash recovery and listing, adoption, backlinks and media locators, vault scans, the file watcher, the media worker and frame reads, and export and download (task 3.4).
  - An end-to-end test evicts, runs reconciliation, governance and the deletion fan-out, and asserts that nothing is purged.
- **Edge outage.** Offload queues and eviction stalls; the volume grows toward its cap. Mitigation: an alert on offload backlog age.
- **Cold playback.** A large evicted original hydrates in full before ranged playback starts, which takes seconds to tens of seconds; the spike measures it.
- **Key loss.** Media recovery follows the cell root's custody and recovery rules in `cloud-cell-key-custody`. This change adds no separate escrow or bypass.
- **Sidecar edits.** An edit could drop or alter the reference. Mitigations: the descriptor key is governed, and a version stays in the store while any retained snapshot references it.
- **Compromised edge.** It holds a bucket-wide credential, so it can bypass its own active-set check. It sees every cell's ciphertext and object names, can write new versions under any prefix (a revoked cell's included), and can deny service or run up storage. It cannot decrypt, destroy versions or change a version that a reference pins. A captured *cell* credential is narrower: the edge refuses it once revoked. Mitigations: the edge holds no media key, version destruction stays with cellctl, and cellctl's metering alerts on unexpected bytes.
- **Hydration storms.** An export or a CLIP model change can hydrate many originals. Concurrency and temporary space are bounded (D3).
- **Holds per window (D12).** Each window restarts the cell at least twice. A large import therefore costs the owner several short outages. The rehearsal measures the downtime, and R is the follow-up.
- **Stage references are cell-local state (D8).** Losing the state folder loses the references of unfiled stored members. The person still holds the originals, and the import is repeated.
- **Eviction of unread originals (D3).** M4 lets a recording leave the volume before its transcript exists. The worker hydrates it once when it claims the job.

## Open questions

Each question below names what settles it.

- **Eviction target, margin and minimum idle age.** Settled by two to four weeks of hydration records on the canary cell.
- **Edge integration:** the custom service is selected above. Task 2.1 still measures 1 GB hydration; sandbox provider checks verify exact-version recovery after lost replies.
- **Export of more media than the export staging volume can hold.** This covers staging, part limits and deadlines; Cloud export today sizes its total from the cell. It is settled with the owners of `add-cloud-owner-export` and gates the fleet switch (D11).
- **Direct presigned upload to the store for files above the 2 GiB upload-session limit.** Settled by the first such file.
- **Unbound binaries.** A file that was neither preserved nor matched by the extraction registry has no sidecar and stays on the volume. Whether discovery should give large unbound files a sidecar is settled by their byte share in canary vaults.
- **The archive member pool.** It stores content-addressed members in the vault. Whether it is in scope is settled by its byte share.
- **Chatbot-native file delivery.** Settled per client, once its support is confirmed and the transfer it needs is known.
- **Key rotation, video transcoding for playback, and paid block prices.** Each is decided when its first need appears.
- **Windows (D12).** Four facts are unmeasured. Rehearsal 5.4.12 settles three of them: the hold count and restart time per window, and the derived-state size and start-up scan cost at the final Source count. It also settles whether a note links to an attachment that an earlier window stored. The first CLI test against a real cell settles whether the CLI's OAuth client may call cell tools through the gateway.
- **Turning the store off while staged members are stored.** The drain rule above hydrates evicted originals. A stored staged member has no sidecar yet. Whether the drain restores it to its stage, or blocks until the stage is discarded, is decided before task 5.4.4 lands.
