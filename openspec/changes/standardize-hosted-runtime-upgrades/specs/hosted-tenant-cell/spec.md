## ADDED Requirements

### Requirement: Cell rollforward preserves tenant identity and canonical data

An explicit rollforward operation SHALL move one existing hosted cell to a newer authorized runtime in place without changing its cell identity, tenant binding, persistent volume, canonical vault bytes, security authority, credential versions, OAuth grants, entitlement, or lifecycle generation lineage. The operation MUST NOT provision a successor cell or use tenant deletion as an upgrade step. Routing SHALL fail closed only for that cell from quiescence until verified readiness.

#### Scenario: Bound cell moves to the target

- **WHEN** a rollforward completes for a bound ready cell
- **THEN** the same cell identity, tenant binding, and volume serve the authorized target
- **AND** all canonical vault files present before quiescence are byte-identical afterwards

#### Scenario: Cell is mid-rollforward

- **WHEN** a cell is quiescing, migrating, transitioning, confirming, recording, or recovering
- **THEN** requests for that cell receive a stable not-ready response and no alternate tenant cell is tried
- **AND** unrelated tenant cells remain serviceable

#### Scenario: Upgrade attempts to replace or delete the tenant

- **WHEN** a rollforward plan selects a successor cell, replacement volume, tenant destruction, binding rewrite, or entitlement change
- **THEN** it is refused before the selected tenant resource is mutated

### Requirement: Cell rollforward is operator-authorized, forward-only, and replay safe

The target release, protocol, profile, command fingerprint, schema digest, and compatibility digest SHALL come from an operator-authorized rollout assignment and SHALL be carried through one leased, checkpointed lifecycle operation. The target MUST be newer than the cell's recorded release. Durable checkpoints and idempotency keys SHALL let a reconciler resume without replaying a completed migration, runtime transition, observation write, or other destructive step.

#### Scenario: Authorized forward target is accepted

- **WHEN** a rollforward operation references the cell's active assignment and the exact target from the deployment lock
- **THEN** the provisioner may begin the fenced transition from the recorded legacy release

#### Scenario: Target is older or unauthorized

- **WHEN** a rollforward names an older release, a target absent from the deployment lock, a mismatched assignment, or a changed digest set
- **THEN** it fails before migration or runtime mutation with a stable content-free code

#### Scenario: Reconciler resumes after an external step

- **WHEN** a lease expires after migration, runtime transition, confirmation, or observation recording completed
- **THEN** the next owner verifies the checkpoint and continues without performing that completed step again

### Requirement: Declared privileged migrations are bounded and separate from serving

When the authorized target declares a privileged tree migration, rollforward SHALL run it as a dedicated bounded TTL job before the serving runtime moves. The job SHALL receive only the existing minimal filesystem capability set required by initialization. The serving container MUST remain non-root and MUST NOT inherit migration capabilities. A target declaring no migration MUST NOT render a privileged job.

#### Scenario: Target declares a migration

- **WHEN** signed target metadata declares a privileged vault-tree migration
- **THEN** the bounded migration job succeeds before the serving pod transitions
- **AND** the serving container's user and capabilities remain unchanged

#### Scenario: Migration fails

- **WHEN** the declared job fails, exceeds its bound, or cannot prove the expected cell and volume identity
- **THEN** the serving runtime remains on its prior release
- **AND** the operation fails terminal without changing the routable contract observation

#### Scenario: Target declares no migration

- **WHEN** the target metadata contains no privileged migration
- **THEN** no root-capable migration job is rendered

### Requirement: Authorized intent is confirmed before runtime identity moves

After the runtime transition, the operation SHALL read private authenticated readiness and require exact equality with the authorized release, protocol, profile, command fingerprint, schema digest, and compatibility digest. It SHALL also prove the post-transition canonical-vault fingerprint equals the quiesced pre-transition fingerprint except for an explicit bounded set of rebuildable derived indexes. Both fingerprints SHALL be produced by one fixed no-argument command from the immutable provisioner image, with only the canonical vault mounted read-only under an exact restricted admission contract; the selected tenant runtime MUST NOT be required to contain an upgrade-only evidence command. The provisioner fingerprint classification SHALL remain parity-tested against the runtime portability contract. Only then MAY it update the routable observation for the same cell identity and restore routing. The cell's report MAY veto a transition but MUST NOT originate trusted identity.

#### Scenario: Cell and vault confirm the authorized target

- **WHEN** private readiness exactly matches the operation target and canonical vault preservation verifies
- **THEN** the routable observation for the same cell identity is updated idempotently to the target
- **AND** routing resumes only after desired state and readiness are green

#### Scenario: Cell advertises another identity

- **WHEN** any advertised release, protocol, profile, fingerprint, or digest differs from the authorized target
- **THEN** no target observation is recorded on the strength of the cell's claim
- **AND** the transition fails and invokes the bounded pre-record recovery path

#### Scenario: Canonical vault bytes differ

- **WHEN** any canonical vault file differs or disappears outside the declared rebuildable derived-index set
- **THEN** the operation fails closed, records preservation failure without content, and does not mark rollforward complete

### Requirement: Cell rollforward failure has bounded recovery semantics

A migration or runtime failure before target observation SHALL preserve the prior control-plane identity and return the Helm release to its recorded prior revision when a transition occurred. A failure after target observation SHALL stop automatic progression and require a separately authorized recovery or restore; it MUST NOT use reverse rollforward, silent relabelling, tenant deletion, or unobserved Helm mutation.

#### Scenario: Runtime transition never becomes ready

- **WHEN** the target pod fails the bounded atomic wait before target observation is written
- **THEN** Helm returns the cell to its prior revision and the prior routable identity remains authoritative

#### Scenario: Failure occurs after target observation

- **WHEN** the recorded target cell becomes unhealthy after rollforward completed
- **THEN** the cell and vault remain intact and the system does not automatically downgrade or relabel them
- **AND** an operator must select an explicit recovery or restore operation

### Requirement: Destroyed cells cannot remain routable

When an explicitly authorized tenant-destroy operation reaches its terminal destroyed checkpoint, the control plane SHALL clear that cell's routable contract observation in the same fenced lifecycle transition. Runtime upgrade and reviewer promotion inventory MUST treat a routable observation for an absent destroyed cell as blocking inconsistent state.

#### Scenario: Tenant destruction completes

- **WHEN** a tenant's authorized destroy operation reaches its terminal checkpoint
- **THEN** the destroyed cell is absent from the routable set

#### Scenario: Destroyed-cell ghost is discovered

- **WHEN** preflight finds a routable observation whose tenant binding and cell workload are terminal or absent
- **THEN** upgrade and promotion stop until an explicit repair clears the inconsistency
