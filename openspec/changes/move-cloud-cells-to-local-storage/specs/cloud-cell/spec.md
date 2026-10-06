## MODIFIED Requirements

### Requirement: The cell controller converges desired rows without its own state

cellctl SHALL read desired cell state from the control database and converge the cluster to it with Kubernetes server-side apply under one field manager. It SHALL write observed state back, and SHALL hold no database, lease, fence or checkpoint of its own.

- It SHALL read readiness from pod conditions, and MUST NOT connect to a cell over the network.
- A transient condition MUST be recorded as a non-terminal observed state and retried. Transient conditions include a pending volume, a pulling image, a running init container, a terminating pod and an unavailable API.
- Only an identity conflict, or an init container still failing after its deadline, SHALL mark a cell `failed`. An identity conflict is an existing namespace or volume that belongs to a different cell. A cell volume on any configured cell storage class is not, by its class alone, an identity conflict.
- A value-free refusal code that an init container reports SHALL be recorded on the row as soon as it is observed, without waiting for the deadline.
- `observed_generation` SHALL advance only when the observation matches the applied desired generation.
- An ordinary pass SHALL render the cell's current image. Only a release attempt SHALL change it.
- A maintenance operation SHALL hold a cell's image, and a stopping operation also its replica count, through one hold marker. The controller SHALL record the hold kind and start time on the row, and SHALL resume the hold after a restart.

#### Scenario: New cell requested

- **WHEN** a row is inserted with `desired_state = running`
- **THEN** cellctl creates the cell's resources and reports `provisioning` while the volume binds and the init container runs
- **AND** it reports `running` and ready once the pod's readiness condition holds

#### Scenario: Controller restarts mid-apply

- **WHEN** cellctl stops after applying some of a cell's resources
- **THEN** the next pass re-applies the same manifests with no duplicate resources and no failure state

#### Scenario: Desired state changes during a stopped backup

- **WHEN** a cell's row flips to `read_only` while a stopping backup hold is active
- **THEN** the cell stays stopped until the backup finishes, and then starts read-only on its current image

#### Scenario: Desired state changes during an hourly backup

- **WHEN** a cell's row flips to `read_only` while its hourly backup hold is active
- **THEN** the cell restarts read-only without waiting for the backup
- **AND** the backup's clone and snapshot are still deleted when it ends

#### Scenario: Controller restarts mid-upgrade

- **WHEN** cellctl restarts while a cell carries an upgrade hold
- **THEN** the row shows the hold kind and start time, and the attempt resumes from its recorded step rather than leaving the cell stopped

#### Scenario: Foreign namespace

- **WHEN** namespace `exo-cell-<cell_id>` exists with a cell label that differs from the row
- **THEN** cellctl marks the row `failed` with an identity-conflict code and does not modify that namespace

#### Scenario: A cell not yet migrated

- **WHEN** the configured cell storage classes include both the local class and the class a not-yet-migrated cell's volume is bound to
- **THEN** cellctl keeps serving and backing up that cell on its existing volume without an identity conflict

### Requirement: Cells are isolated per tenant

Each cell SHALL have its own namespace, ResourceQuota, fixed-size volume on encrypted node-local storage, Secret, backup key and object-storage key. A cell's volume SHALL be its own logical volume with its own filesystem, so that a cell filling its volume cannot reduce a neighbour's volume or the node's root filesystem. The storage pool backing cell volumes SHALL NOT be overcommitted: the sum of volume and snapshot sizes on a node SHALL NOT exceed the pool. Its namespace SHALL enforce Pod Security `restricted`. Its pods SHALL run non-root, with seccomp `RuntimeDefault`, a read-only root filesystem, dropped capabilities and no ServiceAccount token.

A default-deny network policy SHALL:
- admit runtime ingress only from the cloud gateway;
- grant the runtime pod no egress unless explicitly selected for Cloud artifact transport, in which case admit only the designated internal artifact-broker namespace/pod peer on TCP 8767, with no DNS or direct public egress;
- limit backup and restore pods to object-storage egress.

No request field, path or header SHALL select a cell. An admission policy SHALL confine cellctl's own writes to cell namespaces, restricted namespaces and digest-pinned images from the configured repository. It SHALL admit a cell volume claim only on a configured cell storage class, a clone claim only when its source is a snapshot of the same cell's own volume, and cellctl's delete of a cell's volume claim only when the bound volume's reclaim policy is Retain. cellctl's only write to a persistent volume SHALL be setting its reclaim policy to Retain, and only on a volume whose claim is in a cell namespace.

#### Scenario: Another workload attempts direct access

- **WHEN** a pod other than the gateway, including another tenant's cell, connects to a cell's port
- **THEN** the network policy drops the connection

#### Scenario: Runtime attempts outbound access

- **WHEN** an unselected runtime process attempts any outbound connection, including DNS, or a selected runtime attempts a destination other than its designated internal artifact broker on TCP 8767
- **THEN** the connection is refused by policy

#### Scenario: Selected artifact transport remains confined

- **WHEN** an explicitly selected runtime preserves a client file through Cloud artifact transport
- **THEN** its only outbound edge is the designated internal artifact broker on TCP 8767, and the broker requires exact-file, account-bound retrieval authority
- **AND** DNS, direct public HTTPS, metadata, other cells and backup/restore access to the broker remain denied

#### Scenario: Controller attempts an out-of-scope write

- **WHEN** cellctl's ServiceAccount writes outside a cell namespace, creates a namespace without the restricted labels, or applies an image that is not digest-pinned from the cell repository
- **THEN** admission denies the request

#### Scenario: A cell fills its volume

- **WHEN** a cell writes until its volume is full
- **THEN** that cell's writes fail with a storage error
- **AND** neighbouring cells' volumes, the node's root filesystem and the pool's other volumes keep their free space

#### Scenario: A clone claims another cell's snapshot

- **WHEN** a claim in one cell's namespace names a snapshot of a different cell's volume as its source
- **THEN** admission denies the claim

#### Scenario: Controller writes a volume outside its allowance

- **WHEN** cellctl patches a persistent volume whose claim is not in a cell namespace, or changes any field of a cell's volume other than setting its reclaim policy to Retain
- **THEN** admission denies the request

#### Scenario: Controller deletes a claim whose volume would be deleted with it

- **WHEN** cellctl deletes a cell's volume claim while the bound volume's reclaim policy is Delete
- **THEN** admission denies the request

### Requirement: Each cell is backed up consistently, encrypted with its own key

Cloud backups SHALL use a dedicated private bucket in the existing business B2
account. The controller SHALL receive a separately named key-management
credential, never the provider/master credential. That parent credential MUST NOT
be supplied to tenant runtime or backup pods. Tenant credentials SHALL be
restricted to the Cloud bucket and their own cell prefix. The controller's
key-management authority remains account-wide; bucket and prefix restrictions
MUST NOT be presented as containing that parent authority. Existing products'
buckets and credentials SHALL remain unchanged.

cellctl SHALL back up each running cell whose storage class supports snapshots hourly without stopping it, as a non-stopping hold: from a crash-consistent snapshot of the cell's volume, cloned to a read-only claim that the backup reads, with the clone and snapshot deleted when the backup ends, whatever its outcome. A cell SHALL carry at most one hold at a time, so an hourly backup never overlaps another backup or restore of the same cell. A running cell whose storage class has no snapshot support SHALL keep the nightly stopped backup in its existing window, one cell at a time, with that night's prune. cellctl SHALL also back up each cell before every image change, with the cell stopped during that copy. A backup that misses its deadline SHALL record `BACKUP_FAILED`; a stopped pre-upgrade backup that misses its deadline SHALL also restart the cell. Retention SHALL keep 24 hourly, 7 daily and 4 weekly snapshots. Pruning SHALL run at most once a day and never as part of the pre-upgrade backup. Backup concurrency SHALL be configurable per node. The cell's quota SHALL admit its serving pod together with one backup Job. An alert SHALL fire when a running cell's last successful backup is more than two hours old, or more than 26 hours old for a cell on the nightly backup.

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

### Requirement: Deleting a cell removes all of its data and destroys its key

When a row's `desired_state` becomes `deleted`, cellctl SHALL remove the cell's namespace and volume and every object version under its backup prefix. It SHALL delete its object-storage key and destroy its wrapped backup key.

It SHALL report `deleted` only after it has observed that the namespace, persistent volume, the storage backend's volume for that cell, any snapshot or clone of it, and every backup object version are all absent. A failed observation MUST NOT count as absence. A volume on a node whose destruction the operator has confirmed SHALL count as absent.

Encrypted cluster-state snapshots MAY retain the cell's Secret until their retention expires. That bound SHALL be documented in the operator runbook.

#### Scenario: Tenant deletes their account

- **WHEN** a tenant's cell row is set to `deleted`
- **THEN** the namespace, persistent volume, backend volume, its snapshots and clones, and every backup object version are eventually absent
- **AND** the per-cell object-storage key and `backup_key_wrapped` are gone, and the row reports `deleted`

#### Scenario: Backend or object listing unavailable during deletion

- **WHEN** the storage backend's volume listing or the object-version listing fails during deletion
- **THEN** the row stays `deleting` and the check is retried

### Requirement: Capacity is published from observed limits

For each node in the configured cell storage domain, cellctl SHALL publish `cell_slots` from the node's observed storage. On local storage that is the size of the node's cell storage pool, minus a reserve of twice the largest cell volume for each concurrent backup the node allows, divided by the default cell volume size. The result is further bounded by any qualified occupancy, CPU and memory limits. A cell larger than the default size SHALL consume additional slots in proportion to its size. A node whose storage capacity is not positively observed, including a node with no cell storage pool, SHALL publish zero slots. Admission SHALL count non-deleted cell rows against the sum of `cell_slots`. No reservation ledger SHALL exist that can hold capacity after a cell is gone.

#### Scenario: Node is full

- **WHEN** non-deleted cell rows equal the published `cell_slots`
- **THEN** no further cell is admitted until a node is added or a cell is deleted

#### Scenario: Storage capacity not yet observed

- **WHEN** a node has joined but its storage driver has not yet published its pool
- **THEN** the node publishes zero slots, and a later observation counts it without a manual step

#### Scenario: Capacity pass during a backup

- **WHEN** a capacity pass runs while a cell's backup snapshot and clone exist
- **THEN** the node publishes the same slots as without the backup

## ADDED Requirements

### Requirement: A lost cell node is recovered from off-site backups

The operator SHALL be able to relocate a cell whose node is lost onto other capacity, through cellctl, by recreating its volume and restoring the cell's latest backup before the cell starts. Relocation SHALL be operator-triggered, never automatic, and SHALL begin only once the old node's stop is confirmed by the same rule as node removal. It SHALL then force the cell's pod off the old node, set the old volume's reclaim policy to Retain before deleting the claim, restore into a new claim, record the new volume identity, and start the cell only after the restore succeeds.

After a node loss, recovery SHALL proceed in order:
1. etcd, from its off-site snapshots, when the server node was lost, followed by reconciling volumes: a logical volume that matches a cell row's recorded volume identity SHALL be re-adopted by an operator step that binds that existing volume, with its data, to a recreated storage-driver volume object and persistent volume, and that releases the row's recorded identity only by compare-and-set on the matched identity, so that cellctl records the new one under its class and claim checks; a logical volume that matches no row SHALL be released only by an operator step on its host; a row whose volume cannot be re-adopted SHALL be relocated from its backup, and the identity check SHALL NOT be skipped;
2. replacement capacity;
3. the owner's cell;
4. the other cells by rollout priority.

The recovery point for a node loss SHALL be no older than one hour. The fleet recovery time SHALL be at most four hours, including bringing up replacement capacity.

A node-loss drill SHALL be run on disposable infrastructure before any tenant cell uses local storage, and repeated on production by destroying a recovery agent that carries the dedicated-node reservation and holds only a seeded test cell selected onto it. Its measured recovery point and recovery time SHALL be recorded.

#### Scenario: Agent holding cells is destroyed

- **WHEN** an agent holding cells is destroyed and the operator relocates its cells onto remaining capacity
- **THEN** each relocated cell answers recall with the notes from its latest backup, reports the same governance schema, and accepts a governed write
- **AND** no relocated cell's data is older than one hour before the loss

#### Scenario: Node only partitioned

- **WHEN** a node stops reporting but its cells may still be running
- **THEN** no cell is relocated until the operator triggers it and the node's stop is confirmed

#### Scenario: Restore interrupted

- **WHEN** a relocation's restore Job fails or is interrupted
- **THEN** the cell does not start, and the retained old volume is untouched

#### Scenario: etcd restored from an older snapshot

- **WHEN** etcd is restored from a snapshot older than the latest volume changes
- **THEN** every logical volume recorded on a cell row is re-adopted with its data, and no logical volume is released except by an operator step on its host

### Requirement: A local cell grows online before it fills

cellctl SHALL grow a running cell's local volume online when its filesystem passes 80% use, observed by its hourly backup. Each growth SHALL add one default cell size, up to a configured cap per cell, and at most one growth SHALL run per backup. cellctl SHALL grow a cell only while its node's published free bytes cover the step and the larger snapshot reserve the new size implies. Otherwise it SHALL raise an alert and leave the size unchanged. A grown cell SHALL never be rendered with a smaller claim, and SHALL consume slots for its grown size.

#### Scenario: Cell passes 80% use

- **WHEN** a local cell's hourly backup reports more than 80% filesystem use, and its node has room for the step
- **THEN** cellctl expands the cell's claim by one default cell size without restarting the cell

#### Scenario: Node has no room to grow

- **WHEN** a local cell passes 80% use, but its node's free bytes do not cover the step and the larger reserve
- **THEN** cellctl raises an alert and leaves the cell's size unchanged

### Requirement: A cell never initializes an empty vault over a lost volume

A cell whose Secret records that it has a completed backup SHALL refuse to initialise an empty vault on an empty volume. It SHALL remain not ready, and SHALL report a value-free reason that reaches the cell's row. Recording the backup SHALL NOT change the cell's pod template. A cell with no recorded backup SHALL initialise an empty vault as before.

#### Scenario: Volume recreated after a node loss

- **WHEN** a previously backed-up cell starts on an empty volume
- **THEN** initialisation refuses, the cell stays not ready, its row shows the refusal reason, and no empty vault is served

#### Scenario: New cell

- **WHEN** a new cell with no recorded backup starts on an empty volume
- **THEN** it initialises an empty vault and becomes ready

#### Scenario: First backup recorded

- **WHEN** a cell's first backup completes
- **THEN** the cell keeps running without a restart, and its render digest is unchanged
