## Context

Exomem PR #227 implements a hosted single-vault cell and Substrate PR #32 implements the Vercel-hosted customer/control plane, but there is no real `exomem-cell-provisioner.v1`, deployable cluster, or durability plane between them. The inspected branches also disagree on the immutable runtime contract, and the runtime lacks supported init/restore/credential-overlap entrypoints required by its own lifecycle.

The approved product boundary is one account/workspace/cell. Canonical knowledge remains Markdown and media inside an isolated Exomem process and volume; the shared Neon control plane stores commercial and operational metadata only. The first deployment serves the owner and at most five paid private-alpha accounts on one dedicated Hetzner K3s node. It accepts node-level downtime but declares and proves recovery targets.

The full decision record is `docs/superpowers/specs/2026-07-14-exomem-hosted-private-alpha-infrastructure-design.md`. This artifact summarizes the implementation-driving decisions.

## Goals / Non-Goals

**Goals:**

- Recreate the hosted cloud, cluster, platform, and cell substrate from versioned code and recoverable encrypted state.
- Provision, inspect, rotate, quiesce, suspend, resume, export, restore, discard, and destroy a cell through the exact Substrate protocol without operator shell work.
- Preserve strong application/filesystem/network boundaries between cells and fail closed on identity, contract, or fence mismatch.
- Recover acknowledged writes after pod or node replacement and keep volume-loss recovery within the declared one-hour RPO using 30-minute off-cluster backups.
- Prove the complete owner and non-technical invite journey before opening the alpha.

**Non-Goals:**

- Teams, shared vaults, multiple vaults per account, or organization billing.
- Multi-region, multi-node HA, automatic node autoscaling, or a zero-RPO claim after volume loss.
- GPU inference, semantic/media workers, or a server-side reasoning model.
- A custom Kubernetes operator, Terraform during signup, or reuse of Q's cluster/state.

## Decisions

### Keep the Vercel control plane and isolate the K3s operational plane

Substrate remains the public application, user-authentication boundary, gateway, entitlement source, and lifecycle coordinator. Neon remains its metadata store. A dedicated Hetzner K3s cluster runs the provisioner and one namespace/StatefulSet/PVC per cell.

Alternative considered: move the hosted gateway into K3s. Rejected because it duplicates or rewrites PR #32 and expands the first deployment unnecessarily.

### Schedule frequent Substrate work from K3s without moving its routes

Vercel continues to host the authenticated Substrate cron handlers, but Vercel Hobby cannot schedule their minute/hour cadences. Substrate therefore publishes version `1` of `ops/exomem-hosted-schedules.json`, and the K3s platform chart renders exactly three outbound CronJobs against the contract's canonical origin `https://substratesystems.io`: `GET /api/cron/exomem-access-delivery` and `GET /api/cron/exomem-reconcile` every minute, plus `GET /api/cron/exomem-export-gc` hourly at minute 17. The frequent jobs remain absent from `vercel.json`.

Version `1` encodes, rather than merely documents, one K3s sender variable `EXOMEM_HOSTED_SCHEDULER_SECRET`, active/previous Vercel receiver variables `EXOMEM_HOSTED_SCHEDULER_SECRET`/`EXOMEM_HOSTED_SCHEDULER_SECRET_PREVIOUS`, and `maxReceiverVersions: 2`. Its request policy fixes method `GET`, redirect `error`, five-second connect and 20-second total timeouts, and success status `[200]`. Its Kubernetes policy fixes `startingDeadlineSeconds: 45`, `activeDeadlineSeconds: 30`, `concurrencyPolicy: Forbid`, `backoffLimit: 1`, at most two attempts, successful/failed history limits of one/three, and `ttlSecondsAfterFinished: 300`. Its content-free observability block names attempt/failure counters `exomem_hosted_scheduler_attempts_total`/`exomem_hosted_scheduler_failures_total`, duration histogram `exomem_hosted_scheduler_duration_seconds`, last-success gauge `exomem_hosted_scheduler_last_success_unixtime`, a missed-run alert 180 seconds after due time, and a consecutive-failure threshold of two. The chart and cross-repository validation fail closed on any field drift.

The dedicated hosted-scheduler bearer is delivered only to these three Vercel handlers and the K3s scheduler. It MUST NOT authorize unrelated routes protected by Vercel's global `CRON_SECRET`, and K3s never receives that global value. Rotation stages new active plus old previous at the Vercel receiver, moves the single K3s sender to new, and retires previous only after new acceptance, old rejection, cross-route isolation, and cadence health are proven. The internal 30-minute vault/database durability schedules remain separate K3s work.

Alternative considered: declare the three frequent jobs in Vercel cron configuration. Rejected because Hobby accepts only daily schedules, so the declared minute/hour configuration cannot deploy.

### Split infrastructure by reconciliation speed and blast radius

Terraform has separate `foundation` and `durability` roots/states. Foundation owns Hetzner compute/network/firewall and Cloudflare Tunnel/DNS/Access. Durability owns B2 buckets, retention/Object Lock, and scoped keys. Both use one versioned B2 S3-compatible backend with separate state keys and lockfiles; deployment is blocked until real-account locking and recovery tests pass. Every production apply uses a reviewed saved plan and a destroy/replacement guard.

Ansible hardens the host and installs pinned K3s `v1.35.6+k3s1` with embedded etcd, secrets encryption, metadata-safe audit, `cryptsetup`, and off-host snapshots. Helm/Kubernetes reconciles the static platform and dynamic cell release. Signup invokes only the durable provisioner.

Alternative considered: one Q-style Terraform state and deploy command. Rejected because unrelated drift in one lifecycle domain can replace another; this failure has already happened in Q.

### Use a durable application provisioner, not a manifest proxy

`infra/provisioner` is an independently packaged FastAPI service with SQLAlchemy 2/Alembic state in a dedicated Neon role/schema, an API process, and a restart-safe worker. It implements all 14 Substrate actions. Before side effects it records the canonical request hash, idempotency key, operation/checkpoint, monotonic tenant fence, resource references, progress, and encrypted result material. Every durable Kubernetes, HCloud, route, and B2 side effect also records immutable opaque tenant/cell-or-candidate/operation/fence metadata outside PostgreSQL. Every action returns a strict pending-or-final union. Pending moves Substrate into a non-attempt-consuming waiting checkpoint on the same endpoint/key, so work can outlive six cron runs or seven days without becoming terminal; replay resumes or returns the exact final proof. `discard` targets one candidate; `destroy(tenantId)` enumerates every tagged/registered tenant resource.

The worker uses the official Kubernetes client and pinned Helm CLI. Its RBAC can manage only namespaces, read bound PV metadata, and manage the fixed namespaced kinds required by the cell chart. Platform-owned admission rejects arbitrary images, privileged/host namespaces, hostPath, and cross-cell Secret/PVC references. A separately invoked privileged volume worker uses the HCloud API to label/record/delete/verify the CSI volume and reconstruct a static PV/PVC against the original `volumeHandle` after etcd loss. Provision remains pending until `volumeHandle`/location storage and provider-label verification are durable; ordered deletion removes and verifies both the released PV object and HCloud volume.

Alternative considered: run Helm/Terraform synchronously from the Vercel cron. Rejected because cron budgets are shorter than storage/export/restore work and neither Vercel nor Kubernetes apply provides durable idempotency/fencing.

### Freeze one runtime release unit before chart work

Exomem image digest, Exomem release, hosted protocol, generated command registry, and contract digest form one immutable release unit. Phase 0 refreshes PR #227/#32, adds supported init/offline restore/active-pending credential rotation, regenerates the Substrate fixture, and verifies the deployed private contract through the real route.

Alternative considered: override only `EXOMEM_CELL_RELEASE_VERSION`. Rejected because Substrate compares the full generated semantic contract and digest; the inspected 0.22.0 and 0.19.1 fixtures are incompatible.

### Route control through Access and large transfers directly

Cloudflare Tunnel is the only ingress. A control hostname requires a Cloudflare Access service token held by Vercel and exposes only provisioner and private cell-control paths through Traefik. The independent provisioner/cell bearer remains required.

Vercel Functions cap request bodies at 4.5 MB, so file bodies never pass through them. Substrate returns a small direct-transfer ticket. The browser streams up to a 90 MiB payload through a separate transfer hostname that exposes only cell upload/download. Exomem validates the short-lived signed grant, exact origin, cell/tenant/principal/operation/limits, and durably consumes its JTI before any bytes. CORS preflight permits only the canonical origin and exact transfer headers; Origin is defense in depth rather than authorization. Replay rejects after restart, aborted transfers require a fresh ticket, and a ticket cannot change path/operation. Long-lived cell credentials are never sent to the browser. Export downloads use short-lived B2 URLs.

Alternative considered: proxy the existing 100 MiB route through Vercel. Rejected because it deterministically fails before application streaming code runs. A credential-injecting edge proxy was also rejected because it would duplicate every cell credential at a second public security boundary.

### Make the cell contract fixed, private, and resource-bounded

Each cell has one namespace, one single-replica StatefulSet, one ClusterIP service, and one 10 GiB LUKS-backed Hetzner PVC with `Retain`. Kubernetes quota permits exactly that one 10 GiB claim and denies another; the separate 5 GiB application entitlement is enforced by Exomem rather than a 5 GiB PVC quota. An init step creates invariant absolute vault/state/log directories with no symlinks, mode `0700`, and the runtime UID. Those paths and the original cell ID are binding inputs. Alpha policy is storage entitlement 5 GiB, upload payload 90 MiB, worker count zero, semantic/media false, and no vision/diarization/file watcher.

Tenant namespaces enforce restricted Pod Security and default-deny ingress/egress. Only labelled Traefik pods may reach the Exomem port; the cell has no external egress. The authenticated hosted readiness helper, not the personal OAuth healthcheck, gates readiness.

Alternative considered: shared process or shared writable volume. Rejected because it expands the canonical-data and credential blast radius and undermines the existing single-vault runtime boundary.

### Centralize durability around the portable export contract

A system durability worker starts every 30 minutes. Under the durable per-cell operation lock it disables both Traefik control and transfer routes, externally proves rejection (including an unused ticket), quiesces/drains, creates and stages a verified portable archive on a bounded system scratch volume, releases/resumes/reopens within a two-minute objective, then performs envelope AES-256-GCM encryption and B2 upload off the snapshot copy. Runtime B2 credentials have upload/list but no delete. Recovery age warns at 45 minutes and blocks alpha at 60. Recovery objects use seven-day Object Lock and 30-day normal retention. Durable recovery and plaintext-delivery ledgers retain tenant-scoped exact B2 version references and the authoritative lock deadline; routine deletion uses explicit-page-size exact-key checks with hard page/item ceilings and stops at lexicographic siblings rather than scanning a bucket. A credential-free dispatcher precomputes an exact Job name, atomically binds one eligible destroy/discard claim to that identity, and only then creates the admission-scoped short-lived deletion Job. The Job resumes only its unexpired named claim; a failed create leaves a bounded lease to expire. Admission fixes the complete Job/pod/container/resource/environment/security/volume shape. Only that Job receives deletion, HCloud, wrapping-key, and public-verifier credentials; it removes exact versions and associated markers without governance bypass after the maximum durable/live lock expiry, re-reads the ledger before wrapped-key/final proof, and then exits. Restore runs as an offline, pinned Job into a fresh candidate and recreates destination binding state.

Primary cluster DR is declarative reconstruction from Terraform/Ansible/Helm plus the external provisioner/control-plane records and retained volumes/backups. SOPS/age protects static secrets; dynamic credentials, fences, resource references, and wrapped keys are encrypted in the external provisioner store under an escrowed root key. A 30-minute encrypted logical backup covers the complete Substrate application database and provisioner schema; post-restore provider scans compute the maximum fence from immutable side-effect metadata before accepting mutations, then adopt or quarantine newer resources. A non-printing secret handoff enforces a per-secret destination/version matrix: Access credentials go only to Vercel, the Tunnel credential only to K3s, one `EXOMEM_HOSTED_SCHEDULER_SECRET` sender version only to the K3s scheduler, active plus optional previous hosted-scheduler receiver versions only to the three Exomem hosted Vercel handlers, global `CRON_SECRET` remains confined to Vercel, and other shared application credentials only to their named producer/consumer pair. Access and Tunnel rotate separately; hosted-scheduler rotation uses the contract's two-version receiver overlap and retires the old version only after sender cutover, cross-route denial, and cadence proof; root wrapping-key rotation uses dual-key overlap, versioned rewrap/re-encryption, and proof that no live record remains on the old key. Off-host etcd snapshots are secondary.

Alternative considered: per-cell filesystem backup CronJobs. Rejected because they cannot truthfully stop Substrate routing or safely coordinate export/release and would require B2 egress/credentials inside every tenant namespace.

### Declare cost, capacity, and recovery gates

The alpha starts on CX33 and stops automatic provisioning at six active USER cells, two RECOVERY/restore-candidate cells, and eight potential/attached cell volumes, preserving eight spare attachments beneath the provider limit of sixteen. Increasing the cap requires a fresh soak and cost sheet. EUR 5 is explicitly subsidized and is checked against actual Paddle/tax and cloud costs before invitations.

Capacity admission combines three authorities: a five-minute Ed25519 live receipt from a collector holding the signing seed and read-only HCloud token; a fresh strict Kubernetes Namespace/Node/PV/PVC/VolumeAttachment observation; and a serialized SQLAlchemy/Alembic PostgreSQL reservation ledger. The collector and worker list namespaces broadly, ignore only names with neither the exact cell resource-name shape nor any owned identity marker, and fail closed when a candidate has a stripped or malformed identity. The receipt is bound to kube-system UID, exact foundation server ID/location, separate USER/RECOVERY counts, and exact-server attached volume count. Routine and volume-registration workers receive only the public verifier and immutable contract. Under the existing TenantFence -> Operation -> CellOperationLock order, admission locks the singleton CapacityLedger before destructive history and active reservations, so concurrent workers cannot over-admit or resurrect capacity after proof-valid destruction. Reservations survive retries, pending work, failures, claim loss, and restarts. Final provider-proved DISCARD or DESTROY completion releases matching older reservations atomically and always writes an immutable tenant/cell or tenant destructive fence; an equal-or-newer fence blocks later admission while a genuinely later provision remains eligible. Collector sequence is diagnostic rather than monotonic authority: signature, TTL, cluster/server binding, fresh reconciliation, and PostgreSQL serialization permit clean-cluster reconstruction without replay state.

Targets are RPO 0/RTO 5 minutes for pod failure, RPO 0/RTO 60 minutes for node replacement with intact volume, and RPO one hour/RTO four hours for volume or operational-database loss. Account access/billing stop immediately on deletion; `deleted` waits until locked recovery data expires and all proof fields are true.

## Risks / Trade-offs

- **One node can take every cell offline** -> accept for private alpha, retain independent volumes, automate declarative replacement, and measure the 60-minute RTO.
- **A 30-minute backup schedule is not zero data loss after volume loss** -> state the one-hour RPO, alert on backup age, and run clean restores.
- **The provisioner is a trusted cluster-wide boundary** -> narrow RBAC, fixed charts/values, platform-owned admission, encrypted external state, and independent destructive-action probes.
- **Retain protects data but defeats namespace-only deletion** -> record HCloud IDs/labels, isolate the provider credential in a privileged worker, verify provider absence, and test static-PV rebind to the original handle.
- **Long operations exceed Substrate retry ceilings** -> land the pending/final action union and non-attempt-consuming waiting checkpoint before infrastructure integration.
- **Cloudflare's 100 MB request cap bounds uploads** -> set 90 MiB payload plus measured multipart headroom and test the real edge path.
- **B2 S3 lockfile compatibility is not yet proven** -> fail the deployment bootstrap until concurrent-lock and version-recovery tests pass; do not fall back to local state.
- **Hetzner CSI may expose only a StorageClass-level LUKS secret** -> prove the pinned driver behavior and describe any cluster-shared volume key honestly; retain distinct per-cell application/archive keys.
- **Object Lock delays final deletion** -> revoke service and destroy online data immediately, expose the maximum seven-day retention delay, and emit `deleted` only after final proof.
- **EUR 5 has weak margin after Paddle/tax** -> hard-cap seats and require live fixed/marginal cost evidence before each cap increase.

## Migration Plan

1. Land the independent current-main cache fix so the baseline suite is deterministic.
2. Refresh Exomem PR #227, resolve current-main conflicts, add init/restore/rotation/direct-transfer runtime contracts with TDD, and build the immutable image.
3. Refresh Substrate PR #32, update the exact fixture/release, add Access headers, direct-transfer tickets, and pending/final action waiting, fix deployment CI, and verify migrations/Paddle webhooks plus the versioned external K3s scheduler contract for the three authenticated Vercel routes.
4. Apply foundation/durability Terraform from reviewed saved plans, bootstrap K3s with Ansible, and install pinned platform charts from an empty environment.
5. Deploy the provisioner and canary cell; pass protocol, fence/idempotency, route, network, backup/restore, and deletion drills.
6. Provision and soak the owner's account through the real product path; record cost/resource/latency evidence.
7. Invite the first non-technical account only after the owner, clean restore, and no-shell onboarding gates pass.

Rollback keeps the Substrate hosted feature/invites disabled, stops new reconciliation, preserves retained PVCs/B2 objects and external operation state, and rolls the immutable runtime/system releases back to their prior contract-compatible digests. Destructive Terraform or tenant cleanup is never part of application rollback.

## Open Questions

No product decision remains open. Three deployment validation gates remain: prove B2 S3 lockfile behavior in the real account, pin the exact CSI release/key behavior compatible with K3s 1.35, and record the live Paddle account fee/tax choice. Failure of any gate blocks deployment and reopens only that technical decision.
