## 1. Provider facts (D2, D4, D5)

- [ ] 1.1 On a disposable bucket with Object Lock in compliance mode, with sandbox credentials that the operator authorizes, check five points:
  - † a key limited to the bucket and a prefix, with only `listFiles` and `writeFiles`, hides a locked current version;
  - † the lifecycle rule deletes a hidden locked version only after its lock lapses, and deletes it then; record the delay after the due day;
  - † the provider does or does not let a key delete a hide marker in the locked bucket;
  - † an expired key does or does not appear in the key listing;
  - † a key with only `listFiles` sees the `start` entries of unfinished large files.

  Verify: the results are recorded in design.md, and each point marked † in this change is confirmed or corrected.

## 2. Substrate (D1, D4)

- [ ] 2.1 Substrate (separate repository): add the deletion grant and the watcher's endpoint.
  - Add the `deletion_grant` column.
  - Sign the grant when Substrate sets a cell's desired state to deleted.
  - Backfill grants for deleted cells.
  - Add the deletion-set endpoint for the watcher.

  Verify: a signed grant passes Exomem's verifier fixture. The endpoint refuses the scheduler capability. It returns only cells whose grant Substrate signed within the watch window.

## 3. cellctl (D1, D6)

- [ ] 3.1 In `cellctl`, relay each unexpired grant into the ConfigMap `exomem-cell-deletions`, and add its admission exception. Raise `cell-storage-cleanup-pending` only for a cell with data in the legacy backup bucket. Verify these with tests:
  - reconcile tests for deleted and live rows, and for an expired grant;
  - the K3s Live job admits a `cellctl` write only for that ConfigMap name and key.

## 4. Key custodian (D2)

- [ ] 4.1 Add the backup purge to the custodian. It deletes the cell's key once the namespace is gone. After `purge_after`, it mints the purge key, hides, and deletes the purge key. Verify with fake B2 and a fake key store:
  - a missing, forged or other-cell grant causes no action;
  - no action happens while the namespace exists or the cell is held;
  - the cell's key is deleted at once, but no hide happens before `purge_after`;
  - after `purge_after`, the purge key has the exact shape, every current name is hidden, and the purge key is deleted;
  - a rerun changes nothing;
  - a stale purge key is replaced;
  - a recreated namespace gets no key.

## 5. Media edge (D3)

- [ ] 5.1 Add the edge's internal purge pass with the list-only key, through persisted `hide` intents. Verify with tests:
  - a missing, forged or other-cell grant causes no hide;
  - a held cell gets no hide;
  - no hide happens before `purge_after`;
  - after `purge_after`, every current name is hidden, and a rerun changes nothing;
  - a cell that is still in `cells` or `collectors` gets no hide;
  - the pass waits while a write intent for the cell is pending;
  - a restart in the middle of a purge resumes it, and each name is hidden once.

## 6. Terraform (D3, D4)

- [ ] 6.1 Add `<prefix>-media-edge-list`, with only `listFiles` on the media bucket, to the durability stack. Add a watcher key with only `listFiles` on the backup bucket to the cell-data stack. Output both key IDs, and update each stack's `OWNERSHIP.md`. Verify: `terraform test` shows each key's exact bucket and capabilities.

## 7. Key watcher (D4)

- [ ] 7.1 Add the deletion set, the per-cell states, the anomaly alert and the three account-level changes to the watcher. Verify with synthetic listings:
  - each state is reached, and each per-cell alert fires and resolves;
  - each anomaly condition fires, and a hide marker older than `iat` raises no anomaly;
  - the hide budget excludes the prefixes of deleting cells and still counts live ones;
  - a purge key of a live cell raises `PURGE_KEY_UNEXPECTED`;
  - an unreadable deletion set raises `DELETIONS_UNKNOWN` and counts every hide;
  - the log holds no cell ID.

## 8. Cleanup script (D5, D6)

- [ ] 8.1 In `infra/scripts/cell_storage_cleanup.py`, replace the apply modes for the cell-data account and the media bucket with `--rescue`, and keep the `founder` mode. Verify: a test shows that `--rescue` never deletes an `upload` entry.

## 9. Chart and runbooks (D3, D5, D6)

- [ ] 9.1 Add `cellDeletion.holdCells` and the grant public key to the custodian and the edge. Make the edge-off check refuse while `exomem-cell-deletions` lists a grant. Verify: chart tests render both values into the custodian and the edge, and the edge-off check refuses while a grant is listed.
- [ ] 9.2 Update three runbooks:
  - `docs/runbooks/cloud-key-custody.md`: the cell clean-up, the media bucket of a deleted cell and the pinned set;
  - `docs/runbooks/cloud-media-edge.md`: the deletion of a cell that used the media store;
  - `docs/runbooks/cloud-cell-deletion.md`: step 8.

  Verify: each runbook names the new alerts, the hold list and `--rescue`. No step runs an apply mode for the cell-data account or the media bucket.

## 10. Live rehearsal (D4, Rollout)

- [ ] 10.1 With the operator's approval, delete a disposable cell that holds media and backups. Verify: the operator receives `CELL_DELETION_PENDING` at the deletion, then `CELL_DELETION_HIDDEN` with PENDING resolved after `purge_after`; both listings show no current file under the cell's prefix.
- [ ] 10.2 Keep watching the same cell until the provider deletes its versions, about G plus R plus 1 days after the deletion. Verify: `CELL_DELETION_HIDDEN` resolves, and both listings show no `upload` and no `start` entry.
