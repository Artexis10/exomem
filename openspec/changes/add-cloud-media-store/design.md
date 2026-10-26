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
- Re-rooting key custody. `make-cloud-cells-operator-resistant` owns that work (D5).
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

A sidecar gains one governed descriptor key: the stored-object reference. It holds the store identity, the key version and the object name. An original is in exactly one state:

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

- **Candidates:** originals in the cached state, idle for at least the minimum idle age, with no pending or running media job, hydration or download.
- **Order:** largest first within the eligible set, ties broken by longest idle. This frees the space with the fewest evictions.
- **Idle time:** the cell's own last-read record, not file access time.

Small originals such as scans, photos and documents are expected to stay cached. This is an expectation that canary data checks, not a guarantee under all pressure.

**Hydration** brings an evicted original back. The cell fetches the ciphertext through the edge, decrypts it to a temporary file, verifies size and SHA-256 against the sidecar and renames it into place. A mismatch fails closed and leaves the path empty.

A replaced file gets a new inode and change time, and restoring the modification time cannot restore the change time. So hydration records a *cache identity* for the file in cell-local state: its inode, change time, size and verified SHA-256. Reconciliation, the governance companion check and the file watcher treat a file that matches its cache identity as unchanged. None of them rewrites the sidecar's recorded provenance, which keeps describing the original as it was ingested. Eviction and hydration are managed operations, so neither publishes a deletion or creation to the deletion fan-out.

Hydration concurrency and temporary space are bounded, so a bulk re-encode or an export cannot fill the volume.

### D4. Encryption in the cell

The cell encrypts each original with `age` to its own X25519 recipient before upload. `age` streams 64 KiB authenticated chunks, and the cell image already ships it for imports.

The object name is HMAC-SHA-256 of the original's SHA-256 under a per-cell name key. A bucket listing then cannot confirm that a tenant holds a known file. Identical originals inside one cell share one object.

On the media-storage path, the media edge and the provider see only ciphertext. Playback and export are other paths, and D8 and the table below cover them.

### D5. Key custody follows the cell's key hierarchy

cellctl mints a per-cell media key bundle: the `age` identity and the name key. It stores the bundle wrapped with AES-GCM under the same master key that wraps the backup key, with the cell ID, purpose and version bound as additional data. It renders the bundle into the cell's Secret.

Only the runtime and the export packing step (D8) mount the bundle. Neither has public egress. A restore needs no media key, because evicted originals stay in the store. The edge never receives it.

The media key is one more per-cell data key in that hierarchy, and it has no custody path of its own. `make-cloud-cells-operator-resistant` re-roots the backup, volume and media keys under one per-cell root key. That change also owns the tenant-locked state, in which the media key is unavailable and media work reports `locked`.

### Who can read an original

| Where the original is | Who can read the plaintext |
|---|---|
| In the store, current or hidden version | Nobody without the media key. The provider and the edge hold only ciphertext and keyed names. |
| The media key | The operator, who holds the master key that wraps it. After the re-root, whoever can unwrap the cell root key can read it. That is still the operator in standard mode, but not in tenant-locked mode while the cell is locked. |
| In the cell during processing, hydration or playback | The cell runtime. The operator could reach it only through audited break-glass access. |
| On its way to the owner | The public TLS edge and the gateway, which see requests and responses in transit, as the privacy copy already discloses. |
| In an export | The packing step decrypts without public egress (D8). The archive itself then follows the export contract: today a plain ZIP over presigned parts, under `add-cloud-owner-export`. Ciphertext-only delivery belongs to the operator-resistant change. |

The privacy copy states this custody truthfully (`cloud-operator-access`).

### D6. A media edge holds the only bucket credential

A new in-cluster service, the media edge, moves ciphertext between cells and the media bucket. It follows the artifact broker's shape: its own Deployment and Service on a literal ClusterIP, Recreate rollout, and a canary list plus a fleet switch.

- **Network.** A selected cell's runtime gains one egress edge, to the edge's ClusterIP and port, with no DNS. The export packing step (D8) reaches it on the same terms and has no other egress.
- **Authentication.** cells carry no ServiceAccount token. cellctl signs a per-cell edge credential with its own signing key, binding audience, cell ID and version, and renders it into the cell's Secret. The edge holds only the public key. It maps the credential's cell ID to that cell's object prefix and accepts no prefix from the request.
- **Revocation.** A valid signature is not enough. cellctl publishes the active set: each selected cell's ID and current credential version. The edge accepts a credential only while its cell and version are in that set. Rotation bumps the version, and the old credential stops working once the edge loads the new set. The edge reports the set generation it has loaded, so cellctl can observe when a revocation takes effect.
- **Operations.** Multipart upload with declared size, ranged read, head, listing of the caller's own prefix, and hide. The edge's bucket key cannot delete file versions. Only cellctl's key-management authority destroys versions (D9).
- **Quota.** The edge refuses an upload whose declared size exceeds the cell's remaining media allowance (D10).

Alternatives:
- **Projected ServiceAccount tokens.** These would break the cell's no-token rule.
- **A forwarding proxy to the provider.** Its endpoint is shared by all of the provider's customers, so it would be an exfiltration channel.
- **Folding this into the artifact broker.** The broker fetches arbitrary public URLs; storage credentials do not belong behind that surface.

Whether the edge is custom code or a maintained S3 gateway with per-caller prefixes (versitygw, `rclone serve s3`) is settled by the spike in task 2.1.

### D7. A separate, versioned bucket on the backup provider

Media lives in its own B2 bucket. It is not under the backup prefixes, so the edge's bucket-wide key cannot reach restic packs.

The bucket keeps versions. A hidden version is destroyed only when every retained backup snapshot of the cell is newer than the moment the version was hidden (D9). No fixed age is used. restic keeps the latest snapshot of each of the last 4 weeks that *have* snapshots, so a stopped or rarely backed-up cell can keep a months-old snapshot. Every restorable backup therefore still resolves its references. The only lifecycle rule cancels unfinished multipart uploads. No object lock is set, so cell deletion can complete.

The same provider holds backups and media. A second copy at another provider was considered and rejected on cost for now. The revisit trigger is any of:
- the fleet's stored media passes 1 TB;
- a provider incident;
- a paying tenant asks for it.

### D8. Data paths

- **Arrival.** Files arrive as today: capture, attachments, imports, uploads. After the vault commit, an offload job on the existing durable media job ledger encrypts and uploads the original, confirms size and digest with a head, then writes the reference into the sidecar.
- **Live capture.** The gateway proxies resumable (tus) uploads to the cell's existing upload sessions. This lifts the Cloud exclusion of those routes by its recorded condition.
- **Processing.** Extraction runs at ingest while the original is local. Eviction never selects an original with pending work. Re-processing and CLIP re-encoding hydrate first.
- **Playback and download.** The gateway serves a download route behind the owner's signed-in session. It forwards to the cell, which hydrates if needed and streams the original with range support. A tool result may carry this link, because it grants nothing without the owner's session. Local agents use `exomem cloud download` over OAuth, which verifies size and SHA-256 before it renames the file into place. No tool result carries a bearer link.
- **Export.** Export runs in two steps, so the media key never shares a pod with public egress:
  - A *packing* step mounts the media key. It reaches only the media edge and builds the archive on the export staging volume, writing evicted originals byte-identical. Each manifest SHA-256 must equal the sidecar's.
  - A separate *upload* step without the media key transfers the archive as today's export does.

  The archive's format and delivery stay those of the export contract. Export of more media than the staging volume can hold is an open question; until it is settled, no cell other than the canary may store more media than its export can stage (D11).
- **Restore.** restic restores sidecars and any cached originals. Evicted originals stay in the store, because no version that a retained snapshot references is destroyed (D9).

The job ledger is the job architecture. A Postgres-backed task queue such as Procrastinate was considered and does not fit: a cell runs on SQLite and holds no Postgres.

### D9. Deletion

Collection is new lifecycle logic in two steps. No existing sweep supplies it: the archive member pool's sweep removes only abandoned temporary files.

- **Hide.** A collector in the cell lists the cell's own prefix through the edge. It hides each object that neither a sidecar in the vault (trash included) nor an offload in flight on the job ledger references. The collector and offload run on the same ledger, so a confirmed upload whose reference is not yet written is never hidden. Two sidecars that share an object keep it until both are gone. An offload of identical bytes whose object is hidden uploads a new current version.
- **Destroy.** After each successful prune, cellctl records the time of the cell's oldest retained snapshot. With its key-management authority, it destroys only hidden versions that were hidden before that time. An owner's deleted original therefore leaves the store exactly when the last backup that could restore it is pruned, as deleted vault files leave restic today.
- **Cell deletion.** cellctl works in this order:
  1. Remove the cell from the edge's active set.
  2. Observe that the edge has loaded that set.
  3. Abort every unfinished multipart upload under the cell's prefix.
  4. Delete every version under the prefix and destroy the wrapped media key bundle.

  It reports `deleted` only after it observes that the versions and the bundle are absent. A revoked credential cannot repopulate the prefix after the absence check.

### D10. Quota: an included media allowance

Each cell has a media allowance, separate from its volume size. It is a configured value in the one limits source; its first value is 100 GB. Paid blocks follow the existing paid-storage observation.

- **Metering.** cellctl meters stored bytes from the bucket listing.
- **Admission.** An upload or import whose declared media bytes exceed the remaining allowance is refused before any byte arrives, with a code that names the limit.
- **Overflow.** An original that the allowance cannot cover stays local and is reported in status.
- **Two meters.** Status and the Home display report two figures, each named for what it counts:
  - *Vault storage:* notes, sidecars, extracted text, indexes and datasets on the volume, against the volume size. It excludes cached copies of stored originals, which would otherwise be counted twice.
  - *Media storage:* each stored original counted once, against the allowance.

  Vault storage is provisioned per cell and costs more per gigabyte; media storage is billed by the bytes stored. A meter that cannot be read shows as unknown, never as zero.

The 20 GiB volume cap is unchanged. It now bounds notes, sidecars, indexes, datasets and the cache, not media.

S2 import admission is unchanged by this change. A later streaming-import slice splits it in two:
- **Volume admission:** current use plus twice the largest declared file plus the reserve.
- **Store admission:** total declared media bytes against the remaining allowance.

### D11. Rollout and sequencing

1. Ship core behind the store being disabled. Tasks 3.x prove the states and eviction against a filesystem-backed store.
2. Ship the bucket, edge, keys and policies with the edge off.
3. Enable one canary cell. Back-fill: upload and verify every bound original, then evict to target.
4. Enable the fleet switch only after export can deliver every selected cell's media (task 5.5). Until then, only the canary may store more media than its export can stage.

Phases 1–6 of `move-cloud-cells-to-local-storage` do not depend on this change. Phase 7 does for any media-heavy cell. Either this change's rollout finishes first, or a media-light cell migrates first.

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
- **Key loss.** Losing both the wrapped bundle and the master key loses evicted originals. The bundle shares the backup key's escrow, but more depends on it.
- **Sidecar edits.** An edit could drop or alter the reference. Mitigations: the descriptor key is governed, and a version the collector hides stays recoverable until no retained snapshot predates the hide.
- **Compromised edge.** It sees every cell's ciphertext and object names, can write and hide objects, and can deny service or run up storage. It cannot decrypt, destroy versions or reach a revoked cell. Mitigations: the edge holds no media key, version destruction stays with cellctl, and hidden versions remain recoverable.
- **Hydration storms.** An export or a CLIP model change can hydrate many originals. Concurrency and temporary space are bounded (D3).

## Open questions

Each question below names what settles it.

- **Eviction target, margin and minimum idle age.** Settled by two to four weeks of hydration records on the canary cell.
- **Edge implementation:** custom service or maintained S3 gateway with per-caller prefixes. Settled by the task 2.1 spike. The spike also confirms that the provider's hide operation works without version-delete capability.
- **Export of more media than the export staging volume can hold.** This covers staging, part limits and deadlines; Cloud export today sizes its total from the cell. It is settled with the owners of `add-cloud-owner-export` and gates the fleet switch (D11).
- **Direct presigned upload to the store for files above the 2 GiB upload-session limit.** Settled by the first such file.
- **Unbound binaries.** A file that was neither preserved nor matched by the extraction registry has no sidecar and stays on the volume. Whether discovery should give large unbound files a sidecar is settled by their byte share in canary vaults.
- **The archive member pool.** It stores content-addressed members in the vault. Whether it is in scope is settled by its byte share.
- **Chatbot-native file delivery.** Settled per client, once its support is confirmed and the transfer it needs is known.
- **Key rotation, video transcoding for playback, and paid block prices.** Each is decided when its first need appears.
