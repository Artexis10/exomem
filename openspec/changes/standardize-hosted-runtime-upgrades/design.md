## Context

Hosted runtime identity is split across three authorities:

- Exomem's deployment lock selects the immutable runtime image and contract used for new cells.
- Substrate's candidate catalog, release fixtures, rollout assignments, and routable observations decide which contract may be assigned, observed, and promoted.
- The reviewer promotion harness produces the time-bounded client evidence that admits a candidate into the live cohort.

Changing only one authority does not upgrade Hosted. A new deployment lock changes the birth runtime of future cells but leaves existing cells on their assigned images. Candidate promotion changes client admission but does not move runtime bytes. A direct Helm image change can preserve a vault, but it is invisible to the control plane and therefore cannot establish trusted runtime identity.

The platform already has expand/contract deployment locks for mixed-version admission. Substrate's `add-hosted-cell-rollforward` change specifies the missing per-cell primitive: an operator-authorized, leased, checkpointed, in-place transition whose observation must exactly confirm the authorized target before the routable record moves. This change supplies the fleet-level workflow that composes those primitives into one repeatable release upgrade.

Concrete release numbers, digests, commits, fleet membership, and evidence belong to an execution record and tasks—not to the reusable requirements.

## Goals / Non-Goals

**Goals:**

- Define one deterministic workflow for every Hosted runtime release.
- Verify one exact signed target and import its exact agent and gateway contracts at every release-pinned consumer site.
- Make release adoption itself incapable of mutating an existing tenant cell.
- Inventory and reconcile control-plane, provisioner, and cluster views before declaring the fleet empty or beginning mutation.
- Roll existing cells sequentially under explicit operator authority while preserving cell identity, binding, volume, canonical vault bytes, security state, credentials, grants, and entitlement.
- Keep mixed-version expand admission active until no routable, assigned, or unfinished legacy dependency remains.
- Fail closed on drift, preserve recovery authority, and never use tenant deletion or downgrade as automatic rollback.
- Require reviewer promotion and personal-account acceptance before declaring a runtime adopted.
- Produce a durable, redacted execution record that makes each release run auditable and restartable.

**Non-Goals:**

- Per-cell zero downtime. A single replica on ReadWriteOnce storage requires a bounded interruption while the old pod releases the volume.
- Automatic background fleet upgrades. Operators authorize each release and each cell transition.
- Runtime downgrade through rollforward. Recovery to older bytes remains a separately authorized restore/replacement decision.
- Widening OAuth client partitions, deleting arbitrary clients, bypassing reviewer authority, or automating human browser consent.
- Changing product profiles, deployment-lock protocol generations, or command membership merely because a newer release exists.
- Deleting ordinary tenants as cleanup. Only explicitly identified reviewer-purpose resources may use the existing bounded cleanup action.

## Decisions

### 1. Model an upgrade as a durable state machine, not a version-bump checklist

Each execution advances through explicit phases:

`selected → trusted → expanded → inventoried → rolling → drained → contracted → promoted → accepted → complete`.

Every phase records exact input identities, evidence hashes, timestamps, and the next safe action. A retry revalidates the phase's current facts before advancing. A mismatch stops without guessing or silently regenerating authority.

The alternative—copying a runbook and editing version strings—cannot prove which steps ran, whether a fleet was truly empty, or whether a resumed session is acting on the same release.

### 2. Separate release adoption from tenant mutation

The Substrate trusted-release deployment and Exomem expand lock are additive platform changes. They select the target for future provisioning and admit the target beside every authoritative legacy release. They MUST NOT enqueue a lifecycle operation or mutate a tenant namespace, workload, volume, vault, binding, credential state, grants, entitlement, or routing observation.

Existing cells move only in the `rolling` phase through the dedicated rollforward operation. This preserves a strong boundary: adoption supplies eligibility; a cell-specific operation supplies mutation authority.

### 3. Inventory from independent authorities and reconcile before mutation

Preflight builds a redacted inventory from:

- Substrate routable cell observations;
- tenant bindings, rollout assignments, unfinished lifecycle operations, and capacity claims;
- provisioner desired state;
- cluster tenant namespaces, Helm releases using the ConfigMap driver, StatefulSets, and persistent volumes;
- active reviewer authorities and reviewer-purpose tenants.

The inventory classifies each cell as target, legacy, reviewer-purpose, terminal, or inconsistent. An empty fleet is valid only when all authoritative views agree. Missing, duplicate, ghost, unbound, or release-divergent entries block the upgrade until reconciled by an explicit repair path. The execution record stores hashes and bounded identifiers, not tenant content or secrets.

Relying only on the routable table was rejected because prior out-of-band upgrades and destroy-path ghosts have already proven that one view can be stale.

### 4. Compose trust before deployment and retain every referenced legacy runtime

Substrate imports the target's exact signed agent and gateway fixtures into every release-pinned mapping before Exomem composes a deployment lock naming the reviewed consumer commit. The expand lock's legacy catalog contains every release/protocol unit referenced by a routable cell, active assignment, or unfinished legacy operation at preflight—not merely the immediately previous release.

The target is the exact release tag commit, immutable OCI digest, candidate bytes, source-closure proof, and attestations. Source closure is component-scoped: the already published runtime closes at its signed candidate source commit, while the provisioner closes from its candidate source through the platform composition commit. This lets a later platform composition consume an older immutable stable runtime without pretending that unrelated post-release runtime-source changes built those bytes. Mutable tags, regenerated head artifacts, null mappings, architecture mismatch, or digest drift fail before deployment.

### 5. Roll one explicitly assigned cell at a time

The operator creates a target rollout assignment for one cell, then creates the fenced rollforward operation carrying that assignment and exact target. The reconciler does not auto-enrol the fleet. The sequence is:

1. confirm the cell is bound, ready, entitled, and currently represented by the inventory;
2. quiesce routing and drain admitted work;
3. run the fixed provisioner-owned, read-only fingerprint job from the immutable provisioner image, fingerprint canonical vault bytes, and record the prior Helm revision and control-plane identity;
4. run a declared privileged migration job when the target requires one;
5. perform the atomic Helm transition using the deployment lock's target;
6. require private readiness to advertise the exact authorized release, protocol, profile, command fingerprint, schema digest, and compatibility digest;
7. run the same provisioner-owned fingerprint job again, allowing only declared rebuildable derived indexes to differ;
8. upsert the routable observation for the same cell identity and complete the assignment;
9. restore routing only after desired state and readiness are green.

Failure before the observation moves returns the Helm release to its prior revision and leaves the prior control-plane identity. If the provisioner transition has already committed when the control plane's independent readiness check vetoes the target, the control plane invokes one operation-scoped `rollback-rollforward` action; the provisioner accepts it only while the same operation's Helm marker and prior revision remain authoritative. Failure after the observation moves stops the fleet, keeps expand mode active, and requires explicit recovery; the workflow never disguises a downgrade as rollforward.

Helm rollback removes the marker from current values, so replay also accepts a bounded
historical proof: one or more retained revisions may carry the same operation digest, but
all of them must name one unique prior revision and that revision's values must equal the
current deployed values. This permits the runtime transition and route-reopen revisions
created by one operation while rejecting conflicting predecessor authority. Provisioner
fleet projection applies a finalized rollback to the saved prior reviewed runtime, carries
compatibility whenever the selected runtime declares it, and resolves historical full v2
identities against the retained legacy catalog. Older reviewed runtime identities without
compatibility remain observable for backward-compatible health and inventory.

Sequential execution was chosen over parallel batches because the alpha fleet is small, per-cell downtime is already bounded, and one-at-a-time rollout gives a clean canary and limits data risk. A future capability may add bounded batching without changing the per-cell contract.

### 6. Treat observation as confirmation, never authority

The operation's reviewed target authorizes what may be recorded. The cell's private readiness response can only confirm exact equality or veto the transition. It cannot originate or widen its own trusted identity. This preserves the control-plane security model while eliminating the stale-record problem of out-of-band Helm changes.

### 7. Contract only after a zero-legacy proof

Contract mode is eligible only when a fresh inventory proves all of the following:

- every routable ordinary cell reports the exact target identity;
- no active assignment or unfinished operation references a legacy target;
- no provisioner desired state or cluster workload remains on a legacy image;
- every expected tenant binding and capacity claim reconciles;
- no failed rollforward awaits recovery.

The contract lock must share the expand lock's exact component and catalog lineage and differ only in immutable admission mode. A stale or partial zero count is insufficient.

### 8. Promotion and acceptance close the release, not the deployment

After contract cutover, the reviewer flow runs its free preflight, prepares immutable Claude and OpenAI artifacts, spends reviewer authority only when the human is ready, imports both evidence chains within their real validity, and promotes the target cohort. When the bounded operator-client partition is full, reuse is allowed only for an explicitly supplied, disabled, exact-configuration, never-authorized pinned client that Substrate revalidates; the harness never auto-selects or directly enables one.

A non-reviewer personal account then proves target identity, OAuth, bootstrap, recall, governed write/read-back, refresh, reconnect, and cleanup/leak checks. Deployment readiness alone is not adoption success.

### 9. Rollback preserves tenants and depends on the last completed phase

- Before expand deployment: discard only uncommitted target artifacts.
- After expand but before any target cell exists: restore the prior platform lock and expire target stages.
- During a cell transition before observation: the atomic Helm operation returns that cell to its prior revision and the fleet stops.
- After any cell is recorded at target: keep expand mode and both runtime identities trusted. Do not delete, relabel, or downgrade the cell automatically; use the separately authorized recovery/restore path.
- After contract or promotion: reopen expand only through a reviewed lock and stop further admission while preserving every cell and vault.

This makes rollback a safe stop-and-recover procedure rather than a promise that all forward-only tenant mutations are globally reversible.

### 10. Keep concrete releases in execution records

The generic specification contains target/current roles and invariants. A release execution records the actual version, source commit, image/candidate digests, Substrate and Exomem commits, expand/contract lock hashes, fleet inventory hashes, per-cell operation IDs and preservation evidence, promotion evidence, acceptance results, and final state. Later releases reuse the same schema and workflow with new reviewed values.

## Risks / Trade-offs

- **[Control-plane and cluster state disagree]** → Stop before mutation and require explicit reconciliation; never infer an empty fleet from one table.
- **[A target needs a privileged filesystem migration]** → Declare it in signed release metadata, run a bounded TTL job with the existing minimal capability set, and leave the serving container unprivileged.
- **[A cell is unavailable during rollforward]** → Route fails closed for only that tenant; sequence cells so there is no global maintenance window.
- **[Vault content changes during comparison]** → Quiesce and drain before the first fingerprint; exclude only named rebuildable indexes, never arbitrary paths.
- **[The selected stable runtime predates the upgrade evidence command]** → Run canonical fingerprinting from the immutable provisioner image under an exact admission policy, and prove its classification remains byte-for-byte equivalent to the runtime portability contract.
- **[A rollforward verifies but fails later]** → Keep expand active, stop the fleet, retain the target cell and its vault, and require explicit recovery.
- **[Legacy catalog is incomplete]** → Derive it from the reconciled authoritative inventory and refuse both composition and deployment on a missing referenced unit.
- **[Reviewer authority expires mid-run]** → Resolve all free prerequisites before starting the clock and issue sibling credentials immediately after OAuth.
- **[Execution records expose tenant data]** → Store opaque IDs, hashes, release identities, result codes, and timestamps only; exclude vault paths, titles, content, credentials, and browser tokens.
- **[Cross-repository changes drift]** → Land and pin the Substrate consumer first, then compose Exomem locks from its immutable commit and exact fixture corpus.

## Migration Plan

1. Implement and land the Substrate `hosted-cell-rollforward` control-plane primitive, including destroy-path routable cleanup and database/integration coverage.
2. Implement and land the Exomem provisioner/Helm rollforward primitive and its preservation, migration, replay, and runtime-confirmation tests.
3. Implement the generic upgrade execution record, inventory/preflight, lock gates, reviewer-client reuse, and operator runbook across both repositories.
4. Exercise the complete workflow in local/PostgreSQL/K3s acceptance environments, including empty, mixed-version, divergent, failed-rollforward, and resumed executions.
5. For each production release, create a reviewed execution record and import the exact target contract into Substrate.
6. Deploy Substrate, compose and deploy the Exomem expand lock, then rerun inventory and prove adoption caused no tenant mutation.
7. If the fleet is empty, record the no-op. Otherwise roll one canary and then each remaining legacy cell sequentially, stopping on the first failure.
8. Prove zero legacy dependencies and deploy the contract lock.
9. Run reviewer promotion and personal-account acceptance.
10. Record final fleet, capacity, cohort, authority, and lock state; keep tenant cells live.

## Open Questions

None before implementation. The operator-initiated, pre-assigned, sequential rollforward policy and forward-only recovery boundary are decided here; later batching or automation requires a separate change.
