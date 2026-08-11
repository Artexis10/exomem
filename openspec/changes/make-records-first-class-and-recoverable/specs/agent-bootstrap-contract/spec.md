## MODIFIED Requirements

### Requirement: Bootstrap teaches Records routing and boundaries

Bootstrap SHALL expose `record` as a beginner-facing and product-front-door action and SHALL describe Records as governed observed state distinct from Sources, Evidence, compiled Notes, Entities, Planning intent, Review, Imported staging, and built-in assistant memory. It SHALL teach agents to infer Records participation from durable observed context rather than wait for the user to name Records or issue a magic save verb. It SHALL teach natural capture/query/update intents, proactive existing-collection behavior, proposal-before-first-schema, manual-first behavior, template independence, derived-view provenance, and the rule that conclusions belong in compiled Notes.

#### Scenario: Implicit observation routes to Records
- **WHEN** a client asks bootstrap how to handle a new durable measurement, session, transaction, or maintenance event without explicit save/log/Records wording
- **THEN** bootstrap points to `record_memory`, teaches compatible-collection resolution, and does not route the fact into a compiled conclusion, raw Source, Evidence artifact, or Planning item

#### Scenario: Planning intent does not become a Record
- **WHEN** a client asks where a future goal, priority, commitment, or candidate task belongs
- **THEN** bootstrap identifies it as Planning intent and explains that Records can later supply observed progress evidence without mirroring the plan

#### Scenario: Missing collection is proposed, not silently activated
- **WHEN** observed state fits Records but inventory contains no compatible collection
- **THEN** bootstrap directs the agent to describe and validate a concise collection proposal and forbids silent schema creation

### Requirement: Bootstrap exposes collection guidance compactly

Bootstrap SHALL expose all finite Records actions and a bounded product-facing collection-authoring summary. Compact bootstrap SHALL identify `_collection.md`, supported Records collection versions and profiles, the `describe -> validate -> create -> inspect -> append` authoring workflow, and the `validate -> revise` plus `inspect -> rebaseline` maintenance workflows. It SHALL route clients to `record_memory(action="describe")` for the complete technical contract and SHALL NOT embed the full manifest JSON Schema, parser field table, or worked manifest. It SHALL teach intent before storage vocabulary and SHALL NOT imply that guidance activates a collection or migration.

#### Scenario: Pack guidance is not mutation
- **WHEN** bootstrap includes health or personal-records Records guidance
- **THEN** the payload marks it as guidance and no collection, folder, template, migration, or canonical data is activated merely by reading bootstrap

#### Scenario: Template remains optional
- **WHEN** a collection recommends an ordinary Markdown template
- **THEN** bootstrap explains that humans may insert or edit it directly and that schema validation does not depend on the template file

## ADDED Requirements

### Requirement: Records routing is salient in compact bootstrap

Compact bootstrap SHALL serialize beginner/front-door actions and the bounded Records route before the large semantic-authoring projection. It SHALL enforce a total compact-size budget and a maximum byte position for the `record` route so Records cannot remain technically present but practically buried. Full parser/schema detail SHALL remain opt-in through `record_memory(action="describe")`.

#### Scenario: Record route appears before semantic authoring detail
- **WHEN** compact bootstrap is serialized with the full active MCP product surface
- **THEN** the `record` action and Records intent boundary appear before semantic-authoring detail and within the tested early-position budget

#### Scenario: Compact budget rejects salience regression
- **WHEN** unrelated bootstrap material grows enough to push Records beyond its byte-position or total-size budget
- **THEN** the bootstrap contract test fails even though a Records key still exists somewhere in the payload
