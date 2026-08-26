## ADDED Requirements

### Requirement: The capture predicate covers stated intent and observed outcomes

The capture axis of the engagement contract at `balanced` and `maximal` SHALL name two lifecycle classes beside a durable conclusion, a recurring entity and a carried-out method: a *stated intent or commitment* — the user says what they will do, commits to a batch or workstream, sequences work, or re-prioritises — routes to Planning through `plan_memory`; an *observed outcome or event* — the conversation reports that something happened, was produced, measured, delivered, approved, published or failed — routes to Records through `record_memory`. The contract SHALL state the pairing rule: an observed outcome that lands on an open committed Planning item is one landing with two consequences, the Records append and then the Planning transition, performed together and reported once. It SHALL state that a tentative claim is never written as an event and that elapsed time is never an outcome. The existing Records engagement policy (exactly one compatible collection and a sufficiently identified observation → write and report; competing collections → one focused question; no collection → propose, never silently create) SHALL be the write gate for both classes. `light` SHALL keep capture-only-when-asked and `off` SHALL be unchanged. Both `SKILL.md` copies, `prominence.py` and the bootstrap `intent_boundary` and `capture_examples` SHALL carry the same rule, and the compact payload SHALL stay within its measured ceiling.

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

### Requirement: Lifecycle consequences are reported in domain language

The contract SHALL teach that the report of a lifecycle consequence states what is now true in the user's vocabulary and cites the page or collection the way recall cites pages. The words Planning, Records, collection, schema and natural key SHALL never be required of the user and SHALL NOT lead the report.

#### Scenario: Report names the outcome, cites the store

- **WHEN** the agent has appended a production event and completed the matching work item
- **THEN** the report reads as a statement about the deliverable ("done and logged; the other five stay queued") with the collection paths as citations, not as a description of tool calls

### Requirement: Bootstrap exposes the Planning inventory and the plan simple action

Bootstrap SHALL teach that `plan_memory(action="inspect")` without a collection returns the Planning inventory, that an observation is resolved to a Planning item with `query` filtered on `title` or the natural-key fields plus `lifecycle` and `status`, and SHALL list `plan` among the simple front-door actions with the same phrasing `SKILL.md` uses. Reading the inventory SHALL create nothing.

#### Scenario: Fresh session finds the Planning surface

- **WHEN** a session starts with no collection named and the user refers to "the next one" in a workstream
- **THEN** bootstrap routes the agent to the Planning inventory and a bounded query rather than to a question about which collection to use

### Requirement: The client capture nudge recognises lifecycle writes

Where a client hook enforces the capture contract, its detector of knowledge-base writes SHALL count `record_memory`, `plan_memory` and `observe_memory` mutations as writes, and its reminder SHALL name the lifecycle classes in one line. The hook SHALL remain structural: it decides whether a write happened, never what should have been written.

#### Scenario: A turn that filed the record is not nudged

- **WHEN** a turn's only knowledge-base mutation is a `record_memory` append
- **THEN** the capture nudge does not fire

#### Scenario: The reminder names the classes

- **WHEN** the capture nudge fires
- **THEN** its reminder names stated intent → Planning and observed outcome → Records alongside the existing classes, in one line
