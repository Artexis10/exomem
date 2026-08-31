## ADDED Requirements

### Requirement: Consolidation enrollment is explicit and offline

An exactly absent consolidation-seal subtree SHALL denote a legacy vault that
is not enrolled, not an inferred `open` state. Enrollment SHALL be an explicit
owner-only pre-start operation over an already authenticated cell identity. It
SHALL exclude every server and direct-CLI runtime for that vault, create the
immutable revision-0 `open` snapshot and active pointer, and reload their exact
identity binding before the runtime may advertise readiness. Hosted SHALL use
its cell lifetime exclusion; local SHALL use process-safe vault presence slots
that allow concurrent readers but block new registrations during enrollment.
Ordinary dispatch, readiness, and admission SHALL NOT initialize a seal or
adopt identity. Any seal-path presence other than a complete authenticated
store SHALL fail closed.

#### Scenario: Enrollment is requested while a runtime is live

- **WHEN** a Hosted cell, local server, or direct CLI invocation holds runtime presence for the destination
- **THEN** enrollment refuses before identity or seal publication
- **AND** ordinary traffic continues under the unchanged legacy or enrolled state

#### Scenario: Enrollment crashes after the initial snapshot

- **WHEN** revision 0 is durable but its active pointer is absent after restart
- **THEN** ordinary admission and readiness fail closed
- **AND** only an exact explicit enrollment retry may complete that pointer without adopting a different identity or timestamp

### Requirement: A durable consolidation seal composes with the shared vault boundary

The destination-wide consolidation seal SHALL be keyed by canonical vault
identity and SHALL compose with the existing lifecycle and process-safe mutation
boundaries. Seal acquisition SHALL first obtain exclusive lifecycle/writer
authority, persist the sealing intent, stop admission of new commands,
transfers, and background writers, drain already-admitted readers and writers,
then persist `sealed` before any policy or content publication. Failure to drain
within the configured bound SHALL fail before publication and SHALL leave a
recoverable durable sealing state.

While sealing, sealed, publishing, verifying, aborting, rolling back, or
recovering, the boundary SHALL reject every new ordinary content read and
mutation for that destination, including owner reads and writes. Unrelated
canonical vault identities SHALL remain independent. The seal marker and
operation phase SHALL be loaded and reconciled before server readiness or any
ordinary command admission after restart. Missing, malformed, conflicting, or
indeterminate nonterminal state SHALL fail closed.

Lifecycle state SHALL use a typed effective union: `open`, irreversible
`deletion-sealed(checkpoint)`, or
`consolidation-sealed(vault_id,run_id,operation_id,phase,journal_digest)`.
Deletion seal SHALL dominate and SHALL never be removed by generic resume,
consolidation recovery, or consolidation unseal. Only the exact bound
consolidation journal MAY advance/remove its seal; stale/foreign run or operation
ids fail. Export/quiescence SHALL compose without erasing either seal kind.

#### Scenario: Read and upload arrive during seal acquisition

- **WHEN** consolidation has persisted sealing intent while an admitted read is draining and a new upload arrives
- **THEN** publication waits for the admitted read and the new upload is rejected before commit
- **AND** `sealed` is persisted only after all pre-seal participants have left the shared boundary

#### Scenario: Process restarts during publication

- **WHEN** startup discovers a nonterminal consolidation seal or a journal state it cannot reconcile exactly
- **THEN** ordinary reads, mutations, transfers, and background writers remain unavailable for that vault
- **AND** only the closed owner-control action set defined below is reachable, with `status`/`recover` available whenever applicable and no ordinary product admission

#### Scenario: Another vault is active

- **WHEN** vault A is sealed for consolidation while vault B has a different canonical identity
- **THEN** vault B continues to use its own read/write boundary normally
- **AND** vault A's run identity, phase, or lock facts are not exposed to vault B

#### Scenario: Consolidation recovery sees a deletion seal

- **WHEN** an owner invokes consolidation recover or a generic resume against a deletion-sealed cell
- **THEN** the irreversible deletion checkpoint remains effective and no consolidation authority is constructed
- **AND** no ordinary or control command reopens the cell

### Requirement: Lifecycle-changing consolidation uses owner-control mutation admission

`apply`, identical apply resume, `verify`, `recover`, `abort`, and `rollback`
SHALL NOT execute inside Hosted's ordinary full-leaf `admit_mutation()` wrapper.
They SHALL use a control-mutation lane that authenticates the exact owner context
before run/body-dependent lookup, atomically reserves operation id/JTI/idempotency
and exclusive writer/lifecycle authority, then compare-and-swap converts exactly
its own admission from the ordinary active-mutation counter into a journal-bound
control participant before seal drain. No other reader, mutation, transfer, or
background writer is excluded. Internal batches execute under the seal and
phase/action-bound authority. Registering a lifecycle-changing action through
the ordinary full-leaf wrapper SHALL fail coverage/startup because it would wait
for its own active count.

After sealed restart, generic dispatch SHALL remain closed, while an explicit
private owner-control lane SHALL admit only `status`, `recover`, `abort`,
`rollback`, `verify`, and a necessary identical `apply` resume. It SHALL bypass
only lifecycle/outer-seal admission, verify owner before run existence, match
exact vault/run/operation/journal, and construct the narrow in-process authority.
It SHALL not start a new apply, admit other actions, bypass governance, or cross
a deletion seal. Conversion/reservation and recovery SHALL be journaled so a
crash cannot leave the operation both counted and excluded or neither owned nor
recoverable.

The owner-control rollback request SHALL be the closed tagged union
`rollback_mode=nonterminal-contingency|terminal-plan`. The
common branch SHALL require the opaque `successor_context_ref` and digest.
`nonterminal-contingency` SHALL resolve that context, under fresh owner control,
to the original apply operation id, sealed apply-journal digest, cutover-plan
and contingency digests, expected publication-state digest, exact current
control predecessor id/digest, deadline, and apply-reserved contingency
authority ref/digest; all those protected fields and terminal rollback plan/
token fields SHALL be forbidden in its body. `terminal-plan` SHALL require the
separately rendered and approved rollback-plan digest/token and resolve its
approval predecessor from the matching context; it SHALL forbid original-apply
and contingency fields. Each branch SHALL reserve its
explicit operation id and SHALL reject a retry that changes the mode or any
bound field. An eligible nonterminal branch remains reachable after restart
without widening generic dispatch; a terminal branch remains unreachable until
its own human review and token reservation have committed.

#### Scenario: Apply seals without waiting on itself

- **WHEN** Hosted v5 admits apply as a control mutation and begins seal drain
- **THEN** its own exact operation has been converted out of the ordinary active counter while all other participants must drain
- **AND** apply cannot deadlock on `active_mutations == 0` or exclude another mutation

#### Scenario: Restarted sealed cell reaches recovery

- **WHEN** generic dispatch is closed after a crash in publication and the owner invokes recover with the exact operation/journal
- **THEN** the owner-control lane reaches the leaf and reconstructs only its phase-bound authority
- **AND** normal reads/mutations remain sealed and a non-owner learns no run existence

#### Scenario: Restarted sealed apply reaches its reserved contingency

- **WHEN** an owner submits `rollback_mode=nonterminal-contingency` after a publication crash with the current opaque context that resolves to the exact apply journal, contingency authority, revision, publication state, and control predecessor
- **THEN** the owner-control lane reaches the sealed run without requiring a terminal rollback token and executes only the pre-approved contingency
- **AND** a terminal-plan or caller-supplied protected field, changed operation id, stale context/predecessor, or mismatched journal fails before restoration

#### Scenario: Completed cutover requires terminal rollback review

- **WHEN** an owner submits rollback after the run has reached a terminal cutover state
- **THEN** only `rollback_mode=terminal-plan` with the exact separately rendered plan and reserved rollback token is admitted
- **AND** the earlier nonterminal contingency authority cannot authorize that rollback

### Requirement: Consolidation authority is narrow, in-process, and phase-bound

Only trusted consolidation control code MAY construct
`ConsolidationAuthority`, and only after verifying the destination vault id,
run id, operation journal, current seal phase, and permitted action. The
capability SHALL be an unforgeable in-process value; it SHALL NOT serialize to
run state, logs, receipts, REST/MCP/CLI fields, Hosted claims, or retry stores.
Caller content SHALL never be deserialized or coerced into that capability.

The capability MAY bypass the outer seal only for the exact reserved-run read,
private-artifact access, approved policy/content batch, preimage restoration, or
named verification probe allowed in its phase. Every internal write SHALL still
hold the shared mutation boundary and use the existing transactional writer.
Every pre-unseal verification probe SHALL remain in-process and still traverse
ordinary authentication-context resolution, governance, projection, scrubbing,
response serialization, and evidence code after crossing the seal. The
authority SHALL never cross or be reconstructed from MCP, REST, CLI, Hosted, or
retry serialization. Disposable/clone post-unseal transport parity SHALL use
normal authentication and remain supplemental release evidence. A real cutover
SHALL additionally use the exact-cell `transport-verifying` phase: public
routing is durably stopped/drained, trusted control temporarily suspends only
that operation's consolidation seal, and normal-auth black-box adapters run
without any serialized authority before routing may open.

#### Scenario: Request supplies a capability-shaped value

- **WHEN** MCP, REST, CLI, Hosted, a retry record, or persisted JSON includes fields shaped like `ConsolidationAuthority`
- **THEN** admission rejects or treats them as ordinary untrusted data
- **AND** no sealed content or internal mutation path becomes reachable

#### Scenario: Authority is used in the wrong phase

- **WHEN** a valid in-process authority bound to verification is presented to a policy, content-publication, rollback, or unseal operation
- **THEN** the action fails before reading private bytes or changing state
- **AND** the destination remains sealed under its existing journal phase

### Requirement: Publication uses policy-first exact journaled batches

The sealed coordinator SHALL verify the complete destination preimage before
activating the exact approved restrictive policy through the existing
governance transaction journal and critical-receipt protocol. It SHALL not admit
the first content batch until policy activation reaches its exact committed
terminal. Content SHALL then publish in the approved deterministic partition of
bounded `batch_atomic_write` batches while exclusive destination authority is
held.

Each phase and batch SHALL durably bind operation identity, request digest,
prior fingerprint, prepared fingerprint, final fingerprint, exact action-set
digest, and receipt/journal terminals. The engine SHALL claim atomicity only
within the existing bounded batch; the overall consolidation SHALL be described
and recovered as a saga. Exact-current-state classification SHALL make an
identical retry idempotent. A changed run, artifact, plan/token, action set,
batch partition, principal/session, destination binding, or mutation identity
SHALL conflict rather than reuse prior work.

The approved cutover plan SHALL bind an immutable `control_basis_digest` and a
closed plan-successor automaton. At apply, the coordinator SHALL resolve the
owner-only successor-context pair returned by approval and separately
revalidate the exact current apply-predecessor event id/digest produced by the
allowed same-run rendering, acknowledgement, approval, and token-reservation
path. It SHALL NOT require the mutable physical receipt head or the complete
run-control tree to equal their values at plan materialization. An unexpected
same-run control event, missing or reordered declared predecessor, or replayed
successor SHALL stale the plan; an unrelated correctly chained receipt MAY
advance the physical head without staling it.

#### Scenario: Policy activation fails before content

- **WHEN** restrictive policy preparation or activation cannot reach its approved exact terminal
- **THEN** no content batch is invoked and the destination stays sealed
- **AND** recovery classifies the governance journal before allowing abort or forward completion

#### Scenario: Acknowledgement is lost after a content batch commits

- **WHEN** exact final files exist but the client did not receive the batch terminal
- **THEN** identical retry classifies the final fingerprint, completes any missing receipt/journal terminal, and advances without invoking the batch again
- **AND** a retry with changed bytes or arguments conflicts

#### Scenario: Crash leaves mixed batch files

- **WHEN** current canonical files match neither the recorded prior, prepared, nor final batch fingerprint
- **THEN** automatic retry and unseal are refused
- **AND** the owner receives bounded repair facts while every ordinary surface remains sealed

#### Scenario: Apply follows the exact approved successor path

- **WHEN** the plan basis remains an ancestor and apply's context resolves to the current approval predecessor on the declared render/ack path, after which token reservation commits
- **THEN** apply may proceed even though unrelated receipts advanced the physical local chain head
- **AND** it binds the resulting token-reservation predecessor in its journal and first `seal-intent`, without accepting that protected value in its body

#### Scenario: An unexpected run event intervenes

- **WHEN** a duplicate, reordered, skipped, or unlisted same-run control event appears between plan materialization and apply
- **THEN** the closed successor automaton rejects the apply as stale
- **AND** equality of the original plan bytes or a later global receipt head cannot authorize it

### Requirement: Preimage restoration and unseal are census-gated

Before policy publication, the coordinator SHALL content-address and verify a
complete destination preimage in private artifact storage while holding the
seal. Abort before the first committed content batch and rollback after that
boundary SHALL restore through journaled bounded transactions while the same
destination remains sealed. Canonical derivatives SHALL be rebuilt after
restore; source or preimage derived databases SHALL not be installed directly.

Final unseal/public reopening after apply, abort, or rollback SHALL require the exact expected
canonical census, policy/access/review fingerprints, required derived readiness,
mandatory probe terminals, and append-only critical evidence. For a completed
real cutover it SHALL additionally require the bound exact-cell transport
terminal and routing-open journal; the temporary seal suspension used for
transport verification SHALL be phase-bound, occur only while ingress/routing is
stopped, and SHALL not count as generic unseal. For a completed cutover that was
previously unsealed, every rollback SHALL first materialize and separately
approve the exact rollback plan. If the current full canonical census differs
from the plan basis, including any post-cutover write, execution SHALL yield
`ROLLBACK_RECONCILIATION_REQUIRED`; the restore writer SHALL not start until a
new exact union reconciliation and treatments are separately rendered and
approved.
Governance, consolidation, and mutation receipt history SHALL be appended to,
never restored backward from the preimage or deleted.

#### Scenario: Abort restores the exact pre-publication state

- **WHEN** abort runs before the first content batch and policy had already activated
- **THEN** the exact prior policy/canonical state is restored and verified under the seal
- **AND** unseal occurs only after the prior census matches and aborted evidence is durable

#### Scenario: Automatic rollback sees post-cutover drift

- **WHEN** a destination that was unsealed after cutover contains any later canonical write
- **THEN** rollback refuses before a restore batch starts and records the need for reconciliation
- **AND** no later write is overwritten or silently omitted from the new review

#### Scenario: Restore attempts to rewind receipts

- **WHEN** a preimage contains earlier receipt state or excludes later cutover evidence
- **THEN** restore does not replace, truncate, or delete the live append-only receipt history
- **AND** rollback appends its own intent, phase, and terminal evidence

#### Scenario: Transport verification crashes after seal suspension

- **WHEN** the exact-cell probe process crashes while public routing remains stopped but the consolidation seal was temporarily suspended
- **THEN** startup does not admit ordinary traffic and deterministically re-establishes that operation's typed consolidation seal or owner-only recovery
- **AND** generic resume cannot open the cell or alter any deletion seal
