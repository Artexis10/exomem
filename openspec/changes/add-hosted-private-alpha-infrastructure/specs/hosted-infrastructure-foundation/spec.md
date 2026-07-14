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
The platform SHALL install CSI, SOPS-managed static secrets, Traefik, Cloudflare Tunnel, lightweight metrics/alerts, contract-rendered external Substrate CronJobs, and the Exomem system chart from pinned versions. Provider locks SHALL be committed; Helm/chart versions and first-party image digests SHALL be recorded; mutable `latest` references SHALL be rejected.

#### Scenario: Empty-cluster platform install is deterministic
- **WHEN** the platform release is installed into a freshly bootstrapped cluster with the same inputs
- **THEN** the rendered resources and immutable image references match the reviewed release inventory and become healthy

### Requirement: Provisioner database bootstrap is packaged, serialized, and split-authority
The immutable provisioner image SHALL carry byte-matching Alembic configuration and revisions at a fixed root-owned, non-writable, recursively non-symlinked path, SHALL expose environment-only bootstrap, runtime-migration, and exact-head-validation commands, and SHALL require one packaged head equal to the runtime `DATABASE_REVISION`. Every packaged directory and file SHALL be root-owned and non-writable, and every packaged file byte SHALL match the canonical input. Bootstrap SHALL accept direct or explicitly session-stable PostgreSQL endpoints and SHALL reject known transaction-pooling shapes. It SHALL hold one deterministic bounded database-and-schema advisory lock across exact role/schema validation or creation, runtime-authenticated migration, and final runtime authentication proof. While retaining the admin session, it SHALL also hold a cryptographically unpredictable advisory challenge that the runtime connection non-blockingly proves is already held; successful acquisition by runtime SHALL be released immediately and SHALL fail bootstrap as a different database lock domain. The runtime role SHALL be distinct from admin, target the same database, own only its dedicated schema and not the database, and SHALL be `LOGIN`, non-superuser, without `CREATEDB`, `CREATEROLE`, replication, `BYPASSRLS`, or incoming or outgoing role membership. Existing authority or revision drift SHALL fail closed rather than be repaired.

Admin authority SHALL exist in K3s only as an ephemeral Secret consumed by one bootstrap Job and SHALL be deleted on success, failure, timeout, or interruption, followed by required provider-side rotation or revocation. Its receipt SHALL be a private, non-symlinked regular file owned by the operator, SHALL bind the current attempt and credential version, and SHALL be newer than the attempt boundary. Persistent private attempt state SHALL prevent another attempt from materializing authority until the preceding attempt's receipt validates. Stable hooks and long-lived workloads MUST NOT reference admin authority. The pre-install gate SHALL migrate to and prove the packaged head using only runtime authority before API/worker rollout. The pre-upgrade gate SHALL validate/no-op only when already at the new head and SHALL block ordinary Helm rollout instead of advancing a revision. A future revision-advancing release requires a separately reviewed forward-only expand/contract procedure.

#### Scenario: Fresh control plane is reconstructed from immutable artifacts
- **WHEN** a fresh PostgreSQL 17 database and empty cluster are bootstrapped from the reviewed provisioner image and runbook
- **THEN** the dedicated runtime role/schema are created under one lock, packaged migrations reach the exact runtime head through runtime authentication, the admin Job and Secret are absent, and stable workloads become eligible to roll out

#### Scenario: Concurrent or interrupted bootstrap is retried
- **WHEN** two bootstrap attempts race or one fails after committing the valid role/schema boundary
- **THEN** the advisory lock serializes them, retry validates the exact partial state without privilege repair, and no bootstrap/migration self-deadlock or secret disclosure occurs

#### Scenario: Admin and runtime URLs reach different clusters with the same database name
- **WHEN** the admin URL targets one PostgreSQL cluster and the runtime URL targets an already-migrated database of the same name on another cluster
- **THEN** the runtime connection can acquire and immediately releases the unpredictable challenge, and bootstrap fails closed without accepting the unrelated migration state

#### Scenario: Another login inherits the runtime role
- **WHEN** the runtime role is granted to another login even though the runtime role itself has no parent membership
- **THEN** bootstrap and exact-head validation reject the incoming membership edge as unsafe authority drift

#### Scenario: Upgrade would advance the database revision
- **WHEN** a platform upgrade renders an image head different from the current database revision
- **THEN** the runtime-only pre-upgrade gate fails content-free before API/worker rollout and performs no revision advance

#### Scenario: Bootstrap authority cleanup is incomplete
- **WHEN** the ephemeral admin Secret or Job remains, or a current attempt/version/time-bound provider rotation/revocation receipt is absent after any bootstrap attempt outcome
- **THEN** deployment remains blocked and the admin URL is not copied into any stable or governed secret destination

### Requirement: Frequent Substrate schedules are external, exact, and observable
Vercel SHALL continue to host the authenticated cron route handlers, but the three minute/hour Exomem schedules MUST NOT be declared in Vercel Hobby cron configuration. The K3s platform release SHALL consume pinned version `1` of Substrate's `ops/exomem-hosted-schedules.json` contract. The contract itself SHALL encode canonical origin `https://substratesystems.io` and exactly: `GET /api/cron/exomem-access-delivery` at `* * * * *`; `GET /api/cron/exomem-reconcile` at `* * * * *`; and `GET /api/cron/exomem-export-gc` at `17 * * * *`.

Version `1` SHALL also encode `authentication.scheme: bearer`, K3s sender variable `EXOMEM_HOSTED_SCHEDULER_SECRET`, Vercel active/previous receiver variables `EXOMEM_HOSTED_SCHEDULER_SECRET`/`EXOMEM_HOSTED_SCHEDULER_SECRET_PREVIOUS`, and `maxReceiverVersions: 2`. Its request policy SHALL encode method `GET`, redirect `error`, connect/total timeouts of five/20 seconds, and success status `[200]`. Its Kubernetes job policy SHALL encode `startingDeadlineSeconds: 45`, `activeDeadlineSeconds: 30`, `concurrencyPolicy: Forbid`, `backoffLimit: 1`, `maxAttempts: 2`, successful/failed job-history limits of one/three, and `ttlSecondsAfterFinished: 300`. Its observability block SHALL set `contentFree: true`; name `exomem_hosted_scheduler_attempts_total`, `exomem_hosted_scheduler_failures_total`, `exomem_hosted_scheduler_duration_seconds`, and `exomem_hosted_scheduler_last_success_unixtime`; and define alerts at 180 seconds after a missed due time and two consecutive failures. The renderer and cross-repository release validation SHALL reject an unsupported version or any contract/rendered-manifest drift.

Each CronJob SHALL send exactly `Authorization: Bearer <EXOMEM_HOSTED_SCHEDULER_SECRET>`, render every request/job/observability setting from the contract, and treat every redirect or non-success status as failure. The dedicated secret MUST authorize only the three contract routes and MUST NOT authorize global-`CRON_SECRET` routes such as backup GC, claim follow-ups, or IndexNow. K3s MUST NOT receive global `CRON_SECRET`, database, Paddle, provisioner, cell, Cloudflare Access, or browser credentials. These external jobs are distinct from K3s-internal vault and database durability schedulers.

#### Scenario: Versioned contract renders the production jobs
- **WHEN** platform chart inputs select scheduler contract version `1`
- **THEN** the rendered manifests match its origin, three path/method/cadence tuples, redirect/timeouts, deadlines, concurrency, retry/attempt bounds, history/TTL limits, and content-free metric/alert definitions exactly, and cross-repository validation proves none is present in Vercel Hobby cron configuration

#### Scenario: Cron route redirects
- **WHEN** a scheduled request receives any redirect response
- **THEN** contract redirect policy `error` fails the job without following the location or forwarding `EXOMEM_HOSTED_SCHEDULER_SECRET`, records a content-free outcome/failure metric, and becomes alertable under the encoded thresholds

#### Scenario: Scheduler bearer is absent or wrong
- **WHEN** the live K3s scheduler calls any of the three routes without the exact deployed `EXOMEM_HOSTED_SCHEDULER_SECRET`
- **THEN** the Vercel route fails closed without performing work, while the correct live bearer succeeds

#### Scenario: Scheduler bearer is tried on an unrelated cron route
- **WHEN** the dedicated hosted-scheduler bearer is sent to backup GC, claim follow-ups, IndexNow, or any route absent from contract version `1`
- **THEN** the unrelated route rejects it and no unrelated cron work runs

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
Static platform secrets SHALL be SOPS/age ciphertext with the age key escrowed off-cluster. Dynamic cell credentials, fences, provider references, and wrapped keys SHALL be encrypted in the external provisioner store under a separately escrowed root key. One non-printing handoff workflow SHALL enforce a versioned per-secret destination matrix without placing plaintext in repository files, shell history, saved-plan output, or CI logs. Every Vercel destination SHALL bind the exact expected organization/project identity and reject a mismatched local project link before reading a value. Every Vercel write SHALL serialize on that destination, reserve an immutable content-free version receipt before the provider call, recheck strict destination-local monotonicity while locked, and finalize destination/project/version evidence only after CLI success. Equal version numbers at different destination IDs SHALL NOT assert shared plaintext identity. Existing or lower SOPS destination versions SHALL never be overwritten; each ciphertext SHALL pass destination-shape and exact decrypt round-trip verification, and all selected local ciphertext destinations SHALL publish before any external Vercel write. A partial handoff SHALL consume its version at every destination with a durable reservation or artifact and recover that destination through a higher version rather than overwriting or retrying the uncertain version. Ansible SHALL consume its SOPS variable artifacts only through an executable FIFO or verified private tmpfs path with cleanup, and operator passthrough arguments SHALL NOT override those secret extra-vars. Cloudflare Access client credentials SHALL go only to Vercel; the Tunnel credential SHALL go only to K3s; `EXOMEM_HOSTED_SCHEDULER_SECRET` SHALL go only to the three Exomem hosted Vercel handlers and K3s scheduler; global Vercel `CRON_SECRET` SHALL NOT go to K3s; the provisioner bearer SHALL go only to its Vercel caller and K3s service; cell credentials SHALL go only from encrypted provisioner state to their cell Secret; wrapping keys SHALL go only to their named workloads and offline escrow.

#### Scenario: Clean secret reconstruction succeeds
- **WHEN** a new cluster is built without the previous etcd state
- **THEN** static secrets decrypt from SOPS and dynamic cell secrets reconcile from encrypted external state with matching recorded versions

#### Scenario: Vercel project link targets a different application
- **WHEN** a selected Vercel destination is invoked from a checkout linked to any other organization or project
- **THEN** the handoff rejects before reading the secret source or contacting Vercel

#### Scenario: Multi-destination handoff fails partway
- **WHEN** a local ciphertext or later Vercel destination fails after the version has been reserved
- **THEN** no Vercel call runs before every local ciphertext is durable, content-free final/pending receipts preserve the confirmed/uncertain remote state, each affected destination rejects reuse of its existing version, and recovery uses a higher version at those destinations

#### Scenario: Access credential rotates without stranding either side
- **WHEN** a Cloudflare Access service token is rotated
- **THEN** overlapping Cloudflare tokens allow the new client ID/secret to be staged and verified in Vercel before the old token is revoked, while K3s receives no Access client secret

#### Scenario: Tunnel credential rotates independently
- **WHEN** the Cloudflare Tunnel credential is rotated
- **THEN** only the versioned K3s Secret changes and the Vercel Access secret remains unchanged

#### Scenario: Scheduler bearer rotates through a coordinated cutover
- **WHEN** `EXOMEM_HOSTED_SCHEDULER_SECRET` rotates
- **THEN** the Vercel handlers first accept new active plus old previous, the still-old K3s sender succeeds during overlap, K3s changes to new, the previous receiver version retires only after the new sender succeeds, the old bearer then rejects, unrelated cron routes reject both hosted-scheduler versions, global `CRON_SECRET` remains unchanged and outside K3s, and cadence monitoring records no missed run

#### Scenario: Provisioner root key rotates without orphaning ciphertext
- **WHEN** a new root wrapping-key version becomes active
- **THEN** old and new decrypt versions overlap, new writes use the new version, every stored data key or legacy ciphertext is rewrapped or re-encrypted and verified, and the old key retires only after no live record references it

### Requirement: Declarative reconstruction is the primary cluster recovery path
The primary disaster-recovery procedure SHALL rebuild cloud/host/platform state from Terraform, Ansible, and Helm; restore or attach recorded volumes; restore external operational records; and reconcile dynamic Secrets and routes. Off-host etcd snapshots SHALL be secondary evidence and MUST NOT be the only recovery method.

#### Scenario: Production node is replaced from an empty host
- **WHEN** the original node is treated as permanently lost while cell volumes and external records remain
- **THEN** the documented reconstruction produces a working cluster and ready cells without copying untracked configuration from the old node
