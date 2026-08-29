# records Specification

## Purpose
TBD - created by archiving change add-first-class-records. Update Purpose after archive.

## Requirements

### Requirement: Records are observed state, not interpretation
The Records profile SHALL represent what happened or what was measured: events, sessions, symptoms, measurements, transactions, state changes, status history, and durable operational facts. It SHALL NOT infer goals, priority, success, failure, regression, diagnoses, or personal judgments. Conclusions derived from Records SHALL be compiled explicitly into Notes with links back to the collection or items.

#### Scenario: Harder-band rep drop is not labelled regression
- **WHEN** a training query shows fewer repetitions after a change to a harder band
- **THEN** generic Records output presents the observed band and repetition values without calling the change progress or regression

#### Scenario: Symptom record remains a fact
- **WHEN** a user records a symptom and severity
- **THEN** the item remains an observed report and Exomem does not generate a diagnosis or medical conclusion

### Requirement: Records remain distinct from existing layers
Sources SHALL remain externally received raw material; Evidence SHALL remain proof-bearing artifacts; Notes SHALL remain distilled conclusions; Entities SHALL remain stable reusable identities; Planning SHALL remain intended future state; Records SHALL remain observed state; Review SHALL compare intent with observation; and Imported SHALL remain adoption staging. A Record MAY link to any relevant source, evidence artifact, note, entity, plan, protocol, project, or asset without becoming that linked object.

#### Scenario: Receipt links to maintenance record
- **WHEN** a vehicle-maintenance record links to a purchase receipt
- **THEN** the maintenance event remains a Record and the proof-bearing receipt remains Evidence

#### Scenario: Imported tracker is adopted into Records
- **WHEN** a user explicitly adopts an imported tracker for ongoing use
- **THEN** its live collection is governed as Records and Imported remains only provenance/staging rather than the permanent mutable home

### Requirement: Records are exempt from compiled-note minimums
Record manifests, canonical logs, record-item files, datasets, templates, histories, and generated views SHALL be governed mutable data and SHALL be exempt from compiled-note semantic-unit minimums unless an artifact is intentionally compiled into a Note.

#### Scenario: Valid record item needs no claim block
- **WHEN** a schema-valid record item is appended to a collection
- **THEN** the write does not require an Observation, Claim, Decision, or other compiled-note semantic unit

### Requirement: Tracker compatibility is additive and explicit
Existing `type: tracker` Markdown and compatible chronological logs SHALL remain directly inspectable at collection level by a caller-supplied path without forced migration. This delivery SHALL NOT add vault-wide legacy-tracker enumeration. Attaching an adjacent collection manifest with a complete adapter descriptor SHALL enable item query/mutation without rewriting the canonical log, undated archive, notation, legend, or historical templates. A manifest-less tracker SHALL NOT be item-parsed using guessed domain grammar. Explicit identifiers SHALL be added prospectively.

#### Scenario: Manifest-less tracker stays collection-level only
- **WHEN** a tracker has no adjacent manifest or explicit complete adapter descriptor
- **THEN** Exomem can discover and inspect it at collection level but refuses item query/mutation rather than guessing block or field semantics

#### Scenario: Adjacent descriptor unlocks legacy item query without rewrite
- **WHEN** a tracker has an adjacent manifest declaring its section, heading grammar, fields, delimiter, insertion direction, schema, and natural key
- **THEN** Exomem can query uniquely identified legacy items while leaving the tracker file unchanged

#### Scenario: Removing manifest leaves tracker usable
- **WHEN** a user rolls back Records adoption by removing an adjacent manifest
- **THEN** the original tracker and templates remain ordinary usable Markdown with no data loss

### Requirement: X3 chronological-log acceptance
The implementation SHALL support the existing newest-first X3 training log plus an adjacent explicit manifest as a real acceptance fixture. It SHALL preserve `Records/Health/X3/Training Log.md` as canonical and byte-identical before a requested mutation, keep `Historical Reps (undated).md` separate, exclude templates and fenced fake headings/rows, preserve the existing notation and legend, accept completed, partial, and aborted Push/Pull sessions, and retrieve date, workout type, movement, band, and raw repetition notation including blank, `+`, `!`, and `?` values. No delivery step SHALL force migration to files or a dataset.

#### Scenario: Agent append targets the Sessions section
- **WHEN** a valid structured X3 session is appended by an agent
- **THEN** it is inserted immediately under `## Sessions (newest first)` in the existing notation rather than appended after the legend or at end of file

#### Scenario: Aborted session remains queryable
- **WHEN** a session records an aborted status and partial movements
- **THEN** its observed status and available movements are queryable without inventing missing repetitions

#### Scenario: Explicit marker survives reorder and date correction
- **WHEN** a marked session is reordered or its date is corrected manually
- **THEN** its collection-scoped item identity remains stable and its exact-source item version and collection snapshot change

#### Scenario: Duplicate legacy session key is readable but ambiguous
- **WHEN** two unmarked sessions have the same manifest-declared natural key
- **THEN** both remain visible in bounded query output with ambiguity metadata and neither is selected for targeted update

#### Scenario: Undated archive is not live progress
- **WHEN** a query uses the live X3 collection without explicitly including historical undated material
- **THEN** `Historical Reps (undated).md` does not contribute dated progress rows or aggregates

#### Scenario: Derived CSV response does not change representation
- **WHEN** a user requests X3 query output as derived CSV
- **THEN** the response preserves source/item provenance and the training log remains canonical with no representation migration action in this delivery

#### Scenario: Parser ignores misleading text outside the declared grammar
- **WHEN** the log contains fenced fake headings, delimited legend rows, template text, or headings outside the declared Sessions section
- **THEN** the adapter excludes them from Record items and movement rows

### Requirement: Generic second-domain acceptance
The implementation SHALL include a one-Markdown-file-per-item vehicle-maintenance fixture that exercises materially different fields and targeted update behavior. Its schema SHALL include occurred date, asset relation, integer odometer, provider, service array, decimal amount, three-letter currency, status, optional receipt/Evidence link, and nullable next-due date/odometer.

#### Scenario: Maintenance correction targets one item
- **WHEN** a user corrects the odometer or next-due value on one identified maintenance event with current guards
- **THEN** only that item changes and unrelated events, receipts, templates, and collection metadata remain unchanged

#### Scenario: Duplicate-date events remain distinct
- **WHEN** two vehicle events occurred on the same date
- **THEN** their explicit stable item identities keep them distinct without using date alone as a universal key

#### Scenario: Withheld item cannot influence total
- **WHEN** one maintenance item is withheld and query requests total amount or latest odometer
- **THEN** authorization excludes that file before reduction and its values do not affect totals, latest selection, categories, or pagination metadata

#### Scenario: Manual status correction is visible immediately
- **WHEN** a human directly changes one item from scheduled to completed while preserving its explicit key
- **THEN** the next fresh query returns the corrected status and a changed item/source version without rewriting unrelated files

### Requirement: Query-only dataset acceptance
The implementation SHALL include an unrelated CSV, TSV, or JSON fixture proving dataset-backed collection queries, declared-key projection, hard row and distinct caps, deterministic ordering, source-snapshot pagination, and direct-edit invalidation. Dataset append/update SHALL refuse as unsupported in this delivery.

#### Scenario: Dataset query returns exact declared fields
- **WHEN** the fixture is queried by its declared time, category, and numeric fields
- **THEN** the response returns the expected bounded rows, collection-scoped declared keys, source snapshot, and capped aggregate metadata

#### Scenario: Direct dataset edit invalidates continuation
- **WHEN** a human edits the canonical dataset after a first page is returned
- **THEN** a continuation bound to the old snapshot refuses rather than skipping or duplicating rows

### Requirement: Templates are ordinary independent scaffolds
A collection MAY recommend ordinary Markdown templates, default properties, validation guidance, entry examples, and future form descriptors. The binding schema SHALL live independently in the collection contract. Changing a template SHALL NOT rewrite history; using a template SHALL NOT require an agent, Obsidian, or any plugin; and returning a template SHALL pass the same governance boundary as other files.

#### Scenario: Obsidian insertion stays ordinary
- **WHEN** a vault is configured to use `Knowledge Base/Templates/` and a user invokes `Templates → Insert template`
- **THEN** an X3 Push or Pull template can be inserted and manually completed without Exomem runtime involvement

#### Scenario: Template change does not change schema or history
- **WHEN** a user edits a collection template
- **THEN** historical records remain byte-identical and the collection schema changes only through an explicit manifest/schema update

### Requirement: Pack Records guidance does not fork core
Existing knowledge-pack guidance MAY suggest Records use, folders, fields, templates, query views, capture guidance, and review workflows for domains such as health, personal records, business, and creative work. This delivery SHALL use the current validated pack extension surface; machine-readable collection blueprints and activation are deferred. Packs SHALL NOT introduce domain-specific storage engines, rigid taxonomies, or hidden migrations.

#### Scenario: Health pack guides without activating silently
- **WHEN** the health pack is selected
- **THEN** bootstrap may explain training, symptom, measurement, protocol, or appointment Records but creates or migrates none without explicit user action

#### Scenario: Pack language stays advisory
- **WHEN** personal-records guidance discusses vehicle-maintenance fields
- **THEN** the adjacent collection manifest remains the binding schema and the generic collection validator remains the only enforcement path

### Requirement: Opaque Planning reference contract
A Record collection manifest MAY store one or more opaque Planning references paired with bounded Records query descriptors. A Record MAY link to a plan, goal, initiative, protocol, project, asset, person, entity, or decision. A Planning reference MAY additionally carry a bounded `join`: one to four pairs mapping a declared record field to a plan field name, where the plan-side name is bounded non-empty text that Records does not check against the target. Records SHALL validate, round-trip, and governance-project these descriptors and the join without resolving Planning, comparing intent with observations, copying record history, inferring progress/completion, or mutating either side. The join is an authored declaration consumed only by the attention surface's `unreflected_outcomes` family; no Records operation SHALL resolve it.

#### Scenario: Planning link round-trips without resolution
- **WHEN** a manifest stores an opaque Planning reference plus a bounded query descriptor
- **THEN** inspection returns the authorized descriptor unchanged and Records performs no Planning lookup or planned-versus-recorded comparison

#### Scenario: Join round-trips without resolution
- **WHEN** a manifest's Planning reference carries a join from a declared record field to a plan field
- **THEN** inspection returns the join unchanged, `describe` documents the shape, and Records performs no lookup of the referenced Planning collection

#### Scenario: Malformed join refuses
- **WHEN** a join names a record field the schema does not declare, has more than four pairs, or has an empty plan-side name
- **THEN** manifest validation refuses before acceptance and names the offending pair

#### Scenario: External software execution truth remains external
- **WHEN** a software initiative links to an accepted OpenSpec change, git state, tests, or deployment result
- **THEN** Records may preserve the observed outcome and Planning may preserve intent, while repository/OpenSpec artifacts remain execution truth

### Requirement: Neutral observed-state query views
Records query views SHALL display bounded observed values and provenance. Domain-specific interpretation SHALL require an explicit analysis or protocol layer and SHALL NOT be embedded in generic Records machinery. Planned-versus-recorded comparison SHALL live in the read-only plan-progress review rather than in Records machinery: a Records view SHALL remain the same neutral observed-state rendering whether it is read directly or read as bound Planning evidence, and Records SHALL NOT gain a comparison, progress, completion, or success semantic of its own.

#### Scenario: Three-month X3 view remains neutral
- **WHEN** a user asks for X3 progression over three months
- **THEN** the derived view shows chronological session, movement, band, and repetition values with source provenance and no unsupported performance judgment

#### Scenario: Vehicle latest-state view cites events
- **WHEN** a user asks for current mileage or next due maintenance
- **THEN** the result identifies the governing collection/query and the record item from which the latest value was derived

#### Scenario: A view read as plan evidence is the same view
- **WHEN** the same saved view is read directly through the Records command and read again as a Planning item's bound progress evidence
- **THEN** both reads run the same declared view over the same canonical snapshot and produce the same observed numbers
- **AND** the Records view definition, response shape, and neutrality are unchanged by having been used as evidence

### Requirement: Records authoring is self-describing and safely preflightable
The Records product command SHALL expose content-free `describe` and `validate` actions in addition to collection inspection and mutation. `describe` SHALL return the complete supported manifest contract, all closed enum values, exact open constraints, and generic minimal and nested-measurement examples. `validate` SHALL run the binding parser, Records-profile rule, safe-path rules, create-only checks, and scaffold checks without requiring a mutation reason, acquiring writer authority, writing an audit event, or changing the vault.

#### Scenario: Generic client creates the first collection without guessing
- **GIVEN** an empty sample vault and a client with no repository, skill, or fixture-manifest access
- **WHEN** the client calls `describe`, authors a manifest only from that response, and calls `validate`
- **THEN** validation succeeds without any guessed field name or enum value
- **AND** the same manifest can be created, inspected, and appended through the public command

#### Scenario: Laboratory example teaches nested observed measurements
- **WHEN** a client requests the Records authoring contract
- **THEN** the complete example shows a panel date, provenance link, and child analytes with values, inequalities, units, ranges, cancellation, and specimen qualifiers
- **AND** it contains no diagnosis, interpretation, private identity, or domain-specific storage engine

#### Scenario: Validation performs no mutation
- **WHEN** a valid or invalid manifest is submitted to `validate`
- **THEN** no manifest, source, directory scaffold, activity event, governance receipt, or writer-lease mutation is produced

### Requirement: Records inventory is available before a selector is known
Calling `record_memory(action="inspect")` without a collection SHALL return a bounded, governance-filtered inventory of releasable first-class Records manifests and exact Records-layer legacy trackers. Supplying a collection SHALL preserve targeted inspection behavior. Inventory SHALL NOT parse legacy item grammar or return item contents.

#### Scenario: Empty vault inventory is useful and empty
- **WHEN** a generic client inspects an empty Records layer without a collection selector
- **THEN** it receives empty first-class and legacy inventories plus the route to `describe`

#### Scenario: Denied inventory candidate stays absent
- **WHEN** governance withholds a first-class manifest or legacy tracker
- **THEN** inventory does not reveal its path, title, identity, type, or existence

### Requirement: Installed-artifact Records release journey
The release gate SHALL build and install the Exomem wheel in a clean environment, initialize a temporary human-owned vault, discover `record_memory` through a real MCP transport, and complete one bounded chronological-log Records journey without importing product code from the source checkout. The journey SHALL preserve ordinary template/manual entry, perform a guarded append and targeted guarded update, return a structured query and ephemeral derived view, round-trip an opaque Planning evidence descriptor, survive a server restart, observe a direct canonical-file edit, report its positive audit gap without silently repairing it, and keep row-only content out of ordinary semantic recall. Detailed domain, governance-reduction, mutation-crash, ambiguity, dataset, and scale cases MAY remain focused release-blocking tests rather than duplicate black-box scenarios.

#### Scenario: Manual and guarded Records state survives restart
- **WHEN** the installed product queries a manually inserted template block, appends and updates an identified item through `record_memory`, stops, receives a direct canonical-log edit, and restarts
- **THEN** the next MCP query returns the manual entry, guarded mutation, and direct edit from the same canonical Markdown file, and inspection reports an audit gap without migration, silent repair, or a competing source of truth

#### Scenario: Installed collection retains Planning and recall boundaries
- **WHEN** the installed product inspects the collection and asks ordinary memory for content that exists only in a raw record row
- **THEN** the opaque Planning reference/query descriptor round-trips unchanged and ordinary semantic recall does not return the raw canonical log

#### Scenario: Public inspection exposes only governed opaque Planning descriptors
- **WHEN** `record_memory(action="inspect")` reads a released collection containing a bounded Planning reference/query descriptor
- **THEN** `inspect.contract.plans` strictly reconstructs and returns that descriptor through the existing governance projection, without invoking an internal-only API, resolving or target-authorizing the opaque Planning reference, or revealing a withheld link-typed query value

#### Scenario: Inspection Planning projection remains default-deny
- **WHEN** a projected inspection contains an extra, malformed, over-limit, noncanonical, or untyped nested Planning descriptor value
- **THEN** the inspection egress validator refuses the invalid payload instead of passing the nested value through

#### Scenario: Unresolved remote Records access fails closed
- **WHEN** an auth-required installed HTTP MCP surface receives a protocol-valid unauthenticated raw `POST /mcp` JSON-RPC `tools/call` request naming `record_memory`
- **THEN** authenticated ingress returns exactly 401 with a Bearer challenge naming the local protected-resource metadata URL, reveals no collection, rows, Planning references, paths, or aggregates in the raw response, and leaves the separate no-auth local HTTP harness in explicit owner mode

### Requirement: Records presentation remains neutral observed state

Managed presentation SHALL display only selected canonical observed fields. It SHALL preserve nulls, inequalities, units, ranges, cancellation/status, precision, qualifiers, and provenance without inventing bounds, certainty, diagnoses, interpretation, ranking, or domain meaning. Unrenderable values SHALL refuse rather than coerce arbitrarily.

#### Scenario: Laboratory panel is readable without interpretation
- **WHEN** panel declares summary, measurements table, notes, and collapsed provenance
- **THEN** body shows exact observations with no diagnosis, judgment, reconstructed range, or advice

#### Scenario: Source qualifiers survive rendering
- **WHEN** children include less-than, cancellation, null, or specimen qualifier
- **THEN** table preserves each distinction

### Requirement: Records queries safely project and expand one child field

Before filtering, sorting, aggregation, pagination, rendering, or returning any query, every child array named by `record_presentation` SHALL be replaced in the governed snapshot with its type-valid declared columns and audience-specific link projection. Undeclared or withheld nested values SHALL never enter expanded or unexpanded query machinery. Type mismatch SHALL refuse rather than fall back to raw objects.

`expand_child` SHALL name a table field in `record_presentation`. Each child becomes one bounded row containing parent values except selected container; parent identity/system fields; `parent_record_id`, `child_field`, `child_index`; and only the safe child projection. A hard total child cap SHALL precede materialization.

`expand_children: true` SHALL resolve only one Markdown-log container or exactly one Records Markdown-item table. Open object arrays and datasets are ineligible. No/multiple eligible fields or both selectors SHALL refuse actionably. False/omitted expansion SHALL preserve parent rows containing only the safe nested projection under the response cap.

#### Scenario: Markdown-item measurements expand instead of disappearing
- **WHEN** seven panels have `measurements` and caller uses `expand_child: measurements`
- **THEN** exact child columns paginate with parent correlation/snapshot rather than return zero

#### Scenario: Boolean resolves one table
- **WHEN** Records presentation has exactly one table and client sends `expand_children: true`
- **THEN** it behaves exactly like explicit selection

#### Scenario: Multiple tables require selection
- **WHEN** two tables exist and only boolean true is sent
- **THEN** query refuses with releasable selectors and no partial rows

#### Scenario: Child pagination avoids parent-array overflow
- **WHEN** parent array exceeds non-expanded cap but child rows fit
- **THEN** expansion omits the container and provides bounded continuation pages without duplicate/skip

#### Scenario: Unauthorized parent produces no child facts
- **WHEN** governance withholds a parent
- **THEN** expansion does not parse, count, render, or return its children

#### Scenario: Expanded and unexpanded egress match
- **WHEN** child objects have extra keys or a declared link resolves to a withheld target
- **THEN** both query modes exclude undeclared/withheld nested values before any observable query operation

#### Scenario: Policy changes query but not file
- **WHEN** link policy changes with identical canonical bytes
- **THEN** query follows current authorization while managed bytes/hash remain unchanged

### Requirement: Agents infer Records participation from observed state

The agent-facing Records contract SHALL route durable observed events and state to `record_memory` from semantic fit rather than requiring the user to say “save”, “log”, “record”, or “Records”. Covered observations SHALL include measurements, sessions, symptoms, transactions, maintenance events, inventory changes, status history, and other attributable facts. The routing contract SHALL preserve the existing boundary: future intent belongs to Planning, received raw material to Sources, proof-bearing artifacts to Evidence, and conclusions to Notes.

When exactly one existing collection is compatible and the observation is sufficiently identified for that schema, an agent operating under a proactive engagement policy MAY append or update it and SHALL report the mutation. Ambiguous collections, missing required identity/date/provenance, or uncertain ownership SHALL produce one focused clarification. When no compatible collection exists, the agent SHALL propose a collection and SHALL NOT silently create a long-lived schema.

#### Scenario: Measurement is inferred without a magic verb
- **WHEN** a user states a new dated measurement in context without asking to save, log, record, or use Records and exactly one existing collection accepts it
- **THEN** the agent routes the observation through `record_memory`, preserves the observed value without interpretation, and reports the committed mutation

#### Scenario: No collection produces a proposal
- **WHEN** a durable observed event fits Records but no compatible collection exists
- **THEN** the agent uses Records discovery and authoring guidance to propose a collection and does not write the event into a Note, Source, Evidence artifact, or silently invented collection

#### Scenario: Competing collections require one clarification
- **WHEN** two releasable Records collections are equally compatible with the observed event
- **THEN** the agent asks one focused collection-selection question and performs no guessed mutation

#### Scenario: Interpretation remains outside Records
- **WHEN** a Records query supplies values that support a possible conclusion
- **THEN** the Records response remains neutral observed state and any durable conclusion is compiled explicitly into a linked Note

### Requirement: Records serves cross-profile review as an ordinary governed reader

A planned-versus-recorded reviewer SHALL reach Records only through the existing governed read path: resolution of a fully released manifest, authorization of the named saved view, authorization of the canonical source before it is parsed, and the default-deny query envelope. Records SHALL NOT gain a review-specific query surface, filter operator, saved-view feature, bulk export, or relaxed authorization path, and SHALL NOT resolve Planning, compare intent with observation, copy plan state, or mutate either side.

A reviewer SHALL take only bounded provenance and counts from the envelope — the matched count, the returned count, the truncation flag, and the collection and snapshot identifiers — and SHALL NOT receive record rows, bodies, item identities, record values, or a view's declared aggregate through the review. A withheld envelope SHALL yield no numbers at all rather than partial ones.

#### Scenario: Review cannot widen Records authorization
- **WHEN** governance withholds a Records collection, its canonical source, or the named saved view
- **THEN** the cross-profile review receives the same refusal an ordinary Records reader receives and obtains no rows, counts, snapshot, or existence signal

#### Scenario: Review reads counts, not records
- **WHEN** a bound saved view matches many records
- **THEN** the review reports the exact matched and returned counts, the truncation flag, and the snapshot identifier
- **AND** no record row, body, or item identity appears in the review response

#### Scenario: An aggregating view discloses no more than a plain one
- **WHEN** a bound view declares an aggregate that would return a full record row, distinct record values, grouped values, or a mean
- **THEN** the review discloses exactly the same fields it discloses for a view with no aggregate
- **AND** the aggregate's row, values, and statistic are withheld entirely rather than partially projected

#### Scenario: Records keeps no review state
- **WHEN** a plan-progress review executes bound Records views repeatedly
- **THEN** no Records manifest, canonical source, audit head, mutation receipt, or query cache is written, and the collection's next ordinary query is unaffected

### Requirement: Records presentation recipes have one active owner

A Records Markdown-item manifest SHALL declare at most one of legacy `record_presentation` and shared `item_presentation`. Existing legacy recipes SHALL retain their current rendering and safe child-projection semantics. Conversion SHALL require a complete guarded manifest revision and a transactional replacement of every owned block; implicit dual rendering is forbidden.

#### Scenario: Existing Records manifest remains compatible

- **WHEN** an existing collection declares only a valid `record_presentation`
- **THEN** validation, mutation, query child projection, and managed rendering continue under the existing contract

#### Scenario: Dual recipes refuse

- **WHEN** a Records manifest declares both presentation recipe forms
- **THEN** validation refuses before reading or writing item content

#### Scenario: Conversion is explicit

- **WHEN** a guarded revision replaces `record_presentation` with `item_presentation`
- **THEN** the revision preview identifies every affected block and publication leaves exactly one current managed block per applicable item

### Requirement: Human-owned Records representation remains neutral observation

Records `item_filename` and `item_presentation` SHALL preserve exact observed values, nulls, inequalities, units, ranges, cancellation or status, precision, qualifiers, and provenance. They SHALL NOT add interpretation, diagnosis, ranking, reconstructed bounds, confidence, or advice. A field that cannot be represented without semantic coercion SHALL refuse rendering.

#### Scenario: Event filename carries identity but not mutable status

- **WHEN** a collection's natural key includes occurrence date, event title, and immutable event kind
- **THEN** its filename may render those values but excludes usability, approval, processing, and other mutable state

#### Scenario: Readable body preserves qualifiers

- **WHEN** selected Record values contain null, less-than, cancellation, unit, or source qualifiers
- **THEN** the managed body preserves each distinction exactly and adds no explanation of what it means

### Requirement: Records inspection reports legacy and shared representation debt

Records inspection SHALL apply the shared representation diagnostics to both presentation forms and SHALL continue to find owned legacy markers after the recipe is removed from the current manifest. The absence of a body recipe SHALL NOT make a stale or orphan block healthy.

#### Scenario: Removed legacy recipe remains inspectable

- **WHEN** a collection contains a legacy managed block but its current manifest declares no presentation recipe
- **THEN** Records inspection reports the orphan and identifies explicit cleanup or manifest restoration as remediation

### Requirement: Records inspection surfaces observed free-string vocabulary

Collection inspection SHALL report, for every declared string-typed field that declares no
`enum`, a bounded summary of the distinct values the authorized items already carry,
paired with each value's occurrence count. The summary SHALL be additive: every existing
inspection key keeps its shape and meaning.

Values SHALL be counted by their full text with surrounding whitespace removed, and empty
results SHALL be ignored. Counting SHALL NOT key on a shortened form: two distinct values
that share a prefix long enough to collide once shortened SHALL remain two entries with
their own counts.

The summary SHALL be bounded independently of collection size. At most 20 distinct values
per field SHALL be emitted, and when a field carries more, the retained values SHALL be the
most frequent ones, ranked by descending count and breaking ties by ascending value, so
that the cap drops the rarest terms rather than the ones the item pass happened to meet
last. A per-field truncation flag SHALL say so whenever the distinct-value cap binds.

Each emitted value SHALL be cut to a bounded display length and SHALL carry its own
always-present flag stating whether that cut applied. Fields declaring an `enum` SHALL NOT
be summarized, because the declaration is already the vocabulary.

The summary SHALL be derived from the same authorized item pass that produces the rest of
the inspection payload and SHALL carry the same serve-time filtering, under the same
path-granular authorization that pass already applies. Neither a value nor a count SHALL
reflect an item the requesting audience may not read. Inspection SHALL NOT perform an
additional or unbounded scan to produce it, and SHALL omit the summary entirely when no
item pass ran.

#### Scenario: Free-string field reveals the vocabulary already in use
- **GIVEN** a collection whose manifest declares a free-string field and whose items carry
  three or more distinct values for it
- **WHEN** a client inspects the collection
- **THEN** the response reports each distinct value with its occurrence count and with the
  flag stating that no display cut applied
- **AND** an appending agent can reuse an existing term instead of echoing the user

#### Scenario: Capped field keeps its most frequent terms
- **WHEN** a free-string field carries more distinct values than the cap admits
- **THEN** inspection reports exactly the capped number of distinct values, most frequent
  first, with ties broken by ascending value
- **AND** a term seen many times late in the item pass is retained over a term seen once
- **AND** the field's summary is flagged as truncated rather than silently partial

#### Scenario: Long values stay distinct and say they were cut
- **WHEN** two distinct values share a prefix long enough to collide at the display length
- **THEN** inspection reports two entries, each with its own count
- **AND** each entry is flagged as display-truncated, even though their emitted text matches

#### Scenario: Declared enum is not restated as observed usage
- **WHEN** a manifest declares a field as `enum`
- **THEN** inspection reports no observed-value summary for that field
- **AND** the declared values remain discoverable through the authoring contract

#### Scenario: Withheld item contributes neither a value nor a count
- **GIVEN** governance withholds one item of an otherwise released collection
- **WHEN** a client of that audience inspects the collection
- **THEN** a value occurring only on the withheld item is absent from the summary
- **AND** a value the withheld item shares with released items is counted from the released
  items alone
- **AND** the counts, the truncation flags, and the rest of the payload disclose no trace of
  that value or of the item's existence

#### Scenario: Unreadable collection claims no sweep
- **WHEN** inspection cannot parse the collection's canonical items
- **THEN** the response omits the observed-value summary entirely rather than reporting an
  empty one, which would claim a sweep that never ran
