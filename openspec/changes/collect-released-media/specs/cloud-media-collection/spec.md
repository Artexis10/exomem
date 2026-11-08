## ADDED Requirements

### Requirement: A stored object name receives one upload

A new protection record SHALL get a fresh random 32-byte object name, written as 64 hexadecimal characters, and SHALL store it in the record. A retry of the same record SHALL reuse the record's name. After a record retires, no upload SHALL use its name again. A new name SHALL NOT derive from the original's digest. A reuse of an existing version SHALL copy the reference of a live sidecar, as the write-through requirement of `add-cloud-media-store` states.

#### Scenario: A deleted recording is added again

- **WHEN** an owner deletes a stored recording and later adds the same bytes again
- **THEN** the new upload gets a new name
- **AND** the first name receives no second version

#### Scenario: An upload reply is lost

- **WHEN** an offload loses the reply to its upload and retries
- **THEN** the retry uses the record's name
- **AND** nonce recovery finds the first upload under that name

#### Scenario: A second sidecar reuses a stored version

- **WHEN** an offload reuses a version that a live sidecar references
- **THEN** the new sidecar carries that sidecar's name and version, and no upload occurs

### Requirement: The media bucket deletes only hidden or superseded versions

The media bucket SHALL carry one bucket-wide lifecycle rule. The rule SHALL delete a version a configured number of days after a hide or a newer upload made it non-current. The number of days SHALL come from a variable that has no default for a stack with the media bucket. The bucket SHALL NOT hide a version by age. A current version SHALL survive the rule. No process in the cluster SHALL hold a key that deletes versions.

#### Scenario: The number of days is unset

- **WHEN** the durability stack with the media bucket is planned without a value for the condemned days
- **THEN** the plan fails and no rule changes

#### Scenario: A current version outlives the grace

- **WHEN** a version is current, and more than the configured days pass
- **THEN** the bucket keeps the version

#### Scenario: A hide marker is removed within the grace

- **WHEN** the operator deletes the hide marker of a name before the configured days pass
- **THEN** the version is current again and keeps its content

### Requirement: The edge condemns a name only for a collector credential

The media edge SHALL offer one more operation, `condemn`, which hides a name. It SHALL accept `condemn` only from a collector credential, which `cellctl` signs with the audience `exomem-media-edge-collector`. It SHALL refuse `condemn` from a runtime credential. It SHALL refuse every other operation from a collector credential. It SHALL derive the prefix from the credential's cell ID, so that a collector credential condemns only under its own cell's prefix. It SHALL hide a name only when a HEAD by name still shows the name, so that a repeated `condemn` hides nothing. The edge SHALL still offer no list or delete route. HEAD by name SHALL report the status `condemned` in two cases. One case is a version that exists while its name is hidden. The other is a version whose name has a different current version.

The active set that `cellctl` publishes SHALL carry a `collectors` map, with an entry only for a cell that the chart's `mediaEdge.collectCells` list names and that holds a store. A listed cell whose store is turned off but whose store identity still exists SHALL keep its collector entry. A cell taken off the list SHALL lose its entry and its collector credential. `cellctl` SHALL hold no capability to hide or delete.

#### Scenario: A runtime credential asks to condemn

- **WHEN** a runtime credential calls `condemn`
- **THEN** the edge refuses it and hides nothing

#### Scenario: A collector credential names another cell's object

- **WHEN** a collector credential calls `condemn` for a name that exists only under another cell's prefix
- **THEN** the edge resolves the name under the credential's own prefix and hides nothing

#### Scenario: The operator takes a cell off the collect list

- **WHEN** the operator removes a collecting cell from `mediaEdge.collectCells`
- **THEN** the next published set has no collector entry for the cell, and its backup Job holds no collector credential

#### Scenario: A name is condemned twice

- **WHEN** a collector credential calls `condemn` for a name that is already hidden
- **THEN** the edge hides nothing again and reports success

#### Scenario: Both credential kinds meet the route check

- **WHEN** a caller of either audience asks the edge to list, delete or hide by any route other than `condemn`
- **THEN** no such route exists

### Requirement: A collector credential reaches only the backup Job

`cellctl` SHALL render a cell's collector credential only into that cell's backup Job. The runtime SHALL NOT mount it, and the Job SHALL NOT mount the runtime credential. The backup Job SHALL gain egress to the media edge's address and port, with an admission rule for that peer. It SHALL gain no other peer. The edge-off check SHALL refuse while the `collectors` map is not empty.

#### Scenario: The credentials are rendered

- **WHEN** `cellctl` renders a cell with collection enabled
- **THEN** the backup Job holds the collector credential and the runtime holds only the runtime credential
- **AND** neither workload holds the other's credential

#### Scenario: The edge is turned off with collectors present

- **WHEN** an operator turns the edge off while the `collectors` map has an entry
- **THEN** the check refuses

### Requirement: A condemned version is never evicted or reused

When HEAD reports a reference as `condemned`, the cell SHALL NOT evict the original on the strength of that reference. The cell SHALL NOT reuse the version for another sidecar. When the cell still holds the bytes, it SHALL offload them again under a new name.

#### Scenario: An export meets a condemned version

- **WHEN** an export or a restore holds an original whose version a hold condemned
- **THEN** the cell does not evict it or reuse the version
- **AND** the cell offloads the bytes again under a new name

### Requirement: A reference is released before it retires

The cell SHALL keep an append-only release ledger. Before any reference to a stored name retires, the cell SHALL commit a release row for the name with a full sync. The retiring paths are the forgetting of an original, the drop of an absent version, a replacement, an emptied Trash and an abandoned protection record. A crash between the release and the unreference SHALL leave the reference in place. A missed release MAY leak storage and SHALL NOT cause a condemnation.

#### Scenario: A crash follows the release

- **WHEN** the cell crashes after it commits a release row and before it removes the reference
- **THEN** the sidecar still holds the reference after restart
- **AND** the release row stays

#### Scenario: A recording is deleted

- **WHEN** an owner deletes a stored recording
- **THEN** the cell commits a release row for its name before the reference retires

### Requirement: Each backup carries a media manifest

Each backup snapshot SHALL contain a media manifest inside the restic snapshot. The cell password SHALL encrypt it, so that prune removes the manifest with its snapshot. The backup SHALL compute the manifest from the read-only clone that it backs up, as strictly as the census of `add-cloud-media-store`: a companion that it cannot read SHALL make the census unknown. The manifest SHALL list the referenced (name, version) pairs, Trash included, the names under a protection record, and the released names. When the census is unknown, the manifest SHALL say so and SHALL NOT list a partial result as complete.

#### Scenario: A manifest is written

- **WHEN** a cell holds live and Trash references, a protection record and a released name
- **THEN** the manifest lists each of the four

#### Scenario: The census is unknown

- **WHEN** an unreadable sidecar makes the reference census unknown during a backup
- **THEN** the backup's manifest is marked unknown

### Requirement: Candidates come from retained manifests

After a hold's backup and prune both succeed, the backup Job SHALL read the manifest of every retained snapshot. The candidates SHALL be the released names of the newest manifest, less every name that any retained manifest references or protects. The Job SHALL call `condemn` for each candidate. When any retained snapshot has a missing or unknown manifest, the Job SHALL condemn nothing for the cell. No clock, age or timestamp SHALL decide a candidate.

#### Scenario: An old snapshot names the version

- **WHEN** a retained months-old snapshot's manifest names a released name
- **THEN** the Job does not condemn it
- **AND** after that snapshot is pruned, a later hold condemns it

#### Scenario: A sidecar is deleted after the clone

- **WHEN** the clone holds a reference and the owner deletes the sidecar before the backup ends
- **THEN** the Job condemns nothing for that name in this hold

#### Scenario: Only a synced ledger record survives the capture

- **WHEN** a crash-consistent copy of the ledger holds the fully synced protection record of an upload but not its later commit
- **THEN** the Job condemns nothing for that name

#### Scenario: The reuse source was released

- **WHEN** a reuse source was released while the reuser's record or reference exists
- **THEN** the Job condemns nothing for that name

#### Scenario: An old backup is restored after several holds

- **WHEN** several holds and lifecycle ticks pass, and the cell is then restored from its oldest retained snapshot
- **THEN** every restored reference reads its pinned version as current

#### Scenario: A snapshot lacks a manifest

- **WHEN** one retained snapshot has no manifest
- **THEN** the Job makes no `condemn` call for the cell

### Requirement: Hides are detected and can be rescued within the grace

`cellctl` SHALL report the count of hidden names of each cell, with at most a few of the names. The out-of-cluster key watcher SHALL hold a list-only key that is limited to the media bucket. It SHALL alert when the hide markers that appeared since its last run exceed a pinned budget. The alert SHALL carry a count and a reason token and SHALL NOT carry a name. The grace period SHALL exceed the watcher's interval plus the operator's response time. The operator SHALL be able to rescue a name by deleting its hide marker with the out-of-cluster cleanup key.

#### Scenario: A hide burst exceeds the budget

- **WHEN** the number of new hide markers exceeds the pinned budget between two watcher runs
- **THEN** the watcher alerts with a count and a reason token and no name

#### Scenario: A single owner deletion

- **WHEN** an owner deletes one recording and a hold hides its name
- **THEN** `cellctl` lists the hidden name for the cell and the watcher raises no alert

#### Scenario: A rescued name that a sidecar still references

- **WHEN** a compromised edge hides a name that a live sidecar references, and the operator deletes its hide marker within the grace
- **THEN** the version is current again and the next hold does not hide the name, because a referenced name is never a candidate

### Requirement: The meter counts names with more than one upload version

The `cellctl` media meter SHALL count, for each cell, the names in its prefix that hold more than one upload version. It SHALL report the count. A name from before unique names can hold such versions, and the lifecycle rule would delete the older one.

#### Scenario: The canary holds a repeated name

- **WHEN** the canary's prefix holds a name with two upload versions
- **THEN** the meter reports a count of one for the canary
- **AND** the operator re-offloads that original before the rule applies

#### Scenario: Every name has one version

- **WHEN** every name in the canary's prefix holds one upload version
- **THEN** the meter reports zero
