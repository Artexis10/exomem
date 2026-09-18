# context-activation-continuity

## ADDED Requirements

### Requirement: Continuity token is client-carried and never resolves alone
`activate_context` SHALL return an opaque `continuity` token encoding the resolved
anchor refs, the selected roles, the vault identity, the role-registry hash and the
activation index generation of the packet, and SHALL accept it back as an optional
`continuity` argument. A valid token SHALL contribute only the `continuity` evidence
kind, a qualifier, to the anchors it names; it SHALL never create a candidate, SHALL
never satisfy the two-kinds rule on its own and SHALL never override a current-turn
`unresolved` outcome. A token whose vault identity, registry hash or index generation
does not match the serving state SHALL be ignored and reported under
`generation.continuity = "stale"`. The server SHALL keep no per-conversation state.

#### Scenario: Continuity strengthens but does not resolve
- **WHEN** a turn carries one contact kind for an anchor the previous packet resolved
  and passes that packet's token
- **THEN** the anchor resolves with evidence `[<contact kind>, continuity]`, and the
  same turn without the token yields `partial`

#### Scenario: Unresolved turn stays unresolved
- **WHEN** a turn reaches no anchor by any contact kind and passes a valid token
- **THEN** the packet abstains with `unresolved`, and the token's anchors are not
  injected

#### Scenario: Stale token is ignored
- **WHEN** a token was minted under an older index generation
- **THEN** the packet is built without it and reports `generation.continuity =
  "stale"`

### Requirement: Anchor override resolves ambiguity by the agent's choice
`activate_context` SHALL accept an optional `anchor` argument naming one canonical ref
returned in a previous packet's `ambiguity` block; the operation SHALL then treat that
anchor as `resolved` with evidence `[agent_choice]`, run the role lanes for it and
omit the competing anchors, and SHALL refuse with a structured error when the ref is
not an anchor in the activation index or is withheld for the caller's audience.

#### Scenario: Ambiguity resolved by choice
- **WHEN** a turn returned `ambiguous` between two hub anchors and the agent calls
  again with `anchor` set to one of them
- **THEN** the packet is `resolved` on that anchor alone with evidence
  `[agent_choice]`

#### Scenario: Withheld anchor refused
- **WHEN** `anchor` names a page the caller's audience may not see
- **THEN** the operation refuses without naming the page and without building a
  packet

### Requirement: Anchor decisions are recorded as a review family
The product SHALL register a `working_set` review family so an agent can record, per
packet, which anchors it accepted or rejected through the existing triage surface
(`exomem://review/working-set/<packet fingerprint>`), with the closed reason
vocabulary the review store already defines and a manual origin. The ledger SHALL
carry the packet fingerprint, the anchor refs, the evidence kinds and the decision,
SHALL emit no due-state counter, and SHALL never influence resolution, ranking,
retrieval or policy.

#### Scenario: Decision recorded without effect
- **WHEN** an agent dismisses an anchor of a packet with reason `false_positive`
- **THEN** the review store holds the decision with the packet fingerprint and
  evidence kinds, the next identical turn produces an identical packet, and no
  due-state category changes
