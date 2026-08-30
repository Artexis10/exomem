## MODIFIED Requirements

### Requirement: Bootstrap teaches Records routing and boundaries

Bootstrap SHALL expose `record` as a beginner-facing and product-front-door action and SHALL describe Records as governed observed state distinct from Sources, Evidence, compiled Notes, Entities, Planning intent, Review, Imported staging, and built-in assistant memory. It SHALL teach agents to infer Records participation from durable observed context rather than wait for the user to name Records or issue a magic save verb. It SHALL teach natural capture/query/update intents, proactive existing-collection behavior, proposal-before-first-schema, manual-first behavior, template independence, derived-view provenance, and the rule that conclusions belong in compiled Notes. It SHALL teach that Planning can operate standalone or coordinate with any companion under a resolved user-authored workflow contract; no named companion SHALL be a product-wide default.

#### Scenario: Implicit observation routes to Records
- **WHEN** a client asks bootstrap how to handle a new durable measurement, session, transaction, or maintenance event without explicit save/log/Records wording
- **THEN** bootstrap points to `record_memory`, teaches compatible-collection resolution, and does not route the fact into a compiled conclusion, raw Source, Evidence artifact, or Planning item

#### Scenario: Log intent routes to Records
- **WHEN** a client asks bootstrap how to handle “log this session”, “record this measurement”, “add this transaction”, or “update this maintenance event”
- **THEN** bootstrap points to `record_memory` and does not route the fact into a compiled conclusion, raw Source, or Evidence unless the user’s intent matches those layers

#### Scenario: Planning intent does not become a Record
- **WHEN** a client asks where a future goal, priority, commitment, or candidate task belongs
- **THEN** bootstrap identifies it as Planning intent and explains that Records can later supply observed progress evidence without mirroring the plan

#### Scenario: Companion execution artifacts stay with their declared owner
- **WHEN** future intent is promoted into an artifact whose type a resolved workflow contract assigns to a companion tool
- **THEN** bootstrap tells the agent to keep only an opaque Planning execution reference and connective context while the declared artifact contents and execution state remain with the companion

#### Scenario: Standalone is the absence-safe default
- **WHEN** no active workflow contract applies
- **THEN** bootstrap teaches that Planning remains fully functional standalone and does not require or infer an external tool

#### Scenario: Missing collection is proposed, not silently activated
- **WHEN** observed state fits Records but inventory contains no compatible collection
- **THEN** bootstrap directs the agent to describe and validate a concise collection proposal and forbids silent schema creation

## ADDED Requirements

### Requirement: Bootstrap exposes workflow contract resolution compactly

Every bootstrap profile SHALL carry the immutable workflow invariant kernel and built-in standalone fallback. A profile exporting `schema_memory` SHALL also carry at most one released default summary, at most eight released scoped summaries in deterministic key order, exact released-total/projection-truncation metadata after a complete scan, and the shared schema/configuration route for contract inventory and resolution. A profile omitting the command SHALL advertise no route and report `resolution_available: false`; it SHALL expose built-in standalone only for an empty released inventory with no migration requirement, otherwise fixed `workflow_resolution_unavailable` with contract-aware proactive routing disabled. Bootstrap SHALL teach route-capable agents to resolve context at the start of substantial work or durable scoping, including explicit null for known-absent dimensions, then inspect relevant Planning before capture. It SHALL distinguish declared companion tools from capabilities actually exported by the active client. If the durable pre-feature marker requires review without an active released workflow contract, bootstrap SHALL report `workflow_contract_migration_required` and SHALL NOT silently select standalone.

#### Scenario: Generic client learns the personalized workflow
- **WHEN** a generic MCP client whose active profile exports `schema_memory` starts against a vault with a scoped companion contract
- **THEN** bootstrap gives it enough information to resolve that contract before planning work without installing a private skill or receiving hard-coded companion instructions

#### Scenario: Contract inventory creates nothing
- **WHEN** bootstrap reads workflow summaries
- **THEN** no contract, Planning item, Record, external artifact, or integration state is created or changed

#### Scenario: Legacy workflow requires a choice
- **WHEN** an upgraded pre-feature vault has a review-required migration marker and no active released workflow contract
- **THEN** bootstrap reports a content-bounded migration requirement and a route-capable profile directs the agent to explicit session selection or reviewed contract save before ordinary fallback

#### Scenario: Profile without schema command remains honest
- **WHEN** a Planning-capable active profile omits `schema_memory` and a released scoped contract exists
- **THEN** bootstrap reports `workflow_resolution_unavailable`, advertises no unavailable route, and disables contract-aware proactive routing rather than choosing a broader/default contract

#### Scenario: Hidden contract is absent from bootstrap
- **WHEN** the vault contains an unreleased workflow contract
- **THEN** bootstrap is byte-equivalent to the result for an otherwise identical vault without that contract
