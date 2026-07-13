## ADDED Requirements

### Requirement: Cloud lifecycle domains are independently planned
The hosted infrastructure SHALL use separate Terraform roots and remote state for `foundation` and `durability`. Foundation SHALL own Hetzner compute/network/firewall and Cloudflare Tunnel/DNS/Access; durability SHALL own B2 buckets, retention, Object Lock, and backup credentials. Production mutation MUST apply an exact reviewed saved plan, SHALL reject unapproved destroy/replacement actions, and MUST NOT run Terraform from account provisioning.

#### Scenario: Durability credential change cannot replace the node
- **WHEN** a B2 application-key change is planned in `durability`
- **THEN** the plan contains no foundation resource and cannot alter or replace the K3s server

#### Scenario: Unexpected replacement is blocked
- **WHEN** a production plan contains an unapproved destroy or replacement action
- **THEN** the plan guard fails before apply and emits the affected resource addresses without sensitive values

### Requirement: Terraform state is remote, locked, versioned, and recoverable
Both Terraform roots SHALL use separate keys in a versioned B2 S3-compatible backend with lockfiles. Deployment MUST remain blocked until the real account proves mutual exclusion for concurrent writers and recovery of a prior state version. State credentials, state files, and sensitive backend configuration MUST NOT be committed or printed.

#### Scenario: Concurrent writer is rejected
- **WHEN** one process holds the foundation state lock and a second process attempts a mutating operation
- **THEN** the second process fails without writing state or cloud resources

#### Scenario: Prior state version is recovered
- **WHEN** the latest test state object is deliberately made unusable
- **THEN** the documented procedure restores a prior version and a refresh-only plan matches the expected resources

### Requirement: Host bootstrap is pinned, hardened, and idempotent
Ansible SHALL configure a declared Linux image with security updates, unattended upgrades, hardened key-only SSH, host firewall, fail2ban, time sync, log/disk hygiene, `cryptsetup`, and verified installation artifacts. It SHALL install pinned K3s `v1.35.6+k3s1` with embedded etcd, secrets encryption, metadata-safe audit policy, bounded logs/images, and off-host encrypted snapshots. Normal administration SHALL use a restricted credential and SHALL keep cluster-admin as offline break glass.

#### Scenario: Second bootstrap converges cleanly
- **WHEN** the complete Ansible site playbook runs twice against the same healthy node
- **THEN** the second run reports no unintended changes and K3s remains ready

#### Scenario: Secret bodies are absent from audit logs
- **WHEN** a Secret or service-account token is created through the API
- **THEN** audit records contain metadata needed for attribution but no credential body or issued bearer token

### Requirement: Platform releases are immutable and reproducible
The platform SHALL install CSI, SOPS-managed static secrets, Traefik, Cloudflare Tunnel, lightweight metrics/alerts, and the Exomem system chart from pinned versions. Provider locks SHALL be committed; Helm/chart versions and first-party image digests SHALL be recorded; mutable `latest` references SHALL be rejected.

#### Scenario: Empty-cluster platform install is deterministic
- **WHEN** the platform release is installed into a freshly bootstrapped cluster with the same inputs
- **THEN** the rendered resources and immutable image references match the reviewed release inventory and become healthy

### Requirement: Online cell volumes are encrypted, retained, and provider-identifiable
Each cell SHALL receive one minimum 10 GiB Hetzner CSI volume using a tested LUKS StorageClass and `Retain` reclaim policy. The system SHALL record the bound PV `volumeHandle`, location, and immutable opaque tenant/cell/operation/fence provider labels in external state. A privileged volume lifecycle worker, separate from routine namespaced reconciliation, SHALL use the HCloud API for label verification, retained-volume deletion proof, and clean-cluster static-PV/PVC rebind. Final provision success SHALL require that provider ownership checkpoint to be durable and independently verified.

#### Scenario: Namespace deletion retains the provider volume
- **WHEN** a test cell namespace and PVC are deleted outside the ordered destroy flow
- **THEN** the HCloud volume remains, is discoverable from provider labels, and is not reported destroyed

#### Scenario: Original volume is rebound after etcd loss
- **WHEN** a clean cluster restores a cell whose provider volume survived but Kubernetes state did not
- **THEN** recovery creates a static PV/PVC for the recorded original `volumeHandle` and the cell reads its prior canonical content

#### Scenario: Storage destruction is independently proven
- **WHEN** the ordered destroy operation removes a retained cell volume
- **THEN** the released Kubernetes PV object and recorded HCloud volume ID are both confirmed absent before `storageDestroyed` becomes true

#### Scenario: Provisioner crashes after CSI binding
- **WHEN** a volume binds and the provisioner restarts before provider registration completes
- **THEN** replay adopts the bound `volumeHandle`, records and verifies its provider labels, and does not create a second volume or return final success early

### Requirement: Static and dynamic secrets have one recoverable handoff path
Static platform secrets SHALL be SOPS/age ciphertext with the age key escrowed off-cluster. Dynamic cell credentials, fences, provider references, and wrapped keys SHALL be encrypted in the external provisioner store under a separately escrowed root key. One non-printing handoff workflow SHALL enforce a versioned per-secret destination matrix without placing plaintext in repository files, shell history, saved-plan output, or CI logs. Cloudflare Access client credentials SHALL go only to Vercel; the Tunnel credential SHALL go only to K3s; the provisioner bearer SHALL go only to its Vercel caller and K3s service; cell credentials SHALL go only from encrypted provisioner state to their cell Secret; wrapping keys SHALL go only to their named workloads and offline escrow.

#### Scenario: Clean secret reconstruction succeeds
- **WHEN** a new cluster is built without the previous etcd state
- **THEN** static secrets decrypt from SOPS and dynamic cell secrets reconcile from encrypted external state with matching recorded versions

#### Scenario: Access credential rotates without stranding either side
- **WHEN** a Cloudflare Access service token is rotated
- **THEN** overlapping Cloudflare tokens allow the new client ID/secret to be staged and verified in Vercel before the old token is revoked, while K3s receives no Access client secret

#### Scenario: Tunnel credential rotates independently
- **WHEN** the Cloudflare Tunnel credential is rotated
- **THEN** only the versioned K3s Secret changes and the Vercel Access secret remains unchanged

#### Scenario: Provisioner root key rotates without orphaning ciphertext
- **WHEN** a new root wrapping-key version becomes active
- **THEN** old and new decrypt versions overlap, new writes use the new version, every stored data key or legacy ciphertext is rewrapped or re-encrypted and verified, and the old key retires only after no live record references it

### Requirement: Declarative reconstruction is the primary cluster recovery path
The primary disaster-recovery procedure SHALL rebuild cloud/host/platform state from Terraform, Ansible, and Helm; restore or attach recorded volumes; restore external operational records; and reconcile dynamic Secrets and routes. Off-host etcd snapshots SHALL be secondary evidence and MUST NOT be the only recovery method.

#### Scenario: Production node is replaced from an empty host
- **WHEN** the original node is treated as permanently lost while cell volumes and external records remain
- **THEN** the documented reconstruction produces a working cluster and ready cells without copying untracked configuration from the old node
