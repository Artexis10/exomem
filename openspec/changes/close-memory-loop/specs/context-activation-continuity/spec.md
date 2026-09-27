## MODIFIED Requirements

### Requirement: Continuity token is client-carried and never resolves alone
`activate_context` SHALL return an opaque `continuity` token minted from the packet as
served after the egress guard, encoding the refs of the anchors that packet RESOLVED
(never the refs of anchors it only listed as `partial`), the selected roles,
the activation index identity (the sidecar's own identity token, which differs per
vault and per rebuilt sidecar), the role-registry hash, the index generation and the
time the packet was served, and
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
`recency`; on any other turn the token SHALL only qualify anchors the turn reached.
A token MAY name a compiled page that is not an anchor — one the agent picked or recall
carried — by its path. That tier SHALL lead only while it is the latest thing that
happened: once a deliberate act — work, an admitted pick or a recorded episode — on a
page outside the token's refs is later than the time the token was served, the token's
refs SHALL be ranked like any other; a read does not move a conversation on. Where the
caller supplies a session key, only that session's own deliberate acts SHALL end its
token's lead. A token
that does not say when it was served, or says it unreadably, SHALL lead as before and
SHALL NOT be reported `stale` for that reason. A
token whose index identity
or registry hash does not match the serving state, or that cannot be decoded, SHALL be
ignored and reported as `generation.continuity = "stale"`; a token minted under an
older generation of the same index remains valid and its refs are re-validated against
the current index, so ordinary vault writes do not discard continuity. The packet
SHALL report `generation.continuity` as `applied`, `stale` or `absent`, and SHALL
report `applied` only when at least one of a valid token's refs, visible to the
current audience, qualified something: an anchor that carries `continuity` evidence,
or a page a referential turn resumed. A valid token whose refs qualified nothing —
naming nothing, naming only what this audience may not see, or naming a row or page
this turn neither reached nor resumed — SHALL be reported `stale`. A request that
carries a token or an `anchor` override SHALL never be served another request's cached
packet. The server SHALL keep no per-conversation state beyond the bounded,
machine-local heat projection, and SHALL keep there only salted, audience-scoped
derivations of the keys a caller supplies, never the values: for a caller that supplies a
session key, the refs of the last packet that session was served and the workspace key it
was last seen with (the session-to-workspace map the workspace tier ranks by); the
session and workspace keys on that caller's admitted picks; and, on a recorded episode's
events, the episode key it was recorded under, which is the session key the hooks pass.
None of it is served back or part of a packet, so a session that lost its token still
continues its own thread (memory-loop: "A turn that names nothing MAY resolve to the
hottest recent anchor").

Every packet, an abstention included, SHALL carry a token, except a `withheld`
abstention, for which the egress guard served no packet at all, and the token SHALL name
the caller's thread: an opaque random value the server chose, which the caller passes
back unchanged on its next call in the same conversation. The server instructions and
the tool description SHALL ask the caller to do so. A token that names no ref SHALL
carry generation 0, whoever the audience. A token that carries a thread and
no refs SHALL be reported `generation.continuity = "absent"`; the refs' semantics above
are otherwise unchanged, and a packet's thread never adds a ref to its token. A valid
thread is one minted by this vault's index, carried by a packet served no longer ago
than a declared idle bound, and begun no longer ago than a declared lifetime bound.
Where the caller supplies no session key of its own, a valid thread SHALL be its session
key for the heat projection: its admitted picks and its last served thread are recorded
under it, and its session tier ranks by it. It SHALL be stored only as a salted
derivation scoped to the caller's audience, distinct from any supplied session key's,
never as the value. It SHALL NOT entitle the caller to the vault tier's referent
(memory-loop: "A turn that names nothing MAY resolve to the hottest recent anchor"). An
unreadable, foreign or lapsed thread SHALL be ignored and the call served as a keyless
caller with a fresh thread, never refused; the packet SHALL report
`generation.continuity_thread` as `applied`, `stale` or `absent`. Where the server
abstains before it can read the token (`index_warming`, `unavailable`, `disabled`), it
SHALL hand a thread-bearing token back unchanged, so a conversation survives a warming
index or a spent request budget.

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

#### Scenario: A referential turn follows work done after the token
- **WHEN** a referential turn passes the token of an earlier packet, and since that
  packet was served the user edited a different anchor on its own
- **THEN** that edited anchor is the referent, not the token's anchors

#### Scenario: A read or another session's work does not end a token's lead
- **WHEN** a referential turn passes a session key and the token of an earlier
  packet, and since then the user only read another page, or another session
  worked elsewhere
- **THEN** the token's refs are still the referent

#### Scenario: A token names a page
- **WHEN** the previous packet carried a compiled page that is not an anchor and a
  referential turn passes its token
- **THEN** the token names that page by its path and the page is resumed, reported
  `resolved` on `continuity` and `recency`

#### Scenario: A token from another index is ignored
- **WHEN** a token was minted by a different vault's index, or under a different
  role-registry hash, or is not decodable
- **THEN** the packet is built without it and reports `generation.continuity =
  "stale"`

#### Scenario: A token that names nothing is not reported applied
- **WHEN** a token this index issued is passed, and none of its refs names a row
  of the current index or an eligible compiled page
- **THEN** the packet reports `generation.continuity = "stale"`

#### Scenario: A withheld ref answers exactly as a missing one
- **WHEN** a token names a page or an anchor the current audience may not see,
  on a referential turn or any other
- **THEN** the response's status, abstention and `generation` block are identical
  to those for a token naming a page that does not exist, and report
  `generation.continuity = "stale"` with no `carried_by`

#### Scenario: A vault write does not discard continuity
- **WHEN** a token was minted under an older generation of the same index and one of
  its anchors has since been removed
- **THEN** the remaining anchors still receive `continuity`, the removed one is
  dropped silently, and the packet reports `generation.continuity = "applied"`

#### Scenario: The token never carries a withheld ref
- **WHEN** the egress guard removes an anchor from the served packet
- **THEN** the decoded token of that packet does not contain that anchor's ref

#### Scenario: A keyless conversation keeps its own thread
- **WHEN** a caller that supplies no session or workspace key receives an abstained
  packet and passes its token back on the next call
- **THEN** the abstained packet carried a token with a thread and no refs, the next
  packet reports `generation.continuity_thread = "applied"` and
  `generation.continuity = "absent"`, and its token names the same thread
- **AND** the heat projection holds no copy of the thread's value

#### Scenario: A lapsed or unreadable thread is served as keyless
- **WHEN** a caller passes a token whose thread lapsed, or a string that is not a token
- **THEN** the call is served as a keyless caller, reports
  `generation.continuity_thread = "stale"`, and returns a token naming a fresh thread

#### Scenario: A withheld page in a thread answers as an absent one
- **WHEN** a caller other than the owner, under a governed policy, passes a thread whose
  session tier also touched a page it may not see
- **THEN** its packets, `generation` included, are identical to those it receives when
  that page was never touched, and the thread is still reported `applied`
