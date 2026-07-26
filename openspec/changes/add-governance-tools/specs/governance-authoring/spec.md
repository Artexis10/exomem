# governance-authoring

## ADDED Requirements

### Requirement: Natural-language propose and validated commit

`govern_memory(operation="propose")` SHALL resolve a natural-language intent into
a deterministic proposal and return the plain-language interpretation, the
canonical policy it would write, the resolved affected membership, the
consequences, overlaps with existing rules, the duration, the reversal path, and a
single-use proposal id. The membership preview SHALL render each affected item at
its current effective disclosure ceiling and SHALL NOT leak titles or excerpts of
items that a not-yet-committed rule would restrict. `govern_memory(operation="commit")`
SHALL reserve the proposal id for one deterministic operation id, allow only that
event to retry, and mark it spent exactly once on successful activation. Exact-
prior abort SHALL release the reservation, so a crash before policy mutation
does not destroy the user's confirmed proposal. Commit SHALL refuse when the
policy fingerprint or any affected item's content fingerprint has changed since
propose time, writing nothing on refusal. On success it SHALL
write the policy files under the receipt-first activation protocol, archive prior
versions, and bump the policy fingerprint so the rule is enforced on the next
call only after durable terminal evidence exists.

#### Scenario: Propose does not leak restricted members

- **WHEN** a proposal would restrict N pages and the preview lists affected
  membership
- **THEN** counts and current-ceiling samples are returned, but no title or
  excerpt of a would-be-restricted page crosses the boundary

#### Scenario: Commit consumes the nonce once

- **WHEN** a proposal id is committed and then committed again
- **THEN** the first commit writes the policy and the second is refused as spent

#### Scenario: Commit refuses on drift

- **WHEN** the policy or an affected item changed between propose and commit
- **THEN** the commit refuses with a stale-policy error and writes nothing

#### Scenario: Crash after nonce reservation does not spend the proposal

- **WHEN** commit reserves the proposal but crashes while authoritative policy
  still matches exact prior
- **THEN** reconciliation aborts the operation and releases the reservation;
  another event cannot steal it, and the user may retry the proposal

### Requirement: Release-time grants and revocation

`govern_memory(operation="grant")` SHALL redeem a single-use escalation token from
a withheld notice to lift disclosure for a bounded audience, item set, level, and
duration, recording an ephemeral session grant that the evaluator applies only
after receipt-backed activation. The model SHALL only be able to redeem tokens
Exomem minted and SHALL NOT be able to author a grant broader than the token's
bounds. Token redemption and grant creation SHALL be distinct receipt events
linked by one causation id, but token consumption and creation of the exact
pending grant row SHALL commit atomically in one SQLite `BEGIN IMMEDIATE`
transaction after both intents are durable. The grant SHALL activate only after
both idempotent committed terminals exist.
`govern_memory(operation="revoke")` SHALL revoke a grant, and `revoke` scoped to
the session SHALL clear every grant authored in that conversation immediately.

#### Scenario: Allow this once

- **WHEN** a user says to allow a withheld item once and the model redeems its
  token
- **THEN** the item is disclosed at the token's bound level for the session, and a
  second use of the same token is refused

#### Scenario: Revoke the conversation's grants

- **WHEN** the user revokes everything authorised in the session
- **THEN** all session grants are cleared and the next query re-applies the
  standing policy

#### Scenario: Crash cannot consume approval without a recoverable grant

- **WHEN** a crash occurs between any token-redemption/grant-creation child
  receipt, the compound SQLite commit, either terminal, or activation
- **THEN** state is either exact prior with begun children aborted, or exact
  target with the consumed token and pending grant together; reconciliation
  finishes missing terminals and activates without redeeming the token again

### Requirement: Suspend, resume, undo with coherent dependents

`govern_memory` SHALL support suspending and resuming a whole rule-set and undoing
the last policy change by restoring its archived prior version. `undo` SHALL
re-resolve grants that depended on the restored version's selectors and SHALL
expire or flag any whose member set changed, so a restore never silently widens or
narrows a grant against a version it was not reviewed for. The operation's exact
target SHALL cover the restored YAML and every dependent-grant row; policy SHALL
NOT activate over a stale dependent grant.

#### Scenario: Undo restores and reconciles

- **WHEN** the last policy change is undone
- **THEN** the prior policy version is restored and any grant whose resolved
  members changed is expired or flagged for review

### Requirement: Read-only inspection operations

`govern_memory` `list`, `explain`, and `simulate` SHALL be read-only and SHALL NOT
write policy or state. `explain` SHALL show the effective policy for an item, scope,
or audience after layering, with the participating rule chain. `simulate` SHALL
dry-run a query or item release. Toward an audience below an item's ceiling, these
operations SHALL return counts and rule ids only, never titles or excerpts of
restricted items.

#### Scenario: Explain shows the effective chain

- **WHEN** `explain` is called for an item and audience
- **THEN** it returns the effective ceiling and the ordered participating rules
  without leaking restricted content

### Requirement: Enforcement is independent of the authoring tool

Release enforcement SHALL apply on every surface regardless of whether the
`govern_memory` authoring tool is exposed. Where the Tier-2 admin tool is disabled,
existing policy SHALL still be enforced.

#### Scenario: Enforcement without the admin tool

- **WHEN** the governance authoring tool is not exposed on a surface but policy
  exists
- **THEN** recall and reads on that surface still honor the disclosure decisions

### Requirement: Authorization changes are receipted before activation

Every authorization-affecting `govern_memory` operation — `commit`, `grant`,
`revoke`, `suspend`, `resume`, `undo`, and `declare` — SHALL append and fsync a
plaintext-free intent before changing state and SHALL append and fsync exactly
one terminal receipt per critical event. The operation SHALL compute
phase-domain-separated canonical prior, prepared, and final-active composite
digests covering every affected YAML path and authorization-state sidecar row,
including statuses, proposal consumption, and dependent grants. No final target
authorization state SHALL activate before its committed terminal evidence
exists.

Direction SHALL be computed from the resolved before/after effective disclosure
lattice over affected membership, audiences, purposes, and levels, never from
the operation name. Only a target proven pointwise no more permissive everywhere
is narrowing; incomplete proof is widening/unknown. After durable intent a
proven narrowing MAY install a separate fail-closed overlay, but the target still
activates last. Widening/unknown retains prior enforcement warm or BLOCKED cold.
`propose` SHALL be excluded because storing a pending nonce changes no
authorization state.

#### Scenario: Direction follows semantics, not operation name

- **WHEN** tests exercise widening and narrowing variants of `commit`, `suspend`,
  `resume`, `undo`, and `declare`
- **THEN** each is classified from its before/after lattice; every target
  activates only after terminal evidence, and only proven narrowing may apply an
  earlier fail-closed overlay

#### Scenario: Suspending a restrictive rule is widening

- **WHEN** suspending a rule would raise any affected ceiling
- **THEN** the prior restriction remains enforced until the committed receipt
  exists and activation completes

#### Scenario: Resuming a restrictive rule may fail closed early

- **WHEN** resuming a rule is proven pointwise no more permissive
- **THEN** a pending overlay may enforce the restriction after intent, while the
  target state itself remains pending until terminal evidence

#### Scenario: Every state-changing operation has a receipt mapping

- **WHEN** a new authorization-affecting operation is added to `govern_memory`
- **THEN** registry-derived coverage fails until the operation declares its
  receipt event and crash-recovery behavior

### Requirement: Pending YAML mutation blocks hybrid activation

Before replacing active policy documents, Exomem SHALL durably create a reserved,
plaintext-free pending marker containing the critical event id, operation,
the three composite digests, and affected relative paths/sidecar-row ids. An
authoritative pending operation row SHALL guard activation even if the marker was
already removed. Policy loading SHALL check any pending operation row before
the marker, compilation, or cache return; it SHALL retain last-good plus any
proven-narrowing overlay warm and return the existing BLOCKED L0 floor cold. It
SHALL NOT compile a partially replaced hybrid. With neither guard, direct manual
YAML edits SHALL retain their current behavior.

The marker and operation journal SHALL be control metadata excluded from logical
composite inputs; the journal stores the digests and cannot hash its own full
encoding. Reconcile SHALL validate it separately by event id, embedded digest
set, affected-id set, and phase. With journal phase `pending`, logical components
matching prior SHALL be exact prior and components matching prepared SHALL be
exact prepared; any mixture SHALL be partial. Final-active SHALL be the
phase-domain-separated after-image accepted by the activation transaction only
when all terminals exist. The pending journal SHALL distinguish prepared from
final-active for a YAML-only operation even when policy bytes are identical.
Proposal reservation SHALL live in journal control metadata and leave the
proposal row logically unspent until the activation transaction.

#### Scenario: Crash during multi-file policy commit does not activate a hybrid

- **WHEN** a crash occurs after only some target policy documents were replaced
- **THEN** the pending marker prevents those files from becoming active and the
  last-good policy or cold-start BLOCKED floor remains in effect

### Requirement: Restart reconciliation never replays or guesses

Before accepting another governance-authoring write, Exomem SHALL reconcile the
operation row, marker, and every YAML/sidecar component named by its three
composites. Exact prior SHALL append/recognize aborted terminals and clear
guards/reservations. Exact prepared with intact intent/chain SHALL append or
recognize all required committed child terminals and then activate idempotently.
Activation SHALL remove and directory-fsync the marker while the pending
operation row still blocks, then atomically change every prepared sidecar row to
its final encoding, verify the final-active after-image/terminal set, and mark
the journal `closed`. Exact-prior abort SHALL likewise close the journal after
clearing guards/reservations. Closed/aborted journals SHALL be retired from live
component comparison. Any mixed, partial, or other open state SHALL remain
blocked. Reconciliation SHALL NOT replay the requested semantic mutation.

#### Scenario: Crash in exact prepared state completes safely

- **WHEN** current state exactly matches the prepared composite but one or more
  terminal receipts were not appended before the crash
- **THEN** restart reconciliation appends the missing terminals, performs only
  the activation transition, and does not replay the semantic mutation

#### Scenario: Crash after terminal but before activation completes safely

- **WHEN** a committed receipt exists but a crash left the marker or session row
  pending
- **THEN** restart reconciliation activates the exact target once and does not
  append a duplicate terminal or reapply the mutation

#### Scenario: Third-state recovery blocks

- **WHEN** current state matches none of prior, prepared, or final-active, or
  mixes components from more than one composite
- **THEN** governance authoring remains blocked and reports manual repair rather
  than choosing a state

#### Scenario: Final-active without evidence is invalid

- **WHEN** components match final-active but any required terminal is absent
- **THEN** reconciliation blocks rather than inventing evidence after activation

### Requirement: Pending recovery state is pinned against TTL and GC

Every TTL or garbage-collection path SHALL exclude token, proposal, grant,
purpose, and dependent rows referenced by a pending operation journal. Logical
expiry MAY make a pending target non-authorizing, but physical deletion SHALL NOT
destroy an exact recovery composite. Activation or exact-prior abort SHALL close
the journal; closed journals SHALL not participate in later live composite
comparison, so ordinary expiry and state evolution may resume.

#### Scenario: Prepared grant survives expiry until reconciliation

- **WHEN** a crash leaves exact prepared token/grant state, time advances beyond
  its TTL, and a sweep runs before reconciliation
- **THEN** the referenced rows remain pinned, the expired grant authorizes
  nothing, and reconciliation can finish terminals/activation safely

#### Scenario: Expiry after activation does not reopen history

- **WHEN** activation closes its journal and a later sweep deletes an expired
  grant/token/purpose row
- **THEN** subsequent governance authoring ignores the historical closed
  composite and does not block

#### Scenario: Undo cannot activate over a stale dependent grant

- **WHEN** target policy YAML is present but any dependent-grant row does not
  match the target composite digest
- **THEN** reconciliation treats the operation as partial and keeps authoring and
  target activation blocked

### Requirement: Governance-tools migration is monotonic

`governance/store.py` SHALL exclusively migrate the prerequisite v2 sidecar to
v3 for proposals, pending operations, session grants, and purpose state. Every
opener SHALL preserve v3 or any later version and SHALL NOT reset
`PRAGMA user_version`.

#### Scenario: Existing opener cannot downgrade tools state

- **WHEN** any token, receipt, policy, or governance-tools path opens a v3
  sidecar
- **THEN** the schema remains v3 and migrations do not rerun
