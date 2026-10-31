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

An import larger than the volume needs room while it runs. The rules below change, and the text above stays as the first design.

- **M3, when eviction runs.** Eviction runs after each offload and each media job when use is above the target, and on a timer. It no longer waits for the worker's start-up scan. It takes candidates from an indexed query on the cache table, which gains a size column. It does not read every sidecar.
- **Eviction on demand.** While an import series waits for room, its sender records a demand: the series ID and the bytes that its next window needs (D12). The cell keeps one demand for each series and serves the largest live one. Eviction then also runs when two conditions hold:
  - the window budget is below the demand;
  - the window budget plus the bytes of the eligible originals covers the demand.

  Without the second condition, eviction would take every eligible original for a demand that it cannot meet yet. Eviction stops when the budget covers the demand, or when no eligible original is left. The cell ignores a demand above the reachable budget (D12), because any status caller can send one. The order and the eligibility rules stay the same. The everyday target and margin do not change, so the owner's cache gets colder only while a series waits. Each demand is a lease that the series' status calls renew. It lapses after 15 minutes without a renewal, so a sender that stops leaves the everyday target in force. The CLI renews its demand at least every 5 minutes.
  - The alternative is a lower target for every cell. The band between the target and the growth threshold must then hold the fixed budget terms and a useful window. On a 20 GiB cell that needs a target of about 0.55, which makes every owner's cache colder every day.
- **M4, what counts as use.** Idle time counts owner reads only: playback, download and viewing. Offload, extraction, hydration for processing and export packing record no read. Today offload records a read when it caches the original. An original that no owner has read is eligible whatever the minimum idle age. Eviction takes unread originals in this order: first those with no pending job, then those with one, each group largest first. It then takes owner-read originals in today's order. Filing by rename (D8) puts each filed original on the volume with pending jobs, so the job rule changes too:
  - A pending job that no worker has claimed does not keep its original on the volume.
  - A job that a worker has claimed keeps its original until the job completes, fails or is blocked. This holds across the timeout slices and memory stops that return a claimed job to the queue as pending. Without it, a long recording would be evicted and hydrated again for each slice.
  - A failed or blocked job does not keep its original. An engine wait blocks its job, so the original can leave the volume, and the worker hydrates it again when the engine returns.
  - A pin, a hydration or a download still keeps it.
- **Claim order.** Today the worker claims the oldest fresh job before any resumed speech job. Under M4 each claimed job keeps its original. Jobs whose originals the worker hydrated could therefore pin more than the volume holds. The worker counts the claimed but unfinished jobs for which it hydrated the original. While that count is at `max_hydrations`, it claims no job whose original must be hydrated. It always claims a job whose original is on the volume before a job whose original must be hydrated. Within each group, today's rule holds: fresh jobs before resumed ones.
  - The cap reuses `max_hydrations`, because the window budget reserves room for that many placed copies.
  - A job whose original was on the volume at its claim adds no bytes, so the count leaves it out. Long recordings that stay on the volume therefore never delay a capture. Their pins can delay eviction on demand until their jobs finish, and the series then waits.
  - A resumed job waits behind every fresh job whose original is on the volume. A resumed job that the memory brakes refuse therefore never blocks those fresh jobs. Today a refusal stops the claim at the job that the brakes refuse.
- **Hydration failure defers or blocks the job.** Today a hydration error inside a media job fails the job, and the pending-work scan never queues a failed job again. Under M4 the worker hydrates an evicted original when it claims the job. These outcomes follow:
  - A transport failure, or hydration capacity that other hydrations or offloads hold, defers the job. The deferral consumes no attempt and keeps the job's speech progress.
  - A deferred job waits a retry time before a worker claims it again. The retry time starts at 1 minute and doubles with each deferral, up to 15 minutes.
  - An original whose plaintext exceeds the whole hydration capacity blocks the job with `MEDIA_HYDRATION_TOO_LARGE`. Waiting never clears it. Today `_reserve` reports both capacity cases as `MEDIA_STORE_BUSY`, so hydration must tell them apart. The download routes refuse such an original with the same code. Today they report every media store error as `ORIGINAL_UNAVAILABLE`, which tells the client to retry.
  - A verification mismatch still fails the job.
- **Resident originals.** Offload applies the classification rule of D12 before it encrypts. An original that the rule calls resident gets a recorded refusal, and offload writes no temporary file for it. Today offload encrypts the whole file on the volume before the edge refuses it by size. Eviction never selects an original whose plaintext exceeds the hydration capacity. Such an original can exist when the capacity shrinks after its offload. The rule checks configured bounds, so D1 still keeps no size list.
- **Scope (confirmed by the operator on 2026-10-30).** M3 and M4 apply to every original, not only to imported ones. Everyday eviction still starts only above the target. A captured recording that the owner has not played can leave the volume before its transcript exists, and the worker then hydrates it once. During an import, eviction on demand can also take just-filed recordings before transcription, once no unread original without a pending job is left. Each such recording then costs a download and a decrypt, and the hydration capacity bounds transcription throughput while the series runs. The operator chose this scope over a rule for imported originals only. That rule would keep captures unchanged, but it needs an origin flag on each cache record.

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

The operator confirmed the shape in D12 on 2026-10-30. It adds windows and filing by rename. It changes one rule above: M4 in D3 replaces the rule that eviction never selects an original with pending work.

- **Arrival in windows.** A sender splits a folder larger than the cell's window budget into ordinary `adopt` imports, called windows. Each window has its own import ID, recipient and checkpoint. C5 and cellctl do not change. The import Job changes in one way: it records each member's identity in the stage manifest, beside the path, size and SHA-256. The identity is the staged file's inode, size, `mtime_ns` and `ctime_ns`, read after the Job sets the modification time.
  - The identity leaves out the device number. The encrypted volume is opened for each pod, so the Job and the runtime can see different device numbers for one file.
  - The change time still catches a backup restore, because a restore cannot set it.
- **Filing by rename.** The stage and the Knowledge Base are on one filesystem. Adoption files an artifact member through a new Source-writer primitive that renames the staged file into place. The primitive takes these steps:
  1. Compare the staged file's identity with the manifest identity. On a match, use the manifest SHA-256 as the Source's `binary_sha256` and read no byte.
  2. On a mismatch, for example after a backup restore, hash the file once between two stat checks. Continue only when the digest equals the manifest SHA-256 and the two stats agree. Otherwise the member changed, and it goes to the residual.
  3. Rename the staged file to the Source's binary path with a no-replace rename, and sync both directories. When a file already exists at that path, the rename fails, and the member stays unfiled with the conflict reported.
  4. Commit the Source page, which is the binary's sidecar, through the batch writer. The binary is never one of the batch writer's destinations, so that writer's rollback never removes it.
  5. Queue the artifact's offload job when the classification rule (D12) calls it offloadable.

  A rename that fails with a cross-device error falls back to the streamed copy of `scale-agent-led-imports`.
- **Crash states.** The writer leaves one of two states after a stop:
  - *Renamed, Source page not committed.* The binary is at its Knowledge Base path, and its staged path is empty. Replay compares the binary's inode, size and `mtime_ns` with the manifest identity. The rename changed the change time, so replay does not compare it. On a match, replay commits the Source page with the manifest SHA-256. On a mismatch, replay hashes the file where it is, within the ceiling, and commits the Source page when the digest matches. Otherwise it leaves the file unbound at its Knowledge Base path and reports it as changed. Replay never moves a file back into a stage, because a discard deletes the stage.
  - *Source page committed.* The member is filed. Replay settles it as already applied by comparing the Source page's `binary_sha256` with the manifest, without reading bytes. It queues the offload job if none exists and the member is offloadable.

  Replay on three fields can accept a file that a write changed and then reset to the same size and modification time. Only the cell's own processes write there, and offload's digest check catches such a change in an offloadable file. Slice 1 keeps this risk (see Open questions).
- **Rollback.** A failed batch rolls back the Source pages that it wrote. The primitive then renames each binary of the batch back to its staged path with a no-replace rename. It recreates a staged parent folder that `finish` removed. When the staged path is taken, the binary stays unbound at its Knowledge Base path, and the run reports it. The primitive never removes a renamed binary, because it is the only copy.
- **Discard.** `import_discard` refuses with `IMPORT_RENAMES_PENDING` while a member of the window is renamed but its Source page is not committed. The Job writes the Knowledge Base paths of those members to the receipt's `renames_pending` field, keeps the stage and the manifest, and settles nothing itself. The person or agent then runs `apply` again, which settles the member by the replay above, or moves or deletes the file, and retries the discard. A discard that proceeds drops the field in the write that drops the manifest. Without this rule, a discard would delete the manifest that replay needs, and nobody would learn of the unbound binary.
  - Decided on 2026-10-31 under the operator's standing rule for reversible choices; the operator may revert it. The other option settled the member inside the discard, by replay. The Job does not apply from a stopped cell, so it refuses and names the member. When the refusal fires wrongly, the agent pays one more call.
  - The discard then keeps a residual record in the window's receipt. The record lists each staged member that adoption did not file, with its path, bytes and residual code. It leaves out the series file. The discard writes the record, empty or not, in the same receipt write that drops the member manifest. A stop therefore leaves both changes or neither, and a retried discard finds no manifest and keeps the record. When the stage exists and the discard cannot read its receipt, it refuses with `IMPORT_RECEIPT_UNREADABLE` and keeps the stage. Today `_drop_members` returns without an error in that case, and the discard then deletes the stage with no record. Without the record, the discard would erase the only sign that a member was never filed, and the person could delete the source.
  - The refusal names its exit: `import_discard` with `walk_stage` set (simplest choice, marked). That discard walks the stage and records each staged file except the series file, with its path, bytes and the code `receipt_unreadable`. It writes the record to a separate file for the import ID, leaves the unreadable receipt in place, and then removes the stage. A retried walk keeps the record that an earlier walk wrote. Status, a resend and an acknowledgement treat that record as the window's residual record. A discard refuses `walk_stage` with `IMPORT_REQUEST_INVALID` when it can read the receipt. A walk would otherwise skip the settling of renamed members and record the wrong codes.
  - The walk can list a text member that adoption filed by copy, and the person can acknowledge it. It cannot find a member that was renamed but not committed, because that member has no staged copy. The refusal above comes first, so a walking discard never passes over such a member while its rename hold is live. When the receipt is unreadable, the Job writes the held paths under `renames_pending` into the walk file, which then holds no member list. A retried walk replaces that file with the member record.
  - The refusal comes first because a read that fails once can succeed on a retry. Only the receipt's manifest lets replay settle a renamed member. When the refusal fires wrongly, the agent pays one more call.
- **Ceiling.** The adoption ceiling of `scale-agent-led-imports` bounds copies only, because a rename reads no byte. The re-hash in step 2 and in replay reads every byte, so it keeps the ceiling. A member that the ceiling stops goes to the residual, and the sender can resend it (D12).
- **Text.** Text members file by the copy and consume path of `scale-agent-led-imports`.
- **After filing.** A filed original leaves the volume through write-through (D1) and eviction (D3). This shape adds no stage reference and no stage offload, and it keeps D2 unchanged.
- **Processing.** Extraction of a filed original queues as today. M4 in D3 lets eviction take the original before a worker claims the job, and the worker hydrates it at the claim.

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

Under filing by rename (D12), each window is an ordinary import within the limits that cellctl already publishes. The split above needs no new C5 field for windows. This amendment says what remains of it.

- **One census per batch.** Offload today runs the full reference census up to three times per member. Each run holds the mutation boundary through the consistency guard. A series of thousands of members makes that quadratic. Offload therefore takes one census per batch. It passes that census to each member's admission through the existing `usage` parameter, and it adds each newly referenced version's size as it publishes.
  - The batch reuses its census across guard releases. It never holds the mutation boundary for the whole batch, so writes still run between two members.
  - Today a caller may share a census only inside one enclosing guard (`_offload_admission`). The batch census changes that contract.
  - The reuse is sound only when the referenced total rises through offload alone while the runtime serves. A restore runs under a hold, and other writers can only keep or lower the count. Task 5.4.9 tests this assumption (open question).
- **Terminal stops.** "Media usage unknown" and "allowance exhausted" stop the sender. It reports the stop and does not wait, because neither clears without the owner or the operator. The cell keeps its behaviour above: the original stays local with retryable demand.
- **Store admission.** The bullet above, total declared media bytes against the remaining allowance, moves to the sender. Before each window, the sender compares the series' remaining offloadable bytes with the remaining allowance that the cell reports. Complete ingress admission for single uploads stays part of task 4.5.
- **Volume admission.** The open Substrate change `admit-cloud-imports-against-published-limits` applies one `space_factor`, 2, to every import kind. It grows a claim when used + `space_factor` × declared + `reserve_bytes` exceeds the current size. The sender keeps each window within that size (D12), so admission grows nothing. Factor 2 is larger than a rename needs, which only makes windows on attachable cells smaller.
- **What remains of the split.** A per-kind space factor is the only part left. Under rename an artifact member needs its bytes once, but a text member still needs two copies. One factor for all `adopt` imports cannot tell them apart, so a factor of 1 would under-reserve text. The rehearsal decides whether a finer factor is worth a C5 field. Slice 1 adds no C5 field and publishes no media limits.
- **Older cells.** A cell that reports no window budget follows the older-cell rule of D12.

**Dependency on Substrate.** Slice 1 needs no Substrate change. When the imports route returns the published `import_limits`, the sender reads them. Otherwise admission keeps its legacy rule, which grows no claim. Home windows need a Substrate change of their own (D12). Each production step needs the operator: the cellctl and chart rollout, the cell image rollout, and the store canary. A reimport of customer data needs the operator as well.

### D11. Rollout and sequencing

1. Ship core behind the store being disabled. Tasks 3.x prove the states and eviction against a filesystem-backed store.
2. Ship the bucket, edge, keys and policies with the edge off.
3. Enable one canary cell. Back-fill: upload and verify every bound original, then evict to target.
4. Enable the fleet switch only after export can deliver every selected cell's media (task 5.5). Until then, only the canary may store more media than its export can stage. Imports larger than the volume also stay on the canary until then (D12, choice 4).

Phases 1–6 of `move-cloud-cells-to-local-storage` do not depend on this change. Phase 7 does for any media-heavy cell. Either this change's rollout finishes first, or a media-light cell migrates first.

### D12. An import larger than the volume arrives in windows

Added 2026-10-30 for task 5.4 and revised the same day to file by rename. It builds on `scale-agent-led-imports`, which supplies the stage manifests, consume-on-filing, paging, collection identity and the space stop.

Today an import fails at the first member that does not fit, after the whole upload, and the Job deletes its staging tree. Nothing lands.

**Choice 1, the shape: file by rename (confirmed by the operator on 2026-10-30).** A sender splits the folder into windows. The agent files each window, and adoption renames each artifact member into the Knowledge Base (D8). Write-through and eviction then move the filed originals off the volume, and the sender sends the next window. Text members file by copy.

The cost: the volume drains only as fast as the agent files. The sender waits for filing between windows, so the person's machine stays online until filing catches up.

- **Window budget.** The cell computes the budget from the growth threshold's headroom: growth threshold × volume size − used. The growth threshold is cellctl's online-growth trigger, 80%, which cellctl renders into the store policy. The cell then subtracts these amounts:
  - the import Job's free-space reserve;
  - the hydration capacity, `max_temporary_bytes`, which holds a streamed hydration's plaintext and an offload's ciphertext temporary file (choice 3);
  - the placed hydrated copies, `max_hydrations` × the largest offloadable file;
  - the text bytes that landed windows have not filed yet, because each needs a second copy.

  A window fits when 2 × its text bytes + its artifact bytes is at most the budget; a rename adds no copy. The cell reports the budget in `adoption_studio status`. It reports unknown with a reason, never 0, when an input cannot be read.
- **Reachable budget.** The cell also reports the reachable budget: the budget once the agent files every landed window, pending offloads finish and eviction frees every eligible original. It counts the offloadable originals, staged or filed, that no pin, claimed job or recent owner read keeps. It leaves out an original that an owner read within the minimum idle age, so the figure errs low.
- **Job-pinned bytes.** The cell also reports the job-pinned bytes: the offloadable originals that claimed, unfinished jobs keep on the volume, the copies that the worker hydrated for them included. A hydrated copy counts in `used`, so its release raises the budget like any other original. The reachable budget leaves them out, but they become eligible when their jobs end. Take an archive of long recordings that each ran one slice. Without this figure, it would read as a band too small, and the sender would stop instead of waiting.
- **Effective file limit (decided on 2026-10-30 under the operator's standing rule for reversible choices; the operator may revert it).** The cell also reports the effective file limit: the largest offloadable member that can land now. It is the reachable budget plus the job-pinned bytes, capped by the largest offloadable file.
  - Before the first upload, the sender refuses a series with an offloadable member above that limit. The refusal lists each such member with its size.
  - The limit can shrink during a series, because notes, derived state and staged text grow. "Band too small" can therefore still stop a series later.
  - Without the limit, a member that the band cannot hold stops the series only after earlier windows landed.
- **Why eviction on demand.** Everyday eviction runs only above the eviction target and stops at the target minus the margin. Use can settle between that stop point and the target, and then eviction never runs. Take a 20 GiB cell with a target of 0.7, a margin of 0.1, 2 GiB used and 10.5 GiB of fixed terms. After one window of 3.5 GiB, use settles near 5.5 GiB, below the 14 GiB target, and the budget stays near 0. Eviction on demand (D3) removes this stall. While the sender waits, each status call carries the series ID and the next window's bytes as the demand. Eviction then frees eligible originals until the budget covers them.
- **Growth.** When the sender sizes each window, use stays at or below the growth threshold, so a local cell does not grow at backup. This is best effort. Owner hydrations and derived state can push use past the threshold during a window. Two series that run at once can also each create a window from one budget reading. The sender reads the budget again right before each create, which narrows that race but does not close it. A local cell then grows at its next backup, as today. The Job checks free space, not the threshold, so its free-space check stays the hard limit.
- **Admission.** On a cell that publishes `import_limits`, the sender also keeps used + `space_factor` × the window's bytes + `reserve_bytes` within the cell's current size. Admission then grows no claim (D10).
- **Classification rule (choice 3).** An artifact member is offloadable when three bounds hold. Its `age` ciphertext size is within the store's object bound and within the hydration capacity. Its plaintext size is within the volume fraction × the volume size. Text follows the importer's text rule. One module holds the rule. The CLI, the adoption inventory, offload and eviction import it (D3). The cell reports the rule's inputs and the effective largest offloadable file in status, so a sender keeps no copy of the rule.
- **Resident members.** An artifact member above the largest offloadable file still files by rename, but stays on the volume. Text stays on the volume too, and its staged copy stays until discard. The sender counts both as resident. Before the first upload, the sender refuses a series when 2 × its text bytes + its resident artifact bytes exceed the reachable budget. The refusal lists the resident files.
- **Order.** Windows carry offloadable artifacts first, resident artifacts next and text last, each in path order. This keeps the artifacts-before-text order of `scale-agent-led-imports` across windows, so a note's links meet filed attachments.
- **Pacing.** The sender sizes each window within the reachable budget. It reads the budget again right before it creates the window, and creates it only when that reading covers it. A prepare hold therefore never interrupts filing. A window that the Job refuses for space is planned again from the current budget. On resume, the sender checks the cell's import receipts before it skips a window that its checkpoint calls landed. A window whose receipt is gone, for example after a cell restore, is sent again.
- **Waiting and stopping.** The sender waits and reports in these cases:
  - the budget is unknown;
  - the reachable budget covers the next window, and the budget does not yet;
  - the reachable budget cannot hold the next member, and the reachable budget plus the job-pinned bytes can.

  It stops and reports in these cases:
  - "media usage unknown" or "allowance exhausted" (D10);
  - "band too small": the reachable budget plus the job-pinned bytes cannot hold even the next member. The stop gives the budget, the reachable budget, the job-pinned bytes and that member's bytes.

  Every stop says that the members not yet sent are not in Exomem, and that the person must keep them.
- **Older cells.** A cell that reports no budget at all is an older cell, or a cell without the store. The sender then refuses when the declared bytes exceed the cell's known size, which is `max_storage_gib` when `import_limits` is published. Otherwise it sends one import, as today, and the Job's free-space check decides.
- **The series file.** The sender records the series' full path list and its label, the source folder's name. Each window carries both in one reserved file at its stage root, `.exomem-series.json`, which the Job stages like any member. The CLI refuses a source folder that holds a file of that name at its top level. Adoption never files the series file. It uses the file in three ways:
  - It passes the path list as `whole_scan`, so a bare name resolves against the whole series.
  - It applies the collection-root rule to the path list, not to one window. Every window then gets the same root, and collection paths stay stable.
  - It takes the label as the default collection label. Today a stage with several top-level entries takes its import ID as the label, and each window has its own ID.
- **Resending the residual.** A window keeps its receipt after its discard, so a rerun of the series skips it and never resends its residual. The discard keeps a residual record in that receipt (D8). Status reports the record's members as "discarded, not filed" until the person resends or acknowledges them. The person resends them as a new series, with new import IDs:
  - The CLI takes their paths from the residual records of discarded windows. It resends no member of a window that is not discarded yet, because that member's staged copy can still file.
  - Each resend window carries the first series' file unchanged. Adoption then applies the collection-root rule to the same path list, so the resend keeps the first series' root, label and `whole_scan` list. The series file therefore needs no root of its own.
  - The CLI refuses a member whose source file differs in size or modification time from the series record.
  - After a resend window lands, the CLI clears its members from the old records. From then on, that window's own residual tracks them.
- **Acknowledging the residual (simplest choice, marked).** `adoption_studio(action="acknowledge")` takes an import ID and the paths to clear from that window's residual record. It refuses a call without paths, so one call cannot clear a whole record. Its tool text says that acknowledging is the person's decision, for members that they do not want filed.
- **Keep the source.** The CLI reports done when the last window lands, before filing ends. Tool text and the CLI therefore say to keep the source folder while an unfiled member remains or a residual record lists a member. An unfiled member is a manifest member, other than the series file, that no adoption run has filed. A window with no run counts every member except the series file. A residual record that lists no member does not count, so the warning ends once a series is fully filed and discarded. Status reports both. The residual alone does not decide, because adoption computes it only at `finish`, over a run.
- **Holds.** Each window takes three holds: prepare, import and discard. Each hold restarts the cell, runs the start-up scans and stops any filing and offload in flight. The agent may defer the discards to the end of the series, which leaves two holds per window while it runs. A deferred discard keeps the window's text staged, and the budget counts it through used.
- **Rehearsal.** The rehearsal runs on a local-storage cell, where admission publishes no limits. It sizes the cell or its large file from the band, and records which. Its store is a sandbox bucket in the existing B2 account, with a bucket-restricted key. This was decided on 2026-10-30 under the operator's standing rule for reversible choices; the operator may revert it. The operator creates the bucket and key before task 5.4.16. Pull request 2216 lands first. It keeps a proven runtime ready while its recall catalogue lags, so the cell stays ready during a large apply.

**Choice 2, senders in slice 1 (confirmed by the operator on 2026-10-30): the CLI only.** Exomem specifies only the cell's contract, in `adoption_studio status`. It holds the window budget, the reachable budget, the effective file limit, the inputs of the classification rule and the remaining allowance. The reachable budget is the window budget after filing, offload and eviction free all that they can. MCP tools drive start, status, filing and discard for every agent. An agent without a shell gives the person the CLI command. Home windows need a Substrate change of their own, so this change states no Home behaviour.

**Choice 3, the largest single file (confirmed by the operator on 2026-10-30): 5 GiB, with streaming hydration.** It replaces "1 GiB now, streaming later". The largest offloadable file is the smallest of three bounds:
- the object bound less the `age` overhead. The chart default of `mediaEdge.maxObjectBytes` rises from 1 GiB to 5 GiB plus 2 MiB. The overhead of a 5 GiB file is about 1.25 MiB: 16 bytes for each 64 KiB chunk, and a header;
- the volume fraction × the volume size. The fraction is a store-policy value, 0.25, so the fixed budget terms stay near half the volume;
- the hydration capacity less the `age` overhead, because offload reserves its ciphertext from that capacity.

On a 20 GiB cell the largest offloadable file is 5 GiB, and a larger file files by rename and stays resident. The cell reports this value in status, as today.

A 20 GiB cell does not land every 5 GiB file during an import:
- The band below the growth threshold is about 5.5 GiB on a 20 GiB cell: 16 GiB less the 10.5 GiB of fixed terms.
- Every use that eviction cannot free sits in that band too. That covers notes, sidecars, indexes, derived state, resident files, staged text and recent owner reads.
- With 2.5 to 3.5 GiB of such use, a 20 GiB cell lands files of only 2 to 3 GiB.
- A larger cell lands larger files. A 40 GiB cell would have about 21.5 GiB of band, so a 5 GiB file would land. That size is above the 20 GiB volume cap that this change keeps, so it needs that cap raised for importing cells. A raised cap costs density per host, because each node reserves twice its largest cell per concurrent backup (D6). It also costs restore time, which grows by about 16 minutes for each 10 GiB (D4).
- The cell therefore reports the effective file limit, and the sender refuses a larger offloadable member before upload. This was decided on 2026-10-30 under the operator's standing rule for reversible choices; the operator may revert it. The operator's 5 GiB stays the upper bound.

- **Streaming hydration (slice 1).** Hydration downloads and decrypts in one stream into one temporary file in the original's folder. It checks the ciphertext digest, the `age` exit status and the plaintext digest, then links the file into place with no replace.
  - Peak use is then about X for a file of size X. Today it is about 3X: `_hydrate_reserved` writes a ciphertext temporary file and a plaintext temporary file, then copies the plaintext into place. `_reserve` then counts plaintext bytes only.
  - The download and the decryption run outside the vault guard. Only the link runs under it, as today's copy into place does. held_fs already installs its publish temporary with a no-replace link, so the link needs an entry point for a temporary that is already written.
  - The temporary file has the reserved publish name, which scans ignore. A stop leaves nothing at the original's path.
  - A stop can leave the temporary file, up to 5 GiB, and holds stop the runtime three times per window. Today's hydration temporaries are unnamed, so a stop frees them. held_fs removes its publish temporary only while the process lives, and nothing sweeps the reserved name.
  - The hydration therefore records the temporary file's name in its reservation row (simplest choice, marked). Each runtime start, and each later reservation, clears the rows of dead processes and removes their files. Only the recorded rows are read: a start-up sweep of the whole Knowledge Base would be unsound at 50,000 files.
  - A reservation row names its process by ID and start time. A restarted container reuses process IDs, so an ID alone can keep a dead row, its bytes and its file.
  - `age --decrypt` already reads a stream and authenticates each chunk, so decryption needs no change.
- **Downloads (slice 1).** The download routes serve the hydrated original from a held read handle, with no private copy. Today `stage` in `artifact_recovery` copies the original to a private file on the volume, and refuses a file above half the free space. The route compares the file's version after the last byte, as the copy does today.
  - When the file matches its recorded cache identity, the route does not hash it. Hydration and `recover_cache` record that identity. Hashing every response would read 5 GiB again for each seek of ranged playback.
  - Otherwise the route hashes the file once before the first byte. An original with no cache identity, such as one that was never offloaded, is hashed on each request, as today.
  - A ranged read from the store is not simpler, because the `age` CLI cannot decrypt a byte range.
- **Offload: a gap in slice 1 (decided on 2026-10-30 under the operator's standing rule for reversible choices; the operator may revert it).** The operator's ruling asked for streaming read-back and streaming upload. Slice 1 streams read-back only. Offload still encrypts into a ciphertext temporary file, which is unnamed, so a stop frees it. Streaming upload meets an obstacle:
  - The cell's upload declares the ciphertext size and SHA-256 before the body. The edge refuses an upload without them, and it writes the digest into the large file's info before the first part. B2 sets a large file's info only at its start.
  - The `age` CLI picks a random file key, so the digest is unknown until the encryption ends.
  - Offload therefore reserves the bytes of its temporary file from the hydration capacity, and waits when too few bytes are free. The window budget then needs no separate offload term.
  - Offload takes no hydration slot. With `max_hydrations` at 1, a slot held by offload would refuse every owner read of an evicted original during an import.
  - The cost of the gap: offload and owner reads compete for the capacity, and each offload writes and reads its ciphertext once more. The gap does not change the band, because the capacity must hold a 5 GiB hydration anyway.
  - Task 5.4.18 holds the edge change that removes the temporary file. The operator can pull it into slice 1.
- **The edge.** The edge already uploads in parts. For a 5 GiB object its part is 8 MiB, the largest of its configured part, B2's minimum part and the size over 10,000. It therefore sends about 641 parts, within the 10,000-part limit. Its one-hour limit on a whole request needs about 1.5 MB/s for 5 GiB. Task 2.1 and the rehearsal measure the transfer rate.

The cost on a 20 GiB cell with 5.25 GiB of hydration capacity and `max_hydrations` at 1:
- The fixed budget terms take about 10.5 GiB of the 16 GiB below the growth threshold. They are the reserve, the hydration capacity and one placed copy. Windows are therefore smaller during an import: at most about 5.5 GiB less use.
- A large offload holds bytes of the hydration capacity while it runs, so a large owner read can wait behind it.
- During an import, the effective file limit on a 20 GiB cell is often 2 to 3 GiB, not 5 GiB.
- While a series waits, eviction on demand takes the owner's eligible originals, unread ones first, and later reads hydrate them again.

**Read-back space (confirmed by the operator on 2026-10-30).** Importing test cells get 5.25 GiB of hydration capacity, with `max_hydrations` at 1. It holds one streamed read-back or one offload of a 5 GiB file, with 0.25 GiB beside it for small offloads. Each extra GiB would let more work run beside a large file, and would make every window 1 GiB smaller. Under eviction on demand, the everyday target and margin need no change for imports.

**Choice 4, test cells only until task 5.5 (confirmed by the operator on 2026-10-30).** Imports larger than the volume are canary-only until task 5.5 lands, and until trash, move, delete and export work without hydrating whole trees. Today each of these hydrates every original of the tree it touches. One-way door: a cell that stores more media than its volume holds can never drain its store, because its evicted originals do not fit back.

**The retained store (confirmed by the operator on 2026-10-30).** Such a cell keeps its media key, edge credential and edge egress after its store is turned off, so its media stays readable.
- Offload stays off. Eviction keeps running, so that a hydrated copy leaves the volume again. The cell reports the shortfall in bytes, and it drains once the shortfall reaches zero.
- cellctl keeps the key, the credential, the egress and the active-set entry until a report from the cell shows no evicted original. The nightly backup Job carries that report, beside the use that it already reports to cellctl (simplest choice, marked).
- With no report, cellctl keeps them. Removing them would make every evicted original unreadable. Keeping them too long only leaves a drained cell's edge access in place until its next report.
- Only a report that the cell captured after the store was turned off counts. An earlier report can show no evicted original although eviction ran after it. cellctl records when it turned the store off, and the report carries its capture time (simplest choice, marked).
- The report says unknown, never 0, when its census of sidecars is incomplete. cellctl treats unknown as evicted originals present.
- Cell deletion and key retirement override this rule. When the cell row becomes `deleted`, the deletion steps of D9 remove the active-set entry and retire the keys without a report.
- cellctl renders such a cell in a third mode: key, credential, egress and active-set entry, with offload off. The active set therefore includes cells that the edge no longer selects. Today it holds only selected cells.
- Today cellctl does otherwise. It gives a cell that the edge stops serving a retained store at once: the store identity, disabled, with no key, credential or egress. The drain requirement already kept hydration until no original is evicted, so the code and the spec disagreed before task 5.4. Task 5.4.15 changes the code.
- The cost: a turned-off cell that cannot drain keeps the posture of a served cell. The edge still accepts its credential for writes (see Open questions).

**The network wall follows a namespace label (decided by the operator on 2026-10-31).** In the code, the retained store above is the `draining` mode. The code's `retained` mode is the store identity alone, which a cell keeps after it drains. Before this decision, the edge's ingress and cellctl's admission both followed `mediaEdge.cellIds`, so a draining cell lost its network path to the edge when it left the list.
- cellctl sets the label `exomem.io/media-edge-access: "true"` on a cell's namespace while the cell's store is served or draining.
- The edge's ingress admits cellctl and the runtime pods of cell namespaces that carry the label. cellctl's admission lets it give a runtime edge egress only in such a namespace.
- `mediaEdge.cellIds` and the fleet switch only choose which cells the edge serves.
- Removal order, in apply order: the label leaves first, then the credential, then the active-set entry. One render drops the label and the credential, and it applies the namespace before the Secret (simplest choice, marked). The active-set entry leaves at the start of the next pass.
- The cost: the wall holds no operator list. cellctl sets the label, and cellctl already holds the signing key, so the list did not confine a compromised cellctl.

**The label reaches every served cell at once.** The chart keys the wall on the label in the same release that brings the label, so a served cell's namespace can lack it until the cell's next re-render. cellctl therefore applies the label on any pass that finds a served or draining cell's namespace without it, apart from the cell's restart and digest cadence. That apply sends the namespace whole, so it drops none of cellctl's other labels.

**The chart refuses to turn the edge off while the active set lists a cell.** Without the edge settings, cellctl can sign no credential, so a cell that still drains would lose access to its evicted originals.
- An upgrade with `mediaEdge.enabled=false` reads ConfigMap `exomem-media-edge-active-set` with `lookup`. It fails while `active-set.json` lists any cell, and draining cells stay listed. It also fails when the set exists but cannot be read.
- The upgrade fails before it changes anything, and the check runs only when someone deploys.
- `helm template`, a client-side dry run and `helm rollback` skip `lookup`, so they skip the check. The runbook says so.
- cellctl does not check this at startup. A startup check ran on every reconnect, so one record merged to unknown made a failover crash-loop cellctl for every cell.

Options considered:

| Option | Holds | Agent between windows | Large media | Verdict |
|---|---|---|---|---|
| F: windows, filing by rename | 3 per window, 2 with deferred discards | required: the sender waits for filing | renamed, never copied; offloaded up to the largest offloadable file | recommended |
| B: windows, store-backed stage, filing by reference | 3 per window | not required: the upload finishes before filing | stored before filing, never copied | alternative when the upload must finish before filing |
| A: windows, files copied | 3 per window | required | copied; members above the 200 MiB ceiling stay in the residual | rejected: each window needs twice its bytes, and F files the same members with no copy |
| R: the runtime streams one archive from the media bucket through the edge | about 0 | no | stored, never copied | follow-up if holds hurt |
| P: per-file gateway upload (tus) | 0 | no | caps of 100 MiB and 2 GiB | rejected for now; depends on the paused gateway work (task 5.3) |
| One archive unpacked in parts | n | not applicable | not applicable | rejected: a resume decrypts the prefix again, and the client holds the whole ciphertext |

B needs a stage reference in cell-local state, an exception to D2, stage offload, filing by reference, a retention-manifest change and a drain rule for stored staged members. R needs a new C5 transport, media-bucket multipart in cellctl, a D9 import object and a runtime unpacker.

The recommendation changes in three cases:
- The rehearsal measures hold downtime above about 5 minutes per window, or more than about 20 windows for typical sizes. Move to R and keep filing by rename.
- The rehearsal shows the sender waiting on filing for longer than the upload itself takes. Move to B.
- The CLI cannot call cell tools through the gateway. The agent then mediates each window: it reads the budget and passes it to the CLI.

Delivery slices:
1. **Slice 1.** Exomem: the member identity, filing by rename, the classification rule, and M1 to M4 with eviction on demand and the claim order. It also covers one census per batch, budget status, CLI windows and tool text. It adds streaming hydration, downloads without a copy, offload within the hydration capacity and the 5 GiB edge bound. The local-storage rehearsal proves it.
2. **Slice 2.** Home windows in Substrate, and a per-kind space factor if the rehearsal shows that factor 2 makes windows on attachable cells too small.
3. **Slice 3.** Streaming offload through the edge change of task 5.4.18, one key per series to pipeline uploads, and R if the rehearsal shows that holds hurt. The operator can pull task 5.4.18 into slice 1.

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
- **Holds per window (D12).** Each window takes three holds, or two with deferred discards, and each hold restarts the cell. A large import therefore costs the owner several short outages. The rehearsal measures the downtime of all three holds, and R is the follow-up.
- **The source is the second copy (D12).** Until filing ends, the person's source folder is the only other copy of an unfiled member. After a discard, it is the only copy. The CLI reports done when the last window lands, not when filing ends. Tool text and the CLI say to keep the source while a landed window has an unfiled member or a residual record lists a member. Status reports both.
- **A small window budget (D12).** Take a 20 GiB volume with 5 GiB files and 5.25 GiB of hydration capacity. The fixed terms then take about 10.5 GiB of the 16 GiB below the growth threshold. They are the reserve, the hydration capacity and one placed copy. Windows are then a few GiB, and a large series needs many windows and holds. The effective file limit is often 2 to 3 GiB, so the sender refuses a larger offloadable member before upload. The rehearsal records the window count.
- **A colder cache during a series (D3).** Eviction on demand takes eligible originals below the everyday stop point while a series waits. Owner reads during the series then hydrate more often. The demand lapses 15 minutes after the sender stops renewing it.
- **The one-way door (D12).** A cell that stores more media than its volume holds can never drain its store. It stays bound to the edge and the media key, and its media stays readable after its store is turned off. Imports larger than the volume stay on the canary until task 5.5.
- **Replay on three fields (D8).** Replay of a renamed but uncommitted member trusts inode, size and `mtime_ns`. Slice 1 keeps this risk. Only the cell's own processes write at a Knowledge Base path, and offload's digest check catches a change in an offloadable file.
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
- **Windows (D12).** Rehearsal 5.4.16 measures the hold count and restart time per window, the window count, the filing rate and the census time per batch. It also measures the derived-state size and start-up scan cost at the final Source count. It also settles whether a note links to an attachment that an earlier window carried. The first CLI test against a real cell settles whether the CLI's OAuth client may call cell tools through the gateway.
- **The sandbox store for the rehearsal (decided on 2026-10-30 under the operator's standing rule for reversible choices; the operator may revert it).** The rehearsal uses a sandbox bucket in the existing B2 account, with a key restricted to that bucket and stored in the operator's secret store.
  - The other option was a disk-backed server built from `tests/fake_b2.py`, inside the rehearsal cluster. It proves less than a real bucket, and it adds test tooling. The rehearsal's existing S3 double cannot serve instead, because the edge speaks the native B2 API.
  - Operator ask (pending): the operator creates the bucket and key before task 5.4.16.
- **Two drafting defaults.** Each was decided on 2026-10-30 under the operator's standing rule for reversible choices; the operator may revert it:
  - The effective file limit (D12, choice 3). The cell reports the largest landable file live, and the CLI lists and refuses a larger file before upload. The other options were a lower volume fraction, or a volume cap above 20 GiB for importing test cells. A raised cap costs density per host and restore time.
  - Streaming upload (D12, choice 3). It stays a gap in slice 1, and task 5.4.18 stays optional.
- **The demand lease (D3).** The CLI renews its demand at least every 5 minutes, and the lease lasts 15. The rehearsal settles whether that is long enough.
- **Re-hash above the ceiling (D8).** A member whose identity no longer matches is hashed once, and that read keeps the adoption ceiling. After a backup restore during a series, a large member therefore goes to the residual and is imported again. Whether the re-hash should run in the background instead is settled by how often a restore meets an unfiled series.
- **Replay without the change time (D8).** Replay of a renamed but uncommitted member trusts inode, size and `mtime_ns`, and slice 1 keeps that risk. Task 5.4.5 decides whether replay should record the binary's stat after the rename and compare all four fields. It also settles how the file watcher and media scans treat an unbound binary in that state.
- **One census per batch (D10).** The batch census assumes that offload publication is the only path that adds a referenced version while the runtime serves. It reuses the census across guard releases. Task 5.4.9 checks every writer that can bind a version before the batch census replaces the per-member census.
- **The claim cap (D3).** The worker caps the claimed but unfinished jobs for which it hydrated the original at `max_hydrations`. Jobs whose originals stayed on the volume are not counted, and their pins can delay eviction on demand. The rehearsal settles whether those pins stall a series long enough to need a bound of their own.
- **Two series at once (D12).** Each series keeps its own demand, and the sender reads the budget again right before each create. Two senders can still create windows from one reading, and the Job then checks free space only. Settled by whether the rehearsal or the canary ever runs two series at once. A Job-side budget check is the fix if one does.
- **The acknowledgement action (D12).** `adoption_studio(action="acknowledge")` with explicit paths is the simplest choice for clearing members of a residual record. The CLI uses it to clear resent members, so it also depends on the CLI calling cell tools. Settled with the tool text in task 5.4.8.
- **The series root (D12).** A resend reuses the first series' file, so the collection-root rule sees the same path list. A change to that rule between the first series and the resend could still move the root. Settled by the first change to the rule; the series file would then carry its root.
- **Streaming offload (D12).** Offload keeps an unnamed ciphertext temporary file in slice 1, because the edge needs the digest before the body. Task 5.4.18 specifies the deferred-digest edge change. Leaving that task optional, in slice 3, was decided on 2026-10-30 under the operator's standing rule for reversible choices; the operator may revert it.
- **Writes from a retained store (D12).** A turned-off cell that cannot drain keeps its edge credential, and the edge accepts writes from it as from a served cell. The cell's offload is off, so only a compromised cell would write. Settled when the edge's active set can mark a cell as read-only.
- **The unbound binary after a stage walk (D8).** A discard that walks the stage cannot find a member that was renamed but not committed. Settled in task 5.4.5: the discard refuses with `IMPORT_RENAMES_PENDING` while the member's rename hold is live (decided on 2026-10-31 under the operator's standing rule for reversible choices; the operator may revert it).
- **Substrate's usage reading (D10).** Admission sizes growth from its own usage reading, which can lag the cell's. A reading above the cell's live use can still grow an attachable cell for a window that the sender sized correctly. Settled by the first windowed import on an attachable cell, or by a C5 option that refuses instead of growing.
