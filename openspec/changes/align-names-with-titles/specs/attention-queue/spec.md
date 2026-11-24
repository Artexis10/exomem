## ADDED Requirements

### Requirement: Name drift is one review item for the vault

While a rename-only `curation.relocate` would rename any page, the review surface SHALL carry one `name_drift` item for the vault. Detection and relocation SHALL derive a page's planned path with one shared function. The item's fingerprint SHALL change with the drifted set or its planned names. Its text SHALL state the counts, the time they were taken and the spelled-out `curation.relocate` call, with the item's `review_ref`, that renames the next batch.

#### Scenario: A backlog of stale names is one item

- **WHEN** 2,500 pages carry slug names while their titles say otherwise
- **THEN** the default review listing holds one `name_drift` item with the count and the call for the next batch

#### Scenario: The fingerprint follows the set

- **WHEN** one drifted page is renamed to its planned name and the item is computed again
- **THEN** the item's fingerprint differs from before and its count is one lower

#### Scenario: A vault whose names follow their titles has no item

- **WHEN** no page drifts
- **THEN** the review listing holds no `name_drift` item

#### Scenario: Stored counts say when they were taken

- **WHEN** the listing builds the item from the stored due-state row
- **THEN** the item's text states the time of the scan that the row's counts come from

### Requirement: The name-drift item keeps its identity

The `name_drift` item SHALL be anchored on the vault's top index, so that renaming drifted pages never changes its identity. A `review_ref` given before a batch SHALL resolve to the batch that is next when it is used. A vault without a top index SHALL have no item and no item reference.

#### Scenario: The item keeps its identity through its own renames

- **WHEN** a batch of drifted pages is renamed and pages still drift
- **THEN** the item keeps its `review_ref`
- **AND** a `review_ref` given before the batch names the batch that is next now

#### Scenario: A vault without a top index has no item

- **WHEN** pages drift and the vault has no top `index.md`
- **THEN** no `name_drift` item and no rename call by `review_ref` is offered
- **AND** `inspect_name_drift` still lists the drifted pages

### Requirement: The default listing serves the name-drift item from a stored row

The `name_drift` item SHALL join the default review listing after the other backlog queues. For a caller who may read every page, the default review listing SHALL build the item from the stored due-state row without a scan of the vault. A reconcile and the end of a renaming curation run SHALL rewrite that row.

#### Scenario: A dated obligation ranks first

- **WHEN** the vault owes a dated obligation and pages drift
- **THEN** the default review listing serves the dated obligation before the `name_drift` item

#### Scenario: The listing reads no page for the item

- **WHEN** the owner reads the default review listing and the due-state row holds the item
- **THEN** the listing carries the item and its call without a scan of the vault

### Requirement: The item's call renames the next batch

A `curation.relocate` call whose `review_ref` names the `name_drift` item SHALL rename the next batch of drifted pages, read afresh when the call runs, and not only the page the item is anchored on. Each page SHALL take the path that the shared function plans for it.

#### Scenario: One call renames the batch

- **WHEN** an agent proposes `curation.relocate` with the item's `review_ref` and the user approves the preview
- **THEN** every page in the batch takes its planned name, and one compensation restores every page

### Requirement: A refused page leaves the name-drift batch

For a `curation.relocate` by the item's `review_ref`, a page whose move or referrer rewrite the move contract refuses SHALL be left out of the plan and listed with its code. The call SHALL NOT fail for it. Pages that a caller names SHALL keep the refusal.

#### Scenario: A refused page does not fail the batch

- **WHEN** the next batch holds a page that the move contract refuses, and other pages
- **THEN** the relocation seals the other pages and lists the refused page with its code

### Requirement: A refused page waits until it changes

The runtime SHALL remember a refused page with its code, bound to its exact bytes and those of the scanned pages that link to it, outside the stored due-state row. It SHALL apply the memory only to callers with the refusing caller's audience, purpose and connector ceiling. The batch resolver SHALL NOT propose it again until one of those pages changes. No scan SHALL prepare a move to learn a refusal.

#### Scenario: An edit lets a refused page back in

- **WHEN** a page was refused and the owner then edits it, or edits or deletes a page that links to it
- **THEN** the next batch can hold the page again

### Requirement: Refused pages stay visible

The item's text and `inspect_name_drift` SHALL report the refused pages' count with their codes. When every drifted page that remains is refused, the item SHALL close.

#### Scenario: The item closes over refused pages

- **WHEN** every remaining drifted page is refused
- **THEN** the review listing holds no `name_drift` item
- **AND** `inspect_name_drift` lists each refused page with its code

### Requirement: A renaming run refreshes the name-drift item

When a curation run that renamed pages ends, the runtime SHALL refresh the `name_drift` item from a fresh scan, so the due-state block keeps the item while pages drift and drops it when none do.

#### Scenario: The backlog closes batch by batch

- **WHEN** an agent repeats the item's call, with the user's yes for each preview, until no page drifts
- **THEN** each finished run refreshes the item, and the item closes after the last batch

### Requirement: An oversized name-drift batch shrinks

Before the relocation prepares the next batch, it SHALL size the batch, from its pages and their links, to what one sealed plan holds with room to spare. When a sized batch's plan is still too large to seal, the relocation SHALL halve the batch, up to four times. It SHALL name the pages it left for the next batch in its summary.

#### Scenario: A batch too large to seal shrinks

- **WHEN** the next batch's plan would exceed the sealed-size bound
- **THEN** the relocation seals a smaller batch and lists the remaining pages under `deferred_to_next_batch`
- **AND** it prepares no step for a plan that it does not seal

### Requirement: Name drift judges only pages named by their title

A page that holds the collision-numbered name its title gets while another page holds the bare name SHALL NOT count as drift. A name that differs from the planned name only in case SHALL NOT count as drift, because a case-blind volume holds both as one file. Captured Sources, recaps, navigation pages and every type that the registry does not name by title SHALL be out of scope, for detection and for every relocation. An entity SHALL be renamed in its own folder.

#### Scenario: Dated captures and collision copies are not drift

- **WHEN** a Source with a dated name exists, and a page named `Title-2` with the title `Title` exists beside a page named `Title`
- **THEN** neither the Source nor `Title-2` is counted as drift

### Requirement: Pages without a plannable name are counted apart

A page with no usable title, a page whose rename this caller cannot plan, and a refused page SHALL be counted apart from the drifted pages and never proposed for a rename.

#### Scenario: A page with no usable title is never proposed

- **WHEN** a compiled page has no frontmatter title
- **THEN** it is counted apart from the drifted pages and no planned name is given for it

### Requirement: Drifted pages are listed in bounded pages

The read operation `inspect_name_drift` SHALL return the total number of drifted pages and one bounded page of rows: the page's ref, title, current name and planned name, in path order, with a cursor for the next page. A row for a page with no usable title SHALL say so and carry no planned name. A refused row SHALL carry its code. A scan that did not finish SHALL say so and SHALL NOT report a total.

#### Scenario: A listing pages through the backlog

- **WHEN** a caller reads the listing with a limit smaller than the total and then passes the returned cursor
- **THEN** the second page continues after the first and no row repeats

#### Scenario: An unfinished scan has no total

- **WHEN** the read budget stops the scan
- **THEN** the answer says the scan is incomplete and reports no total

### Requirement: A retitle returns the rename call

A write that changes a page's title so that its file name no longer follows it SHALL carry a `structure_suggestion` of kind `rename`. It SHALL hold the page's ref, current name, planned name and the spelled-out `curation.relocate` call for that one page, with no destination project. The plan SHALL be made from the new title without a scan of the vault.

#### Scenario: A retitle asks for the rename

- **WHEN** a page named for its title is retitled
- **THEN** the write response carries the `rename` suggestion with the call for that page

#### Scenario: An unrelated edit asks nothing

- **WHEN** a page's status changes and its title does not
- **THEN** the write response carries no `rename` suggestion

### Requirement: A write that leaves the name in step asks nothing

A write that leaves the title as it was, or whose new title leaves the name in step, SHALL carry no `rename` suggestion. The `structural_suggestions` envelope SHALL turn the suggestion off.

#### Scenario: A new title that matches the name asks nothing

- **WHEN** a page's title changes to the text that its file name already carries
- **THEN** the write response carries no `rename` suggestion
