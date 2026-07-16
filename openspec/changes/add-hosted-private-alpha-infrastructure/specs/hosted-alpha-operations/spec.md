## ADDED Requirements

### Requirement: Private-alpha capacity is hard-capped until measured
The first deployment SHALL use one x86 Hetzner CX33 and SHALL stop automatic provisioning at six active USER cells, two RECOVERY/restore-candidate cells, and eight potential/attached cell volumes. It MUST preserve at least eight unused attachments beneath Hetzner's 16-volume server limit. Admission SHALL reconcile a fresh signed cluster/server/location-bound live receipt with a strict local Kubernetes observation and SHALL serialize durable PostgreSQL reservations. Both observations SHALL list namespaces broadly, ignore only namespaces with neither an exact cell resource-name shape nor any owned cell marker, and reject a candidate whose complete identity or USER/RECOVERY class is stripped, malformed, ambiguous, or inconsistent with its reservation. Increasing the cap SHALL require a fresh resource/latency soak and reviewed cost sheet.

#### Scenario: Seventh user cell is requested
- **WHEN** six user cells are already active and no reviewed cap increase exists
- **THEN** provisioning remains queued/blocked with a capacity reason and does not allocate another user volume

#### Scenario: Concurrent sixth-slot requests race
- **WHEN** five USER reservations exist and two workers concurrently request distinct sixth slots
- **THEN** the PostgreSQL ledger admits exactly one, leaves the other pending with a content-free capacity reason, and retains no unreserved provider effect

#### Scenario: Recovery or attachment headroom is exhausted
- **WHEN** a request would create a third RECOVERY cell or a ninth potential/orphan attachment
- **THEN** provisioning remains pending before namespace, PVC, Helm, or HCloud mutation

#### Scenario: Collector sequence restarts after reconstruction
- **WHEN** a newly signed, fresh, cluster/server-bound receipt has a lower sequence after clean-cluster reconstruction and matches the fresh local observation
- **THEN** it remains admissible because no worker-local or PostgreSQL monotonic-sequence state is required

#### Scenario: A cell label is stripped before collection
- **WHEN** a cell-shaped or otherwise Exomem-marked namespace no longer has the exact tenant-cell label and complete immutable identity
- **THEN** both signed collection and local admission still discover the candidate and fail closed instead of undercounting it

### Requirement: EUR 5 pricing is treated as measured subsidy
Before activating the friend Paddle catalog or inviting a paid account, the operator SHALL record actual server, IPv4, per-cell volume, B2 retained storage/operations, Neon/Vercel marginal cost, alerting, actual Paddle account fee, and VAT-inclusive/exclusive treatment. The sheet SHALL show expected net receipt for EUR 5 and SHALL distinguish this private-alpha subsidy from a later EUR 10-15 public tier.

#### Scenario: Actual payment economics are unknown
- **WHEN** Paddle account pricing/tax choice or cloud plan prices have not been recorded from live configuration
- **THEN** paid invitations remain disabled even if technical provisioning is ready

### Requirement: Owner soak drives resource sizing and upgrades
Cell CPU/memory requests and limits SHALL be set from an owner/canary soak covering concurrent capture, recall, review, direct transfer, backup staging, restart, and upgrade. CX43, additional nodes, CPX, or dedicated-vCPU CCX SHALL be chosen only from observed CPU/memory/latency/attachment evidence.

#### Scenario: Shared CPU contention harms latency
- **WHEN** the soak shows sustained throttling or product latency outside the accepted budget
- **THEN** the evidence identifies whether request tuning, CX43 resize, another node, or dedicated CPU is the next reviewed change

### Requirement: Minimal observability covers independent failure domains
The alpha SHALL use external black-box control-path and backup-freshness checks, Kubernetes event/resource metrics, provisioner structured metrics, the schedule-contract-encoded content-free `exomem_hosted_scheduler_attempts_total` and `exomem_hosted_scheduler_failures_total` counters, `exomem_hosted_scheduler_duration_seconds` histogram, `exomem_hosted_scheduler_last_success_unixtime` gauge, and alert delivery. At least one availability and backup-age signal SHALL run outside the production node. Logs SHALL use opaque cell/operation/request IDs and MUST NOT contain emails, user IDs, credentials, authorization headers, cron response bodies, grants, presigned URLs, filenames, note/query text, snippets, embeddings, media, or environment dumps.

#### Scenario: Production node disappears
- **WHEN** the K3s node and every in-cluster monitor are unavailable
- **THEN** an external check alerts on the unavailable hosted path and stale backup signal

#### Scenario: Logs are audited for sensitive data
- **WHEN** representative provision, transfer, export, restore, and deletion flows complete
- **THEN** application/proxy/provisioner/Kubernetes logs contain operational IDs and timing but none of the prohibited identity/content/secret fields

#### Scenario: External Substrate schedule stops advancing
- **WHEN** one of the three contract-rendered K3s CronJobs misses its cadence or repeatedly receives a redirect, authentication failure, or non-success status from Vercel
- **THEN** the contract's content-free metrics identify only the declared job and contract version, the missed-run alert fires 180 seconds after due time or the failure alert fires after two consecutive failures, and no response body or bearer is retained

### Requirement: Alerts cover lifecycle and durability failures
Alerts SHALL cover failed/terminal provisioning, ready-cell unavailability, pending work exceeding its expected window, backup age at 45/60 minutes, restore-drill failure, PVC reserved-space pressure, node pressure, tunnel/Access failure, external scheduler contract drift, a run 180 seconds overdue, two consecutive scheduler failures, secret-version mismatch, and exhausted safe volume attachments.

#### Scenario: Cell logs approach their cap or PVC reserve
- **WHEN** rotating logs approach 128 MiB or PVC free space approaches the reserved 1 GiB
- **THEN** oldest logs are bounded, an alert is emitted, and canonical writes are not silently crowded out by logs

### Requirement: Operator runbooks are executable and non-destructive by default
Versioned runbooks SHALL cover backend bootstrap/recovery, saved-plan review, host/cluster bootstrap, secret handoff/rotation, cell inspection/retry, maintenance gating, suspension/resume, volume rebind, backup/restore, export, ordered deletion, node resize/replacement, and break-glass access. Routine cell creation SHALL require no SSH, database surgery, or manual Kubernetes resource creation.

#### Scenario: New operator follows the owner provision runbook
- **WHEN** an authorized maintainer uses only the documented deployment and product paths from a clean workstation
- **THEN** the owner cell becomes ready without undocumented console or shell mutation

### Requirement: Owner proof precedes every non-technical invitation
The private alpha SHALL remain closed until the owner account completes real invite/onboarding, capture, recall, epistemic review, direct upload/download, export, pod restart, compatible upgrade, clean restore, suspension/resume, and disposable-tenant deletion. The proof SHALL also include contract digest match, isolation attacks, pending/fence replay, node reconstruction, backup age/RPO, secret rotation, and the live cost sheet.

#### Scenario: One proof gate remains incomplete
- **WHEN** any required owner, security, durability, recovery, or cost check lacks passing evidence
- **THEN** the system does not issue the first non-technical invite

### Requirement: First non-technical journey needs no operator intervention
After owner proof, the first invited non-technical account SHALL accept the invite, complete entitlement/checkout or founder access, wait for asynchronous provisioning, reach a ready vault, and complete representative capture/recall/review/export without SSH, database edits, Kubernetes commands, or maintainer repair.

#### Scenario: Provisioning needs shell repair
- **WHEN** the first invite journey stalls and an operator must mutate infrastructure or database state manually
- **THEN** the alpha proof fails, the user is not treated as successfully onboarded, and the failure becomes a reconciler/runbook fix before another invite

### Requirement: Release evidence spans all three implementation surfaces
Opening the alpha SHALL require green Exomem runtime tests, Substrate control-plane/billing/webhook tests, infrastructure static/contract/security tests, strict OpenSpec validation, real deployment drills, and an independent review. The image digest, Exomem release, hosted protocol, and command-contract digest SHALL be recorded together.

#### Scenario: Fixture and deployed runtime differ
- **WHEN** the deployed private contract differs from the recorded Substrate fixture or image release unit
- **THEN** routing/binding remains disabled and release evidence fails
