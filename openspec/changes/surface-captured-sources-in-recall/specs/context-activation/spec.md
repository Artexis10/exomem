## ADDED Requirements

### Requirement: Activation points to uncompiled captured material that a turn names
A served packet SHALL carry at most `EXOMEM_ACTIVATION_SOURCE_SLOTS` pointers to uncompiled captured pages: pages in the product's append-only raw-material trees (`Sources/`, `Evidence/`), excluding Episodes. A captured page SHALL qualify only through the carry phrase gate that compiled carries use, with the same corroboration, term budget and bounded catalogue. Its score SHALL take the same note-type factor that recall applies, including the media exemption. A captured page SHALL qualify only when no compiled page answers the same phrase more strongly after that factor. A phrase whose results fill the fetch window with captured pages names a family, not a document, and SHALL yield no captured pointer.

Each captured pointer SHALL use the existing `pointers[]` shape. Its `ref` SHALL be the captured page, its `title` the page title, its `reason` `requires_read`, and its `why` a fixed label stating that the material is uncompiled, attributed to its origin and not a conclusion. A captured page that a carried or resolved compiled page cites SHALL appear only as a pointer whose `anchor` is that compiled page. Captured pointers SHALL be charged against `max_chars` after all compiled material. A captured page SHALL never become an anchor, a unit or a current-state entry, and the `anchor` override SHALL continue to refuse it. A bound of 0 SHALL run no captured-material query and SHALL produce the packet the product produced before this requirement.

#### Scenario: A document only a captured page answers is pointed to
- **WHEN** a turn names a phrase that only an uncompiled captured page contains, and no anchor resolves
- **THEN** the packet is served with no anchor and one captured pointer carrying the uncompiled-material label

#### Scenario: A compiled note keeps precedence
- **WHEN** a compiled note and an uncompiled captured page answer the same phrase with similar strength
- **THEN** the compiled note is carried and no standalone captured pointer appears

#### Scenario: A cited captured page hangs from its note
- **WHEN** a carried compiled note cites the captured page that the turn also names
- **THEN** the packet carries exactly one pointer to that page, and its `anchor` is the compiled note

#### Scenario: Episodes and families yield nothing
- **WHEN** a turn's phrase matches only Episodes, or matches enough captured pages to fill the fetch window
- **THEN** the packet carries no captured pointer

#### Scenario: Bound 0 restores the previous packet
- **WHEN** `EXOMEM_ACTIVATION_SOURCE_SLOTS` is 0
- **THEN** every packet equals the packet the same vault produced without this requirement, ignoring only `freshness_key` and `index_generation`

### Requirement: Captured pointers cross the same release and egress guard
Captured pointers SHALL exist before the egress guard runs, and the guard SHALL decide them as it decides every other pointer: the connector boundary, raw-material protection floors, notice-level `withheld` markers and silent L0 omission all apply unchanged. Request admission SHALL admit captured candidates through the same admission object as compiled candidates. A captured page the caller's admission withholds SHALL take no row, slot or term statistic. A packet that resolved no anchor and lost every pointer, unit and current-state entry to the guard SHALL abstain: `withheld` when the release is at a notice level, and `unresolved` at L0.

#### Scenario: A withheld captured page is invisible at L0
- **WHEN** the only page that answers a turn is a captured page that the caller's audience withholds at L0
- **THEN** the packet abstains as `unresolved` and equals the packet from a vault without that page

#### Scenario: A withheld captured page is marked at a notice level
- **WHEN** the same page is withheld at a notice level
- **THEN** the packet abstains as `withheld` and names nothing from the page

### Requirement: The turn slice lists captured pointers as source leads
In the default `maps` packet, the turn slice SHALL list each standalone captured pointer that the caller may see as a lead of kind `source`, with the page title stripped of wikilink syntax and no description. A captured pointer attached to a compiled page SHALL NOT appear as a separate lead. Slice leads SHALL pass the slice's existing release guard unchanged.

#### Scenario: A captured document reaches the default packet
- **WHEN** a turn names a phrase that only an uncompiled captured page answers, under the default packet mode
- **THEN** `turn_slice.items` contains one `source` lead for that page
