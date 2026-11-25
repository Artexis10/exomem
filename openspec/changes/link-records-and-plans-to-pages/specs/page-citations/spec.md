# page-citations

## ADDED Requirements

### Requirement: One read seam answers which rows and plan items cite a page

Exomem SHALL answer which Records rows and Planning items cite a page through one function, `inbound_citations(ref, caller, limit, more)`. It SHALL read the derived `item_references` index in indexed reads, never one per page, per collection or per citing row. It SHALL return at most `limit` citations (default 20, maximum 100), the count the caller may see, and a continuation when more remain. No other code path SHALL compute inbound citations.

#### Scenario: A page cited by two rows and a plan item

- **WHEN** two inventory rows link to a page and one plan item's `results` link to it
- **THEN** `inbound_citations` for that page returns the three citations and the count 3

#### Scenario: Many citing rows cost the reads of a few

- **WHEN** a row-level policy decides each row of a collection, and 40 of its rows cite a page instead of 3
- **THEN** the read runs the same statements, deciding the citing rows in one batch for the collection

### Requirement: A read counts row-decided citations up to a configured limit

A citing row that a row-level policy releases on its own SHALL be counted up to `EXOMEM_CITATION_COUNT_LIMIT` visible such rows (default 1,000; a batch shares the limit equally among its pages). When more visible such rows cite the page, the count SHALL be the limit, marked `count_complete: false`; otherwise it SHALL be exact. A continuation SHALL still reach every visible citation. Where the count stops SHALL depend on visible rows only.

#### Scenario: Past the limit the count is a lower bound

- **WHEN** a row-level policy decides each row of a collection, and more of its visible rows cite a page than the limit
- **THEN** the reply counts the limit with `count_complete: false`, and following `more` lists every visible citing row

#### Scenario: Exactly the limit is an exact count

- **WHEN** a row-level policy decides each row of a collection, and exactly the limit of its visible rows cite a page
- **THEN** the reply counts the limit with `count_complete: true`

#### Scenario: A withheld row never moves the cut

- **WHEN** two pages have the same visible citing rows in number, and only one of them is also cited by a withheld row
- **THEN** both replies carry the same count and the same `count_complete`, at the limit and below it

#### Scenario: A reader whom no row rule names decides no row

- **WHEN** a scope names one row of a collection, and its only rule applies to the external audience
- **THEN** the owner's read decides no row of that collection and reports the exact count

#### Scenario: A recall hit says when its count is a lower bound

- **WHEN** a recall or search hit's page is cited past the limit
- **THEN** the hit carries `cited_by_count_complete: false` next to `cited_by_count`

### Requirement: Citations are visible only when both ends are visible

A citation SHALL be returned and counted only when the caller may read the citing row's collection and field and also the cited page. A withheld page SHALL produce the same answer as a missing one. A withheld row SHALL be absent from the items and from the count.

#### Scenario: A withheld collection stays invisible

- **WHEN** a collection that the caller cannot read holds a row that links to the page
- **THEN** that row is absent from the items and from the count, and nothing in the reply tells it apart from a vault without that collection

#### Scenario: A withheld page answers like a missing one

- **WHEN** a caller asks for the citations of a page that it may not read, and its twin asks for a page id that does not exist
- **THEN** both replies are identical, while the index warms as well as once it is built

### Requirement: Withheld citing rows do not change the reply's state, within one bound

The number of withheld rows that cite a page SHALL NOT change the reply's state. One bound applies: withheld rows that a read examines one at a time. These are rows that a scope names one by one by ref, and the rows of a collection that no row filter describes. Past about 25,000 such withheld rows in one collection, a read of a page that they cite MAY run past the query deadline and read `unavailable`.

#### Scenario: Thousands of withheld citing rows leave the reply ready

- **WHEN** under the shipped query deadline, a page is cited by three visible rows and 5,000 rows withheld from the caller, and its twin by three visible rows only
- **AND** the collection keeps its rows in one summary, or keeps one view file a row that no path selector tells apart
- **THEN** both replies are `ready` with the count 3 and `count_complete: true`

#### Scenario: Past the bound, only the cited page reads as unavailable

- **WHEN** under the shipped query deadline, a scope names 100,000 rows of one collection by ref, and each of them cites a page
- **THEN** the read of that page MAY be `unavailable`, while a page that no such row cites reads `ready`

### Requirement: Each citation names its row, field and declared relation

Each citation SHALL carry the profile, the collection title, the citing row's canonical ref and label, the field path, and the field's declared `relation` when the manifest names one.

#### Scenario: A relation is reported only when declared

- **WHEN** one link field declares `relation: cites` and another declares none
- **THEN** citations through the first carry `cites`, and citations through the second carry no relation

### Requirement: The citation index is derived from the rows

The `item_references` index SHALL hold one entry per distinct canonical ref in each top-level `link` field value of each store-backed row. A row whose field also holds values that are not canonical refs SHALL have one tally entry that counts them and that no citation read can find. Only the canonical item transaction and the index backfill SHALL write those entries. The index SHALL be rebuildable from the rows alone. A title or path value SHALL NOT be indexed.

#### Scenario: A rebuilt index matches the rows

- **WHEN** the derived index is dropped and rebuilt
- **THEN** every canonical link ref in the rows has its entry again, each row that holds other values has its tally again, and no other entry exists

### Requirement: Citation coverage is reported honestly

While a link field's backfill is incomplete, `inbound_citations` SHALL report the state `warming`, never an empty list. A collection's `inspect` SHALL report, per link field, the number of indexed refs (each distinct ref once per row) and the number of non-canonical values that are not indexed. A link field that the index never holds, such as one of a derived collection, SHALL report the state `not_indexed` and count every value as not indexed.

#### Scenario: The index is warming

- **WHEN** a link field's backfill has not finished and a caller reads a page's citations
- **THEN** the reply has the state `warming` and no empty list that a caller could read as "no citations"

#### Scenario: Title values are reported, not hidden

- **WHEN** a collection holds seven link values that are page titles
- **THEN** its inspect reports the seven values as not indexed

### Requirement: Inspect reads a built field's counts from the index

When a link field is built and every row releases alike for the caller, `inspect` SHALL read both of the field's counts from the index in a fixed number of statements, whatever the number of rows.

#### Scenario: Inspect counts cost the same at any size

- **WHEN** an owner inspects a collection whose link fields are built, once with 3 rows and once with 300
- **THEN** both inspections run the same number of statements, and each reports exact counts

### Requirement: A link field may declare its relation from the registry

A `link` field in a Records or Planning manifest MAY declare `relation`, naming a relation in the relation registry whose `origins` include `collection_link`. Create and revise SHALL refuse any other key, naming the admissible relations from the registry. Code SHALL NOT name a relation key.

#### Scenario: An undeclared origin is refused

- **WHEN** a manifest declares `relation: kin_of` on a link field and `kin_of` lacks the origin `collection_link`
- **THEN** create refuses and lists the relations whose origins include `collection_link`

### Requirement: A page read lists its citations

`read_page` SHALL return `cited_by` with the items, count, `count_complete`, continuation and state from `inbound_citations` for the page it reads. A continuation SHALL page through the citations without re-reading the page.

#### Scenario: A lent cable's page shows the cable's row

- **WHEN** an inventory row whose `page` links to a person's page records a cable as lent to that person
- **THEN** reading that page lists the row in `cited_by` with count 1

### Requirement: Recall page hits carry citation context from one batched read

`recall_context` and `search_memory` SHALL add `cited_by_count` and at most three citing items to each of the top ten page hits of a response. One batched `inbound_citations` call SHALL serve all those hits. A hit whose citations are warming or unavailable SHALL carry `cited_by_state` instead of a count. Passage-level results are out of scope. Rows and plan items SHALL NOT become recall hits because of a citation.

#### Scenario: Recall finds a page and shows the state that cites it

- **WHEN** recall returns a page that two rows cite
- **THEN** the hit carries `cited_by_count` 2 and both rows

#### Scenario: Ten hits cost the reads of one

- **WHEN** a recall response holds ten page hits
- **THEN** the response reads the citation index once for all ten hits, with one probe and one statement per round, never once per hit

### Requirement: The duplicate-fact detector compares only linked pairs

A maintenance job SHALL compare each string field value of a row with the units of the page that the row's link field resolves to. It SHALL consider only resolved row-to-page links, and it SHALL run incrementally when a row or a page changes.

#### Scenario: A row with no resolved link is not compared

- **WHEN** a row's link value is an unresolved title
- **THEN** the job does not compare that row

### Requirement: The duplicate-fact detector proposes, and the owner decides

A unit that contains the normalised field value and mentions the row's label SHALL open one review item. The item SHALL propose the registry's `duplicates` relation between the row and the unit, naming the field, the unit and both values. The job SHALL NOT change any page or row. Only an accepted review decision SHALL change one.

#### Scenario: The same location is written twice

- **WHEN** a row records `location: home office`, and the page that the row links to has a unit that says the item is in the home office
- **THEN** the job opens one review item that proposes `duplicates` between the row and that unit, and changes nothing else
