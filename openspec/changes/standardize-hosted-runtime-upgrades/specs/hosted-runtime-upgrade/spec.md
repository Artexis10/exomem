## ADDED Requirements

### Requirement: Runtime upgrades bind one exact published target across repositories

The upgrade workflow SHALL select one exact stable runtime from its release tag commit, immutable architecture-specific OCI digest, signed runtime-candidate bytes, source-closure proof, contract artifacts, and allowed workflow attestations. The runtime source closure SHALL be anchored exactly at the signed runtime candidate commit, while the provisioner source closure SHALL extend from its signed candidate commit to the platform composition commit. Substrate SHALL trust the exact agent and gateway contracts at every release-pinned production site before Exomem composes an expand/contract deployment-lock pair naming that immutable consumer commit. Mutable tags, regenerated head artifacts, null or stale mappings, unsigned candidates, architecture drift, and digest mismatch MUST be rejected before deployment.

#### Scenario: Exact target is trusted completely

- **WHEN** every release, image, candidate, source, attestation, fixture, mapping, and consumer-commit identity resolves to the reviewed target
- **THEN** the target is eligible for deployment-lock composition
- **AND** the prior authoritative releases remain available as legacy identities

#### Scenario: One pinned consumer is stale

- **WHEN** any production contract store, bootstrap control, lifecycle mapping, canary, admin catalog, gateway fixture, integration fixture, or runbook assertion resolves the target to null or another release
- **THEN** validation fails before a deployment lock can name that consumer

#### Scenario: Runtime bytes are mutable or mismatched

- **WHEN** the source revision, OCI subject, architecture, candidate bytes, attestation, protocol, profile, or contract digest differs from the reviewed target
- **THEN** composition or deployment fails before the image is assignable to a cell

#### Scenario: A later platform composes an older stable runtime

- **WHEN** the runtime candidate remains signed and immutable while the provisioner and platform composition are newer
- **THEN** the runtime closure remains anchored to its own candidate source and the provisioner closure reaches the platform composition commit
- **AND** the lock does not claim that later runtime-source changes produced the older image

#### Scenario: The authoritative legacy dependency set is empty

- **WHEN** reconciled fleet authority proves that no live cell, assignment, or unfinished operation depends on a legacy runtime
- **THEN** composition emits no legacy catalog unit merely to satisfy rollback verification
- **AND** the historical rollback manifest remains fixed by its reviewed digest and independently passes its strict release self-consistency checks

### Requirement: Every upgrade has a durable redacted execution record

The workflow SHALL persist an execution record with an explicit phase, exact release and repository identities, lock and inventory hashes, bounded operation identifiers, evidence hashes, timestamps, stable result codes, and the next safe action. Retrying a phase MUST revalidate current facts against that record before advancing. The record MUST NOT contain tenant content, vault paths, note titles, credentials, browser tokens, or raw control-plane secrets.

#### Scenario: Upgrade resumes after interruption

- **WHEN** an operator resumes an incomplete execution
- **THEN** the workflow reloads the last committed phase and revalidates its exact inputs and live invariants
- **AND** it advances only from the recorded next safe action without replaying completed tenant mutations

#### Scenario: Live facts drift from the record

- **WHEN** a release, lock, fleet, authority, assignment, or evidence identity no longer matches the recorded phase
- **THEN** the workflow stops with a stable bounded diagnostic
- **AND** it does not regenerate authority, guess a replacement identity, or advance the phase

#### Scenario: Execution evidence is inspected

- **WHEN** an operator reviews an upgrade record
- **THEN** it contains only opaque identifiers, hashes, release identities, result codes, and timestamps needed to audit the run
- **AND** it exposes no tenant-derived content or secret

### Requirement: Preflight reconciles the complete fleet before declaring it empty or mutable

Before expand deployment or any cell rollforward, the workflow SHALL reconcile Substrate routable observations, tenant bindings, rollout assignments, unfinished lifecycle operations, capacity claims, provisioner desired state, cluster tenant namespaces, ConfigMap-driver Helm releases, workloads, persistent volumes, reviewer authorities, and reviewer-purpose tenants. It SHALL classify every cell and block on missing, duplicate, ghost, unbound, release-divergent, or otherwise inconsistent state. An empty fleet SHALL be accepted only when every authoritative view agrees.

#### Scenario: All authorities report an empty fleet

- **WHEN** the reconciled inventory contains no ordinary or reviewer-purpose tenant cell, binding, workload, volume, routable observation, assignment, unfinished operation, or capacity claim
- **THEN** the per-cell rollout phase records an explicit no-op
- **AND** no tenant resource is created, changed, restarted, or deleted by that phase

#### Scenario: One live cell exists

- **WHEN** any authoritative view identifies a bound or running cell
- **THEN** the workflow records that cell in the fleet inventory and does not treat the rollout phase as empty

#### Scenario: Authorities disagree

- **WHEN** a cell, release, binding, volume, routable observation, assignment, operation, or capacity claim appears inconsistently across authoritative views
- **THEN** the upgrade stops before platform or tenant mutation
- **AND** continuation requires an explicit repair followed by a fresh reconciled inventory

#### Scenario: The installed provisioner predates fleet observation

- **WHEN** the current provisioner image does not contain the fixed fleet-observation command during a first upgrade to this workflow
- **THEN** inventory MAY run that command in one bounded Job from the exact digest-pinned, signed, and reviewed incoming provisioner candidate
- **AND** the Job receives only the current database reference, envelope-key reference, database identity, and selected deployment-lock ConfigMap needed to decrypt and classify operation history
- **AND** it receives no API bearer, provider signer, Kubernetes service-account token, tenant credential, or mutation command
- **AND** inventory is refused if the Job, its fixed security shape, its output, or its cleanup cannot be proven

#### Scenario: Destroyed operation history names an older runtime

- **WHEN** a finalized destroy or discard removes the last desired, unfinished, and cluster dependency on an older reviewed runtime
- **THEN** the provisioner observer retains the terminal operation history without requiring that dead runtime in the next deployment lock
- **AND** any runtime that still contributes to desired state or unfinished work remains exactly resolvable or inventory fails closed

#### Scenario: A destroyed binding has only historical provisioner desired-state residue

- **WHEN** Substrate marks a tenant binding destroyed, Kubernetes reports no namespace, Helm release, workload, or volume for that cell, and no route, assignment, unfinished operation, capacity claim, or reviewer authority remains
- **AND** the provisioner operation ledger is the only authority that still projects desired state for the cell
- **THEN** the provisioner observer MAY redact an identity absent from the reviewed active and legacy catalogs to a null runtime solely for cross-authority reconciliation
- **AND** inventory retains that redacted desired-state surface as terminal evidence but excludes the cell and its runtime from live and legacy dependency counts
- **AND** any remaining control-plane, reviewer, operation, capacity, or Kubernetes surface keeps the cell inconsistent and blocks the upgrade
- **AND** every unfinished operation or desired state with an independently live surface still requires an exact reviewed runtime identity or inventory fails closed

### Requirement: Expand adoption changes only the future-cell target

Deploying target trust or the expand lock SHALL select the target runtime for future provisioning and admit every authoritative legacy runtime required by the reconciled inventory. Adoption itself MUST NOT enqueue a tenant lifecycle operation or mutate, restart, migrate, replace, delete, relabel, or re-route an existing cell, namespace, workload, persistent volume, canonical vault, derived state, binding, security state, credential, OAuth grant, entitlement, assignment, or lifecycle generation.

#### Scenario: Expand is deployed beside legacy cells

- **WHEN** the target expand lock is deployed while one or more legacy cells exist
- **THEN** each existing cell remains on its assigned runtime and contract without a Kubernetes or control-plane mutation caused by adoption
- **AND** future separately authorized provisions select the target

#### Scenario: Expand inventory contains several legacy releases

- **WHEN** routable cells, active assignments, or unfinished operations reference more than one legacy release
- **THEN** the expand lock retains one exact verified catalog entry for every referenced release/protocol unit
- **AND** a missing or duplicate entry blocks deployment

#### Scenario: Adoption creates an implicit tenant operation

- **WHEN** applying trusted-release or expand-lock state would enqueue or target an existing tenant
- **THEN** the deployment is refused before that operation or mutation is committed

### Requirement: Existing cells roll forward sequentially through explicit authority

Every legacy cell SHALL move only through a separately authorized rollout assignment and fenced `rollforward` lifecycle operation carrying the exact target. The fleet workflow SHALL process one cell at a time, quiesce and drain it, require its preservation and exact-runtime evidence, and stop on the first non-success terminal. A cell MUST NOT be auto-enrolled merely because the platform target changed.

#### Scenario: One legacy cell rolls successfully

- **WHEN** the operator authorizes a target assignment and rollforward for one reconciled ready cell
- **THEN** the workflow waits for migration, runtime transition, exact-target confirmation, vault-preservation proof, routable observation, and readiness completion before selecting another cell

#### Scenario: Cell rollforward fails

- **WHEN** migration, transition, target confirmation, preservation, recording, or readiness fails for a cell
- **THEN** the fleet rollout stops and expand mode remains active
- **AND** no later cell is selected automatically

#### Scenario: Operator has not authorized a legacy cell

- **WHEN** a legacy cell is present but has no target assignment and rollforward operation
- **THEN** the cell remains on its existing runtime and contract under expand mode
- **AND** platform adoption supplies no implicit authority to move it

### Requirement: Contract mode requires a fresh zero-legacy proof

The workflow SHALL deploy the contract lock only after a fresh reconciled inventory proves that every routable ordinary cell advertises the exact target; no active assignment, unfinished operation, provisioner desired state, cluster workload, or failed recovery references a legacy target; and tenant bindings and capacity claims reconcile. The contract lock SHALL share the expand lock's exact component and catalog lineage and differ only in immutable admission mode.

#### Scenario: Fleet is fully drained to target

- **WHEN** every zero-legacy predicate is proven against current control-plane, provisioner, and cluster state
- **THEN** the reviewed contract lock is eligible for deployment

#### Scenario: One legacy dependency remains

- **WHEN** any routable cell, assignment, unfinished operation, desired state, workload, or recovery obligation still references a legacy target
- **THEN** contract cutover is refused before admission changes
- **AND** expand mode remains active

#### Scenario: Contract lineage differs from expand

- **WHEN** the proposed contract lock changes any component, target, catalog, evidence, or consumer identity in addition to admission mode
- **THEN** deployment is refused as an unreviewed lock rather than treated as the paired contract cutover

### Requirement: Promotion and personal acceptance close the upgrade

The workflow SHALL consider a target adopted only after contract cutover, free reviewer preflight, successful time-bounded Claude and OpenAI evidence import, candidate promotion, and non-reviewer personal-account acceptance. Reviewer bootstrap MAY reuse an existing client only when the operator explicitly supplies its ID and Substrate proves it is pinned, disabled, exact-configuration, and never previously authorized for reviewer bootstrap. The harness MUST NOT auto-select, delete, directly enable, or silently substitute a client.

#### Scenario: Reviewer client partition has eligible reusable capacity

- **WHEN** the bounded partition is full and prepare receives an explicitly reviewed eligible client ID
- **THEN** Substrate revalidates and rebinds that exact record without inserting another client
- **AND** invite or authority creation occurs only after registration succeeds

#### Scenario: Reusable client is ineligible

- **WHEN** the supplied client differs in platform or configuration, is not operator-managed and pinned, is enabled, or was previously reviewer-authorized
- **THEN** preflight or prepare fails before invite or authority creation
- **AND** the harness does not select another stored client

#### Scenario: Personal account accepts the target

- **WHEN** a non-reviewer personal account authorizes through the promoted cohort and proves exact target identity, bootstrap, recall, governed write/read-back, refresh, and reconnect
- **THEN** the execution may enter complete only after unfinished operations, reviewer authority, capacity reservations, and unintended tenants are also absent

#### Scenario: Acceptance fails after a tenant exists

- **WHEN** the personal tenant exists but target identity, behavior, refresh, or reconnect acceptance fails
- **THEN** the upgrade stops without deleting, relabelling, downgrading, or mutating that tenant
- **AND** recovery requires a separately authorized action

### Requirement: Rollback is phase-aware and tenant preserving

Rollback SHALL choose the last safe action from the execution phase. Before any target cell exists, it MAY restore the prior platform default and expire target stages. A failed in-flight cell transition MAY return that cell to its recorded prior Helm revision before its routable observation moves. After any cell is recorded at the target, automatic rollback MUST NOT delete, relabel, or downgrade that cell; the system SHALL keep expand mode and the required runtime identities trusted until a separately authorized recovery or restore completes.

#### Scenario: Expand is reverted before target cells exist

- **WHEN** the target expand deployment fails acceptance and inventory proves no cell was created or moved to the target
- **THEN** the prior platform default may be restored and target stages may be expired without tenant mutation

#### Scenario: In-flight transition fails before recording

- **WHEN** a cell cannot reach or confirm the target before its routable observation changes
- **THEN** the atomic transition returns it to the recorded prior Helm revision and preserves the prior control-plane identity
- **AND** the fleet stops under expand mode

#### Scenario: Recorded target cell later proves unhealthy

- **WHEN** a cell already recorded at the target fails after the transition completed
- **THEN** automatic rollback preserves that cell, its volume, vault, binding, and target record
- **AND** an operator must invoke a separately governed recovery or restore path

### Requirement: Stranded private-alpha provisions may retarget forward in place

An explicitly authorized recovery MAY retarget a never-routed unfinished provision from a
reviewed source runtime to the selected forward runtime only after the signed helper proves both
the source and target deployment locks and
existing init-retry receipt, exact request and request hash, tenant fence, operation and provider
identity, four authenticated durable resources, one unreleased reservation, no result, no route,
no admitted runtime, two stable live observations, and no unexpired worker claim. The transaction
SHALL change only a v1 request's release/protocol pair or a v2 request's complete `runtimeTarget`,
encrypted request, canonical request hash,
claim fields, availability timestamp, and append-only content-free retarget receipt while
preserving every operation, tenant, cell, fence, resource, volume, credential, policy, and caller
checkpoint identity. Repetition SHALL verify the existing receipt without a second mutation.

#### Scenario: Exact stranded provision advances to the reviewed target

- **WHEN** the operator authorizes a recovered `volume-owned` provision with no route, result, admitted runtime, conflicting operation, or active claim
- **THEN** one compare-and-swap transaction retargets that same operation to the selected runtime and makes it claimable without replacing its cell or volume

#### Scenario: A retargeted stranded provision resumes through the declared migration

- **WHEN** the retargeted operation resumes while its never-routed source-runtime Helm release and retained volume still exist
- **THEN** the worker proves the runtime stopped, fingerprints the same vault, runs the deployment lock's declared migration, upgrades the existing release in place, proves the fingerprint unchanged and exact target health, and only then admits the runtime and opens routes

#### Scenario: A failed retarget resume is recovered once without weakening authorization

- **WHEN** that exact retargeted operation fails closed before tenant mutation because its source authorization bundle is stale
- **THEN** a separately invoked signed recovery validates both prior recovery receipts, the selected target request, the unchanged four-resource identity, active reservation, stable unrouted live state, and absence of conflicts before making the same operation claimable once; the offline migration may read the stale bundle only to render its non-serving Job, and serving still requires a freshly transitioned target-version bundle

#### Scenario: Offline migration replay identity is bound to the selected target request

- **WHEN** a recovered retarget runs its target-image migration after an earlier initialization proof exists for the same provider operation lineage
- **THEN** the migration initialization identity is derived from the durable operation and canonical selected-target request digest, so retries of that exact request are byte-stable and a different target request cannot collide with its proof

#### Scenario: A failed resumed retarget receives one separately attributed retry

- **WHEN** that resumed retarget fails closed with a provider-metadata conflict before migration while the same four resources remain stopped, unrouted, reserved, and conflict-free
- **THEN** a separately invoked signed retry validates the original recovery, retarget, and resume receipts plus the unchanged selected-target request and live identity before appending one content-free retry receipt and making the same operation claimable once more

#### Scenario: Retarget mismatch is a no-op

- **WHEN** any request, receipt, claim, fence, resource, reservation, live identity, runtime, route, result, or compare-and-swap invariant differs
- **THEN** recovery refuses without changing the operation, request, resource, reservation, PVC, PV, or provider volume
