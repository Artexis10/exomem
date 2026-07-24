# release-gate

## ADDED Requirements

### Requirement: Disclosure ladder and single per-level projector

The release plane SHALL support an ordered disclosure ladder — L0 none, L1
notice, L2 constraint, L3 abstract, L4 redacted excerpt, L5 excerpt, L6 full —
and SHALL render each item at the highest permitted, lowest sufficient level for
its decision. A single per-level projector SHALL be the only path from any
candidate (hit, page, pack element, unit) to a wire representation. Ranking
signals, graph seed provenance, relation-match annotations, matched units,
supersession pointers, and parent references SHALL appear only at L5–L6 and SHALL
be stripped at every level when they name a sub-notice item. Any content-returning
command without a registered projector SHALL fail closed, emitting no path,
title, or excerpt.

#### Scenario: Low levels strip metadata oracles

- **WHEN** an item is released at L1
- **THEN** the response carries only its rule id and scope label, and no score,
  signal, graph seed, relation match, matched unit, supersession pointer, or path

#### Scenario: Scores only at high levels

- **WHEN** an item is released below L5
- **THEN** no ranking signal or similarity score for it crosses the boundary

#### Scenario: Unregistered surface fails closed

- **WHEN** a content-returning command has no registered projector
- **THEN** it emits no path, title, or excerpt, and the omission is a test failure

### Requirement: Decision annotation and request-deterministic counts

Release decisions SHALL be computed after retrieval returns and before response
assembly, per item, keyed on the caller's audience and declared purpose and the
active grants. Withheld items SHALL be replaced by next-best permitted candidates
drawn from a pre-committed over-fetch pool so the shown result count is a function
of the request, not of how many items were withheld. When the pool is exhausted,
L1-and-above scopes SHALL still emit their notice while L0 scopes SHALL return a
silently shorter list. The shared retrieval hot cache SHALL remain principal-free;
decisions SHALL be a separate per-request memo and SHALL NOT fragment the recall
cache by audience or purpose.

#### Scenario: Withholding does not change the visible count

- **WHEN** a query would return N permitted items and some candidates are withheld
  while the over-fetch pool can still fill N
- **THEN** exactly N items are returned and the count does not reveal that any
  were withheld

#### Scenario: Cache stays principal-free

- **WHEN** two different audiences run the same query and the second hits the
  retrieval cache
- **THEN** the second response carries its own decisions and no cached candidate
  copy carries another audience's decision

### Requirement: Canonical audience resolution, threaded and fail-closed

Every content-returning read SHALL resolve a canonical audience at its surface
boundary — MCP OAuth principal, REST key scope, hosted cell principal, or `owner`
for stdio/CLI — normalized into one comparable identity space, and SHALL thread it
to the release decision. A grant authored against one surface SHALL match the same
principal on another surface. When identity should resolve but cannot, the release
decision SHALL fail closed to the most restrictive outcome, never to full
disclosure.

#### Scenario: Same principal across surfaces

- **WHEN** the same human queries via MCP and via REST
- **THEN** a grant authored for that principal applies on both

#### Scenario: Unresolved identity denies

- **WHEN** an authenticated surface cannot resolve the expected principal
- **THEN** the decision is most-restrictive, not OPEN

### Requirement: Single-use content-bound escalation tokens

A withheld or abstracted item MAY carry a single-use escalation token bound to the
audience, the item's content fingerprints, a maximum level, and an expiry, minted
with a per-machine secret and consumed exactly once in a per-machine store. A
token SHALL NOT authorize a level above its bound maximum, SHALL NOT be replayable
across sessions or clients, and SHALL fail closed when the referenced item's
content has changed since minting, offering a fresh escalation rather than
disclosing changed content.

#### Scenario: Single use, bounded

- **WHEN** an escalation token is redeemed
- **THEN** it discloses at most its bound level, and a second redemption is
  refused

#### Scenario: Content drift invalidates the token

- **WHEN** the item's content changed after the token was minted
- **THEN** redemption is refused and a fresh escalation referencing the new
  content is offered

### Requirement: Terminal secret scrubber at the shared dispatcher

An always-on deterministic secret scrubber SHALL run at the dispatcher shared by
the MCP, REST, hosted, and CLI surfaces, over the final result of every
content-returning command, blocking credential-shaped strings from crossing the
boundary and replacing them with a notice. The scrubber SHALL run even when no
governance policy is present, SHALL allowlist structural identifier fields
(content hashes, refs, fingerprints) to avoid false positives, and MAY be disabled
by an explicit standing rule.

#### Scenario: Credential blocked on an ungoverned vault

- **WHEN** a result contains a private key block or an API-key-shaped token and no
  `_Governance/` policy exists
- **THEN** the credential value does not cross the boundary and a notice reports
  the block

#### Scenario: Structural identifiers are not false positives

- **WHEN** a result contains legitimate content hashes and `exomem://` refs
- **THEN** the scrubber leaves them intact

#### Scenario: Every surface is covered

- **WHEN** the same restricted query is issued over MCP, REST, and CLI (including
  the retrieve-inject hook path)
- **THEN** all three responses carry field-identical projections with no
  sub-notice paths or excerpts

### Requirement: Empty-policy fast path

When no `_Governance/` policy exists, the release plane SHALL short-circuit to
baseline behavior plus the always-on secret scrubber, preserving current response
shape and latency. The credential scrubber SHALL be the only behavioral difference
for an ungoverned vault.

#### Scenario: Ungoverned recall is baseline

- **WHEN** a query runs on a vault with no `_Governance/` directory
- **THEN** results match baseline except that credential-shaped strings are
  blocked, and the latency gate is unchanged
