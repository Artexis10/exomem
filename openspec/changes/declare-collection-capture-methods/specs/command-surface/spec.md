## MODIFIED Requirements

### Requirement: Compiled and Evidence writes may return one advisory Records routing hint
When a compiled-note mutation or an Evidence preserve commits, the system SHALL compare the written page's terms with the effective claims of every routing-target collection the caller may read. The written page's terms SHALL be its title, page tags and unit tags; its `type`, categories (page and unit), projects (`project` and `projects`) and tags SHALL be its facets, which decide `claims.match` predicates and SHALL NOT count as coverage terms. An Evidence write's facets SHALL be those its sidecar carries. When exactly one routing-target collection declares `claims.match` predicates that all hold on the page, that collection SHALL win with strength `strong` regardless of word coverage, carrying the sorted `matched_predicates`; when several do, the one holding strictly the most predicates wins, then the one with strictly the highest coverage, and a tie on both SHALL stay silent. When none does, coverage routing applies over every collection the page does not contradict: a page that states a `type` or `project` a collection's `match` declares, none of which that key allows, SHALL NOT route to that collection. `tags` and `category` SHALL never contradict, and a layer type such as an Evidence sidecar's `type: source` SHALL count as silence. Among those collections, any that holds at least one of its declared predicates on the page and is itself a valid word route (covered at or above the minimum coverage, with a subject signal) SHALL rank ahead of every collection that holds none, by the number of predicates held and then by coverage, and a tie on both SHALL stay silent; a partly held declaration SHALL order candidates and SHALL NOT make a route `strong`. With no such collection, when exactly one remaining collection is covered at or above the minimum coverage and strictly more than any other, the successful result SHALL include at most one `records_routing` advisory, projected into the default compact terminal as a bounded top-level field, carrying the collection's manifest path and title, the sorted matched terms (at most six), the collection's declared natural-key field names, and a `strength` of exactly `strong` or `moderate`. When the routed collection declares `capture.method` and the caller may read the page it names, the advisory SHALL also carry `method`, a bounded `title` and a `read_ref`; otherwise `method` SHALL be absent, and a method that names no page and one that names a page withheld from the caller SHALL be indistinguishable. A failure to resolve the method SHALL cost the advisory its `method` and nothing else. A strong route that a full `claims.match` declaration produced SHALL carry a disposition whatever the page's type, category or other facet values; any other strong route SHALL carry one only for a failure-shaped observation. When a `file`, `append_occurrence` or `ask` disposition accompanies a `method`, its instruction SHALL tell the agent to read `method.read_ref` and follow it when it acts. It SHALL NOT report a numeric confidence. It SHALL name no page other than a manifest the caller may read. It is advisory: it SHALL NOT alter `status`, `mutated`, `path`, `warnings_count`, mutation identity or replay behaviour, SHALL be absent rather than null when nothing qualifies, and a failure in its computation SHALL cost the caller the advisory and never the write. The runtime SHALL NOT append a record as a consequence of the advisory.

#### Scenario: A preserved publication artifact names its collection
- **WHEN** an Evidence artifact whose tags and description carry a platform and an account value that one collection's effective claims cover is preserved
- **THEN** the write commits unchanged and the compact response carries one `records_routing` advisory naming that collection and the matched terms

#### Scenario: A compiled observation names its collection
- **WHEN** a compiled note's units carry tags covering exactly one collection's claims
- **THEN** the committed response carries the advisory with the same shape as for Evidence

#### Scenario: Ties and misses stay silent
- **WHEN** two collections are covered equally, or no collection reaches the minimum coverage
- **THEN** the response contains no `records_routing` key

#### Scenario: A withheld collection is never named
- **WHEN** the only covering collection is withheld from the caller
- **THEN** the response contains no `records_routing` key and the write is unchanged

#### Scenario: Advisory failure leaves the write committed
- **WHEN** the routing computation raises
- **THEN** the mutation remains committed with its existing terminal and no `records_routing`, warning or error is produced

#### Scenario: A failure note routes by declared membership
- **WHEN** a compiled note with `type: failure` and `projects: [example-product]` is written and exactly one collection declares `match: {type: [failure], project: [example-product]}`
- **THEN** the advisory names that collection with strength `strong` and its matched predicates, even when no word is shared

#### Scenario: Another product's note does not reach this product's collection
- **WHEN** a note with `project: gadget` titled "Login sync broke" is written and a collection declaring `match: {project: [widget]}` claims `login` and `sync`
- **THEN** no advisory names that collection, while a note that names no project may still route there by shared words

#### Scenario: A partly declared collection outranks a wider undeclared one
- **WHEN** a failure note that names no project shares two claim words with a collection declaring `match: {type: [failure], project: [example-product]}` and three with a collection declaring no `match`
- **THEN** the advisory names the declaring collection with strength `moderate` and no `matched_predicates`, where before it named the undeclared one

#### Scenario: A partly held declaration still needs its own word route
- **WHEN** the declaring collection of that note is covered below the minimum coverage, or shares only claim words that are common to every collection
- **THEN** it does not outrank the undeclared collection, and the route is the one that coverage alone gives

#### Scenario: Partial declarations tie silently
- **WHEN** two collections hold the same number of declared predicates and the same coverage
- **THEN** the response contains no `records_routing` key

#### Scenario: Open keys and layer types do not exclude
- **WHEN** a page tagged `[sync, outage]` shares claim words with a collection declaring `match: {tags: [incident]}`, or an Evidence artifact shares claim words with a collection declaring `match: {type: [failure], project: [widget]}`
- **THEN** each routes to that collection by coverage

#### Scenario: The narrower declaration wins
- **WHEN** a failure note satisfies one collection declaring `type` and `project` and another declaring only `type` that shares more words with it
- **THEN** the advisory names the collection holding two predicates, and two collections holding as many predicates with equal coverage stay silent

#### Scenario: Prose claims do not capture unrelated pages
- **WHEN** a collection's declared terms are prose and a page shares only function words with them
- **THEN** no advisory names that collection

#### Scenario: Maximal prominence files a strong failure route
- **WHEN** a strong route of a failure-shaped observation commits at effective capture prominence `maximal`
- **THEN** the compact advisory carries `disposition: file`, an instruction to perform it without asking, and a `record_memory` append payload for the named collection whose item is filled from the note where the manifest's fields map and cites the note in `sources`; performing that payload through `record_memory` succeeds under ordinary governance

#### Scenario: Balanced prominence asks once
- **WHEN** the same strong failure route commits at `balanced`
- **THEN** the advisory carries `disposition: ask` and one precomposed yes/no question without system vocabulary, and a later write of the same signal version carries `disposition: hold` and no question

#### Scenario: Light and off hold
- **WHEN** the route commits at `light` or `off`
- **THEN** the advisory carries `disposition: hold` with no payload and no question, and the observation remains in the review surfaces

#### Scenario: A recurrence appends an occurrence
- **WHEN** the observation matches an existing item of the same collection by natural-key value or by a bounded symptom overlap
- **THEN** the disposition is `append_occurrence` carrying a complete `record_memory` update, with the item's key, its sources extended by the note and both hash guards, not a new item; performing that payload exactly as returned adds the occurrence

#### Scenario: Editing a filed note does not act again
- **WHEN** a note that a record already cites, or whose entry the owner dismissed or snoozed, is edited
- **THEN** the advisory carries no `file`, `append_occurrence` or `ask` disposition, and no item citing the note is proposed as its recurrence; an edited note whose entry is still open and untriaged keeps its disposition

#### Scenario: An oversized disposition costs only itself
- **WHEN** the question or payload a disposition would carry exceeds the compact terminal's bounds
- **THEN** the advisory is delivered without a disposition, and nothing is recorded as asked

#### Scenario: A moderate route keeps the plain advisory
- **WHEN** the route is `moderate`, or it is `strong` by a natural-key value alone for an observation that is not failure-shaped
- **THEN** the advisory carries no disposition

#### Scenario: A declared route acts whatever the note's type
- **WHEN** a note of a type no failure vocabulary names fully satisfies a collection's `match` and commits at `maximal`
- **THEN** the advisory carries `disposition: file` and the ready `record_memory` payload for that collection, and the same note at `balanced` carries `disposition: ask`

#### Scenario: A natural-key route keeps the failure-shaped gate
- **WHEN** a note that no `match` produced routes `strong` by a natural-key value and is not failure-shaped
- **THEN** the advisory carries no disposition

#### Scenario: A routed collection names its capture method
- **WHEN** a note routes, `strong` or `moderate`, to a collection whose manifest declares `capture.method` naming a page the caller may read
- **THEN** the advisory carries `method` with that page's title, without link markup, and a `read_ref` the caller can read
- **AND** a `file`, `append_occurrence` or `ask` instruction tells the agent to read `method.read_ref` and follow it when it acts, while a `hold` instruction does not

#### Scenario: An unresolved or withheld method costs only the method
- **WHEN** the declared page does not exist, or exists and is withheld from the caller
- **THEN** the advisory has the same collection, strength and disposition as without a declaration, carries no `method`, and does not differ between the two cases

#### Scenario: A malformed method is dropped whole
- **WHEN** the advisory carries a `method` with an unknown key, an empty or oversized title or reference, or a non-mapping value
- **THEN** the compact terminal carries no `records_routing` and the write is unchanged

#### Scenario: A malformed disposition is dropped whole
- **WHEN** the advisory carries an unknown disposition, a payload for another collection or action, an oversized payload or question, or an unknown key
- **THEN** the compact terminal carries no `records_routing` and the write is unchanged
