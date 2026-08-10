## ADDED Requirements

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
A Record collection manifest MAY store one or more opaque Planning references paired with bounded Records query descriptors. A Record MAY link to a plan, goal, initiative, protocol, project, asset, person, entity, or decision. Records SHALL validate, round-trip, and governance-project these descriptors without resolving Planning, comparing intent with observations, copying record history, inferring progress/completion, or mutating either side.

#### Scenario: Planning link round-trips without resolution
- **WHEN** a manifest stores an opaque Planning reference plus a bounded query descriptor
- **THEN** inspection returns the authorized descriptor unchanged and Records performs no Planning lookup or planned-versus-recorded comparison

#### Scenario: External software execution truth remains external
- **WHEN** a software initiative links to an accepted OpenSpec change, git state, tests, or deployment result
- **THEN** Records may preserve the observed outcome and Planning may preserve intent, while repository/OpenSpec artifacts remain execution truth

### Requirement: Neutral observed-state query views
Records query views SHALL display bounded observed values and provenance. Domain-specific interpretation SHALL require an explicit analysis or protocol layer and SHALL NOT be embedded in generic Records machinery. Planned-versus-recorded comparison is outside this delivery.

#### Scenario: Three-month X3 view remains neutral
- **WHEN** a user asks for X3 progression over three months
- **THEN** the derived view shows chronological session, movement, band, and repetition values with source provenance and no unsupported performance judgment

#### Scenario: Vehicle latest-state view cites events
- **WHEN** a user asks for current mileage or next due maintenance
- **THEN** the result identifies the governing collection/query and the record item from which the latest value was derived
