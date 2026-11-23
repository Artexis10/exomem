## ADDED Requirements

### Requirement: Name drift is one review item for the vault

While any compiled or entity page has a file name that differs from the name the creation path gives its title, the review surface SHALL carry one `name_drift` item for the vault. Its fingerprint SHALL change with the drifted set or its planned names. Its text SHALL state the count and the spelled-out `curation.relocate` call, with the item's `review_ref`, that renames the next batch. It SHALL join the default review listing after the other backlog queues.

#### Scenario: A backlog of stale names is one item

- **WHEN** 2,500 pages carry slug names while their titles say otherwise
- **THEN** the default review listing holds one `name_drift` item with the count and the call for the next batch

#### Scenario: The fingerprint follows the set

- **WHEN** one drifted page is renamed to its planned name
- **THEN** the item's fingerprint differs from before and its count is one lower

#### Scenario: A vault whose names follow their titles has no item

- **WHEN** no page drifts
- **THEN** the review listing holds no `name_drift` item

### Requirement: The item's call renames the next batch

A `curation.relocate` call whose `review_ref` names the `name_drift` item SHALL rename the next batch of drifted pages, read afresh when the call runs, and not only the page the item is anchored on. When that batch's plan is too large to seal, the relocation SHALL halve the batch, up to four times, and SHALL name the pages it left for the next batch in its summary.

#### Scenario: One call renames the batch

- **WHEN** an agent proposes `curation.relocate` with the item's `review_ref` and the user approves the preview
- **THEN** every page in the batch takes its planned name, and one compensation restores every page

#### Scenario: A batch too large to seal shrinks

- **WHEN** the next batch's plan would exceed the sealed-size bound
- **THEN** the relocation seals a smaller batch and lists the remaining pages under `deferred_to_next_batch`

### Requirement: Name drift judges only pages named by their title

A collision number on an otherwise matching name SHALL NOT count as drift. Captured Sources and every type that the registry does not name by title SHALL be out of scope. A page with no usable title SHALL be counted apart and never proposed for a rename.

#### Scenario: Dated captures and collision copies are not drift

- **WHEN** a Source with a dated name and a page named `Title-2` for the title `Title` exist
- **THEN** neither is counted as drift

#### Scenario: A page with no usable title is never proposed

- **WHEN** a compiled page has no frontmatter title
- **THEN** it is counted apart from the drifted pages and no planned name is given for it

### Requirement: Drifted pages are listed in bounded pages

The read operation `inspect_name_drift` SHALL return the total number of drifted pages and one bounded page of rows: the page's ref, title, current name and planned name, in path order, with a cursor for the next page. A row for a page with no usable title SHALL say so and carry no planned name. A scan that did not finish SHALL say it is incomplete and SHALL NOT report a total.

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
