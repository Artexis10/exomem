## ADDED Requirements

### Requirement: Persistent machine-local state lives outside the vault

Every persistent vault-scoped machine-local family SHALL be classified as
`external-state` and live under a per-user, per-vault state root outside the
vault directory. That root SHALL resolve through a single code seam: an
absolute `EXOMEM_STATE_ROOT` environment override, else the platform state
directory. No external-state consumer SHALL compose the root itself. Vault
content such as notes, access policy and durable human-owned artifacts SHALL
remain `vault-canonical`.

Batch and held-publication intermediates SHALL be classified separately as
`target-adjacent`: they SHALL remain beside the publication destination for
same-volume atomic rename/link, SHALL exist only during an active publication
or bounded crash recovery, and SHALL NOT be treated as migratable persistent
state. After migration completes, no persistent machine-local state SHALL
remain under a quiescent vault.

#### Scenario: A synced quiescent vault carries no persistent machine-local state

- **WHEN** a vault that completed state migration has no publication in progress
- **THEN** no persistent index store, epoch record, receipt, lock, rebuild directory, projection, review record, or other external-state family exists under the vault for a file-sync agent to hash, hold, or replace

#### Scenario: Atomic publication scratch follows its target

- **WHEN** a batch or held publication stages a destination
- **THEN** its target-adjacent intermediate is created under the destination parent on the same volume
- **AND** it is cleaned after publication or surfaced through bounded crash recovery
- **AND** it is never migrated to the external state root merely because it is reserved

#### Scenario: The state root resolves through one seam

- **WHEN** any external-state consumer derives its persistent state path
- **THEN** the path resolves under the root returned by the single resolver
- **AND** setting an absolute `EXOMEM_STATE_ROOT` relocates every external-state consumer at once
- **AND** a relative `EXOMEM_STATE_ROOT` is rejected

#### Scenario: A pre-existing nested state root never gains admission

- **WHEN** an absolute state-root override resolves to the vault or one of its descendants, even if it already contains a structurally valid complete manifest
- **THEN** readiness and every external-state owner refuse before cache admission or state I/O
- **AND** neither the manifest nor pre-existing destination bytes can waive the outside-vault invariant

#### Scenario: Distinct vaults get distinct roots

- **WHEN** two vaults are served on the same machine
- **THEN** their state roots are distinct directories keyed by stable vault identity

#### Scenario: Placement inventory cannot be self-consistently incomplete

- **WHEN** a persistent operational family is introduced or described by a source constructor
- **THEN** a contract test requires it to have a reserved descriptor and explicit placement
- **AND** the constructor inventory and registry inventory are checked independently

### Requirement: State migration requires explicit offline authority and is never lossy

Before a service or stateful CLI consumer opens an external-state family, it
SHALL call a read-only readiness gate. That gate SHALL NOT copy or delete
family bytes, resume interrupted work, adopt an authority, or upgrade a
manifest. It SHALL refuse with the stable path-free
`STATE_MIGRATION_OFFLINE_REQUIRED` code when the manifest is absent,
in-progress, stale relative to the descriptor registry, complete while any
legacy duplicate remains, or carries an active or complete governance rollback
marker — with one bootstrap exception: when the manifest is absent, the vault
carries zero legacy external-state members, and the external root carries zero
state, the gate SHALL admit by creating the external root and durably writing
the first empty complete manifest under the migration lock, re-verifying both
emptiness proofs under that lock before writing. Because that lock is never
exclusion of an older writer, the bootstrap SHALL additionally fence the
manifest write: re-scan both authorities after publishing and durably roll the
manifest back if any state appeared, keeping the refusal. The bootstrap SHALL
acquire the migration lock with a bounded wait and refuse on contention rather
than block startup, and its own bookkeeping (manifest, lock file, manifest
staging temporaries) SHALL never count as external state. A refusal there
protects no bytes and instead fails first-run onboarding (a container boots
the server directly over a just-initialized vault). An uninspectable side is
not proof of absence and SHALL keep the refusal; every other manifest-absent
shape SHALL keep the refusal.

#### Scenario: A provably-fresh deployment admits without offline ceremony

- **WHEN** a service or stateful CLI starts over a vault with zero legacy external-state members, an external root holding no state (or no root at all), and no migration manifest
- **THEN** the readiness gate creates the external root and durably writes the first empty complete manifest under the migration lock
- **AND** startup admits and builds regenerable state fresh in the external root
- **AND** a legacy member, unexplained external state, or an uninspectable side observed before or under the lock keeps the `STATE_MIGRATION_OFFLINE_REQUIRED` refusal
- **AND** state that lands after the manifest write is fenced: the bootstrap re-scans both authorities, durably rolls the manifest back, and keeps the refusal

All state placement mutation SHALL require an explicit offline migration
authority and SHALL be reachable only through
`exomem maintain --migrate-state --offline` — the fresh bootstrap above is the
single exception, and it is not placement mutation: it moves, copies, and
deletes no family bytes, publishes only the first empty complete manifest over
proven emptiness, and un-publishes that manifest when the proof is invalidated.
Deployment SHALL prove every
legacy writer stopped independently of the migration lock; that lock serializes
only new migrators and SHALL NOT be treated as exclusion of an older writer.
The offline migrator SHALL durably record its versioned manifest and per-family
progress, resume an interrupted transition idempotently only on a later
explicit offline invocation, and never delete bytes it did not first copy,
verify, durably publish, and directory-flush. Unreadable source enumeration,
invalid manifests, and unexplained dual authority SHALL fail closed.

#### Scenario: Ordinary startup refuses without mutating legacy state

- **WHEN** a service or stateful CLI starts over a vault whose migration manifest is in-progress or stale, or absent while legacy in-vault members or external state exist
- **THEN** it refuses with `STATE_MIGRATION_OFFLINE_REQUIRED`
- **AND** it does not copy or unlink a source, resume progress, or rewrite the manifest
- **AND** it leaves no external root behind beyond, at worst, a refused bootstrap attempt's empty directory and released lock file, which never count as state

#### Scenario: A legacy WAL writer remains authoritative until the stop window

- **WHEN** an older process holds and writes an in-vault SQLite WAL without observing the new migration lock
- **THEN** ordinary target startup refuses without copying or unlinking that database
- **AND** a transaction committed by the older process after the refusal remains readable after it exits
- **AND** migration succeeds only on a later explicit offline invocation under asserted authority

#### Scenario: Explicit offline migration over an existing vault completes

- **WHEN** every legacy writer is proven stopped and the operator invokes the explicit offline migration over in-vault state with an empty destination
- **THEN** an in-progress manifest is durably stamped before bytes move
- **AND** the external-state families are moved to the external root and the manifest is marked complete
- **AND** a later read-only readiness check admits serving from the external root

#### Scenario: An interrupted offline migration resumes without loss

- **WHEN** an offline migration is interrupted after durably recording some verified families
- **THEN** ordinary startup refuses without resuming it
- **AND** a later explicit offline invocation recognizes the in-progress manifest and resumes the remaining families
- **AND** no family's bytes are deleted before destination contents and directory entries are moved, verified and durably flushed

#### Scenario: Unexplained dual state refuses rather than guesses

- **WHEN** source and destination contain the same external-state family without a valid in-progress or complete manifest establishing authority
- **THEN** the doctor reports a failure naming both paths and the remediation
- **AND** neither copy is silently preferred or deleted

#### Scenario: Completed migration leaves a later legacy duplicate

- **WHEN** a complete manifest establishes an external family and the same legacy family later appears in-vault
- **THEN** ordinary service and stateful CLI admission refuses with `STATE_MIGRATION_OFFLINE_REQUIRED`
- **AND** doctor reports the legacy duplicate as a failure
- **AND** the duplicate is neither merged nor deleted implicitly

#### Scenario: Governance rollback publishes D0, then commits and aligns D1

- **WHEN** an authenticated offline full downmigration or backup restore rolls
  governance back while every writer is stopped
- **THEN** it acquires state-migration, exclusive receipt-sequence,
  governance-store identity, and retained SQLite-transaction ownership in that
  order
- **AND** before COMMIT it computes `D0` from the actual uncommitted transaction
  only by serialize, deserialize into an isolated source, SQLite backup into a
  fresh snapshot, VACUUM, and canonical full digest
- **AND** it raw-hash-CAS writes a closed v2 `governance_rollback` marker into
  the ordinary v1 state manifest with immutable operation/event/plan/target,
  transform timestamp, `D0`, `D1`, terminal endpoint, deterministic
  predecessor/stage bindings, and `schema_fence_generation` as a positive
  integer or null, initially at `prepared`
- **AND** a backup restore binds operation-derived `backup_plan_digest` and
  `source_store_digest` as exact 64-hex values while a full downmigration binds
  both fields as null, so replay derives the result from the marker even when a
  backup artifact is unavailable
- **AND** it publishes only exact `D0` at the historical
  `Knowledge Base/.governance.sqlite` through the recorded `held_fs` stage
- **AND** it then commits and verifies the sole exact receipt-head terminal as
  `D1`, aligns the predecessor database to exact `D1`, advances the marker
  through `receipt-committed` and `legacy-aligned`, and advances the v3/G+1
  schema fence last

#### Scenario: Rollback publication crash replay accepts only recorded identities

- **WHEN** rollback crashes at any prepared marker, uncommitted transform,
  D0-stage/link/unlink, receipt-terminal, durable-terminal/head-lag,
  D1-alignment, or fence boundary
- **THEN** offline replay accepts only the recorded regular-file identities and
  exact phase evidence: absent predecessor plus exact single-link stage, the
  recorded two-link stage/predecessor residue, or exact single-link predecessor
- **AND** symlinks, non-regular files, different inode/file identities, digests,
  link counts, receipt suffixes, schema/keysets, or database mutations refuse
  without deleting the conflicting authority
- **AND** head-lag heals only from the exact durable terminal and its adjacent
  receipt chain, and `D1` normalizes back to `D0` by changing only the six
  recorded mutable active-head fields

#### Scenario: Fence-final replay cannot touch a live predecessor

- **WHEN** a durable `legacy-aligned` marker remains after a crash and its
  immutable fence binding is either null with no fence or a positive `G+1` with
  the schema fence exactly v3 at that recorded generation
- **THEN** the predecessor may write immediately
- **AND** replay reaches `complete` only by metadata CAS from that marker and
  fence evidence, verifying only the immutable marker, exact external `D1`, and
  the exact recorded no-fence or v3-at-`G+1` fence state
- **AND** it does not reopen, redigest, realign, re-prove legacy tail/adjacency,
  or require v4 custody of the legacy database

#### Scenario: Descriptor-scoped governance adoption restores relocated service

- **WHEN** predecessor operation is complete and an operator invokes explicit
  offline `--adopt-state governance-store=vault`
- **THEN** it migrates only the governance-store descriptor back to the external
  root, preserving every unrelated external family byte-identically
- **AND** it proves the predecessor receipt chain is a valid descendant anchored
  at recorded `D1`, rather than requiring that a live predecessor still equals
  `D1`
- **AND** it clears the rollback marker only after exact migration proof
- **AND** generic global-vault adoption is not accepted as this recovery path

#### Scenario: Adoption resumes after copy before its phase CAS

- **WHEN** descriptor-scoped governance adoption has a durable prepared record
  for digest `A`, and both the external governance store and legacy predecessor
  are exact regular-file `A` because copy committed before its phase CAS
- **THEN** replay accepts that exact `external=A, legacy=A` state, proceeds to
  durable legacy removal, and records its later phase without treating it as
  unexplained dual authority
- **AND** absent/exact, copied/exact, and removed/exact crash states converge
  only through their recorded descriptor identities; every mismatch refuses
- **AND** every unrelated external family remains byte-identical throughout

#### Scenario: Real predecessor acceptance and backup restore have identical proof

- **WHEN** the full downmigration path or backup-restore path reaches its fence
- **THEN** the actual old-v3 binary performs a receipt-bearing write against the
  legacy predecessor database, not merely startup or a no-op command
- **AND** both paths prove every rollback crash boundary and leave current
  relocated admission fenced until descriptor-scoped adoption completes

#### Scenario: A later release adds an external descriptor

- **WHEN** a complete manifest records an older descriptor set than the current registry
- **THEN** ordinary admission refuses with `STATE_MIGRATION_OFFLINE_REQUIRED`
- **AND** only an explicit offline invocation transactionally migrates each newly external descriptor before updating the recorded set
- **AND** destination bytes for a newly external descriptor that are not established by the older manifest or an exact matching legacy source refuse as unexplained authority until explicitly adopted
- **AND** no consumer opens the new family before that offline upgrade completes

#### Scenario: Tree deletion is durable before family completion

- **WHEN** a tree-family migration removes the last legacy file and directory and crashes before recording that family complete
- **THEN** every removed directory entry has been fenced by flushing its affected parent before completion can be published
- **AND** a later explicit offline invocation safely converges any empty legacy directory resurrected by the crash instead of demanding adoption

#### Scenario: An adopted vault regenerates on a new machine

- **WHEN** a vault is moved to a machine that has no state root for it
- **THEN** the explicit offline initialization writes a complete empty manifest, or the readiness gate's fresh-deployment bootstrap writes the same manifest when startup arrives first over the proven-empty pair
- **AND** the admitted system builds fresh state from vault content for every regenerable family without requiring the old machine's runtime state

### Requirement: Deployment proves an offline writer-free transition

Desktop deployment SHALL use one ordered transition: before stop, atomically
persist outside the vault a durable receipt binding the selected service
identity, configured port, exact sticky state root, phase, and complete captured
worker/listener PID set; then stop, prove the service manager stopped, every
captured PID absent, and the configured listener unbound; persist one absolute
`EXOMEM_STATE_ROOT` binding for both the managed service and operator-run
migration, install the target, run the target interpreter's offline migration,
run doctor, start, and prove the configured listener belongs to the new selected
worker and its live health version equals the target interpreter version. A
post-stop failure SHALL leave the service stopped and the transition SHALL NOT
permit a staged no-restart outcome. Recovery from that state SHALL require an
explicit stopped-transition resume that loads the exact receipt and re-proves
the selected manager stopped, every captured PID absent, the listener unbound,
and the same sticky root before rolling forward. A bare resume flag SHALL NOT be
authority. The receipt SHALL be cleared only after full live acceptance.
After a failed target start, cleanup SHALL NOT change a pre-accepted,
non-resumable `starting` or `started` receipt to resumable `failed` until it has
durably appended the selected target worker and every attributable listener PID
on both the original and target ports. Hidden, ambiguous or unavailable
enumeration, or failure to durably publish the expanded proof set, SHALL retain
the same pre-accepted phase so resume fails closed.

Hosted deployment SHALL close and drain both routes, prove zero tenant runtime
pods, and run the migration Job from the target image. That Job SHALL hold the
existing hosted lifetime lock while the offline migrator holds its migration
lock. After successful state migration, recovery SHALL keep routes closed and
move forward on the target image; it SHALL NOT restore an old image.

#### Scenario: Desktop target installation and migration are one transaction

- **WHEN** an installed Windows service is upgraded across the state-root transition
- **THEN** its exact pre-stop worker and configured-listener PID set is recorded before stop and every captured PID is proven absent after SCM reaches `Stopped`
- **AND** the configured listener is proven unbound before target installation or migration
- **AND** the installer/operator platform state root is pinned once for both target CLI and LocalSystem startup
- **AND** target install precedes offline migration and doctor, which precede start, new worker/listener ownership proof, and exact target-version proof
- **AND** any failure after stop leaves the service stopped

#### Scenario: POSIX deployment proves the selected service and listener stopped

- **WHEN** a systemd or launchd service is upgraded across the state-root transition
- **THEN** every worker, stop, start, wait, and cleanup observation uses the selected unit identity
- **AND** a visible occupied listener whose PID cannot be attributed is not treated as unbound
- **AND** one sticky absolute state root is persisted after stop proof and before package replacement
- **AND** success requires a new live worker whose health version equals the target interpreter version
- **AND** a failed transition can resume only with its exact durable receipt after every recorded PID, selected manager, listener and root are re-proven

#### Scenario: A detached writer cannot escape a failed transition receipt

- **WHEN** a captured listener process closes its socket after a failed transition but remains alive long enough to commit to a legacy SQLite database
- **THEN** stopped-transition resume refuses while that captured PID remains alive even though the manager is stopped and the listener is unbound
- **AND** the process can finish its legacy commit without migration unlinking the database beneath it

#### Scenario: Failed target startup cannot omit a new listener

- **WHEN** a failed target start creates a listener process that later closes its socket but remains alive as a legacy SQLite writer
- **THEN** cleanup durably adds that listener PID to the transition receipt before marking the transition resumable
- **AND** resume refuses until that PID is absent even though both configured ports are unbound
- **AND** if cleanup cannot prove or publish the complete listener set, the receipt remains `starting` and cannot authorize resume

#### Scenario: Hosted target-image migration excludes every runtime pod

- **WHEN** a hosted cell rolls forward across the state-root transition
- **THEN** both routes are closed and drained and a fresh provider observation proves zero tenant runtime pods
- **AND** the target-image migration Job holds the hosted lifetime lock and the state-migration lock in that order
- **AND** a later failure never restores the old runtime image against migrated state

### Requirement: Relocation preserves privacy, hosted isolation, and portability

The unauthenticated health surface SHALL report placement and migration status
without returning an absolute state-root path or other host-identifying path.
Local or authenticated doctor output SHALL report the detailed root and
conflicts. Hosted startup SHALL bind `EXOMEM_STATE_ROOT` beneath the existing
private `EXOMEM_HOSTED_STATE_ROOT` and SHALL fail closed when private-root
preparation fails, while preserving the independent
`EXOMEM_WRITER_LEASE_STATE_DIR` contract. External families classified as
portable-derived SHALL remain included in export and restore.

#### Scenario: Public health is path-free

- **WHEN** an unauthenticated client reads `/health`
- **THEN** it sees placement and migration status
- **AND** it does not receive the absolute state-root, vault, checkout, interpreter, or host path

#### Scenario: Hosted state stays within the cell binding

- **WHEN** a hosted cell binds its environment
- **THEN** `EXOMEM_STATE_ROOT` resolves beneath its private hosted state root
- **AND** the writer-lease state-root setting retains its existing value and precedence
- **AND** failure to establish the private root stops startup instead of creating an unprotected directory

#### Scenario: Portable-derived external state survives export and restore

- **WHEN** a hosted portability export includes graph receipts, review decisions, or another external `PORTABLE_DERIVED` family
- **THEN** export reads that family from the external state root
- **AND** the schema-v1 archive retains its logical path and portability classification without encoding a physical target state root
- **AND** restore preserves its governed portability semantics on the target cell

#### Scenario: Restore relocates portable-derived state before readiness

- **WHEN** a hosted restore publishes a verified archive, including an archive with no portable-derived members or a deployment lock with `migrationMode=none`
- **THEN** restore binds the exact request target and invokes the offline migrator while retaining the hosted lifetime lock
- **AND** it writes a complete current state manifest and records its digest plus the target placement identity in the durable `state_migrated` journal phase
- **AND** canonical archive bytes exist only in the vault while portable-derived archive bytes exist only in the external state leaf with their exact size and digest
- **AND** replay from `canonical_published` resumes absent, partial, or already-complete relocation idempotently
- **AND** placement-aware repair cannot recreate a portable-derived legacy path in the vault
- **AND** promotion remains forbidden until the restore journal is `complete` and the existing READY terminal is present
