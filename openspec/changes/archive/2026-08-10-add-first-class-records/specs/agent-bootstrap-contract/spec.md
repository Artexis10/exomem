## ADDED Requirements

### Requirement: Bootstrap teaches Records routing and boundaries
Bootstrap SHALL describe Records as governed observed state and distinguish it from Sources, Evidence, compiled Notes, Entities, Planning intent, Review, Imported staging, and built-in assistant memory. It SHALL teach natural capture/query intents, manual-first behavior, template independence, derived-view provenance, and the rule that conclusions belong in compiled Notes.

#### Scenario: Log intent routes to Records
- **WHEN** a client asks bootstrap how to handle “log this session”, “record this measurement”, “add this transaction”, or “update this maintenance event”
- **THEN** bootstrap points to `record_memory` and does not route the fact into a compiled conclusion, raw Source, or Evidence unless the user’s intent matches those layers

#### Scenario: Planning intent does not become a Record
- **WHEN** a user describes a goal, desired outcome, initiative, priority, horizon, or candidate future work
- **THEN** bootstrap identifies it as Planning intent and explains that Records can later supply observed progress evidence without mirroring the plan

### Requirement: Bootstrap exposes collection and template guidance compactly
Bootstrap SHALL expose the five finite Records actions, existing selected-pack Records guidance, manifest template descriptors, and relevant schema/storage guidance in a bounded product-facing shape. It SHALL teach intent before storage vocabulary, SHALL NOT require clients to understand internal adapters for ordinary use, and SHALL NOT imply that machine-readable pack blueprints or activation exist in this delivery.

#### Scenario: Pack guidance is not mutation
- **WHEN** bootstrap includes health or personal-records Records guidance
- **THEN** the payload marks it as guidance and no collection, folder, template, or migration is activated merely by reading bootstrap

#### Scenario: Template remains optional
- **WHEN** a collection advertises an ordinary Markdown template
- **THEN** bootstrap explains that humans may insert or edit it directly and that schema validation does not depend on the template file
