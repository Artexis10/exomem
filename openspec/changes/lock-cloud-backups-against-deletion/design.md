## Context

- **Repository.** Each Cloud cell backs up with restic to `s3:<endpoint>/<bucket>/cells/<cell_id>`.
- **Keys.** Its key comes from a per-cell object-store key with list, read, write and delete in that prefix. The controller creates those keys with a parent key that holds `listKeys`, `writeKeys`, `deleteKeys`, `listFiles` and `deleteFiles`.
- **Retention.** Restic's policy is `--keep-daily 7 --keep-weekly 4` (`--keep-hourly 24` added where hourly backups run), and `forget --prune` follows each backup.
- **Bucket contents.** The same bucket holds import and export staging objects under each cell's prefix. It has native versioning but no Object Lock.

## Goals / Non-Goals

**Goals**
- No running service, and no single leaked key, can remove a cell's backup data before a fixed retention date.
- Restic keeps working unchanged.
- Import and export staging objects keep their short lifetimes.

**Non-Goals**
- Confidentiality of backups. That is `contain-cloud-controller-compromise` D5.
- Protection against the hosting account owner closing the account.

## Decisions

### D1. A dedicated backup bucket

Object Lock's default retention applies to every new object in a bucket. If the lock covered the existing bucket, each staged import and each export would also stay for the retention period. That raises cost, and it keeps a tenant's export after the tenant deleted it.

The backups therefore move to a new bucket with Object Lock enabled and default retention set. The existing bucket keeps imports and exports.

### D2. Compliance mode, with locks kept 30 days ahead

- **Compliance mode.** No key can shorten or remove a version's retention in this mode, the account owner's included. Governance mode allows a bypass with a capability, and that capability would be one more key to protect.
- **Retention runs from creation, not from deletion.** The default retention sets each version's lock to its creation time plus N days. Restic keeps long-lived packs for stable data. A pack written 25 days ago with a 30-day default is locked for only 5 more days. If an attacker hides it today and the hide is noticed a week later, the pack is gone.
- **Locks are extended on a schedule.** Compliance mode allows a lock to be extended, never shortened. A daily job extends the lock of each current version in the backup bucket to today plus 30 days. So whenever a delete happens, every version that it hides stays locked for at least 23 days afterwards (see the 7-day rule below).
  - The job holds a key that can only list files and extend retention. In compliance mode, that key cannot shorten or remove anything.
  - It runs as its own small CronJob, with its own service account, in its own namespace, outside both the `exo-cell-*` namespaces and `cellctl`'s namespace. `cellctl`'s admission scope and RBAC reach neither its Job nor its key.
  - A missed run reduces the margin by one day, and an alert fires on a missed run.
  - It extends only versions whose lock ends within 7 days. That keeps the margin at least 23 days, with far fewer calls than extending every version daily.
- **An extension is irreversible, so the extender fails closed.** In compliance mode, nobody can undo a lock date that was set too far ahead. A bug that set years instead of days would lock every version, and its storage cost, for years.
  - The target is a fixed constant: today plus 30 days.
  - The job refuses any retention date more than 31 days ahead, and alerts instead of applying it.
  - Its key is admin-tier custody under `make-cloud-cells-operator-resistant` D6.
- **30 days.** This margin covers the time to notice a mass deletion and recover from it.
- **Cost.** One retention update per current version each time its lock nears expiry, across all cells, at the provider's per-call price. Task 1.2 measures it at fleet scale with the storage.

### D3. Deletes hide; the lifecycle removes after the lock

- **What restic sees.** A delete through the S3 API on a versioned bucket hides the file and keeps its versions. Restic sees a normal delete, and its `prune` works on the visible files.
- **Removal.** A lifecycle rule removes hidden (noncurrent) versions once their lock has expired. It never expires a current version, so live restic packs and the cell's escrow copy (`contain-cloud-controller-compromise` D5) stay until something hides them.
- **Minted keys stay harmless.** The provider limits a minted key's capabilities to a subset of its parent key's. The controller's parent key lacks `writeBuckets` and any retention-bypass capability, so no key that the controller can create can change the bucket's lock, versioning or lifecycle. Task 1.1 checks this rule on the provider.
- **The controller's keys.** In compliance mode, no key can remove a locked version. A per-cell key may therefore keep the delete capability that restic needs, because on a locked version that delete only hides. The parent key loses `deleteFiles`, so the controller removes nothing directly. Task 1.1 checks which capability a hide needs on this provider.
- **A deleted cell.** The controller hides the cell's files with its per-cell key, then deletes the key. The lifecycle rule removes the versions once their retention ends.

### D4. Recovery from a mass deletion

A runbook procedure lists a cell's file versions as of a point in time and restores the hidden versions of its repository. It then checks the repository with `restic check`. A rehearsal proves the procedure on a disposable cell whose repository was deleted.

### D5. Migration

1. New cells start in the locked bucket.
2. Each existing cell copies its repository with `restic copy` to the new bucket, one cell at a time.
3. Each cell then switches its repository.
4. The old repository stays until the cell's newest copied snapshot is 30 days old, then cellctl removes it.

## Risks / Trade-offs

- **Cost.** Hidden versions stay for 30 days, so storage grows by about 30 days of each repository's churn. Task 1.2 measures it on a real cell before the migration.
- **A mistaken backup cannot be removed early.** That is the point of compliance mode. The same rule holds a deleted tenant's backups until retention ends, and the deletion record states that date.
- **Enabling Object Lock.** Whether the provider allows Object Lock on an existing bucket does not matter, because D1 uses a new bucket.

## Open Questions

- The margin, if task 1.2 shows that 30 days of hidden versions and daily extensions cost too much.
