## MODIFIED Requirements

### Requirement: Continuity token is client-carried and never resolves alone
`activate_context` SHALL return an opaque `continuity` token minted from the packet as
served after the egress guard, encoding the refs of the anchors that packet RESOLVED
(never the refs of anchors it only listed as `partial`), the selected roles,
the activation index identity (the sidecar's own identity token, which differs per
vault and per rebuilt sidecar), the role-registry hash and the index generation, and
SHALL accept it back as an optional `continuity` argument. A token SHALL never encode
a ref the guard removed. A valid token SHALL contribute the `continuity` evidence
kind, a qualifier, to the anchors it names that still exist in the current index;
that kind SHALL never create a candidate, SHALL never resolve an anchor on its own or
together with qualifiers only, and SHALL never override a current-turn `unresolved`
outcome. An anchor carrying `continuity` together with at least one contact kind of
either family SHALL resolve. This is a deliberate exception to the rule that retrieved
contact alone is at most `partial`: the token names only anchors an earlier turn of the
same conversation resolved on sound evidence, so a follow-up turn that reaches such an
anchor again, even by recall alone, is continuing a subject rather than discovering
one. The token's anchors SHALL also be the first tier of the recency hot profile
(memory-loop: "A turn that names nothing MAY resolve to the hottest recent anchor"), so
on a REFERENTIAL turn that names nothing they MAY supply the referent and resolve on
`recency`; on any other turn the token SHALL only qualify anchors the turn reached. A
token whose index identity
or registry hash does not match the serving state, or that cannot be decoded, SHALL be
ignored and reported as `generation.continuity = "stale"`; a token minted under an
older generation of the same index remains valid and its refs are re-validated against
the current index, so ordinary vault writes do not discard continuity. The packet
SHALL report `generation.continuity` as `applied`, `stale` or `absent`. A request that
carries a token or an `anchor` override SHALL never be served another request's cached
packet. The server SHALL keep no per-conversation state.

#### Scenario: Continuity strengthens but does not resolve
- **WHEN** a turn carries one contact kind for an anchor the previous packet resolved
  and passes that packet's token
- **THEN** the anchor resolves with evidence `[<contact kind>, continuity]`, and the
  same turn without the token yields `partial`

#### Scenario: A listed candidate is not carried forward
- **WHEN** a packet resolves one anchor and lists a second as `partial`
- **THEN** the token encodes the first anchor's ref and not the second's, and on the
  next turn the second anchor gains no `continuity` evidence

#### Scenario: Unresolved turn stays unresolved
- **WHEN** a turn that is not referential reaches no anchor by any contact kind and
  passes a valid token
- **THEN** the packet abstains with `unresolved`, and the token's anchors are not
  injected

#### Scenario: A referential turn resumes the previous answer
- **WHEN** a referential turn that names nothing, such as "continue", passes the
  token of a packet that resolved an anchor
- **THEN** that anchor resolves with evidence `[continuity, recency]` and its role
  lanes serve its material, and the same turn without a token and with no recent
  edit or read still abstains `unresolved`

#### Scenario: A token from another index is ignored
- **WHEN** a token was minted by a different vault's index, or under a different
  role-registry hash, or is not decodable
- **THEN** the packet is built without it and reports `generation.continuity =
  "stale"`

#### Scenario: A vault write does not discard continuity
- **WHEN** a token was minted under an older generation of the same index and one of
  its anchors has since been removed
- **THEN** the remaining anchors still receive `continuity`, the removed one is
  dropped silently, and the packet reports `generation.continuity = "applied"`

#### Scenario: The token never carries a withheld ref
- **WHEN** the egress guard removes an anchor from the served packet
- **THEN** the decoded token of that packet does not contain that anchor's ref
