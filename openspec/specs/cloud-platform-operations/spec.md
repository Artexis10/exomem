# Cloud Platform Operations

## Purpose

Preserve the infrastructure contracts that support standalone Cloud cells after Hosted retirement.
The platform uses Terraform, Ansible, Helm and cellctl as its existing operating tools.

## Requirements

### Requirement: Terraform state is remote, locked, versioned, and recoverable

The foundation and durability roots SHALL use their declared remote HCP Terraform workspaces.
Each workspace SHALL use local execution, the pinned Terraform version, no VCS attachment and no automatic apply.
State sharing SHALL remain disabled unless the declared workspace contract requires it.
The existing wrappers SHALL verify the workspace contract before initialization and apply only a reviewed saved plan.
HCP Terraform SHALL preserve state history and serialize writers.
State credentials, binary plans and raw state SHALL stay outside public artifacts and logs.
Recovery SHALL duplicate a selected prior state version under an operator lock, then require a refresh-only resource review.

#### Scenario: A second writer races a held state lock
- **WHEN** one writer holds the state lock and another writer attempts a mutation
- **THEN** the second writer makes no state or provider change

#### Scenario: A prior version is recovered
- **WHEN** the current state cannot be used
- **THEN** the operator recovers a selected prior version and reviews actual resources before another apply

### Requirement: Host bootstrap is pinned, hardened, and idempotent

Ansible SHALL install artifacts pinned by the repository tool and image contracts.
Host bootstrap SHALL configure updates, key-only administration, a host firewall, time synchronization and bounded disk and log use.
K3s SHALL use secrets encryption, metadata-only credential audit records and off-host encrypted etcd snapshots.
The operator-access and node-pool contracts SHALL govern administrator credentials, node admission and recovery access.
A second complete convergence SHALL produce no unintended changes and leave the cluster ready.

#### Scenario: Bootstrap is repeated
- **WHEN** the complete site playbook runs twice against a healthy declared fleet
- **THEN** the second run leaves the cluster ready without unintended changes

#### Scenario: A Secret enters the API
- **WHEN** the API receives a Secret or service-account token operation
- **THEN** audit records retain attribution metadata and omit credential bodies and issued tokens

### Requirement: Platform releases are immutable and reproducible

The platform SHALL install Cloud control workloads, CSI, ingress, static secrets, schedules and observability from pinned release inputs.
Provider locks SHALL be committed, chart versions SHALL be pinned and first-party images SHALL use immutable digests.
The existing chart SHALL render the same release inventory from the same inputs on an empty cluster.
Cell rollout SHALL remain cellctl's responsibility under the Cloud cell release contract.
Hosted pricing receipts and the retired capacity collector SHALL NOT gate a Cloud deployment.

#### Scenario: An empty cluster receives the release
- **WHEN** the operator installs the same reviewed platform inputs on a fresh cluster
- **THEN** the rendered inventory and immutable image references match the reviewed release

### Requirement: External schedules remain exact and observable

The platform SHALL consume the checked versioned schedule contract at `infra/contracts/exomem-hosted-schedules-v1.json`.
The contract SHALL define exactly the kept access-delivery and reconciliation routes and their cadences.
Renderers SHALL use its origin, method, path, redirect refusal, timeouts, deadlines, retries, history and metric settings exactly.
The dedicated scheduler bearer SHALL authorize only the declared routes, with the contract's receiver overlap during rotation.
Global cron, database, payment, cell and provider credentials SHALL NOT enter scheduler Jobs.
The scheduler SHALL suspend when both Cloud control workloads are disabled.
The removed export garbage collector SHALL NOT be rendered or required by Cloud.

#### Scenario: A schedule renders
- **WHEN** the platform selects the supported schedule contract
- **THEN** each rendered Job matches its declared request, execution and content-free observability settings

#### Scenario: A response redirects or fails
- **WHEN** a scheduler request receives a redirect or a status outside the contract's success set
- **THEN** the Job records a content-free failure without following the redirect or retaining the response body

### Requirement: Encrypted volumes retain provider identity for recovery

Cloud cell volumes SHALL follow the canonical Cloud cell and node-pool storage contracts.
The shared CSI class selected by `storage.className` SHALL use tested encryption and `Retain`.
Cloud cell CSI volumes SHALL use tested encryption and the Cloud cell contract's reclaim policy.
Each CSI volume SHALL preserve the bound provider volume ID in external control state.
Local volumes SHALL follow their explicit source-managed storage and recovery contract rather than claim CSI survivability.
Recovery SHALL verify the recorded volume identity before reattachment or static PV/PVC reconstruction.
Namespace absence SHALL NOT prove provider-volume destruction.

#### Scenario: A namespace disappears outside ordered deletion
- **WHEN** a cell namespace is removed while its retained CSI volume remains
- **THEN** the volume remains identifiable and the controller does not report storage destruction

#### Scenario: Kubernetes state is lost
- **WHEN** the original provider volume survives an empty-cluster reconstruction
- **THEN** recovery binds that verified volume and checks its canonical data before reopening the cell

### Requirement: Secrets have one recoverable handoff path

Static secrets SHALL use the versioned destination matrix and SOPS/age ciphertext, with age custody outside the cluster.
Cellctl SHALL reconstruct dynamic cell Secrets from encrypted external control records under the Cloud cell secret contract.
The existing handoff SHALL keep plaintext outside repository files, shell history, plans and logs.
It SHALL verify each external destination identity, serialize destination writes and reserve immutable content-free version evidence before uncertain writes.
It SHALL publish every selected local ciphertext before an external destination write and never overwrite an existing or lower version.
Incomplete handoff SHALL retain confirmed and uncertain destination evidence and recover with a higher destination version.
Equal version numbers at different destinations SHALL NOT assert equal plaintext.
The active-secret registry SHALL bind the current matrix, exact active selection and ciphertext digests before application.
Ansible SHALL consume secret variables through the existing private ephemeral transport and prevent passthrough overrides.
Removing a destination SHALL NOT imply provider revocation or deletion of a manually managed Secret.

#### Scenario: A handoff stops partway
- **WHEN** a destination write has an uncertain outcome
- **THEN** its reserved version remains consumed and recovery uses a higher version without overwriting evidence

#### Scenario: The cluster is reconstructed
- **WHEN** the old etcd state is unavailable
- **THEN** static ciphertext and external dynamic records reconstruct the declared Secret versions

### Requirement: Declarative reconstruction is the primary recovery path

Recovery SHALL reconstruct hosts and platform state from Terraform, Ansible and Helm.
It SHALL restore external control records and verify original volumes or restore verified B2 backups through the existing helpers.
Reconciliation SHALL reconstruct declared Secrets and routes before authenticated readiness.
Off-host etcd snapshots SHALL remain a secondary recovery source rather than the only recovery procedure.
Restored cluster resources SHALL be checked against current control records before ingress reopens.

#### Scenario: A node is permanently lost
- **WHEN** the operator replaces it from an empty declared host
- **THEN** the existing runbooks recover cells without copying untracked configuration from the old node

### Requirement: Monitoring spans independent failure domains

The platform SHALL use its checked observability contract for external availability and backup-age checks, scheduler metrics and alerts.
At least one availability and backup-age signal SHALL run outside the production node.
Signals SHALL distinguish unavailable, stale, failed and unknown from a successful zero value.
Logs SHALL retain operational identifiers and timing without credentials, identity claims, vault content or response bodies.
Alerts SHALL cover unavailable cells, recall that serves a stalled catalogue, overdue work, backup age, restore failure, disk and node pressure, scheduler failure, secret-version mismatch and attachment exhaustion.
Thresholds SHALL come from the checked observability and schedule contracts rather than obsolete Hosted limits.
A Cloud cell SHALL serve its readiness answer on a health-only port that answers no other request.
cellctl SHALL read cell readiness only on that port, and network policy and admission SHALL admit cellctl to no other cell port.
The stalled-recall alert SHALL fire while a serving cell reports its recall lagging on a catalogue that the cell has measured unchanged for at least the contract threshold.
It SHALL resolve only when the cell reads its recall strictly ready; an unread cell, a lag with no age and a younger lag SHALL leave it unchanged.

#### Scenario: The production node disappears
- **WHEN** the node and in-cluster monitors become unavailable
- **THEN** an external signal detects unavailable service and stale backup evidence

#### Scenario: A ready cell's recall catalogue stops advancing
- **WHEN** a serving cell stays ready while it reports recall lagging on a catalogue unchanged for the contract threshold
- **THEN** cellctl sends the stalled-recall alert through the alert receiver and logs only the cell's id

#### Scenario: A cell restarts during a stall
- **WHEN** the restarted cell reports a younger lag, or does not answer
- **THEN** cellctl sends no resolution until the cell reads its recall strictly ready

#### Scenario: cellctl reaches for the cell port
- **WHEN** cellctl connects to a cell's MCP port, or writes cell ingress that would admit it there
- **THEN** network policy refuses the connection and admission refuses the write

#### Scenario: The health-only port receives another request
- **WHEN** a peer sends the health-only port any request other than `GET /health/ready`
- **THEN** the cell answers 404 with an empty body

### Requirement: Alert delivery terminates outside the cluster failure domain

Alert transitions SHALL terminate at an owned endpoint outside K3s.
The receiver SHALL preserve the existing exact HTTPS target, redirect refusal, bounded request and compact content-free payload contract.
Only the sender SHALL hold the URL capability; the receiver SHALL retain its digest.
The receiver SHALL authenticate and validate requests, deduplicate transition identity and commit durably before acknowledging.
Notification delivery SHALL remain outside the acknowledgement path, with bounded retries under an exclusive lease and an observable failed backlog.

#### Scenario: Notification is slow
- **WHEN** notification cannot finish within the sender's budget
- **THEN** the receiver acknowledges the durable transition and completes notification outside that request

#### Scenario: Delivery retries overlap
- **WHEN** two invocations deliver the same transition
- **THEN** one holds the delivery lease and the operator receives one notification

### Requirement: Runbooks are executable and non-destructive by default

The checked runbook index SHALL cover state recovery, deployment, secret rotation, node recovery, cell recovery, export and deletion.
Runbooks SHALL use the existing plan, inspection, restore, export and rehearsal tools.
Read-only inspection and scratch drills SHALL precede destructive disposal or production recovery.
A destructive operation SHALL identify its target and require explicit operator authorization.
Routine cell creation SHALL require no shell repair or direct database mutation.

#### Scenario: A new operator follows the documented path
- **WHEN** an authorized operator deploys and inspects a cell using the checked runbooks
- **THEN** the cell becomes ready without undocumented configuration or shell repair

### Requirement: Hosted credential retirement keeps an explicit pending inventory

The retirement runbook SHALL inventory the 29 original secret records, their 34 destinations and eight collector destinations, with 42 destination records total.
Each destination SHALL remain pending until the operator records provider revocation and any materialized-resource absence separately.
Each of the eight obsolete BWS entries SHALL require a separate operator go before deletion.
Orphan Secrets, ConfigMaps, tenant data and Terraform resources SHALL require separate disposal authority and evidence.
Kept Cloud, scheduler, artifact, CSI, volume-encryption and alert destinations SHALL remain active.

#### Scenario: Repository retirement ships
- **WHEN** the retired destination entries and ciphertext files leave the repository
- **THEN** the operator inventory remains pending and makes no claim that live keys or data were deleted
