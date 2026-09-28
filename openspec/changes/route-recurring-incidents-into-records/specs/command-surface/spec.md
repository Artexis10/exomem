## MODIFIED Requirements

### Requirement: Compiled and Evidence writes may return one advisory Records routing hint
When a compiled-note mutation or an Evidence preserve commits, the system SHALL compare the written page's terms with the effective claims of every routing-target collection the caller may read. The written page's terms SHALL be its title, page tags and unit tags; its `type`, categories (page and unit), projects (`project` and `projects`) and tags SHALL be its facets, which decide `claims.match` predicates and SHALL NOT count as coverage terms. An Evidence write's facets SHALL be those its sidecar carries. When exactly one routing-target collection declares `claims.match` predicates that all hold on the page, that collection SHALL win with strength `strong` regardless of word coverage, carrying the sorted `matched_predicates`; when several do, the one holding strictly the most predicates wins, then the one with strictly the highest coverage, and a tie on both SHALL stay silent. When none does, coverage routing applies over every collection the page does not contradict: a page that states a `type` or `project` a collection's `match` declares, none of which that key allows, SHALL NOT route to that collection. `tags` and `category` SHALL never contradict, and a layer type such as an Evidence sidecar's `type: source` SHALL count as silence. Otherwise, when exactly one remaining collection is covered at or above the minimum coverage and strictly more than any other, the successful result SHALL include at most one `records_routing` advisory, projected into the default compact terminal as a bounded top-level field, carrying the collection's manifest path and title, the sorted matched terms (at most six), the collection's declared natural-key field names, and a `strength` of exactly `strong` or `moderate`. It SHALL NOT report a numeric confidence. It SHALL name no page other than a manifest the caller may read. It is advisory: it SHALL NOT alter `status`, `mutated`, `path`, `warnings_count`, mutation identity or replay behaviour, SHALL be absent rather than null when nothing qualifies, and a failure in its computation SHALL cost the caller the advisory and never the write. The runtime SHALL NOT append a record as a consequence of the advisory.

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
- **WHEN** the route is `moderate` or the observation is not failure-shaped
- **THEN** the advisory carries no disposition

#### Scenario: A malformed disposition is dropped whole
- **WHEN** the advisory carries an unknown disposition, a payload for another collection or action, an oversized payload or question, or an unknown key
- **THEN** the compact terminal carries no `records_routing` and the write is unchanged
