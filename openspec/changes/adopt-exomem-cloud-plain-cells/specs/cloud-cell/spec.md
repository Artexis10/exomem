## ADDED Requirements

### Requirement: A cloud cell runs the standalone runtime with its own custody

When `EXOMEM_CLOUD_CELL=1`, the runtime SHALL start on the standalone path: local runtime activation, the authorization-session middleware and the local writer lease. It MUST NOT load hosted cell configuration, projected custody, serving membership or attestation state, and MUST NOT read a `.env` file.

Standalone custody SHALL resolve under the image user's home directory on the tenant volume. Custody and writer-lease state SHALL keep their owner-only file modes across first start, pod replacement and restore.

Before the server starts, a same-image init container SHALL, idempotently:

1. initialize the vault when absent;
2. run offline state migration;
3. migrate a schema-v3 governance store to v4 through the standalone plan, stage and commit sequence.

#### Scenario: First start on an empty volume

- **WHEN** a cloud cell starts with an empty tenant volume mounted at `/data`
- **THEN** the init container creates the vault and standalone custody, migrates state, and brings governance to schema v4
- **AND** the runtime becomes ready, with no external custody, attestation or operator step

#### Scenario: Governed write, then pod replacement

- **WHEN** a governed write commits and the pod is deleted and recreated on the same volume
- **THEN** custody and writer-lease files keep owner-only modes
- **AND** the new pod serves recall that includes the write, and accepts a further governed write

### Requirement: A cloud cell authenticates only its per-cell bearer as a fixed non-owner principal

A cloud cell SHALL authenticate every MCP request by comparing the presented bearer, in constant time, against `EXOMEM_CLOUD_CELL_TOKEN` or, during a rotation, `EXOMEM_CLOUD_CELL_TOKEN_PREVIOUS`, and SHALL reject any other credential.

A request that authenticates SHALL carry the fixed claims `{sub: <cell_id>, iss: "exomem-cloud-cell"}`. Those claims resolve to a non-owner principal, and MUST NOT be derived from the bearer or its key version.

The cell SHALL register only the MCP endpoint, `GET /health` and `GET /health/ready`. It MUST NOT register REST, upload or download routes. Health routes MUST NOT disclose vault content.

#### Scenario: Request without the cell bearer

- **WHEN** a request reaches `/mcp` with a missing, malformed or different bearer
- **THEN** the cell answers 401 without executing a tool

#### Scenario: Governed write and an owner-only operation through the bearer

- **WHEN** an authenticated client performs a governed product write, and then an owner-only governance authoring operation
- **THEN** the write commits and is recalled
- **AND** the owner-only operation is refused with `GOVERNANCE_OWNER_REQUIRED`

#### Scenario: REST route requested

- **WHEN** any caller requests `/api/<command>`, `/upload` or `/download` on a cloud cell
- **THEN** the route does not exist

### Requirement: A cloud cell publishes the product surface minus technical exclusions

A cloud cell SHALL publish the standard product tool surface, minus the members of `CLOUD_SURFACE_EXCLUSIONS`. Every exclusion SHALL record the command, the technical reason it cannot work in a cell, and the condition that lifts it. An exclusion MUST NOT exist only because some profile pins its membership.

#### Scenario: Excluded command

- **WHEN** a client lists tools on a cloud cell
- **THEN** `transfer_artifact`, `adopt_vault`, `process_media` and `read_media` are absent
- **AND** every other product tool, including `configure_memory` and `activate_context`, is present

### Requirement: A cloud cell can serve read-only

With `EXOMEM_CLOUD_READ_ONLY=1`, a cloud cell SHALL refuse every mutating command with `CLOUD_CELL_READ_ONLY` before vault access, and SHALL continue to serve every read. Mutation SHALL be classified from the command registry.

#### Scenario: Write while read-only

- **WHEN** a client calls a mutating tool on a read-only cell
- **THEN** the call is refused with `CLOUD_CELL_READ_ONLY` and the vault is unchanged
- **AND** a recall on the same cell succeeds

### Requirement: A cloud cell keeps content out of logs

A cloud cell SHALL enable content-private logging and the content-free call trace. Query text, arguments, note bodies and results MUST NOT appear in runtime or access logs; only content-free identifiers, sizes, durations and error codes may appear.

#### Scenario: A recall is logged

- **WHEN** a client runs a recall containing a distinctive phrase
- **THEN** no log line from the cell contains that phrase

### Requirement: The cell controller converges desired rows without its own state

cellctl SHALL read desired cell state from the control database and converge the cluster to it with Kubernetes server-side apply under one field manager. It SHALL write observed state back, and SHALL hold no database, lease, fence or checkpoint of its own.

- It SHALL read readiness from pod conditions, and MUST NOT connect to a cell over the network.
- A transient condition MUST be recorded as a non-terminal observed state and retried. Transient conditions include a pending volume, a pulling image, a running init container, a terminating pod and an unavailable API.
- Only an identity conflict, or an init container still failing after its deadline, SHALL mark a cell `failed`. An identity conflict is an existing namespace or volume that belongs to a different cell.
- `observed_generation` SHALL advance only when the observation matches the applied desired generation.
- An ordinary pass SHALL render the cell's current image. Only a release attempt SHALL change it.
- A maintenance operation SHALL hold a cell's image and replica count through one hold marker. The controller SHALL record the hold kind and start time on the row, and SHALL resume the hold after a restart.

#### Scenario: New cell requested

- **WHEN** a row is inserted with `desired_state = running`
- **THEN** cellctl creates the cell's resources and reports `provisioning` while the volume binds and the init container runs
- **AND** it reports `running` and ready once the pod's readiness condition holds

#### Scenario: Controller restarts mid-apply

- **WHEN** cellctl stops after applying some of a cell's resources
- **THEN** the next pass re-applies the same manifests with no duplicate resources and no failure state

#### Scenario: Desired state changes during a nightly backup

- **WHEN** a cell's row flips to `read_only` while its backup hold is active
- **THEN** the cell stays stopped until the backup finishes, and then starts read-only on its current image

#### Scenario: Controller restarts mid-upgrade

- **WHEN** cellctl restarts while a cell carries an upgrade hold
- **THEN** the row shows the hold kind and start time, and the attempt resumes from its recorded step rather than leaving the cell stopped

#### Scenario: Foreign namespace

- **WHEN** namespace `exo-cell-<cell_id>` exists with a cell label that differs from the row
- **THEN** cellctl marks the row `failed` with an identity-conflict code and does not modify that namespace

### Requirement: Cells are isolated per tenant

Each cell SHALL have its own namespace, ResourceQuota, encrypted volume, Secret, backup key and object-storage key. Its namespace SHALL enforce Pod Security `restricted`. Its pods SHALL run non-root, with seccomp `RuntimeDefault`, a read-only root filesystem, dropped capabilities and no ServiceAccount token.

A default-deny network policy SHALL:
- admit runtime ingress only from the cloud gateway;
- grant the runtime pod no egress;
- limit backup and restore pods to object-storage egress.

No request field, path or header SHALL select a cell. An admission policy SHALL confine cellctl's own writes to cell namespaces, restricted namespaces and digest-pinned images from the configured repository.

#### Scenario: Another workload attempts direct access

- **WHEN** a pod other than the gateway, including another tenant's cell, connects to a cell's port
- **THEN** the network policy drops the connection

#### Scenario: Runtime attempts outbound access

- **WHEN** the runtime process attempts any outbound connection, including DNS
- **THEN** the connection is refused by policy

#### Scenario: Controller attempts an out-of-scope write

- **WHEN** cellctl's ServiceAccount writes outside a cell namespace, creates a namespace without the restricted labels, or applies an image that is not digest-pinned from the cell repository
- **THEN** admission denies the request

### Requirement: A release is one image digest, rolled out one cell at a time with restore-based rollback

The platform release SHALL be the digest in the `cell_image` setting.

- A cell's upgrade target SHALL be its row's `desired_image` if set, otherwise `cell_image`.
- A new cell SHALL start on `desired_image` if set, otherwise on `cell_image` while the rollout is not paused, otherwise on `last_good_image`. While paused with no `last_good_image`, a new cell SHALL wait with `NO_GOOD_IMAGE`.
- `last_good_image` SHALL be set whenever a cell becomes ready on `cell_image`, including at first provisioning.

cellctl SHALL change at most one cell's image at a time, lowest `rollout_priority` first. Each attempt SHALL:

1. stop the cell;
2. take a backup, within a bounded deadline;
3. start the target image;
4. wait up to 10 minutes for readiness.

If the backup misses its deadline, cellctl SHALL restart the cell on its current image, record `BACKUP_FAILED`, and retry later. It MUST NOT leave the cell stopped on an object-storage failure.

If readiness does not arrive, cellctl SHALL:

- stop the cell;
- restore the pre-attempt backup so that no file created by the new image remains;
- start the previous image;
- pause the rollout, recording the error and the held cell.

It MUST NOT roll back by image digest alone, and MUST NOT start either image on an unrestored volume. While paused, no cell SHALL change image.

No candidate, lock, fixture or promotion artifact SHALL be required to release.

#### Scenario: Upgrade succeeds

- **WHEN** the `cell_image` setting changes to a new digest
- **THEN** the canary cell is stopped, backed up, started on the new digest and becomes ready
- **AND** `last_good_image` becomes the new digest, the next cell follows, and vault content and recall are unchanged

#### Scenario: Canary fails readiness after migrating state

- **WHEN** the canary's new image migrates state and then fails to become ready within 10 minutes
- **THEN** cellctl restores the pre-attempt backup, starts the previous image, and pauses the rollout, recording the error and the held cell
- **AND** no other cell changes, and the canary serves its pre-attempt content

#### Scenario: Object storage unavailable during an upgrade

- **WHEN** the pre-upgrade backup cannot reach object storage before the backup deadline
- **THEN** the cell restarts on its current image, the row records `BACKUP_FAILED`, and the attempt is retried after a backoff

#### Scenario: First release fails

- **WHEN** the very first `cell_image` fails readiness on the canary, and no `last_good_image` exists
- **THEN** the rollout pauses, and new cells wait with `NO_GOOD_IMAGE` instead of starting on no image

### Requirement: Each cell is backed up consistently, encrypted with its own key

cellctl SHALL back up each cell nightly and before every image change. The cell SHALL be stopped during the copy, so the copy is crash-consistent. A backup that misses its deadline SHALL restart the cell and record `BACKUP_FAILED`.

A backup SHALL:
- include both the vault and the custody home;
- be written to the cell's own object-storage prefix with a key restricted to that prefix;
- be encrypted with a random per-cell data key, envelope-encrypted by a master key held outside the database.

The wrapped data key and the wrapped per-cell object-storage key SHALL be stored once on the cell row, so that a stateless controller can re-render the cell's Secret and delete the key later.

A restore into a new namespace SHALL produce a cell that answers recall, reports governance schema v4, and accepts a governed write.

#### Scenario: Restore drill

- **WHEN** a backup is restored into a scratch namespace and a cell starts on it
- **THEN** recall returns the notes that existed at backup time, governance status reports schema v4, and a governed write commits
- **AND** the source cell is unaffected

#### Scenario: Backup job attempts another tenant's prefix

- **WHEN** a cell's backup credentials are used against another cell's prefix
- **THEN** object storage refuses the request

### Requirement: Deleting a cell removes all of its data and destroys its key

When a row's `desired_state` becomes `deleted`, cellctl SHALL remove the cell's namespace and volume and every object version under its backup prefix. It SHALL delete its object-storage key and destroy its wrapped backup key.

It SHALL report `deleted` only after it has observed that the namespace, persistent volume, provider volume and every backup object version are all absent. A failed observation MUST NOT count as absence.

Encrypted cluster-state snapshots MAY retain the cell's Secret until their retention expires. That bound SHALL be documented in the operator runbook.

#### Scenario: Tenant deletes their account

- **WHEN** a tenant's cell row is set to `deleted`
- **THEN** the namespace, persistent volume, provider volume and every backup object version are eventually absent
- **AND** the per-cell object-storage key and `backup_key_wrapped` are gone, and the row reports `deleted`

#### Scenario: Provider listing unavailable during deletion

- **WHEN** the provider volume listing or object-version listing fails during deletion
- **THEN** the row stays `deleting` and the check is retried

### Requirement: Capacity is published from observed limits

For each node, cellctl SHALL publish `cell_slots`, equal to the provider's per-server attachment limit minus configured headroom minus attachments not owned by cells, together with attachments in use. Admission SHALL count non-deleted cell rows against the sum of `cell_slots`. No reservation ledger SHALL exist that can hold capacity after a cell is gone.

#### Scenario: Node is full

- **WHEN** non-deleted cell rows equal the published `cell_slots`
- **THEN** no further cell is admitted until a node is added or a cell is deleted

### Requirement: Vault traffic is TLS-terminated only on our own servers

The cloud MCP hostname SHALL be served by the platform ingress, exposed only through its TLS entrypoint. Its certificate SHALL be obtained through ACME, and its DNS record SHALL NOT be proxied by a third party. Only the gateway route SHALL be public. Cells, cellctl, the plaintext entrypoint and the cluster API MUST NOT be reachable from the internet.

#### Scenario: Public request to anything but the gateway

- **WHEN** an internet client requests any host, port or path other than the gateway's routes on 443
- **THEN** no cell, controller, plaintext entrypoint or cluster API responds

### Requirement: The control database runs on its own server with point-in-time recovery

The control database SHALL run on a server separate from any fleet node, provisioned by the same Terraform and Ansible tree. It SHALL:

- use TLS with verify-full for every client;
- separate the schema-owner role from runtime roles;
- give the gateway and cellctl least-privilege roles, reachable only over the private network;
- admit only the Substrate roles on its public listener, with SCRAM;
- serve migrations through a session-mode pool, so a migration's session lock never leaks;
- issue and renew its own public certificate on the server;
- archive WAL to object storage for point-in-time recovery;
- have its backup restore verified weekly.

#### Scenario: Fleet node is lost

- **WHEN** the fleet node is destroyed
- **THEN** the control database, with every tenant and cell row, is unaffected, and cells can be restored onto a rebuilt node from their backups

#### Scenario: Controller exceeds its privileges

- **WHEN** cellctl attempts to modify a desired-state column, the settings table, or any table other than cell observed columns (including wrapped keys), capacity and the rollout row
- **THEN** the database refuses the statement

#### Scenario: Gateway role from the internet

- **WHEN** a client on the public listener authenticates as the gateway or cellctl role
- **THEN** the connection is refused
