## MODIFIED Requirements

### Requirement: Bootstrap teaches Records routing and boundaries
Bootstrap SHALL describe Records as governed observed state and Planning as governed intended future state, and SHALL distinguish both from Sources, Evidence, compiled Notes, Entities, Review, Imported staging, and built-in assistant memory. It SHALL teach natural capture/query intents, manual-first behavior, template independence, derived-view provenance, the Planning/Records relationship, and the rule that durable conclusions belong in compiled Notes. For software work it SHALL state that Exomem Planning owns intent, priority, horizon, and durable coordination context while OpenSpec, git, tests, and code own accepted change contracts and execution truth.

#### Scenario: Log intent routes to Records
- **WHEN** a client asks bootstrap how to handle “log this session”, “record this measurement”, “add this transaction”, or “update this maintenance event”
- **THEN** bootstrap points to `record_memory` and does not route the fact into Planning, a compiled conclusion, raw Source, or Evidence unless the user’s intent matches those layers

#### Scenario: Planning intent does not become a Record
- **WHEN** a user describes a goal, desired outcome, encountered bug, feature candidate, initiative, priority, horizon, commitment, or other future work
- **THEN** bootstrap points to `plan_memory`, does not route the intent into Records, and explains that Records can later supply observed evidence without mirroring or automatically changing the plan

#### Scenario: Accepted software contract stays in the repository
- **WHEN** future software intent is promoted into an OpenSpec change
- **THEN** bootstrap tells the agent to keep only a thin `{kind, ref, label?}` Planning pointer and the item's single authored health field while phase, requirements, tasks, tests, code, and execution state remain in the repository

## ADDED Requirements

### Requirement: Bootstrap exposes Planning actions and guidance compactly
Bootstrap SHALL expose the six finite Planning actions, the four Planning kinds, lifecycle/priority/commitment/horizon vocabulary, manual-first Markdown ownership, default inbox capture, opaque Records evidence and execution-pointer boundaries, and relevant selected-pack guidance in a bounded product-facing shape. It SHALL teach intent before collection vocabulary and SHALL NOT imply that reading guidance creates a collection, performs Review, synchronizes an external system, or activates a pack blueprint.

#### Scenario: Agent can file an idea without internal vocabulary
- **WHEN** a generic client asks how to remember a possible future feature
- **THEN** bootstrap teaches candidate inbox capture through `plan_memory(action="add")` without requiring the user to choose schema, adapter, marker, or folder internals

#### Scenario: Multi-horizon question routes to structured query
- **WHEN** a user asks what is planned this week, quarter, year, or over multiple years
- **THEN** bootstrap routes to a bounded Planning saved view/query, labels horizons as explicitly maintained buckets rather than inferred calendar truth, and uses date filters when the question requires exact calendar bounds

#### Scenario: Pack guidance creates nothing
- **WHEN** bootstrap includes technical, business, health, personal, or creative Planning guidance
- **THEN** the payload marks it as guidance and no collection, item, template, hierarchy, or migration is activated merely by reading bootstrap

#### Scenario: No command-alias workflow skill is required
- **WHEN** an agent has the generic Exomem skill or bootstrap contract
- **THEN** ordinary Planning capture, triage, and query are fully routable without installing a separate skill that only repeats `plan_memory` arguments
