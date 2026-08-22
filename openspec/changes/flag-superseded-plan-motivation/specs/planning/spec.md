## MODIFIED Requirements

### Requirement: Planned-versus-recorded review over shipped primitives

Exomem SHALL expose a read-only planned-versus-recorded review as `review_memory(mode="plan-progress")`, reaching MCP, REST, and CLI through the existing generated command without a signature change. The review SHALL select Planning items that are `lifecycle: active`, `status: active`, `commitment: committed`, and carry at least one `progress_evidence` descriptor or at least one `motivation` reference. Status and commitment SHALL be selected through the shipped bounded Planning query grammar; the presence of `progress_evidence` and of `motivation` SHALL be tested on the returned item, because an otherwise valid Planning manifest need not declare either optional field.

The `motivation` column SHALL be projected only where `motivation` is the governed reference list — a declared field of array type. A collection that declares `motivation` in any other shape SHALL be reviewed exactly as a collection that does not declare it at all, and SHALL NOT be refused. Widening the selection SHALL NOT relax the lifecycle, status, or commitment conditions.

For each selected item the review SHALL execute every authored `progress_evidence` descriptor as the named Records saved view over the referenced Records collection, using only shipped primitives: released-manifest resolution, the governed Records query path, and the default-deny query envelope. The review SHALL NOT define a new query grammar, filter operator, saved-view feature, storage adapter, index, cache, or persisted artifact, and SHALL NOT traverse a Records manifest `links.plans` descriptor in the opposite direction.

An optional `collection` selector MAY restrict the review to one Planning collection. Absent a selector, the review SHALL scan authorized Planning collections. A Planning collection that cannot be resolved or queried SHALL increment a bounded unavailable counter and SHALL disclose no path, title, identity, or existence.

#### Scenario: Committed active item with evidence is reviewed
- **WHEN** an active committed Planning item names a Records collection saved view as progress evidence and that collection holds matching records
- **THEN** the review presents the item's authored intent together with the exact number of records the bound saved view matched
- **AND** it names the collection, the role, and the view for every executed binding

#### Scenario: Items outside the reviewed selection are absent
- **WHEN** a Planning collection also contains candidate, planned, blocked, uncommitted, archived, or items carrying neither evidence nor motivation
- **THEN** those items do not appear in the review and no placeholder, stub, or inferred entry is produced for them

#### Scenario: Undeclared evidence field does not refuse the collection
- **WHEN** a Planning manifest does not declare the optional `progress_evidence` field
- **THEN** the review returns zero reviewed items for that collection instead of refusing it

#### Scenario: Committed active item carrying only motivation is reviewed
- **WHEN** an active committed Planning item declares one or more `motivation` references and binds no `progress_evidence` descriptor
- **THEN** the item is reviewed, its motivation references are presented, and its evidence list is empty

#### Scenario: Legacy free-text motivation is neither read nor refused
- **WHEN** a Planning manifest declares `motivation` as a field that is not an array
- **THEN** the review reports zero motivation references for every item in that collection, discloses no part of the authored value, and does not refuse the collection

### Requirement: Bound evidence executes under authorization-precedes-resolution

Every cross-profile evidence hop SHALL authorize before it resolves and SHALL resolve before any canonical Records source is parsed. The review SHALL resolve an evidence target only as a fully released Records manifest, SHALL authorize the named saved view before the query runs, and SHALL read observed numbers only from a non-withheld default-deny query envelope.

An evidence binding that cannot be executed SHALL be reported with exactly one bounded reason from `collection_unavailable`, `profile_mismatch`, `view_unavailable`, `query_unavailable`, `result_withheld`, or `budget_exhausted`. A missing collection and a withheld collection SHALL both report `collection_unavailable`, so the review cannot be used to probe for hidden collections. An unresolved binding SHALL contribute no observed numbers and SHALL NOT remove the item from the review.

The same rule SHALL govern every `motivation` reference. A motivation reference SHALL be authorized before its page is parsed, and a motivation reference that does not resolve to exactly one authorized page SHALL be reported with the single bounded reason `motivation_unavailable`. A reference the vault does not hold, a reference the vault holds in more than one page, a malformed reference, a reference whose page a governance decision blocks, and a reference whose page an access tier excludes SHALL be indistinguishable from one another in every part of the response: the same reason, no path, no title, no page count, no successor, and no separate counter. A reference SHALL be authorized before its uniqueness is decided, so that the presence of an unreleased page carrying the same identity cannot change the outcome the reader observes. Resolution work SHALL be a function of the references asked for alone and SHALL NOT vary with which of them the vault holds or with who may read the pages holding them, so the effort a review expends discloses nothing its response withholds. Resolution SHALL create, rebuild, or refresh no index, cache, or sidecar.

The review SHALL be bounded independently of vault size. It SHALL cap the number of reviewed items, cap the number of distinct evidence-query executions per call, execute each distinct `(collection, view)` pair at most once per call and reuse the result, and report truncation explicitly rather than returning a silently partial answer.

Motivation resolution SHALL carry its own budget, separate from the evidence-execution budget, so `budget_exhausted` keeps meaning that a Records view was skipped. The motivation budget SHALL be applied to the distinct references of the retained items after ordering and truncation, and its verdict SHALL be computed from a counter before any target is consulted, so a reference reported as `motivation_budget_exhausted` is independent of whether its target exists. Each distinct reference SHALL be resolved at most once per call.

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

#### Scenario: Every unresolvable motivation reference reads the same
- **WHEN** one item cites a reference the vault does not hold, another cites a reference two pages both claim, another cites a reference whose page a governance ceiling blocks, and another cites a reference whose page the access tier excludes
- **THEN** all four report `motivation_unavailable` with no path, title, page count, or successor, and their entries are identical once each item's own authored reference is set aside

#### Scenario: A hidden page cannot be detected through the review
- **WHEN** two vaults are identical except that one holds an unreleased page carrying a cited memory identity
- **THEN** a review of each vault returns the same response apart from its generation timestamp, including every counter and the whole unavailable tally

#### Scenario: Motivation budget truncates independently of the evidence budget
- **WHEN** the retained items together cite more distinct memory references than the motivation budget allows
- **THEN** the unconsulted references report `motivation_budget_exhausted`, the response reports motivation truncation as true, the evidence-execution budget and its `budget_exhausted` reason are unaffected, and no reference is silently dropped

### Requirement: Plan-progress review presents divergence without adjudicating it

The review SHALL present authored intent next to observed numbers and SHALL leave every judgment to the human or calling agent. For each reviewed item it SHALL return the canonical `exomem://plan/<collection-uuid>/<plan-uuid>` reference, the authored intent fields it echoes unchanged, the ordered evidence bindings with their role, view, opaque collection reference, and either observed numbers or an unavailable reason, the ordered motivation entries, and a divergence block of non-negative integers containing `evidence_bindings`, `resolved_bindings`, `unresolved_bindings`, `progress_bindings`, `completion_bindings`, `progress_observations`, `completion_observations`, `motivation_refs`, `motivation_resolved`, `motivation_unresolved`, and `motivation_superseded`.

The four motivation counts SHALL be present on every reviewed item, including an item that cites no knowledge, where they SHALL read zero. They SHALL be counts and never flags, and `motivation_refs` SHALL equal `motivation_resolved` plus `motivation_unresolved` while `motivation_superseded` SHALL NOT exceed `motivation_resolved`. No motivation entry field SHALL be named `ref`, because a plan-progress response mints no triageable reference.

Observed numbers per executed binding SHALL be exactly the matched count, the returned count, the truncation flag, and the canonical collection and snapshot identifiers. The review SHALL NOT return Records rows, bodies, or item identities.

A bound saved view's own declared aggregate SHALL NOT be passed through, whatever its shape. The aggregate grammar admits a latest-row selector that carries a complete record including its identity and version, distinct-value and grouped-value shapes that carry record values, and mean/sum/min/max shapes that carry a derived statistic — rows, identities, and a score-shaped value respectively, all three of which this review refuses. The matched count is computed identically under every aggregate shape, so refusing the aggregate withholds no count from the reader.

The response SHALL be marked derived and read-only, SHALL carry a generation timestamp, and SHALL report the number of Planning collections scanned, the number of items matched, the number of items presented, item truncation, binding truncation, the number of distinct memory references consulted, motivation truncation, and a bounded tally of unavailable reasons.

Items SHALL be ordered deterministically by collection identity then plan identity, and evidence SHALL keep its authored descriptor order. Motivation entries SHALL keep their authored reference order. Ordering SHALL carry no ranking meaning.

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

#### Scenario: Motivation counts are integers on every item
- **WHEN** the review returns an item that cites motivating knowledge and an item that cites none
- **THEN** both carry `motivation_refs`, `motivation_resolved`, `motivation_unresolved`, and `motivation_superseded` as non-negative integers, the second reads zero for all four, and no value in the divergence block is a boolean

#### Scenario: Review changes nothing it reads, including the reference sidecar
- **WHEN** the review resolves motivation references over a vault whose reference sidecar already exists
- **THEN** the sidecar's bytes are unchanged afterwards, and where no sidecar existed none is created

## ADDED Requirements

### Requirement: Plans premised on superseded knowledge surface for review

A Planning item's `motivation` references SHALL be read by the plan-progress review, so a committed plan premised on knowledge the vault has since replaced stops executing unexamined. For each authored reference the review SHALL report the reference as the reader authored it, whether it resolved, and — when it resolved — whether the vault has superseded the page it names. The entry field carrying the reference SHALL be named `memory`.

Supersession SHALL be read as the target page's authored `status` being `superseded`, and from nothing else. A non-empty `superseded_by` on a page whose status was never changed SHALL NOT be treated as supersession; reporting that inconsistency belongs to the audit surface.

The review SHALL NOT name, resolve, describe, or count the successor of a superseded page. It SHALL NOT derive a verdict, health value, score, ratio, or ranking from supersession, SHALL NOT reorder items by it, SHALL NOT mint a triageable reference for it, and SHALL NOT enqueue it as an attention category. Presenting the count is the whole claim; adjudication stays with the reader.

Reading a plan's motivation SHALL leave the vault byte-identical, including every derived index and sidecar that a canonical byte census would not cover.

#### Scenario: Plan citing superseded knowledge is surfaced
- **WHEN** an active committed Planning item cites a memory reference whose page the vault has marked `status: superseded`
- **THEN** the item's motivation entry reports the authored reference, resolution, and supersession, and its divergence block reports one motivation reference, one resolved, none unresolved, and one superseded

#### Scenario: Plan citing live knowledge is surfaced as unsuperseded
- **WHEN** an active committed Planning item cites a memory reference whose page carries any status other than `superseded`
- **THEN** the entry reports the reference as resolved and not superseded, and `motivation_superseded` reads zero

#### Scenario: A superseded page's successor is never named
- **WHEN** a superseded target page declares the page that replaced it
- **THEN** no part of the response names, links, describes, or counts that successor

#### Scenario: A hand-edited supersession pointer is not supersession
- **WHEN** a target page declares `superseded_by` while its own `status` is unchanged
- **THEN** the review reports the reference as resolved and not superseded

#### Scenario: Reviewing motivation writes nothing
- **WHEN** a review resolves motivation references across a vault
- **THEN** no plan, page, manifest, index, cache, or reference sidecar is created, modified, or deleted, and repeating the review changes nothing further
