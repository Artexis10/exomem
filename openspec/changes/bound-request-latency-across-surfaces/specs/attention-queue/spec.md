## ADDED Requirements

### Requirement: Write due advice describes affected pages

A mutation's due-state advice SHALL count only affected pages and SHALL carry `scope: affected_paths`. Bootstrap and explicit review SHALL retain global counts. Stored terminals without scope SHALL retain their historical global meaning. Unknown scope SHALL omit the advisory without changing the mutation outcome.

Affected pages SHALL include dependency buckets that the mutation's existing delta updates. A batch SHALL carry the union of those pages.

The two scopes SHALL retain separate emission history. A local block MUST NOT suppress or re-enable an unchanged global block. First-surfaced records SHALL use the category and fingerprint actually delivered. Existing disclosure, triage, dispositions, and client conversation guidance SHALL remain in force.

#### Scenario: A write touches one page among unrelated due items

- **WHEN** that page has one eligible item and unrelated pages also have due items
- **THEN** the write counts only its affected pages and declares that scope
- **AND** bootstrap and explicit review still report their global view

#### Scenario: Global and local advice alternate

- **WHEN** a global block is delivered, followed by a local block, followed by the unchanged global block
- **THEN** the local delivery neither suppresses nor re-enables the unchanged global block

#### Scenario: A record updates a matched plan's due state

- **WHEN** a record's declared join updates one Planning page while unrelated Planning pages also have due items
- **THEN** the response includes the matched page's advice and excludes the unrelated pages
- **AND** a batch includes only the union of pages affected by its writes

#### Scenario: The projection changes before delivery recording

- **WHEN** a delivered row's fingerprint differs from a later projection
- **THEN** first-surfaced recording uses the delivered fingerprint

### Requirement: The id of a stored due entry binds from the entry

A decision that names a review item by its id SHALL bind the id before writer admission. For the owner, when a stored due-state entry has exactly this id, the entry SHALL bind the decision, and the service SHALL NOT read a review queue to bind it. Any other id SHALL bind from the review queues.

A restricted audience is served fingerprints recomposed over what it may see. Its ids SHALL bind only from the queues that this audience can read. A stored entry SHALL NOT answer a restricted audience.

#### Scenario: The keep-whole call of a note-cohesion question is decided

- **WHEN** a compiled write returns a note-cohesion question on a vault that holds its due-state projection, and the agent sends the dismissal that the response spells out
- **THEN** the service saves the decision, and the next write within the same step asks nothing
- **AND** the decision reads no page other than the item's own

#### Scenario: An id that no stored entry has binds from the review queues

- **WHEN** a decision names an id, and no stored entry has exactly this id
- **THEN** the review queues decide the id, as they did before this requirement

#### Scenario: A restricted caller names the id of a withheld page

- **WHEN** a restricted caller names the id of a stored entry whose page is withheld from it
- **THEN** the service gives the same refusal as for an id that does not exist

### Requirement: Review listings read a held review pass

For an unrestricted caller, the attention and activation views SHALL take their review findings from a pass that the service holds in process memory, per vault. An unrestricted caller is a bound caller from whom the release rules can withhold no page. The service SHALL store no row and no file for the pass. Filtering, ranking, review state and release decisions SHALL run in each request on copies of the held findings. A held pass SHALL be current while the vault's change generation, the UTC day and a maximum age hold.

Only the background worker of the serving runtime SHALL run a pass. A request SHALL never run one. A read with no held pass SHALL run its own audit of its own categories, as before, and SHALL ask the worker for a pass.

A read of a stale pass SHALL answer from it and ask for one background refresh. The stale answer SHALL drop the items of a page that no longer exists and the due items that the due-state projection no longer holds, and it SHALL mark with `page_changed` the items of a page that changed after the pass. When the due-state projection cannot be read, the stale answer SHALL keep the due items. The listing reply SHALL carry the pass time as `as_of` and SHALL say in `refreshing` whether a pass is due or running.

The worker SHALL run a pass when a read asks for one, and SHALL run the first pass of the process once the service has been quiet. Nothing else SHALL start a pass. Two passes SHALL start at least a set multiple of the last pass's duration apart, and that gap SHALL double with each failure in a row. A failed pass SHALL replace nothing. After a failed pass, a read of a stale pass SHALL keep the request path until a pass succeeds.

A decision on an item whose page changed after the pass SHALL be refused with `REVIEW_ITEM_CHANGED`.

A restricted caller, a bound collection writer and a call with no principal SHALL never read a held pass and SHALL have no pass held for them. Each SHALL keep the request path. A process with no worker and a vault with no live change generation SHALL hold nothing.

A decision that binds an id from the review queues SHALL read each queue whole and SHALL record no first-surfaced stamp.

#### Scenario: A listing after a completed pass reads no page

- **WHEN** an unrestricted caller lists the review queue and then decides one listed item by its id, after a completed pass
- **THEN** neither the listing nor the decision opens a vault page
- **AND** the listing equals a listing with no pass held

#### Scenario: A restricted caller never reads a held pass

- **WHEN** an unrestricted caller's pass is held and a restricted caller lists the review queue twice
- **THEN** each listing runs its own audit under that caller
- **AND** each listing equals a listing with no pass held

#### Scenario: A listing with no held pass does no more work than before

- **WHEN** an unrestricted caller lists one review queue while no pass is held
- **THEN** the request runs one audit of that queue's categories and no whole pass
- **AND** the worker runs the whole pass in the background

#### Scenario: One failing check fails only the queues that run it

- **WHEN** one review check fails, so that the worker's pass fails
- **THEN** a listing of a queue that does not run that check succeeds
- **AND** a listing of a queue that runs it reports the failure

#### Scenario: A serving runtime holds a pass before the first listing

- **WHEN** the serving runtime has started and the service has been quiet
- **THEN** the service runs one review audit pass and one activation scan in the background
- **AND** the first listing by an unrestricted caller runs no pass

#### Scenario: A stale pass is renewed only when a listing reads it

- **WHEN** a held pass is stale, because a page changed or the pass is older than the maximum age
- **THEN** the service runs no background pass until a listing reads the held pass

#### Scenario: A failed pass is tried again only when a listing asks

- **WHEN** the worker's pass fails and no listing arrives
- **THEN** the worker runs no further pass
- **AND** it runs one after a listing asks, once the gap has passed

#### Scenario: A stale pass is not read after a failed pass

- **WHEN** a held pass is stale and the worker's last pass failed
- **THEN** a listing runs its own audit, as with no pass held
- **AND** a listing reads a held pass again after a pass succeeds

#### Scenario: A decision on a changed item is refused

- **WHEN** an unrestricted caller edits a listed page and decides its item before the next pass
- **THEN** the service refuses the decision with `REVIEW_ITEM_CHANGED`

#### Scenario: A stale answer drops what a write resolved

- **WHEN** an unrestricted caller compiles a Source, deletes a page or moves a page, and lists the queue before the next pass
- **THEN** the listing holds no item for the compiled Source, the deleted page or the moved page's old path

#### Scenario: A stale answer marks an edited page

- **WHEN** an unrestricted caller edits a listed page and lists the queue before the next pass
- **THEN** the item of that page carries `page_changed`
- **AND** the reply carries the earlier pass's `as_of` and `refreshing: true`

#### Scenario: Many stale reads start one pass

- **WHEN** an unrestricted caller lists a stale queue several times while one background pass runs
- **THEN** the service runs one pass for them

#### Scenario: An item ranked anywhere binds

- **WHEN** a decision names an item ranked past the first 200 of its queue
- **THEN** the decision binds that item
- **AND** no item that the decision's reads saw gains a first-surfaced record

#### Scenario: An unknown id is refused without a pass

- **WHEN** a decision names an id that no queue holds, after a completed pass
- **THEN** the service refuses it with `ITEM_NOT_FOUND` and runs no review pass
