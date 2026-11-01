## ADDED Requirements

### Requirement: A cell accepts a request only with a valid gateway proof

A Cloud cell SHALL accept a request on its service port only when the request carries a valid gateway proof for that request's purpose and for the cell's own cell ID. The cell SHALL verify the proof against the pinned gateway verifying key.

No component SHALL hold a key that derives a static credential that a cell accepts for a request. The cell's health-only port SHALL stay unauthenticated, and it SHALL serve only the readiness answer.

#### Scenario: A request with a bearer and no proof

- **WHEN** a request reaches a cell with a valid legacy bearer but no gateway proof
- **THEN** the cell refuses it

#### Scenario: A proof for another cell

- **WHEN** a request carries a proof that the gateway signed for a different cell ID
- **THEN** the cell refuses it

#### Scenario: The controller's keys are searched for a request credential

- **WHEN** every key that the controller holds is listed
- **THEN** none of them signs a proof or derives a credential that any cell accepts

### Requirement: The admission policy pins every trust anchor that a cell reads

The platform SHALL render each trust anchor into the admission policy as a literal value. The trust anchors are:
- the gateway verifying key;
- the grant verifying key;
- the object-store endpoint and bucket;
- the escrow recipient.

The admission policy SHALL accept a cell StatefulSet or Job only when each anchor that the workload reads equals the pinned current or next value. It SHALL also require that the cell ID a workload reads equals the cell ID in its namespace name. The controller SHALL NOT be able to change the pinned values.

The platform SHALL pin a minimum cell release, and admission SHALL refuse a cell image below it.

#### Scenario: The controller swaps a verifying key

- **WHEN** the controller's identity writes a cell StatefulSet whose gateway verifying key differs from the pinned value
- **THEN** admission refuses the write

#### Scenario: A key rotates

- **WHEN** a chart release pins a next verifying key beside the current one
- **THEN** admission accepts workloads with either key, and a later release that drops the old key refuses it

### Requirement: Cell workloads run a closed pod template

The admission policy SHALL accept a cell StatefulSet or Job only when its whole pod template has an allowlisted shape. Every container and init container SHALL meet all of these:
- it runs the fixed process preamble, followed by `exomem` and one subcommand from the closed set;
- it has no lifecycle hooks;
- its probes are `httpGet` on the cell's own fixed ports and paths;
- its volume mounts are exactly the known pairs for its subcommand, with no `subPath`;
- `workingDir` is unset;
- it has no `envFrom`;
- every environment name is on the allowlist for its subcommand.

The pod SHALL have no ephemeral containers, no `dnsConfig` and no `hostAliases`, and its `dnsPolicy` SHALL be `ClusterFirst`. The `cell-credentials` Secret SHALL hold only allowlisted data keys.

No cell workload SHALL run a shell command string that the controller assembles.

#### Scenario: The controller runs a shell in a cell Job

- **WHEN** the controller's identity writes a Job in a cell namespace whose command is `sh -c` with its own string
- **THEN** admission refuses the write

#### Scenario: The controller adds an environment variable

- **WHEN** the controller's identity writes a cell Job that sets an environment name outside the closed set, such as one that changes the interpreter's module path
- **THEN** admission refuses the write

#### Scenario: The controller adds a lifecycle hook or an exec probe

- **WHEN** the controller's identity writes a cell StatefulSet whose container has a `postStart` hook or an exec probe
- **THEN** admission refuses the write

#### Scenario: The controller plants a certificate authority

- **WHEN** the controller's identity writes a cell Job that sets a CA bundle variable, mounts a CA file, or sets `dnsConfig`
- **THEN** admission refuses the write

#### Scenario: A backup runs

- **WHEN** a cell's scheduled backup runs
- **THEN** its Job runs `exomem cell-backup` with no destination argument, and admission accepts it

### Requirement: Data moves only to destinations that a signed grant names

A cell SHALL run an export, import or restore only when it holds a grant that Substrate signed for that cell, that operation and that operation ID, and that has not expired. The cell SHALL verify the grant against the pinned grant verifying key.

Before the cell moves data, it SHALL parse each URL that it uses and check three things:
- the host equals the pinned object-store endpoint;
- the path is the pinned bucket, then the grant's object key;
- the object key sits under the cell's own prefix.

A backup SHALL go only to the repository that the cell derives from its own cell ID, its repository generation and the pinned endpoint and bucket. The cell SHALL keep the generation on its volume.

A refused operation SHALL log a cataloged, content-free event that names the cell, the operation and the reason.

#### Scenario: An export with no grant

- **WHEN** the controller starts an export Job for a cell with no grant, or with a grant for another export
- **THEN** the cell refuses the export and uploads nothing

#### Scenario: An export to a foreign host

- **WHEN** an export Job carries a valid grant and a presigned URL whose host is not the pinned endpoint
- **THEN** the cell refuses the export and uploads nothing

#### Scenario: A restore with no grant

- **WHEN** the controller starts a restore Job without a restore grant for that snapshot
- **THEN** the cell refuses the restore and leaves its volume unchanged

### Requirement: Exports and imports are encrypted to recipients the controller cannot replace

A cell SHALL encrypt an owner export, before upload, to the recipient that the signed export grant names. The tenant's client SHALL generate that recipient.

The cell SHALL report the archive's SHA-256 on a gateway-authenticated route that the controller cannot reach. The tenant's client SHALL accept a download only when its digest matches, and SHALL refuse it when no digest is available. Substrate SHALL take the digest only from that route.

A cell SHALL decrypt an import only when the signed import grant names the cell's own import recipient. Substrate SHALL obtain that recipient from the cell through the gateway, on a path that the controller cannot reach.

#### Scenario: The controller reads a completed export

- **WHEN** an identity holding everything the controller holds downloads a completed owner export
- **THEN** it cannot decrypt the archive

#### Scenario: The controller forges an export

- **WHEN** the controller replaces a completed export with an archive that it encrypted to the tenant's recipient
- **THEN** the tenant's client refuses the download, because its digest differs from the one that the cell reported through the gateway

#### Scenario: The controller substitutes an import recipient

- **WHEN** the controller writes its own recipient into an import request row
- **THEN** the signed grant still names the cell's recipient, and the cell refuses an upload that was encrypted to any other recipient

### Requirement: The controller cannot decrypt a cell's backups

Each cell SHALL generate its own backup repository password and keep it on its encrypted volume, outside every backup and export root. It SHALL write an escrow copy, under its own object-store prefix, encrypted only to the recipient that a valid signed mode statement names. The cell SHALL keep the highest statement generation it has seen and refuse an older one. A standard-mode statement SHALL name the pinned operator recipient.

No platform service SHALL hold the password or a key that decrypts it. In standard mode the escrow recipient SHALL be the operator's presence-gated identity. In tenant-locked mode it SHALL be the tenant's own key, and the platform SHALL write no copy that it can open. A restore after the loss of the cell's volume SHALL need the holder of the escrow recipient's identity.

A mode statement SHALL carry an audience that no operation grant carries. In tenant-locked mode the backup password SHALL sit only inside the tenant's inner layer, and a restore after volume loss SHALL receive it only on the tenant-lock key channel. A cell that leaves tenant-locked mode SHALL start an empty repository at its next generation, SHALL erase the locked-period password before the platform re-wraps its root key, and SHALL hide the locked-period repository. It SHALL escrow to the operator no password that opens a snapshot from the locked period.

A cell SHALL destroy its platform-wrapped password only after the new repository passes `restic check --read-data` and holds every snapshot of the old one, matched by original snapshot ID.

#### Scenario: The controller downgrades a locked cell

- **WHEN** the controller replays an older standard-mode statement to a tenant-locked cell, or renders its own recipient
- **THEN** the cell writes no escrow copy to that recipient

#### Scenario: An existing cell migrates

- **WHEN** an existing cell moves to a cell-held password
- **THEN** its snapshots are copied into a new repository with a new master key, and the old platform password opens none of the new repository's snapshots

#### Scenario: A tenant-locked cell's backup

- **WHEN** the operator, with every platform credential and their authenticator, downloads a tenant-locked cell's backup repository and escrow copy
- **THEN** they cannot decrypt any snapshot

#### Scenario: The controller reads a backup

- **WHEN** an identity holding everything the controller holds downloads a cell's backup repository
- **THEN** it cannot decrypt any snapshot

#### Scenario: A tenant leaves the lock

- **WHEN** a tenant-authenticated statement moves a locked cell back to standard mode
- **THEN** the cell backs up into a new, empty repository, erases the locked-period password, and hides the locked-period repository
- **AND** the operator's escrow copy opens no snapshot taken while the cell was locked

#### Scenario: A grant is replayed as a mode statement

- **WHEN** a valid operation grant reaches the cell as a mode statement
- **THEN** the cell refuses it, because its audience is not the mode statement audience

#### Scenario: A migration copy is incomplete

- **WHEN** the new repository fails `restic check --read-data`, or lacks a snapshot of the old one by original ID
- **THEN** the cell keeps its platform-wrapped password and reports the migration as failed

#### Scenario: A volume is lost

- **WHEN** a cell's volume is lost and the operator restores the cell from backup
- **THEN** the restore succeeds only after the operator opens the escrow copy with their presence-gated identity

### Requirement: Attack signals alert through a path that excludes the controller

The platform SHALL raise an alert through the platform alert receiver for each of these signals:
- an admission denial for the controller's identity;
- a grant or destination refusal that a cell logged;
- a Secret read by an identity outside the expected set;
- a denied write of an unknown workload kind;
- an object-store key or a backup bucket setting outside the fixed shapes.

The alert path SHALL NOT pass through the controller. Each alert SHALL carry only an identity, a cell ID and a reason token. The evaluator SHALL coalesce signals by cell ID and reason, so that a flood of one class cannot bury another class.

#### Scenario: The controller is denied

- **WHEN** admission denies a write by the controller's identity
- **THEN** the operator receives an alert even though the controller sends nothing

### Requirement: Only the key custodian can create object-store keys

Cell backups SHALL live in a provider account that holds no other data. No in-cluster component other than the key custodian SHALL hold a key that can create object-store keys, in any provider account.

The custodian SHALL run outside every namespace that the controller can write, on a node that runs no cell. It SHALL create only the per-cell key shape: the pinned bucket, the prefix of the cell ID in the cell's namespace name, and exactly `listBuckets`, `listFiles`, `readFiles`, `writeFiles` and `deleteFiles`. It SHALL write each key only to its own Secret in the cell's namespace, and SHALL read no other Secret. It SHALL hide a cell's prefix only on a deletion record that Substrate signed, after a grace period, and SHALL raise an alert on each hide.

Until Substrate holds the grant key, the custodian SHALL hide and delete nothing. A cell deletion SHALL remove the cell's staged export and import objects and raise a content-free cleanup alert. The operator SHALL delete the cell's keys in both provider accounts, and hide its backup prefix after the grace period, only after confirming that Substrate set the cell's desired state to deleted. The grace period SHALL start from the operator's own first dry run, never from a time the controller wrote.

A key watcher SHALL run outside the cluster, at least hourly, with keys that can only list keys and buckets and read bucket retention, encryption and replication settings. It SHALL match each single-purpose key by its key ID, and each per-cell key by its exact bucket, prefix shape and capability set, with at most one key per prefix. It SHALL raise an alert for every other key, for every backup bucket setting that differs from its pinned value, for every failure to authenticate or list, and for every missed run. Its alerts SHALL use a receiver credential that no in-cluster component holds.

#### Scenario: The controller's keys are searched for key creation

- **WHEN** every object-store key that the controller holds is listed
- **THEN** none of them can create keys or change bucket settings

#### Scenario: The controller reaches the infrastructure state

- **WHEN** an identity holding everything the controller holds tries to read the Terraform state
- **THEN** it holds no key that can read it or create a key that can

#### Scenario: A rogue key appears

- **WHEN** an identity with the custodian's key creates a key that can change bucket settings, or a copy of an allowed key with the same name and capabilities
- **THEN** the key watcher raises an alert at its next run, through a path that excludes the cluster

#### Scenario: The watcher's key is deleted

- **WHEN** an identity with the custodian's key deletes the watcher's key
- **THEN** the watcher's next run fails to authenticate and raises an alert, and a missed heartbeat raises one too

#### Scenario: The controller deletes a cell's namespace

- **WHEN** the controller deletes or recreates a cell's namespace without a signed deletion record
- **THEN** the custodian hides none of that cell's backups

#### Scenario: The controller raises a false cleanup alert

- **WHEN** the controller raises a cleanup alert for a cell whose desired state Substrate has not set to deleted
- **THEN** the operator's cleanup deletes no key and hides no file of that cell

#### Scenario: The backup bucket's default retention is removed

- **WHEN** a key removes the default retention of the backup bucket
- **THEN** the key watcher raises an alert at its next run, and every version locked before the change stays locked
