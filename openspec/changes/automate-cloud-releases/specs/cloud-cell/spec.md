## RENAMED Requirements

- FROM: `### Requirement: A release is one image digest, rolled out one cell at a time with restore-based rollback`
- TO: `### Requirement: A release is one image digest, rolled out canary first with restore-based rollback`

## MODIFIED Requirements

### Requirement: A release is one image digest, rolled out canary first with restore-based rollback

The platform release SHALL be the digest in the `cell_image` setting.

- A cell's upgrade target SHALL be its row's `desired_image` if set, otherwise `cell_image`.
- A new cell SHALL start on `desired_image` if set, otherwise on `cell_image` while the rollout is not paused, otherwise on `last_good_image`. While paused with no `last_good_image`, a new cell SHALL wait with `NO_GOOD_IMAGE`.
- `last_good_image` SHALL be set whenever a cell becomes ready on `cell_image`, including at first provisioning.

cellctl SHALL start the canary's attempt as soon as its target changes. The canary is the non-deleted cell with the lowest `rollout_priority`. While the canary's image differs from its target (`desired_image` or `cell_image`), no other attempt SHALL run. A canary that is behind its target and cannot start, because it is stopped, not ready, refused or in backoff, SHALL block every tenant attempt. After that, cellctl SHALL start an attempt only for a cell in the due set, oldest candidate first by the time it became a candidate. Until per-node concurrency ships, one attempt SHALL run fleet-wide. After it ships, at most the configured number of cells per node SHALL carry an upgrade hold. A restore hold anywhere SHALL block every new attempt. Each attempt SHALL:

1. pin its target when it starts, and use that pinned target on every later pass, whatever `cell_image` becomes;
2. stop the cell;
3. take a restore point while the cell is stopped and its volume is flushed: a volume snapshot on a storage class with snapshot support, otherwise a backup without a prune, within a bounded deadline;
4. start the pinned target;
5. on a snapshot class, back up one clone of the snapshot off site, after the target has started, and keep the clone and the upgrade hold until that backup ends;
6. wait for readiness up to the configured upgrade readiness deadline (30 minutes by default).

cellctl MUST NOT take the restore point while the cell serves.

When the rollout pauses, cellctl SHALL end every attempt that has not applied its pinned target, and SHALL restart that cell on its current image. While paused, cellctl SHALL apply no target to any cell. An attempt that applied its target before the pause SHALL finish its readiness wait and, on a miss, its restore or its forward-only hold. The first failure SHALL set the held cell, and a later concurrent failure SHALL NOT replace it.

If the stopped backup misses its deadline, cellctl SHALL restart the cell on its current image, record `BACKUP_FAILED`, and retry later. It MUST NOT leave the cell stopped on an object-storage failure. A failed off-site backup of the clone SHALL NOT end the attempt.

If readiness does not arrive, cellctl SHALL:

- stop the cell;
- restore the attempt's restore point so that no file created by the new image remains: the snapshot, or the attempt's own off-site backup when the snapshot is gone;
- start the previous image;
- pause the rollout, recording the error and the held cell.

An attempt crosses a one-way barrier when a `one-way` `cell` entry of the barrier file lies between two releases. The first is the release of the image that the cell ran before the attempt. The second is the release of its pinned target. cellctl SHALL map each digest to its release through Substrate's image history. A previous image that maps to no release SHALL count as older than every entry. A target that maps to no release SHALL count as newer than every entry. When a crossing attempt misses readiness, cellctl SHALL instead pause the rollout and keep the cell stopped under its upgrade hold, recording `UPGRADE_FORWARD_ONLY`. It SHALL neither restore the cell nor start the previous image, and it SHALL NOT re-apply a target to the held cell. It SHALL keep the attempt's restore point until the hold ends. Only a person's clear of the pause SHALL end the hold, after which cellctl SHALL delete the held attempt's snapshot and clone, where they exist, and start one new attempt toward the current target.

cellctl SHALL refuse an attempt whose target maps to a release before a `one-way` `cell` entry that the previous image is past. It SHALL then start nothing, keep the cell on its current image, and pause the rollout with `UPGRADE_BACKWARD_CROSSING`.

It MUST NOT roll back by image digest alone, and MUST NOT start either image on an unrestored volume. While paused, no cell SHALL start a new image.

No candidate, lock, fixture or promotion artifact SHALL be required to release.

#### Scenario: Upgrade succeeds

- **WHEN** the `cell_image` setting changes to a new digest
- **THEN** the canary cell is stopped, given a restore point, started on the new digest and becomes ready
- **AND** `last_good_image` becomes the new digest, each other cell follows in its own upgrade window, and vault content and recall are unchanged

#### Scenario: Canary fails readiness after migrating state

- **WHEN** the canary's new image migrates state and then fails to become ready within the configured upgrade readiness deadline
- **THEN** cellctl restores the attempt's restore point, starts the previous image, and pauses the rollout, recording the error and the held cell
- **AND** no other cell changes, and the canary serves its pre-attempt content

#### Scenario: Object storage unavailable during an upgrade

- **WHEN** the stopped pre-upgrade backup of a cell without snapshot support cannot reach object storage before the backup deadline
- **THEN** the cell restarts on its current image, the row records `BACKUP_FAILED`, and the attempt is retried after a backoff

#### Scenario: First release fails

- **WHEN** the very first `cell_image` fails readiness on the canary, and no `last_good_image` exists
- **THEN** the rollout pauses, and new cells wait with `NO_GOOD_IMAGE` instead of starting on no image

#### Scenario: A local cell takes a snapshot restore point

- **WHEN** an attempt starts for a cell whose storage class supports snapshots
- **THEN** cellctl stops the cell, snapshots its volume, and starts the target before any off-site backup runs
- **AND** the off-site backup reads a clone of that snapshot

#### Scenario: A local cell fails readiness

- **WHEN** a local cell's target misses the upgrade readiness deadline
- **THEN** cellctl copies the snapshot's vault and host state back onto the cell's volume, deleting each file that the target created
- **AND** it starts the previous image and pauses the rollout

#### Scenario: Neither restore point exists

- **WHEN** a failed attempt finds neither its snapshot nor its own off-site backup
- **THEN** cellctl records `RESTORE_FAILED`, keeps the cell stopped, and starts neither image

#### Scenario: A cell without snapshot support upgrades

- **WHEN** an attempt starts for a cell whose storage class has no snapshot support
- **THEN** its stopped pre-upgrade backup runs without `forget --prune`

#### Scenario: Several due cells share a node

- **WHEN** per-node concurrency has shipped, the canary runs the target, the per-node concurrency is 1, and three due cells share one node
- **THEN** one of them starts an attempt, and a due cell on another node also starts one

#### Scenario: A cell crosses a one-way barrier on a later release

- **WHEN** a cell's previous image is older than a one-way `cell` entry, its pinned target is a later release past that entry, and the target misses the upgrade readiness deadline
- **THEN** cellctl pauses the rollout and keeps the cell stopped under its upgrade hold, recording `UPGRADE_FORWARD_ONLY`
- **AND** it neither restores the volume nor starts the previous image

#### Scenario: A forward-only hold waits for a person

- **WHEN** a cell is in the forward-only hold and further passes run
- **THEN** cellctl applies no target to the cell, and the cell stays stopped
- **AND** after a person clears the pause, cellctl starts one new attempt toward the current target

#### Scenario: A target moves back across a one-way barrier

- **WHEN** a person sets `cell_image` to a release before a one-way `cell` entry, and a cell's previous image is past that entry
- **THEN** cellctl starts no attempt for the cell, the cell keeps serving its current image, and the rollout pauses with `UPGRADE_BACKWARD_CROSSING`

#### Scenario: The canary is behind and cannot start

- **WHEN** the canary's image differs from its target, and the canary is stopped, not ready, refused or in backoff
- **THEN** no tenant attempt starts, whatever the due set holds

#### Scenario: The canary's attempt ended on an older pin

- **WHEN** `cell_image` changed during the canary's attempt, and that attempt ended on its pinned target
- **THEN** the canary starts a new attempt toward the new `cell_image`, and no tenant attempt starts before the canary runs it

#### Scenario: An attempt does not cross

- **WHEN** a cell's previous image is already past every one-way `cell` entry, and its target misses readiness
- **THEN** cellctl restores the restore point and starts the previous image, as for any failed attempt

#### Scenario: cell_image changes during an attempt

- **WHEN** `cell_image` changes while a cell's attempt waits for readiness on its pinned target
- **THEN** the attempt keeps its pinned target, and the cell takes the new image only in a later attempt

#### Scenario: The rollout pauses during concurrent attempts

- **WHEN** one attempt pauses the rollout while another cell's attempt has stopped its cell but not applied its target
- **THEN** cellctl restarts that cell on its current image and ends its attempt
- **AND** the first failing cell stays the held cell

#### Scenario: A cell missed its window

- **WHEN** a due cell found no free slot in yesterday's window, and newer candidates share its window today
- **THEN** cellctl starts the older candidate first

#### Scenario: A write just before the stop

- **WHEN** the cell acknowledges a write just before its stop, and the attempt then fails readiness
- **THEN** the restored cell holds that write

### Requirement: Each cell is backed up consistently, encrypted with its own key

Cloud backups SHALL use a dedicated private bucket in the existing business B2
account. The controller SHALL receive a separately named key-management
credential, never the provider/master credential. That parent credential MUST NOT
be supplied to tenant runtime or backup pods. Tenant credentials SHALL be
restricted to the Cloud bucket and their own cell prefix. The controller's
key-management authority remains account-wide; bucket and prefix restrictions
MUST NOT be presented as containing that parent authority. Existing products'
buckets and credentials SHALL remain unchanged.

cellctl SHALL back up each running cell whose storage class supports snapshots hourly without stopping it, as a non-stopping hold: from a crash-consistent snapshot of the cell's volume, cloned to a read-only claim that the backup reads, with the clone and snapshot deleted when the backup ends, whatever its outcome. A cell SHALL carry at most one hold at a time, so an hourly backup never overlaps another backup or restore of the same cell. A running cell whose storage class has no snapshot support SHALL keep the nightly stopped backup in its existing window, one cell at a time, with that night's prune. Before every image change, cellctl SHALL also take the attempt's restore point with the cell stopped, as the release requirement states. On a storage class with snapshot support, that restore point SHALL be a volume snapshot, and its off-site backup SHALL read a clone of the snapshot after the target starts. Any other cell SHALL be backed up while stopped. A backup that misses its deadline SHALL record `BACKUP_FAILED`; a stopped pre-upgrade backup that misses its deadline SHALL also restart the cell. Retention SHALL keep 24 hourly, 7 daily and 4 weekly snapshots. Pruning SHALL run at most once a day and never as part of the pre-upgrade backup. Backup concurrency SHALL be configurable per node. The cell's quota SHALL admit its serving pod together with one backup Job. An alert SHALL fire when a running cell's last successful backup is more than two hours old, or more than 26 hours old for a cell on the nightly backup.

A backup SHALL:
- include both the vault and the custody home, including derived indexes;
- be written to the cell's own object-storage prefix with a key restricted to that prefix;
- be encrypted with a random per-cell data key, envelope-encrypted by a master key held outside the database.

The wrapped data key and the wrapped per-cell object-storage key SHALL be stored once on the cell row, so that a stateless controller can re-render the cell's Secret and delete the key later. The only exception is a per-cell object-storage key that lacks a capability the backup needs, which SHALL be replaced.

The per-cell object-storage key SHALL carry every capability the backup tool needs through the provider's S3-compatible API, including bucket listing where the provider needs it to report a missing object as absent rather than forbidden. When cellctl applies a cell outside a hold and the provider's key listing shows the stored key without one of those capabilities, it SHALL create a fresh key, replace the stored one in a single guarded write, and delete the old key, before any backup Job of the next hold renders. A key the listing does not show, a failed listing, or a failed key creation SHALL keep the stored key. A running backup or restore hold SHALL never have its key replaced.

Platform credentials with multiple fields SHALL be handed off as one complete, immutable, versioned SOPS Kubernetes Secret artifact. The declared exact key sets SHALL be enforced at the matrix, source, ciphertext and decrypted apply boundaries; every sensitive field SHALL be encrypted and verification decryption SHALL reproduce the full input document before publication. A server-side apply SHALL use the validated fields as base64 Secret `data` without changing the stored ciphertext artifact. Apply SHALL verify that the live Secret contains exactly the expected data key names before reporting success, including when another field manager retains an old key; verification output SHALL contain no secret values.

#### Scenario: Cell-token key rotation bundle

- **WHEN** a platform Secret handoff supplies either `current` with `currentVersion`, or those two fields plus `previous` with `previousVersion`
- **THEN** exactly those fields are published in one versioned artifact and applied together
- **AND** an incomplete previous-version pair or an undeclared field is rejected before publication

A restore into a new namespace SHALL produce a cell that answers recall, reports the same governance schema as its source cell, and accepts a governed write.

#### Scenario: Preparing the Cloud backup controller

- **WHEN** an operator prepares the Cloud controller's key-management credential
- **THEN** the Cloud bucket and separately named controller credential belong to the existing business account
- **AND** the provider/master credential is not supplied to the controller or tenant pods
- **AND** the preparation records the controller's account-wide key-management authority and each tenant key's bucket/prefix restrictions

#### Scenario: Restore drill

- **WHEN** a backup is restored into a scratch namespace and a cell starts on it
- **THEN** recall returns the notes that existed at backup time, governance status matches the source cell, and a governed write commits
- **AND** the source cell is unaffected

#### Scenario: Hourly backup of a serving cell

- **WHEN** an hourly backup runs while the cell is serving requests
- **THEN** the cell keeps serving without a restart
- **AND** the backup restores to a consistent state no newer than the snapshot, which answers recall, reports its governance status and accepts a governed write

#### Scenario: Backups stop succeeding

- **WHEN** a running cell on hourly backups has no successful backup for more than two hours, or a cell on the nightly backup for more than 26 hours
- **THEN** an alert fires

#### Scenario: Cell still on a volume without snapshots

- **WHEN** a running cell's volume is on a storage class without snapshot support
- **THEN** it is backed up nightly with the cell stopped, as before this change

#### Scenario: Stored key lacks a capability the backup needs

- **WHEN** cellctl starts a backup hold for a cell whose stored object-storage key the provider lists without a required capability
- **THEN** the cell row and the cell's Secret carry a fresh key with every required capability
- **AND** the old key is deleted
- **AND** the next backup can open or initialize the cell's repository

#### Scenario: Backup job attempts another tenant's prefix

- **WHEN** a cell's backup credentials are used against another cell's prefix
- **THEN** object storage refuses the request

## ADDED Requirements

### Requirement: A cloud cell counts its own hourly traffic

A cloud cell SHALL count the requests to its MCP path per UTC hour, with the seconds it ran in each hour. It SHALL keep 28 days of these counts on its own volume, outside the vault, and SHALL report its retention. Probes SHALL never count. Its readiness `traffic` block SHALL report the requests in flight and the age of the newest request. The retained hours SHALL be served on their own health-port path, not in readiness. An hour that the cell did not run SHALL read as not observed, never as zero. A failure of the counts SHALL never stop the cell.

#### Scenario: The cell restarts

- **WHEN** a cell restarts on the same volume
- **THEN** its history path reports the counts of the hours before the restart, less at most the last minute

#### Scenario: The cell was down for an hour

- **WHEN** a cell was stopped for a whole UTC hour
- **THEN** that hour reports no observed seconds, not zero requests

#### Scenario: Only probes arrive

- **WHEN** a running cell receives probes and no MCP request for a whole hour
- **THEN** that hour reports zero requests with its observed seconds

#### Scenario: The counts file is unreadable

- **WHEN** the cell finds its counts file unreadable at start
- **THEN** the cell starts and serves, and it begins a new set of counts

#### Scenario: The idle state

- **WHEN** a request is being handled
- **THEN** readiness reports at least one request in flight
- **AND** a cell that has received no request since it started reports the newest request's age as null, not zero

#### Scenario: A probe reads readiness

- **WHEN** the kubelet or cellctl reads the cell's readiness
- **THEN** the answer carries no hourly history

### Requirement: Each cell upgrades in its own quiet window

cellctl SHALL give each cell a one-hour UTC upgrade window: the hour with the lowest request rate over the configured lookback. Below the configured coverage, the window SHALL be a default hour that a fixed digest of the cell ID (SHA-256) picks inside the configured default range. cellctl SHALL refuse to start when that range overlaps the nightly backup window. cellctl SHALL record the window and its source on the cell's StatefulSet, keep it across every apply, and recompute it once per UTC day. Apart from the canary, a cell SHALL be due only inside its window, after the configured idle minutes or in the window's last configured minutes. Render-digest re-applies SHALL obey the due set.

#### Scenario: The window comes from history

- **WHEN** a cell observed more than the minimum coverage of its lookback, and its lowest request rate falls in the hour 04:00 UTC
- **THEN** its StatefulSet records the window `4 history`

#### Scenario: A cell has too little history

- **WHEN** a cell observed less than the minimum coverage of its lookback
- **THEN** its window is an hour inside the configured default range, and its StatefulSet records the source `default`

#### Scenario: Many new cells have no history

- **WHEN** many cells have too little history
- **THEN** their default hours spread across the default range, and none falls in the nightly backup window

#### Scenario: A routine apply of the StatefulSet

- **WHEN** cellctl applies a cell's StatefulSet for another reason
- **THEN** the window annotations keep their values, and cellctl does not recompute the window

#### Scenario: An idle cell in its window

- **WHEN** a candidate's window is open, and its readiness reports nothing in flight and no request for the configured idle minutes
- **THEN** the cell is due

#### Scenario: A busy cell in its window

- **WHEN** a candidate's window is open, and requests keep arriving
- **THEN** the cell is not due until fewer than the configured force minutes remain in its window, and it is due then

#### Scenario: The canary is outside its window

- **WHEN** the canary's target changes outside the canary's window
- **THEN** the canary's attempt starts at once

#### Scenario: A render-digest change outside the window

- **WHEN** a cell's render digest changes outside its window
- **THEN** cellctl re-applies the cell only once the cell is due

#### Scenario: Windows are off

- **WHEN** the chart sets `cellctl.upgradeWindow.enabled` to `false`
- **THEN** every eligible cell is due at once, as before upgrade windows

#### Scenario: Two cells have equal quiet hours

- **WHEN** two cells have the same lowest rate in several hours
- **THEN** each cell's window follows a fixed digest (SHA-256) of its cell ID, and a recompute on the same history picks the same hour

#### Scenario: A recompute changes the window

- **WHEN** cellctl records a new window for a running cell
- **THEN** no pod of the cell restarts

#### Scenario: A window value is missing

- **WHEN** the chart omits a window value or the per-node concurrency
- **THEN** cellctl refuses to start and names the value
