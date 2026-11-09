## ADDED Requirements

### Requirement: Substrate signs a deletion grant when it deletes a cell

When Substrate sets a cell's desired state to deleted, it SHALL sign a deletion grant with its grant key. The grant SHALL be an Ed25519 JWT with exactly the claims `aud` (`exomem-cell-delete`), `iat`, `exp`, `jti`, `cell_id` and `purge_after`. `purge_after` SHALL be `iat` plus the deletion grace, which Substrate's configuration holds in one place. Substrate SHALL store the grant in a column that `cellctl` can read and cannot write.

#### Scenario: A tenant deletes their account

- **WHEN** Substrate sets a cell's desired state to deleted
- **THEN** it stores a deletion grant that names only that cell
- **AND** the grant's `purge_after` is `iat` plus the deletion grace

#### Scenario: Substrate holds no grant key

- **WHEN** Substrate sets a cell's desired state to deleted while it holds no grant key
- **THEN** it stores no deletion grant, and no executor hides a file of that cell

### Requirement: A cell's deletion is final

Substrate SHALL NOT reverse a cell's deletion. A restore of a deleted cell's data SHALL go into a new cell ID.

#### Scenario: The operator restores a deleted cell's backups

- **WHEN** the operator restores the backups of a deleted cell
- **THEN** the data goes into a new cell ID, and the deleted cell stays deleted

### Requirement: The controller relays deletion grants and cannot forge them

`cellctl` SHALL relay each unexpired deletion grant into the ConfigMap `exomem-cell-deletions` in its own namespace. Admission SHALL let `cellctl` write only that ConfigMap name and its one key, beyond the existing exceptions. `cellctl` SHALL pass no file name and no key ID to an executor.

#### Scenario: The controller writes a forged grant

- **WHEN** an identity holding everything that `cellctl` holds writes a grant that it signed itself into the ConfigMap
- **THEN** no executor deletes a key or hides a file because of it

#### Scenario: A grant expires

- **WHEN** a deletion grant's `exp` passes
- **THEN** `cellctl` removes it from the ConfigMap

#### Scenario: The controller withholds a grant

- **WHEN** `cellctl` leaves a valid grant out of the ConfigMap
- **THEN** no executor purges the cell, and the key watcher raises its anomaly alert after `purge_after` plus one day

### Requirement: An executor acts only on a verified deletion grant

The key custodian and the media edge SHALL verify each deletion grant with the grant public key from a chart value. Neither SHALL read that key from a value that `cellctl` renders. Each SHALL check the type, audience, exact claims, integer times, lifetime and expiry. Each SHALL refuse a grant whose `purge_after` is less than `iat` plus one day. Each SHALL take the cell ID only from a verified grant.

#### Scenario: A grant has no grace

- **WHEN** a grant that verifies carries a `purge_after` equal to its `iat`
- **THEN** neither executor deletes a key or hides a file of that cell

#### Scenario: A grant names another cell

- **WHEN** a valid grant names one cell
- **THEN** each executor acts only on that cell's prefix, and touches no other cell's files or keys

### Requirement: The custodian deletes a deleted cell's key once its namespace is gone

For a cell with a valid deletion grant, the key custodian SHALL read the cell's namespace with a direct request. Only when that request returns not found SHALL it delete the keys that the provider's key listing shows for the cell's prefix. It SHALL take key IDs only from that listing. While a valid grant names a cell, the custodian SHALL create no cell key for that cell.

#### Scenario: The namespace still exists

- **WHEN** a valid grant names a cell whose namespace still exists
- **THEN** the custodian deletes no key of that cell

#### Scenario: The namespace is gone

- **WHEN** a valid grant names a cell whose namespace returns not found, before `purge_after`
- **THEN** the custodian deletes the cell's key
- **AND** it hides no file of the cell

#### Scenario: The namespace appears again

- **WHEN** a namespace for a cell with a valid grant appears again
- **THEN** the custodian creates no key for that cell

### Requirement: The custodian hides a deleted cell's backups after the grace

At or after `purge_after`, the key custodian SHALL mint a purge key. The key SHALL have the backup bucket, the cell's prefix, exactly `listFiles` and `writeFiles`, and a short validity. With it, the custodian SHALL hide every current file under the prefix, list again until none remains, and then delete the purge key. It SHALL keep at most one key per prefix. It SHALL never mint a key for a cell without a valid grant. Every step SHALL be safe to repeat.

#### Scenario: The grace still runs

- **WHEN** a valid grant's `purge_after` has not come
- **THEN** the custodian mints no purge key and hides no file

#### Scenario: The grace has ended

- **WHEN** the custodian runs at or after `purge_after` for a cell with a valid grant
- **THEN** it mints a purge key of the exact shape, hides every current file under the prefix, and deletes the purge key

#### Scenario: A pass crashed and left a purge key

- **WHEN** the custodian finds a purge key that a crashed pass left for the prefix
- **THEN** it deletes that key before it mints a new one, and finishes the hides
- **AND** a later rerun changes nothing

### Requirement: The media edge hides a deleted cell's media after the grace

The media edge SHALL purge a cell in an internal pass that no HTTP operation starts. It SHALL purge a cell only when five conditions hold. The grant verifies. The loaded active set lists the cell in neither `cells` nor `collectors`. No write intent for the cell is pending. The time is at or after `purge_after`. The hold list, `cellDeletion.holdCells`, does not name the cell. It SHALL hide each current name under the cell's prefix through a persisted `hide` intent, and list again until none remains.

#### Scenario: The edge still admits the cell

- **WHEN** a valid grant's `purge_after` has passed, and the loaded active set still lists the cell
- **THEN** the edge hides no name of the cell

#### Scenario: A write for the cell is unsettled

- **WHEN** a write intent for the cell is still pending
- **THEN** the edge waits, and hides no name of the cell until the intent settles

#### Scenario: The edge restarts during a purge

- **WHEN** the edge restarts after it has hidden some of the cell's names
- **THEN** it resumes from its persisted intents and a new listing, and hides each remaining name once

### Requirement: Only the media edge holds the edge's list-only key

The media edge SHALL hold a second key on the media bucket with only `listFiles`. No other component SHALL hold that key. The edge's existing key SHALL keep its capabilities.

#### Scenario: The media bucket's keys are listed

- **WHEN** the founder account's keys for the media bucket are listed
- **THEN** the edge list key has only `listFiles`, and the edge's other key is unchanged

### Requirement: Provider lifecycle rules delete a deleted cell's versions

Only the buckets' lifecycle rules SHALL delete a deleted cell's file versions. The backup bucket's rule SHALL delete a hidden version after its lock lapses. The media bucket's rule SHALL delete a hidden version after the media grace. This capability SHALL add no key that deletes file versions or creates keys in the founder account, in the cluster or in CI.

#### Scenario: The keys that this capability adds are listed

- **WHEN** the edge list key, the watcher's backup list key and a purge key are listed
- **THEN** none of them can delete a file version or create a key

### Requirement: Turning the media edge off waits for listed grants

A chart upgrade that turns the media edge off SHALL be refused while `exomem-cell-deletions` lists any deletion grant.

#### Scenario: The operator turns the edge off during a deletion

- **WHEN** the operator deploys the chart with the edge turned off, while the ConfigMap lists a grant
- **THEN** the upgrade is refused, and the edge keeps running

### Requirement: The key watcher takes the deletion set only from Substrate

The key watcher SHALL take the set of deleting cells only from a Substrate endpoint. The endpoint SHALL accept only the watcher's receiver capability. It SHALL return each cell whose grant Substrate signed within the watch window, with its `iat` and `purge_after`. The watch window SHALL exceed the deletion grace plus the larger of the media grace and the backup retention, plus two days.

#### Scenario: Another capability asks for the set

- **WHEN** a caller with the scheduler capability calls the endpoint
- **THEN** the endpoint refuses it and returns no cell

#### Scenario: The controller reports a deleted cell

- **WHEN** `cellctl` reports a cell as deleted that Substrate did not delete
- **THEN** the watcher's deletion set does not contain that cell

### Requirement: The key watcher reports each deletion's state

On each run, the key watcher SHALL derive each deleting cell's state from both buckets' listings, the key listing and the time. It SHALL fire `CELL_DELETION_PENDING:<cell_id>` while the grace runs or a current file or a key for the prefix remains. When neither remains, it SHALL resolve that alert and fire `CELL_DELETION_HIDDEN:<cell_id>`. It SHALL resolve the hidden alert when no `upload` and no `start` entry remains in either bucket.

#### Scenario: A deletion runs to its end

- **WHEN** a cell is deleted, its grace ends, the executors hide its files, and the lifecycle rules delete its versions
- **THEN** the operator receives the pending alert, then the hidden alert as the pending one resolves, then the hidden alert's resolution

### Requirement: The key watcher raises an anomaly alert for a stalled or early deletion

The key watcher SHALL fire `CELL_DELETION_ANOMALY:<cell_id>` while any of three conditions holds. A current file or a key for the prefix remains after `purge_after` plus one day. An `upload` or `start` entry remains after `purge_after` plus the larger of the media grace and the backup retention, plus two days. A hide marker under the prefix carries a time after `iat` and before `purge_after`.

#### Scenario: An executor stalls

- **WHEN** current files remain under a deleting cell's prefix one day after `purge_after`
- **THEN** the watcher fires the anomaly alert for that cell

#### Scenario: A hide comes before the grace ends

- **WHEN** a hide marker under a deleting cell's prefix appears after `iat` and before `purge_after`
- **THEN** the watcher fires the anomaly alert for that cell

#### Scenario: A live cell pruned before its deletion

- **WHEN** the only hide markers under a deleting cell's prefix are older than its grant's `iat`
- **THEN** they raise no anomaly alert

### Requirement: Account-level alerts follow the deletion set

The key watcher SHALL count toward `MEDIA_HIDE_BURST` only the hides on prefixes of cells outside the deletion set. It SHALL raise `PURGE_KEY_UNEXPECTED` for a key of the purge shape whose cell is outside the set. When it cannot read the set, it SHALL raise `DELETIONS_UNKNOWN`, and it SHALL treat every cell as outside the set.

#### Scenario: A deletion hides many names

- **WHEN** the edge hides every name of a deleting cell in one window
- **THEN** those hides do not count toward the hide budget, and hides of live cells still count

#### Scenario: A purge key appears for a live cell

- **WHEN** the key listing shows a key of the purge shape for a cell outside the deletion set
- **THEN** the watcher raises `PURGE_KEY_UNEXPECTED`

#### Scenario: The deletion set cannot be read

- **WHEN** the watcher cannot read the deletion set
- **THEN** it raises `DELETIONS_UNKNOWN`, counts every hide toward the budget, and reports every purge key

### Requirement: The key watcher's public log names no cell

The key watcher's log SHALL carry counts and reason tokens only. It SHALL NOT carry a cell ID. Only the alerts that the watcher sends to the receiver SHALL carry a cell ID.

#### Scenario: A deletion is in progress

- **WHEN** the watcher runs while a cell is being deleted
- **THEN** its CI log shows counts, and no cell ID

### Requirement: The operator can hold a deletion

Both executors SHALL skip every cell that the chart value `cellDeletion.holdCells` names. The value SHALL be admin tier, so that `cellctl` cannot change it. A hold SHALL NOT stop the key watcher's alerts for the cell.

#### Scenario: The operator holds a cell during the grace

- **WHEN** the hold list names a cell, and its `purge_after` passes
- **THEN** neither executor deletes a key or hides a file of that cell
- **AND** the watcher fires the anomaly alert for it one day after `purge_after`

### Requirement: Rescue deletes only hide markers

The cleanup script's `--rescue` mode SHALL delete only `hide` entries under a cell's prefix. It SHALL NOT delete an `upload` entry. The script SHALL have no apply mode that hides or deletes files in the cell-data account or the media bucket.

#### Scenario: The operator rescues a cell's media

- **WHEN** the operator runs `--rescue` for a cell's prefix within the media grace
- **THEN** the script deletes each hide marker, each version becomes current again, and no `upload` entry is deleted

### Requirement: The legacy backup bucket keeps the manual path

For a cell with data in the founder account's legacy backup bucket, `cellctl` SHALL raise `cell-storage-cleanup-pending-<cell_id>`, and the cleanup script SHALL keep its `founder` mode. `cellctl` SHALL raise that alert for no other cell. The executors SHALL still purge such a cell's files in the cell-data account and the media bucket.

#### Scenario: A cell created before the custodian is deleted

- **WHEN** a cell with data in the legacy backup bucket is deleted
- **THEN** `cellctl` raises the cleanup alert for it, and the executors still purge its files in the cell-data account and the media bucket

#### Scenario: A newer cell is deleted

- **WHEN** a cell with no data in the legacy backup bucket is deleted
- **THEN** `cellctl` raises no cleanup alert for it
