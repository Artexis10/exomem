# Exomem Hosted Private Alpha Infrastructure Design

**Status:** Approved for implementation  
**Date:** 2026-07-14  
**Audience:** Exomem and Substrate maintainers  
**Scope:** The infrastructure and operating path required to offer Exomem Hosted to the owner first, then a small private alpha of non-technical users

## Decision

Build Exomem Hosted as a shared control plane that provisions an isolated Exomem cell for each account. Run the operational plane on a dedicated, single-node Hetzner K3s cluster described completely as code. Keep the product UI on Vercel and control-plane records in Neon. Use Paddle for subscriptions, Brevo for transactional email, Cloudflare Tunnel for public ingress, and Backblaze B2 for encrypted off-cluster backups.

Keep the authenticated Substrate cron route handlers on Vercel, but do not rely on Vercel Hobby to schedule the minute/hour work it cannot express. The K3s platform release owns three outbound CronJobs rendered from a pinned, versioned Substrate schedule contract. They call the exact production Substrate origin, path, and method with the dedicated `EXOMEM_HOSTED_SCHEDULER_SECRET` bearer, refuse redirects, and publish content-free freshness/outcome metrics and alerts. This credential authorizes only those three Exomem hosted routes; the global Vercel `CRON_SECRET` remains outside K3s and continues to protect unrelated jobs.

The first deployment optimizes for a trustworthy private alpha, not hypothetical global scale. It starts on one x86 Hetzner CX33, keeps every entitled active alpha cell always on, and makes adding a node or moving to a CX43 an explicit, measured change. Suspension deliberately quiesces and scales a cell to zero. A single-node failure may cause service downtime. An intact detached volume preserves acknowledged writes; loss of the volume recovers to the last successful off-cluster backup under the explicit RPO below.

## Why this shape

The prospective users are non-technical. Asking them to install, configure, expose, update, or back up a local Exomem process defeats the product. Hosting is therefore part of the product, not an optional deployment guide.

At the same time, putting all users in one Exomem process or one writable filesystem creates an unacceptable isolation boundary. A namespace, workload, persistent volume, credentials, network policy, quota, and backup identity per account gives us a small blast radius while preserving the existing single-vault Exomem runtime.

Kubernetes is useful here because it is the reconciler and isolation substrate for repeatable cells. Terraform and Ansible have different jobs: Terraform creates slow-moving cloud resources; Ansible establishes and hardens the host; Helm and Kubernetes reconcile the platform and tenant workloads. A signup never invokes Terraform or Ansible.

## Goals

- A non-technical user can accept an invite, subscribe or receive a founder entitlement, create an account, and reach a ready vault without operator shell work.
- Every account maps to exactly one isolated vault cell in the private alpha.
- Provisioning, suspension, reactivation, backup, restore, export, and deletion are idempotent and observable.
- The complete cloud and cluster setup can be recreated from versioned code, SOPS/age static secrets, and encrypted external dynamic state.
- Canonical Markdown and media survive pod replacement, node replacement with volume reattachment, and a clean-room restore to the declared backup RPO.
- The owner can prove the whole product path before inviting friends.
- The architecture can add capacity by adding nodes and cells without redesigning the product model.

## Non-goals for the private alpha

- Teams, shared vaults, multiple vaults per account, organization billing, or cross-account collaboration.
- Multi-region active/active service, zero-downtime node failure, or automatic cluster-node autoscaling.
- GPU inference or always-on heavy media extraction.
- A custom Kubernetes operator or bespoke container orchestrator.
- Terraform in the user signup path.
- Self-service selection of regions, compute sizes, or storage classes.
- Sharing Q's cluster, state, secrets, or deployment lifecycle.

## System boundary

```text
Browser
  | normal UI/API                   | direct upload/download with short-lived grant
  v                                 v
Substrate web app + hosted gateway (Vercel) ---- Paddle / Brevo
  |                         |                         |
  | control-plane records   | Cloudflare Access      | signed webhooks
  v                         | service token          v
Neon PostgreSQL             v
                    Cloudflare Access + Tunnel
                              |
                              v
                    Traefik restricted routes (K3s)
                       |                    |
                       | /provisioner/*     | /c/<opaque cell id>/*
                       v                    v
                Provisioner API       tenant cell Service
                       |                    |
                       | reconcile          v
                       v              Exomem StatefulSet
                 Kubernetes API         + 10 GiB PVC
                       |
                       +-- exomem-system namespace
                       |     provisioner, tunnel, Traefik,
                       |     monitoring, backup scheduler
                       |
                       +-- tenant-<opaque id> namespaces
                       |
                       +---------------------------> encrypted B2 objects

Browser direct-transfer lane
  -> public transfer hostname (no Access service token)
  -> Traefik permits only /c/<opaque id>/private/exomem/v1/{upload,download}
  -> cell validates one-time signed transfer grant, origin, identity, operation, and byte limit

K3s platform scheduler
  -> GET https://substratesystems.io/api/cron/exomem-access-delivery every minute
  -> GET https://substratesystems.io/api/cron/exomem-reconcile every minute
  -> GET https://substratesystems.io/api/cron/exomem-export-gc hourly at minute 17
  -> Authorization: Bearer <EXOMEM_HOSTED_SCHEDULER_SECRET>; redirects forbidden
```

The browser never receives cluster credentials, service credentials, Cloudflare Access credentials, or a tenant cell's internal service address. Substrate owns customer-facing sessions, the hosted gateway, and account lifecycle. It authenticates the user, resolves the account to a cell, and forwards only authorized application traffic.

The `privateEndpoint` stored by Substrate is an external HTTPS base such as `https://cells.example/c/<opaque-cell-id>/`, not a ClusterIP. Cloudflare Access requires a service token held only by Vercel and operators. Cloudflare Tunnel carries accepted requests to Traefik, which strips the opaque path prefix and routes to the matching ClusterIP service. The cell's own per-cell bearer credential and identity headers remain mandatory behind that edge. The provisioner has narrowly scoped Kubernetes permissions; its HTTPS route is protected by both Cloudflare Access and its independent provisioner bearer credential.

Vercel Functions cannot proxy the product's 90 MiB upload limit because their request-body cap is 4.5 MB. Substrate therefore authenticates the user and returns a small, short-lived direct-transfer ticket containing the public transfer URL and bounded identity headers. The browser streams directly through a separate Cloudflare hostname to the cell. Only upload/download paths exist on that hostname. Those two Exomem routes accept the signed transfer grant instead of the long-lived cell bearer, persist the grant JTI before transfer to prevent replay, validate tenant/cell/principal/operation/expiry/byte claims, and enforce CORS for the canonical Substrate origin. Commands and all lifecycle routes remain behind Cloudflare Access and the service bearer. User export downloads already use short-lived presigned B2 URLs and likewise bypass Vercel bodies.

The transfer hostname answers unauthenticated CORS preflight only for the exact canonical Substrate origin, methods, and headers, and exposes only the required response headers. The signed grant—not the spoofable Origin header—is authorization. Its JTI is durably consumed before any body byte or download is admitted; reuse rejects after pod restart, an aborted transfer requires a fresh ticket, and one ticket cannot change operation or path.

## Repository ownership

The current hosted work is deliberately split:

- **Exomem PR #227** owns the hosted tenant-cell runtime: tenant context, isolation-aware APIs, readiness and lifecycle behavior, and runtime tests.
- **Substrate PR #32** owns the customer-facing product and control plane: account records, onboarding, entitlements, Paddle webhook handling, and hosted UI.
- **This change** owns the reproducible operational substrate: Terraform, Ansible, Helm/Kubernetes platform definitions, the real provisioner/reconciler service, backup and restore tooling, monitoring, and proof drills.

The three changes must meet at versioned contracts. Infrastructure must not duplicate account truth from Substrate or reimplement tenant isolation already enforced by Exomem. The control plane must not generate ad hoc Kubernetes manifests or shell into nodes.

The contract audit found targeted prerequisites that belong on the existing product branches rather than being hidden in IaC:

- Exomem needs supported, versioned cell-initialization and offline-restore entrypoints plus overlapping service-credential rotation.
- Exomem needs a direct-transfer authorization lane that consumes short-lived, cell-bound grants without exposing the long-lived service bearer.
- Substrate must pin the exact contract generated by the selected Exomem commit, send Cloudflare Access service-token headers on provisioner and cell calls, and mint small direct-transfer tickets instead of proxying large bodies through Vercel Functions.
- Substrate must keep its three authenticated frequent-job handlers deployed on Vercel while publishing their versioned external schedule contract; the K3s platform consumes that contract because Vercel Hobby cannot schedule minute/hour cadences.
- The selected Exomem image digest, Exomem release, hosted protocol, command registry, and command-contract digest ship as one release unit.

At the inspected revisions, Exomem reports `0.22.0` and contract digest `49ac4d346991f0f1de5f692a78ad043de6020f9a1692cafc951ec84490f02940`, while Substrate is pinned to a `0.19.1` fixture with a different digest. That mismatch is a deployment blocker, not a configuration warning.

## Account, subscription, and cell model

One Substrate account owns one hosted workspace and one cell. The control-plane database is authoritative for commercial and lifecycle state; Kubernetes is authoritative only for observed workload state. Database access uses the existing Substrate ORM and migrations rather than hand-maintained production SQL.

Founder/friend pricing is represented as a Paddle price or explicit entitlement, not a hard-coded email allowlist. The initial friend price is EUR 5 per month. Later public prices can be EUR 10-15 per month without changing the runtime topology. Paddle MCP, CLI, or REST may create and inspect catalog objects during setup; the running service depends only on Paddle's supported API and verified webhooks.

The lifecycle state machine is explicit:

```text
invited -> account_ready -> entitled -> provisioning -> ready
                                      |               |
                                      v               v
                                    failed <------- degraded

ready -> suspended -> reactivating -> ready
ready/suspended -> deleting -> retained -> deleted
```

Every transition records an idempotency key, desired state, observed state, attempt count, last error category, and timestamps. Retrying the same command converges on the same cell. Unknown or contradictory state fails closed; it never silently routes a user to another account's cell.

## Infrastructure layers

### 1. Terraform: slow-moving cloud resources

Terraform is split into two independently planned and applied root modules so an object-storage credential change cannot replace the production node:

- `foundation` owns Hetzner compute, network, firewall, stable addressing, Cloudflare Tunnel/DNS, and Access policy;
- `durability` owns B2 backup buckets, retention/Object Lock policy, and backup identities.

Together they create and own:

- a dedicated Hetzner project boundary assumed to exist or supplied by identifier;
- one pinned x86 CX33 server in the selected EU region, with CX43 as the documented resize fallback;
- SSH key attachment, private network and subnet, and a restrictive Hetzner firewall;
- Backblaze B2 buckets and least-privilege application keys for vault backups;
- stable outputs consumed by Ansible and operator tooling.

The initial server has no public Kubernetes API, HTTP, or HTTPS ingress. Cloudflare Tunnel originates outbound from the cluster. SSH is limited to declared administrator CIDRs for bootstrap and recovery; normal operations use scoped Kubernetes credentials. Terraform outputs generate Ansible inventory without copying secrets into committed files.

Provider versions and module inputs are pinned, and the generated provider lock file is committed. Production mutation uses `terraform plan -out` followed by review and application of that exact saved plan. A plan guard rejects unapproved destroy or replacement actions; protected server, volume, and bucket resources use lifecycle safeguards as a second line of defense.

`foundation` and `durability` use separate keys in one bootstrapped, versioned B2 S3-compatible state bucket with Terraform's S3 lockfile support. This is the selected backend, not one branch of two maintained paths. Deployment is blocked until an integration test proves lock exclusion and prior-version recovery against the real B2 account; failure reopens the backend decision rather than silently falling back to local state. State credentials come from the operator environment; backend configuration, state, and saved plans containing credentials are never committed. The one-time backend bootstrap and recovery path are documented separately from normal apply.

### 2. Ansible: host bootstrap and hardening

Ansible configures the server idempotently:

- current security updates and unattended upgrades;
- a non-root administration path, SSH hardening, UFW, and fail2ban;
- required packages including `cryptsetup` for encrypted CSI volumes;
- time synchronization, log rotation, disk-pressure hygiene, and kernel settings appropriate to K3s;
- a pinned K3s release, explicitly initialized with embedded etcd, secrets encryption, and metadata-safe audit logging enabled;
- bounded etcd snapshots shipped off-host rather than trusted on the production disk;
- a restricted founder kubeconfig and a documented break-glass administrator path.

The first pin is K3s `v1.35.6+k3s1`. Upgrades are explicit changes tested against the pinned Hetzner CSI release before rollout. Ansible never embeds live application secrets in repository variables.

### 3. Helm and Kubernetes: platform reconciliation

Pinned platform releases install, in dependency order:

1. Hetzner Cloud CSI from the official chart, with an encrypted volume StorageClass and `Retain` reclaim policy;
2. SOPS/age ciphertext for static platform secrets, decrypted only by the operator deployment path;
3. the K3s-bundled, version-tied Traefik controller and Cloudflare Tunnel for restricted egress-only exposure;
4. lightweight resource/event metrics and alert exporters sized for a single node;
5. the Exomem system chart containing the provisioner API/worker, service accounts, RBAC, scheduled backup/maintenance jobs, and external Substrate scheduler CronJobs;
6. the versioned Exomem cell chart used by the provisioner.

The CSI chart is pinned to a release that explicitly supports Kubernetes 1.35; the implementation records the exact tested chart and image digest rather than tracking `latest`. All first-party workload images use immutable digests or commit-derived tags. Containers run non-root where supported, drop Linux capabilities, prohibit privilege escalation, use runtime-default seccomp, and disable service-account token mounting unless the component calls Kubernetes.

Cluster-level definitions and the system chart are applied by deployment automation. Tenant cells are reconciled dynamically by the provisioner using the versioned cell chart. A release label records the cell chart version so upgrades can be rolled out deliberately and inspected per account.

Vercel Hobby is deliberately not the source of the frequent hosted schedules. Substrate publishes `ops/exomem-hosted-schedules.json`, and the private alpha pins and renders this complete normative version `1` contract:

```json
{
  "version": 1,
  "scheduler": "kubernetes-cronjob",
  "origin": "https://substratesystems.io",
  "authentication": {
    "scheme": "bearer",
    "schedulerEnvironmentVariable": "EXOMEM_HOSTED_SCHEDULER_SECRET",
    "receiverActiveEnvironmentVariable": "EXOMEM_HOSTED_SCHEDULER_SECRET",
    "receiverPreviousEnvironmentVariable": "EXOMEM_HOSTED_SCHEDULER_SECRET_PREVIOUS",
    "maxReceiverVersions": 2
  },
  "requestPolicy": {
    "method": "GET",
    "redirect": "error",
    "connectTimeoutSeconds": 5,
    "totalTimeoutSeconds": 20,
    "successStatusCodes": [200]
  },
  "kubernetesJobPolicy": {
    "concurrencyPolicy": "Forbid",
    "startingDeadlineSeconds": 45,
    "activeDeadlineSeconds": 30,
    "backoffLimit": 1,
    "maxAttempts": 2,
    "successfulJobsHistoryLimit": 1,
    "failedJobsHistoryLimit": 3,
    "ttlSecondsAfterFinished": 300
  },
  "observability": {
    "contentFree": true,
    "attemptCounterMetric": "exomem_hosted_scheduler_attempts_total",
    "durationHistogramMetric": "exomem_hosted_scheduler_duration_seconds",
    "lastSuccessMetric": "exomem_hosted_scheduler_last_success_unixtime",
    "failureCounterMetric": "exomem_hosted_scheduler_failures_total",
    "missedRunAlertAfterSeconds": 180,
    "consecutiveFailureAlertThreshold": 2
  },
  "jobs": [
    {
      "name": "exomem-access-delivery",
      "path": "/api/cron/exomem-access-delivery",
      "schedule": "* * * * *"
    },
    {
      "name": "exomem-reconcile",
      "path": "/api/cron/exomem-reconcile",
      "schedule": "* * * * *"
    },
    {
      "name": "exomem-export-gc",
      "path": "/api/cron/exomem-export-gc",
      "schedule": "17 * * * *"
    }
  ]
}
```

Cross-repository release validation rejects an unsupported contract version, any field drift, non-HTTPS origin, and any attempt to put these jobs back in Vercel's Hobby cron configuration. Each CronJob sends exactly `Authorization: Bearer <EXOMEM_HOSTED_SCHEDULER_SECRET>` and renders the request, retry, deadline, concurrency, history, TTL, metric, and alert policy from the contract rather than hidden chart defaults. The three routes remain on Vercel and fail closed when this dedicated bearer is absent or wrong. The dedicated bearer MUST NOT authorize global-`CRON_SECRET` routes such as backup GC, claim follow-ups, IndexNow, or any future route outside this contract. Metrics and alerts contain no response body, user identity, or credential. The existing internal 30-minute cell/database durability schedulers remain separate K3s jobs; this external contract covers only Substrate's three frequent Vercel routes.

### 4. Provisioner: dynamic tenant reconciliation

The provisioner is a small independently packaged Python service under the infrastructure tree: FastAPI for the exact HTTP contract, SQLAlchemy 2 and Alembic for its durable PostgreSQL state, the official Kubernetes client for bounded resource operations, and the pinned Helm CLI for cell releases. It uses a dedicated Neon database role/schema; it does not write Substrate's tables or use hand-maintained production SQL as an application data layer.

The provisioner consumes authenticated lifecycle commands from the control plane and reconciles desired cell state. It creates only resources whose names derive from an opaque, immutable cell identifier. Human emails and names never appear in Kubernetes resource names, labels, its operation store, or logs.

Every call persists the action, canonical request hash, idempotency key, tenant fence generation, checkpoint, progress, result, and provider resources before side effects advance. Reusing a key with different input is a terminal conflict. A stale fence cannot mutate or recreate resources after a newer fence. Every durable provider side effect also carries immutable opaque tenant, cell/candidate, operation, and fence metadata outside PostgreSQL: Kubernetes annotations/labels on the namespace, release, PVC/PV, and routes; HCloud volume labels; and authenticated B2 object metadata/manifest fields. After a stale database restore, the provisioner scans all providers and computes the maximum observed fence before accepting any mutation. Long-running actions are queued durably. Each existing action returns a strict union of `pending` or its existing final proof: pending carries the operation/checkpoint and retry delay, moves Substrate to a non-attempt-consuming waiting state, and is replayed with the same key until final. Pending time does not consume the six failure attempts; transport/action failures still do. This is a Phase 0 change to PR #32 and keeps the 14-action surface rather than adding a status endpoint. Kubernetes apply semantics alone are not the operation store.

The versioned v1 surface implements every action expected by Substrate: `provision`, `health`, `rotate-credential`, `quiesce`, `resume`, `stop`, `export`, `export-release`, `export-delete`, `export-download`, `restore`, `seal`, `discard`, and tenant-wide `destroy`. The provisioner protocol header, bearer credential, request/response bounds, timeout behavior, and proof fields are contract-tested against Substrate's client parser.

For a new cell it:

1. validates the control-plane request and claims or resumes its durable idempotent operation;
2. rejects an altered request or stale tenant fence;
3. creates the namespace and baseline labels;
4. creates scoped secrets and encryption/backup identity;
5. creates quotas, limits, service account, and default-deny policies;
6. installs the pinned cell chart with one replica and one 10 GiB encrypted PVC and waits for CSI binding;
7. records the bound PV `volumeHandle` and location, applies and verifies HCloud tenant/cell/operation/fence labels, and durably checkpoints that ownership;
8. runs the supported idempotent cell initializer and waits for authenticated readiness;
9. creates the restricted Traefik route and returns its external HTTPS base only after every gate passes.

A partial failure leaves inspectable resources and a retryable operation. The reconciler can adopt resources from an interrupted attempt. It never treats “Helm command returned zero” as sufficient readiness.

The worker's dedicated ServiceAccount can create/delete namespaces, read bound PV metadata, and manage only the namespaced resource kinds needed by the fixed cell chart: Secrets, ServiceAccounts, ConfigMaps/Helm release records, PVCs, Services, StatefulSets, Jobs, ResourceQuotas, LimitRanges, NetworkPolicies, and Traefik routes. It cannot mutate nodes, CRDs, PVs, cluster roles/bindings, admission policy, or platform namespaces. Platform-owned validating admission rules reject privileged containers, host namespaces, hostPath, arbitrary image references, and cross-cell Secret/PVC references even if provisioner input validation fails. Namespace isolation does not defend against a compromised node kernel, cluster administrator, or provisioner credential; those remain trusted operational boundaries.

A separate privileged volume lifecycle worker holds the HCloud provider credential only while it runs. After CSI binding it records the PV `volumeHandle`, location, and deterministic tenant/cell/operation/fence labels in the external resource registry and confirms those labels through the HCloud API. Provision cannot return a final ready proof until that checkpoint is durable; a crash between binding and registration replays into adoption and verification rather than creating another volume. Destruction deletes the released Kubernetes PV object, deletes the provider volume, and independently verifies both absent after the retained PVC is no longer needed. Clean-cluster recovery creates a static PV/PVC bound to the recorded original `volumeHandle` before reconciling the StatefulSet. This worker, not routine Helm deletion, owns `storageDestroyed` proof and retained-volume reattachment.

## Tenant cell contract

Each private-alpha cell has:

- one namespace;
- one single-replica StatefulSet with a stable identity;
- one 10 GiB Hetzner volume through a PVC;
- one internal ClusterIP Service;
- one service account with no Kubernetes API access unless explicitly required;
- resource requests, conservative limits, a ResourceQuota, and a LimitRange;
- default-deny ingress and egress, followed by the smallest explicit allowances;
- per-cell application/gateway credentials and archive-encryption metadata;
- labels for opaque cell ID, chart version, lifecycle state, and cost/usage attribution.

The cell receives the exact hosted environment contract: hosted mode, original control-plane cell ID, protocol and release pins, a service credential of at least 32 bytes, application storage entitlement 5 GiB, upload payload limit 90 MiB, worker count zero, and no semantic/media grants. Kubernetes storage quota separately permits exactly one 10 GiB PVC and denies a second claim; the 5 GiB application entitlement is never expressed as `ResourceQuota.requests.storage`. The 90 MiB limit leaves deterministic multipart headroom below Cloudflare Free/Pro's 100 MB request cap; Vercel's 4.5 MB Function body cap is never in the data path. Vision, diarization, and file watching are not granted implicitly.

One volume contains three fixed, pairwise-disjoint directories mounted at invariant absolute paths for vault content, hosted mutable state, and private logs. An init container creates them without symlinks, mode `0700`, and ownership matching the non-root runtime UID. Those absolute paths are part of the persisted cell binding and do not change across pod replacements or upgrades. Logs rotate under a hard 128 MiB retained-byte cap and delete oldest files under pressure; alerts fire while at least 1 GiB remains reserved on the PVC so logs cannot consume canonical-write headroom. A separate size-limited `emptyDir` supplies compatible temporary space while the container root filesystem stays read-only where the pinned image permits it.

Markdown and media are canonical. SQLite indexes, caches, embeddings, and queues are derived and rebuildable; they may be backed up for recovery speed but are never the only copy of knowledge. The runtime's authenticated hosted readiness—not the personal OAuth or Docker Compose healthcheck—is authoritative. Kubernetes uses a purpose-built exec probe/helper that constructs fresh content-free identity headers from the mounted cell secret.

Heavy media extraction is disabled by default and, when enabled later, runs asynchronously under separate resource limits. Normal recall and capture remain responsive while background work is queued.

## Storage and encryption

Hetzner volumes are the online block store. The alpha uses one volume per cell because it gives a clear persistence and failure boundary. Hetzner permits 16 attached volumes per server, so this topology has an explicit per-node cell ceiling. Capacity planning adds a node before approaching that limit; it does not multiplex unrelated users into a shared writable volume to squeeze past it.

The CSI StorageClass uses LUKS encryption and requires `cryptsetup` on every eligible node. The implementation must verify the exact keying behavior supported by the pinned CSI driver. If CSI supports only a StorageClass-level secret, the private alpha uses a dedicated cluster volume-encryption key plus distinct per-cell application and backup keys, records that limitation honestly, and keeps the design ready for per-cell StorageClasses or a different key provider. It must not claim per-cell at-rest volume keys without proving them. PVC deletion protection and the `Retain` reclaim policy ensure a provisioner bug cannot immediately destroy the underlying volume.

Secrets at rest in Kubernetes are protected by K3s secrets encryption. Repository-held static platform secrets use SOPS/age ciphertext; the age private key is escrowed off-cluster and the clean-cluster recovery order is tested. Dynamic per-cell credentials, wrapped backup keys, fence state, and resource references are encrypted in the provisioner's external PostgreSQL store under a separately escrowed root key, then materialized into Kubernetes Secrets as needed. They are never committed and do not rely on a surviving etcd alone. Root/bootstrap credentials live in the operator's secret store and are rotated after initial deployment. Tenant namespaces enforce Kubernetes' built-in restricted Pod Security standard.

One non-printing secret handoff command reads Terraform sensitive outputs and the SOPS bundle in process memory, routes each versioned secret only to its declared consumer, verifies destination versions, and clears temporary process state. The destination matrix is explicit: the Cloudflare Access client ID/secret goes to Vercel only; the Cloudflare Tunnel credential goes to K3s only; K3s receives one `EXOMEM_HOSTED_SCHEDULER_SECRET` sender version, while only the three Exomem hosted Vercel handlers may receive active `EXOMEM_HOSTED_SCHEDULER_SECRET` plus previous `EXOMEM_HOSTED_SCHEDULER_SECRET_PREVIOUS`, capped at two accepted receiver versions; the global Vercel `CRON_SECRET` never goes to K3s; the provisioner bearer is shared only by the Vercel caller and K3s provisioner; per-cell credentials flow from encrypted provisioner state to the corresponding cell Secret; encryption wrapping keys flow only to the provisioner/durability workloads and offline escrow. It never places plaintext in Terraform output logs, shell history, saved plan text, files, or CI logs. Access rotation creates overlapping Cloudflare tokens, stages and verifies the new Vercel version, then revokes the old token without changing K3s. Tunnel rotation is a separate K3s operation. Hosted-scheduler rotation first stages new-active/old-previous on the three Vercel receivers, proves the still-old K3s sender succeeds through overlap, changes the K3s sender to the new version, proves new acceptance and unrelated-route denial, removes the previous receiver version, and proves the old bearer rejects without a missed cadence. Root-key rotation stages a new version alongside the old decrypt-only version, writes new ciphertext with the new key, rewraps every stored data key (or re-encrypts legacy direct ciphertext), verifies that no live record references the old version, then retires it while preserving the required recovery escrow.

Primary cluster disaster recovery is declarative reconstruction: recreate cloud resources, bootstrap K3s, apply the system release, restore/attach volumes, restore the provisioner/control-plane records, and reconcile dynamic Secrets and routes from external durable state. Encrypted off-host etcd snapshots are a secondary emergency aid, not the only recovery plan.

Every 30 minutes, a system logical-backup job takes a transactionally consistent export of the complete Substrate application database—including users, authentication/account ownership, billing, and hosted tables—and the provisioner schema with dedicated read-only backup credentials, encrypts the dump under the escrowed recovery key, uploads it to a protected B2 prefix, and verifies the object. A scratch restore must recover owner login and tenant resolution as well as pending operations, fence generations, provider references, wrapped keys, and cell credentials. After any stale database restore, reconciliation scans Kubernetes, HCloud volumes, Traefik routes, and B2 prefixes by immutable tenant/cell/operation/fence metadata, computes the maximum provider-observed fence before accepting mutations, and adopts or quarantines post-backup resources without lowering it.

## Network and request isolation

Cloudflare Tunnel is the only public ingress into the K3s operational plane. On the control hostname, Cloudflare Access rejects requests that lack the Vercel-held service token before they reach the tunnel. Traefik exposes provisioner `/cells/*` and per-cell `/c/<opaque-id>/private/exomem/v1/*` control routes there, strips the cell prefix, and rejects the personal OAuth/public surface. A separate transfer hostname bypasses Access only for per-cell upload/download paths because browsers cannot hold the service token; those paths require a one-time signed transfer grant and strict origin/CORS checks. Cells remain ClusterIP-only.

The only reverse control-plane call is from the `exomem-system` scheduler to the three exact public Vercel cron URLs above. It carries only `EXOMEM_HOSTED_SCHEDULER_SECRET`, never the global `CRON_SECRET`, Cloudflare Access, provisioner, Paddle, database, or cell credentials. Contract `requestPolicy.redirect: "error"` prevents a platform/domain misconfiguration from forwarding the bearer to another origin. Tenant namespaces retain no external egress.

Every tenant namespace starts with deny-all ingress and egress. The alpha cell runtime has no external egress: embeddings/media workers and update checks are disabled, and B2 traffic stays in `exomem-system`. Explicit policy permits:

- ingress to the cell only from Traefik on the Exomem port;
- monitoring scrape traffic where needed.

The Substrate gateway validates user identity, entitlement, tenant binding, protocol, release, and the generated command-contract digest on every request. It then sends Cloudflare Access credentials, the per-cell bearer, a derived principal-scope digest, and fresh request headers. Caller-provided hostnames, namespace names, upstream URLs, or routing headers are never trusted. The provisioner generates route targets from its resource registry rather than accepting an upstream URL from Substrate.

Every cell route has a provisioner-owned maintenance gate. Before a backup or other snapshot boundary, the durable operation lock serializes against export, restore, suspend, rotation, deletion, and another backup; the provisioner disables both the control and transfer Traefik routes, externally verifies that both reject—including a previously issued unused transfer ticket—and only then calls the cell's internal quiesce/drain API with the routing-stopped assertion. Routes reopen only after the local snapshot checkpoint is safely released and the cell resumes. Already in-flight cell operations are included in the runtime drain.

The control path is tested with commands and lifecycle calls. The direct-transfer path is tested with 90 MiB uploads and representative large downloads; Cloudflare, Traefik, and Exomem must stream without buffering the whole body. Vercel carries only ticket JSON and never the file. Isolation tests attempt missing/incorrect Access credentials, missing/replayed/altered transfer grants, hostile origins, cross-account path routing, bearer and pending-token substitution, host-header manipulation, namespace discovery, and network access from a compromised cell. Executable network probes prove that a cell cannot reach another cell, the Kubernetes API, Neon, B2, or node/cloud metadata, and that an unlabelled system pod cannot reach a cell.

## Backup, restore, export, and deletion

The provisioner backup scheduler starts every 30 minutes with retry margin and drives the same safe cell export protocol used for user exports: close both routes, quiesce and drain, create the cell-local portable archive, stream it to a size-bounded system scratch volume, verify archive/manifest digests and size, release the local checkpoint with resume, then reopen routes. Encryption and B2 upload happen from the verified scratch copy after the cell is serving again. It encrypts with envelope AES-256-GCM, records the wrapped data key and opaque provider reference, and writes ciphertext under a cell-scoped B2 prefix whose authenticated metadata/manifest includes the immutable opaque tenant, cell/candidate, operation, and fence identity. The job rejects implausibly small archives, verifies remote object existence, emits metrics, and applies retention. It never substitutes a live filesystem tar for the runtime's portability contract.

RPO is measured from the newest successfully verified remote object, not job start time. Backup age warns at 45 minutes, becomes an alpha-blocking alert before 60 minutes, and keeps retrying without overlapping the next operation. The owner soak includes a near-5-GiB representative vault and requires route closure/quiescence to remain under two minutes; if it fails, the alpha storage entitlement is lowered or the snapshot contract is revised before invitations.

B2 credentials are scoped to the required bucket or prefix as tightly as the provider supports. The system durability worker receives upload/list access without delete; retention and Object Lock protect recovery objects, while a separate restore/deletion credential is available only to the corresponding privileged job. Backup encryption keys are separate from storage credentials. Losing the cluster or B2 credential alone must not expose plaintext vaults.

A user-visible export uses the same verified ciphertext path and yields only an opaque export reference and a presigned HTTPS download valid for at most 15 minutes. The cell-local ZIP path never reaches Substrate or the user. Restore runs as a version-pinned offline Job against a stopped candidate cell, invokes the supported Exomem restore helper, recreates destination binding markers instead of copying source hosted state, and starts the runtime only after atomic publication.

A backup is not accepted until a clean-room restore drill can:

1. create an empty replacement cell;
2. download and decrypt a selected backup;
3. validate its manifest and checksums;
4. restore canonical content;
5. rebuild derived SQLite/index state;
6. pass representative capture, recall, review, and export checks.

Credential rotation is an overlap protocol, not a Secret replacement race. The provisioner stages a pending credential, proves the cell accepts it while the active credential remains valid, promotes it, finalizes the runtime state, and independently proves the old credential is rejected. The Exomem runtime stores only digests and version metadata in its durable private state; plaintext tokens remain in Kubernetes Secrets.

### Private-alpha recovery objectives and retention

| Failure | RPO | RTO target | Recovery path |
|---|---:|---:|---|
| Cell pod/process failure | 0 acknowledged writes | 5 minutes | StatefulSet restart on the same PVC |
| Node loss with intact volume | 0 acknowledged writes | 60 minutes | replace/rebuild node, attach retained volume, reconcile cell |
| Volume loss or corruption | at most 1 hour | 4 hours | latest verified 30-minute B2 backup into a fresh cell |
| Provisioner/control-plane database loss | at most 1 hour | 4 hours | managed recovery plus a 30-minute encrypted logical backup, then reconcile |

Verified recovery objects use seven-day Object Lock retention and a 30-day normal lifecycle window for the private alpha. User-visible exports retain their existing shorter product TTL and are not silently converted into recovery backups. On account deletion, access and billing stop immediately, but the lifecycle remains `deleting`/`retained` until the last lock expires. The privileged deletion worker then overrides normal 30-day retention, deletes and independently verifies provider absence, destroys the wrapped key, and returns the final proofs. Only then does the control plane emit `deleted`. These clocks and the maximum seven-day delay are disclosed in the deletion UI.

Deletion is ordered and auditable:

1. revoke sessions and route access;
2. quiesce writes;
3. create and verify the promised final export, if applicable;
4. destroy online compute, writable volume, route, and active application credentials immediately;
5. retain only locked recovery ciphertext until the disclosed retention expiry;
6. delete retained backup objects and destroy their wrapped keys when the lock expires;
7. retain only a content-free deletion receipt.

No “deleted” status is emitted while writable content or recoverable backups remain outside the declared retention contract.

## Observability and operations

The alpha observability stack stays deliberately small: external black-box availability and backup-freshness checks, Kubernetes events/resource metrics, provisioner structured metrics, and alert delivery. It does not install a full Grafana/Prometheus platform until a concrete signal requires one. The system exports low-cardinality metrics for:

- lifecycle queue depth, transition duration, retry count, and terminal failures;
- cells desired, ready, degraded, suspended, and deleting;
- pod restarts, readiness, CPU throttling, memory working set, filesystem usage, and volume attachment failures;
- request latency and error rate by operation class, never by user email;
- backup age, duration, bytes, checksum failures, upload failures, and last proven restore;
- the contract-declared content-free `exomem_hosted_scheduler_attempts_total` and `exomem_hosted_scheduler_failures_total` counters, `exomem_hosted_scheduler_duration_seconds` histogram, and `exomem_hosted_scheduler_last_success_unixtime` gauge per low-cardinality job name and contract version;
- node pressure, certificate/tunnel health, K3s health, and remaining attachable-volume capacity.

Alerts cover failed provisioning, a ready cell becoming unavailable, backup age beyond policy, restore-drill failure, volume pressure, node pressure, tunnel failure, unsupported scheduler-contract drift, a Substrate cron run 180 seconds overdue, and two consecutive scheduler failures. At least one black-box availability and backup-freshness signal runs outside the production node so node loss cannot silence the only alarm. Logs carry request/operation IDs and opaque cell IDs, with secrets, query text, vault content, cron response bodies, and authorization headers redacted.

The operator runbook covers deployment, saved-plan review, cell inspection, retry, suspension, secret rotation, node resize/replacement, restore, deletion, and break-glass access. Day-to-day cell creation requires no SSH.

## Capacity and cost policy

The first node is an x86 Hetzner CX33 with four shared vCPUs and 8 GiB RAM. The July 2026 list price is EUR 8.49/month excluding VAT in Germany/Finland, plus primary IPv4 if retained. A 10 GiB volume is budgeted at the current account/API price (roughly EUR 0.48 excluding VAT after the 2026 storage adjustment) and the deployment records the actual Terraform-plan price before creation. It hosts the control components, owner, and a deliberately bounded friend/relative alpha. Every cell starts with requests and limits measured during the owner soak rather than copied from a desktop configuration.

We watch three independent ceilings:

- **memory/CPU:** sustained working set, throttling, and latency under concurrent capture/recall;
- **storage:** per-volume usage and backup growth;
- **attachments:** Hetzner's 16-volume-per-server limit.

The response is empirical: tune requests first, resize to CX43 if a larger single node is the cheapest safe step, and add a worker node before volume attachments or failure-domain concerns demand it. CPX or dedicated-vCPU CCX becomes justified only from observed CPU contention.

The EUR 5 friend price is a subsidized private-alpha price. Paddle's public standard fee is 5% + EUR 0.50 per checkout, so a EUR 5 charge yields at most EUR 4.25 before tax treatment; the real account rate and VAT-inclusive/exclusive choice are recorded before catalog activation. Automatic provisioning stops at six active user cells (owner plus at most five paid friend accounts), with two additional volume slots reserved for canary/restore drills. Increasing that cap requires a fresh resource soak and a monthly cost sheet covering server, IPv4, all volumes, B2 retained bytes/operations, Neon/Vercel marginal cost, Paddle fees, and alerting. The owner proof starts with no paid seats; the first friend is invited only after the measured fixed and marginal cost is visible.

## Delivery sequence

### Phase 0: reconcile the existing product changes

- Bring Exomem PR #227 onto current `main`, resolve the vault/writer-lease conflicts semantically, and rerun the complete test and isolation suite.
- Add supported idempotent hosted init, offline candidate restore, and active/pending credential rotation to Exomem with TDD.
- Add one-time direct-transfer grant consumption to Exomem and change the fixed alpha upload payload limit to 90 MiB.
- Bring Substrate PR #32 onto current `main`, fix the failed deployment check, add Cloudflare Access service-token headers, direct-transfer tickets, and a non-attempt-consuming pending/final action union on the same 14 provisioner endpoints; verify its database, billing, webhook, and control-plane contract.
- Keep the three frequent authenticated cron handlers on Vercel, remove their unsupported minute/hour schedules from `vercel.json`, and publish the versioned external K3s schedule contract with the exact production URLs and methods.
- Build one immutable Exomem image, regenerate the Substrate fixture from it, and freeze image digest + release + hosted protocol + command-contract digest as one tested release unit.

### Phase 1: reproducible cluster

- Implement split-state Terraform with saved-plan workflow, destroy/replacement guards, outputs, and cost-safe defaults.
- Implement Ansible hardening and pinned K3s bootstrap.
- Install CSI, encrypted storage, SOPS-managed static secrets, Traefik restricted routes, Cloudflare Tunnel/Access, the three contract-rendered external Substrate CronJobs, and baseline metrics through pinned charts/manifests.
- Recreate the cluster from an empty operator environment and retain the evidence.

### Phase 2: system and cell reconciliation

- Package the provisioner API/worker and Exomem cell charts.
- Implement the exact 14-action `exomem-cell-provisioner.v1` surface with durable SQLAlchemy/Alembic operation, fence, resource, credential, export, and backup state.
- Wire and contract-test pending/final replay over more than six cron runs, authenticated health, fixed mounts, maintenance-gated routes, HCloud retained-volume ownership, and restricted external endpoints.
- Add network, RBAC, quota, and cross-tenant attack tests.

### Phase 3: durability and operations

- Implement centralized, routing-aware encrypted B2 backup and retention work with upload-only system credentials and protected retention.
- Implement clean-room restore tooling and derived-state rebuild.
- Add external checks, lightweight metrics, alerts, and operator runbooks.
- Exercise pod kill, node replacement, failed provisioning, backup failure, and deletion.

### Phase 4: prove the product

- Provision the owner's account through the real invite/onboarding path.
- Complete capture, recall, epistemic review, export, restart, and upgrade journeys.
- Run an owner soak on the CX33 and record latency, resource, storage, and marginal-cost evidence.
- Restore the owner vault into a clean replacement cell and verify representative knowledge.
- Delete a disposable test tenant and prove the deletion receipt and retention behavior.
- Invite the first non-technical friend only after the full journey needs no shell intervention.

## Acceptance gates

The private alpha is ready only when all of the following are demonstrated, not merely implemented:

- Separate `foundation` and `durability` saved plans produce the declared Hetzner/B2 resources, and an unrelated durability change cannot alter the node.
- Remote-state locking rejects a concurrent writer and a prior state version can be recovered.
- Ansible can bootstrap a fresh server idempotently and a second run is clean.
- Platform and system charts install from pinned versions and pass policy validation.
- Contract version `1` itself carries the canonical origin, three exact jobs, redirect/timeouts, deadline/concurrency/retry/attempt/history/TTL bounds, content-free metric types, and alert thresholds; rendered CronJobs are absent from Vercel Hobby schedules, reject a bad dedicated bearer, accept the live K3s `EXOMEM_HOSTED_SCHEDULER_SECRET`, cannot use it on unrelated global-cron routes, and match every contract field.
- The cell chart admits its single 10 GiB PVC while denying a second claim, and Exomem independently enforces the 5 GiB application entitlement.
- The real cell contract through Vercel -> Cloudflare Access/Tunnel -> Traefik exactly matches Substrate's fixture and immutable image release unit.
- A control-plane request provisions one isolated, ready cell with no manual cluster action; repeating the same request/key returns the same provider reference, altered input conflicts, and a stale fence cannot mutate it.
- Restarting the provisioner midway through provision, export, restore, or destroy resumes without duplicate resources or lost proof state.
- A pending operation remains waiting without consuming failure attempts for more than six cron runs and across both Substrate/provisioner restarts, then accepts its exact final proof; the seven-day deletion wait uses the same contract.
- Fresh init is idempotent, an identical mount/binding survives pod replacement, and a changed path or cell binding fails closed.
- Missing or incorrect Cloudflare Access token, cell bearer, cell ID, protocol, request UUID, or principal scope rejects through the real external route without content disclosure.
- Credential rotation proves active/pending overlap, pending-token health, promotion, finalization, and old-token rejection.
- Cross-tenant application and network attacks fail closed, including cell attempts against Kubernetes, Neon, B2, metadata, and another cell.
- A bound HCloud volume is recorded by provider ID and tenant/cell/operation/fence labels before provision becomes final, survives total etcd loss, and is rebound through a static PV to the original `volumeHandle`; destructive cleanup independently verifies both the released PV object and provider volume absent before `storageDestroyed` is true.
- Killing the cell pod preserves canonical content and returns the cell to readiness.
- Replacing the node through the named declarative reconstruction runbook reattaches retained volumes, restores external dynamic state, and meets the 60-minute RTO.
- A 30-minute provider backup disables and externally verifies both control/transfer routes, rejects a previously issued transfer ticket during the boundary, quiesces/exports/stages/releases/resumes within the two-minute objective, then encrypts/uploads/verifies; it survives a worker restart, keeps the last verified object younger than one hour, and restores a different candidate identity within four hours.
- A clean encrypted logical-database restore preserves owner authentication/tenant resolution plus pending work/fences/wrapped keys, computes the maximum provider-observed fence before mutation, rejects stale replay, and adopts or quarantines resources created after the restored snapshot.
- A 90 MiB upload and representative large download go browser -> transfer hostname -> Traefik -> cell without entering a Vercel Function; altered/replayed tickets and hostile origins fail closed. User export download uses its presigned provider URL.
- CORS preflight allows only the canonical origin and exact transfer methods/headers; a JTI is consumed before bytes, survives pod restart, rejects replay/path changes, and aborted transfers require a fresh ticket.
- `discard` removes only the failed candidate; tenant-wide `destroy` discovers active/orphan compute, volume, route, secrets/keys, exports, and backups and returns every exact proof after retention expires.
- Suspension, reactivation, export, and ordered deletion match the declared lifecycle.
- The live cost sheet confirms the six-user-cell cap, two reserved drill slots, actual Paddle account fee/tax choice, and Terraform-plan infrastructure prices.
- A clean secret handoff proves the per-secret destination/version matrix without plaintext in state output, files, shell history, plans, or logs; separate Access, Tunnel, coordinated `EXOMEM_HOSTED_SCHEDULER_SECRET`, and root-key rotations prove destination, cross-route isolation, cutover/rejection, overlap, rewrap, and retirement semantics as applicable.
- The owner journey and a non-technical invite journey complete without SSH or database surgery.
- Full Exomem, Substrate, infrastructure validation, security checks, and independent review are green.

## Explicit risks and triggers

- **Single-node downtime:** accepted for private alpha. Trigger for multi-node work is real user availability need or node maintenance becoming disruptive.
- **Volume attachment ceiling:** one volume per cell means a practical limit below 16 cells on the first node. Trigger is projected onboarding reaching the safe reserved threshold.
- **Shared volume-encryption key if forced by CSI:** accepted only if verified and documented; per-cell backup/application keys remain mandatory. Trigger is CSI/key-provider support or stronger compliance need.
- **Control-plane/runtime contract drift:** mitigated by versioned schemas and contract tests across both repositories.
- **Subsidized EUR 5 accounts:** acceptable for a few known users; public launch requires measured marginal cost and a sustainable EUR 10-15 tier.
- **Operational complexity:** mitigated by pinned dependencies, one deployment path, idempotent reconciliation, restore drills, and written runbooks.

## Final architectural stance

This is not a custom multi-tenant rewrite of Exomem. It is a small hosted platform around the runtime already built: shared identity, billing, and lifecycle control; strongly isolated vault cells; and infrastructure whose desired state is reviewable in Git. That is the shortest path from an impressive local product to something Olivia, Kim, Ash, and later public customers can actually trust and use.
