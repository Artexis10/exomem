## ADDED Requirements

### Requirement: Relocation steps execute through a prepared-relocation protocol

This requirement replaces the v1 fail-closed refusal in
`add-governed-curation-lane` ("Relocation steps (`move`, `delete`, `recover`)
remain valid in the plan schema ... and is out of scope for v1"). Once the held
filesystem exposes durable monotonic parent-namespace generation tokens, a
relocation step SHALL NOT be refused before dispatch; it SHALL execute through
the protocol below, and every refusal SHALL be an outcome of that protocol
rather than a blanket pre-dispatch guard.

Before a `move`, `delete`, or `recover` namespace rename, the source and
destination parents SHALL already exist and be retained through held no-follow
handles; V1 curation placement MUST NOT create a knowledge or trash destination
directory as a precursor effect. After the required held-parent capability,
identity, generation, and durability preflight, the leaf SHALL first durably
publish one canonical create-only inert relocation candidate and then
one canonical create-only relocation authorization that binds the immutable
plan and approval digests to the candidate's complete canonical digest. The
candidate file, containing transition directory, and every newly created
governed-run ancestor entry SHALL be durable before the authorization is
published. The authorization file and containing transition directory SHALL be
durable before any rename. A candidate without its matching verified
authorization MUST NOT authorize an effect.

The candidate SHALL bind the run, plan, approval, ordinal, step, operation id,
source and destination, exact source hash/size/stable held-filesystem identity,
both parent identities and filesystem epoch, destination absence, complete
auxiliary-write manifest, governed lifecycle and graph-transition identities
where present, optional parent compensation identity, logical witness-time
basis, and the complete deterministic final-witness bytes and closed-terminal
projection including their digests.

`preparation_id` SHALL hash a domain separator and immutable candidate core.
`authorization_id` SHALL hash its own domain, approval digest, operation id, and
`preparation_id`. The final witness SHALL bind those ids, the logical witness-
time basis, and closed result digest. The candidate SHALL then bind the exact
final-witness bytes and their SHA-256. The authorization SHALL bind the approval
digest and approved plan membership, operation and preparation ids, SHA-256 of
the complete candidate bytes, final-witness SHA-256, and `authorization_id`.
The logical witness-time basis MUST NOT be represented as the physical rename
instant.

Before candidate publication, a missing destination parent SHALL refuse without
creating it. The held filesystem SHALL capability-probe and capture
durable monotonic namespace-generation tokens for both source and destination
parents. The tokens SHALL be restart-comparable, non-repeating within their
filesystem epoch, and SHALL change for every namespace-entry mutation including
rename followed by inverse rename. Unrelated entry mutation MAY conservatively
cause refusal. Both retained parents SHALL be durably flushed after token
capture and before candidate publication. Unsupported tokens or retained-parent
flush capability SHALL refuse before candidate publication, authorization, and
rename.

Immediately before rename the leaf SHALL revalidate its authorization, source
identity, destination absence, guards, and parent generations. After rename it
SHALL durably flush the retained source parent and then the retained destination
parent, or the shared parent once. A post-rename flush failure MUST NOT trigger
an inverse rename. Exact-target recovery SHALL repeat both idempotent parent
flushes before any auxiliary suffix. Its final witness SHALL reproduce the exact
bytes sealed by the candidate; the witness file and containing evidence
directory SHALL be durable before any terminal receipt. Exact target placement
without a final witness SHALL be adopted as the current authorized desired
placement without attributing its actor, recoverable by rolling forward only the
bound missing suffix, and MUST NOT invoke an executor rename. Exact prior
placement MAY reissue the same rename after a prior attempt only when both parent
generation tokens remain exactly at their prepared values and every guard still
matches, proving no durable namespace transition. The invariant is at most one
durable or adopted placement effect, not at most one rename syscall across power
loss. Any ambiguous placement,
identity substitution, competing or substituted transition record, missing
authorization after target placement, third-state auxiliary write, or
transition/witness/live mismatch SHALL block as
`CURATION_OUTCOME_UNCERTAIN`.
An advanced exact-prior generation, an unchanged exact-target generation, or an
unavailable, reset, non-monotonic, cross-epoch, or incomparable generation for
either placement SHALL
block as `CURATION_RENAME_HISTORY_UNPROVABLE`; no rename, rollback,
compensation, or automatic retry may proceed.

Every relocation final witness SHALL bind the run, plan, ordinal, step,
operation id, exact before/after target manifest, governed leaf identity, result
digest, and optional parent compensation identity. Relocation candidates and
relocation authorizations SHALL be create-only. The system MUST NOT claim
cross-file power-loss atomicity for a namespace rename and a separate witness
file.

#### Scenario: Process stops after an authorized rename commits

- **WHEN** a move, delete, or recover has a durable verified relocation
  authorization, its
  source is absent, its exact destination with the prepared stable identity is
  present, the parent generations are comparable in the prepared filesystem
  epoch and show a namespace transition, and its final witness or auxiliary
  suffix is incomplete
- **THEN** recovery rolls forward only the prepared missing suffix, witness, and
  receipt
- **AND** the operation has at most one durable or adopted placement effect and
  recovery issues no rename from exact-target placement

#### Scenario: Process stops between candidate and authorization

- **WHEN** a relocation candidate is durable but its authorization is absent
- **THEN** exact prior placement with unchanged parent generations may recreate
  only the deterministic authorization after every live guard is revalidated
- **AND** target or ambiguous placement blocks without rename or roll-forward

#### Scenario: Prepared relocation placement is ambiguous

- **WHEN** a relocation transition exists but both paths, neither path, a
  substituted identity, missing authorization after target placement, or an
  auxiliary third state is observed
- **THEN** the run blocks with `CURATION_OUTCOME_UNCERTAIN`
- **AND** no rename, rollback, retry, or compensation proceeds automatically

#### Scenario: Placement has unprovable rename history

- **WHEN** an exact-prior placement has a changed parent generation, an exact-
  target placement has an unchanged parent generation, or either placement has
  a reset, unavailable, non-monotonic, cross-epoch, or incomparable token
- **THEN** the run blocks with `CURATION_RENAME_HISTORY_UNPROVABLE`
- **AND** no rename, rollback, retry, or compensation proceeds automatically
