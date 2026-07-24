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
SHALL consume the proposal id exactly once from a dedicated durable store and
SHALL refuse when the policy fingerprint or any affected item's content fingerprint
has changed since propose time, writing nothing on refusal. On success it SHALL
write the policy files, archive prior versions, and bump the policy fingerprint so
the rule is enforced on the next call.

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

### Requirement: Release-time grants and revocation

`govern_memory(operation="grant")` SHALL redeem a single-use escalation token from
a withheld notice to lift disclosure for a bounded audience, item set, level, and
duration, recording an ephemeral session grant that the evaluator applies
immediately. The model SHALL only be able to redeem tokens Exomem minted and SHALL
NOT be able to author a grant broader than the token's bounds.
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

### Requirement: Suspend, resume, undo with coherent dependents

`govern_memory` SHALL support suspending and resuming a whole rule-set and undoing
the last policy change by restoring its archived prior version. `undo` SHALL
re-resolve grants that depended on the restored version's selectors and SHALL
expire or flag any whose member set changed, so a restore never silently widens or
narrows a grant against a version it was not reviewed for.

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
