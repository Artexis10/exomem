## 1. Restore a trustworthy baseline

- [x] 1.1 Land the independently reviewed FrontmatterCache invalidation/clear fix on current Exomem `main` and rerun the complete lean suite.
- [x] 1.2 Refresh Exomem PR #227 from current `main`, resolve `vault.py`, `writer_lease.py`, and writer-lease tests semantically, and record a clean full-suite baseline.
- [x] 1.3 Refresh Substrate PR #32 from current `main`, reproduce/fix its failed Vercel check, and record its unit, integration, type, lint, migration, and build baseline.

## 2. Complete the Exomem hosted runtime deployment contract

- [x] 2.1 Add an OpenSpec delta on the PR #227 branch for supported init/restore, active-pending credential rotation, authenticated probe, and direct-transfer grant behavior.
- [x] 2.2 Add failing pure tests for a supported idempotent hosted cell initializer using the exact cell/root/UID binding inputs.
- [x] 2.3 Implement the versioned init CLI/helper in the hosted image and pass binding/idempotency tests.
- [x] 2.4 Add failing tests for offline candidate restore that rejects source binding state and atomically publishes under a new candidate identity.
- [x] 2.5 Implement the versioned offline restore CLI/helper and pass archive/path/digest/binding/rebuild tests.
- [x] 2.6 Add failing tests for active/pending credential stage, overlap health, promotion, finalization, restart persistence, and old-token rejection.
- [x] 2.7 Implement durable credential-overlap state using token digests/version metadata while keeping plaintext in injected Secrets.
- [x] 2.8 Add failing tests for direct-transfer CORS preflight, signed claims, consume-before-bytes JTI replay rejection across restart, abort/new-ticket behavior, and path/operation binding.
- [x] 2.9 Implement direct upload/download grant authorization without the long-lived cell bearer and set the alpha payload limit to 90 MiB.
- [x] 2.10 Add and test a content-free authenticated hosted exec-probe helper that generates fresh request identity headers.
- [x] 2.11 Validate a non-root/read-only-root hosted container shape with fixed writable mounts and immutable image reference.
- [x] 2.12 Run PR #227 focused security/isolation/portability tests, full Python suite, Ruff, package/image checks, strict OpenSpec validation, and independent review.

## 3. Complete the Substrate hosted control-plane contract

- [x] 3.1 Add failing provisioner-client/reconciler tests for strict pending-or-final action responses on the same 14 endpoints.
- [x] 3.2 Implement a durable non-attempt-consuming waiting checkpoint so work can remain pending past six cron cycles and process restarts.
- [x] 3.3 Add failing tests for Cloudflare Access service-token headers on provisioner and private cell control calls, including version rotation overlap.
- [x] 3.4 Implement Access header injection from validated Vercel environment configuration without accepting browser-supplied values.
- [x] 3.5 Add failing route/browser tests for small direct-transfer ticket issuance, canonical origin, one-time grant metadata, and absence of file bodies from Vercel Functions.
- [x] 3.6 Implement direct upload/download ticket routes/browser flow and update fixed alpha upload limits to 90 MiB.
- [x] 3.7 Generate the hosted gateway fixture from the selected Exomem commit and update release/protocol/command/digest expectations as one unit.
- [x] 3.8 Add integration tests holding provision, restore, and seven-day simulated deletion pending beyond six runs before accepting final proofs.
- [ ] 3.9 Verify migrations 0017-0021+, database access layer, Paddle sandbox/live configuration, signed webhooks, Brevo delivery, and build/deploy checks; prove the three frequent authenticated handlers remain on Vercel but outside Hobby cron configuration and publish the complete version `1` K3s schedule contract, fail closed on `EXOMEM_HOSTED_SCHEDULER_SECRET`, reject that dedicated bearer on unrelated global-cron routes, and never expose global `CRON_SECRET` to K3s.
- [x] 3.10 Run the full Substrate hosted test/build suite and independent review, then refresh PR #32 title/body/checklist with exact evidence.

## 4. Freeze the cross-repository release unit

- [x] 4.1 Build and publish or locally load an immutable Exomem hosted image from the reviewed PR #227 commit.
- [x] 4.2 Record image digest, Exomem release, hosted protocol, command registry, and generated contract digest in one release manifest.
- [x] 4.3 Contract-test the selected Substrate fixture against the real image `/contract` route and fail on any semantic or digest drift.
- [x] 4.4 Pin the release manifest in infrastructure values and reject mutable tags or partial version overrides.

## 5. Scaffold and statically validate the IaC surfaces

- [x] 5.1 Create `infra/terraform/{foundation,durability}`, `infra/ansible`, `infra/helm/{platform,cell}`, `infra/provisioner`, `infra/scripts`, and `docs/runbooks/hosted` with ownership documentation.
- [x] 5.2 Add pinned tool/provider/chart versions, committed Terraform lock files, `.gitignore` rules for state/plans/plaintext, and reproducible local validation commands.
- [x] 5.3 Add CI/static tests for `terraform fmt/validate`, TFLint/security policy, Ansible lint/syntax, Helm lint/template/schema, Kubernetes policy, SOPS ciphertext, Ruff/type/test, and secret scanning.
- [x] 5.4 Add a plan-inspection test that rejects unapproved destroy/replacement and sensitive output in logs.

## 6. Implement split-state Terraform

- [x] 6.1 Add failing fixture/plan tests for cost-safe defaults, explicit admin CIDRs, no public 80/443/6443, lifecycle protection, and stable outputs.
- [x] 6.2 Implement foundation resources for the dedicated Hetzner network/CX33/primary IP/firewall/SSH key and Cloudflare Tunnel/DNS/Access/service token.
- [x] 6.3 Add failing plan tests proving a durability-only change cannot touch foundation resources.
- [x] 6.4 Implement durability resources for B2 state/backup/database-backup buckets, Object Lock/lifecycle, and least-privilege upload/restore/delete identities.
- [x] 6.5 Implement the original one-time versioned B2 backend bootstrap plus separate foundation/durability keys and lockfiles; retain its already-applied state/resources unchanged after the real compatibility proof reopens the backend decision.
- [ ] 6.6 Add the separate HCP Terraform state-only bootstrap, fixed production workspaces, fail-closed preflight, and disposable proof workspace; prove real-account concurrent locking and prior-version recovery before any production apply.
- [x] 6.7 Add saved-plan/approval/apply wrappers, cost output, 0600 artifact handling, and destroy/replacement guard tests.

## 7. Implement idempotent Ansible and K3s bootstrap

- [x] 7.1 Add Molecule/check-mode or equivalent tests for base hardening, exact package/service configuration, and no fetched cluster-admin kubeconfig.
- [x] 7.2 Implement base OS updates, key-only SSH, administrator path, UFW, fail2ban, time sync, log rotation, disk hygiene, and `cryptsetup`.
- [x] 7.3 Add tests for pinned/checksummed K3s installation, embedded etcd, secrets encryption, metadata-safe audit, token expiry policy, image/log GC, and restricted kubeconfig.
- [x] 7.4 Implement K3s bootstrap plus encrypted off-host etcd snapshots and break-glass escrow.
- [ ] 7.5 Generate Ansible inventory from non-sensitive Terraform outputs and prove two consecutive site runs converge cleanly.

## 8. Implement platform and cell Helm releases

- [x] 8.1 Pin and validate the exact Hetzner CSI chart/image compatible with K3s 1.35 and prove its supported LUKS key behavior.
- [x] 8.2 Implement the encrypted `Retain` StorageClass, SOPS static-secret application, Traefik, Cloudflare Tunnel, platform namespaces, and the three CronJobs rendered only from the complete pinned Substrate schedule contract: exact origin/jobs; the sender/active/previous/max-two hosted-scheduler auth fields; `GET`/redirect `error`/five-second connect/20-second total/[200] request policy; 45/30-second starting/active deadlines; `Forbid`; backoff one/maximum two attempts; one/three history limits; 300-second TTL; the four exact content-free metric names; and 180-second missed-run/two-failure alerts.
- [x] 8.3 Add failing rendered-manifest tests for one fixed cell resource set, immutable image, original cell ID, invariant roots, 0700 init ownership, non-root/read-only security, 5 GiB application entitlement, 90 MiB upload, zero workers, and 128 MiB log cap; prove quota admits exactly one 10 GiB PVC and denies a second claim.
- [x] 8.4 Implement the versioned cell chart with StatefulSet, one 10 GiB PVC, Service, Secret, PVC-count/storage quota, application entitlement, limits, probe helper, and bounded temporary/log behavior.
- [x] 8.5 Add default-deny ingress/egress, exact Traefik ingress selectors, restricted Pod Security, and platform-owned validating admission for images/privilege/host/cross-cell references.
- [x] 8.6 Add executable network-policy probes denying cell-to-cell, Kubernetes, Neon, B2, metadata, and unlabelled-platform access.

## 9. Implement the durable provisioner core

- [x] 9.1 Add a standalone provisioner package, migrations, configuration validation, redacting logs, health endpoints, and unit-test harness.
- [x] 9.2 Add failing model/repository tests for canonical request hashes, idempotency conflicts, monotonic fences, pending/final states, resources, credentials, exports, backups, immutable provider operation/fence metadata, and restart recovery.
- [x] 9.3 Implement SQLAlchemy 2 models/repositories and Alembic migrations in a dedicated Neon role/schema, with transactional fresh-schema bootstrap, owner validation, and exact-revision readiness.
- [x] 9.4 Add failing API contract tests for bearer/protocol/content type, exact 14 paths, field/identifier bounds, no redirects, body limits, retry status, pending/final unions, and redaction.
- [x] 9.5 Implement the FastAPI v1 surface and a database-backed worker that resumes claimed operations after restart, uses PostgreSQL time for leases, locks the tenant fence before claim writes, and binds durable side effects to the current operation claim and identity.
- [x] 9.6 Add cross-language contract fixtures/tests against Substrate's real TypeScript parser for every request, pending response, final proof, and error class.
- [x] 9.7 Package exact provisioner migrations and environment-only database commands; add serialized split-authority bootstrap with ephemeral admin cleanup, runtime-only install migration, validation-only upgrade gating, and built-image PostgreSQL 17 reconstruction/concurrency/failure proofs.

## 10. Implement HCloud retained-volume ownership and recovery

- [x] 10.1 Add fake-provider tests for PV handle discovery, immutable tenant/cell/operation/fence HCloud labels, encrypted provider reference storage, released-PV/provider absence proof, and credential redaction.
- [x] 10.2 Implement the privileged volume lifecycle worker and narrowly scoped HCloud/Kubernetes recovery permissions.
- [x] 10.3 Add a clean-cluster test that reconstructs a static PV/PVC for the original recorded `volumeHandle` and rejects location/cell mismatches.
- [x] 10.4 Implement provider orphan discovery/quarantine, released-PV cleanup, and require Kubernetes PV plus HCloud volume absence before `storageDestroyed`.

## 11. Implement cell provisioning, health, and lifecycle

- [x] 11.1 Add failing reconciliation tests for deterministic opaque names, fixed Helm values, partial-attempt adoption, no PII labels, readiness gating, and crash/replay between CSI bind and HCloud registration.
- [x] 11.2 Implement provision through namespace/policy/Secret/PVC/Helm/volume-handle-registration/provider-label-verification/init/readiness/route checkpoints and return one stable provider reference/endpoint only after volume ownership is durable.
- [x] 11.3 Add failing health tests for authenticated live/ready/contract flattening and mismatched identity/release/protocol/policy/admission.
- [x] 11.4 Implement exact Substrate health responses and fail closed on every mismatch.
- [x] 11.5 Add failing lifecycle tests for quiesce, safe stop, resume, overlap rotation, seal, and serialization with maintenance operations.
- [x] 11.6 Implement lifecycle actions through the authenticated internal cell API and Kubernetes scaling with durable checkpoints.

## 12. Implement control, transfer, and maintenance routing

- [x] 12.1 Add rendered-route tests for the Access-protected control hostname, public transfer hostname, exact allowed paths, prefix stripping, and rejection of personal routes/upstream selectors.
- [x] 12.2 Implement per-cell Traefik routes and Cloudflare configuration with no direct public cell Service.
- [ ] 12.3 Add real-browser/preflight tests for canonical CORS, exact headers/methods, 90 MiB streaming, large download, abort/new-ticket, replay, path/operation alteration, and hostile origin.
- [x] 12.4 Add failing maintenance tests that close both routes, externally verify rejection of an unused ticket, drain in-flight work, serialize actions, and reopen only after release/resume.
- [ ] 12.5 Implement the durable maintenance gate and prove control/transfer behavior through the real Cloudflare Tunnel.

## 13. Implement export, backup, restore, and database durability

- [x] 13.1 Add failing export tests for truthful route stop, quiesce, archive/manifest/size verification, scratch staging, envelope AES-256-GCM, authenticated tenant/cell/operation/fence object metadata, opaque refs, release, and presigned TTL.
- [x] 13.2 Implement user export/release/download/delete with upload-only runtime and privileged restore/delete B2 credentials.
- [x] 13.3 Add failing scheduled-backup tests for 30-minute cadence, non-overlap, 45/60-minute age thresholds, two-minute staging/quiescence, scratch cleanup, post-resume encryption/upload, and restart recovery.
- [x] 13.4 Implement centralized backup scheduling, remote verification, seven-day Object Lock, 30-day normal retention, and metrics/alerts.
- [x] 13.5 Add failing restore tests for offline candidate publication, source-binding rejection, derived rebuild, authenticated readiness, and product checks.
- [x] 13.6 Implement provider-object restore through the versioned Exomem helper and durable pending/final proof.
- [x] 13.7 Add encrypted 30-minute transactionally consistent logical backups for the complete Substrate application database and provisioner schema plus a verified empty-environment scratch restore that proves owner authentication and tenant/cell resolution.
- [x] 13.8 Implement post-database-restore Kubernetes/HCloud/Traefik/B2 rediscovery that computes the maximum immutable provider-observed fence before any mutation, rejects lower-fence replay, and adopts or quarantines newer resources.

## 14. Implement ordered discard and deletion

- [x] 14.1 Add failing discard tests proving only the failed candidate is removed while the active cell and exports remain.
- [x] 14.2 Implement candidate discard with independent compute/storage/key absence proofs.
- [x] 14.3 Add failing tenant-destroy tests covering active/orphan compute, retained volumes, routes, credentials, exports, backups, provider rediscovery, and seven-day pending retention.
- [x] 14.4 Implement immediate online revocation/destruction, non-attempt-consuming retained waiting, tenant-ledger-driven exact-version and associated-marker removal after the maximum durable/live lock expiry without governance bypass, explicit-page-size exact-key checks with hard page/item/cursor bounds and sibling stop, crash-safe provider-absence/key-erasure proof with a final ledger re-read, durable plaintext-delivery deletion, and a credential-free dispatcher that atomically binds one eligible operation to the exact admission-locked short-lived Job identity before launch and resumes only that claim, plus all four exact final booleans.

## 15. Add secret handoff, observability, and runbooks

- [x] 15.1 Add a non-printing SOPS/Terraform secret handoff command with an enforced destination/version matrix—Access to Vercel only, Tunnel to K3s only, `EXOMEM_HOSTED_SCHEDULER_SECRET` to the three Exomem hosted Vercel handlers and K3s scheduler only, global `CRON_SECRET` never to K3s, shared application secrets only to named peers—and fixtures that scan output/files/history for plaintext.
- [ ] 15.2 Add separate staged Cloudflare Access, Tunnel, provisioner, and cell rotation drills, a two-version Vercel receiver/single-version K3s sender `EXOMEM_HOSTED_SCHEDULER_SECRET` rotation proving old-sender overlap, new acceptance, old rejection after retirement, unrelated-route denial, and no missed cadence without changing global `CRON_SECRET`, plus a root wrapping-key dual-version rewrap/re-encryption drill; retire an old version only after destination or ciphertext-reference proof.
- [ ] 15.3 Add external black-box availability and backup-freshness checks, external scheduler contract/outcome/last-success signals, Kubernetes event/resource signals, structured provisioner metrics, redacted logs, and actionable alerts.
- [x] 15.4 Write executable backend, deploy, secret, cell, maintenance, volume-rebind, backup/restore, deletion, node-replacement, and break-glass runbooks. (Drafts are tracked; live owner-canary rehearsal remains.)
- [ ] 15.5 Add the live monthly cost sheet and actual Paddle fee/tax record, then retain them with invitation evidence. (The automated signed-live-receipt plus PostgreSQL six-USER/two-RECOVERY/eight-attachment capacity sub-gate is implemented and tested; live economics evidence remains open.)

## 16. Deploy and prove the private alpha

- [ ] 16.1 Apply reviewed foundation/durability saved plans and retain redacted plan/cost/locking evidence.
- [ ] 16.2 Bootstrap the CX33 twice, install the platform, and reconstruct it once from an empty operator environment.
- [ ] 16.3 Provision a canary twice with one key, conflict changed input, reject stale fences, restart mid-operation, and verify exact release/readiness/isolation.
- [ ] 16.4 Run direct-transfer, credential-rotation, complete external scheduler contract/render/auth/cross-route/redirect/timeout/deadline/non-overlap/retry/history/TTL/metrics/alert drills, maintenance, 30-minute backup, near-5-GiB quiescence, clean restore, volume rebind, database restore/rediscovery, discard, and retained deletion drills.
- [ ] 16.5 Provision the owner through the real invite/entitlement path and complete capture, recall, Review Studio, transfer, export, restart, compatible upgrade, suspend/resume, and restore journeys.
- [ ] 16.6 Run the CX33 owner soak and record latency, CPU/memory, PVC/log headroom, attachment capacity, backup age, RPO/RTO, and fixed/marginal costs.
- [ ] 16.7 Run full Exomem, Substrate, Terraform/Ansible/Helm/provisioner, security, strict OpenSpec, and deployment verification plus independent final review.
- [ ] 16.8 Invite the first non-technical account only after every preceding gate is green, and prove onboarding/use/export without shell or database intervention.
