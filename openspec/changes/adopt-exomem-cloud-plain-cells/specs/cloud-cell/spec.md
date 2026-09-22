## ADDED Requirements

### Requirement: A cloud cell runs the standalone runtime with its own custody

When `EXOMEM_CLOUD_CELL=1`, the runtime SHALL start on the standalone path: local runtime activation, the authorization-session middleware, in-process schema migration and the local writer lease. It MUST NOT load hosted cell configuration, projected custody, serving membership or attestation state, and MUST NOT read a `.env` file. Standalone custody SHALL resolve under the image user's home directory on the tenant volume, so that custody survives pod replacement and is included in backups.

#### Scenario: First start on an empty volume

- **WHEN** a cloud cell starts with an empty tenant volume mounted at `/data`
- **THEN** it creates the vault at `EXOMEM_VAULT_PATH` and standalone custody under `/data/host`
- **AND** it becomes ready without any external custody, attestation or migration step

#### Scenario: Governed write, then pod replacement

- **WHEN** a governed write commits and the pod is deleted and recreated on the same volume
- **THEN** the new pod serves recall that includes the committed write
- **AND** it accepts further governed writes without any acknowledgement from outside the cell

### Requirement: A cloud cell authenticates only its per-cell bearer

A cloud cell SHALL authenticate every MCP request by comparing the presented bearer against `EXOMEM_CLOUD_CELL_TOKEN` in constant time, and SHALL reject any other credential. It SHALL register only the MCP endpoint and `GET /health`, and MUST NOT register REST, upload or download routes. `/health` MUST NOT disclose vault content.

#### Scenario: Request without the cell bearer

- **WHEN** a request reaches `/mcp` with a missing, malformed or different bearer
- **THEN** the cell answers 401 without executing a tool

#### Scenario: REST route requested

- **WHEN** any caller requests `/api/<command>`, `/upload` or `/download` on a cloud cell
- **THEN** the route does not exist

### Requirement: A cloud cell publishes the product surface minus technical exclusions

A cloud cell SHALL publish the standard product tool surface, minus the members of `CLOUD_SURFACE_EXCLUSIONS`. Every exclusion SHALL record the command, the technical reason it cannot work in a cell, and the condition that lifts it. An exclusion MUST NOT exist only because some profile pins its membership.

#### Scenario: Excluded command

- **WHEN** a client lists tools on a cloud cell
- **THEN** `transfer_artifact`, `adopt_vault`, `process_media` and `read_media` are absent
- **AND** every other product tool, including `configure_memory` and `activate_context`, is present

### Requirement: A cloud cell keeps content out of logs

A cloud cell SHALL enable content-private logging. Query text, arguments, note bodies and results MUST NOT appear in runtime or access logs; only content-free identifiers, sizes, durations and error codes may appear.

#### Scenario: A recall is logged

- **WHEN** a client runs a recall containing a distinctive phrase
- **THEN** no log line from the cell contains that phrase

### Requirement: The cell controller converges desired rows without its own state

cellctl SHALL read desired cell state from the control database and converge the cluster to it with Kubernetes server-side apply under one field manager. It SHALL write observed state back, and SHALL hold no database, lease, fence or checkpoint of its own.

- A transient condition MUST be recorded as a non-terminal observed state and retried. Transient conditions include a pending volume, a pulling image, a terminating pod and an unavailable API.
- Only an identity conflict SHALL mark a cell `failed`: an existing namespace or volume that belongs to a different cell.
- `observed_generation` SHALL advance only when the observation matches the applied desired generation.

#### Scenario: New cell requested

- **WHEN** a row is inserted with `desired_state = running`
- **THEN** cellctl creates the cell's resources, reports `provisioning` while the volume binds and the pod starts, and reports `running` and ready once `/health` answers

#### Scenario: Controller restarts mid-apply

- **WHEN** cellctl stops after applying some of a cell's resources
- **THEN** the next pass re-applies the same manifests with no duplicate resources and no failure state

#### Scenario: Foreign namespace

- **WHEN** namespace `exo-cell-<cell_id>` exists with a cell label that differs from the row
- **THEN** cellctl marks the row `failed` with an identity-conflict code and does not modify that namespace

### Requirement: Cells are isolated per tenant

Each cell SHALL have its own namespace, ResourceQuota, encrypted volume, Secret and backup key. Its namespace SHALL enforce Pod Security `restricted`. Its pods SHALL run non-root, with seccomp `RuntimeDefault`, a read-only root filesystem, dropped capabilities and no ServiceAccount token.

A default-deny network policy SHALL admit runtime ingress only from the cloud gateway, and MUST NOT grant the runtime pod internet egress. No request field, path or header SHALL select a cell.

#### Scenario: Another workload attempts direct access

- **WHEN** a pod other than the gateway, including another tenant's cell, connects to a cell's port
- **THEN** the network policy drops the connection

#### Scenario: Runtime attempts outbound internet access

- **WHEN** the runtime process attempts an outbound connection to an internet address
- **THEN** the connection is refused by policy

### Requirement: A release is one image digest rolled out one cell at a time

The platform release SHALL be the digest in the `cell_image` setting. When it changes, cellctl SHALL:

1. take a pre-upgrade snapshot of each cell before changing its image;
2. move cells one at a time, lowest `rollout_priority` first;
3. advance only after the new pod is ready.

If a cell is not ready within 10 minutes, cellctl SHALL return that cell to its previous digest, pause the rollout and record the error. A row's own `desired_image` SHALL override the setting for that cell. No candidate, lock, fixture or promotion artifact SHALL be required to release.

#### Scenario: Upgrade succeeds

- **WHEN** the `cell_image` setting changes to a new digest
- **THEN** the canary cell is snapshotted, restarted on the new digest and becomes ready, and then the next cell follows
- **AND** the vault content is unchanged and recall answers as before

#### Scenario: Canary fails readiness

- **WHEN** the canary cell is not ready on the new digest within 10 minutes
- **THEN** cellctl restores the previous digest on that cell, sets `rollout_paused`, and changes no other cell

### Requirement: Each cell is backed up encrypted with its own key

Each cell SHALL be backed up nightly, and before every image change, to object storage under its own prefix. The backup SHALL be encrypted with a random per-cell data key that is envelope-encrypted by a master key held outside the database. A backup SHALL include both the vault and the custody home. A restore into a new namespace SHALL produce a cell that serves the same notes.

#### Scenario: Restore drill

- **WHEN** a backup is restored into a scratch namespace and a cell starts on it
- **THEN** recall returns the notes that existed at backup time
- **AND** the source cell is unaffected

### Requirement: Deleting a cell removes all of its data and destroys its key

When a row's `desired_state` becomes `deleted`, cellctl SHALL remove the cell's namespace, volume and backup prefix, and SHALL destroy its wrapped backup key. It SHALL report `deleted` only after it has observed that the namespace, persistent volume, provider volume and backup prefix are all absent. A failed observation MUST NOT count as absence.

#### Scenario: Tenant deletes their account

- **WHEN** a tenant's cell row is set to `deleted`
- **THEN** the namespace, persistent volume, provider volume and backup objects are all eventually absent, and `backup_key_wrapped` is null
- **AND** the row reports `deleted`

#### Scenario: Provider listing unavailable during deletion

- **WHEN** the provider volume listing fails during deletion
- **THEN** the row stays `deleting` and the check is retried

### Requirement: Capacity is published from observed limits

cellctl SHALL publish, for each node, the provider volume attachments in use, the provider's per-server limit, and the configured headroom. Admission SHALL use this published capacity. No reservation ledger SHALL exist that can hold capacity after a cell is gone.

#### Scenario: Node is full

- **WHEN** non-deleted rows equal the published capacity
- **THEN** the published capacity leaves no room for another cell until a node is added or a cell is deleted

### Requirement: Vault traffic is TLS-terminated only on our own servers

The cloud MCP hostname SHALL be served by the platform ingress with certificates obtained through ACME, and its DNS record SHALL NOT be proxied by a third party. Only the gateway route SHALL be public. Cells, cellctl and the cluster API MUST NOT be reachable from the internet.

#### Scenario: Public request to a cell or controller

- **WHEN** an internet client requests any host or path other than the gateway's routes
- **THEN** no cell, controller or cluster API responds

### Requirement: The control database runs on its own server with point-in-time recovery

The control database SHALL run on a server separate from any fleet node, provisioned by the same Terraform and Ansible tree. It SHALL:

- use TLS with verify-full for every client;
- give the gateway and cellctl least-privilege roles;
- archive WAL to object storage for point-in-time recovery;
- have its backup restore verified weekly.

#### Scenario: Fleet node is lost

- **WHEN** the fleet node is destroyed
- **THEN** the control database, with every tenant and cell row, is unaffected, and cells can be restored onto a rebuilt node from their backups

#### Scenario: Controller exceeds its privileges

- **WHEN** cellctl attempts to modify a desired-state column or any table other than cell observations, capacity and wrapped keys
- **THEN** the database refuses the statement
