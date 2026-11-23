## MODIFIED Requirements

### Requirement: Bootstrap teaches Records routing and boundaries

Bootstrap SHALL expose `record` as a beginner-facing front-door action and describe Records as governed observed state, distinct from Sources, Evidence, compiled Notes, Entities, Planning intent, Review, Imported staging and built-in assistant memory. It SHALL teach agents to infer Records participation from durable observed context without waiting for the user to name Records or issue a save verb. This teaching SHALL stay command-free so reduced surfaces keep it.

#### Scenario: Bootstrap teaches the Records intents and their boundaries
- **WHEN** an agent reads the bootstrap Records teaching
- **THEN** it teaches natural capture, query and update intents, proactive existing-collection behavior, the recognition of recurring structured work, bounded creation of a minimal collection under the served capture disposition, template independence, derived-view provenance, and that conclusions belong in compiled Notes

#### Scenario: Planning has no default companion
- **WHEN** an agent reads how Planning relates to other tools
- **THEN** it learns that Planning operates standalone or coordinates with any companion under a resolved user-authored workflow contract, and that no named companion is a product-wide default

#### Scenario: Bootstrap teaches the routing advisory
- **WHEN** a committed note or Evidence write names a collection
- **THEN** bootstrap teaches the agent to route the observation into it through `record_memory` under the served capture disposition, resuming a held candidate when one exists and never appending from the advisory alone without the observation; that a collection may declare `claims.match`; that a disposition on the advisory carries its own instruction; and that a grouped backfill item is asked about once at every prominence and never filed without a yes

#### Scenario: Bootstrap names the collection candidate category
- **WHEN** an agent reads the bootstrap structural-suggestion teaching
- **THEN** it names the `collection_candidate` category and teaches that a strong candidate is proposed in domain language with a schema drafted through `describe` and `validate`, that creating the collection follows the served capture disposition within the automatic-creation bounds, and that backfill uses only exactly dated units cited in `sources`

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
- **WHEN** observed state fits Records, inventory contains no compatible collection, and the served capture disposition is `advisory`
- **THEN** bootstrap directs the agent to describe and validate a concise collection proposal, to create it only after one confirmation, and forbids unreported schema creation

#### Scenario: Missing collection is created within bounds and reported
- **WHEN** recurring structured work fits Records, inventory contains no compatible collection, and its identity, type and scope are clear
- **THEN** bootstrap directs the agent to describe and validate a minimal collection, to create it with `automatic: true` when the served capture disposition is `silent` or to propose it when the disposition is `advisory`, and to name the created collection and rows in its reply

#### Scenario: A routing advisory is acted on in domain language
- **WHEN** a client asks bootstrap what to do with a `records_routing` advisory on a committed Evidence write
- **THEN** bootstrap tells it to append the observation to the named collection under the served capture disposition, to resume a held candidate if one exists, and never to append from the advisory alone

#### Scenario: A candidate becomes a collection only after one confirmation
- **WHEN** a client asks bootstrap what to do with a strong `collection_candidate` and the served capture disposition is `advisory`
- **THEN** bootstrap tells it to draft the schema through `describe` and `validate`, ask one question in domain language, create only on confirmation, and backfill only exactly dated units cited in `sources`

#### Scenario: A candidate becomes a collection under silent capture
- **WHEN** a client asks bootstrap what to do with a strong `collection_candidate` and the served capture disposition is `silent`
- **THEN** bootstrap tells it to draft the schema through `describe` and `validate`, to create it with `automatic: true` within the automatic-creation bounds or to ask one question in domain language when a bound fails, and to backfill only exactly dated units cited in `sources`

#### Scenario: Compact bootstrap stays within its byte ceiling
- **WHEN** the compact profile is measured after these clauses are added
- **THEN** it does not exceed the pinned ceiling, with the trimmed text and both measured sizes recorded

#### Scenario: Bootstrap points at the disposition without naming commands
- **WHEN** a client reads the compact bootstrap's routing teaching
- **THEN** it names `claims.match`, says the disposition carries its own instruction, says a grouped backfill is asked about once at any prominence, and names no command, within the compact byte ceiling

### Requirement: The capture predicate covers stated intent and observed outcomes

The capture axis of the engagement contract at `balanced` and `maximal` SHALL name two lifecycle classes beside a durable conclusion, a recurring entity and a carried-out method: a *stated intent or commitment* routes to Planning through `plan_memory`, and an *observed outcome or event* routes to Records through `record_memory`. `light` SHALL keep capture-only-when-asked and `off` SHALL be unchanged.

#### Scenario: Intent and outcome are defined
- **WHEN** an agent reads the capture axis
- **THEN** a stated intent or commitment is the user saying what they will do, committing to a batch or workstream, sequencing work, or re-prioritising, and an observed outcome or event is the conversation reporting that something happened, was produced, measured, delivered, approved, published or failed

#### Scenario: One landing has two consequences
- **WHEN** an observed outcome lands on an open committed Planning item
- **THEN** the contract says it is one landing with two consequences, the Records append and then the Planning transition, performed together and reported once

#### Scenario: Elapsed time is not an outcome
- **WHEN** a conversation only shows that time has passed
- **THEN** the contract says elapsed time is never an outcome and a tentative claim is never written as an event

#### Scenario: The Records engagement policy gates both classes
- **WHEN** an agent writes an intent or an outcome
- **THEN** exactly one compatible collection and a sufficiently identified observation mean write and report, competing collections mean one focused question, and no collection means a proposal under `advisory` `proactive_capture` or one minimal collection within the server's automatic-creation bounds, reported in the same reply, under `silent`

#### Scenario: One rule is carried everywhere
- **WHEN** the capture rule is served
- **THEN** both `SKILL.md` copies, `prominence.py` and the bootstrap `intent_boundary` and `capture_examples` carry the same rule, and the compact payload stays within its measured ceiling

#### Scenario: A stated outcome closes a queued deliverable without a magic word

- **WHEN** a Planning collection keyed on title holds a queued committed work item and the user says in ordinary language that that deliverable was produced today, without mentioning Exomem, Planning, Records, save, track or remember
- **THEN** an agent under `balanced` or `maximal` appends the production event to the one compatible Records collection, transitions the work item out of its open state, and reports both in one line in the user's domain language

#### Scenario: Sequencing language files intent, not events

- **WHEN** the user says the remaining deliverables will be done next time
- **THEN** the agent leaves them queued in Planning, writes no Records event, and does not ask whether to "track" them

#### Scenario: A tentative claim is not an event

- **WHEN** the user says a deliverable was probably published but is not sure
- **THEN** the agent writes no publication event and, where the collection offers a note field, may record the stated uncertainty there; it never fabricates an observed event

#### Scenario: Light prominence does not widen

- **WHEN** the engagement level is `light`
- **THEN** neither lifecycle class triggers an unprompted write and the contract text for that level does not name them as proactive

#### Scenario: An observed outcome with no collection gets a bounded one under silent capture

- **WHEN** the conversation reports a recurring observed outcome, no compatible collection exists, the identity, type and scope are clear, and the effective `proactive_capture` is `silent`
- **THEN** the agent creates one minimal Records collection with `automatic: true` within the server's bounds, appends the outcome, and names the collection and the row in its reply
