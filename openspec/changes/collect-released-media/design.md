## Context

`add-cloud-media-store` writes every bound original to a versioned media bucket. A cell keeps a reference to an exact stored version in each sidecar. The bucket holds each version until a mechanism destroys it, and the contract gives that job to `cellctl`'s key-management authority (its D9 and task 4.6).

Three facts shape this change:

- An owner who deletes a recording leaves its ciphertext in the bucket. The owner pays for storage that no sidecar and no retained backup can reach.
- Restic keeps the latest snapshot of each of the last 4 weeks that have snapshots. A stopped cell can keep a months-old snapshot, so a clock cannot decide when a version is safe to destroy. `add-cloud-media-store` D7 already rules out an age cutoff.
- A version-deleting key in the controller lets a compromised controller destroy the media of every cell. `contain-cloud-controller-compromise` exists to limit that class of damage.

The object name is today an HMAC of the original's SHA-256 under a per-cell name key. The same bytes added twice therefore get one name and two versions. A bucket rule that removes non-current versions would destroy the first, which a retained snapshot can still reference.

Provider behaviour in this document marked † is not yet confirmed on the provider. Task 1 confirms each such point on a disposable bucket and records the result here.

## Goals / Non-Goals

**Goals**
- Remove a stored version once nothing can reference it: no live sidecar, no Trash entry and no retained backup snapshot.
- Hold no version-deleting key in the cluster.
- Decide with reachability only. No clock, age or timestamp decides a deletion.
- Make a wrong deletion visible and reversible for a grace period.

**Non-Goals**
- Changing key custody, encryption, quota or the edge's authentication. `add-cloud-media-store` and `cloud-cell-key-custody` own them.
- Collecting unpublished orphans before cell deletion. Cell deletion already removes the whole prefix.
- Giving the operator a per-original undo after the grace period. The grace period is the undo.

## Decisions

### D1. A stored name receives one upload

A new protection record gets a fresh random 32-byte name, written as 64 hexadecimal characters, which the edge accepts. The record stores it in its `object` column. A retry of the same record reuses the record's name. This keeps nonce recovery and the edge's serial-write hold. After a record retires, no upload uses its name again.

A retry uploads again only when no confirmed version exists, so no sidecar references the earlier attempt. The earlier attempt is an unreferenced version, and the lifecycle rule (D2) removes it.

Reason: with keyed names, a re-add after a delete uploads a new version under the old name, and the old version becomes non-current. A re-offload after an absent HEAD does the same. With unique names, a referenced version stays current until a hide (D3) changes it.

This breaks no reader:
- The name key is read only where a name is derived. The derivation stops for new records.
- A reuse copies the reference of a live sidecar, name and version together.
- Allowance metering deduplicates by (store, object, version), so it does not depend on how a name is built.
- Hydration and HEAD read (object, version).

Privacy gain: identical plaintexts no longer share a name that the provider can see. Object sizes stay visible, as in `add-cloud-media-store` D4. The change amends the naming statement in task 3.3 of that change.

### D2. One bucket-wide lifecycle rule deletes hidden and superseded versions

The media bucket keeps its one rule that cancels unfinished multipart uploads. The rule gains `days_from_hiding_to_deleting`, set from `var.cloud_media_condemned_days`. The variable has no default for a stack with the media bucket, so a plan of that bucket without a value fails. A stack without the media bucket needs no value, so its plans do not wait for a grace that it never uses. No rule hides a version by age.

† The provider deletes a version that a hide or a newer upload made non-current once the configured days pass. It keeps a current version. Task 1 confirms both, and confirms that deleting a hide marker keeps the version.

With D1, a hide is the only way to make a referenced version non-current. The rule therefore removes only what D3 and D5 condemned, plus unreferenced duplicate attempts.

No process in the cluster deletes versions. The edge key keeps `readFiles` and `writeFiles`. A hide needs `writeFiles`, which that key already holds, so the key gains no capability.

### D3. The backup Job condemns, through the edge, with its own credential

The edge gains one operation, `condemn`, which hides a name. The backup Job calls it with a collector credential.

- **Audience.** `cellctl` signs a collector credential with the audience `exomem-media-edge-collector`. The runtime audience stays `exomem-media-edge`. The edge refuses `condemn` for a runtime credential. It refuses every other operation for a collector credential.
- **Prefix.** The edge derives the prefix from the credential's cell ID, as for runtime credentials. A collector credential can condemn only names under its own cell's prefix.
- **Active set.** The active set that `cellctl` publishes gains a `collectors` map. A retained cell, one whose store is turned off but whose store identity still exists, keeps its collector entry. Its backups continue to collect.
- **Idempotence.** `condemn` hides a name only when a HEAD by name still shows the name. A repeated call hides nothing.
- **HEAD status.** HEAD gains a `condemned` status. A version is condemned when it exists while its name is hidden, or when the name's current version differs from it.
- **cellctl.** `cellctl` stays list-only. It signs credentials and lists. It has no new key capability.

Placement: `cellctl` renders the collector credential only into the backup Job. The runtime never mounts it. The backup Job gains egress to the edge's address and port, and an admission rule for that peer. The edge-off check refuses while `collectors` is non-empty, so a turn-off cannot silently stop collection for a retained cell.

### D4. Detection and rescue

Every owner deletion now ends in a hide. A single hide is therefore normal, and an alert on each would carry no signal.

- **cellctl** reports the hidden names of each cell, from its list-only view. It raises no alert for a single hide.
- **The out-of-cluster key watcher** gets a list-only key limited to the media bucket. It counts the hide markers that appeared since its last run, and it alerts when the count exceeds a pinned budget. Its output is value-free: it carries a count and a reason token, never a name.
- **Grace.** The grace period N must exceed the watcher's interval plus the operator's response time. An alert then arrives before the first deletion of a burst.
- **Rescue.** The operator deletes the hide marker with the out-of-cluster cleanup key. The version is current again, with its content unchanged. A rescue is for a name that a sidecar or a protection record still holds, such as a hide by a compromised edge. A hold never condemns such a name, because candidates are only released names that no manifest references or protects (D5). So the next hold does not hide it again. A released name that nothing references is a correct candidate, and a later hold hides it again.

Honest cost: from this change on, any hide leads to a deletion after N days unless someone rescues it. Before it, nothing in the cluster could destroy a version. A compromised edge can now hide any name, because its key holds `writeFiles`. A compromised backup Job can hide names under its own cell's prefix. The budget alert and the grace period bound that exposure. They do not remove it.

### D5. Manifests, the release ledger and candidates

Reachability decides each deletion. Three records carry it:

1. **Release ledger.** A new append-only table in the cell. Before any reference to a stored name retires, the cell commits a release row for it with a full sync. The retiring paths are `forget_original`, the drop of an absent version, a replacement, an emptied Trash and an abandoned protection record. A crash between the release and the unreference keeps the reference. A missed release only leaks storage.
2. **Media manifest.** Each backup snapshot contains a manifest, inside the restic snapshot. It is encrypted with the cell password, and prune removes it with its snapshot. The backup computes it from the clone that restic backs up. The clone is mounted read-only, and the census walk of `add-cloud-media-store` task 4.5.1 needs a writable root, so the manifest reads the frozen tree directly. It is as strict as that census: any companion that it cannot read makes it unknown. It lists the referenced (name, version) pairs, Trash included, the names under a protection record and the released names. If the census is unknown, the manifest says so.
3. **Candidates.** After the backup and the prune both succeed, the Job reads every retained snapshot's manifest. A candidate is a name in the newest manifest's released names (`released_newest`) that no retained manifest references or protects. If any retained snapshot has a missing or unknown manifest, the Job condemns nothing for the cell. The Job calls `condemn` for each candidate.

No clock enters this. A candidate depends on which snapshots restic retains, and on nothing else.

The cell never evicts an original on the strength of a condemned reference, and it never reuses a condemned version for another sidecar. When the cell still holds the bytes, it offloads them again under a new name. This happens, for example, after an export or a restore that raced with a hold.

**How the conditions of `add-cloud-media-store` D9 hold under this design**

| Case | Why the name is not condemned |
|---|---|
| A months-old retained snapshot names the version | The name is in that snapshot's manifest, so the filter excludes it until prune removes the snapshot. |
| A sidecar is deleted between the clone and the backup | The cell releases the name after the clone, so the clone's ledger lacks the row and the name is not in `released_newest`. The manifest still references it. Nothing is condemned this hold. |
| An upload commits after the clone, and its protection record was synced first | The cell never released the name. The clone holds the protection record, the reference, or both, so the manifest protects it. |
| A reuse whose source sidecar was deleted before the capture | The reuser's protection record or reference names the version, so the manifest protects it. |
| A same-cell restore of the oldest retained backup | Its references stayed in the filter while that snapshot was retained. Names released after it leak, which is safe. |
| A retained snapshot has no readable manifest | The Job condemns nothing for the cell. |

## Rollout and operator steps

The canary cell may use the media store before this change ships. The fleet switch waits for this change and for task 5.5 of `add-cloud-media-store`.

The operator carries these steps:
1. Choose the grace period N and the hide budget.
2. Authorize the disposable-bucket check of task 1 with sandbox credentials.
3. Before the lifecycle rule applies, confirm that the canary shows zero names with more than one upload version. Otherwise re-offload those originals first.
4. Apply the durability stack from a saved plan.
5. In the same window, update `KEY_WATCHER_PINNED_SET` by read-modify-write of its live value. Coordinate with `contain-cloud-controller-compromise` first, because that change owns the watcher's pinned set.
6. Deploy the cell image, the edge, `cellctl` and the chart as one release, because the manifest, the credential and the operation depend on each other.
7. Confirm that the out-of-cluster cleanup key has `deleteFiles` on the media bucket, for rescue.

The undo is the grace period (D4). Removing a cell's collector entry stops all collection for that cell.

## Residuals

- Released names have no retention rule. A name stays in the release table after condemnation, so the table grows. If holds slow, add HEAD-driven pruning of rows whose name HEAD shows as condemned or absent.
- Each hold makes one re-condemn HEAD for every released name that no retained manifest names. The call is idempotent but not free.
- An unpublished orphan, an upload whose reference never published, leaks until cell deletion.

## Rejected options

- **`cellctl` hides with `writeFiles`.** This makes `cellctl` the second holder of a key that can hide, next to the edge. The candidates would also travel from the backup Job to `cellctl`, which adds a channel that a compromised Job could shape.
- **An operator script.** This puts a human in the routine loop of every cell's backup hold, and the human has no better evidence than the Job.
- **No collection until a later change.** Every deleted recording stays stored. The operator chose on 2026-11-06 to build collection before any cell other than the canary uses the media store.
- **An age cutoff.** `add-cloud-media-store` D7 already rules it out: a stopped cell keeps a months-old snapshot.

## Risks / Trade-offs

- **A wrong condemnation destroys a stored original after N days.** The mitigations are idempotent hides, the missing-manifest rule, the budget alert and the rescue path. A false negative in the release ledger only leaks storage.
- **The edge key can hide.** It already could, because `writeFiles` permits it. Before this change, a hide had no effect on retention. Now it does. The watcher's budget is the detector.
- **Every backup hold reads each retained manifest.** The cost grows with the number of retained snapshots.

## Open questions

- **Grace period N and hide budget.** The operator chooses them. The grace period must exceed the watcher interval plus the operator's response time.
- **Provider behaviour.** Task 1 confirms every point marked †, and also whether a hide of an already hidden name is an error or a no-op.
- **The name key.** Whether the per-cell name key can be retired once no cell holds a keyed name. Settled after the canary's names are re-offloaded or confirmed single-version.
- **Rescue of a released name.** D4 rescues names that something still references. A name that the cell released by mistake is hidden again at the next hold. Whether such a mistake needs a per-name exclusion list is settled by the first rescue.
