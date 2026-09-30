## MODIFIED Requirements

### Requirement: Bootstrap teaches Records routing and boundaries

Bootstrap SHALL expose `record` as a beginner-facing and product-front-door action and SHALL describe Records as governed observed state distinct from Sources, Evidence, compiled Notes, Entities, Planning intent, Review, Imported staging, and built-in assistant memory. It SHALL teach agents to infer Records participation from durable observed context rather than wait for the user to name Records or issue a magic save verb. It SHALL teach natural capture/query/update intents, proactive existing-collection behavior, proposal-before-first-schema, manual-first behavior, template independence, derived-view provenance, and the rule that conclusions belong in compiled Notes. It SHALL teach that Planning can operate standalone or coordinate with any companion under a resolved user-authored workflow contract; no named companion SHALL be a product-wide default. It SHALL teach the `records_routing` advisory: when a committed note or Evidence write names a collection, route the observation into it through `record_memory` under the served capture disposition, resuming a held candidate when one exists, and never append from the advisory alone without the observation. It SHALL name the `collection_candidate` category and teach that a strong candidate is proposed in domain language with a schema drafted through `describe` and `validate`, that creating the collection requires one inline confirmation, and that backfill uses only exactly dated units cited in `sources`.

#### Scenario: Implicit observation routes to Records
- **WHEN** a client asks bootstrap how to handle a new durable measurement, session, transaction, or maintenance event without explicit save/log/Records wording
- **THEN** bootstrap points to `record_memory`, teaches compatible-collection resolution, and does not route the fact into a compiled conclusion, raw Source, Evidence artifact, or Planning item

#### Scenario: Log intent routes to Records
- **WHEN** a client asks bootstrap how to handle “log this session”, “record this measurement”, “add this transaction”, or “update this maintenance event”
- **THEN** bootstrap points to `record_memory` and does not route the fact into a compiled conclusion, raw Source, or Evidence unless the user’s intent matches those layers

#### Scenario: Planning intent does not become a Record
- **WHEN** a client asks where a future goal, priority, commitment, or candidate task belongs
- **THEN** bootstrap identifies it as Planning intent and explains that Records can later supply observed progress evidence without mirroring the plan

#### Scenario: Accepted software contract stays in the repository
- **WHEN** future software intent is promoted into an OpenSpec change
- **THEN** bootstrap tells the agent to keep only a thin `{kind, ref, label?}` Planning pointer and the item's single authored health field while phase, requirements, tasks, tests, code, and execution state remain in the repository

#### Scenario: Companion execution artifacts stay with their declared owner
- **WHEN** future intent is promoted into an artifact whose type a resolved workflow contract assigns to a companion tool
- **THEN** bootstrap tells the agent to keep only an opaque Planning execution reference and connective context while the declared artifact contents and execution state remain with the companion

#### Scenario: Standalone is the absence-safe default
- **WHEN** no active workflow contract applies
- **THEN** bootstrap teaches that Planning remains fully functional standalone and does not require or infer an external tool

#### Scenario: Missing collection is proposed, not silently activated
- **WHEN** observed state fits Records but inventory contains no compatible collection
- **THEN** bootstrap directs the agent to describe and validate a concise collection proposal and forbids silent schema creation

#### Scenario: A routing advisory is acted on in domain language
- **WHEN** a client asks bootstrap what to do with a `records_routing` advisory on a committed Evidence write
- **THEN** bootstrap tells it to append the observation to the named collection under the served capture disposition, to resume a held candidate if one exists, and never to append from the advisory alone

#### Scenario: A candidate becomes a collection only after one confirmation
- **WHEN** a client asks bootstrap what to do with a strong `collection_candidate`
- **THEN** bootstrap tells it to draft the schema through `describe` and `validate`, ask one question in domain language, create only on confirmation, and backfill only exactly dated units cited in `sources`

#### Scenario: Compact bootstrap stays within its byte ceiling
- **WHEN** the compact profile is measured after these clauses are added
- **THEN** it does not exceed the pinned ceiling, with the trimmed text and both measured sizes recorded
