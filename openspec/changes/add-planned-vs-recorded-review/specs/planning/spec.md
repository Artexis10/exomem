## ADDED Requirements

### Requirement: Planned-versus-recorded review over shipped primitives

Exomem SHALL expose a read-only planned-versus-recorded review as `review_memory(mode="plan-progress")`, reaching MCP, REST, and CLI through the existing generated command without a signature change. The review SHALL select Planning items that are `lifecycle: active`, `status: active`, `commitment: committed`, and carry at least one `progress_evidence` descriptor. Status and commitment SHALL be selected through the shipped bounded Planning query grammar; the presence of `progress_evidence` SHALL be tested on the returned item, because an otherwise valid Planning manifest need not declare that optional field.

For each selected item the review SHALL execute every authored `progress_evidence` descriptor as the named Records saved view over the referenced Records collection, using only shipped primitives: released-manifest resolution, the governed Records query path, and the default-deny query envelope. The review SHALL NOT define a new query grammar, filter operator, saved-view feature, storage adapter, index, cache, or persisted artifact, and SHALL NOT traverse a Records manifest `links.plans` descriptor in the opposite direction.

An optional `collection` selector MAY restrict the review to one Planning collection. Absent a selector, the review SHALL scan authorized Planning collections. A Planning collection that cannot be resolved or queried SHALL increment a bounded unavailable counter and SHALL disclose no path, title, identity, or existence.

#### Scenario: Committed active item with evidence is reviewed
- **WHEN** an active committed Planning item names a Records collection saved view as progress evidence and that collection holds matching records
- **THEN** the review presents the item's authored intent together with the exact number of records the bound saved view matched
- **AND** it names the collection, the role, and the view for every executed binding

#### Scenario: Items outside the reviewed selection are absent
- **WHEN** a Planning collection also contains candidate, planned, blocked, uncommitted, archived, or evidence-free items
- **THEN** those items do not appear in the review and no placeholder, stub, or inferred entry is produced for them

#### Scenario: Undeclared evidence field does not refuse the collection
- **WHEN** a Planning manifest does not declare the optional `progress_evidence` field
- **THEN** the review returns zero reviewed items for that collection instead of refusing it

### Requirement: Bound evidence executes under authorization-precedes-resolution

Every cross-profile evidence hop SHALL authorize before it resolves and SHALL resolve before any canonical Records source is parsed. The review SHALL resolve an evidence target only as a fully released Records manifest, SHALL authorize the named saved view before the query runs, and SHALL read observed numbers only from a non-withheld default-deny query envelope.

An evidence binding that cannot be executed SHALL be reported with exactly one bounded reason from `collection_unavailable`, `profile_mismatch`, `view_unavailable`, `query_unavailable`, `result_withheld`, or `budget_exhausted`. A missing collection and a withheld collection SHALL both report `collection_unavailable`, so the review cannot be used to probe for hidden collections. An unresolved binding SHALL contribute no observed numbers and SHALL NOT remove the item from the review.

The review SHALL be bounded independently of vault size. It SHALL cap the number of reviewed items, cap the number of distinct evidence-query executions per call, execute each distinct `(collection, view)` pair at most once per call and reuse the result, and report truncation explicitly rather than returning a silently partial answer.

#### Scenario: Withheld evidence target matches an absent one
- **WHEN** one item's evidence names a governance-withheld Records collection and another item's evidence names a collection that does not exist
- **THEN** both bindings report `collection_unavailable` with no path, title, identity, snapshot, or count
- **AND** both items remain in the review with the rest of their bindings intact

#### Scenario: Unknown saved view is a bounded reason, not a failure
- **WHEN** an evidence descriptor names a view the Records manifest does not declare
- **THEN** that binding reports `view_unavailable` and the review still returns every other binding and item

#### Scenario: Execution budget truncates explicitly
- **WHEN** the selected items together bind more distinct collection-and-view pairs than the per-call execution budget allows
- **THEN** the unexecuted bindings report `budget_exhausted`, the response reports binding truncation as true, and no binding is silently dropped

#### Scenario: Repeated binding executes once
- **WHEN** several reviewed items bind the same Records collection and the same saved view
- **THEN** that query executes once per call, every item reports the same exact numbers, and the shared execution counts once against the budget

### Requirement: Plan-progress review presents divergence without adjudicating it

The review SHALL present authored intent next to observed numbers and SHALL leave every judgment to the human or calling agent. For each reviewed item it SHALL return the canonical `exomem://plan/<collection-uuid>/<plan-uuid>` reference, the authored intent fields it echoes unchanged, the ordered evidence bindings with their role, view, opaque collection reference, and either observed numbers or an unavailable reason, and a divergence block of non-negative integers containing `evidence_bindings`, `resolved_bindings`, `unresolved_bindings`, `progress_bindings`, `completion_bindings`, `progress_observations`, and `completion_observations`.

Observed numbers per executed binding SHALL be exactly the matched count, the returned count, the truncation flag, and the canonical collection and snapshot identifiers. The review SHALL NOT return Records rows, bodies, or item identities.

A bound saved view's own declared aggregate SHALL NOT be passed through, whatever its shape. The aggregate grammar admits a latest-row selector that carries a complete record including its identity and version, distinct-value and grouped-value shapes that carry record values, and mean/sum/min/max shapes that carry a derived statistic — rows, identities, and a score-shaped value respectively, all three of which this review refuses. The matched count is computed identically under every aggregate shape, so refusing the aggregate withholds no count from the reader.

The response SHALL be marked derived and read-only, SHALL carry a generation timestamp, and SHALL report the number of Planning collections scanned, the number of items matched, the number of items presented, item truncation, binding truncation, and a bounded tally of unavailable reasons.

Items SHALL be ordered deterministically by collection identity then plan identity, and evidence SHALL keep its authored descriptor order. Ordering SHALL carry no ranking meaning.

#### Scenario: Divergence is exact integers
- **WHEN** a committed active item binds two progress views and one completion view, and the completion view matches nothing
- **THEN** the divergence block reports three bindings, two progress bindings, one completion binding, the exact progress match counts, and `completion_observations: 0`
- **AND** the response contains no percentage, ratio, score, estimate, verdict, or severity

#### Scenario: Aggregate-declaring evidence view leaks nothing
- **WHEN** a bound saved view declares a latest-row, distinct-value, grouped-value, or mean aggregate
- **THEN** the binding still resolves and reports its exact matched count
- **AND** no record row, record identity, item version, record value, or derived statistic from that aggregate appears anywhere in the response

#### Scenario: Authored health is echoed, never written
- **WHEN** a reviewed item declares `health: unknown` while its completion evidence matches nothing
- **THEN** the review echoes `unknown` unchanged, proposes no other health value, and the canonical item file is byte-identical afterwards

#### Scenario: Review changes nothing it reads
- **WHEN** the review runs over a vault containing Planning collections, Records collections, and evidence bindings
- **THEN** no plan, record, manifest, audit head, activity event, review state, index, or cache is created, modified, moved, or deleted
- **AND** the only writes a review may produce are the governance kernel's own disclosure receipts for the reads it performed, exactly as any ordinary governed read produces them

#### Scenario: Ordering implies no priority
- **WHEN** the review returns several items with very different observation counts
- **THEN** the items are ordered by collection and plan identity rather than by any measure of divergence
