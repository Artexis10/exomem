## 1. Upgrade execution contract

- [x] 1.1 Add a strict versioned schema for redacted Hosted runtime-upgrade execution records, including phases, immutable release/repository/lock identities, inventory hashes, per-cell operation references, evidence hashes, stable result codes, and next-safe-action state
- [x] 1.2 Add red-first unit tests for valid phase progression, safe retry, stale-input refusal, content/secret-field rejection, and phase-aware recovery decisions
- [x] 1.3 Implement the pure execution state machine and canonical record hashing without performing external effects
- [x] 1.4 Add a governed operator CLI that creates, inspects, revalidates, and advances an execution record only through the state machine
- [x] 1.5 Keep concrete release runs under `infra/operations/` as schema-validated evidence while excluding credentials, browser tokens, tenant content, paths, and titles

## 2. Reconciled fleet inventory

- [x] 2.1 Define the normalized inventory model for routable observations, tenant bindings, assignments, unfinished operations, capacity claims, provisioner desired state, Kubernetes namespaces/workloads/volumes, ConfigMap-driver Helm releases, and reviewer-purpose state
- [x] 2.2 Add red-first pure-logic tests for empty-fleet agreement, ordinary and reviewer cells, mixed releases, multiple legacy releases, out-of-band release divergence, destroyed-cell ghosts, missing bindings, duplicate identities, and stale capacity claims
- [x] 2.3 Implement reconciliation and classification with deterministic redacted output and one canonical inventory digest
- [x] 2.4 Add read-only Substrate, provisioner, and Kubernetes collectors with bounded timeouts and content-free diagnostics
- [x] 2.5 Make preflight refuse expand, rollforward, contract, or promotion when authoritative views disagree
- [x] 2.6 Record a zero-cell rollout as an explicit no-op only when every collector agrees; otherwise require repair and a fresh inventory
- [x] 2.7 Bootstrap the first pre-expand provisioner observation from one exact incoming image in a minimum-authority, tokenless, deadline-bounded Job when the installed legacy image lacks the collector

## 3. Substrate per-cell rollforward companion

- [x] 3.1 Reconcile the existing `add-hosted-cell-rollforward` artifacts with this design: operator-created assignment, explicit per-cell initiation, sequential fleet ownership, forward-only targets, and no reverse-rollforward recovery
- [x] 3.2 Add the `rollforward` lifecycle operation kind and any database constraint/migration updates with leased, checkpointed, idempotent state
- [x] 3.3 Activate an exact target assignment from a rollforward operation without requiring a successor cell while preserving every existing target and generation equality check
- [x] 3.4 Fail routing closed for a cell from quiescence through verified completion without affecting other tenants
- [x] 3.5 Upsert the routable contract observation for the same cell identity only after exact target and preservation confirmation
- [x] 3.6 Reject older, absent-lock, changed-digest, mismatched-assignment, or self-asserted targets before trusted identity changes
- [x] 3.7 Clear the routable observation atomically when an authorized tenant destruction reaches its terminal checkpoint
- [x] 3.8 Add PostgreSQL and unit coverage for operation leases/replay, assignment activation, same-cell observation, forward-only refusal, mid-roll routing, destroy-path cleanup, and routable-set digest movement

## 4. Exomem provisioner and cell rollforward

- [x] 4.1 Extend the strict provisioner wire models with authorized `rollforward` and operation-scoped pre-record `rollback-rollforward` actions plus exact target/compatibility identity; persist checkpoints and prior Helm revision as provisioner-owned evidence; return only bounded content-free completion
- [x] 4.2 Add red-first provisioner tests for quiesce/drain, idempotent checkpoints, target fencing, unauthorized drift, exact confirmation, preservation failure, strict operation-scoped Helm recovery, and post-record stop behavior
- [x] 4.3 Implement the live and in-memory provider rollforward action against the deployment lock's target while retaining unauthorized fixed-value drift protection
- [x] 4.4 Add a `migrate` workload mode to the cell chart using a bounded TTL job with the existing minimal root filesystem capabilities and no serving-container privilege change
- [x] 4.5 Bind privileged migration necessity to signed target metadata and prove no migration job renders for a plain image transition
- [x] 4.6 Quiesce the selected cell, drain admitted work, and capture a canonical vault fingerprint plus prior Helm revision before mutation
- [x] 4.7 Run migration when declared, perform `helm upgrade --atomic --wait`, and require authenticated private readiness to match every authorized runtime identity field
- [x] 4.8 Recompute the canonical vault fingerprint, exclude only the declared rebuildable derived indexes, and refuse completion on any other byte loss or change
- [x] 4.9 Return a pre-record failure to the prior Helm revision and identity; after target observation, stop and require explicit recovery rather than downgrading or relabelling
- [x] 4.10 Add cross-language contract fixtures and K3s acceptance proving same-cell/same-volume operation, bounded unavailability, vault preservation, runtime confirmation, and replay safety
- [x] 4.11 Make committed Helm rollback replay-safe across multiple same-operation revisions with one unique prior revision, restore prior fleet intent after finalized rollback, accept compatibility-bearing inventory, and preserve historical full v2 identities plus optional compatibility for pre-upgrade health

## 5. Generic expand, drain, and contract orchestration

- [x] 5.1 Extend release verification to emit the exact target identities consumed by both Substrate fixtures and Exomem deployment-lock composition
- [x] 5.2 Derive the authoritative legacy catalog from the reconciled routable, assigned, and unfinished-operation release set and reject missing, duplicate, mutable, or unreferenced entries
- [x] 5.3 Make the operator CLI prove that Substrate's reviewed consumer commit trusts the target at every pinned site before composing the Exomem lock pair
- [x] 5.4 Gate expand deployment on exact lock verification and prove post-deploy that adoption enqueued no tenant lifecycle operation and changed no existing tenant Kubernetes or control-plane resource
- [x] 5.5 Implement explicit canary selection followed by sequential per-cell assignment/rollforward, stopping on the first non-success terminal
- [x] 5.6 Gate contract deployment on a fresh zero-legacy proof across every collector and exact expand/contract lineage equality
- [x] 5.7 Implement phase-aware stop/recovery output that keeps expand active whenever any target cell or unresolved transition exists
- [x] 5.8 Update the Hosted alpha runbook so operators execute the CLI phases and evidence checks rather than manually editing release-specific command blocks

## 6. Reviewer promotion and release acceptance

- [x] 6.1 Add red-first reviewer-bootstrap tests for an optional explicit existing client ID, unchanged fresh-ID behavior, full-partition reuse, mismatched configuration, enabled client, prior reviewer authorization, and no silent fallback
- [x] 6.2 Implement explicit client reuse through Substrate's ordinary pinned registration so the server revalidates eligibility before invite or authority creation
- [x] 6.3 Add the target execution and reconciled zero-legacy state to the free promotion preflight without starting the reviewer clock
- [ ] 6.4 Preserve the prepare/run boundary and require both Claude and OpenAI evidence chains to import within their actual assignment validity before promotion
- [ ] 6.5 Add personal-account acceptance for exact target identity, OAuth, bootstrap, recall, governed write/read-back, refresh, reconnect, and authority/capacity/unfinished-operation leak checks
- [ ] 6.6 Ensure failed reviewer cleanup can select only the explicit reviewer-purpose tenant and can never select an ordinary tenant or its volume

## 7. Generic verification and independent review

- [x] 7.1 Run focused Exomem unit, provisioner, Helm, cross-language, K3s, scaffold leak, and lint gates for the new workflow
- [ ] 7.2 Run focused Substrate unit, PostgreSQL integration, migration, contract fixture, OAuth client, lifecycle, promotion, and runbook gates
- [ ] 7.3 Exercise empty fleet, one-cell canary, mixed fleet, divergent authorities, failed migration, failed readiness, preservation mismatch, interrupted/replayed operation, and post-record failure in non-production environments
- [ ] 7.4 Review both repository diffs for tenant-data safety, authorization boundaries, secret handling, compatibility, rollback truthfulness, and unintended release/profile changes
- [ ] 7.5 Verify the complete workflow end to end from a fresh execution record before authorizing production

## 8. First execution: runtime 0.57.2

- [x] 8.1 Verify the exact `v0.57.2` source commit, amd64 image digest, signed runtime candidate, source closure, attestations, protocol, profile, command fingerprint, schema digest, and compatibility digest
- [x] 8.2 Import the exact `0.57.2` agent and gateway fixtures into every Substrate release-pinned production site while retaining every release found by the live legacy inventory
- [ ] 8.3 Land and deploy the reviewed Substrate trusted-release, rollforward, reviewer-client, migration, and operator changes; record the immutable consumer commit
- [ ] 8.4 Compose, verify, review, and land the Exomem `0.57.2` expand/contract lock pair and provisioner image from the exact Substrate consumer and signed target evidence
- [ ] 8.5 Create the schema-valid `0.57.2` execution record, run the live reconciled inventory, and resolve every ghost, divergence, stale assignment, unfinished operation, or capacity mismatch before mutation
- [ ] 8.6 Deploy Substrate and the Exomem expand lock, then prove adoption changed no existing tenant resource or data
- [ ] 8.7 If the reconciled fleet is empty, record the no-op; otherwise roll one canary and every remaining legacy cell sequentially with preservation and exact-runtime evidence
- [ ] 8.8 Prove zero legacy dependencies and deploy the reviewed contract lock
- [ ] 8.9 Run free preflight, prepare the explicit eligible reviewer client, and conduct the human-timed Claude/OpenAI promotion ceremony
- [ ] 8.10 Complete personal-account OAuth and full read/write/reconnect acceptance on `0.57.2`, keep the personal tenant live, and record final fleet, lock, cohort, authority, capacity, and operation state
