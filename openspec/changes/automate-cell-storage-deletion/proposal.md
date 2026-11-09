## Why

A Cloud cell deletion ends in manual work today. `cellctl` raises the alert `cell-storage-cleanup-pending-<cell_id>`, and the operator then runs `infra/scripts/cell_storage_cleanup.py` for up to three accounts and buckets. The script counts a 7-day grace from the operator's own first dry run. So each deletion needs one operator session at the start and another after the grace. A forgotten second run leaves a deleted tenant's ciphertext stored and billed.

`contain-cloud-controller-compromise` D9 already plans part of an automatic path. The key custodian hides a deleted cell's backup prefix on a deletion record that Substrate signed, after a grace period. Nothing plans the same for a deleted cell's media, and no component reports a deletion that stalls.

The requirement, decided on 2026-11-09: a cell's storage deletion runs without an operator script, and it alerts the operator at each step. A compromised `cellctl` can delay a deletion, but it cannot cause one.

## What Changes

- **Substrate signs a deletion grant.** When Substrate sets a cell's desired state to deleted, it signs a deletion grant with its grant key. The grant names the cell and the earliest purge time, `purge_after`, at the end of a 7-day deletion grace. It has no expiry, because a deletion is final. `cellctl` relays each grant until the deletion has finished, and it cannot forge one.
- **The key custodian purges backups.** Once the cell's namespace is gone, the custodian deletes the cell's object-store key. After `purge_after`, it mints a short-lived purge key that can only list and hide in the cell's prefix. It hides every current backup file, deletes the purge key, and marks the cell done, so it mints no further key for it.
- **The media edge purges media.** After `purge_after`, and only when the edge no longer admits the cell, a pass inside the edge hides every current media file of the cell. The edge gets a second key, which can only list the media bucket.
- **Provider lifecycle rules delete.** The existing lifecycle rules of both buckets delete the hidden versions. No component gains a key that deletes file versions or creates keys in the founder account.
- **The key watcher witnesses and alerts.** The out-of-cluster key watcher reads the set of deleted cells from Substrate, never from `cellctl`. For each cell it raises a pending, a hidden and an anomaly alert, and it resolves each alert when its condition clears. A run that could not read everything changes no state. The hide budget ignores the hides of a deleted cell. A purge key for a live cell raises an alert. No deletion alert comes from `cellctl`.
- **Hold and rescue.** During the grace, the operator can hold a cell in a chart value, and both executors then skip it. A release resumes the purge. The cleanup script keeps a `--rescue` mode that deletes only hide markers, and its mode for the legacy backup bucket.
- **Deletion is final.** Substrate never reverses a cell's deletion. A restore goes into a new cell ID.

## Capabilities

### New Capabilities

- `cloud-cell-storage-deletion`: the deletion grant, both purges, the watcher's states and alerts, the hold list, rescue, and the manual path for the legacy backup bucket.

### Modified Capabilities

None canonical. Four active changes amend in place, as listed below.

### Modified elsewhere

This delivery adds pointer text to the design of each change below. It ticks no task. The implementation delivery makes the other edits.

- `contain-cloud-controller-compromise`, with no task ticks:
  - design D9: the custodian creates a second key shape, the purge key, and it deletes a deleted cell's key once the namespace is gone. The key watcher raises every deletion alert, and it gains a list key on the backup bucket;
  - spec requirement "Only the key custodian can create object-store keys": its key shape, hide alert and cleanup steps follow the same edits.
- `lock-cloud-backups-against-deletion` design D3: the custodian deletes a deleted cell's key first, and a purge key then hides the files.
- `add-cloud-media-store`:
  - design D6 and D7: the edge gains a list-only key and an internal purge pass that hides names;
  - design D9: the edge purge and the lifecycle rule replace the script's media mode. The cleanup alert fires only for a cell with data in the legacy backup bucket;
  - spec requirement "A media edge is the only path between cells and the media bucket" follows the D6 edit;
  - spec requirement "Deleting originals and cells removes their stored objects" follows the D9 edit.
- `collect-released-media` design D4 and its detection requirement: the hide budget ignores the hides on a deleted cell's prefix. An unreadable deletion set makes the watcher count every hide.

## Impact

- **Substrate (separate repository):** a grant column, signing at deletion, and a database refusal to revive a deleted cell. It also gains a backfill for deleted cells and the watcher's deletion-set endpoint.
- **cellctl:** relays grants into one ConfigMap, with one admission exception, until its list-only views show each deletion finished. The cleanup alert narrows to cells with data in the legacy backup bucket.
- **Key custodian:** the backup purge, its own grant verifier with `pyjwt[crypto]` at runtime, and a record of finished purges in its own namespace.
- **Media edge:** the purge pass, its grant verifier and its list-only key.
- **Key watcher:** the per-cell states, its own watch window, three account-level alerts, a list key on the backup bucket, and the deletion set from Substrate.
- **Terraform:** the edge list key in the durability stack, and the watcher's backup list key in the cell-data stack.
- **Chart:** `cellDeletion.holdCells` and the grant public key for the custodian and the edge, and the custodian's access to two ConfigMaps.
- **Cleanup script and runbooks:** `--rescue` replaces the apply modes for the cell-data account and the media bucket.
- **Not changed:** content-key retirement, which still runs first; the backup lock; the manual path for the legacy backup bucket; the grace length.
