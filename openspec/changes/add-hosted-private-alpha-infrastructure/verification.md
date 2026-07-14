# Verification evidence

Evidence is appended as implementation gates close. A checked task means the result below was reproduced or explicitly inherited from the reviewed branch baseline; known red baselines remain named until their later task closes them.

## 2026-07-14 — trustworthy baseline

### Exomem `main`

- Frontmatter cache freshness fix merged through PR #230 at `80eb668` after independent review.
- Fresh lean suite: 2,233 passed, 19 optional platform/model skips.
- The fix hashes current page bytes before cache reuse, so same-size/same-mtime external edits cannot serve stale frontmatter.

### Exomem PR #227

- Refreshed branch head: `d925642` (`feat/hosted-multi-tenant-service`), mergeable and pushed.
- Semantic merge preserves current-main content-hash freshness, operation-scoped writer fencing, hosted mutation serialization, idempotency isolation, privacy, transfers, and workers.
- Independent review found and then approved the invocation-level hosted admission fix: read-only `connect_memory` modes remain usable during mutation-authority outage/quiescence; write and unknown modes fail closed.
- Fresh full lean suite: 2,392 passed, 19 optional platform/model skips. Focused hosted/lifecycle/lease suite, Ruff correctness/scoped checks, latency gate, installed-wheel product E2E, capability generation, package/image checks, and strict OpenSpec validation passed.
- Every GitHub check on the recorded head passed, including Python 3.11/3.13, retrieval golden gate, Docker smoke, installed-wheel E2E, package, lint/types, onboarding, capabilities, and OpenSpec.

### Substrate PR #32

- Refreshed branch head: `ffc499c` (`feat/exomem-hosted-service`), mergeable and pushed.
- Reproduced failed Vercel deployment: three minute/hour Exomem jobs in `vercel.json` violated the Vercel Hobby once-per-day cron limit.
- Fixed by keeping the authenticated handlers on Vercel while moving cadence ownership to the versioned K3s schedule contract. The contract pins origin, paths/cadence, dedicated least-privilege bearer and two-version receiver overlap, redirect denial, timeouts/deadlines, non-overlap, retries/history/TTL, content-free metrics, and alerts. Global `CRON_SECRET` is not exposed to K3s.
- Fresh unit/contract suite: 451 passed across 102 suites; TypeScript (`npx tsc --noEmit`), changed-file Prettier, strict OpenSpec (16 items), production Next build, and diff checks passed. Independent code and cross-repository artifact reviews approved the hosted implementation.
- Replacement Vercel deployment check passed on `21bb0f1`, proving the original deployment failure is closed.
- Real PostgreSQL integration/migration baseline inherited unchanged from reviewed `52dabed`: 22 tests across five suites through migration 0021. This session had no `DATABASE_URL`, so the local production-only migration launcher correctly skipped; static migration serialization/transaction tests and the production build passed.
- Next 16 lint baseline repaired at `ffc499c`: `npm run lint` now uses ESLint 9 flat config and exits with 0 errors plus 23 visible pre-existing warnings. The new Hooks rule remains error-level for new/hosted code and is disabled only for eight exact legacy files. A clean scratch `npm ci --ignore-scripts`, lint, changed-code zero-warning lint, TypeScript, focused 14-test suite, build, and independent review all passed.

## 2026-07-14 — Exomem runtime deployment contract frozen

- Exomem PR #227 head `b38bea8` adds `complete-hosted-runtime-deployment-contract`, including normative operator v1 and direct-transfer v2 JSON artifacts.
- The contract fixes exact init/restore delivery, binding v2 identity, offline crash-safe candidate restore, Secret-backed active/pending credential rotation with abort/finalize, fresh literal-loopback authenticated probe, durable one-time JTI consumption, direct raw browser transfer, route-specific CORS, response/error tuples, temp quotas, and real-K3s image/mount gates.
- Strict OpenSpec validation and JSON/structural assertions passed. Final independent adversarial review approved the frozen artifact hashes with P0 0, P1 0, P2 0.

## 2026-07-14 — IaC scaffold and saved-plan guard

- Added explicit ownership boundaries for split Terraform foundation/durability/bootstrap roots, Ansible, platform/cell Helm charts, the provisioner, scripts, and hosted runbooks.
- Pinned Terraform 1.15.8, HCloud 1.66.0, Cloudflare 5.22.0, B2 0.12.1, K3s `v1.35.6+k3s1`, Hetzner CSI 2.21.1, and every validation tool. Provider locks include Linux amd64/arm64 checksums.
- Git ignores state, saved plans/JSON, tfvars, generated inventory, decrypted SOPS material, and age keys. Reproducible validation/plan/apply commands are documented.
- The non-printing JSON plan inspector rejects unapproved delete/replacement, unknown plan actions, duplicate approval, and secret-like output lacking Terraform sensitivity. Six focused tests pass; ShellCheck, Terraform formatting, Ruff, and diff checks pass.
- Apply accepts only a mode-0600 saved plan, re-inspects that exact plan, and never recomputes it. Destructive approval is per exact Terraform address.

## 2026-07-14 — split-state Terraform implementation

- Foundation now declares the dedicated CX33, stable IPv4, private network, restricted SSH-only firewall, Cloudflare Tunnel, exact control/transfer DNS ingress, and a service-token-only Access application. Destructive provider controls and Terraform `prevent_destroy` protect the node, address, network, SSH key, firewall, and tunnel.
- Durability now owns only the recovery and complete-database B2 buckets. Both use B2 server-side encryption, seven-day governance Object Lock, 30-day current-object retention, and split upload/restore/delete identities; the database backup identity cannot delete.
- The one-time local bootstrap owns only the encrypted/versioned B2 state bucket and two prefix-scoped backend identities. Foundation and durability use distinct state keys plus native S3 lockfiles. Its wrapper accepts reviewed mode-0600 plans, seals local state with SOPS/age, verifies decryption, and removes plaintext only after successful atomic replacement; interrupted applies retain recoverable local state rather than losing it.
- Normal plan/apply wrappers require a mode-0600 backend config and prefix-scoped B2 credentials from environment variables. Backend examples contain no credentials. State, backend config, plan, JSON, and tfvars artifacts remain ignored.
- Red-to-green contract tests cover lifecycle-domain separation, fixed cost-safe defaults, explicit non-global SSH CIDRs, absence of public 80/443/6443, destroy protection, exact two-host Tunnel ingress, Access service authentication, B2 retention/capability splits, sensitive outputs, backend bootstrap, sealed-state workflow, and backend-config permissions.
- Terraform 1.15.8 formatted and validated foundation, durability, and bootstrap against the committed provider locks. Focused infrastructure suites pass: 14 tests; Ruff, ShellCheck, and `git diff --check` pass.
- Real B2 concurrent lock contention and prior-version recovery remain deliberately open as task 6.6; no live apply is permitted until that evidence is recorded.

## 2026-07-14 — hardened host and K3s bootstrap

- Added idempotent Ansible roles for safe package updates, a locked dedicated administrator, key-only SSH, explicit-CIDR UFW, Kubernetes forwarding rules, fail2ban, systemd time sync, bounded journal storage, unattended security updates, and `cryptsetup`.
- The K3s role downloads only the pinned `v1.35.6+k3s1` amd64 binary with the official SHA-256 `2b52a2c1ca6eb502e2a0ffa1a4cf79eef94875926577c1e43347ed292cc92432`. The downloaded 75.9 MiB release binary independently matched that checksum, and its own help output confirmed the configured embedded-etcd, secrets-encryption, kubeconfig-group, kernel-default, S3 retention, and S3 timeout flags.
- The single-server configuration enables embedded etcd and secrets encryption, restricts the kubeconfig to the operator group, disables bundled Traefik/ServiceLB/local storage, caps service-account token lifetime, enables metadata-safe audit rotation, bounds image/container logs, and schedules 30-minute B2 S3 snapshots with seven-day retention. S3 credentials and the cluster token are written only by a `no_log` mode-0600 task.
- No task fetches or slurps the cluster-admin kubeconfig. A mode-0600 atomic inventory generator consumes only two explicitly non-sensitive Terraform outputs and ignores all sensitive output values.
- Four focused structural/behavior tests pass with one opt-in Ansible syntax test; the opt-in test passes under ansible-core 2.21.2. The playbook also passes direct syntax validation and ansible-lint 26.6.0's production profile with pinned community collections, while Ruff, ShellCheck, and diff checks pass.
- Encrypted variable handoff/break-glass escrow and a live double-run convergence proof remain open under tasks 7.4, 7.5, and 15.1; the role is not yet authorized for a live node.
