## ADDED Requirements

### Requirement: Review Items Expose Selected Dimensions Before Filtering

Every queue listed by `list_review_items` SHALL annotate its semantic subject with dimension status: known, unknown, or not applicable.
Known subjects SHALL carry `dimensions`, including an exact empty list.
Unknown subjects SHALL omit `dimensions` and identify the unavailable dependency in diagnostics.
Unfiltered listings SHALL retain unknown and not-applicable items.

Each queue SHALL supply its subject from producer fields.
An explicit page-scoped reason SHALL select its page; otherwise, unanimous resolved unit reasons SHALL select their unit.
Conflicting or unresolved unit reasons SHALL remain unknown, including missing addresses on partitioned findings.
Producer unit addresses SHALL use the same parent identity as selected structures, including stable memory references on ID-bearing pages.
Relation suggestions SHALL concern the author page, upkeep its subject, and procedure approvals the uniquely resolved procedure page.
Vocabulary items SHALL use an explicit semantic page when present.
Import proposals, planning intents, and registry overlays SHALL be not applicable.

`list_review_items` SHALL accept optional `filters` restricted to `unit.dimension` predicates and Boolean composition through the shared structured-filter grammar.
One request-local adapter SHALL prepare selected parent structures and required family membership in batches through the existing readers.
Filtering SHALL evaluate the full expression on one target unit, with same-unit existential evaluation for a page.
A page without units and a not-applicable item SHALL match no dimension predicate, including negation.
Unavailable definitions, structures, required edges, or target addresses SHALL remain unknown under negation.
Display MAY union dimensions across units; filtering SHALL NOT evaluate that union as a unit.
Filter eligibility SHALL consume only its required dependencies, independently of optional annotation completeness.
Coverage SHALL distinguish unresolved filter classifications from unknown annotations.
Each target batch SHALL record required-family filter truth before reading the disjoint remaining annotation families through the shared deadline-aware reader.
An incomplete optional batch SHALL NOT demote a known filter match or reread its required families.

Admission SHALL precede preparation and dimension counts.
Each producer SHALL retain its ranking, state, upstream windows, and continuation meaning while applying dimension eligibility before its final output slice.
For a positive attention limit, the existing state-eligible ranked prefix SHALL be the dimension window, without overfetch or recursive refill.
Procedure approvals SHALL retain their bounded reference prefix.
Relation review SHALL filter before its final group or page break while retaining source and branch windows.
Vocabulary review SHALL authorize its original visible prefix of `limit` items through the existing wrapper rule before batching dimensions in that window.
It SHALL preserve the registry-proposal authorization exception, candidate order, and the unfiltered continuation tail.
Explicit uncapped listings SHALL prepare batches under the existing deadline; internal uncapped identity lookup SHALL acquire no annotations.

Existing totals SHALL retain their upstream meaning.
Dimension coverage SHALL name the evaluated window, known matches, unknown items, and unsearched remainder separately.
An underfilled or empty partial result SHALL describe only its evaluated window.
The producer SHALL record surfacing only for returned items.
Annotations SHALL be attached after identity computation, outside reasons, fingerprints, and relation `from` fields.
A registry edit that changes only dimensions SHALL NOT reopen a recorded review decision.

#### Scenario: Page filtering requires one unit to satisfy the expression

- **GIVEN** a page has one unit in dimension A and a different unit in dimension B
- **WHEN** review filtering requests A and B together
- **THEN** the page does not match unless one unit satisfies both predicates
- **AND** its unfiltered annotation may display both dimensions

#### Scenario: Unit findings retain their target grain

- **GIVEN** a partitioned review item has unanimous resolved reasons for one unit
- **WHEN** the listing annotates or filters it
- **THEN** it evaluates that unit only
- **AND** missing or conflicting unit addresses produce unknown membership instead of page membership

#### Scenario: Page reasons govern a folded item

- **GIVEN** a fused item has an explicit page reason and a unit reason
- **WHEN** its dimensions are prepared
- **THEN** its subject is the page
- **AND** its review reference and fingerprint remain unchanged

#### Scenario: Due findings on ID-bearing pages resolve their selected units

- **GIVEN** an ID-bearing page contains a due prediction or an aging question and a differently dimensioned sibling
- **WHEN** the public listing filters that finding by dimension
- **THEN** its producer address resolves the selected due unit through the page's stable memory reference
- **AND** sibling membership does not qualify the finding

#### Scenario: Unknown membership never matches a complement

- **GIVEN** a subject has unavailable definitions, structures, required edges, or a unit address
- **WHEN** a dimension filter uses negation or inequality
- **THEN** the item remains unknown and is omitted from known matches
- **AND** an unfiltered listing retains it with dependency diagnostics

#### Scenario: Nonsemantic work stays visible without fabricated dimensions

- **WHEN** import proposals, planning intents, or registry overlays are listed without dimension filters
- **THEN** they report not-applicable dimension status and remain visible
- **AND** no dimension predicate matches them

#### Scenario: Optional edges do not block a kind-only filter

- **GIVEN** a subject has complete selected units but its edge projection is unavailable
- **WHEN** a kind-only dimension filter matches it
- **THEN** the listing returns the exact match with unknown annotation status and no `dimensions` list
- **AND** coverage counts a known filter match and an unknown annotation separately
- **AND** an edge-dependent predicate, including negation, remains unresolved

#### Scenario: A ranked window can underfill honestly

- **GIVEN** matching attention items exist beyond the original positive-limit ranked prefix
- **WHEN** dimension filtering rejects some items in that prefix
- **THEN** the listing may return fewer items than its limit
- **AND** coverage reports the evaluated window and unsearched remainder without claiming the whole queue is empty
- **AND** excluded items acquire no surfacing record

#### Scenario: Optional annotation expiry preserves a proven family match

- **GIVEN** a batch has completed the families required by its filter
- **WHEN** the disjoint remaining annotation batch reaches its deadline
- **THEN** the listing retains the known filter match with unknown full annotation
- **AND** no required family is read again for annotation

#### Scenario: Withheld work cannot affect dimension coverage

- **GIVEN** a candidate is withheld from the caller
- **WHEN** review dimensions are prepared and counted
- **THEN** that candidate supplies no membership, unknown count, or evidence to the response

#### Scenario: A vocabulary continuation retains its original tail

- **WHEN** dimension filtering narrows a vocabulary review window with a continuation
- **THEN** the original visible prefix is authorized before dimensions are prepared, without selecting an out-of-prefix item
- **AND** an underfilled result reports partial coverage over that authorized window
- **AND** the continuation preserves the unfiltered candidate order and tail
- **AND** a changed filter does not reinterpret the continuation as an already filtered stream

#### Scenario: Annotation does not change a recorded decision

- **GIVEN** a review item has a recorded dismissal or snooze
- **WHEN** a registry edit changes only its selected dimensions
- **THEN** its reference, fingerprint, and recorded decision remain unchanged
- **AND** internal identity lookup performs no dimension preparation

#### Scenario: Other filter fields are rejected before queue work

- **WHEN** `list_review_items` receives a predicate outside `unit.dimension` and Boolean composition
- **THEN** the shared filter validation rejects it by field before the queue reads candidates
