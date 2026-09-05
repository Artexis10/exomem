## MODIFIED Requirements

### Requirement: Relation governance preserves registry-generated surface parity
The product registry SHALL expose relation-intent resolution through the
connection command; relation registry infer, validate, diff, proposal, and
delta-save behavior through the schema-governance command; relation review
through the review, triage, and connection commands; and traversal profile
selection through the context command. Requested-relation, relation census
date scope, source-hint, proposal, reason, and hash-guard parameters SHALL have
identical semantics across MCP, REST, CLI, OpenAPI, generated docs,
annotations, and schema-fidelity fixtures. Newly returned relation-review items
SHALL require their supplied source hint on accept/triage. Existing hintless
relation-review calls SHALL receive bounded-prefix compatibility or a typed
refresh-required response rather than unbounded recovery work.

Read-only relation resolution, proposal, inventory, inference without save,
validation, diff, queue, and context selectors SHALL not acquire writer
authority. Relation delta save, candidate accept, and triage SHALL use the
existing mutation boundary, idempotency, receipt, terminal, and retry
contracts; unknown or uncovered selectors MUST fail closed.

#### Scenario: One registry definition exposes relation governance everywhere
- **WHEN** the generated surfaces are inspected after this change
- **THEN** connection accepts relation resolution and source-hinted acceptance,
  schema governance accepts relation/profile subjects and reviewed
  propose/save operations, review and triage preserve source hints, context
  accepts traversal profiles, and every surface exposes the same defaults,
  bounds, hash guards, and validation codes

#### Scenario: Existing callers retain prior behavior
- **WHEN** callers omit relation-governance subjects, requested relation,
  census dates, source hints, and traversal profiles
- **THEN** existing schema contract behavior, review refs, and broad context
  traversal remain unchanged, except that a hintless relation decision outside
  the bounded current queue prefix requires refresh rather than an unbounded
  compatibility scan

#### Scenario: Proposal and resolution remain lease-free
- **WHEN** a caller resolves relation intent, proposes a relation delta, or
  infers relation vocabulary with save disabled
- **THEN** invocation classification treats the operation as read-only and does
  not contact the writer coordinator

#### Scenario: Relation delta save enters the governed mutation path
- **WHEN** a caller saves a reviewed relation delta
- **THEN** invocation classification enters writer authority and preserves the
  shared idempotency, receipt, terminal, and retry semantics
- **AND** an unregistered future selector cannot default to either read or write
  behavior without explicit coverage
