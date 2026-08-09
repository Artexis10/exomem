## ADDED Requirements

### Requirement: Path-Anchored Evolution Honors The Requested Page
When a caller requests an evolution view for a named page, the system SHALL
anchor the returned timeline at that page: `topic_anchor` is the requested
page's resolved path, the chain membership and ordering are identical to
the topic-driven route (the walk covers the whole chain from any entry
point), and `chain_id` remains the active head. A named page that cannot be
resolved SHALL produce an explicit error; the system MUST NOT silently fall
back to topic search when a path was given.

#### Scenario: Anchor is the requested page
- **WHEN** an evolution view is requested for a page that is a superseded
  member of a chain
- **THEN** the returned timeline reports `topic_anchor` equal to that
  page's resolved path, `chain_id` equal to the active head, and the same
  version set the topic route returns for that chain

#### Scenario: No silent fallback
- **WHEN** an evolution view is requested for a path that does not resolve
  to a page
- **THEN** the operation fails with an explicit error and does not return
  a topic-search timeline instead

### Requirement: Anchor Semantics Are Declared Per Route
The evolution surfaces SHALL document `topic_anchor`'s meaning on each
route: on the topic-driven route it is the retrieval hit through which the
chain was surfaced; on the path-driven route it is the requested page. No
surface may describe the anchor as the chain's oldest or newest member,
since it is neither by contract.

#### Scenario: Documented meaning matches behaviour
- **WHEN** the op or tool documentation for evolution output is read
- **THEN** it states the per-route anchor semantics above, and the
  rendered `topic_anchor` in each route's output matches the documented
  meaning
