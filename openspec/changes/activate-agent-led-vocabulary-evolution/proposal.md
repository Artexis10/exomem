## Why

Exomem can register custom entity and relationship types, but ordinary agent work can still leave recurring identities unmodelled, existing entities stale, and meaningful connections hidden behind generic links. Vocabulary evolution needs a visible, accountable agent workflow, not just registry endpoints that an agent must remember to discover.

## What Changes

- Make the active agent consider reuse, enrichment and justified extension during ordinary capture and review. The server supplies bounded evidence and candidate definitions; the agent judges meaning. Honest generic connections, no edge and deferral remain valid outcomes.
- Introduce durable, fingerprint-bound vocabulary review items and decisions, including relevant opportunities that do not produce an unknown-label warning today. Repeated unchanged advice is suppressed without pretending the underlying work is resolved.
- Cover entity instances, entity types and relation types first. Define a family adapter contract so future vocabulary families can share the workflow without inheriting permissions, arbitrary fields or executable behaviour. Existing open categories remain open without mandatory registration.
- Extend existing review, triage, connection and schema tools over shared command leaves; do not add a universal mutation endpoint or a server-side reasoning model.
- Add a versioned, opt-in authority contract: users can choose advice, per-action approval, or scoped additive delegation. Entity creation, type registration and edge acceptance have separate grantable actions. Merges, deletion, supersession, disclosure and changes to existing meanings remain separately controlled.
- **BREAKING (opt-in v2 contract only):** additive structural writes require machine-verifiable user authority at the canonical leaves, not merely an agent's assertion that it obtained permission. Vaults remaining on v1 keep their explicitly disclosed confirmation contract; old clients cannot downgrade an activated v2 vault.
- Keep optional expensive discovery default-off and soft-failing. Bounded ordinary guidance uses existing projections and current-write context, adds no model dependency, and never converts a committed note write into a failure.

## Capabilities

### New Capabilities

- `agent-vocabulary-workflow`: Bounded agent-facing consideration, explicit decisions, family extensibility and evidence of real adoption.
- `scoped-additive-authority`: Trusted user grants and exact approvals, per-action/per-vault scope, revocation and leaf enforcement for additive structural writes.

### Modified Capabilities

- `delegation-envelope`: Preserve v1 and separate prominence from the deliberately enabled v2 additive authority contract.
- `agent-bootstrap-contract`: Teach and advertise the complete evolving-vocabulary loop, supported families and actual authority capabilities.
- `mutation-terminal-contract`: Expose bounded vocabulary review context without changing mutation success or replay identity.

## Impact

Expected implementation areas are `commands.py`, `mutation_terminal.py`, the entity and relation registries/resolvers, review projections/state, engagement and authorization binding, MCP/REST/CLI adapters, and the generic skill scaffold. No live policy, registry or graph is changed by this proposal.

Build on the shipped `relation-vocabulary-evolution` and `entity-type-registry` contracts. `complete-recurring-entity-lifecycle` owns ordinary-text identity detection, hydration evidence and entity-family traversal; `add-governed-curation-lane` owns multi-step reviewed execution and recovery. This change owns the common consideration/decision loop and additive authority, not duplicate implementations of those features. Their older confirm-only wording must be reconciled before v2 integration; v1 remains confirm-only. The vault-consolidation governance stack, observability and memory optimisation are outside this change.
