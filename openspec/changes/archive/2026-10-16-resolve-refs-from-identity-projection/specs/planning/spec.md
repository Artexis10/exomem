## MODIFIED Requirements

### Requirement: Bound evidence executes under authorization-precedes-resolution

Every cross-profile evidence hop SHALL authorize before it resolves and SHALL resolve before any canonical Records source is parsed. The review SHALL resolve an evidence target only as a fully released Records manifest, SHALL authorize the named saved view before the query runs, and SHALL read observed numbers only from a non-withheld default-deny query envelope.

An evidence binding that cannot be executed SHALL be reported with exactly one bounded reason from `collection_unavailable`, `profile_mismatch`, `view_unavailable`, `query_unavailable`, `result_withheld`, or `budget_exhausted`. A missing collection and a withheld collection SHALL both report `collection_unavailable`, so the review cannot be used to probe for hidden collections. An unresolved binding SHALL contribute no observed numbers and SHALL NOT remove the item from the review.

The same rule SHALL govern every `motivation` reference. A motivation reference SHALL be authorized before its page is parsed, and a motivation reference that does not resolve to exactly one authorized page SHALL be reported with the single bounded reason `motivation_unavailable`. A reference the vault does not hold, a reference the vault holds in more than one page, a malformed reference, a reference whose page a governance decision blocks, and a reference whose page an access tier excludes SHALL be indistinguishable from one another in every part of the response: the same reason, no path, no title, no page count, no successor, and no separate counter. A reference SHALL be authorized before its uniqueness is decided, so that the presence of an unreleased page carrying the same identity cannot change the outcome the reader observes. Motivation references SHALL be resolved under the stable-memory-references requirement "Disclosure-neutral reference resolution": the review SHALL resolve them in one batch whose source is chosen from vault-wide state alone, so the resolution work a review performs SHALL NOT vary with which of the references the vault holds or with who may read the pages holding them. This bounds the resolution work only; it does not make the completion time of the review constant. Resolution SHALL create, rebuild, or refresh no index, cache, or sidecar.

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
