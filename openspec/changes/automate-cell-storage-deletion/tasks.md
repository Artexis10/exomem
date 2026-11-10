## 1. Provider facts (D2, D4, D5)

- [ ] 1.1 On a disposable bucket with Object Lock in compliance mode, with sandbox credentials that the operator authorizes, check five points:
  - † a key limited to the bucket and a prefix, with only `listFiles` and `writeFiles`, hides a locked current version;
  - † the lifecycle rule deletes a hidden locked version only after its lock lapses, and deletes it then; record the delay after the due day;
  - † the provider does or does not let a key delete a hide marker in the locked bucket;
  - † an expired key does or does not appear in the key listing;
  - † a key with only `listFiles` sees the `start` entries of unfinished large files.

  Verify: the results are recorded in design.md, and each point marked † in this change is confirmed or corrected.

## 2. Substrate (D1, D4)

- [x] 2.1 Substrate (separate repository): add the deletion grant and the watcher's endpoint.
  - Add the `deletion_grant` column, readable and not writable by `cellctl`'s role.
  - Sign the grant, with no `exp`, when Substrate sets a cell's desired state to deleted.
  - Refuse any change to a deleted cell's desired state in the database.
  - Record each deleted cell's stop time.
  - Add the deletion-set endpoint for the watcher.
  - Prepare the backfill of grants for deleted cells, to run at rollout step 10.

  Verify with tests:
  - a signed grant passes the shared grant fixture;
  - with no grant key, a deletion stores no grant;
  - a write that changes a deleted cell's desired state is refused;
  - the endpoint refuses the scheduler capability;
  - the endpoint returns every cell deleted with a grant, with its `iat`, `purge_after` and stop time;
  - the receiver keeps an alert that gets no further transition until the operator resolves it.

## 3. cellctl (D1, D6)

- [x] 3.1 In `cellctl`, relay each grant into the ConfigMap `exomem-cell-deletions`, and add its admission exception outside the edge's switch. Remove a grant only after the list-only views of both buckets show no `upload` and no `start` entry under the prefix. Raise `cell-storage-cleanup-pending` only for a cell with data in the legacy backup bucket. Verify these with tests:
  - reconcile tests for deleted and live rows;
  - a grant stays while either bucket shows an `upload` or `start` entry under the prefix, and goes once neither does;
  - the K3s Live job admits a `cellctl` write only for that ConfigMap name and key, with the edge on and off.

## 4. Key custodian (D1, D2)

- [x] 4.1 Add the backup purge to the custodian, with its own grant verifier, and move `pyjwt[crypto]` to `cellctl`'s runtime dependencies. The custodian deletes the cell's key once the namespace is gone. After `purge_after`, it mints the purge key, hides, deletes the purge key, and marks the cell done. Verify with fake B2 and a fake key store:
  - the verifier passes the shared grant fixture;
  - a missing, forged or other-cell grant causes no action;
  - a malformed entry is skipped, a valid grant beside it acts, and a new cell namespace still gets its key;
  - no action happens while the namespace exists or the cell is held;
  - a released hold resumes the purge on the same grant;
  - the cell's key is deleted at once, but no hide happens before `purge_after`;
  - after `purge_after`, the purge key has the exact shape, every current name is hidden, and the purge key is deleted;
  - a namespace that exists at pass time stops the hide;
  - after a pass finds no current name, the cell is marked done, and neither a later sweep nor a restart mints a key;
  - a stale purge key is replaced;
  - a recreated namespace gets no key.

## 5. Media edge (D1, D3)

- [x] 5.1 Add the edge's internal purge pass, with its own grant verifier, using the list-only key and persisted `hide` intents. Verify with tests:
  - the verifier passes the shared grant fixture;
  - a missing, forged or other-cell grant causes no hide;
  - a malformed entry is skipped, and a valid grant beside it acts;
  - a held cell gets no hide, and a released hold resumes the purge;
  - no hide happens before `purge_after`;
  - after `purge_after`, every current name is hidden, and a rerun changes nothing;
  - a cell that is still in `cells` or `collectors` gets no hide;
  - the pass waits while a write intent for the cell is pending;
  - a restart in the middle of a purge resumes it, and each name is hidden once.

## 6. Terraform (D3, D4)

- [x] 6.1 Add `<prefix>-media-edge-list`, with only `listFiles` on the media bucket, to the durability stack. Add a watcher key with only `listFiles` on the backup bucket to the cell-data stack. Output both key IDs, and update each stack's `OWNERSHIP.md`. Verify: `terraform test` shows each key's exact bucket and capabilities.

## 7. Key watcher (D4)

- [x] 7.1 Add the deletion set, the watch window, the per-cell states, the anomaly alert and the three account-level changes to the watcher. Add the backup list key and the endpoint address to the workflow's secrets. Verify with synthetic listings:
  - each state is reached, and each per-cell alert fires and resolves;
  - a done cell with no key and no current file is Hidden, and raises no pending or anomaly alert;
  - each anomaly condition fires;
  - a hide before the stop time plus the skew margin, or within the margin before `purge_after`, raises no anomaly;
  - a failed listing, a listing at the page cap, or a refused key sends no per-cell transition and resolves no alert; it raises `DELETION_LISTING_FAILURE`;
  - a cell past its window gets no transition, so its firing anomaly alert is not resolved;
  - the hide budget excludes the prefixes of deleted cells and still counts live ones;
  - a purge key of a live cell raises `PURGE_KEY_UNEXPECTED`;
  - an unreadable deletion set raises `DELETIONS_UNKNOWN` and counts every hide;
  - the log holds no cell ID.

## 8. Cleanup script (D5, D6)

- [x] 8.1 In `infra/scripts/cell_storage_cleanup.py`, replace the apply modes for the cell-data account and the media bucket with `--rescue`, and keep the `founder` mode. Verify: a test shows that `--rescue` never deletes an `upload` entry.

## 9. Chart and runbooks (D1, D2, D3, D5, D6)

- [x] 9.1 Add `cellDeletion.holdCells`, as a mounted file, and the grant public key to the custodian and the edge. Give the custodian `get` on `exomem-cell-deletions` and a write of its own `exomem-key-custodian-purges`. Verify with chart tests:
  - both values render into the custodian and the edge;
  - only the edge mounts the edge list key;
  - the custodian's admission allows only its Secret and its own ConfigMap;
  - the `cellctl` admission exception renders with the edge off.
- [x] 9.2 Update three runbooks:
  - `docs/runbooks/cloud-key-custody.md`: the cell clean-up, the media bucket of a deleted cell and the pinned set;
  - `docs/runbooks/cloud-media-edge.md`: the deletion of a cell that used the media store;
  - `docs/runbooks/cloud-cell-deletion.md`: step 8.

  Verify: each runbook names the new alerts, the hold list, its release and `--rescue`. No step runs an apply mode for the cell-data account or the media bucket.

## 10. Live rehearsal (D4, Rollout)

- [ ] 10.1 With the operator's approval, delete a disposable cell that holds media and backups. Verify: the operator receives `CELL_DELETION_PENDING` at the deletion, then `CELL_DELETION_HIDDEN` with PENDING resolved after `purge_after`; both listings show no current file under the cell's prefix.
- [ ] 10.2 Keep watching the same cell until the provider deletes its versions. That takes about G plus R plus 1 days, plus the provider's delay. Verify: `CELL_DELETION_HIDDEN` resolves, and both listings show no `upload` and no `start` entry.
